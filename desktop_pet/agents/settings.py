"""Non-secret Agent preferences; the GUI API key stays in the OS vault."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
import math
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import AgentValidationError
from ..bundle_runtime import worker_python_executable, frozen_workspace_root


GUI_CREDENTIAL_ACCOUNT = "agent-gui-api-key"
GUI_PROVIDERS = ("ui-tars-1.5", "ui-tars-1.0", "doubao-1.5-15b", "doubao-1.5-20b")
_SHARED_GUI_FIELDS = frozenset(("gui_provider", "gui_coordinate_profile", "gui_base_url", "gui_model"))
_PREFIX = "agents/"
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PYTHON = str(worker_python_executable())
AGENT_WORKSPACE_ROOT = _ROOT / "agent_workspace"


def agent_workspace_root():
    return frozen_workspace_root() or AGENT_WORKSPACE_ROOT


def _valid_text(value, *, maximum=2048, empty=True):
    return (isinstance(value, str) and len(value) <= maximum
            and (empty or bool(value.strip()))
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def validate_api(base_url, model, *, require_model=True):
    if (not _valid_text(base_url, empty=False)
            or any(char.isspace() for char in base_url)):
        raise AgentValidationError("Agent API 地址无效，请填写完整的 http(s) 地址。")
    try:
        location = urlsplit(base_url)
        valid = location.scheme in ("http", "https") and bool(location.hostname)
        location.port
    except (TypeError, ValueError):
        valid = False
    if (not valid or location.username is not None or location.password is not None
            or location.query or location.fragment or "?" in base_url or "#" in base_url):
        raise AgentValidationError("Agent API 地址不能包含账户、密码、查询参数或片段。")
    if not _valid_text(model, maximum=512, empty=not require_model):
        raise AgentValidationError("请填写有效的 Agent 模型名称。")


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = True
    tools_enabled: bool = True
    computer_enabled: bool = True
    backend: str = "deepseek-harness"
    python_executable: str = DEFAULT_PYTHON
    workspace: str = field(default_factory=lambda: str(agent_workspace_root()))
    timeout_seconds: float = 0.0  # Zero means no whole-task deadline; Stop still applies.
    max_steps: int = 0  # Zero means no iteration limit; timeout/Stop still apply.
    gui_provider: str = "ui-tars-1.5"
    gui_coordinate_profile: str = "native"
    gui_base_url: str = "https://api.openai.com/v1"
    gui_model: str = ""

    def validate(self) -> None:
        if any(type(value) is not bool for value in
               (self.enabled, self.tools_enabled, self.computer_enabled)):
            raise AgentValidationError("Agent 开关必须为布尔值。")
        if self.backend != "deepseek-harness":
            raise AgentValidationError("当前 Agent 使用 DeepSeek Harness。")
        for value in (self.python_executable, self.workspace):
            if not _valid_text(value) or (value and not Path(value).is_absolute()):
                raise AgentValidationError("Agent Python 和工作目录必须是绝对路径，或留空。")
        if self.workspace and not Path(self.workspace).resolve().is_relative_to(agent_workspace_root().resolve()):
            raise AgentValidationError("Agent 工作目录必须位于项目的 agent_workspace 文件夹内。")
        if self.python_executable:
            executable = Path(self.python_executable)
            name = executable.name.casefold()
            if (not name.startswith("python") or executable.suffix.casefold() not in ("", ".exe")):
                raise AgentValidationError("Agent 解释器必须指向独立环境中的 Python 可执行文件。")
        if (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (float, int))
                or not math.isfinite(self.timeout_seconds)
                or not (self.timeout_seconds == 0 or 5 <= self.timeout_seconds <= 900)):
            raise AgentValidationError("Agent 任务超时必须为 0（不设总时限），或 5～900 秒的兼容限制值。")
        if type(self.max_steps) is not int or not 0 <= self.max_steps <= 40:
            raise AgentValidationError("Agent 步数必须为 0（不设上限），或 1～40 的兼容限制值。")
        if not isinstance(self.gui_provider, str) or self.gui_provider not in GUI_PROVIDERS:
            raise AgentValidationError("请选择受支持的 GUI 模型提供方。")
        if self.gui_coordinate_profile not in ("native", "pixel"):
            raise AgentValidationError("请选择受支持的电脑操作模型类型。")
        if self.gui_coordinate_profile == "pixel" and self.gui_provider != "ui-tars-1.5":
            raise AgentValidationError("通用视觉模型请使用 UI-TARS 1.5 动作格式。")
        validate_api(self.gui_base_url, self.gui_model, require_model=False)


def shared_api_settings(settings: AgentSettings, api) -> AgentSettings:
    """Derive computer-operation routing from the one global API snapshot.

    No preferences or credentials are read or written here. Legacy GUI fields
    remain a compatible internal data shape, never a second runtime route.
    """
    if not isinstance(settings, AgentSettings):
        raise AgentValidationError("Agent 设置类型无效。")
    values = asdict(api) if is_dataclass(api) else api
    if not isinstance(values, dict):
        raise AgentValidationError("Agent 模型配置格式无效。")
    base_url, model = values.get("base_url"), values.get("model")
    validate_api(base_url, model, require_model=False)
    return replace(settings, gui_provider="ui-tars-1.5", gui_coordinate_profile="pixel",
                   gui_base_url=base_url, gui_model=model)


def load_agent_settings(store, *, api=None) -> AgentSettings:
    """Read a QSettings-compatible store, disabling malformed configuration."""
    defaults = AgentSettings()
    values = {}
    try:
        for field in fields(defaults):
            if api is not None and field.name in _SHARED_GUI_FIELDS:
                continue
            default = getattr(defaults, field.name)
            value = store.value(_PREFIX + field.name, default)
            if type(default) is bool:
                if isinstance(value, str) and value.casefold() in ("true", "false"):
                    value = value.casefold() == "true"
                elif type(value) is not bool:
                    raise ValueError
            elif type(default) is int and isinstance(value, str):
                value = int(value)
            elif type(default) is float and isinstance(value, str):
                value = float(value)
            values[field.name] = value
        # Retire the old backend selection without losing the user's switches.
        # This is a settings migration, never an execution fallback.
        if values.get("backend") == "nanobot":
            values["backend"] = "deepseek-harness"
            values["python_executable"] = DEFAULT_PYTHON
        result = AgentSettings(**values)
        if api is not None:
            result = shared_api_settings(result, api)
        result.validate()
        return result
    except Exception:
        # Do not surface raw store/driver values; they can contain credentials.
        return replace(defaults, enabled=False)


def save_agent_settings(store, settings: AgentSettings, *, known_secrets=(), api=None) -> None:
    """Write only validated public fields; never persist a key or provider body."""
    if not isinstance(settings, AgentSettings):
        raise AgentValidationError("Agent 设置类型无效。")
    settings.validate()
    values = asdict(settings)
    if any(isinstance(value, str) and any(secret and secret in value for secret in known_secrets)
           for value in values.values()):
        raise AgentValidationError("Agent 非密钥设置中包含 API Key，未保存。")
    if api is not None:
        shared_api_settings(settings, api).validate()
    try:
        for key, value in values.items():
            if api is not None and key in _SHARED_GUI_FIELDS:
                continue
            store.setValue(_PREFIX + key, value)
        if hasattr(store, "sync"):
            store.sync()
        if hasattr(store, "status"):
            status = store.status()
            if getattr(status, "value", status) != 0:
                raise RuntimeError
    except Exception:
        raise AgentValidationError("Agent 设置保存失败，请检查本地配置权限。") from None
