"""Application launch policy/verification tests; every launch is a fake."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_apps import (ApplicationDiscovery, ApplicationLaunch, AuthorizedApplicationLaunch, InstalledApplication,
                          LaunchUnavailable, PendingLaunch, Shortcut, WindowsApplications, normalized_name, parse_open_request)


class FakeNative:
    def __init__(self, shortcuts=None, *, visible=True, running=True, failure=None):
        self.shortcuts = shortcuts if shortcuts is not None else [Shortcut("Demo App", Path("Demo App.lnk"), Path("Demo.exe"))]
        self.visible = visible
        self.running = running
        self.failure = failure
        self.dispatched = []
        self.looked_up = []
        self.closed = False
        self.prompts = set()
        self.desktop_available = True
        self.pending = None

    def initialize(self):
        pass
    def close(self):
        self.closed = True
    def candidates(self, name):
        self.looked_up.append(name)
        return self.shortcuts
    def running_targets(self, shortcut):
        return {100} if self.running and self.dispatched else set()
    def visible_window(self, pids):
        return self.visible
    def admin_prompt_processes(self):
        return set(self.prompts)
    def input_desktop_available(self):
        return self.desktop_available
    def begin_launch(self, shortcut):
        pending = self.pending = PendingLaunch()
        try:
            self.launch_through_desktop(shortcut)
            pending.before_execute()
        except LaunchUnavailable as exc:
            pending.error_code = exc.code
        pending.done.set()
        return pending
    def launch_through_desktop(self, shortcut):
        if self.failure:
            raise self.failure
        self.dispatched.append(shortcut)


class ParseTests(unittest.TestCase):
    def test_single_original_launch_instruction(self):
        for task, expected in (
            ("给我打开三角洲行动，在桌面上", "三角洲行动"),
            ("请帮我打开桌面上的 Demo App。", "Demo App"),
            ("启动「Demo App」", "Demo App"),
            ("打开 Visual Studio Code", "Visual Studio Code"),
            ("Please open Demo App on the desktop", "Demo App"),
        ):
            with self.subTest(task=task):
                self.assertEqual(parse_open_request(task), expected)

    def test_no_paths_multi_action_advice_or_arbitrary_commands(self):
        for task in (
            "帮我打开 Demo App，然后删除文件", "打开 Demo App 并登录", "打开 Demo App 和 Other App",
            "打开 Demo App & Other", "打开 C:\\Demo.exe", "打开../Demo", "打开 Demo;exit",
            "怎么打开 Demo App", "先别做，打开 Demo App", "解释打开 Demo App 的方法",
            "请看这段文字：打开 Demo App", "open Demo App and type hello", "\"打开 Demo App\"",
            "打开 Demo App，点击开始", "打开 Demo App\n打开 Other", "打开",
        ):
            with self.subTest(task=task):
                self.assertIsNone(parse_open_request(task))

    def test_normalization_is_exact_and_does_not_remove_punctuation(self):
        self.assertEqual(normalized_name("Ｄｅｍｏ　APP"), normalized_name("Demo App"))
        self.assertNotEqual(normalized_name("Demo App 2"), normalized_name("Demo App"))
        self.assertNotEqual(normalized_name("Demo-App"), normalized_name("Demo App"))


class LaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_task_binding_and_one_dispatch_with_verified_window(self):
        native = FakeNative()
        tool = ApplicationLaunch("打开 Demo App", native=native, verification_seconds=0)
        result = json.loads(await tool.run(task="打开 Other App", path="C:\\unsafe.exe"))
        self.assertEqual(native.looked_up, ["Demo App"])
        self.assertEqual(len(native.dispatched), 1)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["verified"])
        repeated = json.loads(await tool.run(task="打开 Other App"))
        self.assertEqual(repeated["code"], "APP_ALREADY_ATTEMPTED")
        self.assertEqual(len(native.dispatched), 1)
        self.assertTrue(native.closed)

    async def test_missing_or_ambiguous_target_never_dispatches(self):
        for links, code in (([], "APP_NOT_FOUND"),
                            ([Shortcut("App", Path("one.lnk"), Path("one.exe")),
                              Shortcut("App", Path("two.lnk"), Path("two.exe"))], "APP_AMBIGUOUS")):
            with self.subTest(code=code):
                native = FakeNative(links)
                result = json.loads(await ApplicationLaunch("打开 App", native=native).run())
                self.assertEqual(result["code"], code)
                self.assertEqual(result["status"], "needs_user")
                self.assertFalse(native.dispatched)

    async def test_duplicate_links_to_same_target_launch_once(self):
        native = FakeNative([Shortcut("App", Path("one.lnk"), Path("app.exe")),
                             Shortcut("App", Path("two.lnk"), Path("app.exe"))])
        result = json.loads(await ApplicationLaunch("打开 App", native=native, verification_seconds=0).run())
        self.assertTrue(result["verified"])
        self.assertEqual(len(native.dispatched), 1)

    async def test_dispatch_without_actual_visible_target_is_not_completed(self):
        for native in (FakeNative(visible=False), FakeNative(running=False)):
            result = json.loads(await ApplicationLaunch("打开 App", native=native, verification_seconds=0).run())
            self.assertEqual(result["status"], "needs_user")
            self.assertEqual(result["code"], "APP_NOT_VERIFIED")
            self.assertFalse(result["verified"])
            self.assertEqual(len(native.dispatched), 1)

    async def test_cancellation_boundary_prevents_late_launch(self):
        native = FakeNative()
        task = asyncio.create_task(ApplicationLaunch("打开 App", native=native).run())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(native.dispatched)
        self.assertTrue(native.closed)

    async def test_shell_error_contains_no_exception_or_path(self):
        native = FakeNative(failure=LaunchUnavailable("APP_SHELL_UNAVAILABLE"))
        result = json.loads(await ApplicationLaunch("打开 App", native=native).run())
        self.assertEqual(result["code"], "APP_SHELL_UNAVAILABLE")
        self.assertFalse(result["dispatched"])
        self.assertNotIn(".exe", json.dumps(result))

    async def test_existing_uac_never_queues_a_second_activation(self):
        native = FakeNative()
        native.prompts = {41116}
        result = json.loads(await ApplicationLaunch("打开 App", native=native).run())
        self.assertEqual(result["code"], "APP_ADMIN_REQUIRED")
        self.assertEqual(result["status"], "needs_user")
        self.assertFalse(result["dispatched"])
        self.assertFalse(native.dispatched)

    async def test_existing_secure_desktop_never_dispatches(self):
        native = FakeNative()
        native.desktop_available = False
        result = json.loads(await ApplicationLaunch("打开 App", native=native).run())
        self.assertEqual(result["code"], "APP_ADMIN_REQUIRED")
        self.assertFalse(native.dispatched)

    async def test_new_uac_returns_needs_user_while_shell_call_is_blocked(self):
        native = FakeNative(visible=False)
        original = native.begin_launch
        def begin(shortcut):
            pending = original(shortcut)
            pending.done.clear()  # Explorer's ShellExecute is still blocked
            native.prompts = {9001}
            return pending
        native.begin_launch = begin
        started = time.monotonic()
        result = json.loads(await ApplicationLaunch("打开 App", native=native).run())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(result["code"], "APP_ADMIN_REQUIRED")
        self.assertTrue(result["dispatched"])
        self.assertTrue(native.pending.cancelled.is_set())
        self.assertEqual(len(native.dispatched), 1)

    async def test_secure_desktop_transition_does_not_wait_for_com(self):
        native = FakeNative(visible=False)
        original = native.begin_launch
        def begin(shortcut):
            pending = original(shortcut)
            pending.done.clear()
            native.desktop_available = False
            return pending
        native.begin_launch = begin
        result = json.loads(await ApplicationLaunch("打开 App", native=native).run())
        self.assertEqual(result["code"], "APP_ADMIN_REQUIRED")
        self.assertEqual(len(native.dispatched), 1)

    async def test_cancellation_during_pending_shell_call_does_not_wait_or_repeat(self):
        native = FakeNative(visible=False)
        original = native.begin_launch
        def begin(shortcut):
            pending = original(shortcut)
            pending.done.clear()
            return pending
        native.begin_launch = begin
        task = asyncio.create_task(ApplicationLaunch("打开 App", native=native).run())
        while native.pending is None:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(native.pending.cancelled.is_set())
        self.assertEqual(len(native.dispatched), 1)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def native(self, entries):
        native = FakeNative()
        native.discover = lambda: entries
        return native

    async def test_partial_executable_names_find_observed_desktop_entry_without_launch(self):
        entry = Shortcut("launcher.exe", Path("C:/Desktop/launcher.exe"), Path("C:/Desktop/launcher.exe"),
                         kind="executable", source="Desktop", description="Delta Force launcher")
        native = self.native([entry, InstalledApplication("Delta Force", "registry:delta", "Publisher")])
        discovery = ApplicationDiscovery(native)
        result = json.loads(await discovery.run("launcher.exe"))
        self.assertEqual(result["applications"][0]["name"], "launcher.exe")
        self.assertEqual(result["applications"][0]["source"], "Desktop")
        self.assertFalse(native.dispatched)
        self.assertNotIn("C:/", json.dumps(result))
        first_id = result["applications"][0]["candidate_id"]
        self.assertEqual(first_id, json.loads(await discovery.run("Delta"))["applications"][0]["candidate_id"])
        self.assertTrue(native.closed)

    async def test_followup_candidate_id_uses_current_observation_without_task_name_parser(self):
        entry = Shortcut("launcher.exe", Path("launcher.exe"), Path("launcher.exe"), kind="executable", source="Desktop")
        native = self.native([entry])
        discovery = ApplicationDiscovery(native)
        observed = json.loads(await discovery.run("launcher.exe"))["applications"][0]
        tool = AuthorizedApplicationLaunch("就叫 launcher.exe 好像", native=native, discovery=discovery, verification_seconds=0)
        first = json.loads(await tool.run(candidate_id=observed["candidate_id"]))
        repeated = json.loads(await tool.run(candidate_id=observed["candidate_id"]))
        self.assertTrue(first["verified"])
        self.assertEqual(first, repeated)
        self.assertEqual(native.dispatched, [entry])
        self.assertFalse(native.looked_up)

    async def test_duplicate_generic_launchers_are_listed_separately_and_not_launched(self):
        entries = [Shortcut("launcher.exe", Path(f"{name}/launcher.exe"), Path(f"{name}/launcher.exe"), kind="executable", source="Desktop", description=name)
                   for name in ("Game One", "Game Two")]
        native = self.native(entries)
        result = json.loads(await ApplicationDiscovery(native).run("launcher.exe"))
        self.assertEqual(len(result["applications"]), 2)
        self.assertEqual(len({entry["candidate_id"] for entry in result["applications"]}), 2)
        self.assertEqual({entry["description"] for entry in result["applications"]}, {"Game One", "Game Two"})
        self.assertFalse(native.dispatched)

    async def test_ids_not_in_latest_results_and_installed_metadata_cannot_launch(self):
        entry = Shortcut("App", Path("App.lnk"), Path("App.exe"))
        native = self.native([entry, InstalledApplication("Other", "registry:other")])
        discovery = ApplicationDiscovery(native)
        old_id = json.loads(await discovery.run("App"))["applications"][0]["candidate_id"]
        metadata = json.loads(await discovery.run("Other"))["applications"][0]
        self.assertFalse(metadata["can_launch"])
        tool = AuthorizedApplicationLaunch("Open it", native=native, discovery=discovery)
        for identifier in (old_id, metadata["candidate_id"], "C:/arbitrary.exe"):
            result = json.loads(await tool.run(candidate_id=identifier))
            self.assertEqual(result["code"], "APP_REDISCOVER_REQUIRED")
            self.assertTrue(result["can_continue"])
        self.assertFalse(native.dispatched)
        self.assertFalse(tool.called)

    async def test_bounded_results_and_failed_lookup_allow_visual_continuation(self):
        native = self.native([InstalledApplication(f"App {index:03}", str(index)) for index in range(100)])
        discovery = ApplicationDiscovery(native)
        result = json.loads(await discovery.run())
        self.assertEqual(len(result["applications"]), 80)
        self.assertTrue(result["truncated"])
        native.shortcuts = []
        tool = AuthorizedApplicationLaunch("Open Demo App", native=native, discovery=discovery)
        missing = json.loads(await tool.run(application_name="Demo App"))
        self.assertEqual(missing["status"], "lookup_only")
        self.assertFalse(tool.called)
        self.assertFalse(native.dispatched)

    async def test_protocol_handler_window_does_not_verify_the_game(self):
        entry = Shortcut("Game", Path("Game.url"), Path("steam.exe"), kind="protocol", protocol_url="steam://rungameid/123")
        native = self.native([entry])
        discovery = ApplicationDiscovery(native)
        identifier = json.loads(await discovery.run())["applications"][0]["candidate_id"]
        tool = AuthorizedApplicationLaunch("Open Game", native=native, discovery=discovery, verification_seconds=0)
        result = json.loads(await tool.run(candidate_id=identifier))
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(result["code"], "APP_PROTOCOL_NOT_VERIFIED")
        self.assertFalse(result["verified"])
        self.assertTrue(result["dispatched"])
        self.assertEqual(len(native.dispatched), 1)


@unittest.skipUnless(os.name == "nt", "Windows COM adapter")
class NativeAdapterTests(unittest.TestCase):
    def test_discovery_includes_desktop_executable_and_safe_protocol_but_not_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as temp:
            desktop, menu = Path(temp) / "desktop", Path(temp) / "programs"
            desktop.mkdir()
            menu.mkdir()
            (desktop / "launcher.exe").write_bytes(b"dummy")
            (desktop / "Game.url").write_text("[InternetShortcut]\nURL=steam://rungameid/123\n", encoding="utf-8")
            (desktop / "Unsafe.url").write_text("[InternetShortcut]\nURL=file:///C:/unsafe.exe\n", encoding="utf-8")
            (desktop / "cmd.exe").write_bytes(b"dummy")
            (menu / "invisible.exe").write_bytes(b"dummy")
            native = WindowsApplications()
            with patch.object(native, "roots", return_value=(desktop, desktop, menu)), patch.object(native, "installed_applications", return_value=[]), patch.object(WindowsApplications, "_protocol_target", return_value=Path("C:/Steam/steam.exe")), patch.object(WindowsApplications, "executable_description", return_value="Game launcher"):
                entries = native.discover()
            self.assertEqual({entry.name for entry in entries}, {"launcher.exe", "Game"})
            self.assertEqual({entry.kind for entry in entries}, {"executable", "protocol"})

    def test_changed_desktop_executable_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "launcher.exe"
            executable.write_bytes(b"first")
            shortcut = WindowsApplications.resolve_executable(executable)
            executable.write_bytes(b"changed file")
            with self.assertRaises(LaunchUnavailable) as caught:
                WindowsApplications().launch_through_desktop(shortcut)
            self.assertEqual(caught.exception.code, "APP_SHORTCUT_CHANGED")

    def test_protocol_shortcuts_reject_non_activation_and_model_command_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            shortcut = Path(temp) / "Game.url"
            for url in ("steam://run/123//--delete", "steam://install/123", "https://example.com", "powershell:evil", "steam://rungameid/123?arg=unsafe"):
                shortcut.write_text("[InternetShortcut]\nURL=" + url + "\n", encoding="utf-8")
                with self.subTest(url=url), self.assertRaises(LaunchUnavailable):
                    WindowsApplications.resolve_url(shortcut)

    def test_blocking_shell_uses_own_com_daemon_thread_without_join(self):
        import pythoncom
        native = WindowsApplications()
        release, entered = threading.Event(), threading.Event()
        calls = []
        main_thread = threading.get_ident()
        def blocked(shortcut, before_execute):
            calls.append(("shell", threading.get_ident(), threading.current_thread().daemon))
            before_execute()
            entered.set()
            release.wait(2)
        with patch.object(native, "launch_through_desktop", side_effect=blocked), patch.object(pythoncom, "CoInitialize", side_effect=lambda: calls.append(("init", threading.get_ident()))), patch.object(pythoncom, "CoUninitialize", side_effect=lambda: calls.append(("close", threading.get_ident()))):
            started = time.monotonic()
            pending = native.begin_launch(Shortcut("Demo", Path("Demo.lnk"), Path("Demo.exe")))
            try:
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.wait(0.5))
                self.assertTrue(pending.requested.is_set())
                self.assertFalse(pending.done.is_set())
                self.assertTrue(all(entry[1] != main_thread for entry in calls))
                self.assertTrue(next(entry[2] for entry in calls if entry[0] == "shell"))
            finally:
                release.set()
                self.assertTrue(pending.done.wait(1))
            self.assertEqual([entry[0] for entry in calls], ["init", "shell", "close"])

    def test_shortcut_resolver_rejects_interpreters_arguments_and_non_executables(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for filename, args in (("cmd.exe", ""), ("Demo.exe", "--delete-everything"), ("Demo.cmd", "")):
                target = folder / filename
                target.write_bytes(b"dummy")
                link = MagicMock()
                link.TargetPath, link.Arguments, link.WorkingDirectory = str(target), args, str(folder)
                with self.subTest(filename=filename), patch("win32com.client.Dispatch") as dispatch:
                    dispatch.return_value.CreateShortcut.return_value = link
                    with self.assertRaises(LaunchUnavailable):
                        WindowsApplications.resolve_shortcut(folder / "Demo.lnk")

    def test_only_exact_names_are_resolved_and_junctions_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in ("Demo App.lnk", "Demo App 2.lnk", "Other.lnk", "Demo App.txt"):
                (folder / name).write_bytes(b"dummy")
            native = WindowsApplications()
            expected = Shortcut("Demo App", folder / "Demo App.lnk", folder / "Demo.exe")
            with patch.object(native, "roots", return_value=(folder,)), patch.object(native, "resolve_shortcut", return_value=expected) as resolve:
                self.assertEqual(native.candidates("ＤＥＭＯ App"), [expected])
                resolve.assert_called_once_with(folder / "Demo App.lnk")

    def test_shell_execute_is_bound_to_existing_explorer_desktop(self):
        import win32com.client
        import win32process
        native = WindowsApplications()
        shortcut = Shortcut("Demo", Path("C:/Demo.lnk").resolve(), Path("C:/Demo.exe").resolve())
        desktop = MagicMock()
        desktop.HWND = 456
        explorer = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe").resolve()
        with patch.object(native, "resolve_shortcut", return_value=shortcut), patch.object(native, "_process_path", return_value=explorer), patch.object(win32process, "GetWindowThreadProcessId", return_value=(1, 2)), patch.object(win32com.client, "Dispatch") as dispatch:
            dispatch.return_value.Windows.return_value.FindWindowSW.return_value = desktop
            native.launch_through_desktop(shortcut)
            dispatch.assert_called_once_with("Shell.Application")
            desktop.Document.Application.ShellExecute.assert_called_once_with(str(shortcut.path), "", str(shortcut.path.parent), "open", 1)

    def test_non_explorer_desktop_cannot_launch(self):
        import win32com.client
        import win32process
        native = WindowsApplications()
        shortcut = Shortcut("Demo", Path("C:/Demo.lnk"), Path("C:/Demo.exe"))
        desktop = MagicMock()
        desktop.HWND = 456
        with patch.object(native, "resolve_shortcut", return_value=shortcut), patch.object(native, "_process_path", return_value=Path("C:/other.exe")), patch.object(win32process, "GetWindowThreadProcessId", return_value=(1, 2)), patch.object(win32com.client, "Dispatch") as dispatch:
            dispatch.return_value.Windows.return_value.FindWindowSW.return_value = desktop
            with self.assertRaises(LaunchUnavailable) as caught:
                native.launch_through_desktop(shortcut)
            self.assertEqual(caught.exception.code, "APP_SHELL_UNAVAILABLE")
            desktop.Document.Application.ShellExecute.assert_not_called()

    def test_running_process_requires_exact_full_target_path(self):
        import win32process
        native = WindowsApplications()
        shortcut = Shortcut("App", Path("App.lnk"), Path("C:/Apps/App.exe").resolve())
        paths = {100: shortcut.target, 200: Path("C:/Other/App.exe").resolve()}
        with patch.object(win32process, "EnumProcesses", return_value=(100, 200)), patch.object(native, "_process_path", side_effect=paths.get):
            self.assertEqual(native.running_targets(shortcut), {100})


if __name__ == "__main__":
    unittest.main(verbosity=2)
