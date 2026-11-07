"""Trusted second-stage body, started only by the Win32k-verified MXC broker.

The host stages this file and its request read-only. The broker queries the
exact suspended process's kernel policy before its first instruction runs.
No model code is evaluated by that first-stage broker.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def main():
    try:
        # -I ignores PYTHONIOENCODING; configure the actual inherited wire.
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="strict")
        request = json.loads(Path(__file__).with_name("request.json").read_bytes())
        if type(request) is not dict or set(request) != {"version", "code", "input"} or request["version"] != 1:
            return 2
        code = request["code"]
        if type(code) is not str or not 1 <= len(code.encode("utf-8")) <= 65536 or "\0" in code:
            return 2
        os.chdir(Path(__file__).parent.parent / "task")
        namespace = {"__name__": "__main__", "input": request["input"],
            "INPUT_DIR": str(Path(__file__).parent / "files"), "OUTPUT_DIR": str(Path.cwd())}
        exec(compile(code, "<aura-mxc-python>", "exec"), namespace)
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
