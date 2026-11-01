"""Discover installed applications and activate an observed candidate once.

There is no executable/path/argument supplied by the model. Only an existing
Desktop/Start Menu entry observed by the discovery tool can reach Explorer.
Explorer owns the launched application, outside the task worker's kill-on-close
Job. Never fall back to ShellExecuteW or subprocess from this worker.
"""
from __future__ import annotations

import asyncio
import configparser
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit


_BAD_NAME = re.compile(r"[\r\n\x00-\x1f/\\:;|&<>*=]|然后|并且|同时|接着|以及|还有|再|并|和|及|与|、|\b(?:and|then|also|after|with)\b", re.I)
_LOCATION = r"(?:在|从)?(?:电脑)?桌面(?:上)?(?:的)?"
_DENIED_TARGETS = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe",
    "mshta.exe", "rundll32.exe", "regsvr32.exe", "reg.exe", "schtasks.exe",
    "python.exe", "pythonw.exe", "python3.exe", "node.exe", "bash.exe",
    "sh.exe", "wsl.exe", "conhost.exe", "wt.exe", "windowsterminal.exe",
    "msiexec.exe", "installutil.exe", "explorer.exe",
})


def normalized_name(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def parse_open_request(task: str) -> str | None:
    """Accept a whole, single launch instruction; never infer from its history."""
    if not isinstance(task, str) or len(task) > 240:
        return None
    value = unicodedata.normalize("NFKC", task).strip().rstrip("。.!！?").strip()
    # A polite Chinese suffix and a desktop location are the only tolerated
    # suffixes. Additional goals, shell syntax, paths and quoted instructions
    # cannot be converted into an application launch.
    value = re.sub(r"(?:一下)?(?:吧|谢谢)$", "", value).strip()
    value = re.sub(r"\s*[,，]?\s*" + _LOCATION + r"$", "", value).strip()
    chinese = re.fullmatch(
        r"(?:(?:请|麻烦)(?:你)?)?(?:(?:帮|替|给)我)?(?:现在|直接)?"
        r"(?:打开|启动)(?:一下)?\s*(?:" + _LOCATION + r")?\s*(.+)", value)
    english = re.fullmatch(r"(?:please\s+)?(?:open|launch)\s+(.+?)(?:\s+(?:on|from)\s+(?:the\s+)?desktop)?", value, re.I)
    if not (chinese or english):
        return None
    name = (chinese or english).group(1).strip()
    if len(name) >= 2 and (name[0], name[-1]) in {('"', '"'), ("'", "'"), ("“", "”"), ("「", "」")}:
        name = name[1:-1].strip()
    if not 1 <= len(name) <= 64 or _BAD_NAME.search(name) or any(char in name for char in ',，。!?？“”"\''):
        return None
    # The remaining name must contain only characters that can be a concise
    # shortcut display name, rather than a path or another sentence.
    if not all(char.isalnum() or char in " ._+-()（）·" for char in name):
        return None
    return name


@dataclass(frozen=True)
class Shortcut:
    name: str
    path: Path
    target: Path
    arguments: str = ""
    working_directory: str = ""
    kind: str = "shortcut"
    source: str = ""
    description: str = ""
    protocol_url: str = ""
    fingerprint: tuple = ()


@dataclass(frozen=True)
class InstalledApplication:
    name: str
    identity: str
    publisher: str = ""


def candidate_id(candidate) -> str:
    """Opaque, stable local identity; never expose filesystem/registry paths."""
    identity = ("installed:" + candidate.identity if isinstance(candidate, InstalledApplication)
                else candidate.kind + ":" + os.path.normcase(str(candidate.path)))
    return "app_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _public_text(value, limit=160):
    text = " ".join(str(value or "").split())[:limit]
    return "[local path omitted]" if re.search(r"[A-Za-z]:[\\/]|\\\\", text) else text


class LaunchUnavailable(Exception):
    def __init__(self, code="APP_LAUNCH_FAILED"):
        self.code = code
        super().__init__(code)


class PendingLaunch:
    """Thread-safe state only; no COM interface crosses a thread boundary."""
    def __init__(self):
        self.requested = threading.Event()
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.error_code = None

    def before_execute(self):
        if self.cancelled.is_set():
            raise LaunchUnavailable("APP_LAUNCH_FAILED")
        self.requested.set()


class WindowsApplications:
    """Lazy Windows COM/process access; construction has no native side effects."""

    def __init__(self):
        self._pythoncom = None

    def initialize(self):
        if os.name != "nt":
            raise LaunchUnavailable("APP_UNSUPPORTED")
        import pythoncom
        self._pythoncom = pythoncom
        pythoncom.CoInitialize()

    def close(self):
        if self._pythoncom is not None:
            self._pythoncom.CoUninitialize()
            self._pythoncom = None

    @staticmethod
    def roots():
        from win32com.shell import shell, shellcon
        identifiers = (shellcon.CSIDL_DESKTOPDIRECTORY, shellcon.CSIDL_COMMON_DESKTOPDIRECTORY,
                       shellcon.CSIDL_PROGRAMS, shellcon.CSIDL_COMMON_PROGRAMS)
        return tuple(Path(shell.SHGetFolderPath(0, value, 0, 0)) for value in identifiers)

    @staticmethod
    def _plain_file(path: Path) -> bool:
        try:
            # Do not walk directory junctions or accept reparse-point links.
            return path.is_file() and not path.is_symlink() and not (path.stat().st_file_attributes & 0x400)
        except OSError:
            return False

    @staticmethod
    def _walkable_directory(path: Path) -> bool:
        try:
            return path.is_dir() and not path.is_symlink() and not (path.stat().st_file_attributes & 0x400)
        except OSError:
            return False

    def candidates(self, name: str):
        wanted, found, seen = normalized_name(name), [], set()
        for root in self.roots():
            if not root.is_dir():
                continue
            inspected = 0
            # Start menu program folders can nest, but this is never a full
            # disk scan. A bounded walk skips junctions/symbolic-link folders.
            for folder, dirs, files in os.walk(root, followlinks=False):
                relative = Path(folder).relative_to(root)
                dirs[:] = [entry for entry in sorted(dirs) if len(relative.parts) < 6
                           and not (Path(folder) / entry).is_symlink()
                           and not ((Path(folder) / entry).stat().st_file_attributes & 0x400)]
                for filename in sorted(files):
                    inspected += 1
                    if inspected > 4000:
                        dirs[:] = []
                        break
                    path = Path(folder) / filename
                    if path.suffix.casefold() != ".lnk" or normalized_name(path.stem) != wanted or not self._plain_file(path):
                        continue
                    resolved = str(path.resolve()).casefold()
                    if resolved not in seen:
                        seen.add(resolved)
                        found.append(self.resolve_shortcut(path))
                if inspected > 4000:
                    break
        return found

    @staticmethod
    def _fingerprint(path):
        info = path.stat()
        return (info.st_size, info.st_mtime_ns)

    @staticmethod
    def executable_description(path):
        """Read version-resource labels, without executing the binary."""
        try:
            import win32api
            translations = win32api.GetFileVersionInfo(str(path), "\\VarFileInfo\\Translation")
            if translations:
                language, codepage = translations[0]
                prefix = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\"
                values = [win32api.GetFileVersionInfo(str(path), prefix + field)
                          for field in ("ProductName", "FileDescription")]
                return _public_text("; ".join(dict.fromkeys(value for value in values if value)))
        except Exception:
            pass
        return ""

    @classmethod
    def resolve_executable(cls, path):
        target = path.resolve()
        if (path.suffix.casefold() != ".exe" or target.name.casefold() in _DENIED_TARGETS
                or str(target).startswith("\\\\") or not cls._plain_file(path)):
            raise LaunchUnavailable("APP_UNSUPPORTED")
        return Shortcut(path.name, target, target, kind="executable",
                        description=cls.executable_description(target), fingerprint=cls._fingerprint(target))

    @staticmethod
    def _protocol_target(scheme):
        """Resolve only the installed protocol handler; never execute its command."""
        import ctypes
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
                winreg.QueryValueEx(key, "URL Protocol")
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme + r"\shell\open\command") as key:
                command = os.path.expandvars(winreg.QueryValueEx(key, None)[0])
            from ctypes import wintypes
            shell = ctypes.WinDLL("shell32", use_last_error=True)
            shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
            shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [wintypes.HLOCAL]
            count = ctypes.c_int()
            arguments = shell.CommandLineToArgvW(command, ctypes.byref(count))
            if not arguments:
                raise LaunchUnavailable("APP_UNSUPPORTED")
            try:
                target = Path(arguments[0])
            finally:
                kernel.LocalFree(arguments)
            if (not target.is_absolute() or target.suffix.casefold() != ".exe"
                    or target.name.casefold() in _DENIED_TARGETS or not target.is_file()
                    or str(target).startswith("\\\\")):
                raise LaunchUnavailable("APP_UNSUPPORTED")
            return target.resolve()
        except (OSError, IndexError, TypeError):
            raise LaunchUnavailable("APP_UNSUPPORTED") from None

    @classmethod
    def resolve_url(cls, path):
        # Game activation URLs are accepted only from a local observed .url,
        # with an installed handler. Arbitrary protocols/HTTP/file URLs and
        # shell command arguments are never accepted from a model.
        if not cls._plain_file(path) or path.stat().st_size > 16_384:
            raise LaunchUnavailable("APP_UNSUPPORTED")
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-16", "mbcs"):
            try:
                content = raw.decode(encoding)
                parser = configparser.ConfigParser(interpolation=None, strict=True)
                parser.read_string(content)
                url = parser.get("InternetShortcut", "URL")
                break
            except (UnicodeError, LookupError, configparser.Error):
                continue
        else:
            raise LaunchUnavailable("APP_UNSUPPORTED")
        if len(url) > 2048 or any(ord(char) < 32 for char in url):
            raise LaunchUnavailable("APP_UNSUPPORTED")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise LaunchUnavailable("APP_UNSUPPORTED") from None
        safe = (parsed.scheme.casefold() == "steam" and parsed.netloc.casefold() in {"rungameid", "run"}
                and re.fullmatch(r"/[0-9]+/?", parsed.path) and not parsed.query and not parsed.fragment)
        if parsed.scheme.casefold() == "com.epicgames.launcher":
            query = parse_qs(parsed.query)
            safe = (parsed.netloc == "apps" and re.fullmatch(r"/[A-Za-z0-9_:.-]+", parsed.path)
                    and query.get("action") == ["launch"] and set(query) <= {"action", "silent"}
                    and query.get("silent", ["true"]) in (["true"], ["false"]) and not parsed.fragment)
        if not safe:
            raise LaunchUnavailable("APP_UNSUPPORTED")
        return Shortcut(path.stem, path.resolve(), cls._protocol_target(parsed.scheme.casefold()),
                        kind="protocol", protocol_url=url, fingerprint=cls._fingerprint(path))

    def resolve_entry(self, shortcut):
        if shortcut.kind == "executable":
            return self.resolve_executable(shortcut.path)
        if shortcut.kind == "protocol":
            return self.resolve_url(shortcut.path)
        return self.resolve_shortcut(shortcut.path)

    @staticmethod
    def installed_applications():
        """Read display labels only, without uninstall commands or credentials."""
        import winreg
        found = []
        for hive, label in ((winreg.HKEY_CURRENT_USER, "user"), (winreg.HKEY_LOCAL_MACHINE, "machine")):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view) as root:
                        for index in range(min(winreg.QueryInfoKey(root)[0], 2000)):
                            key_name = winreg.EnumKey(root, index)
                            try:
                                with winreg.OpenKey(root, key_name) as key:
                                    name = winreg.QueryValueEx(key, "DisplayName")[0]
                                    try:
                                        publisher = winreg.QueryValueEx(key, "Publisher")[0]
                                    except OSError:
                                        publisher = ""
                                    if isinstance(name, str) and name.strip():
                                        found.append(InstalledApplication(_public_text(name), f"{label}:{view}:{key_name}", _public_text(publisher)))
                            except OSError:
                                continue
                except OSError:
                    continue
        return found

    def discover(self):
        found, seen = [], set()
        for index, root in enumerate(self.roots()):
            if not root.is_dir():
                continue
            inspected = 0
            for folder, dirs, files in os.walk(root, followlinks=False):
                relative = Path(folder).relative_to(root)
                # Executables are discovered only on the visible Desktop, not
                # recursively from arbitrary folders or installation trees.
                dirs[:] = [entry for entry in sorted(dirs) if index >= 2 and len(relative.parts) < 6
                           and self._walkable_directory(Path(folder) / entry)]
                for filename in sorted(files):
                    inspected += 1
                    if inspected > 4000:
                        dirs[:] = []
                        break
                    path = Path(folder) / filename
                    if path.suffix.casefold() not in {".lnk", ".url", ".exe"} or not self._plain_file(path):
                        continue
                    if path.suffix.casefold() == ".exe" and index >= 2:
                        continue
                    identity = os.path.normcase(str(path.resolve()))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    try:
                        if path.suffix.casefold() == ".exe":
                            entry = self.resolve_executable(path)
                        elif path.suffix.casefold() == ".url":
                            entry = self.resolve_url(path)
                        else:
                            entry = self.resolve_shortcut(path)
                        # Source is a human-readable location, never an absolute path.
                        source = ("Desktop" if index < 2 else "Start Menu") + (" / " + _public_text(str(relative)) if relative.parts else "")
                        from dataclasses import replace
                        found.append(replace(entry, source=source))
                    except Exception:
                        # A broken COM shortcut or inaccessible entry must not
                        # hide the rest of the user's application directory.
                        continue
                if inspected > 4000:
                    break
        found.extend(self.installed_applications())
        return found

    @staticmethod
    def resolve_shortcut(path: Path) -> Shortcut:
        import win32com.client
        link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
        target = Path(os.path.expandvars(str(link.TargetPath)))
        if (not target.is_absolute() or target.suffix.casefold() != ".exe"
                or target.name.casefold() in _DENIED_TARGETS or not target.is_file()
                or str(target).startswith("\\\\")):
            raise LaunchUnavailable("APP_UNSUPPORTED")
        arguments = str(link.Arguments)
        # A launch is an app activation, not execution of an embedded command.
        # Parameterized shortcuts need the visual path until explicitly supported.
        if arguments.strip():
            raise LaunchUnavailable("APP_UNSUPPORTED")
        return Shortcut(path.stem, path.resolve(), target.resolve(), arguments, str(link.WorkingDirectory),
                        description=_public_text(str(link.Description)) or WindowsApplications.executable_description(target))

    @staticmethod
    def _process_path(pid: int):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            value, count = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            return Path(value.value).resolve() if kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(count)) else None
        finally:
            kernel.CloseHandle(handle)

    def running_targets(self, shortcut: Shortcut):
        import win32process
        expected = os.path.normcase(str(shortcut.target))
        return {pid for pid in win32process.EnumProcesses()
                if pid and (path := self._process_path(pid)) is not None
                and os.path.normcase(str(path)) == expected}

    def admin_prompt_processes(self):
        import win32process
        # Access to a secure desktop process can be denied; input_desktop_available
        # provides an independent protected-desktop check in that case.
        expected = os.path.normcase(str((Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "consent.exe").resolve()))
        return {pid for pid in win32process.EnumProcesses()
                if pid and (path := self._process_path(pid)) is not None
                and os.path.normcase(str(path)) == expected}

    @staticmethod
    def input_desktop_available():
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user.OpenInputDesktop.restype = wintypes.HANDLE
        user.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not handle:
            return False
        user.CloseDesktop(handle)
        return True

    @staticmethod
    def visible_window(pids):
        import win32gui
        import win32process
        found = []
        def inspect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if right > left and bottom > top:
                    found.append(hwnd)
        win32gui.EnumWindows(inspect, None)
        return bool(found)

    def launch_through_desktop(self, shortcut: Shortcut, before_execute=None):
        import win32com.client
        import win32process
        # Recheck the exact link immediately before dispatch; never substitute a
        # model-supplied executable, path, argument or working directory.
        from dataclasses import replace
        if replace(self.resolve_entry(shortcut), source=shortcut.source) != shortcut:
            raise LaunchUnavailable("APP_SHORTCUT_CHANGED")
        try:
            desktop = win32com.client.Dispatch("Shell.Application").Windows().FindWindowSW(0, 0, 8, 0, 1)
            owner = self._process_path(win32process.GetWindowThreadProcessId(int(desktop.HWND))[1])
            explorer = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe").resolve()
            if owner is None or os.path.normcase(str(owner)) != os.path.normcase(str(explorer)):
                raise LaunchUnavailable("APP_SHELL_UNAVAILABLE")
            # This automation object belongs to the already-running Explorer
            # desktop. A direct ShellExecuteW/subprocess fallback would inherit
            # the worker's Job and be killed as soon as its task ends.
            if before_execute is not None:
                before_execute()
            desktop.Document.Application.ShellExecute(str(shortcut.path), "", str(shortcut.path.parent), "open", 1)
        except LaunchUnavailable:
            raise
        except Exception:
            raise LaunchUnavailable("APP_SHELL_UNAVAILABLE") from None

    def begin_launch(self, shortcut: Shortcut):
        pending = PendingLaunch()
        def dispatch():
            import pythoncom
            initialized = False
            try:
                # Every interface used by launch_through_desktop is obtained on
                # this thread. Explorer may block this call while UAC is open;
                # the async task must remain responsive to stop/secure desktop.
                pythoncom.CoInitialize()
                initialized = True
                self.launch_through_desktop(shortcut, pending.before_execute)
            except LaunchUnavailable as exc:
                pending.error_code = exc.code
            except Exception:
                pending.error_code = "APP_LAUNCH_FAILED"
            finally:
                if initialized:
                    pythoncom.CoUninitialize()
                pending.done.set()
        # No ThreadPoolExecutor: its interpreter shutdown joins running threads
        # and would recreate the original indefinite UAC wait on worker exit.
        threading.Thread(target=dispatch, name="aura-shell-activation", daemon=True).start()
        return pending


_MESSAGES = {
    "APP_NOT_FOUND": "没有找到名称完全匹配的桌面或开始菜单应用快捷方式。本次没有启动其他应用。",
    "APP_AMBIGUOUS": "找到了多个名称相同但目标不同的应用快捷方式，请明确要打开哪一个。",
    "APP_UNSUPPORTED": "这个快捷方式暂不支持直接打开，请通过桌面手动确认。",
    "APP_SHELL_UNAVAILABLE": "暂时无法通过 Windows 桌面启动应用，本次没有改用其他命令启动。",
    "APP_LAUNCH_FAILED": "打开应用时遇到问题，请检查桌面上的快捷方式。",
    "APP_NOT_VERIFIED": "已发送一次打开请求，但尚未确认目标应用的可见窗口。请检查桌面；本次不会重复启动。",
    "APP_SHORTCUT_CHANGED": "应用快捷方式发生了变化，本次已停止，请确认后重新发送任务。",
    "APP_ALREADY_ATTEMPTED": "本次任务已经尝试打开过应用，请先检查桌面，不会重复启动。",
    "APP_ADMIN_REQUIRED": "Windows 正在等待管理员授权。请你在系统弹窗中手动点击“是”或“否”；本次不会重复启动应用，也不会自动通过授权。",
    "APP_PROTOCOL_NOT_VERIFIED": "已通过已注册的启动器发送一次游戏快捷方式打开请求；尚未确认游戏本身的可见窗口，本次不会重复启动。",
}


class ApplicationDiscovery:
    """Read-only, bounded observations backing opaque native launch IDs."""
    def __init__(self, native=None):
        self.native = native or WindowsApplications()
        self.observed = {}

    async def run(self, query: str = ""):
        self.observed = {}
        if not isinstance(query, str) or len(query) > 160 or any(ord(char) < 32 for char in query):
            return json.dumps({"status": "lookup_only", "code": "APP_INVALID_QUERY", "applications": [], "can_continue": True})
        try:
            self.native.initialize()
            entries = self.native.discover()
        except Exception:
            return json.dumps({"status": "lookup_only", "code": "APP_DISCOVERY_UNAVAILABLE", "applications": [],
                               "can_continue": True, "summary": "应用目录暂时无法读取，可以通过桌面观察继续查找。"}, ensure_ascii=False)
        finally:
            self.native.close()
        wanted = normalized_name(query)
        def searchable(entry):
            values = [entry.name]
            if isinstance(entry, InstalledApplication):
                values.append(entry.publisher)
            else:
                values.extend((entry.target.name, entry.description))
            return [normalized_name(value) for value in values]
        def rank(entry):
            labels = searchable(entry)
            return (0 if wanted and wanted in labels else 1,
                    1 if isinstance(entry, InstalledApplication) else 0,
                    0 if getattr(entry, "source", "").startswith("Desktop") else 1,
                    normalized_name(entry.name), candidate_id(entry))
        matches = sorted((entry for entry in entries if not wanted or any(wanted in value for value in searchable(entry))), key=rank)
        applications = []
        for entry in matches[:80]:
            identifier = candidate_id(entry)
            self.observed[identifier] = entry
            item = {"candidate_id": identifier, "name": entry.name,
                    "kind": "installed_metadata" if isinstance(entry, InstalledApplication) else entry.kind,
                    "source": "Installed apps" if isinstance(entry, InstalledApplication) else entry.source,
                    "can_launch": not isinstance(entry, InstalledApplication)}
            if isinstance(entry, InstalledApplication):
                item["publisher"] = entry.publisher
            else:
                item.update(executable_name=entry.target.name, description=entry.description)
                if entry.kind == "protocol":
                    item["verification_scope"] = "registered_launcher_only"
            applications.append(item)
        return json.dumps({"status": "completed", "code": "APP_DISCOVERED", "applications": applications,
                           "total_matches": len(matches), "truncated": len(matches) > 80,
                           "summary": "这是只读应用目录结果，尚未启动任何应用。"}, ensure_ascii=False)


class ApplicationLaunch:
    """An original-task-bound, at-most-once controlled tool.

    The same callable serves the deterministic one-app path and its nanobot Tool
    registration. Tool arguments from the planning model are deliberately ignored.
    """
    def __init__(self, original_task: str, native=None, verification_seconds=12.0, *, selected=None):
        self.name = selected.name if selected is not None else parse_open_request(original_task)
        self.selected = selected
        self.native = native or WindowsApplications()
        self.verification_seconds = verification_seconds
        self.called = False
        self.dispatched = False
        self.last_result = None

    def _result(self, code, *, completed=False, already_running=False):
        summary = (_MESSAGES.get(code) or (f"已打开「{self.name}」，并确认目标应用的窗口正在显示。" if not already_running
                                          else f"「{self.name}」已经在运行，已发送打开请求并确认应用窗口。"))
        self.last_result = {"status": "completed" if completed else "needs_user", "code": code,
                            "summary": summary, "dispatched": self.dispatched, "verified": completed}
        return json.dumps(self.last_result, ensure_ascii=False)

    async def run(self, **_model_arguments):
        if self.name is None:
            raise LaunchUnavailable("APP_UNSUPPORTED")
        if self.called:
            return self._result("APP_ALREADY_ATTEMPTED")
        self.called = True
        pending = None
        try:
            self.native.initialize()
            existing_prompts = self.native.admin_prompt_processes()
            if existing_prompts or not self.native.input_desktop_available():
                return self._result("APP_ADMIN_REQUIRED")
            candidates = [self.selected] if self.selected is not None else self.native.candidates(self.name)
            if not candidates:
                return self._result("APP_NOT_FOUND")
            distinct = {(os.path.normcase(str(item.target)), item.arguments) for item in candidates}
            if len(distinct) != 1:
                return self._result("APP_AMBIGUOUS")
            shortcut = candidates[0]
            before = self.native.running_targets(shortcut)
            await asyncio.sleep(0)  # cancellation boundary before any launch
            # Recheck immediately before dispatch so an unrelated UAC opened
            # during shortcut resolution cannot queue a second application.
            existing_prompts = self.native.admin_prompt_processes()
            if existing_prompts or not self.native.input_desktop_available():
                return self._result("APP_ADMIN_REQUIRED")
            pending = self.native.begin_launch(shortcut)
            deadline = time.monotonic() + self.verification_seconds
            while True:
                self.dispatched = pending.requested.is_set()
                if self.dispatched:
                    new_prompts = self.native.admin_prompt_processes() - existing_prompts
                    if new_prompts or not self.native.input_desktop_available():
                        return self._result("APP_ADMIN_REQUIRED")
                    running = self.native.running_targets(shortcut)
                    if running and self.native.visible_window(running):
                        if shortcut.kind == "protocol":
                            return self._result("APP_PROTOCOL_NOT_VERIFIED")
                        return self._result("APP_OPENED", completed=True, already_running=bool(before))
                if pending.done.is_set() and pending.error_code:
                    return self._result(pending.error_code if pending.error_code in _MESSAGES else "APP_LAUNCH_FAILED")
                if time.monotonic() >= deadline:
                    return self._result("APP_NOT_VERIFIED" if self.dispatched else "APP_SHELL_UNAVAILABLE")
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except LaunchUnavailable as exc:
            return self._result(exc.code if exc.code in _MESSAGES else "APP_LAUNCH_FAILED")
        except Exception:
            return self._result("APP_LAUNCH_FAILED")
        finally:
            if pending is not None:
                pending.cancelled.set()
            self.native.close()


class AuthorizedApplicationLaunch:
    """Activate a current observed candidate, at most once, with verification.

    Selection comes from native tool results, not model-authored executable
    paths. The model resolves conversation references under the worker policy.
    The legacy name-only interface remains restricted to the current task.
    """
    def __init__(self, original_task: str, native=None, verification_seconds=12.0, *, discovery=None):
        self.original_task = original_task
        self.native = native
        self.verification_seconds = verification_seconds
        self.called = False
        self.last_result = None
        self._lock = asyncio.Lock()
        self.discovery = discovery

    async def run(self, application_name: str = "", candidate_id: str = ""):
        async with self._lock:
            if self.called:
                return json.dumps(self.last_result or {"status": "needs_user", "code": "APP_ALREADY_ATTEMPTED",
                    "summary": _MESSAGES["APP_ALREADY_ATTEMPTED"], "dispatched": False, "verified": False}, ensure_ascii=False)
            selected = None
            if candidate_id:
                selected = self.discovery.observed.get(candidate_id) if self.discovery is not None and isinstance(candidate_id, str) else None
                if not isinstance(selected, Shortcut):
                    self.last_result = {"status": "lookup_only", "code": "APP_REDISCOVER_REQUIRED",
                                        "summary": "请先读取当前应用目录，再选择其中可以启动的候选 ID。尚未启动应用。",
                                        "can_continue": True, "dispatched": False, "verified": False}
                    return json.dumps(self.last_result, ensure_ascii=False)
            elif (not isinstance(application_name, str) or not 1 <= len(application_name) <= 64
                    or normalized_name(application_name) not in normalized_name(self.original_task)
                    or parse_open_request("open " + application_name) != application_name.strip()):
                self.last_result = {"status": "needs_user", "code": "APP_UNSUPPORTED",
                                    "summary": _MESSAGES["APP_UNSUPPORTED"], "dispatched": False, "verified": False}
                return json.dumps(self.last_result, ensure_ascii=False)
            self.called = True
            launch = ApplicationLaunch("open " + application_name, native=self.native,
                                       verification_seconds=self.verification_seconds, selected=selected)
            result = await launch.run()
            self.last_result = json.loads(result)
            # A failed lookup had no native side effect. It may be followed by
            # discovery or a visual lookup; uncertain activations remain final.
            if self.last_result["code"] in {"APP_NOT_FOUND", "APP_AMBIGUOUS", "APP_UNSUPPORTED"} and not self.last_result["dispatched"]:
                self.called = False
                self.last_result.update(status="lookup_only", can_continue=True)
                return json.dumps(self.last_result, ensure_ascii=False)
            return result
