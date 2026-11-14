"""Stage relocatable worker runtimes from fixed build inputs, never user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from tools.packaging.node_runtime import stage_node
BACKEND_PYTHON = PROJECT_ROOT / ".venv/Scripts/python.exe"
DSH_ROOT = PROJECT_ROOT / "agent_backend/deepseek_harness"
DSH_VERSION = "0.2.2-rc.3"
RETIRED_HOST_MODULES = {"checkpoints.py", "execution_runtime.py", "office_runtime.py",
                        "research_delegation.py", "libreoffice_renderer.py", "libreoffice_helper.py"}
SOURCE_EXCLUDED_DIRS = frozenset({".venv", "node_modules", "__pycache__", "tests", "test", "runtime", "artifacts", "nanobot", "ui_tars"})
BACKEND_SOURCE_SUFFIXES = frozenset({".py", ".mjs", ".js", ".ps1", ".json", ".txt"})
DSH_SOURCE_SUFFIXES = frozenset({".py", ".mjs", ".js", ".json", ".yml", ".yaml", ".txt", ".md"})


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


def iter_source_inputs(source, suffixes, *, project_root=None):
    """The single source selector shared by staging and release verification."""
    source, project_root = Path(source), Path(project_root or PROJECT_ROOT)
    for directory, dirs, files in os.walk(source):
        dirs[:] = sorted(name for name in dirs if name not in SOURCE_EXCLUDED_DIRS)
        for name in sorted(files):
            path = Path(directory) / name
            if path.parent == project_root / "desktop_pet/agents" and name in RETIRED_HOST_MODULES:
                continue
            if path.suffix in suffixes:
                yield path


def iter_staged_worker_sources(*, project_root=None):
    """Union of both DSH-specific and general worker staging passes."""
    root = Path(project_root or PROJECT_ROOT)
    paths = set(iter_source_inputs(root / "agent_backend", BACKEND_SOURCE_SUFFIXES, project_root=root))
    paths.update(iter_source_inputs(root / "agent_backend/deepseek_harness", DSH_SOURCE_SUFFIXES, project_root=root))
    paths.update(iter_source_inputs(root / "desktop_pet", {".py"}, project_root=root))
    return iter(sorted(paths))


def _copy_source(source, target, suffixes):
    for path in iter_source_inputs(source, suffixes):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def stage_dsh_runtime(target: Path) -> dict:
    """Stage the pinned official runtime, never a developer's Harness home."""
    target = Path(target).resolve()
    for name in ("dsh", "dsh-sdk-client"):
        metadata = json.loads((DSH_ROOT / "node_modules/@deepseek-ai" / name / "package.json").read_text(encoding="utf-8"))
        if metadata["version"] != DSH_VERSION:
            raise ValueError("DeepSeek Harness dependency differs from its pinned release")
    helper = DSH_ROOT / "node_modules/@deepseek-ai/dsh-cua-windows/native/bin/win-x64"
    native = json.loads((helper / "BUILD.json").read_text(encoding="utf-8-sig"))
    if not native.get("self_contained") or native.get("runtime") != "win-x64":
        raise ValueError("The self-contained Windows CUA helper is required")
    vendor = PROJECT_ROOT / "third_party/deepseek_harness/cua-windows"
    if (helper / "BUILD.json").read_bytes() != (vendor / "native/bin/win-x64/BUILD.json").read_bytes():
        raise ValueError("Installed CUA helper differs from the reviewed native build")
    if native.get("local_patches_sha256") != hashlib.sha256((vendor / "patches/MANIFEST.json").read_bytes()).hexdigest():
        raise ValueError("Native CUA build does not match the reviewed source patches")
    for name, expected in native["sha256"].items():
        path = (helper / name).resolve()
        if not path.is_relative_to(helper.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Windows CUA helper differs from its build manifest")
    node_target = target / "runtime/node"
    node_pin = stage_node(node_target)
    version = "v" + node_pin["version"]
    _copy_source(DSH_ROOT, target / "agent_backend/deepseek_harness", DSH_SOURCE_SUFFIXES)
    # Dereference the local vendored CUA package into a portable ordinary tree.
    # npm's file dependency must not point back to the development checkout.
    _copy_tree(DSH_ROOT / "node_modules", target / "agent_backend/deepseek_harness/node_modules")
    result = {"backend": "deepseek-harness", "version": DSH_VERSION, "node": version,
              "node_sha256": node_pin["node_sha256"]}
    manifest = target / "runtime/manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.update(agent=result, node=version)
        manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return result


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
    python_dll = "python" + "".join(metadata["version"].split(".")[:2]) + ".dll"
    for name in ("python.exe", "pythonw.exe", "python3.dll", python_dll, "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
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
    node_target = target / "runtime/node"
    dsh = stage_dsh_runtime(target)
    _copy_source(PROJECT_ROOT / "agent_backend", target / "agent_backend", BACKEND_SOURCE_SUFFIXES)
    _copy_source(PROJECT_ROOT / "desktop_pet", target / "desktop_pet", {".py"})
    (target / "shared/reasoning").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "shared/reasoning/profiles.json", target / "shared/reasoning/profiles.json")
    # The root package marker may not exist in a namespace source checkout.
    (target / "agent_backend").mkdir(exist_ok=True)
    version = subprocess.run([str(node_target / "node.exe"), "--version"], check=True,
                             capture_output=True, encoding="utf-8").stdout.strip()
    probe = _run_json([str(runtime / "python.exe"), "-I", "-c",
        "import json,sys,importlib.util;print(json.dumps({'python':sys.version.split()[0],'modules':{n:bool(importlib.util.find_spec(n)) for n in ['openai','keyring']}}))"])
    if not all(probe["modules"].values()):
        raise RuntimeError("Staged worker runtime is missing a required backend module")
    report = {"python": metadata["version"], "node": version, "agent": dsh, "isolated_imports": probe["modules"],
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
