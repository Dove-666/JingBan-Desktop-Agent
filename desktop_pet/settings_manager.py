"""Local preferences and an explicitly selected Windows credential vault.

Only non-secret preferences enter the INI file.  Backend exception details are
deliberately discarded: a provider or credential backend may echo a secret.
Tests may inject a small in-memory backend without touching the user's vault.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ApiSettings:
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    timeout_seconds: float = 30.0

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
    ) -> None:
        if settings is None:
            directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
            if not directory:
                raise SettingsError("无法确定当前用户的本地配置目录。")
            try:
                path = Path(directory) / "settings.ini"
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise SettingsError("无法创建当前用户的本地配置目录，请检查目录权限。") from None
            settings = QSettings(str(path), QSettings.Format.IniFormat)
        self._settings = settings
        self._credentials = credential_store if credential_store is not None else CredentialStore()

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
        defaults = ApiSettings()
        try:
            result = ApiSettings(
                base_url=self._settings.value("api/base_url", defaults.base_url),
                model=self._settings.value("api/model", defaults.model),
                timeout_seconds=float(self._settings.value("api/timeout_seconds", defaults.timeout_seconds)),
            )
            result.validate(require_model=False)
            return result
        except (SettingsError, ValueError, TypeError, OverflowError):
            # A damaged ordinary config must not prevent the pet from starting.
            return defaults

    def _sync(self) -> None:
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise SettingsError("设置未能写入本地配置文件，请检查当前用户的目录权限。")

    def save_api_settings(self, settings: ApiSettings, *, pending_api_key: str = "") -> None:
        settings.validate(require_model=False)
        # Pasting the key into Model Name or a URL path is an easy UI mistake.
        # Those fields are ordinary INI preferences, so reject a known secret
        # before writing anything. A missing vault must still allow non-secret
        # preferences to be saved; the caller can supply an unsaved input key.
        known_keys = [pending_api_key.strip()]
        if self.credentials_available:
            try:
                known_keys.append(self.get_api_key())
            except CredentialsError:
                pass
        if any(key and (key in settings.base_url or key in settings.model) for key in known_keys):
            raise SettingsError("Base URL 和模型名称不能包含 API Key，请只在 API Key 输入框中填写密钥。")
        self._settings.setValue("api/base_url", settings.base_url)
        self._settings.setValue("api/model", settings.model.strip())
        self._settings.setValue("api/timeout_seconds", float(settings.timeout_seconds))
        self._sync()

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

    def save_api_key(self, value: str) -> None:
        # Also protect callers which save the credential separately after
        # having accidentally entered that same key in an ordinary field.
        if isinstance(value, str) and value.strip():
            secret = value.strip()
            fields = (self._settings.value("api/base_url", ""), self._settings.value("api/model", ""))
            if any(isinstance(field, str) and secret in field for field in fields):
                raise CredentialsError("API Key 已出现在普通配置字段中，请先修正 Base URL 或模型名称，再保存密钥。")
        self._credentials.save_api_key(value)

    def clear_api_key(self) -> None:
        self._credentials.clear_api_key()

    def load_perception_settings(self):
        from dataclasses import fields
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
            return defaults  # A damaged setting must never opt in to screen sending.

    def save_perception_settings(self, settings) -> None:
        from dataclasses import fields
        settings.validate()
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
        for field in fields(settings):
            value = getattr(settings, field.name)
            self._settings.setValue("camera/" + field.name, list(value) if isinstance(value, tuple) else value)
        self._sync()

    def load_context_budget(self):
        from dataclasses import fields
        from .conversations import ContextBudget
        defaults = ContextBudget()
        try:
            values = {field.name: self._settings.value("context/" + field.name, getattr(defaults, field.name), type=int)
                      for field in fields(defaults)}
            result = ContextBudget(**values)
            result.validate()
            return result
        except (TypeError, ValueError, OverflowError):
            return defaults

    def save_context_budget(self, budget):
        from dataclasses import fields
        budget.validate()
        for field in fields(budget):
            self._settings.setValue("context/" + field.name, getattr(budget, field.name))
        self._sync()
