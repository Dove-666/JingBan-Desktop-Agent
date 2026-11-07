"""Local preferences and an explicitly selected Windows credential vault.

Only non-secret preferences enter the INI file.  Backend exception details are
deliberately discarded: a provider or credential backend may echo a secret.
Tests may inject a small in-memory backend without touching the user's vault.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QSettings, QStandardPaths


class SettingsError(ValueError):
    """A non-secret preference is invalid or could not be saved."""


class CredentialsError(RuntimeError):
    """Credential storage failed; its message never contains a backend error."""


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


REASONING_EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ApiSettings:
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    timeout_seconds: float = 30.0
    reasoning_effort: str = "default"
    reasoning_profile: str = "auto"
    reasoning_budget_tokens: int = 0
    reasoning_metadata: dict | None = None

    def validate(self, require_model: bool = True) -> None:
        if not isinstance(self.base_url, str) or not self.base_url:
            raise SettingsError("请填写 API Base URL。")
        if _has_control_characters(self.base_url) or any(
            character.isspace() for character in self.base_url
        ):
            raise SettingsError("API Base URL 不能含空白或控制字符。")
        try:
            parts = urlsplit(self.base_url)
            valid_location = parts.scheme in ("http", "https") and bool(parts.hostname)
            # Accessing .port validates malformed or out-of-range port numbers.
            parts.port
        except (TypeError, ValueError):
            raise SettingsError("API Base URL 格式无效，请使用完整的 http(s) 地址。") from None
        if not valid_location:
            raise SettingsError("API Base URL 必须是完整的 http(s) 地址。")
        if parts.username is not None or parts.password is not None:
            raise SettingsError("API Base URL 不能包含用户名或密码，请单独填写 API Key。")
        if "?" in self.base_url or "#" in self.base_url:
            raise SettingsError("API Base URL 不能包含查询参数或片段，密钥仅能填写在 API Key 中。")
        if not isinstance(self.model, str) or _has_control_characters(self.model):
            raise SettingsError("模型名称不能包含控制字符。")
        if require_model and not self.model.strip():
            raise SettingsError("请填写 Model Name。")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise SettingsError("请求超时必须为 1～120 秒。")
        from .reasoning import ReasoningError, resolve_reasoning, validate_selection
        try:
            if self.reasoning_metadata is not None and resolve_reasoning(
                    self.base_url, self.model, metadata=self.reasoning_metadata).source != "metadata":
                raise ReasoningError("invalid capability")
            validate_selection(self.base_url, self.model, value=self.reasoning_effort,
                profile=self.reasoning_profile, budget_tokens=self.reasoning_budget_tokens,
                metadata=self.reasoning_metadata)
        except (ReasoningError, TypeError, ValueError):
            raise SettingsError("当前模型的思考配置无效，请在模型设置中重新选择适配方式、模式或预算。") from None


def read_api_settings(settings: QSettings) -> ApiSettings:
    """Read ordinary API preferences without constructing a credential store."""
    defaults = ApiSettings()
    try:
        effort = settings.value("api/reasoning_effort", defaults.reasoning_effort)
        profile = settings.value("api/reasoning_profile", "auto")
        budget = settings.value("api/reasoning_budget_tokens", 0)
        try:
            budget = int(budget) if not isinstance(budget, bool) else -1
        except (TypeError, ValueError, OverflowError):
            budget = -1
        if not settings.contains("api/reasoning_profile"):
            effort = effort if isinstance(effort, str) and effort in REASONING_EFFORTS else "default"
            profile = "legacy" if effort != "default" else "auto"
        result = ApiSettings(
            base_url=settings.value("api/base_url", defaults.base_url),
            model=settings.value("api/model", defaults.model),
            timeout_seconds=float(settings.value("api/timeout_seconds", defaults.timeout_seconds)),
            reasoning_effort=effort,
            reasoning_profile=profile,
            reasoning_budget_tokens=budget,
        )
        from .model_context import model_context_cache_key
        try:
            cached = settings.value("model_reasoning_metadata/" + model_context_cache_key(result.base_url, result.model), "", type=str)
            metadata = json.loads(cached) if cached else None
            if isinstance(metadata, dict):
                result = replace(result, reasoning_metadata=metadata)
        except (TypeError, ValueError, RecursionError, AttributeError):
            pass
        if not settings.contains("api/reasoning_effort") and not settings.contains("api/reasoning_profile"):
            from .reasoning import default_reasoning_selection
            selection = default_reasoning_selection(result.base_url, result.model, metadata=result.reasoning_metadata)
            result = replace(result, reasoning_effort=selection["value"],
                             reasoning_budget_tokens=selection["budget_tokens"])
        # A broken reasoning preference must not discard a valid provider URL.
        try:
            result.validate(require_model=False)
        except SettingsError:
            result = replace(result, reasoning_effort="default", reasoning_profile="auto",
                             reasoning_budget_tokens=0, reasoning_metadata=None)
            result.validate(require_model=False)
        return result
    except (SettingsError, ValueError, TypeError, OverflowError):
        # A damaged ordinary config must not prevent the pet from starting.
        return defaults


class CredentialStore:
    """Store this application's single API key in Windows Credential Manager.

    Do not use keyring.get_keyring(): user configuration could select an
    insecure plaintext backend.  ``backend`` is solely an injection seam for
    tests or a deliberately provided compatible vault implementation.
    """

    def __init__(
        self,
        service: str = "AURA Desktop Agent",
        account: str = "chat-api-key",
        backend: Any = None,
    ) -> None:
        self.service = service
        self.account = account
        self._backend = backend
        self._status = "Windows 凭据管理器可用，API Key 仅保存在系统凭据中。"
        if backend is not None:
            self._status = "已提供凭据存储后端，API Key 不写入普通设置。"
            return
        if sys.platform != "win32":
            self._status = "当前系统不支持 Windows 凭据管理器；无法保存 API Key，未使用明文回退。"
            return
        try:
            from keyring.backends.Windows import WinVaultKeyring

            vault = WinVaultKeyring()
            if vault.priority <= 0:
                raise RuntimeError("unavailable")
            self._backend = vault
        except Exception:
            self._status = "Windows 凭据管理器不可用，请确认已安装 keyring 且当前账户可访问凭据；未使用明文回退。"

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def status(self) -> str:
        return self._status

    def _require_backend(self) -> Any:
        if self._backend is None:
            raise CredentialsError(self._status)
        return self._backend

    def get_api_key(self) -> str:
        backend = self._require_backend()
        try:
            value = backend.get_password(self.service, self.account)
            if value is not None and not isinstance(value, str):
                raise TypeError("invalid credential value")
            return value or ""
        except Exception:
            self._status = "无法读取系统凭据中的 API Key，请检查当前 Windows 账户的凭据访问权限。"
            raise CredentialsError(self._status) from None

    def save_api_key(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise CredentialsError("API Key 不能为空；删除密钥请使用“清除 API Key”。")
        if _has_control_characters(value):
            raise CredentialsError("API Key 不能包含换行或控制字符。")
        backend = self._require_backend()
        secret = value.strip()
        try:
            backend.set_password(self.service, self.account, secret)
            if backend.get_password(self.service, self.account) != secret:
                raise RuntimeError("credential verification failed")
        except Exception:
            self._status = "API Key 未能可靠保存到 Windows 凭据管理器，请检查权限；未写入明文配置。"
            raise CredentialsError(self._status) from None
        self._status = "API Key 已保存到系统凭据管理器。"

    def clear_api_key(self) -> None:
        backend = self._require_backend()
        try:
            if backend.get_password(self.service, self.account) is not None:
                backend.delete_password(self.service, self.account)
            if backend.get_password(self.service, self.account) is not None:
                raise RuntimeError("credential deletion verification failed")
        except Exception:
            self._status = "无法确认系统凭据中的 API Key 已删除，请检查 Windows 凭据管理器后重试。"
            raise CredentialsError(self._status) from None
        self._status = "API Key 已从系统凭据管理器删除。"


class SettingsManager:
    """Persist ordinary preferences separately from the API credential."""

    def __init__(
        self,
        settings: QSettings | None = None,
        credential_store: CredentialStore | None = None,
        *,
        profile_dir: Path | None = None,
    ) -> None:
        if settings is not None and profile_dir is not None:
            raise SettingsError("独立配置目录不能与现有设置实例同时指定。")
        self.profile_dir = None
        if profile_dir is not None:
            try:
                self.profile_dir = Path(profile_dir).expanduser().resolve()
            except (OSError, ValueError, TypeError):
                raise SettingsError("无法使用指定的独立配置目录，请检查路径是否有效。") from None
        if settings is None:
            directory = (str(self.profile_dir) if self.profile_dir is not None else
                         QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
            if not directory:
                raise SettingsError("无法确定当前用户的本地配置目录。")
            try:
                path = Path(directory) / "settings.ini"
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise SettingsError("无法创建当前用户的本地配置目录，请检查目录权限。") from None
            settings = QSettings(str(path), QSettings.Format.IniFormat)
        self._settings = settings
        if credential_store is not None:
            self._credentials = credential_store
        elif self.profile_dir is not None:
            scope = hashlib.sha256(os.path.normcase(str(self.profile_dir)).encode("utf-8")).hexdigest()[:24]
            self._credentials = CredentialStore(service="AURA Desktop Agent · profile-" + scope)
        else:
            self._credentials = CredentialStore()

    @property
    def credentials_available(self) -> bool:
        return self._credentials.available

    @property
    def credential_status(self) -> str:
        return self._credentials.status

    @property
    def config_path(self) -> str:
        return self._settings.fileName()

    def load_api_settings(self) -> ApiSettings:
        return read_api_settings(self._settings)

    def has_unmigrated_reasoning(self):
        return (not self._settings.contains("api/reasoning_profile") and
                self._settings.value("api/reasoning_effort", "default") in REASONING_EFFORTS[1:])

    def load_character_card(self):
        from .character_cards import CharacterCard, CharacterCardError, MAX_CARD_JSON_BYTES
        raw = self._settings.value("character/card", "")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            if len(raw.encode("utf-8")) > MAX_CARD_JSON_BYTES:
                return None
            return CharacterCard.from_dict(json.loads(raw))
        except (CharacterCardError, ValueError, TypeError, RecursionError):
            return None

    def save_character_card(self, card) -> None:
        from .character_cards import CharacterCard, CharacterCardError
        try:
            if card is not None and not isinstance(card, CharacterCard):
                raise CharacterCardError("角色卡格式无效。")
            document = CharacterCard.from_dict(card.to_dict()).to_dict() if card is not None else None
        except (CharacterCardError, ValueError, TypeError):
            raise SettingsError("角色卡内容无效，请重新导入。") from None
        key = "character/card"
        present, previous = self._settings.contains(key), self._settings.value(key)
        if document is None:
            self._settings.remove(key)
        else:
            self._settings.setValue(key, json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        try:
            self._sync()
        except SettingsError:
            if present:
                self._settings.setValue(key, previous)
            else:
                self._settings.remove(key)
            try:
                self._settings.sync()
                restored = self._settings.status() == QSettings.Status.NoError
            except Exception:
                restored = False
            if not restored:
                raise SettingsError("无法确认角色设置已恢复，请检查配置目录权限后重新打开设置核对。") from None
            raise

    def _sync(self) -> None:
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise SettingsError("设置未能写入本地配置文件，请检查当前用户的目录权限。")

    def _library_operation(self, operation, *args, **kwargs):
        from .character_library import CharacterLibrary, CharacterLibraryError
        try:
            config_path = Path(self.config_path)
            if not config_path.is_absolute():
                raise CharacterLibraryError("无法确定角色库的本地配置目录，请重新打开设置。")
            library = CharacterLibrary(config_path.parent / "character_cards.sqlite3")
            return getattr(library, operation)(*args, **kwargs)
        except CharacterLibraryError as exc:
            raise SettingsError(str(exc)) from None
        except (OSError, TypeError, ValueError):
            raise SettingsError("无法确定角色库的本地配置目录，请重新打开设置。") from None

    def list_character_cards(self):
        return self._library_operation("list")

    def save_library_character_card(self, card, label=None):
        return self._library_operation("save", card, label=label)

    def load_library_character_card(self, identity):
        return self._library_operation("load", identity)

    def rename_library_character_card(self, identity, label):
        return self._library_operation("rename", identity, label)

    def delete_library_character_card(self, identity):
        return self._library_operation("delete", identity)

    def save_api_settings(self, settings: ApiSettings, *, pending_api_key: str = "") -> None:
        settings.validate(require_model=False)
        # Pasting the key into Model Name or a URL path is an easy UI mistake.
        # Those fields are ordinary INI preferences, so reject a known secret
        # before writing anything. A missing vault must still allow non-secret
        # preferences to be saved; the caller can supply an unsaved input key.
        known_keys = (*self.get_known_api_keys(), pending_api_key.strip())
        fields = tuple(self._public_strings(settings))
        if any(key and any(key in field for field in fields) for key in known_keys):
            raise SettingsError("Base URL 和模型名称不能包含 API Key，请只在 API Key 输入框中填写密钥。")
        self._settings.setValue("api/base_url", settings.base_url)
        self._settings.setValue("api/model", settings.model.strip())
        self._settings.setValue("api/timeout_seconds", float(settings.timeout_seconds))
        self._settings.setValue("api/reasoning_effort", settings.reasoning_effort)
        self._settings.setValue("api/reasoning_profile", settings.reasoning_profile)
        self._settings.setValue("api/reasoning_budget_tokens", settings.reasoning_budget_tokens)
        from .model_context import model_context_cache_key
        identity = model_context_cache_key(settings.base_url, settings.model)
        self._settings.setValue("model_reasoning_preferences/" + identity, json.dumps({
            "profile": settings.reasoning_profile, "value": settings.reasoning_effort,
            "budget_tokens": settings.reasoning_budget_tokens}, ensure_ascii=False))
        if settings.reasoning_metadata is not None:
            self._settings.setValue("model_reasoning_metadata/" + identity, json.dumps(settings.reasoning_metadata, ensure_ascii=False))
        self._sync()

    def load_reasoning_preferences(self, base_url, model):
        """Read the explicitly saved choice for this provider/model only."""
        from .model_context import model_context_cache_key
        from .reasoning import validate_selection
        try:
            raw = self._settings.value("model_reasoning_preferences/" + model_context_cache_key(base_url, model), "", type=str)
            value = json.loads(raw) if raw else None
            if not isinstance(value, dict) or set(value) != {"profile", "value", "budget_tokens"}:
                return None
            validate_selection(base_url, model, value=value["value"], profile=value["profile"],
                budget_tokens=value["budget_tokens"], metadata=self.load_reasoning_metadata(base_url, model))
            return value
        except (ValueError, TypeError, RecursionError):
            return None

    def has_reasoning_preferences(self, base_url, model):
        """Distinguish an explicit model choice, including Default, from unset."""
        if self.load_reasoning_preferences(base_url, model) is not None:
            return True
        from .reasoning import reasoning_identity
        active = reasoning_identity(self._settings.value("api/base_url", ApiSettings().base_url),
                                    self._settings.value("api/model", ""))
        return (active == reasoning_identity(base_url, model) and
                (self._settings.contains("api/reasoning_effort") or self._settings.contains("api/reasoning_profile")))

    def load_reasoning_metadata(self, base_url, model):
        from .model_context import model_context_cache_key
        from .reasoning import resolve_reasoning
        try:
            raw = self._settings.value("model_reasoning_metadata/" + model_context_cache_key(base_url, model), "", type=str)
            value = json.loads(raw) if raw else None
            if not isinstance(value, dict) or resolve_reasoning(base_url, model, metadata=value).source != "metadata":
                return None
            return value
        except (ValueError, TypeError, RecursionError):
            return None

    def save_reasoning_metadata(self, metadata, *, base_url, model):
        from .model_context import model_context_cache_key
        from .reasoning import resolve_reasoning
        if not isinstance(metadata, dict) or resolve_reasoning(base_url, model, metadata=metadata).source != "metadata":
            raise SettingsError("模型未提供可确认的思考能力。")
        self._reject_public_api_keys(metadata)
        self._settings.setValue("model_reasoning_metadata/" + model_context_cache_key(base_url, model),
                                json.dumps(metadata, ensure_ascii=False))
        self._sync()

    def load_theme(self) -> str:
        value = self._settings.value("ui/theme", "light")
        return value if value in ("dark", "light") else "light"

    def save_theme(self, value: str) -> None:
        if not isinstance(value, str) or value not in ("dark", "light"):
            raise SettingsError("请选择深色或浅色外观。")
        key = "ui/theme"
        present, previous = self._settings.contains(key), self._settings.value(key)
        self._settings.setValue(key, value)
        try:
            self._sync()
        except SettingsError:
            if present:
                self._settings.setValue(key, previous)
            else:
                self._settings.remove(key)
            raise

    def load_silent_start(self) -> bool:
        value = self._settings.value("startup/silent", False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        return False

    def save_silent_start(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise SettingsError("静默启动开关必须为布尔值。")
        key = "startup/silent"
        present = self._settings.contains(key)
        previous = self._settings.value(key)
        self._settings.setValue(key, value)
        try:
            self._sync()
        except SettingsError:
            if present:
                self._settings.setValue(key, previous)
            else:
                self._settings.remove(key)
            raise

    def load_scale(self) -> int:
        try:
            raw = self._settings.value("pet/scale_percent", 100)
            if isinstance(raw, bool):
                return 100
            value = int(raw)
            return value if 50 <= value <= 200 and float(raw) == value else 100
        except (TypeError, ValueError, OverflowError):
            return 100

    def save_scale(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 50 <= value <= 200:
            raise SettingsError("桌宠大小必须为 50～200 之间的整数百分比。")
        self._settings.setValue("pet/scale_percent", value)
        self._sync()

    def load_reading_font(self) -> int:
        try:
            raw = self._settings.value("pet/reading_font_points", 14)
            value = int(raw)
            return value if not isinstance(raw, bool) and float(raw) == value and 12 <= value <= 22 else 14
        except (ValueError, TypeError, OverflowError):
            return 14

    def save_reading_font(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 12 <= value <= 22:
            raise SettingsError("气泡字号必须为 12～22 之间的整数。")
        self._settings.setValue("pet/reading_font_points", value)
        self._sync()

    def load_chat_font_size(self) -> int:
        try:
            raw = self._settings.value("chat/reading_font_size", 16)
            value = int(raw)
            return value if not isinstance(raw, bool) and float(raw) == value and 12 <= value <= 22 else 16
        except (ValueError, TypeError, OverflowError):
            return 16

    def save_chat_font_size(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 12 <= value <= 22:
            raise SettingsError("聊天字号必须为 12～22 之间的整数。")
        self._settings.setValue("chat/reading_font_size", value)
        self._sync()

    def load_auto_expression(self) -> bool:
        raw = self._settings.value("pet/auto_expression", True)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return True

    def save_auto_expression(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise SettingsError("自动表情开关必须为布尔值。")
        self._settings.setValue("pet/auto_expression", value)
        self._sync()

    def get_api_key(self) -> str:
        return self._credentials.get_api_key()

    def get_known_api_keys(self) -> tuple[str, ...]:
        """Return readable chat/GUI keys for redaction, never backend errors.

        This only reads the configured vault. It does not read plain-text
        settings, initialize a model/Agent, write credentials, or start work.
        """
        keys = set()
        if not self.credentials_available:
            return ()
        for getter in (self.get_api_key, self.get_agent_gui_api_key):
            try:
                value = getter()
                if isinstance(value, str) and value:
                    keys.add(value)
            except Exception:
                # A vault backend may include a secret in its exception text.
                # A different readable credential can still be redacted.
                pass
        return tuple(sorted(keys, key=lambda value: (-len(value), value)))

    @staticmethod
    def _public_strings(value):
        if is_dataclass(value):
            value = asdict(value)
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from SettingsManager._public_strings(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                yield from SettingsManager._public_strings(item)

    def _reject_public_api_keys(self, value) -> None:
        fields = tuple(self._public_strings(value))
        if any(secret in field for secret in self.get_known_api_keys() for field in fields):
            raise SettingsError("普通配置不能包含 API Key，请只在密钥输入框中填写密钥。")

    def _check_new_key_not_in_preferences(self, value) -> None:
        if isinstance(value, str) and value.strip():
            secret = value.strip()
            for name in self._settings.allKeys():
                if any(secret in field for field in self._public_strings(self._settings.value(name))):
                    raise CredentialsError("API Key 已出现在普通配置字段中，请先修正普通设置，再保存密钥。")

    def save_api_key(self, value: str) -> None:
        # Also protect callers which save the credential separately after
        # having accidentally entered that same key in an ordinary field.
        self._check_new_key_not_in_preferences(value)
        self._credentials.save_api_key(value)

    def clear_api_key(self) -> None:
        self._credentials.clear_api_key()

    def load_conversation_agent_mode(self, conversation_id):
        from uuid import UUID
        try:
            if str(UUID(conversation_id)) != conversation_id:
                return False
        except (TypeError, ValueError, AttributeError):
            return False
        value = self._settings.value("chat/agent_mode/" + conversation_id, False)
        return value is True or isinstance(value, str) and value.casefold() == "true"

    def save_conversation_agent_mode(self, conversation_id, enabled):
        from uuid import UUID
        if type(enabled) is not bool or str(UUID(conversation_id)) != conversation_id:
            raise SettingsError("Agent 模式参数无效。")
        self._settings.setValue("chat/agent_mode/" + conversation_id, enabled)
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise SettingsError("Agent 模式未能保存。")

    def load_agent_settings(self):
        from .agents.settings import load_agent_settings
        return load_agent_settings(self._settings, api=self.load_api_settings())

    def save_agent_settings(self, settings, *, pending_api_key="") -> None:
        from .agents.settings import save_agent_settings
        from .agents.contracts import AgentValidationError
        known = (*self.get_known_api_keys(), pending_api_key.strip())
        try:
            save_agent_settings(self._settings, settings, known_secrets=known, api=self.load_api_settings())
        except AgentValidationError as exc:
            raise SettingsError(str(exc)) from None

    def _agent_gui_credentials(self):
        from .agents.settings import GUI_CREDENTIAL_ACCOUNT
        return CredentialStore(service=self._credentials.service, account=GUI_CREDENTIAL_ACCOUNT,
                               backend=self._credentials._backend)

    @property
    def agent_gui_credential_status(self):
        return self._agent_gui_credentials().status

    def get_agent_gui_api_key(self):
        return self._agent_gui_credentials().get_api_key()

    def save_agent_gui_api_key(self, value):
        self._check_new_key_not_in_preferences(value)
        self._agent_gui_credentials().save_api_key(value)

    def clear_agent_gui_api_key(self):
        self._agent_gui_credentials().clear_api_key()

    def load_perception_settings(self):
        from dataclasses import fields, replace
        from .perception.settings import PerceptionSettings

        defaults = PerceptionSettings()
        values = {}
        try:
            for field in fields(defaults):
                default = getattr(defaults, field.name)
                if isinstance(default, tuple):
                    raw = self._settings.value("perception/" + field.name, list(default))
                    values[field.name] = tuple(raw) if isinstance(raw, (list, tuple)) else (raw,)
                elif isinstance(default, bool):
                    raw = self._settings.value("perception/" + field.name, default)
                    if isinstance(raw, bool):
                        values[field.name] = raw
                    elif isinstance(raw, str) and raw.lower() in ("true", "false"):
                        values[field.name] = raw.lower() == "true"
                    else:
                        raise ValueError("Invalid consent preference")
                else:
                    values[field.name] = self._settings.value("perception/" + field.name, default, type=type(default))
            result = PerceptionSettings(**values)
            result.validate()
            return result
        except (ValueError, TypeError, OverflowError):
            # New-profile defaults are distinct from recovery of damaged data.
            return replace(defaults, enabled=False, allow_remote=False)

    def save_perception_settings(self, settings) -> None:
        from dataclasses import fields
        settings.validate()
        self._reject_public_api_keys(settings)
        for field in fields(settings):
            value = getattr(settings, field.name)
            self._settings.setValue("perception/" + field.name, list(value) if isinstance(value, tuple) else value)
        self._sync()

    def load_camera_settings(self):
        from dataclasses import fields
        from .perception.settings import CameraSettings

        defaults = CameraSettings()
        values = {}
        try:
            for field in fields(defaults):
                default = getattr(defaults, field.name)
                raw = self._settings.value("camera/" + field.name, list(default) if isinstance(default, tuple) else default)
                if isinstance(default, tuple):
                    values[field.name] = tuple(raw) if isinstance(raw, (list, tuple)) else (raw,)
                elif isinstance(default, bool):
                    if isinstance(raw, bool):
                        values[field.name] = raw
                    elif isinstance(raw, str) and raw.lower() in ("true", "false"):
                        values[field.name] = raw.lower() == "true"
                    else:
                        raise ValueError("Invalid camera consent preference")
                else:
                    values[field.name] = self._settings.value("camera/" + field.name, default, type=type(default))
            result = CameraSettings(**values)
            result.validate()
            return result
        except (ValueError, TypeError, OverflowError):
            return defaults

    def save_camera_settings(self, settings) -> None:
        from dataclasses import fields
        settings.validate()
        self._reject_public_api_keys(settings)
        for field in fields(settings):
            value = getattr(settings, field.name)
            self._settings.setValue("camera/" + field.name, list(value) if isinstance(value, tuple) else value)
        self._sync()

    def load_context_budget(self):
        from dataclasses import fields
        from .conversations import ContextBudget
        defaults = ContextBudget(mode="model")
        try:
            # Old preferences had no mode and silently clipped to 8192/16.
            # Loading them now selects model policy without rewriting any
            # unrelated setting or contacting the provider.
            values = {field.name: self._settings.value("context/" + field.name, getattr(defaults, field.name), type=int)
                      for field in fields(defaults) if field.name not in {"mode", "model_context_tokens"}}
            values["mode"] = self._settings.value("context/mode", "model", type=str)
            if values["mode"] == "model":
                from .model_context import model_context_tokens
                api = self.load_api_settings()
                values["model_context_tokens"] = model_context_tokens(self.load_model_context_metadata(api), api.model)
            result = ContextBudget(**values)
            result.validate()
            return result
        except (TypeError, ValueError, OverflowError):
            return defaults

    def save_context_budget(self, budget):
        from dataclasses import fields
        budget.validate()
        for field in fields(budget):
            if field.name == "model_context_tokens":
                continue  # A window is associated with endpoint + exact model.
            self._settings.setValue("context/" + field.name, getattr(budget, field.name))
        self._sync()

    def load_model_context_metadata(self, api=None):
        import json
        from .model_context import model_context_cache_key, sanitized_model_context_metadata
        api = api or self.load_api_settings()
        api.validate(require_model=False)
        if not api.model.strip():
            return None
        key = model_context_cache_key(api.base_url, api.model)
        try:
            raw = self._settings.value("model_context/" + key, "", type=str)
            value = json.loads(raw) if raw else None
            return sanitized_model_context_metadata(value, api.model)
        except (TypeError, ValueError, RecursionError):
            return None

    def save_model_context_metadata(self, metadata, api=None):
        """Cache a previously obtained provider model entry, never fetch one."""
        import json
        from .model_context import model_context_cache_key, sanitized_model_context_metadata
        api = api or self.load_api_settings()
        api.validate(require_model=True)
        value = sanitized_model_context_metadata(metadata, api.model)
        if value is None:
            raise SettingsError("模型接口未提供可确认的上下文窗口资料。")
        self._reject_public_api_keys(value)
        key = model_context_cache_key(api.base_url, api.model)
        self._settings.setValue("model_context/" + key, json.dumps(value, ensure_ascii=False))
        self._sync()
