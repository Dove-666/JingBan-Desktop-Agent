"""Fixed container entrypoint; the OS boundary is Docker, not this Python code.

This file is copied into an owned read-only input mount. Never invoke it on
the host with model code. stdout/stderr and generated files are untrusted data.
"""
from __future__ import annotations

import json
import sys
import traceback


def main():
    raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        return 2
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "code", "input"} or value["version"] != 1:
            return 2
        code = value["code"]
        if not isinstance(code, str) or not 1 <= len(code.encode("utf-8")) <= 65536 or "\0" in code:
            return 2
        namespace = {"__name__": "__main__", "input": value["input"],
                     "INPUT_DIR": "/aura/input", "OUTPUT_DIR": "/aura/output"}
        exec(compile(code, "<aura-container-python>", "exec"), namespace)
        return 0
    except BaseException:
        # No host paths, credentials or real documents enter this process.
        # The host still treats this traceback as bounded, untrusted stderr.
        traceback.print_exc(limit=8)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
