"""Compare current project code and assets against a newly frozen release candidate."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import sys
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--payload", type=Path, required=True)
parser.add_argument("--tag", required=True)
parser.add_argument("--require-match", action="store_true")
args = parser.parse_args()
if not args.tag or any(x not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for x in args.tag):
    parser.error("Use a simple unique audit tag")
OUT = ROOT / "artifacts/release-validation" / args.tag
OUT.mkdir(parents=True, exist_ok=False)
payload = args.payload.resolve()
if not (payload / "鲸伴.exe").is_file():
    parser.error("Candidate executable must already exist; no build or app launch is performed")
archive = CArchiveReader(str(payload / "鲸伴.exe"))
pyz = archive.open_embedded_archive("PYZ.pyz")
def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()
def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()

embedded = []
source_fence = {}
staged = []
changes = []
for path in sorted((ROOT / "desktop_pet").rglob("*.py")):
    relative = path.relative_to(ROOT).as_posix()
    if "__pycache__" in path.parts:
        continue
    module = ".".join(Path(relative).with_suffix("").parts)
    if module.endswith(".__init__"):
        module = module[:-9]
    old = pyz.extract(module) if module in pyz.toc else None
    code = compile(path.read_bytes(), old.co_filename if old else relative, "exec", dont_inherit=True, optimize=0)
    source_fence[relative] = digest(path)
    row = {"module": module, "source": relative, "source_sha256": source_fence[relative],
        "embedded_code_matches": old == code}
    embedded.append(row)
    if old != code:
        changes.append({"category": "embedded_python", "source": relative, "change": "changed" if old else "new"})
current_modules = {r["module"] for r in embedded}
for module in sorted(pyz.toc):
    if (module == "desktop_pet" or module.startswith("desktop_pet.")) and module not in current_modules:
        changes.append({"category": "embedded_python", "module": module, "change": "removed_from_current_source"})
for name, relative in (("main", "main.py"),):
    source = ROOT / relative
    old = marshal.loads(archive.extract(name))
    source_fence[relative] = digest(source)
    matches = old == compile(source.read_bytes(), old.co_filename, "exec", dont_inherit=True, optimize=0)
    embedded.append({"module": name, "source": relative, "source_sha256": source_fence[relative], "embedded_code_matches": matches})
    if not matches:
        changes.append({"category": "embedded_entrypoint", "source": relative, "change": "changed"})

def add(source, target, category, *, semantic_json=False, expected_json=None):
    relative = source.relative_to(ROOT).as_posix()
    source_fence[relative] = digest(source)
    old = payload / target
    record = {"source": relative, "target": target, "category": category,
        "source_sha256": source_fence[relative], "comparison": "json" if semantic_json or expected_json is not None else "bytes"}
    if record["comparison"] == "json":
        expected = json.loads(source.read_text(encoding="utf-8-sig")) if expected_json is None else expected_json
        record["expected_json_sha256"] = json_digest(expected)
        try:
            actual = json.loads(old.read_text(encoding="utf-8-sig"))
            matches = expected == actual
        except (OSError, ValueError, UnicodeError):
            matches = False
    else:
        matches = old.is_file() and digest(old) == record["source_sha256"]
    record["payload_matches"] = matches
    staged.append(record)
    if not matches:
        changes.append({"category": category, "source": relative, "target": target, "change": "changed" if old.exists() else "new"})

excluded = {".venv", "node_modules", "__pycache__", "tests", "test", "runtime", "artifacts"}
worker_targets = set()
for directory, suffixes in (("agent_backend", {".py", ".mjs", ".js", ".ps1", ".json", ".txt"}), ("desktop_pet", {".py"})):
    for path in sorted((ROOT / directory).rglob("*")):
        if path.is_file() and path.suffix in suffixes and not any(p in excluded for p in path.relative_to(ROOT / directory).parts):
            target = "_internal/" + path.relative_to(ROOT).as_posix()
            worker_targets.add(target)
            add(path, target, "worker_source")
    old_base = payload / "_internal" / directory
    for old in old_base.rglob("*"):
        if old.is_file() and old.suffix in suffixes and not any(p in excluded for p in old.relative_to(old_base).parts):
            target = old.relative_to(payload).as_posix()
            if target not in worker_targets and target != "_internal/agent_backend/__init__.py":
                changes.append({"category": "worker_source", "target": target, "change": "removed_from_current_source"})
for path in sorted((ROOT / "browser-extension/whale-ask").rglob("*")):
    if path.is_file() and "__pycache__" not in path.parts:
        add(path, path.relative_to(ROOT).as_posix(), "browser_extension")
for relative in ("tools/install_browser_ask.ps1", "tools/uninstall_browser_ask.ps1"):
    add(ROOT / relative, relative, "browser_setup")
for relative in ("desktop_pet/ui/web_chat/index.html", "desktop_pet/ui/web_chat/THIRD_PARTY_NOTICES.txt"):
    add(ROOT / relative, "_internal/" + relative, "web_bundle")

spec = importlib.util.spec_from_file_location("read_only_release_builder", ROOT / "tools/build_windows_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
asset_calls = []
builder.copy = lambda source, destination: asset_calls.append((Path(source), Path(destination)))
normalized = OUT / "normalized_assets"
builder.stage_assets(normalized)
asset_targets = set()
for source, target in asset_calls:
    relative = target.relative_to(normalized).as_posix()
    asset_targets.add(relative)
    add(source, "_internal/" + relative, "asset", semantic_json=source.suffix == ".json")
for path in normalized.rglob("*.json"):
    relative = path.relative_to(normalized).as_posix()
    source = ROOT / relative
    asset_targets.add(relative)
    add(source, "_internal/" + relative, "normalized_animation_metadata", expected_json=json.loads(path.read_text(encoding="utf-8")))
old_assets = payload / "_internal/assets"
for old in old_assets.rglob("*"):
    relative = old.relative_to(payload / "_internal").as_posix()
    # Engine assets have their own pinned staging/provenance rather than the regular asset filter.
    if old.is_file() and "engines" not in old.relative_to(old_assets).parts and relative not in asset_targets:
        changes.append({"category": "asset", "target": "_internal/" + relative, "change": "removed_from_current_source"})

# Freeze build descriptions and frontend inputs separately; only the built web bundle is shipped.
build_inputs = []
for base in (ROOT / "frontend/chat-ui/src",):
    build_inputs.extend(p for p in base.rglob("*") if p.is_file())
for relative in ("requirements.txt", "requirements-build.txt", "requirements-remote.txt", "main.py", "tools/build_windows_release.py",
    "tools/packaging/jingban.spec", "tools/packaging/jingban.iss", "tools/packaging/windows_version.txt", "tools/packaging/worker_runtime.py",
    "frontend/chat-ui/package.json", "frontend/chat-ui/package-lock.json", "shared/reasoning/profiles.json"):
    if (ROOT / relative).is_file():
        build_inputs.append(ROOT / relative)
for path in build_inputs:
    source_fence[path.relative_to(ROOT).as_posix()] = digest(path)
source_fence[Path(__file__).relative_to(ROOT).as_posix()] = digest(Path(__file__))
source_stable = all((ROOT / p).is_file() and digest(ROOT / p) == value for p, value in source_fence.items())
report = {"passed": not changes and source_stable, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "payload": str(payload), "embedded_inventory": embedded, "staged_inventory": staged, "changes": changes,
    "source_stable": source_stable, "source_sha256": source_fence,
    "scope": "Every current desktop_pet Python module plus main; all project worker sources selected by staging; browser extension/setup; built web UI; regular assets including normalized animation JSON. Runtime third-party binaries and pinned engine staging are outside this source comparison.",
    "real_provider_calls": 0, "production_configuration_accessed": False}
(OUT / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"passed": report["passed"], "embedded_count": len(embedded), "staged_count": len(staged),
    "source_count": len(source_fence), "source_stable": source_stable, "changes": changes,
    "report": str(OUT / "comparison.json")}, ensure_ascii=False))
if args.require_match and not report["passed"]:
    raise SystemExit(1)
