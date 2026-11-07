"""Install AURA's pinned official Windows Rapfi CPU engine and evaluation files."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "250615"
URL = "https://github.com/dotb1ue/rapfi/releases/download/250615/Rapfi-engine.7z"
SHA256 = "1a3e24024062a153ac079060ee9589a37c6bdd1ecc54fed3908793c519594e05"
SOURCE = "https://github.com/dotb1ue/rapfi/tree/250615"
FILES = (
    "config.toml", "model210901.bin", "mix9svqfreestyle_bsmix.bin.lz4",
    "mix9svqstandard_bs15.bin.lz4", "mix9svqrenju_bs15_black.bin.lz4",
    "mix9svqrenju_bs15_white.bin.lz4", "AUTHORS",
)


def download(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "AURA-Rapfi-Setup/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


def install(target: Path, archive: Path | None = None) -> dict:
    if platform.system() != "Windows":
        raise RuntimeError("本安装工具提供 Windows 引擎；其他系统可编译 Rapfi 并指定引擎路径。")
    tar = shutil.which("tar.exe")
    if not tar:
        raise RuntimeError("找不到 Windows tar.exe，无法解压官方引擎包。")
    target = target.resolve()
    existing_manifest = target / "manifest.json"
    if existing_manifest.exists():
        try:
            existing = json.loads(existing_manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise RuntimeError("目标目录已有无法识别的引擎记录，未覆盖。") from None
        if not isinstance(existing, dict) or existing.get("name") != "Rapfi":
            raise RuntimeError("目标目录已用于其他引擎，未覆盖。")
    if target.exists() and any(path.name != "rapfi.exe" for path in target.glob("*.exe")):
        raise RuntimeError("目标目录包含其他引擎程序，未覆盖。")
    target.mkdir(parents=True, exist_ok=True)
    parent = target.parent.resolve()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix=".rapfi-setup-", dir=parent) as scratch:
        work = Path(scratch).resolve()
        if not work.is_relative_to(parent):
            raise RuntimeError("临时目录超出引擎目录。")
        package = archive.resolve() if archive else work / "official.7z"
        if archive is None:
            print("正在下载官方 Rapfi 引擎…", flush=True)
            download(URL, package)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        if digest != SHA256:
            raise RuntimeError("Rapfi 官方包校验失败，未安装。")
        names = subprocess.check_output([tar, "-tf", str(package)], text=True,
                                        encoding="utf-8", creationflags=flags).splitlines()
        for name in names:
            member = PurePosixPath(name.replace("\\", "/"))
            if member.is_absolute() or ".." in member.parts or ":" in name:
                raise RuntimeError("引擎包包含不安全的文件路径。")
        extracted = work / "unpacked"
        extracted.mkdir()
        subprocess.run([tar, "-xf", str(package), "-C", str(extracted)], check=True,
                       creationflags=flags, capture_output=True)
        avx2 = bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        executable = f"pbrain-rapfi-windows-{'avx2' if avx2 else 'sse'}.exe"
        ready = work / "ready"
        ready.mkdir()
        copied = []
        for name in (executable, *FILES):
            original = extracted / name
            if not original.is_file() or original.is_symlink() or not original.resolve().is_relative_to(extracted):
                raise RuntimeError("官方引擎包缺少必要文件。")
            installed_name = "rapfi.exe" if name == executable else name
            shutil.copy2(original, ready / installed_name)
            copied.append(installed_name)
        download("https://raw.githubusercontent.com/dotb1ue/rapfi/250615/Copying.txt", ready / "Copying.txt")
        copied.append("Copying.txt")
        manifest = {
            "name": "Rapfi", "version": VERSION, "entry": "rapfi.exe", "license": "GPL-3.0-or-later",
            "source_url": SOURCE, "archive_url": URL, "archive_sha256": digest,
            "archive_bytes": package.stat().st_size, "original_executable": executable,
            "cpu_avx2_available": avx2,
            "files": {name: {"bytes": (ready/name).stat().st_size,
                              "sha256": hashlib.sha256((ready/name).read_bytes()).hexdigest()}
                      for name in copied},
        }
        (ready/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # Finish download, archive checks and staging before replacing any
        # installed file. Never follow a target symlink into another engine.
        for name in (*copied, "manifest.json"):
            destination = target / name
            if destination.is_symlink() or not destination.resolve().is_relative_to(target):
                raise RuntimeError("目标文件指向引擎目录之外，未覆盖。")
        for name in (*copied, "manifest.json"):
            (ready/name).replace(target/name)
        return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=ROOT / "assets/engines/rapfi")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    result = install(args.target, args.archive)
    print(json.dumps({"version": result["version"], "entry": result["entry"],
                      "installed_bytes": sum(item["bytes"] for item in result["files"].values()),
                      "files": list(result["files"])}, ensure_ascii=False))
