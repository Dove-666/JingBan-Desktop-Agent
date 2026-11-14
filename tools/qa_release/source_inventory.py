"""Fingerprint production build inputs without user configuration or credentials."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.packaging.worker_runtime import iter_source_inputs, iter_staged_worker_sources

IGNORED = {".venv", "node_modules", "__pycache__", "artifacts", "tests", "test", "runtime"}
BUILD_SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".ps1", ".html", ".json", ".txt", ".md",
                         ".tsx", ".ts", ".css", ".yaml", ".yml", ".spec", ".iss"}
DSH_REQUIRED_METADATA = ("agent_backend/deepseek_harness/package.json",
                         "agent_backend/deepseek_harness/package-lock.json")


def dsh_dependency_metadata(root=ROOT):
    """Installed metadata is separate from the authored source selection."""
    root = Path(root)
    directory = root / "agent_backend/deepseek_harness/node_modules"
    paths = {path for path in directory.rglob("package.json") if path.is_file()
             and not any(part in {"test", "tests", ".git", ".venv"}
                         for part in path.relative_to(directory).parts)}
    # npm file: dependencies may be directory links that rglob does not follow.
    paths.add(directory / "@deepseek-ai/dsh-cua-windows/package.json")
    paths.add(directory / "@deepseek-ai/dsh-cua-windows/native/bin/win-x64/BUILD.json")
    paths.add(directory / ".package-lock.json")
    return tuple(sorted(path for path in paths if path.is_file()))


def cua_source_inputs(root=ROOT):
    directory = Path(root) / "third_party/deepseek_harness/cua-windows"
    suffixes = BUILD_SOURCE_SUFFIXES | {".cs", ".csproj", ".sln", ".slnx", ".manifest", ".props", ".targets", ".patch"}
    return tuple(sorted(path for path in directory.rglob("*") if path.is_file()
        and not any(part in IGNORED | {"bin", "obj", ".git"} for part in path.relative_to(directory).parts)
        and (path.suffix in suffixes or path.name in {"LICENSE", ".npmrc"})))

def inputs():
    paths = set(iter_staged_worker_sources(project_root=ROOT))
    for folder in ("shared/reasoning", "desktop_pet", "browser-extension/whale-ask", "tools/packaging", "frontend/chat-ui/src"):
        paths.update(iter_source_inputs(ROOT / folder, BUILD_SOURCE_SUFFIXES, project_root=ROOT))
    paths.update(dsh_dependency_metadata(ROOT))
    paths.update(cua_source_inputs(ROOT))
    for path in (ROOT / "assets").rglob("*"):
        if path.is_file() and path.suffix.lower() in {".png", ".json", ".svg", ".ico"} and "engines" not in path.relative_to(ROOT / "assets").parts:
            paths.add(path)
    for name in ("main.py", "requirements.txt", "requirements-build.txt", "requirements-remote.txt", "tools/build_windows_release.py",
                 "tools/install_browser_ask.ps1", "tools/uninstall_browser_ask.ps1", "frontend/chat-ui/package.json", "frontend/chat-ui/package-lock.json"):
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    for name in (*DSH_REQUIRED_METADATA, "agent_backend/deepseek_harness/.npmrc", "third_party/node/runtime.json",
                 "tools/qa_release/source_inventory.py", "tools/qa_release/verify_all_inputs.py"):
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

if __name__ == "__main__":
    target = Path(sys.argv[1])
    with target.open("x", encoding="utf-8") as stream:
        json.dump(inputs(), stream, ensure_ascii=False, indent=2)
    print(target)
