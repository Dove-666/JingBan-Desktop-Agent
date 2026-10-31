"""Public device preferences on disk; the endpoint's private key in the OS vault."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading

from ..settings_manager import CredentialStore, CredentialsError
from .errors import RemoteError


MAX_DEVICES = 16
PEER_ID = re.compile(r"[0-9a-f]{64}\Z")


class RemotePreferences:
    def __init__(self, settings):
        self.directory = Path(settings.config_path).parent / "remote"
        self.path = self.directory / "devices.json"
        self._lock = threading.RLock()
        self._data = {"version": 1, "enabled": False, "devices": []}
        self.load_error = False
        try:
            if self.path.exists():
                if self.path.stat().st_size > 65536:
                    raise ValueError("oversized")
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(data, dict) or data.get("version") != 1
                        or type(data.get("enabled")) is not bool
                        or not isinstance(data.get("devices"), list)
                        or len(data["devices"]) > MAX_DEVICES):
                    raise ValueError("invalid preferences")
                seen = set()
                for device in data["devices"]:
                    if (not isinstance(device, dict) or not isinstance(device.get("id"), str)
                            or not PEER_ID.fullmatch(device["id"]) or device["id"] in seen
                            or not isinstance(device.get("name"), str)
                            or not 1 <= len(device["name"]) <= 80
                            or any(ord(c) < 32 for c in device["name"])
                            or not isinstance(device.get("paired_at"), str)):
                        raise ValueError("invalid device")
                    seen.add(device["id"])
                self._data = {"version": 1, "enabled": data["enabled"],
                              "devices": [{key: device[key] for key in ("id", "name", "paired_at")}
                                          for device in data["devices"]]}
        except (OSError, ValueError, TypeError):
            # A damaged allowlist never silently authorizes an unknown peer.
            self.load_error = True
        scope = hashlib.sha256(str(Path(settings.config_path).resolve()).encode("utf-8")).hexdigest()[:24]
        credentials = getattr(settings, "_credentials", None)
        if credentials is None:
            self._vault = None
        else:
            # Reuse the selected secure backend, including isolated test vaults.
            self._vault = CredentialStore(service=credentials.service,
                account="remote-identity-" + scope, backend=credentials._backend)

    @property
    def enabled(self):
        with self._lock:
            return self._data["enabled"]

    @property
    def devices(self):
        with self._lock:
            return deepcopy(self._data["devices"])

    def authorized(self, peer_id):
        with self._lock:
            return any(device["id"] == peer_id for device in self._data["devices"])

    def _commit(self, data):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise RemoteError("storage_error", "无法保存远程连接设置，请检查本地目录权限。") from None
        self._data = data
        self.load_error = False

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise RemoteError("invalid_request", "连接开关必须为布尔值。")
        with self._lock:
            self._commit({**self._data, "enabled": enabled})

    def add_device(self, peer_id, name, paired_at):
        with self._lock:
            devices = self.devices
            if not self.authorized(peer_id) and len(devices) >= MAX_DEVICES:
                raise RemoteError("device_limit", "已配对设备已达上限，请先撤销不再使用的设备。")
            devices = [item for item in devices if item["id"] != peer_id]
            devices.append({"id": peer_id, "name": name, "paired_at": paired_at})
            self._commit({**self._data, "devices": devices})

    def revoke(self, peer_id):
        with self._lock:
            self._commit({**self._data, "devices": [item for item in self._data["devices"]
                                                     if item["id"] != peer_id]})

    def identity_secret(self):
        if self._vault is None or not self._vault.available:
            raise RemoteError("credentials_unavailable", "无法访问系统凭据，远程设备密钥未使用明文保存。")
        try:
            value = self._vault.get_api_key()
            if value:
                if not PEER_ID.fullmatch(value):
                    raise RemoteError("credentials_unavailable", "系统凭据中的远程设备密钥无效。")
                return value
            value = secrets.token_hex(32)
            self._vault.save_api_key(value)
            return value
        except CredentialsError:
            raise RemoteError("credentials_unavailable", "无法可靠保存远程设备密钥，请检查系统凭据权限。") from None

