"""Stage relocatable worker runtimes from fixed build inputs, never user data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = PROJECT_ROOT / "agent_backend/nanobot/.venv/Scripts/python.exe"


def _run_json(command):
    result = subprocess.run(command, check=True, capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


def _ignore(directory, names):
    del directory
    return {name for name in names if name in {"__pycache__", ".git", ".venv", ".virtual_documents", "test", "tests", "turtledemo"}
            or name.endswith((".pyc", ".pyo")) or name in {"_virtualenv.py", "_virtualenv.pth", "EXTERNALLY-MANAGED"}}


def _copy_tree(source, target, *, ignore=_ignore):
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)


def _copy_source(source, target, suffixes):
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        relative = path.relative_to(source)
        if any(part in {".venv", "node_modules", "__pycache__", "tests", "test", "runtime", "artifacts"} for part in relative.parts):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def stage_worker_runtime(target: Path) -> dict:
    """`target` is the bundle resource root (the final `_internal` folder)."""
    target = Path(target).resolve()
    if target == PROJECT_ROOT or target == Path(target.anchor) or (target / "settings.ini").exists():
        raise ValueError("A separate build resource directory is required")
    metadata = _run_json([str(BACKEND_PYTHON), "-I", "-c",
        "import sys,sysconfig,json; print(json.dumps({'base':sys.base_prefix,'site':sysconfig.get_path('purelib'),'version':sys.version.split()[0]}))"])
    base, site = Path(metadata["base"]), Path(metadata["site"])
    runtime = target / "runtime/python"
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        source = base / name
        if not source.is_file():
            raise FileNotFoundError("Standalone Python build input is incomplete: " + name)
        shutil.copy2(source, runtime / name)
    _copy_tree(base / "DLLs", runtime / "DLLs")
    # The base's site-packages is only its installer. The selected backend
    # packages below have their actual distribution metadata and licenses.
    def stdlib_ignore(directory, names):
        return _ignore(directory, names) | ({"site-packages"} if Path(directory) == base / "Lib" else set())
    _copy_tree(base / "Lib", runtime / "Lib", ignore=stdlib_ignore)
    _copy_tree(site, runtime / "Lib/site-packages")
    if (runtime / "pyvenv.cfg").exists():
        raise ValueError("A development virtualenv is not a redistributable runtime")
    node = Path(shutil.which("node") or "")
    if not node.is_file():
        raise FileNotFoundError("Node.js build input is unavailable")
    node_target = target / "runtime/node"
    node_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(node, node_target / "node.exe")
    for name in ("LICENSE", "LICENSE.txt"):
        if (node.parent / name).is_file():
            shutil.copy2(node.parent / name, node_target / name)
    ui = PROJECT_ROOT / "agent_backend/ui_tars"
    _copy_tree(ui / "node_modules", target / "agent_backend/ui_tars/node_modules")
    _copy_source(PROJECT_ROOT / "agent_backend", target / "agent_backend", {".py", ".mjs", ".js", ".ps1", ".json", ".txt"})
    _copy_source(PROJECT_ROOT / "desktop_pet", target / "desktop_pet", {".py"})
    (target / "shared/reasoning").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "shared/reasoning/profiles.json", target / "shared/reasoning/profiles.json")
    # The root package marker may not exist in a namespace source checkout.
    (target / "agent_backend").mkdir(exist_ok=True)
    version = subprocess.run([str(node_target / "node.exe"), "--version"], check=True,
                             capture_output=True, encoding="utf-8").stdout.strip()
    probe = _run_json([str(runtime / "python.exe"), "-I", "-c",
        "import json,sys,importlib.util;print(json.dumps({'python':sys.version.split()[0],'modules':{n:bool(importlib.util.find_spec(n)) for n in ['nanobot','quickjs','docx','pptx','openpyxl','pypdf','PIL','win32com','websockets','ddgs']}}))"])
    if not all(probe["modules"].values()):
        raise RuntimeError("Staged worker runtime is missing a required backend module")
    report = {"python": metadata["version"], "node": version, "isolated_imports": probe["modules"],
              "source_user_configuration_copied": False, "development_venv_copied": False,
              "memory_optional_installed": False}
    (target / "runtime/manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def install_optional_memory(target: Path) -> dict:
    target = Path(target).resolve()
    runtime = target / "runtime/python"
    # uv's original interpreter remains managed. This is an independent,
    # explicitly staged redistribution and must not retain uv's host marker.
    if not target.is_relative_to(PROJECT_ROOT / "artifacts") or not (target / "runtime/manifest.json").is_file():
        raise ValueError("Optional packages may only be installed into a staged artifact runtime")
    marker = runtime / "Lib/EXTERNALLY-MANAGED"
    if marker.is_file():
        marker.unlink()
    cache = PROJECT_ROOT / "artifacts/windows-build/build-cache"
    temporary = cache / "temp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(TEMP=str(temporary), TMP=str(temporary), PIP_CACHE_DIR=str(cache / "pip"),
                       HF_HOME=str(cache / "hf-unused"), HF_HUB_OFFLINE="1", MEM0_TELEMETRY="False")
    subprocess.run([str(runtime / "python.exe"), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-warn-script-location", "-r", str(PROJECT_ROOT / "requirements-memory-local.txt"), "keyring==25.7.0"],
                   check=True, env=environment)
    probe = _run_json([str(runtime / "python.exe"), "-I", "-c",
        "import importlib.util,json;print(json.dumps({n:bool(importlib.util.find_spec(n)) for n in ['mem0','qdrant_client','fastembed','onnxruntime','keyring']}))"])
    if not all(probe.values()):
        raise RuntimeError("Optional memory packages are incomplete")
    path = target / "runtime/manifest.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report.update(memory_optional_installed=True, memory_modules=probe,
                  bundled_memory_model=False, user_memory_database_copied=False)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("--install-memory", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage_worker_runtime(args.target)), flush=True)
    if args.install_memory:
        print(json.dumps(install_optional_memory(args.target)), flush=True)
