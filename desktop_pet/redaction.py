"""Known-secret redaction for text and JSON values, with no I/O or SDK imports.

Tool arguments can be JSON strings inside another JSON message. Redact the
original value and its JSON-escaped spellings before another serialization
layer is added. The input text length bounds escape expansion; there is no
fixed nesting cutoff that could leave a deeper encoded credential intact.
"""
from __future__ import annotations

import json


def _secrets(values):
    if isinstance(values, str):
        values = (values,)
    return tuple(sorted({value for value in values if isinstance(value, str) and value},
                        key=len, reverse=True))


def _spellings(secret, maximum):
    """Canonical JSON encoders may preserve Unicode or escape it at each layer."""
    if len(secret) > maximum:
        return ()
    pending, seen = [secret], {secret}
    while pending:
        current = pending.pop()
        for ascii_only in (False, True):
            escaped = json.dumps(current, ensure_ascii=ascii_only)[1:-1]
            if len(escaped) <= maximum and escaped not in seen:
                seen.add(escaped)
                pending.append(escaped)
    return seen


def redact_text(text, secrets=(), replacement="[redacted]"):
    """Remove raw and repeatedly JSON-escaped known secrets, without decoding text."""
    if not isinstance(text, str) or not isinstance(replacement, str):
        raise TypeError("Redaction requires text and a textual replacement")
    spellings = set()
    for secret in _secrets(secrets):
        spellings.update(_spellings(secret, len(text)))
    # A shorter raw spelling may be a suffix inside an escaped spelling.
    # Longest-first replacement must consume the complete encoded value.
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, replacement)
    return text


def redact_value(value, secrets=(), replacement="[redacted]"):
    """Project JSON string leaves; preserve keys, containers and numeric types.

    A nested arguments JSON string stays a string. Encoded secret spellings
    are redacted inside it, while unrelated text and JSON structure remain.
    """
    known = _secrets(secrets)
    def clean(item):
        if isinstance(item, str):
            return redact_text(item, known, replacement)
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, tuple):
            return tuple(clean(child) for child in item)
        return item
    return clean(value)
