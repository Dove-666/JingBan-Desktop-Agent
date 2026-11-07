"""Stage redistributable engine binaries and offline upstream licensing material.

The development installation and user configuration are never modified. Engine
release inputs and corresponding native source trees are pinned independently.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import urljoin, urlparse
import zipfile


RAPFI_ARCHIVE_SHA256 = "1a3e24024062a153ac079060ee9589a37c6bdd1ecc54fed3908793c519594e05"
SOURCE_ARCHIVES = (
    {"name": "rapfi", "repo": "dotb1ue/rapfi", "tag": "250615",
     "commit": "1be1551ced57e38d53ed58f6d74bf6f8b4bdc230",
     "sha256": "c87aeb322c8372e91efa4e93a9f25de60e98a66db0c6e50f05d76d5fc96a5a70"},
    {"name": "pikafish", "repo": "official-pikafish/Pikafish", "tag": "Pikafish-2026-09-06",
     "commit": "4c17cee11f888ae1d48a9494f2e2239f019f0a1f",
     "sha256": "2b762c967fae882aa9a8dfe76d868b4665e658f55dead2940b53ce1548f12e0c"},
)
RAPFI_SUBMODULES = (
    {"path": "Gomocalc", "repo": "dhbloo/gomoku-calculator", "name": "rapfi-gomocalc",
     "commit": "87c7d707244aee54c5b87be7ba413646d6e2984e",
     "sha256": "7e5924f43b28be47cda27ade1baf87c3b643d77c2d5fc47f80bf11d91e4a8ae0"},
    {"path": "Trainer", "repo": "dhbloo/pytorch-nnue-trainer", "name": "rapfi-trainer",
     "commit": "91f559a3a8ef9a4529d6ec177eef83cca6775517",
     "sha256": "03270bb1d7c001534ebc62a2019f2e75354edab3c16d056aae1d56f2ccdd68c6"},
    {"path": "Networks", "repo": "dhbloo/rapfi-networks", "name": "rapfi-networks",
     "commit": "918b757a129258e9e765f77fe17d507c2bb1a60b"},
)
NETWORK_BLOBS = {
    "LICENSE": "9bd7c7e01d5e2685e2174affa177333b4f76bdc8",
    "README.md": "ea06e58af427b2cff5b2e82614b508a637e4df03",
    "classical/model210901.bin": "6b7b3a12847cc72fb3162921698cff871c0737e1",
    "classical/model220723.bin": "879fd6f69d5d9b544a068b63a2adac18b3baadaa",
    "config-example/config.toml": "c7ef3facb3fa25394b24726270e656bf86cc4ca0",
    "config-example/gomocalc-classical210901.toml": "15cef39b8b7da3284d8c0e6002c635067a80a450",
    "config-example/gomocalc-classical220723.toml": "73ce5da4b0e2494244ff9f789d54822fbeb09a1d",
    "config-example/gomocalc-mix9svq.toml": "f52b644cbb03633e8d04bb5c3736b9deb67ca6c2",
    "mix9svq/mix9svqfreestyle_bsmix.bin.lz4": "af04ad2bbb1d95aa5721921627294ef30fdbb0b2",
    "mix9svq/mix9svqrenju_bs15_black.bin.lz4": "cf62d3ce5d31fa8e2a516ee06752b39314670a3f",
    "mix9svq/mix9svqrenju_bs15_white.bin.lz4": "e90bde41d7d4d4d77d86e033c1c633ff9ce1bbbd",
    "mix9svq/mix9svqstandard_bs15.bin.lz4": "2be689a99baa98c3dc9a95d3211c5c0bb54ee11d",
    "wasm_preloads.txt": "0810669416b9d53087b170c32dcc7f2c336744ca",
}
RAPFI_FILES = ("config.toml", "model210901.bin", "mix9svqfreestyle_bsmix.bin.lz4",
               "mix9svqstandard_bs15.bin.lz4", "mix9svqrenju_bs15_black.bin.lz4",
               "mix9svqrenju_bs15_white.bin.lz4", "AUTHORS", "Copying.txt")
PIKAFISH_FILES = ("pikafish.exe", "pikafish.nnue", "AUTHORS", "Copying.txt")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(target: Path, root: Path) -> Path:
    target, root = target.absolute(), root.resolve()
    if not target.resolve().is_relative_to(root):
        raise ValueError("Release staging path escapes its destination")
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Release staging may not follow symlinks")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _copy(source: Path, target: Path, root: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Invalid release input")
    shutil.copy2(source, _inside(target, root))
    if _sha(source) != _sha(target):
        raise ValueError("Copied release input failed hash verification")


def _download(url: str, path: Path, *, expected_sha256: str | None = None,
              maximum_bytes: int = 256*1024*1024) -> Path:
    if path.is_file() and path.stat().st_size:
        if expected_sha256 is None or _sha(path) == expected_sha256:
            return path
        raise ValueError(f"Cached upstream input checksum mismatch: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+".part")
    error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Jingban-Release/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as target:
                count = 0
                while block := response.read(64*1024):
                    count += len(block)
                    if count > maximum_bytes:
                        raise ValueError("Upstream input exceeds its release bound")
                    target.write(block)
            if not temporary.stat().st_size:
                raise ValueError("Upstream input was empty")
            if expected_sha256 is not None and _sha(temporary) != expected_sha256:
                raise ValueError("Upstream input checksum mismatch")
            temporary.replace(path)
            return path
        except Exception as caught:
            error = caught
            if isinstance(caught, ValueError):
                break
            if attempt < 2:
                time.sleep(.5*(attempt+1))
    raise RuntimeError(f"Unable to obtain verified upstream input: {path.name}") from error


def _source_archive(spec: dict, cache: Path) -> tuple[Path, str]:
    filename = f"{spec['name']}-{spec['commit']}.zip"
    url = f"https://codeload.github.com/{spec['repo']}/zip/{spec['commit']}"
    path = _download(url, cache/filename, expected_sha256=spec.get("sha256"))
    prefix = spec["repo"].rsplit("/", 1)[-1]+"-"+spec["commit"]+"/"
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Source archive CRC verification failed")
        total = 0
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if (not member.filename.startswith(prefix) or name.is_absolute()
                    or ".." in name.parts or "\\" in member.filename or ":" in member.filename
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Unsafe upstream source archive member")
            total += member.file_size
        if total > 256*1024*1024:
            raise ValueError("Source archive uncompressed bound exceeded")
    return path, url


def _validate_networks_sources(path: Path, commit: str) -> None:
    """Verify all model/source blobs against the pinned upstream Git tree."""
    prefix = f"rapfi-networks-{commit}/"
    with zipfile.ZipFile(path) as archive:
        files = {member.filename[len(prefix):]: member for member in archive.infolist() if not member.is_dir()}
        if set(files) != set(NETWORK_BLOBS):
            raise ValueError("Rapfi Networks source tree is incomplete")
        for name, item in files.items():
            payload = archive.read(item)
            header = b"blob "+str(len(payload)).encode("ascii")+b"\0"
            if hashlib.sha1(header+payload).hexdigest() != NETWORK_BLOBS[name]:
                raise ValueError("Rapfi Networks source blob does not match its pinned Git object")


def prepare_engine_distribution(project_root: Path, staging_root: Path,
                                cache_dir: Path | None = None) -> dict:
    project_root, staging_root = Path(project_root).resolve(), Path(staging_root).resolve()
    if staging_root.is_relative_to(project_root/"assets") or staging_root == project_root:
        raise ValueError("Release staging must not overwrite development resources")
    staging_root.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir).resolve() if cache_dir else staging_root.parent/"cache"
    cache.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "engines": {}, "source_archives": [], "rapfi_submodules": []}
    sources = staging_root/"sources"
    sources.mkdir(exist_ok=True)
    for spec in SOURCE_ARCHIVES:
        path, url = _source_archive(spec, cache)
        _copy(path, sources/path.name, staging_root)
        result["source_archives"].append({**spec, "url": url, "file": "sources/"+path.name,
                                           "bytes": path.stat().st_size, "sha256": _sha(path)})
    # The native Windows engine builds from the main Rapfi source tree. These
    # are its separately pinned companion frontend/trainer/model repositories.
    # Copy already validated optional archives; never package an in-progress file.
    for spec in RAPFI_SUBMODULES:
        path = cache/f"{spec['name']}-{spec['commit']}.zip"
        row = {**spec, "source_url": f"https://github.com/{spec['repo']}/tree/{spec['commit']}",
               "archive_url": f"https://codeload.github.com/{spec['repo']}/zip/{spec['commit']}", "included": False}
        if path.is_file() and path.stat().st_size and zipfile.is_zipfile(path):
            path, _ = _source_archive(spec, cache)
            if spec["name"] == "rapfi-networks":
                _validate_networks_sources(path, spec["commit"])
                row.update(archive_method="Git-tree export; all blobs match the pinned upstream commit",
                           git_blob_objects=NETWORK_BLOBS)
            _copy(path, sources/path.name, staging_root)
            row.update(included=True, file="sources/"+path.name, bytes=path.stat().st_size, sha256=_sha(path))
        result["rapfi_submodules"].append(row)
    installed = project_root/"assets/engines"
    for name, expected_files in (("rapfi", RAPFI_FILES), ("pikafish", PIKAFISH_FILES)):
        original = installed/name
        manifest = json.loads((original/"manifest.json").read_text(encoding="utf-8-sig"))
        expected_name = "Rapfi" if name == "rapfi" else "Pikafish"
        if manifest.get("name") != expected_name or set(manifest.get("files", ())) != set(expected_files) | ({"rapfi.exe"} if name == "rapfi" else set()):
            raise ValueError("Unexpected installed engine manifest")
        target = staging_root/"assets/engines"/name
        target.mkdir(parents=True, exist_ok=True)
        for filename in expected_files:
            source = original/filename
            item = manifest["files"][filename]
            if source.stat().st_size != item["bytes"] or _sha(source) != item["sha256"]:
                raise ValueError("Installed engine input differs from its official manifest")
            _copy(source, target/filename, staging_root)
        if name == "rapfi":
            archive = project_root/"artifacts/gomoku_integration_20261107/Rapfi-engine.7z"
            if not archive.is_file() or _sha(archive) != RAPFI_ARCHIVE_SHA256:
                archive = _download(manifest["archive_url"], cache/"Rapfi-engine.7z",
                                    expected_sha256=RAPFI_ARCHIVE_SHA256)
            tar = shutil.which("tar.exe") or shutil.which("tar")
            if not tar:
                raise RuntimeError("Windows tar is required to read the pinned official engine archive")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            member = "pbrain-rapfi-windows-sse.exe"
            with (target/"rapfi.exe").open("wb") as output:
                subprocess.run([tar, "-xOf", str(archive), member], stdout=output,
                               stderr=subprocess.PIPE, check=True, creationflags=flags)
            if not (target/"rapfi.exe").read_bytes().startswith(b"MZ"):
                raise ValueError("Official SSE engine is not a Windows executable")
            manifest.pop("cpu_avx2_available", None)
            manifest.update(original_executable=member,
                            release_cpu_baseline="x86-64 + SSE4.1 (upstream SSE build)")
        manifest["files"] = {path.name: {"bytes": path.stat().st_size, "sha256": _sha(path)}
                             for path in sorted(target.iterdir()) if path.is_file() and path.name != "manifest.json"}
        (target/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for filename in ("AUTHORS", "Copying.txt"):
            _copy(target/filename, staging_root/"licenses/engines"/name/filename, staging_root)
        result["engines"][name] = {"version": manifest["version"], "entry": str((target/manifest["entry"]).relative_to(staging_root)).replace("\\", "/"),
                                     "bytes": sum(row["bytes"] for row in manifest["files"].values()),
                                     "license": manifest["license"], "source_url": manifest["source_url"],
                                     "original_executable": manifest["original_executable"], "files": manifest["files"]}
    notes = (
        "鲸伴随附的独立棋类引擎 / Bundled standalone game engines\n\n"
        "Rapfi 250615 (GPL-3.0-or-later) and Pikafish 2026-09-06 (GPL-3.0).\n"
        "These unmodified upstream engines run as separate processes. Full native source trees,\n"
        "including their native build scripts, are provided as the pinned source archives here.\n"
        "Rapfi's companion Gomocalc/Trainer/Networks repositories are recorded with exact commits\n"
        "in SOURCE_MANIFEST.json; any included companion archives are identified there.\n"
        "The exact evaluation data used at runtime is provided beside each engine binary.\n"
        "Extract the native source archives and follow their upstream build instructions.\n\n"
        "Rapfi Windows baseline: upstream SSE build, requires x86-64 with SSE4.1; not AVX2.\n"
        "Pikafish Windows baseline: upstream x86-64 universal binary, chooses CPU implementation.\n"
        "This staging procedure does not modify the developer's installed engine directories.\n"
    )
    (sources/"README.txt").write_text(notes, encoding="utf-8")
    (sources/"SOURCE_MANIFEST.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href", "")
        if tag == "a" and ("3rdparty-" in href or "-attribution-" in href) and href.endswith(".html") and "/" not in href:
            self.links.add(href)


def prepare_qt_notices(staging_root: Path, cache_dir: Path | None = None) -> dict:
    staging_root = Path(staging_root).resolve()
    cache = Path(cache_dir).resolve() if cache_dir else staging_root.parent/"cache"
    target = staging_root/"licenses/qt"
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for label, url, marker in (
        ("LGPL-3.0.txt", "https://www.gnu.org/licenses/lgpl-3.0.txt", b"GNU LESSER GENERAL PUBLIC LICENSE"),
        ("GPL-3.0.txt", "https://www.gnu.org/licenses/gpl-3.0.txt", b"GNU GENERAL PUBLIC LICENSE"),
        ("LGPL-2.1.txt", "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt", b"GNU LESSER GENERAL PUBLIC LICENSE"),
        ("GPL-2.0.txt", "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt", b"GNU GENERAL PUBLIC LICENSE"),
    ):
        path = _download(url, cache/label, maximum_bytes=128*1024)
        if marker not in path.read_bytes()[:1024]:
            raise ValueError("Expected full GNU license text")
        _copy(path, target/label, staging_root)
        rows.append({"file": label, "url": url, "bytes": path.stat().st_size, "sha256": _sha(path)})
    links = set()
    for label, url in (
        ("qtwebengine-licensing.html", "https://doc.qt.io/qt-6/qtwebengine-licensing.html"),
        ("licenses-used-in-qt.html", "https://doc.qt.io/qt-6/licenses-used-in-qt.html"),
        ("pyside-licenses.html", "https://doc.qt.io/qtforpython-6/licenses.html"),
    ):
        path = _download(url, cache/label, maximum_bytes=4*1024*1024)
        content = path.read_text(encoding="utf-8")
        expected_marker = "Qt WebEngine 6.140.0" if label == "qtwebengine-licensing.html" else "Qt 6.12"
        if expected_marker not in content and label != "pyside-licenses.html":
            raise ValueError("Qt documentation version changed; refresh the release licensing snapshot")
        _copy(path, target/label, staging_root)
        if label != "pyside-licenses.html":
            parser = _Links()
            parser.feed(content)
            # Cover modules used by Widgets/WebEngine/Multimedia and their Qt
            # runtime dependencies, rather than bundling unrelated Qt products.
            for link in parser.links:
                if link.startswith(("qtwebengine-", "qtcore-", "qtgui-", "qtnetwork-", "qtwidgets-",
                                    "qtmultimedia-", "qtqml-", "qtquick-", "qtwebchannel-",
                                    "qtpositioning-", "qtshadertools-", "qtbase-")):
                    links.add(link)
        rows.append({"file": label, "url": url, "bytes": path.stat().st_size, "sha256": _sha(path)})

    def fetch(link):
        url = urljoin("https://doc.qt.io/qt-6/", link)
        if urlparse(url).netloc != "doc.qt.io":
            raise ValueError("Unexpected license-document host")
        path = _download(url, cache/"qt-license-pages"/link, maximum_bytes=4*1024*1024)
        text = path.read_text(encoding="utf-8")
        if "license" not in text.lower() or len(text) < 500:
            raise ValueError("Incomplete Qt third-party notice page")
        _copy(path, target/link, staging_root)
        return {"file": link, "url": url, "bytes": path.stat().st_size, "sha256": _sha(path)}

    errors = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch, link): link for link in sorted(links)}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as error:
                errors.append({"file": futures[future], "error": type(error).__name__})
    result = {"qt_version": "6.12.0", "qt_webengine_version": "6.140.0", "chromium_version": "140.0.7339.225", "public_license_texts": 4,
              "third_party_pages_expected": len(links), "third_party_pages_downloaded": len(links)-len(errors),
              "files": sorted(rows, key=lambda row: row["file"]), "errors": errors,
              "source_locations": [
                  "https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/",
                  "https://code.qt.io/cgit/qt/qtbase.git/tree/?h=v6.12.0",
                  "https://code.qt.io/cgit/qt/qtwebengine.git/tree/?h=6.140",
                  "https://code.qt.io/cgit/pyside/pyside-setup.git/tree/?h=v6.12.0",
              ]}
    (target/"QT_SOURCE_AND_LICENSES.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (target/"README.txt").write_text(
        "Qt / PySide6 6.12.0; Qt WebEngine 6.140.0; Chromium 140.0.7339.225\n\n"
        "鲸伴 uses dynamically loaded Qt/PySide libraries in its onedir distribution.\n"
        "The Qt DLLs remain separate files and may be replaced with compatible rebuilt libraries.\n"
        "Full GNU LGPLv3, GPLv3, LGPLv2.1 and GPLv2 texts are included here.\n"
        "The offline HTML snapshots include the Qt licensing index and the full notices\n"
        "for shipped Qt module dependencies and Qt WebEngine's Chromium components.\n"
        "QT_SOURCE_AND_LICENSES.json records upstream locations, individual file hashes,\n"
        "the documented Qt version, source retrieval locations and any failed snapshots.\n"
        "Qt/PySide binaries are not locally modified. The adjacent metadata contains\n"
        "their upstream package license declarations. No commercial-license claim is made.\n",
        encoding="utf-8")
    if errors:
        raise RuntimeError(f"Missing {len(errors)} Qt third-party notice documents; see licensing manifest")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--qt-notices", action="store_true")
    args = parser.parse_args()
    result = prepare_engine_distribution(args.project_root, args.staging, args.cache)
    print(json.dumps({"engines": {name: row["original_executable"] for name, row in result["engines"].items()},
                      "source_archives": len(result["source_archives"])}, ensure_ascii=False), flush=True)
    if args.qt_notices:
        result = prepare_qt_notices(args.staging, args.cache)
        print(json.dumps({"qt_notices": result["third_party_pages_downloaded"], "errors": result["errors"]}), flush=True)
