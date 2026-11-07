"""Credential-free argv, JSONL-only UI-TARS subprocess bridge."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from time import monotonic
from gui_verification import checked_feedback, checked_report, report, utc_now, UNCONFIRMED_REPLY
from structured_observations import StructuredObservations


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
        self.effect_state = "not_dispatched"
        super().__init__(self.code)


def authorized_gui_callback(bridge, original_task: str, *, action_id=None, verify_only=False):
    """Bind the host's authorization; model tool arguments cannot widen it."""
    async def execute(**model_arguments):
        # Older callers deliberately ignored the model's task argument. New
        # explicit action identities permit bounded subgoals, never a new parent
        # authorization. The host wrapper owns action IDs and resource locks.
        return await bridge.run(original_task, action_id=action_id,
            subtask=model_arguments.get("subtask") if action_id is not None else None,
            verify_only=verify_only)
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
        self.verification_scope = "parent"
        self.started_at = None
        self._execution_lock = asyncio.Lock()
        self._cached_result: str | None = None
        self._results = {}
        self._payloads = {}
        self._errors = {}
        self._current_subtask = None
        self._current_verify_only = False
        self._current_action_id = None
        self._actions_dispatched = 0
        self._uncertain_effect = False
        self.action_epoch = 0
        self.effect_state = "not_dispatched"
        self._wire_id = self.request_id
        self._running_action_id = None
        self._pause_requested = False
        self._structured_observations = StructuredObservations(secrets=(config.get("api_key"), config.get("apiKey")))

    def remember_structured_observation(self, result_json: str) -> bool:
        """Host-only handoff of a real DesktopControls.observe result.

        This is never a model tool or an accepted action/summary argument.
        It records zero input effects and cannot create a goal-verification pass.
        """
        return self._structured_observations.remember(result_json)

    @property
    def pending_effect(self):
        """An input exception/cancellation is different from a failed goal check."""
        return self._uncertain_effect

    def resolve_uncertainty(self, verification):
        """Host-only settlement after an independent fresh passing observation.

        This changes permission to continue with a NEW action identity, never
        the cached outcome of the old identity and never replays old input.
        """
        value = checked_report(verification, started_at=self.started_at)
        if value is None or value["status"] != "verified":
            return False
        self._uncertain_effect = False
        self.effect_state = "confirmed" if self.called else "not_dispatched"
        self.failed, self.failure_code = False, None
        return True

    async def pause_current(self, action_id=None):
        """Interrupt only the active subgoal so the parent can apply steering.

        A quiet, call-bound abort cannot stop a later subgoal if the old one
        settles concurrently. This never cancels the owning Agent task.
        """
        active = self._running_action_id
        if active is None or action_id is not None and action_id != active:
            return {"paused": False, "action_id": active}
        self._pause_requested = True
        process = self.process
        if process is not None and process.returncode is None and process.stdin is not None:
            try:
                process.stdin.write((json.dumps({"type": "abort", "id": self._wire_id, "quiet": True}) + "\n").encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError):
                # An already dispatched operation may have finished or become
                # uncertain; the normal read/settlement path owns that result.
                pass
        return {"paused": True, "action_id": active}

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

    def _settle_failure(self, failure: BridgeFailure) -> BridgeFailure:
        self._uncertain_effect = self._uncertain_effect or self.effect_state == "uncertain"
        failure.effect_state = self.effect_state
        self.record_failure(failure.code)
        return failure

    async def run(self, task: str, *, action_id=None, subtask=None, verify_only=False) -> str:
        """Run an identified subgoal, settling each input effect independently.

        A legacy call keeps its original one-shot semantics. Explicit identities
        permit sequential UIA/visual/native work: a failed postcondition is an
        observation for the planner, not an uncertain input dispatch. Repeating
        an identity returns its exact result; changing its arguments is rejected.
        """
        explicit = action_id is not None
        if explicit and (not isinstance(action_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", action_id)):
            raise BridgeFailure("GUI_INVALID_REQUEST")
        if (not isinstance(task, str) or not task.strip() or len(task) > 16000
                or subtask is not None and (not isinstance(subtask, str) or not subtask.strip() or len(subtask) > 8000)
                or type(verify_only) is not bool):
            raise BridgeFailure("GUI_INVALID_REQUEST")
        identity = action_id or "legacy"
        payload = hashlib.sha256(json.dumps({"task": task, "subtask": subtask,
            "verify_only": verify_only}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self._execution_lock:
            if not explicit and self._cached_result is not None:
                return self._cached_result
            if explicit and identity in self._payloads and self._payloads[identity] != payload:
                raise BridgeFailure("GUI_INVALID_REQUEST")
            if identity in self._results:
                return self._results[identity]
            if identity in self._errors:
                code, effect_state = self._errors[identity]
                failure = BridgeFailure(code)
                failure.effect_state = effect_state
                raise failure
            if not explicit and self.called:
                raise BridgeFailure(self.failure_code or "GUI_ALREADY_USED")
            if self._uncertain_effect and not verify_only:
                return json.dumps({"status": "needs_user", "effect_state": "uncertain",
                    "reason": "UNKNOWN_INPUT_EFFECT", "action_epoch": self.action_epoch,
                    "summary": "先前输入的派发结果尚不明确，请先只读观察和核对，未重放输入。"}, ensure_ascii=False)
            self._payloads[identity] = payload
            self._current_subtask = subtask
            self.verification_scope = "subgoal" if subtask and subtask.strip() != task.strip() else "parent"
            self._current_verify_only = verify_only
            self._current_action_id = action_id
            self._wire_id = (self.request_id[:100] + ":" + hashlib.sha256(action_id.encode()).hexdigest()[:12]
                             if action_id else self.request_id)
            self._running_action_id = identity
            self._pause_requested = False
            self._actions_dispatched = 0
            self.effect_state = "not_dispatched"
            self.failed, self.failure_code = False, None
            self.called = True
            try:
                result = await self._run_once(task)
                self._results[identity] = result
                if not explicit:
                    self._cached_result = result
                return result
            except BridgeFailure as exc:
                self._errors[identity] = (exc.code, exc.effect_state)
                raise
            except asyncio.CancelledError:
                self.last_status = "cancelled"
                self._uncertain_effect = self._uncertain_effect or self.effect_state == "uncertain"
                result = json.dumps({"status": "cancelled", "effect_state":
                    "uncertain" if self._uncertain_effect else "confirmed" if self._actions_dispatched else "not_dispatched",
                    "summary": "任务已停止；未重新执行电脑操作。", "steps": self.steps}, ensure_ascii=False)
                self._results[identity] = result
                if not explicit:
                    self._cached_result = result
                raise
            finally:
                self._running_action_id = None
                self._pause_requested = False

    async def _run_once(self, task: str) -> str:
        try:
            self.started_at = utc_now()
            try:
                from .runtime_paths import node_executable
            except ImportError:
                from runtime_paths import node_executable
            node = node_executable()
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
                if self.process is None or self.process.returncode is not None:
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
            if self._pause_requested:
                self.last_status = "cancelled"
                return json.dumps({"status": "cancelled", "effect_state": "not_dispatched",
                    "summary": "已暂停当前子目标，等待父任务处理新的用户指令。", "steps": self.steps}, ensure_ascii=False)
            reference_context = self.reference_context
            if self.config.get("api_key"):
                reference_context = reference_context.replace(self.config["api_key"], "[redacted]")
            payload = {"type": "run", "id": self._wire_id, "task": task,
                       **({"subtask": self._current_subtask} if self._current_subtask else {}),
                       "reference_context": reference_context,
                       "config": {"baseURL": self.config["base_url"], "apiKey": self.config["api_key"],
                                  "model": self.config["model"], "provider": self.config.get("provider", "ui-tars-1.5"),
                                  "coordinate_profile": self.config.get("coordinate_profile", "native")},
                       "options": {"maxSteps": self.options["max_steps"],
                                   "timeoutMs": timeout_ms,
                                   "verifyOnly": self._current_verify_only or self.options.get("verify_only", False),
                                   "pythonExecutable": sys.executable}}
            observations = self._structured_observations.payload()
            if observations["frames"]:
                payload["structured_observations"] = observations
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
                if frame.get("id") != self._wire_id:
                    continue
                if frame.get("type") == "progress":
                    if (frame.get("stage") == "acting" and frame.get("status") == "running"
                            and frame.get("action") not in {None, "finished", "call_user", "error_env", "user_stop", "wait"}):
                        # Before input starts, cancellation can leave delivery
                        # uncertain; a terminal frame settles the dispatch.
                        self._actions_dispatched += 1
                        self.effect_state = "uncertain"
                    elif (frame.get("stage") == "acting" and frame.get("status") == "completed"
                          and self._actions_dispatched):
                        self.effect_state = "confirmed"
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
                                action_prefix = (hashlib.sha256(self._current_action_id.encode()).hexdigest()[:12] + "-"
                                                 if self._current_action_id else "")
                                self.emit("activity", kind="progress" if action == "observe" else "computer", name=action,
                                          event_id=f"gui-{action_prefix}{step}-{stage}", status=state, step=step)
                elif frame.get("type") == "error":
                    if frame.get("effect_state") in {"not_dispatched", "confirmed", "uncertain"}:
                        self.effect_state = frame["effect_state"]
                    if frame.get("effect_state") == "not_dispatched":
                        self._actions_dispatched = 0
                    elif frame.get("effect_state") == "confirmed":
                        self._actions_dispatched = 0
                    elif frame.get("effect_state") == "uncertain":
                        self._uncertain_effect = True
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
                    if "actions_dispatched" in frame:
                        if type(frame["actions_dispatched"]) is not int or frame["actions_dispatched"] < 0:
                            raise BridgeFailure("GUI_PROTOCOL")
                        self._actions_dispatched = frame["actions_dispatched"]
                    if self._actions_dispatched:
                        self.action_epoch += 1
                    effect_state = frame.get("effect_state")
                    if effect_state not in {"not_dispatched", "confirmed", "uncertain"}:
                        effect_state = ("uncertain" if status in {"cancelled", "timed_out"} and self._actions_dispatched
                                        else "confirmed" if self._actions_dispatched else "not_dispatched")
                    if self._current_verify_only and self._uncertain_effect:
                        # A fresh passing observation can settle the goal, but
                        # missing evidence must not erase uncertain prior input.
                        if not (self.verification and self.verification["status"] == "verified"):
                            effect_state = "uncertain"
                    self._uncertain_effect = effect_state == "uncertain"
                    self.effect_state = effect_state
                    feedback = checked_feedback(frame.get("feedback"), secrets=(self.config.get("api_key"),))
                    if self.config.get("api_key"):
                        summary = summary.replace(self.config["api_key"], "[redacted]")
                    return json.dumps({"status": status, "summary": summary,
                                       "steps": self.steps, "verification": self.verification,
                                       "verification_scope": self.verification_scope,
                                       "effect_state": effect_state, "action_epoch": self.action_epoch,
                                       **({"feedback": feedback} if feedback else {})}, ensure_ascii=False)
                else:
                    raise BridgeFailure("GUI_PROTOCOL")
        except BridgeFailure as exc:
            self._settle_failure(exc)
            raise
        except (ValueError, UnicodeError, TypeError):
            raise self._settle_failure(BridgeFailure("GUI_PROTOCOL")) from None
        except Exception:
            raise self._settle_failure(BridgeFailure("GUI_FAILED")) from None
        finally:
            # Explicit subgoals share the task-owned Node process and bounded
            # evidence. The outer worker always closes it at task settlement.
            # Legacy one-shot callers and errors retain immediate cleanup.
            if self._current_action_id is None or self.failed:
                await self.close()

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        if process.returncode is None:
            if process.stdin is not None:
                try:
                    process.stdin.write((json.dumps({"type": "abort", "id": self._wire_id, "quiet": True}) + "\n").encode())
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
