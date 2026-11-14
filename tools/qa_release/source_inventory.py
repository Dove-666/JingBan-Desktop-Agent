"""Fingerprint production build inputs without user configuration or credentials."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
IGNORED = {".venv", "node_modules", "__pycache__", "artifacts", "tests", "test", "runtime"}

def inputs():
    paths = set()
    for folder in ("shared/reasoning", "desktop_pet", "agent_backend", "browser-extension/whale-ask", "tools/packaging", "frontend/chat-ui/src"):
        for path in (ROOT / folder).rglob("*"):
            relative = path.relative_to(ROOT / folder)
            if path.is_file() and not any(p in IGNORED for p in relative.parts) and path.suffix in {
                ".py", ".js", ".mjs", ".ps1", ".html", ".json", ".txt", ".md", ".tsx", ".ts", ".css"}:
                paths.add(path)
    for path in (ROOT / "assets").rglob("*"):
        if path.is_file() and path.suffix.lower() in {".png", ".json", ".svg", ".ico"} and "engines" not in path.relative_to(ROOT / "assets").parts:
            paths.add(path)
    for name in ("main.py", "requirements.txt", "requirements-build.txt", "requirements-remote.txt", "tools/build_windows_release.py",
                 "tools/install_browser_ask.ps1", "tools/uninstall_browser_ask.ps1", "frontend/chat-ui/package.json", "frontend/chat-ui/package-lock.json"):
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

if __name__ == "__main__":
    target = Path(sys.argv[1])
    with target.open("x", encoding="utf-8") as stream:
        json.dump(inputs(), stream, ensure_ascii=False, indent=2)
    print(target)
