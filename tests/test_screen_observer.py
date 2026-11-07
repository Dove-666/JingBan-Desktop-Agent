"""Foreground observation tests using synthetic pixels and injected metadata only."""

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

from desktop_pet.perception.screen_observer import ForegroundInfo, Observation, ScreenObserver, windows_foreground
from desktop_pet.perception.settings import PerceptionSettings


def safe_info(**overrides):
    fields = dict(hwnd=100, pid=200, process_name="editor.exe", title="Writing a document",
                  idle_seconds=2, available=True)
    fields.update(overrides)
    return ForegroundInfo(**fields)


def synthetic_image(width=1600, height=900):
    result = QImage(width, height, QImage.Format.Format_RGB32)
    result.fill(QColor(40, 70, 90))
    return result


class ScreenObserverTests(unittest.TestCase):
    def setUp(self):
        self.settings = PerceptionSettings(enabled=True, allow_remote=False)
        self.metadata = Mock(return_value=safe_info())
        self.capture = Mock(return_value=synthetic_image())
        self.observer = ScreenObserver(self.metadata, self.capture, own_pid=999)

    def assert_blocked_before_capture(self, info, expected_text):
        self.metadata.return_value = info
        observed = self.observer.observe(self.settings)
        self.assertIsNone(observed.image)
        self.assertIn(expected_text, observed.blocked_reason)
        self.capture.assert_not_called()
        return observed

    def test_explicitly_disabled_reads_no_metadata_and_captures_no_pixels(self):
        result = self.observer.observe(PerceptionSettings(enabled=False, allow_remote=False))
        self.assertIsNone(result.image)
        self.assertIn("关闭", result.blocked_reason)
        self.metadata.assert_not_called()
        self.capture.assert_not_called()

    def test_native_expected_window_guard_does_not_read_other_window_title(self):
        user, kernel = Mock(), Mock()
        user.GetForegroundWindow.return_value = 100
        user.IsWindowVisible.return_value = True
        user.IsIconic.return_value = False
        with patch("desktop_pet.perception.screen_observer.sys.platform", "win32"), patch(
                "desktop_pet.perception.screen_observer.ctypes.WinDLL", side_effect=[user, kernel]):
            self.assertEqual(windows_foreground(expected_hwnd=101), ForegroundInfo())
        user.GetWindowTextW.assert_not_called()
        kernel.OpenProcess.assert_not_called()

    def test_native_expected_pid_guard_does_not_read_reused_window_title(self):
        user, kernel = Mock(), Mock()
        user.GetForegroundWindow.return_value = 100
        user.IsWindowVisible.return_value = True
        user.IsIconic.return_value = False
        user.GetWindowThreadProcessId.side_effect = lambda hwnd, output: setattr(output._obj, "value", 200)
        with patch("desktop_pet.perception.screen_observer.sys.platform", "win32"), patch(
                "desktop_pet.perception.screen_observer.ctypes.WinDLL", side_effect=[user, kernel]):
            self.assertEqual(windows_foreground(expected_hwnd=100, expected_pid=201), ForegroundInfo())
        user.GetWindowTextW.assert_not_called()
        kernel.OpenProcess.assert_not_called()

    def test_local_observation_does_not_require_remote_upload_permission(self):
        result = self.observer.observe(self.settings)
        self.assertFalse(self.settings.allow_remote)
        self.assertEqual(result.blocked_reason, "")
        self.capture.assert_called_once_with(safe_info())
        self.assertEqual(self.metadata.call_count, 2)

    def test_black_capture_is_not_a_valid_observation(self):
        image = synthetic_image()
        image.fill(QColor("black"))
        self.capture.return_value = image
        result = self.observer.observe(self.settings)
        self.assertIsNone(result.image)
        self.assertEqual(result.blocked_code, "capture_unavailable")
        self.assertIn("未调用模型", result.blocked_reason)

    def test_capture_is_scaled_and_preserves_aspect_ratio(self):
        result = self.observer.observe(self.settings)
        self.assertEqual((result.image.width(), result.image.height()), (640, 360))
        self.capture.return_value = synthetic_image(900, 1600)
        result = self.observer.observe(self.settings)
        self.assertEqual((result.image.width(), result.image.height()), (360, 640))

    def test_own_windows_are_never_captured(self):
        result = self.assert_blocked_before_capture(safe_info(pid=999), "自身")
        self.assertEqual(result.blocked_code, "own_window")

    def test_other_aura_instance_windows_are_blocked_before_capture(self):
        titles = ("AURA · 鲸鱼娘桌宠", "鲸鱼娘 · 调试面板 · 2026.10.30-r7",
                  "和鲸鱼娘聊天", "设置 · 鲸鱼娘", "鲸鱼娘 · 长期记忆",
                  "鲸鱼娘 · 调试面板 · 2026.10.30-r7 - 鲸鱼娘桌宠",
                  "确认删除会话 - 鲸鱼娘桌宠")
        for host in ("python.exe", "PythonW.EXE", "AURA-Desktop-Agent.exe"):
            for title in titles:
                with self.subTest(host=host, title=title):
                    result = self.assert_blocked_before_capture(
                        safe_info(pid=12345, process_name=host, title=title), "自身")
                    self.assertEqual(result.blocked_code, "own_window")
                    self.assertEqual(self.observer.current_blocked_code(self.settings), "own_window")

    def test_renamed_windows_are_blocked_for_python_and_packaged_hosts(self):
        titles = ("鲸伴", "鲸伴 · 调试面板 · 2026.11.07-r26", "设置 · 鲸伴",
                  "鲸伴 · 长期记忆", "和鲸鱼娘聊天 - 鲸伴", "和鲸鱼娘下象棋 - 鲸伴",
                  "和鲸鱼娘下五子棋 - 鲸伴", "确认删除会话 - 鲸伴")
        for host in ("python.exe", "PythonW.EXE", "Jingban.exe", "鲸伴.exe"):
            for title in titles:
                with self.subTest(host=host, title=title):
                    result = self.assert_blocked_before_capture(
                        safe_info(pid=12345, process_name=host, title=title), "鲸伴自身")
                    self.assertEqual(result.blocked_code, "own_window")

    def test_ordinary_python_windows_and_browser_project_pages_are_not_excluded(self):
        for host, title in (("python.exe", "AURA Python environment diagnostics"),
                            ("pythonw.exe", "独立图像编辑器"),
                            ("msedge.exe", "AURA-Desktop-Agent - GitHub"),
                            ("msedge.exe", "鲸伴 - 项目介绍"),
                            ("python.exe", "鲸伴图像测试工具")):
            with self.subTest(host=host):
                self.metadata.return_value = safe_info(process_name=host, title=title)
                result = self.observer.observe(self.settings)
                self.assertEqual(result.blocked_code, "")
                self.assertIsNotNone(result.image)

    def test_switching_to_another_aura_instance_discards_inflight_pixels(self):
        self.metadata.side_effect = [safe_info(), safe_info(
            pid=54321, process_name="pythonw.exe", title="和鲸鱼娘聊天")]
        result = self.observer.observe(self.settings)
        self.assertIsNone(result.image)
        self.assertEqual(result.blocked_code, "own_window")

    def test_other_aura_instance_is_not_valid_for_a_delayed_response(self):
        observation = Observation(safe_info(), synthetic_image(100, 100))
        self.metadata.return_value = safe_info(pid=54321, process_name="pythonw.exe", title="鲸鱼娘 · 长期记忆")
        self.assertFalse(self.observer.is_current(observation, self.settings))
        self.capture.assert_not_called()

    def test_default_sensitive_chinese_and_english_titles_are_blocked_without_echo(self):
        for title in ("输入密码 - SYNTHETIC-PRIVATE", "支付确认 - SYNTHETIC-PRIVATE",
                      "PaSsWoRd reset - SYNTHETIC-PRIVATE", "Incognito - SYNTHETIC-PRIVATE"):
            with self.subTest(case=title.split(" - ")[0]):
                result = self.assert_blocked_before_capture(safe_info(title=title), "隐私")
                self.assertNotIn("SYNTHETIC-PRIVATE", result.blocked_reason)

    def test_default_excluded_apps_are_case_insensitive(self):
        self.assert_blocked_before_capture(safe_info(process_name="KeePassXC.EXE"), "排除")

    def test_custom_privacy_keywords_and_excluded_apps_apply_before_capture(self):
        self.settings = replace(self.settings, privacy_keywords=("Confidential",), excluded_apps=("custom.exe",))
        self.assert_blocked_before_capture(safe_info(title="CONFIDENTIAL design"), "隐私")
        self.assert_blocked_before_capture(safe_info(process_name="CUSTOM.EXE"), "排除")

    def test_long_idle_at_exact_boundary_is_paused(self):
        self.assert_blocked_before_capture(safe_info(idle_seconds=600), "无输入")

    def test_invalid_idle_information_fails_closed_before_capture(self):
        for idle in (float("nan"), float("inf"), -1, True, "unknown"):
            with self.subTest(idle=idle):
                self.assert_blocked_before_capture(safe_info(idle_seconds=idle), "无法确认用户输入")

    def test_unknown_or_incomplete_windows_are_not_captured(self):
        for info in (ForegroundInfo(), safe_info(available=False), safe_info(hwnd=0),
                     safe_info(process_name=""), safe_info(title="   ")):
            with self.subTest(info=info):
                self.assert_blocked_before_capture(info, "无法确认")

    def test_metadata_exception_is_safely_blocked_without_exposing_exception(self):
        self.metadata.side_effect = RuntimeError("SYNTHETIC-PRIVATE-METADATA")
        observed = self.observer.observe(self.settings)
        self.assertIsNone(observed.image)
        self.assertNotIn("SYNTHETIC-PRIVATE", observed.blocked_reason)
        self.capture.assert_not_called()

    def test_metadata_provider_malformed_values_fail_closed(self):
        for value in (None, {}, safe_info(title=None), safe_info(process_name=[])):
            with self.subTest(value=value):
                self.assert_blocked_before_capture(value, "无法确认")

    def test_capture_exception_is_safely_reported_without_its_details(self):
        self.capture.side_effect = RuntimeError("SYNTHETIC-PRIVATE-PIXELS")
        result = self.observer.observe(self.settings)
        self.assertIsNone(result.image)
        self.assertIn("安全跳过", result.blocked_reason)
        self.assertNotIn("SYNTHETIC-PRIVATE", result.blocked_reason)

    def test_null_or_nonimage_capture_is_discarded(self):
        for invalid in (QImage(), None, "not an image"):
            self.capture.return_value = invalid
            result = self.observer.observe(self.settings)
            self.assertIsNone(result.image)
            self.assertIn("截图不可用", result.blocked_reason)

    def test_window_or_title_change_during_capture_discards_pixels(self):
        for after in (safe_info(hwnd=101), safe_info(pid=201), safe_info(title="Another document")):
            with self.subTest(after=after):
                self.metadata.side_effect = [safe_info(), after]
                result = self.observer.observe(self.settings)
                self.assertIsNone(result.image)
                self.assertIn("丢弃", result.blocked_reason)

    def test_second_privacy_check_discards_now_private_or_unavailable_window(self):
        for after in (safe_info(process_name="bitwarden.exe"), safe_info(available=False),
                      safe_info(idle_seconds=1000)):
            with self.subTest(after=after):
                self.metadata.side_effect = [safe_info(), after]
                result = self.observer.observe(self.settings)
                self.assertIsNone(result.image)
                self.assertIn("丢弃", result.blocked_reason)

    def test_current_safety_checks_metadata_without_capturing(self):
        self.assertEqual(self.observer.current_safety(self.settings), "")
        self.metadata.return_value = safe_info(title="银行登录")
        self.assertIn("隐私", self.observer.current_safety(self.settings))
        self.capture.assert_not_called()

    def test_is_current_revalidates_window_identity_and_privacy_without_capturing(self):
        observation = Observation(safe_info(), synthetic_image(100, 100))
        self.assertTrue(self.observer.is_current(observation, self.settings))
        for info in (safe_info(title="Different document"), safe_info(hwnd=101), safe_info(pid=201),
                     safe_info(process_name="bitwarden.exe"), safe_info(available=False)):
            with self.subTest(info=info):
                self.metadata.return_value = info
                self.assertFalse(self.observer.is_current(observation, self.settings))
        self.capture.assert_not_called()

    def test_explicit_request_allows_valid_local_idle_but_automatic_stays_paused(self):
        for idle in (self.settings.idle_pause_seconds, 1000000.0):
            with self.subTest(idle=idle):
                self.metadata.return_value = safe_info(idle_seconds=idle)
                self.capture.reset_mock()
                automatic = self.observer.observe(self.settings)
                self.assertIsNone(automatic.image)
                self.capture.assert_not_called()
                explicit = self.observer.observe_explicit(self.settings)
                self.assertEqual(explicit.blocked_code, "")
                self.assertEqual((explicit.image.width(), explicit.image.height()), (640, 360))
                self.capture.assert_called_once()
                self.assertTrue(self.observer.is_current_explicit(explicit, self.settings))
                self.assertFalse(self.observer.is_current(explicit, self.settings))

    def test_disabled_explicit_capture_and_current_check_read_no_metadata(self):
        disabled = replace(self.settings, enabled=False)
        result = self.observer.observe_explicit(disabled)
        self.assertEqual(result.blocked_code, "disabled")
        self.assertIsNone(result.image)
        self.assertFalse(self.observer.is_current_explicit(Observation(safe_info(), synthetic_image()), disabled))
        self.metadata.assert_not_called()
        self.capture.assert_not_called()

    def test_explicit_request_never_bypasses_privacy_own_window_or_invalid_idle(self):
        blocked = [safe_info(pid=999), safe_info(pid=12345, process_name="python.exe",
                   title="AURA \u00b7 \u9cb8\u9c7c\u5a18\u684c\u5ba0"),
                   safe_info(process_name="KeePassXC.EXE"), safe_info(title="Password SYNTHETIC-PRIVATE"),
                   ForegroundInfo(), safe_info(available=False), safe_info(hwnd=0), safe_info(title=" ")]
        blocked += [safe_info(idle_seconds=value) for value in (float("nan"), float("inf"), -1, True, None, "unknown")]
        for info in blocked:
            with self.subTest(info=info):
                self.capture.reset_mock()
                self.metadata.return_value = info
                result = self.observer.observe_explicit(self.settings)
                self.assertIsNone(result.image)
                self.assertIn(result.blocked_code, {"own_window", "privacy"})
                self.assertNotIn("SYNTHETIC-PRIVATE", result.blocked_reason)
                self.capture.assert_not_called()

    def test_explicit_capture_rechecks_window_and_privacy_after_pixels(self):
        before = safe_info(idle_seconds=1000000)
        for after in (safe_info(hwnd=101, idle_seconds=1000000), safe_info(title="Another document", idle_seconds=1000000),
                      safe_info(pid=999), safe_info(process_name="bitwarden.exe"), safe_info(title="Password"),
                      safe_info(available=False), safe_info(idle_seconds=float("nan"))):
            with self.subTest(after=after):
                self.metadata.side_effect = [before, after]
                result = self.observer.observe_explicit(self.settings)
                self.assertIsNone(result.image)
                self.assertIn(result.blocked_code, {"own_window", "context_changed"})
        self.metadata.side_effect = [before, safe_info(idle_seconds=2000000)]
        self.assertIsNotNone(self.observer.observe_explicit(self.settings).image)

    def test_explicit_capture_still_rejects_black_null_and_nonimage_pixels(self):
        black = synthetic_image(); black.fill(QColor("black"))
        self.metadata.return_value = safe_info(idle_seconds=1000000)
        for image in (black, QImage(), None, "not an image"):
            with self.subTest(image_type=type(image).__name__):
                self.capture.return_value = image
                observed = self.observer.observe_explicit(self.settings)
                self.assertIsNone(observed.image)
                self.assertEqual(observed.blocked_code, "capture_unavailable")

    def test_explicit_current_validation_keeps_all_identity_and_privacy_guards(self):
        observation = Observation(safe_info(idle_seconds=1000000), synthetic_image(100, 100))
        self.metadata.return_value = observation.info
        self.assertTrue(self.observer.is_current_explicit(observation, self.settings))
        for info in (safe_info(hwnd=101), safe_info(pid=201), safe_info(title="Different document"),
                     safe_info(pid=999), safe_info(process_name="bitwarden.exe"), safe_info(title="Password"),
                     safe_info(available=False), safe_info(idle_seconds=float("inf")), safe_info(idle_seconds=-1)):
            with self.subTest(info=info):
                self.metadata.return_value = info
                self.assertFalse(self.observer.is_current_explicit(observation, self.settings))
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
