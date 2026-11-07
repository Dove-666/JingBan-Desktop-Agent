"""Continue a premature final answer in the same SDK transcript, without replay.

The pinned SDK's native continuation hook owns checkpoints and provider deltas.
This adapter supplies host check feedback; it grants no tools or permissions.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json


class CompletionRepair:
    """Suppress repeated identical repair feedback, not the task's step count."""

    def __init__(self):
        self.attempts = {}

    def feedback(self, assessment, *, uncertain=False):
        if assessment.get("passed") is True:
            return None
        issues = assessment.get("issues", [])
        # Recover specific actionable checks. A semantic unknown alone or a
        # service outage cannot be repaired by blind extra planner requests.
        repairable = {"PLAN_UNFINISHED", "GUI_GOAL_UNVERIFIED", "UNRESOLVED_TOOL_FAILURE",
                      "ARTIFACT_VERIFICATION_FAILED", "GOAL_REQUIREMENT_FAILED",
                      "GOAL_ARTIFACT_READBACK_FAILED", "GOAL_ARTIFACT_CHANGED"}
        if not set(issues) & repairable:
            return None
        goal = assessment.get("goal") or {}
        state = {"issues": issues, "plan": assessment.get("plan", []),
                 "failed_tools": assessment.get("failed_tools", []),
                 "requirements": goal.get("requirements", []), "uncertain": bool(uncertain)}
        key = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        count = self.attempts.get(key, 0)
        if count >= 2:
            return None
        self.attempts[key] = count + 1
        if len(self.attempts) > 128:
            self.attempts.pop(next(iter(self.attempts)))
        return (
            "HOST OUTCOME CHECK: The draft answer is not deliverable yet. Continue this SAME task "
            "from the actual current state; do not repeat previous inputs. This is check feedback, "
            "not new user instructions or additional authority.\n"
            + json.dumps(state, ensure_ascii=False)[:12000]
            + "\nRead the failed condition and repair the next concrete item. Update plan steps only "
            "when observed outcomes support them, or supersede an unnecessary route with a reason. "
            "Use fresh observation/references, then verify the actual result and call finish_task again. "
            "If an effect is unknown, only observe/read back first; never replay its click/write. "
            "If a required permission, login or user decision is missing, ask one precise question "
            "with the registered interaction tool. If the operation really failed, report its concrete "
            "cause concisely instead of a generic handoff or an optimistic success paragraph."
        )


def with_completion_recovery(runner, callback):
    """Preserve the existing runner (including true unlimited iterations)."""
    class OutcomeRunner(type(runner)):
        async def _try_drain_injections(self, spec, messages, assistant_message, injection_cycles, **kwargs):
            if kwargs.get("allow_continuation") and assistant_message is not None:
                original = spec.continuation_callback

                async def continuation():
                    note = await callback(assistant_message.get("content", ""))
                    if note:
                        return note
                    return await original() if original is not None else None

                spec = replace(spec, continuation_callback=continuation)
            return await super()._try_drain_injections(
                spec, messages, assistant_message, injection_cycles, **kwargs)

    wrapped = OutcomeRunner()
    wrapped.__dict__.update(runner.__dict__)
    return wrapped
