"""Build a self-contained Windows application from explicit clean inputs."""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "artifacts/windows-build"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))


def app_version():
    module = ast.parse((ROOT/"desktop_pet/__init__.py").read_text(encoding="utf-8-sig"))
    return next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets))


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            result.update(chunk)
    return result.hexdigest()


@contextmanager
def short_compiler_path(payload):
    """Inno 6's file reader needs short source paths, without moving any files."""
    if sys.platform != "win32":
        yield payload
        return
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    subst = windows/"System32/subst.exe"
    logical = ctypes.windll.kernel32.GetLogicalDrives()
    letter = next((char for char in "ZYXWVUTSRQPONMLKJIHGF" if not logical & (1 << (ord(char)-65))), None)
    if letter is None:
        raise RuntimeError("No temporary compiler drive mapping is available")
    drive = letter+":"
    subprocess.run([str(subst), drive, str(payload)], check=True)
    try:
        yield Path(drive+"\\")
    finally:
        device = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.QueryDosDeviceW(drive, device, len(device)):
            expected = "\\??\\"+str(payload).rstrip("\\")
            if device.value.rstrip("\\").casefold() == expected.casefold():
                subprocess.run([str(subst), drive, "/D"], check=True)


def copy(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


ANIMATION_FIELDS = {"schema_version", "name", "canvas", "frame_count", "fps", "loop", "completion",
    "transition_ms", "return_transition", "anchor", "duration_ms", "frames", "frame_pattern", "start_index",
    "complete", "qa_passed"}


def stage_assets(target):
    copy(ROOT/"shared/reasoning/profiles.json", target/"shared/reasoning/profiles.json")
    assets = ROOT/"assets"
    copy(assets/"whale.png", target/"assets/whale.png")
    copy(assets/"branding/windows/whale-maid.ico", target/"assets/branding/windows/whale-maid.ico")
    for folder in ("expressions", "ui", "character/static", "character/eyes", "character/interactions"):
        for path in (assets/folder).rglob("*"):
            if path.is_file() and path.suffix.lower() in {".png", ".svg", ".json"}:
                if any(word in path.name.lower() for word in ("report", "inventory", "production", "qa.")):
                    continue
                copy(path, target/"assets"/path.relative_to(assets))
    for name in ("master.png", "canvas.json", "shared_layout.json"):
        copy(assets/"character"/name, target/"assets/character"/name)
    for directory in sorted((assets/"character/animations").iterdir()):
        if not directory.is_dir() or not (directory/"animation.json").is_file():
            continue
        for path in directory.rglob("*.png"):
            copy(path, target/"assets"/path.relative_to(assets))
        original = json.loads((directory/"animation.json").read_text(encoding="utf-8-sig"))
        value = {key: original[key] for key in ANIMATION_FIELDS if key in original}
        if isinstance(original.get("origin"), dict):
            value["origin"] = {key: original["origin"][key] for key in ("kind", "synthetic") if key in original["origin"]}
        path = target/"assets/character/animations"/directory.name/"animation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def stage_python_licenses(payload):
    destinations = payload/"licenses/python-packages"
    rows = []
    sites = [Path(sys.prefix)/"Lib/site-packages", payload/"_internal/runtime/python/Lib/site-packages"]
    for scope, site in zip(("desktop", "worker"), sites):
        for distribution in importlib.metadata.distributions(path=[str(site)]):
            name = distribution.metadata.get("Name", "unknown")
            version = distribution.version
            destination = destinations/scope/(name+"-"+version)
            saved = []
            for item in distribution.files or ():
                relative = Path(str(item))
                if "__pycache__" in relative.parts or relative.suffix.lower() in {".py", ".pyc", ".pyo", ".pyd", ".dll"}:
                    continue
                if not any(word in str(relative).lower() for word in ("license", "copying", "notice", "authors")):
                    continue
                source = Path(distribution.locate_file(item))
                if not source.is_file() or source.stat().st_size > 8*1024*1024:
                    continue
                # Metadata can contain ../ paths. The notice path stays local.
                safe = Path(*[part for part in relative.parts if part not in ("..", ".")])
                copy(source, destination/safe)
                saved.append(safe.as_posix())
            rows.append({"scope": scope, "name": name, "version": version,
                         "license_expression": distribution.metadata.get("License-Expression", ""),
                         "license": distribution.metadata.get("License", "")[:3000], "notice_files": saved})
    destinations.mkdir(parents=True, exist_ok=True)
    (destinations/"INDEX.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    copy(Path(sys.base_prefix)/"LICENSE.txt", payload/"licenses/PYTHON-DESKTOP-LICENSE.txt")
    copy(ROOT/"desktop_pet/ui/web_chat/THIRD_PARTY_NOTICES.txt", payload/"licenses/FRONTEND-THIRD-PARTY-NOTICES.txt")


def shorten_notice_names(directory, *, originals=None):
    """Keep offline notices usable in ordinary Windows extraction/install paths."""
    replacements = {}
    source = Path(originals) if originals is not None else directory
    for path in source.rglob("*"):
        if path.is_file() and len(path.name) > 100:
            short = path.stem[:65]+"-"+hashlib.sha256(path.name.encode()).hexdigest()[:12]+path.suffix
            old, new = path.relative_to(source).as_posix(), path.with_name(short).relative_to(source).as_posix()
            if (directory/old).is_file():
                (directory/old).rename(directory/new)
            replacements[old] = new
            replacements[Path(old).name] = Path(new).name
    def rewrite(value):
        if isinstance(value, str): return replacements.get(value, value)
        if isinstance(value, list): return [rewrite(item) for item in value]
        if isinstance(value, dict): return {key: rewrite(item) for key, item in value.items()}
        return value
    for path in directory.rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        result = rewrite(value)
        if result != value:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    def replace_href(match):
        target = match.group(2)
        link, separator, anchor = target.partition("#")
        replacement = replacements.get(link)
        return match.group(0) if replacement is None else match.group(1)+replacement+separator+anchor+match.group(3)
    for path in directory.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        result = re.sub(r'''(\bhref\s*=\s*["'])([^"']+)(["'])''', replace_href, text)
        if result != text:
            path.write_text(result, encoding="utf-8")


def write_guides(payload, version):
    guide = """鲸伴

首次使用
1. 运行「鲸伴安装.exe」完成安装，从开始菜单启动「鲸伴」。
2. 免安装版：先完整解压 ZIP，再运行文件夹内「鲸伴.exe」。请保留 _internal 文件夹。
3. 桌宠出现后，双击托盘图标或点击角色打开聊天。关闭聊天窗口会保留桌宠；退出请用托盘菜单「退出鲸伴」。
4. 要聊天或使用电脑 Agent，请在「设置」填写自己的 API 地址、模型和密钥。此包没有预置任何人的账号或密钥。
5. 象棋、五子棋由随包本地引擎计算；不配置聊天 API 也可以对弈。
6. 新配置默认开启屏幕观察和画面发送，配置视觉 API 后可自动分析；可在设置中关闭。摄像头、手机连接和自动记忆仍按需开启。已有配置不会被覆盖。
7. 网页右键提问：可加载 browser-extension/whale-ask 扩展；首次使用运行 tools/install_browser_ask.ps1 -Apply 配置本机连接。此步骤无需 Python，也不会自动更改浏览器设置。

运行环境
- Windows 10 2004 或更新的 64 位 Windows，推荐 Windows 11。
- 已内置 Python、Node、Qt 和本地棋类引擎，无需自行安装开发环境。
- 五子棋随包为上游 SSE4.1 基线版本；不要求 AVX2。非常旧的 CPU 不在本次覆盖范围。

功能说明
- 桌宠动作、聊天、角色表情、棋局、屏幕/摄像头设置、任务助手及手机连接组件。
- API 与网络服务由用户自行配置；本地 Embedding 模型由设置中的显式初始化下载，不随包携带。
- 文档转换依赖外部 Office/LibreOffice 时，会显示实际可用状态；没有内置完整办公套件。
- 生成项目的任意代码运行仍关闭。
- Windows 安装包尚未使用发行者代码签名证书签名。

数据与卸载
配置、聊天记录和记忆写入当前 Windows 用户的本地数据目录，不写入安装文件夹。
更新/卸载应用不会主动删除这些用户数据。请勿把自己的数据目录或密钥转发给其他人。

反馈问题时请附：版本号、Windows 版本、操作步骤、预期/实际现象及截图。
本地诊断可从设置查看；发送反馈前请检查是否包含自己的对话或文件信息。

随包 licenses 与 sources 提供第三方许可证及棋类引擎对应源码。
"""
    (payload/"使用说明.txt").write_text(guide, encoding="utf-8-sig")


def audit_payload(payload):
    prohibited_names = {"settings.ini", "pyvenv.cfg", ".env", "devices.json", "runtime.json", ".git-credentials"}
    prohibited_parts = {".git", ".venv", "agent_workspace", "artifacts", "__pycache__"}
    records = []
    for path in sorted(payload.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(payload)
        if (path.name in prohibited_names or path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".psd", ".mp4"}
                or any(part in prohibited_parts for part in relative.parts)):
            raise ValueError("A development or user data file reached the release: "+relative.as_posix())
        if path.name in {"PACKAGE-MANIFEST.json", "validation-approved.json"}:
            continue
        records.append({"path": relative.as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    for required in ("鲸伴.exe", "鲸伴网页桥.exe", "_internal/PySide6/QtWebEngineProcess.exe", "_internal/runtime/python/python.exe",
                     "_internal/runtime/node/node.exe", "_internal/assets/whale.png",
                     "_internal/assets/engines/rapfi/rapfi.exe", "_internal/assets/engines/pikafish/pikafish.exe"):
        if not (payload/required).is_file():
            raise ValueError("Missing release component: "+required)
    return records


def assemble(version):
    built = WORK/"pyinstaller_dist/鲸伴"
    workers, engines = WORK/"worker_distribution", WORK/"engine_distribution"
    for path in (built/"鲸伴.exe", workers/"runtime/manifest.json", engines/"sources/SOURCE_MANIFEST.json"):
        if not path.is_file():
            raise FileNotFoundError("Release staging is incomplete: "+str(path))
    if not json.loads((workers/"runtime/manifest.json").read_text(encoding="utf-8"))["memory_optional_installed"]:
        raise ValueError("Optional worker dependency installation has not finished")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    payload = WORK/"candidates"/stamp/"鲸伴"
    shutil.copytree(built, payload)
    resource = payload/"_internal"
    shutil.copytree(workers, resource, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "__image_snapshots__", "__snapshots__", "__tests__"))
    stage_assets(resource)
    shutil.copytree(ROOT/"browser-extension/whale-ask", payload/"browser-extension/whale-ask",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("install_browser_ask.ps1", "uninstall_browser_ask.ps1"):
        copy(ROOT/"tools"/name, payload/"tools"/name)
    shutil.copytree(engines/"assets/engines", resource/"assets/engines", dirs_exist_ok=True)
    for folder in ("licenses", "sources"):
        shutil.copytree(engines/folder, payload/folder, dirs_exist_ok=True)
    stage_python_licenses(payload)
    shorten_notice_names(payload/"licenses")
    write_guides(payload, version)
    records = audit_payload(payload)
    manifest = {"product": "鲸伴", "version": version, "platform": "Windows-x64",
                "files": records, "file_count": len(records), "bytes": sum(row["bytes"] for row in records),
                "developer_configuration_included": False, "signed": False}
    (payload/"PACKAGE-MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (WORK/"candidate.json").write_text(json.dumps({"payload": str(payload), "version": version}, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def refresh_workers(payload):
    """Refresh external worker scripts before the final candidate is approved."""
    source = WORK/"worker_distribution"
    for folder in ("desktop_pet", "agent_backend"):
        for path in (source/folder).rglob("*.py"):
            if "__pycache__" not in path.parts:
                copy(path, payload/"_internal"/path.relative_to(source))
    manifest = json.loads((payload/"PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    records = audit_payload(payload)
    manifest.update(files=records, file_count=len(records), bytes=sum(row["bytes"] for row in records))
    (payload/"PACKAGE-MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def stage_current_worker_sources():
    """Keep reusable runtime dependencies, but always package the current source."""
    from tools.packaging.worker_runtime import _copy_source
    target = WORK / "worker_distribution"
    if not (target / "runtime/manifest.json").is_file():
        raise FileNotFoundError("Prepare the standalone worker runtime before building")
    _copy_source(ROOT / "agent_backend", target / "agent_backend", {".py", ".mjs", ".js", ".ps1", ".json", ".txt"})
    _copy_source(ROOT / "desktop_pet", target / "desktop_pet", {".py"})
    copy(ROOT / "shared/reasoning/profiles.json", target / "shared/reasoning/profiles.json")


def build_installer(payload, version):
    compiler = WORK/"tooling/inno/ISCC.exe"
    language = WORK/"tooling/ChineseSimplified.isl"
    if not compiler.is_file() or not language.is_file():
        raise FileNotFoundError("The verified installation compiler and Chinese translation are required")
    output = WORK/"installer_candidate"
    output.mkdir(parents=True, exist_ok=True)
    target = output/"鲸伴安装.exe"
    fingerprint = {"manifest": sha256(payload/"PACKAGE-MANIFEST.json"),
                   "installer_script": sha256(ROOT/"tools/packaging/jingban.iss"), "language": sha256(language)}
    record = output/"build.json"
    if target.is_file() and record.is_file():
        prior = json.loads(record.read_text(encoding="utf-8"))
        if prior.get("inputs") == fingerprint and prior.get("sha256") == sha256(target):
            return target
    with short_compiler_path(payload) as compiler_payload:
        subprocess.run([str(compiler), "/Qp", f"/DAppVersion={version}", f"/DPayloadDir={compiler_payload}", f"/DReleaseDir={output}",
                        f"/DChineseLanguage={language}", str(ROOT/"tools/packaging/jingban.iss")], check=True)
    record.write_text(json.dumps({"inputs": fingerprint, "sha256": sha256(target)}, indent=2), encoding="utf-8")
    return target


def build_portable(payload):
    output = WORK/"portable_candidate"
    output.mkdir(parents=True, exist_ok=True)
    archive = output/"鲸伴.zip"
    fingerprint = sha256(payload/"PACKAGE-MANIFEST.json")
    record = output/"build.json"
    if archive.is_file() and record.is_file():
        prior = json.loads(record.read_text(encoding="utf-8"))
        if prior.get("manifest") == fingerprint and prior.get("sha256") == sha256(archive):
            return archive
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(payload.name)/path.relative_to(payload))
    with zipfile.ZipFile(archive) as bundle:
        bad = bundle.testzip()
        if bad:
            raise ValueError("ZIP CRC validation failed: "+bad)
    record.write_text(json.dumps({"manifest": fingerprint, "sha256": sha256(archive), "all_members_crc_checked": True}, indent=2), encoding="utf-8")
    return archive


def publish(payload, version):
    original = json.loads((payload/"PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    current = audit_payload(payload)
    if current != original["files"]:
        raise ValueError("Candidate changed after verification; rebuild before publishing")
    output = ROOT/"deliverables/windows"/version
    output.mkdir(parents=True, exist_ok=True)
    archive = output/"鲸伴.zip"
    if archive.exists():
        raise FileExistsError("Published archives are immutable; use a new release version")
    copy(build_portable(payload), archive)
    installer = build_installer(payload, version)
    copy(installer, output/installer.name)
    outputs = [archive, output/"鲸伴安装.exe"]
    (output/"SHA256SUMS.txt").write_text("".join(sha256(path)+"  "+path.name+"\n" for path in outputs), encoding="utf-8")
    copy(payload/"使用说明.txt", output/"使用说明.txt")
    report = {"product": "鲸伴", "version": version, "payload": str(payload),
              "outputs": [{"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)} for path in outputs],
              "portable_zip_crc_checked": True, "signed": False}
    (WORK/"release_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    global WORK
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-directory", type=Path, default=WORK)
    parser.add_argument("--skip-freeze", action="store_true")
    parser.add_argument("--assemble-only", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--installer-only", action="store_true")
    parser.add_argument("--refresh-workers", action="store_true")
    parser.add_argument("--portable-only", action="store_true")
    args = parser.parse_args()
    WORK = args.work_directory.resolve()
    if WORK == ROOT or WORK == Path(WORK.anchor) or (WORK / "settings.ini").exists():
        raise ValueError("Use a separate build directory")
    version = app_version()
    WORK.mkdir(parents=True, exist_ok=True)
    if args.publish_only or args.installer_only or args.refresh_workers or args.portable_only:
        payload = Path(json.loads((WORK/"candidate.json").read_text(encoding="utf-8"))["payload"])
    else:
        stage_current_worker_sources()
        if not args.skip_freeze:
            environment = os.environ.copy()
            windows = Path(environment.get("SystemRoot", r"C:\Windows"))
            environment["PATH"] = os.pathsep.join(str(path) for path in
                (Path(sys.executable).parent, Path(sys.base_prefix), windows/"System32", windows))
            subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(WORK/"pyinstaller_dist"),
                "--workpath", str(WORK/"pyinstaller_work"), str(ROOT/"tools/packaging/jingban.spec")], check=True, env=environment)
        payload = assemble(version)
    print(json.dumps({"candidate": str(payload)}, ensure_ascii=False), flush=True)
    if args.refresh_workers:
        refresh_workers(payload)
        print("External worker sources refreshed; complete final acceptance before publishing", flush=True)
        return
    if args.portable_only:
        print(json.dumps({"portable": str(build_portable(payload))}, ensure_ascii=False), flush=True)
        return
    if args.installer_only:
        print(json.dumps({"installer": str(build_installer(payload, version))}, ensure_ascii=False), flush=True)
        return
    if not args.assemble_only:
        print(json.dumps(publish(payload, version), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
