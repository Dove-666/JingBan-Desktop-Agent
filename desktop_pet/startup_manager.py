"""Current-user Windows login startup, isolated from ordinary preferences."""

from pathlib import Path
import subprocess
import sys

from .config import PROJECT_ROOT
from .branding import APP_NAME
from .settings_manager import SettingsError

try:
    import winreg
except ImportError:
    winreg = None


class StartupManager:
    """Own one HKCU Run value; constructing this service never writes to it."""

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE_NAME = APP_NAME
    LEGACY_VALUE_NAME = "AURA-Desktop-Agent"

    def __init__(self, *, project_root=None, python_executable=None, registry=None,
                 platform_name=None, frozen=None, profile_dir=None):
        self._registry = winreg if registry is None else registry
        self._platform = sys.platform if platform_name is None else platform_name
        self._project_root = Path(project_root if project_root is not None else PROJECT_ROOT).resolve()
        executable = Path(python_executable if python_executable is not None else sys.executable).resolve()
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
        self._pythonw = executable if self._frozen else executable.with_name("pythonw.exe")
        self._main = self._project_root / "main.py"
        self._profile_dir = Path(profile_dir).expanduser().resolve() if profile_dir is not None else None

    @property
    def available(self) -> bool:
        return self._platform == "win32" and self._registry is not None

    def _require_available(self):
        if not self.available:
            raise SettingsError("开机启动仅支持 Windows 当前用户，无需管理员权限。")

    def _command(self):
        arguments = [str(self._pythonw)]
        if not self._frozen:
            arguments.append(str(self._main))
        arguments.append("--autostart")
        if self._profile_dir is not None:
            arguments.extend(("--profile-dir", str(self._profile_dir)))
        return subprocess.list2cmdline(arguments)

    def _read_value(self, name=None):
        registry = self._registry
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                                  registry.KEY_QUERY_VALUE) as key:
                return registry.QueryValueEx(key, self.VALUE_NAME if name is None else name)
        except FileNotFoundError:
            return None

    def is_enabled(self) -> bool:
        if not self.available:
            return False
        try:
            expected = (self._command(), self._registry.REG_SZ)
            return (self._read_value() == expected
                    or self._read_value(self.LEGACY_VALUE_NAME) == expected)
        except Exception:
            raise SettingsError("无法读取开机启动状态，请检查当前用户的注册表权限。") from None

    def set_enabled(self, enabled: bool) -> None:
        self._require_available()
        if type(enabled) is not bool:
            raise SettingsError("开机启动设置必须为开启或关闭。")
        registry = self._registry
        if enabled:
            if not self._pythonw.is_file() or not self._frozen and not self._main.is_file():
                raise SettingsError("未找到鲸伴启动程序，请从完整的安装目录启动。" if self._frozen else
                                    "未找到当前项目入口或 pythonw.exe，请从完整的 Python 环境启动桌宠。")
            try:
                with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                                          registry.KEY_SET_VALUE | registry.KEY_QUERY_VALUE) as key:
                    registry.SetValueEx(key, self.VALUE_NAME, 0, registry.REG_SZ, self._command())
                if self._read_value() != (self._command(), registry.REG_SZ):
                    raise ValueError
                # Remove only a legacy registration for this exact checkout.
                # A different checkout's entry is outside this migration.
                if self._read_value(self.LEGACY_VALUE_NAME) == (self._command(), registry.REG_SZ):
                    with registry.OpenKey(registry.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                                          registry.KEY_SET_VALUE | registry.KEY_QUERY_VALUE) as key:
                        registry.DeleteValue(key, self.LEGACY_VALUE_NAME)
                    if self._read_value(self.LEGACY_VALUE_NAME) is not None:
                        raise ValueError
            except Exception:
                raise SettingsError("开机启动未能保存或验证，请检查当前用户的注册表权限。") from None
        else:
            try:
                try:
                    with registry.OpenKey(registry.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                                          registry.KEY_SET_VALUE | registry.KEY_QUERY_VALUE) as key:
                        try:
                            registry.DeleteValue(key, self.VALUE_NAME)
                        except FileNotFoundError:
                            pass
                        if self._read_value(self.LEGACY_VALUE_NAME) == (self._command(), registry.REG_SZ):
                            registry.DeleteValue(key, self.LEGACY_VALUE_NAME)
                except FileNotFoundError:
                    pass
                if self._read_value() is not None or self.is_enabled():
                    raise ValueError
            except Exception:
                raise SettingsError("开机启动未能关闭或验证，请检查当前用户的注册表权限。") from None
