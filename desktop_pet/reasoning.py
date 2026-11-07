"""Declared reasoning controls for OpenAI-compatible Chat Completions.

UI and transports resolve the same capability. No inference call, network probe,
retry, credential access, or arbitrary request-body metadata is performed here.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import re
from urllib.parse import urlsplit

from .runtime_paths import resource_root


class ReasoningError(ValueError):
    """A fixed, non-secret configuration error safe for the settings UI."""


_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_LEGACY = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")
_ERROR_PROFILE = "思考参数协议无效，请重新选择。"
_ERROR_VALUE = "当前模型或接口不支持所选思考设置，请选择可用选项或显式自定义协议。"
_ERROR_BUDGET = "思考预算无效，请按当前接口的范围设置。"


@dataclass(frozen=True)
class ReasoningCapability:
    id: str
    label: str
    source: str
    control: str
    values: tuple[str, ...]
    value_labels: tuple[str, ...]
    allow_custom: bool = False
    budget_min: int = 0
    budget_max: int = 0
    budget_default: int = 0
    budget_required: bool = False
    notice: str = ""
    temperature_policy: str = "preserve"

    def label_for(self, value: str) -> str:
        return self.value_labels[self.values.index(value)] if value in self.values else value

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "source": self.source, "control": self.control,
                "values": list(self.values), "value_labels": list(self.value_labels), "allow_custom": self.allow_custom,
                "budget_min": self.budget_min, "budget_max": self.budget_max, "budget_default": self.budget_default,
                "budget_required": self.budget_required, "notice": self.notice,
                "temperature_policy": self.temperature_policy}


@lru_cache(maxsize=1)
def _catalog() -> dict:
    path = resource_root() / "shared/reasoning/profiles.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
                or not isinstance(value.get("profiles"), dict) or not isinstance(value.get("labels"), dict)
                or not {"auto", "legacy", "custom"} <= value["profiles"].keys()
                or not isinstance(value.get("auto_rules"), list)):
            raise ValueError
        for key, row in value["profiles"].items():
            if (not isinstance(key, str) or not _TOKEN.fullmatch(key) or not isinstance(row, dict)
                    or not isinstance(row.get("label"), str) or not row["label"] or len(row["label"]) > 128
                    or row.get("control") not in ("none", "effort", "custom", "toggle", "budget")
                    or row.get("wire") not in ("omit", "effort", "deepseek", "thinking", "enable_thinking", "qwen_budget", "openrouter", "claude_adaptive", "claude_budget")
                    or not isinstance(row.get("values"), list) or not 1 <= len(row["values"]) <= 16
                    or row["values"][0] != "default"
                    or any(not isinstance(v, str) or not _TOKEN.fullmatch(v) for v in row["values"])):
                raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ReasoningError("思考参数目录不可用，请更新或修复应用。") from None


def profile_choices() -> tuple[tuple[str, str], ...]:
    return tuple((key, value["label"]) for key, value in _catalog()["profiles"].items() if key != "legacy")


def profile_options() -> tuple[tuple[str, str], ...]:
    """All persisted profiles, including the explicitly labelled legacy choice."""
    return tuple((key, value["label"]) for key, value in _catalog()["profiles"].items())


def _value(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ReasoningError(_ERROR_VALUE)
    return value


def _url_identity(base_url: str):
    try:
        if not isinstance(base_url, str) or len(base_url) > 2048 or any(ord(c) < 32 for c in base_url):
            return None
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.casefold(), port, parsed.path.rstrip("/")
    except (ValueError, TypeError):
        return None


def reasoning_identity(base_url: str, model: str) -> tuple | None:
    """A cache identity, never a value intended for logs or UI text."""
    url = _url_identity(base_url)
    return (*url, model) if url is not None and isinstance(model, str) else None


def _capability(profile: str, source: str, values=None) -> ReasoningCapability:
    catalog = _catalog()
    row = catalog["profiles"].get(profile)
    if row is None:
        raise ReasoningError(_ERROR_PROFILE)
    chosen = tuple(values if values is not None else row["values"])
    return ReasoningCapability(profile, row["label"], source, row["control"], chosen,
        tuple(catalog["labels"].get(value, value) for value in chosen), bool(row.get("allow_custom", False)),
        row.get("budget_min", 0), row.get("budget_max", 0), row.get("budget_default", 0),
        bool(row.get("budget_required", False)), row.get("notice", ""), row.get("temperature_policy", "preserve"))


def _metadata_capability(base_url: str, model: str, metadata) -> ReasoningCapability | None:
    """Only a bound, canonical capability document can influence known fields."""
    if not isinstance(metadata, dict) or set(metadata) != {"version", "base_url", "model", "profile", "values"}:
        return None
    if type(metadata["version"]) is not int or metadata["version"] != 1:
        return None
    identity = reasoning_identity(base_url, model)
    if identity is None or identity != reasoning_identity(metadata["base_url"], metadata["model"]):
        return None
    profile, values = metadata["profile"], metadata["values"]
    if (not isinstance(profile, str) or profile not in _catalog()["profiles"] or profile in ("auto", "legacy", "custom")
            or not isinstance(values, list) or not 1 <= len(values) <= 16
            or any(not isinstance(v, str) or not _TOKEN.fullmatch(v) for v in values)):
        return None
    values = tuple(dict.fromkeys(("default", *values)))
    # Toggle/budget profiles have semantic values, not extensible effort enums.
    row = _catalog()["profiles"][profile]
    if row["control"] in ("none", "toggle", "budget") and any(v not in row["values"] for v in values):
        return None
    return replace(_capability(profile, "metadata", values), allow_custom=False)


def resolve_reasoning(base_url: str, model: str, profile: str = "auto", metadata=None) -> ReasoningCapability:
    if not isinstance(profile, str) or profile not in _catalog()["profiles"]:
        raise ReasoningError(_ERROR_PROFILE)
    if profile != "auto":
        return _capability(profile, "explicit")
    declared = _metadata_capability(base_url, model, metadata)
    if declared is not None:
        inferred = resolve_reasoning(base_url, model, "auto")
        if declared.id == inferred.id and inferred.temperature_policy == "omit":
            declared = replace(declared, temperature_policy="omit")
        return declared
    identity = _url_identity(base_url)
    name = model.strip().casefold() if isinstance(model, str) else ""
    for rule in _catalog()["auto_rules"]:
        match = rule["match"]
        if "hosts" in match and (identity is None or identity[1] not in match["hosts"]):
            continue
        if "paths" in match and (identity is None or not any(identity[3] == p or identity[3].startswith(p + "/") for p in match["paths"])):
            continue
        date_match = match.get("allow_date_suffix") is True and any(
            re.fullmatch(re.escape(base) + r"-\d{4}-\d{2}-\d{2}", name) for base in match.get("models", ()))
        if ("models" in match or "model_prefixes" in match) and not (
                name in match.get("models", ()) or date_match or any(name.startswith(p) for p in match.get("model_prefixes", ()))):
            continue
        cap = _capability(rule["profile"], rule["source"], rule.get("values"))
        return replace(cap, allow_custom=False, temperature_policy=rule.get("temperature_policy", cap.temperature_policy))
    return replace(_capability("auto", "unknown"), notice="尚无此模型和接口的能力声明，默认不发送思考参数；可显式选择兼容协议。")


def validate_reasoning(profile: str, value: str = "default", budget_tokens: int = 0, metadata=None) -> None:
    """Validate stored shape without inferring or changing a model's protocol."""
    if not isinstance(profile, str) or profile not in _catalog()["profiles"]:
        raise ReasoningError(_ERROR_PROFILE)
    _value(value)
    if type(budget_tokens) is not int or not 0 <= budget_tokens <= 262144:
        raise ReasoningError(_ERROR_BUDGET)
    if profile == "auto":
        return  # Runtime validation is bound to URL + model + declared metadata.
    cap = _capability(profile, "explicit")
    if value not in cap.values and not cap.allow_custom:
        raise ReasoningError(_ERROR_VALUE)
    if cap.control == "budget" and budget_tokens and not cap.budget_min <= budget_tokens <= cap.budget_max:
        raise ReasoningError(_ERROR_BUDGET)
    if cap.control != "budget" and budget_tokens:
        raise ReasoningError(_ERROR_BUDGET)


def default_reasoning_selection(base_url: str, model: str, profile: str = "auto", metadata=None) -> dict:
    """Choose the strongest understood option only for an unset preference.

    Capability value lists need not be sorted. Unknown vendor tokens cannot be
    ordered from their spelling, and an unknown model keeps its omitted fields.
    Budget protocols enable their declared default; a token budget is not an
    effort tier and must also fit the eventual output/context allowance.
    """
    cap = resolve_reasoning(base_url, model, profile, metadata)
    value, budget = "default", 0
    if cap.control == "effort":
        order = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
        value = next((item for item in reversed(order) if item in cap.values), "default")
    elif cap.control in ("toggle", "budget") and "enabled" in cap.values:
        value = "enabled"
        budget = cap.budget_default if cap.control == "budget" else 0
    return {"profile": profile, "value": value, "budget_tokens": budget}


def validate_selection(base_url: str, model: str, value: str = "default", profile: str = "auto",
                       budget_tokens: int = 0, metadata=None) -> ReasoningCapability:
    validate_reasoning(profile, value, budget_tokens, metadata)
    cap = resolve_reasoning(base_url, model, profile, metadata)
    if value not in cap.values and not cap.allow_custom:
        raise ReasoningError(_ERROR_VALUE)
    if cap.control == "budget":
        if budget_tokens and not cap.budget_min <= budget_tokens <= cap.budget_max:
            raise ReasoningError(_ERROR_BUDGET)
    elif budget_tokens:
        raise ReasoningError(_ERROR_BUDGET)
    return cap


def reasoning_body(base_url: str, model: str, value: str = "default", profile: str = "auto",
                   budget_tokens: int = 0, metadata=None) -> dict:
    cap = validate_selection(base_url, model, value, profile, budget_tokens, metadata)
    if value == "default":
        return {}
    wire = _catalog()["profiles"][cap.id]["wire"]
    if wire == "effort":
        return {"reasoning_effort": value}
    if wire == "deepseek":
        return {"thinking": {"type": "disabled"}} if value == "none" else {
            "thinking": {"type": "enabled"}, "reasoning_effort": value}
    if wire == "thinking":
        return {"thinking": {"type": "disabled" if value == "none" else "enabled"}}
    if wire == "enable_thinking":
        return {"enable_thinking": value != "none"}
    if wire == "qwen_budget":
        return {"enable_thinking": False} if value == "none" else {
            "enable_thinking": True, "thinking_budget": budget_tokens or cap.budget_default}
    if wire == "openrouter":
        return {"reasoning": {"enabled": False}} if value == "none" else {"reasoning": {"effort": value}}
    if wire == "claude_adaptive":
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": value}}
    if wire == "claude_budget":
        return {"thinking": {"type": "disabled"}} if value == "none" else {
            "thinking": {"type": "enabled", "budget_tokens": budget_tokens or cap.budget_default}}
    raise ReasoningError(_ERROR_VALUE)
