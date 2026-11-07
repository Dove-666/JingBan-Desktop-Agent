"""Copy declared runtime sources for reproducible, isolated Agent benchmarks.

No settings, credentials, conversations, assets, or task files are copied. The
existing backend dependency environment is reused through an explicit junction;
this is a source snapshot, not attestation or freezing of installed binaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def sources(root):
    paths = set()
    for directory, pattern in (("desktop_pet", "**/*.py"), ("agent_backend/nanobot", "*.py"),
                               ("agent_backend/nanobot", "*.js"),
                               ("agent_backend/ui_tars", "*.mjs"), ("benchmarks/agent_tasks", "*.py")):
        paths.update((root / directory).glob(pattern))
    paths.update(root / name for name in (
        "tools/run_agent_benchmarks.py", "benchmarks/agent_tasks/catalog.json",
        "agent_backend/nanobot/requirements.lock", "agent_backend/nanobot/requirements.txt",
        "agent_backend/ui_tars/package.json", "agent_backend/ui_tars/package-lock.json"))
    result = {}
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if (not path.resolve().is_relative_to(root) or path.is_symlink()
                or ".venv" in path.parts or "__pycache__" in path.parts):
            raise ValueError("SNAPSHOT_SOURCE_SCOPE_INVALID")
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def freeze(destination, *, root=ROOT):
    root = Path(root).resolve()
    destination = Path(destination).absolute()
    artifacts = root / "artifacts"
    if (not destination.resolve().is_relative_to(artifacts.resolve())
            or destination.resolve() == artifacts.resolve() or destination.exists()
            or not destination.parent.is_dir()):
        raise ValueError("SNAPSHOT_DESTINATION_INVALID")
    runtime = root / "agent_backend/nanobot/.venv"
    if sys.platform != "win32" or not (runtime / "Scripts/python.exe").is_file():
        raise ValueError("SNAPSHOT_RUNTIME_UNAVAILABLE")
    before = sources(root)
    destination.mkdir(exist_ok=False)
    copied = {}
    for relative, expected in before.items():
        raw = (root / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("SNAPSHOT_SOURCE_CHANGED")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        copied[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    stable = sources(root) == before == copied
    manifest = {"schema": "aura.frozen_benchmark_sources.v1",
        "scope": "declared source copy only; existing dependency environment reused; no user data copied",
        "source_files": [{"path": path, "sha256": value} for path, value in copied.items()],
        "source_copy_consistent": stable, "runtime_dependency": "existing isolated SDK via directory junction",
        "loaded_binary_attestation": False, "dependency_tree_copied": False}
    (destination / "snapshot-manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    if not stable:
        raise ValueError("SNAPSHOT_SOURCE_CHANGED")
    import _winapi
    _winapi.CreateJunction(str(runtime), str(destination / "agent_backend/nanobot/.venv"))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = freeze(args.destination)
    except (OSError, ValueError):
        print(json.dumps({"ready": False, "code": "SOURCE_SNAPSHOT_UNAVAILABLE"}))
        return 1
    print(json.dumps({"ready": True, "sources": len(manifest["source_files"]),
                      "source_copy_consistent": manifest["source_copy_consistent"],
                      "loaded_binary_attestation": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
