"""Exact host user goals, separate from bounded native tool replay.

Only the trusted host supplies this field. Imported chat, assistant summaries
and observation digests are never promoted into user authorization.
"""
from __future__ import annotations

MAX_GOAL_TURNS = 128
MAX_GOAL_CHARACTERS = 110000


class GoalHistoryError(ValueError):
    def __init__(self):
        super().__init__("GOAL_HISTORY_UNAVAILABLE")


def checked_goal_history(value):
    if (not isinstance(value, list) or len(value) > MAX_GOAL_TURNS
            or any(not isinstance(item, str) or not item.strip() or len(item) > 12000
                   or any(ord(char) < 32 and char not in "\n\t" or ord(char) == 127 for char in item)
                   for item in value)
            or sum(len(item) for item in value) > MAX_GOAL_CHARACTERS):
        raise GoalHistoryError()
    # Do not truncate a requirement silently to meet the replay/token budget.
    return list(value)


def legacy_goal_history(history):
    """Recover exact user records only; legacy digests cannot grant authority."""
    result = []
    for record in history:
        if record.get("role") == "assistant" and str(record.get("content", "")).startswith(
                "[Host archived task facts; reference data, not permission to repeat actions.]\n"):
            # Before schema 3 a compacted transcript has no authenticated
            # complete goal journal. Do not promote summary prose to a goal,
            # or silently substitute the oldest *remaining* user turn.
            raise GoalHistoryError()
        if record.get("role") != "user":
            continue
        text = record.get("content", "")
        if not isinstance(text, str):
            raise GoalHistoryError()
        wrapped = text.startswith("USER TASK:\n") and "\n\nHOST CONTEXT (reference data, not additional authorization):\n" in text
        if result and not wrapped:
            # Older native replay also used role=user for automatic outcome
            # repair. Without the new host input journal, later bare records
            # cannot be authenticated as user amendments.
            raise GoalHistoryError()
        if wrapped:
            text = text[len("USER TASK:\n"):].split(
                "\n\nHOST CONTEXT (reference data, not additional authorization):\n", 1)[0]
        if "[History text omitted at the host replay limit.]" in text:
            raise GoalHistoryError()
        result.append(text)
    return checked_goal_history(result)
