"""Provider-reported context metadata; no guessed model-name window table.

The application can cache a model entry obtained during an existing model-list
or connection workflow. Reading this cache never performs another API request.
These values describe a window; the provider's tokenizer remains authoritative.
"""
from __future__ import annotations

import hashlib


def model_context_cache_key(base_url: str, model: str) -> str:
    identity = base_url.rstrip("/") + "\0" + model.strip()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def model_context_tokens(metadata: object, model: str) -> int | None:
    """Admit explicit, unambiguous metadata only for the exact selected ID."""
    if not isinstance(metadata, dict) or metadata.get("id") != model.strip():
        return None
    names = ("context_length", "context_window", "context_window_tokens", "max_context_window_tokens")
    values = []
    sources = [metadata]
    for name in ("limits", "top_provider", "metadata"):
        if isinstance(metadata.get(name), dict):
            sources.append(metadata[name])
    for source in sources:
        for name in names:
            value = source.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values.append(value)
    # A disagreement is unknown, not authorization to choose a guessed number.
    return values[0] if values and len(set(values)) == 1 else None


def sanitized_model_context_metadata(metadata: object, model: str) -> dict | None:
    tokens = model_context_tokens(metadata, model)
    if tokens is None:
        return None
    return {"id": model.strip(), "context_window_tokens": tokens, "source": "provider_metadata"}


def context_window_rejected(exception) -> bool:
    """Recognize a provider window failure without exposing its remote text."""
    body = getattr(exception, "body", None)
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if isinstance(code, str) and code.casefold() in {
            "context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded",
            "input_token_limit_exceeded", "prompt_too_long", "too_many_input_tokens"}:
        return True
    message = error.get("message")
    if not isinstance(message, str):
        return False
    text = message[:8192].casefold()
    return any(phrase in text for phrase in (
        "maximum context length", "context length exceeded", "context window exceeded",
        "exceeds the context window", "too many input tokens", "input token limit",
        "prompt is too long", "prompt too long", "上下文长度超", "超出上下文窗口"))
