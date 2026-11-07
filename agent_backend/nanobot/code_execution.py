"""Small, cancellable value-only code execution, with zero host capabilities."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys


class CodeExecutionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json_input(value, depth=0):
    """Reject coercions (tuples, object keys, nonfinite numbers, invalid UTF-8)."""
    if depth > 128:
        raise ValueError("JSON nesting limit")
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        value.encode("utf-8")
    elif type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("Use decimal strings for exact large integers")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite JSON number")
    elif type(value) is list:
        for item in value:
            _json_input(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Non-string JSON object key")
            key.encode("utf-8")
            _json_input(item, depth + 1)
    else:
        raise ValueError("Non-JSON input")


async def _settle(task):
    """Finish termination even if another Stop arrives while reaping a child."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    return task.result()


async def _terminate(process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def available():
    return importlib.util.find_spec("quickjs") is not None


async def run_code(code, input=None):
    if not isinstance(code, str) or not 1 <= len(code) <= 65536:
        raise CodeExecutionError("CODE_INVALID_REQUEST")
    try:
        code.encode("utf-8")
        _json_input(input)
        raw = json.dumps({"code": code, "input": input}, ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise CodeExecutionError("CODE_INVALID_REQUEST") from None
    if len(raw) > 2 * 1024 * 1024:
        raise CodeExecutionError("CODE_INPUT_LIMIT")
    if not available():
        raise CodeExecutionError("CODE_RUNTIME_MISSING")
    # The helper sees no provider credentials, profile paths or proxy secrets.
    environment = {name: value for name, value in os.environ.items()
                   if name.casefold() in {"systemroot", "windir", "systemdrive"}}
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable, "-I", "-u",
        str(Path(__file__).with_name("code_worker.py")), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path(__file__).parent), env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Cancellation during process creation must not orphan an untracked
        # helper after the caller has already returned from Stop.
        process = await _settle(spawn)
        await _settle(asyncio.create_task(_terminate(process)))
        raise
    except OSError:
        raise CodeExecutionError("CODE_RUNTIME_FAILED") from None
    try:
        output, _ = await asyncio.wait_for(process.communicate(raw), timeout=6)
        if process.returncode != 0 or len(output) > 1048576:
            raise CodeExecutionError("CODE_RUNTIME_FAILED")
        value = json.loads(output)
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise CodeExecutionError("CODE_RUNTIME_FAILED")
        if not value["ok"]:
            code = value.get("code", "CODE_RUNTIME_FAILED")
            allowed = {"CODE_INVALID_REQUEST", "CODE_INPUT_LIMIT", "CODE_OUTPUT_LIMIT", "CODE_TIME_LIMIT",
                       "CODE_MEMORY_LIMIT", "CODE_EVALUATION_FAILED", "CODE_RUNTIME_MISSING"}
            raise CodeExecutionError(code if code in allowed else "CODE_RUNTIME_FAILED")
        if set(value) != {"ok", "result", "runtime", "scope"} or value.get("runtime") != "quickjs-ng" or value.get("scope") != "pure_json_computation":
            raise CodeExecutionError("CODE_RUNTIME_FAILED")
        try:
            rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
            rendered.encode("utf-8")
            return rendered
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise CodeExecutionError("CODE_RUNTIME_FAILED") from None
    except asyncio.TimeoutError:
        raise CodeExecutionError("CODE_TIME_LIMIT") from None
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise CodeExecutionError("CODE_RUNTIME_FAILED") from None
    finally:
        if process.returncode is None:
            await _settle(asyncio.create_task(_terminate(process)))
