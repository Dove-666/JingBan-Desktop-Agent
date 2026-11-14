"""Host-owned conversation identities, independent of an Agent implementation."""
from uuid import UUID


def conversation_uuid(value):
    if not isinstance(value, str):
        raise ValueError("invalid conversation id")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid conversation id") from None
    if str(parsed) != value or parsed.int == 0:
        raise ValueError("invalid conversation id")
    return value
