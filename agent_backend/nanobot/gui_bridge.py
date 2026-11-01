"""Credential-free argv, JSONL-only UI-TARS subprocess bridge."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from time import monotonic
from gui_verification import checked_report, report, utc_now, UNCONFIRMED_REPLY


_FINALIZATION_RESERVE_SECONDS = 10.0


GUI_ERROR_CODES = frozenset({
    "GUI_ALREADY_USED", "GUI_RUNTIME_MISSING", "GUI_START_FAILED", "GUI_EXITED",
    "GUI_PROTOCOL", "GUI_FAILED", "GUI_SDK_UNAVAILABLE", "GUI_UNSUPPORTED_PLATFORM",
    "GUI_STEP_LIMIT", "GUI_SCREENSHOT_FAILED", "GUI_MODEL_FAILED", "GUI_ACTION_FAILED", "GUI_CONTEXT_LENGTH_EXCEEDED",
    "GUI_DESKTOP_LOCKED", "GUI_DESKTOP_SESSION_UNAVAILABLE", "GUI_SCREENSHOT_DISPLAY_UNAVAILABLE", "GUI_SCREENSHOT_BLACK_FRAME",
    "GUI_UNSUPPORTED_ACTION", "GUI_SDK_FAILED", "GUI_INVALID_REQUEST",
    "GUI_INVALID_PROVIDER", "GUI_BUSY", "GUI_WORKER_FAILED",
})
_GUI_WORKER_ERRORS = {
    code.removeprefix("GUI_"): code for code in GUI_ERROR_CODES
    if code not in {"GUI_ALREADY_USED", "GUI_RUNTIME_MISSING", "GUI_START_FAILED",
                    "GUI_EXITED", "GUI_PROTOCOL", "GUI_FAILED"}
}


class BridgeFailure(Exception):
    """Only a fixed, credential-free code may cross the worker boundary."""
    def __init__(self, code: str):
        self.code = code if code in GUI_ERROR_CODES else "GUI_FAILED"
        super().__init__(self.code)


def authorized_gui_callback(bridge, original_task: str):
    """Bind the host's authorization; model tool arguments cannot widen it."""
    async def execute(**_model_arguments):
        return await bridge.run(original_task)
    return execute


class GuiBridge:
    def __init__(self, config: dict, request_id: str, options: dict, emit, *, reference_context="",
                 deadline_monotonic: float | None = None):
        self.config = config
        self.request_id = request_id
        self.options = options
        self.emit = emit
        self.reference_context = reference_context
        # Internal absolute task deadline, never accepted from model arguments.
        # Planning and subprocess startup consume the same total task budget.
        self.deadline_monotonic = deadline_monotonic
        self.process: asyncio.subprocess.Process | None = None
        self.last_status = "completed"
        self.steps = 0
        self.called = False
        self.failed = False
        self.failure_code = None
        self.verification = None
        self.started_at = None
        self._execution_lock = asyncio.Lock()
        self._cached_result: str | None = None

    def _timeout_budget_ms(self) -> int:
        configured = int(self.options.get("timeout_seconds", 0) * 1000)
        if configured == 0:
            # Shared protocol sentinel: no complete-task timer in UI-TARS.
            # Do not replace it with the previous hidden 600-second cap.
            return 0
        if self.deadline_monotonic is None:
            return configured
        remaining = self.deadline_monotonic - monotonic() - _FINALIZATION_RESERVE_SECONDS
        return min(configured, max(0, int(remaining * 1000)))

    def _insufficient_time(self) -> str:
        self.last_status = "timed_out"
        return json.dumps({"status": "timed_out",
            "summary": "本次任务剩余时间不足，未开始新的电脑操作；已完成的操作保留。",
            "steps": self.steps}, ensure_ascii=False)

    def record_failure(self, code: str) -> None:
        self.failed = True
        # Retain the initial cause if the model attempts an unauthorized retry.
        if self.failure_code is None:
            self.failure_code = code if code in GUI_ERROR_CODES else "GUI_FAILED"

    async def run(self, task: str) -> str:
        # One bridge belongs to one host-authorized task. The GUI already runs
        # its own multi-step loop for that entire goal. A planner asking again
        # must see the same outcome, not replay input or turn success into a
        # GUI_ALREADY_USED failure. The lock also covers parallel tool calls.
        async with self._execution_lock:
            if self._cached_result is not None:
                return self._cached_result
            if self.called:
                raise BridgeFailure(self.failure_code or "GUI_ALREADY_USED")
            self.called = True
            try:
                self._cached_result = await self._run_once(task)
                return self._cached_result
            except asyncio.CancelledError:
                self.last_status = "cancelled"
                self._cached_result = json.dumps({"status": "cancelled",
                    "summary": "任务已停止；未重新执行电脑操作。", "steps": self.steps}, ensure_ascii=False)
                raise

    async def _run_once(self, task: str) -> str:
        try:
            self.started_at = utc_now()
            node = shutil.which("node")
            worker = Path(__file__).resolve().parent.parent / "ui_tars" / "worker.mjs"
            if node is None or not worker.is_file():
                raise BridgeFailure("GUI_RUNTIME_MISSING")
            env = {key: value for key, value in os.environ.items()
                   if not any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
            # Explicit finite limits reserve final-report time. Zero instead
            # means unrestricted task duration and must remain zero end to end.
            if self.options.get("timeout_seconds", 0) > 0 and self._timeout_budget_ms() < 1000:
                return self._insufficient_time()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(Path(node).resolve()), str(worker), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env=env, creationflags=0x08000000 if os.name == "nt" else 0,
                    limit=131_072,
                )
            except OSError:
                raise BridgeFailure("GUI_START_FAILED") from None
            assert self.process.stdin is not None and self.process.stdout is not None
            timeout_ms = self._timeout_budget_ms()
            if self.options.get("timeout_seconds", 0) > 0 and timeout_ms < 1000:
                # A slow process spawn must not dispatch a task after expiry.
                return self._insufficient_time()
            reference_context = self.reference_context
            if self.config.get("api_key"):
                reference_context = reference_context.replace(self.config["api_key"], "[redacted]")
            payload = {"type": "run", "id": self.request_id, "task": task,
                       "reference_context": reference_context,
                       "config": {"baseURL": self.config["base_url"], "apiKey": self.config["api_key"],
                                  "model": self.config["model"], "provider": self.config.get("provider", "ui-tars-1.5"),
                                  "coordinate_profile": self.config.get("coordinate_profile", "native")},
                       "options": {"maxSteps": self.options["max_steps"],
                                   "timeoutMs": timeout_ms,
                                   "verifyOnly": self.options.get("verify_only", False),
                                   "pythonExecutable": sys.executable}}
            self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    raise BridgeFailure("GUI_EXITED")
                try:
                    frame = json.loads(raw)
                except (UnicodeError, ValueError, TypeError, RecursionError):
                    raise BridgeFailure("GUI_PROTOCOL") from None
                if not isinstance(frame, dict):
                    raise BridgeFailure("GUI_PROTOCOL")
                if frame.get("type") == "ready":
                    continue
                if frame.get("id") != self.request_id:
                    continue
                if frame.get("type") == "progress":
                    self.emit("progress", phase="computer")
                    if self.options.get("include_activity"):
                        stage, step = frame.get("stage"), frame.get("step", 0)
                        action = (frame.get("action") if stage == "acting" else "observe" if stage == "observing"
                                  else "verify" if stage == "verifying" else None)
                        allowed = {"observe", "verify", "click", "left_click", "left_single", "left_double", "double_click",
                                   "right_click", "right_single", "middle_click", "drag", "left_click_drag", "select",
                                   "hover", "mouse_move", "type", "hotkey", "scroll", "wait", "finished", "call_user",
                                   "error_env", "user_stop"}
                        if isinstance(action, str) and action in allowed and type(step) is int and step >= 0:
                            state = frame.get("status", "completed")
                            if state in ("running", "completed", "failed"):
                                self.emit("activity", kind="progress" if action == "observe" else "computer", name=action,
                                          event_id=f"gui-{step}-{stage}", status=state, step=step)
                elif frame.get("type") == "error":
                    code = frame.get("code")
                    # Never echo the child's message, exception, task or model reasoning.
                    mapped = _GUI_WORKER_ERRORS.get(code, "GUI_FAILED") if isinstance(code, str) else "GUI_FAILED"
                    raise BridgeFailure(mapped)
                elif frame.get("type") == "final":
                    status = frame.get("status")
                    if status not in {"completed", "needs_user", "cancelled", "timed_out"}:
                        raise BridgeFailure("GUI_PROTOCOL")
                    steps, summary = frame.get("steps", 0), frame.get("summary", "")
                    if (type(steps) is not int or steps < 0
                            or not isinstance(summary, str)):
                        raise BridgeFailure("GUI_PROTOCOL")
                    self.verification = checked_report(frame.get("verification"), started_at=self.started_at)
                    if status == "completed" and not (self.verification and self.verification["status"] == "verified"):
                        status = "needs_user"
                        self.verification = self.verification or report()
                        summary = UNCONFIRMED_REPLY
                    if status in {"cancelled", "timed_out"}:
                        self.verification = None
                    self.last_status = status
                    self.steps += steps
                    if self.config.get("api_key"):
                        summary = summary.replace(self.config["api_key"], "[redacted]")
                    return json.dumps({"status": status, "summary": summary,
                                       "steps": self.steps, "verification": self.verification}, ensure_ascii=False)
                else:
                    raise BridgeFailure("GUI_PROTOCOL")
        except BridgeFailure as exc:
            self.record_failure(exc.code)
            raise
        except (ValueError, UnicodeError, TypeError):
            self.record_failure("GUI_PROTOCOL")
            raise BridgeFailure("GUI_PROTOCOL") from None
        except Exception:
            self.record_failure("GUI_FAILED")
            raise BridgeFailure("GUI_FAILED") from None
        finally:
            await self.close()

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        if process.returncode is None:
            if process.stdin is not None:
                try:
                    process.stdin.write((json.dumps({"type": "abort", "id": self.request_id}) + "\n").encode())
                    await process.stdin.drain()
                    process.stdin.close()
                except (BrokenPipeError, ConnectionError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), 2.5)
            except asyncio.TimeoutError:
                # A native action already dispatched cannot be undone. Stop the
                # complete helper tree if graceful abort misses its boundary.
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=0x08000000,
                    )
                    await killer.wait()
                elif process.returncode is None:
                    process.kill()
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                except asyncio.TimeoutError:
                    pass
