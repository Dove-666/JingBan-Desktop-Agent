"""Compare archive timestamps by instant while preserving their original text."""

from datetime import datetime, timezone
import sqlite3


def timestamp_key(value: object) -> str:
    """UTC with microsecond precision; unknown legacy dates sort last.

    Visual observations can carry a local offset while ordinary turns use UTC.
    Plain ISO string ordering is therefore unsafe, including across midnight.
    A stable rowid/input order breaks ties, rather than timestamp formatting.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
        except (ValueError, OverflowError):
            pass
    return "~"


def register_timestamp_order(connection: sqlite3.Connection) -> None:
    connection.create_function("aura_timestamp", 1, timestamp_key, deterministic=True)
