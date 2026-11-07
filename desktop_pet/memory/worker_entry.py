"""Fixed packaged worker entry; -I excludes ambient Python paths."""
from pathlib import Path
import os
import runpy
import sys

if __name__ == "__main__":
    root = str(Path(__file__).resolve().parents[2])
    # The GUI's resource root also holds CPython 3.10 extension modules. The
    # independent worker's own stdlib/DLLs must keep priority over that root.
    if os.environ.get("JINGBAN_PACKAGED") == "1":
        sys.path.append(root)
    else:
        sys.path.insert(0, root)
    runpy.run_module("desktop_pet.memory.worker", run_name="__main__")
