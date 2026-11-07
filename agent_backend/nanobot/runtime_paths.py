"""Relocatable optional sidecar lookup for plain-Python backend workers."""
from pathlib import Path
import shutil


def node_executable():
    bundled = Path(__file__).resolve().parents[2] / "runtime" / "node" / "node.exe"
    return str(bundled) if bundled.is_file() else shutil.which("node")
