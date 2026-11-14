"""Provision only the pinned official Node archive; never copy PATH Node."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "third_party/node/runtime.json"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pinned_runtime():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def validate_runtime(directory, pin=None):
    pin = pin or pinned_runtime()
    directory = Path(directory)
    try:
        return (sha256(directory / "node.exe") == pin["node_sha256"]
                and sha256(directory / "LICENSE") == pin["license_sha256"]
                and json.loads((directory / "provenance.json").read_text(encoding="utf-8")) == pin)
    except (OSError, ValueError):
        return False


def provision_node(*, archive=None, target=None):
    """A seed ZIP is optional; missing caches download the same checked URL."""
    pin = pinned_runtime()
    target = Path(target or ROOT / "runtime/node").resolve()
    if validate_runtime(target, pin):
        return target
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-stage-", dir=target.parent) as temporary:
        stage = Path(temporary)
        source = Path(archive).resolve() if archive else stage / "node.zip"
        if archive is None:
            with urllib.request.urlopen(pin["archive_url"], timeout=120) as response, source.open("wb") as output:
                shutil.copyfileobj(response, output)
        if sha256(source) != pin["archive_sha256"]:
            raise ValueError("Official Node archive checksum does not match the pinned release")
        prefix = "node-v" + pin["version"] + "-" + pin["platform"] + "/"
        with zipfile.ZipFile(source) as bundle:
            for name, expected in (("node.exe", pin["node_sha256"]), ("LICENSE", pin["license_sha256"])):
                entry = bundle.getinfo(prefix + name)
                if entry.file_size > 160 * 1024 * 1024:
                    raise ValueError("Node distribution member exceeds its expected size")
                with bundle.open(entry) as input_stream, (stage / name).open("wb") as output:
                    shutil.copyfileobj(input_stream, output)
                if sha256(stage / name) != expected:
                    raise ValueError("Node distribution member checksum does not match")
        (stage / "provenance.json").write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
        for name in ("node.exe", "LICENSE", "provenance.json"):
            os.replace(stage / name, target / name)
    if not validate_runtime(target, pin):
        raise ValueError("Node runtime verification failed")
    return target


def stage_node(target):
    source = provision_node()
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("node.exe", "LICENSE", "provenance.json"):
        shutil.copy2(source / name, target / name)
    if not validate_runtime(target):
        raise ValueError("Staged Node runtime differs from the pinned release")
    return pinned_runtime()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Optional cached official ZIP; checksum is still required")
    args = parser.parse_args()
    print(provision_node(archive=args.archive))
