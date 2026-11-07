"""Normalize only explicit model capability fields; discard other model data."""
from .reasoning import resolve_reasoning
from urllib.parse import urlsplit


class ModelChoices(list):
    def __init__(self, values=(), *, reasoning_metadata=None):
        super().__init__(values)
        self.reasoning_metadata = dict(reasoning_metadata or {})


def normalized_reasoning_metadata(raw, base_url, model):
    if not isinstance(raw, dict) or raw.get("id") != model:
        return None
    declaration = raw.get("reasoning")
    profile, values = None, None
    if isinstance(declaration, dict):
        profile, values = declaration.get("profile"), declaration.get("values")
        if declaration.get("parameter") == "reasoning_effort":
            profile = "openai"
    if values is None and isinstance(raw.get("supported_reasoning_efforts"), list):
        profile, values = "openai", raw["supported_reasoning_efforts"]
    if profile is None and isinstance(raw.get("supported_parameters"), list):
        try:
            if urlsplit(base_url).hostname == "openrouter.ai" and "reasoning" in raw["supported_parameters"]:
                capability = resolve_reasoning(base_url, model, profile="openrouter")
                profile, values = capability.id, list(capability.values)
        except ValueError:
            pass
    if not isinstance(profile, str) or not isinstance(values, list):
        return None
    metadata = {"version": 1, "base_url": base_url, "model": model, "profile": profile, "values": values}
    try:
        if resolve_reasoning(base_url, model, metadata=metadata).source == "metadata":
            return metadata
    except (TypeError, ValueError):
        pass
    return None
