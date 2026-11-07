"""Install the pinned official CPU engine used by AURA's local Xiangqi game.

Only the Windows engine, its evaluation weights and license are retained. The
archive is verified before extraction; no downloaded installer is executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2026-09-06"
URL = "https://github.com/official-pikafish/Pikafish/releases/download/Pikafish-2026-09-06/Pikafish.2026-09-06.7z"
SHA256 = "41952bbfe2520faceb5902c69e6ab4845cc999841d2b49a95cc1be7867a25e5b"
SOURCE = "https://github.com/official-pikafish/Pikafish/tree/Pikafish-2026-09-06"


def install(target: Path, archive: Path | None = None) -> dict:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        raise RuntimeError("此安装脚本提供 Windows 版本；其他系统可自行编译并指定引擎路径。")
    tar = shutil.which("tar.exe")
    if not tar:
        raise RuntimeError("找不到 Windows tar.exe，无法解压官方引擎包。")
    parent = target.parent.resolve()
    with tempfile.TemporaryDirectory(prefix=".pikafish-setup-", dir=parent) as scratch:
        work = Path(scratch).resolve()
        if not work.is_relative_to(parent):
            raise RuntimeError("临时目录超出引擎目录。")
        package = archive.resolve() if archive else work / "official.7z"
        if archive is None:
            request = urllib.request.Request(URL, headers={"User-Agent": "AURA-Pikafish-Setup/1.0"})
            print("正在下载官方皮卡鱼引擎…", flush=True)
            with urllib.request.urlopen(request, timeout=60) as response, package.open("wb") as output:
                shutil.copyfileobj(response, output)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        if digest != SHA256:
            raise RuntimeError("引擎包校验失败，未安装；请重新从官方发布页下载。")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        names = subprocess.check_output([tar, "-tf", str(package)], text=True,
                                        encoding="utf-8", errors="replace", creationflags=flags).splitlines()
        for name in names:
            path = PurePosixPath(name.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in name:
                raise RuntimeError("官方包包含不安全的文件路径，停止解压。")
        extracted = work / "unpacked"
        extracted.mkdir()
        subprocess.run([tar, "-xf", str(package), "-C", str(extracted)], check=True,
                       creationflags=flags, capture_output=True)
        executables = [p for p in extracted.rglob("*.exe") if "pikafish" in p.name.lower()]
        candidates = [p for p in executables if not any(word in str(p.relative_to(extracted)).lower()
                       for word in ("arm64", "aarch64", "x86-32", "32bit"))]
        if not candidates:
            raise RuntimeError("官方包中未找到适合本机的 Windows 引擎。")
        # The official universal binary selects this computer's CPU features.
        candidates.sort(key=lambda p: ("universal" not in str(p).lower(),
                                      "avx" in p.name.lower(), len(p.parts), len(p.name)))
        executable = candidates[0]
        copied = []
        shutil.copy2(executable, target / "pikafish.exe")
        copied.append("pikafish.exe")
        for weight in extracted.rglob("*.nnue"):
            if weight.name not in copied:
                shutil.copy2(weight, target / weight.name)
                copied.append(weight.name)
        licenses = [p for p in extracted.rglob("*") if p.is_file()
                    and p.name.lower() in {"copying.txt", "copying", "license", "license.txt", "authors"}]
        for path in licenses:
            if path.name not in copied:
                shutil.copy2(path, target / path.name)
                copied.append(path.name)
        if not any(name.lower().startswith(("copying", "license")) for name in copied):
            request = urllib.request.Request(
                "https://raw.githubusercontent.com/official-pikafish/Pikafish/Pikafish-2026-09-06/Copying.txt")
            with urllib.request.urlopen(request, timeout=30) as response:
                (target / "Copying.txt").write_bytes(response.read())
            copied.append("Copying.txt")
        manifest = {"name": "Pikafish", "version": VERSION, "entry": "pikafish.exe",
                    "license": "GPL-3.0", "source_url": SOURCE, "archive_url": URL,
                    "archive_sha256": digest, "archive_bytes": package.stat().st_size,
                    "original_executable": executable.relative_to(extracted).as_posix(),
                    "files": {name: {"bytes": (target / name).stat().st_size,
                                      "sha256": hashlib.sha256((target / name).read_bytes()).hexdigest()}
                              for name in copied}}
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=ROOT / "assets/engines/pikafish")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    result = install(args.target, args.archive)
    print(json.dumps({"version": result["version"], "entry": result["entry"],
                      "installed_bytes": sum(f["bytes"] for f in result["files"].values()),
                      "files": list(result["files"])}, ensure_ascii=False))
