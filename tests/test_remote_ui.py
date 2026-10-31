"""Pairing controls never open the transport except through explicit user actions."""

import base64
import json
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.remote_panel import INSTALL_COMMAND, RemotePanel


class MemoryClipboard:
    """Keep UI tests away from the user's real system clipboard."""

    def __init__(self):
        self.value = ""

    def text(self):
        return self.value

    def setText(self, value):
        self.value = value

    def clear(self):
        self.value = ""


class FakeRemoteController(QObject):
    status_changed = Signal(dict)
    pairing_changed = Signal(dict)
    devices_changed = Signal(list)

    def __init__(self):
        super().__init__()
        self.enabled = False
        self.status = {"state": "disabled", "dependency_available": True}
        self.pairing = {}
        self.devices = []
        self.enabled_calls = []
        self.cancel_calls = 0
        self.retry_calls = 0
        self.pair_calls = 0
        self.revoked = []

    def publish_status(self, state, **kwargs):
        self.status = {"state": state, "dependency_available": True, **kwargs}
        self.status_changed.emit(self.status)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.enabled_calls.append(enabled)
        if enabled:
            self.publish_status("listening", endpoint_id="test-endpoint", ticket="test-ticket")
        else:
            self.cancel_pairing()
            self.publish_status("disabled")

    def begin_pairing(self):
        self.pair_calls += 1
        self.pairing = {"code": "314159", "expires_at": time.time() + 300,
                        "ticket": "temporary-ticket"}
        payload = {"version": 1, "endpoint_id": "test-endpoint", **self.pairing}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        self.pairing["uri"] = "aura://pair/" + encoded
        self.pairing_changed.emit(self.pairing)
        return self.pairing

    def cancel_pairing(self):
        self.cancel_calls += 1
        self.pairing = {}
        self.pairing_changed.emit({})

    def retry(self):
        self.retry_calls += 1
        self.publish_status("listening", endpoint_id="test-endpoint", ticket="new-ticket")

    def revoke_device(self, peer_id):
        self.revoked.append(peer_id)
        self.devices = [device for device in self.devices if device["id"] != peer_id]
        self.devices_changed.emit(self.devices)


class RemoteUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.clipboard = MemoryClipboard()
        clipboard_patch = patch("desktop_pet.ui.remote_panel.QApplication.clipboard", return_value=self.clipboard)
        clipboard_patch.start()
        self.addCleanup(clipboard_patch.stop)
        self.controller = FakeRemoteController()
        self.panel = RemotePanel(self.controller)
        self.panel.resize(720, 720)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.clipboard.clear()
        self.app.processEvents()

    def enable(self):
        self.panel.enabled_checkbox.click()
        self.app.processEvents()

    def test_opening_page_leaves_transport_off_and_pairing_unavailable(self):
        self.assertFalse(self.panel.enabled_checkbox.isChecked())
        self.assertEqual(self.controller.enabled_calls, [])
        self.assertEqual(self.controller.pair_calls, 0)
        self.assertIn("已关闭", self.panel.status_label.text())
        self.assertFalse(self.panel.pair_button.isEnabled())
        self.assertFalse(self.panel.copy_connection_button.isEnabled())
        self.assertFalse(self.panel.copy_pair_button.isEnabled())
        self.assertTrue(self.panel.install_command.isReadOnly())

    def test_checkbox_explicitly_enables_and_disables_transport(self):
        self.enable()
        self.assertEqual(self.controller.enabled_calls, [True])
        self.assertTrue(self.panel.pair_button.isEnabled())
        self.assertTrue(self.panel.copy_connection_button.isEnabled())
        self.panel.pair_button.click()
        self.panel.enabled_checkbox.click()
        self.assertEqual(self.controller.enabled_calls, [True, False])
        self.assertEqual(self.panel.pair_code.text(), "")
        self.assertFalse(self.panel.copy_pair_button.isEnabled())
        self.assertFalse(self.panel._timer.isActive())

    def test_start_button_enables_then_error_can_be_retried(self):
        self.panel.retry_button.click()
        self.assertEqual(self.controller.enabled_calls, [True])
        self.controller.publish_status("error", message="当前网络无法连接，请稍后重试。")
        self.app.processEvents()
        self.assertTrue(self.panel.error_label.isVisible())
        self.assertIn("当前网络无法连接", self.panel.error_label.text())
        self.assertTrue(self.panel.retry_button.isVisible())
        self.panel.retry_button.click()
        self.assertEqual(self.controller.retry_calls, 1)
        self.assertFalse(self.panel.error_label.isVisible())
        self.assertIn("已开启", self.panel.status_label.text())

    def test_pairing_displays_and_copies_complete_link_then_cancel_clears_it(self):
        self.enable()
        self.panel.pair_button.click()
        self.assertEqual(self.controller.pair_calls, 1)
        self.assertEqual(self.panel.pair_code.text(), self.controller.pairing["uri"])
        self.assertTrue(self.panel.pair_code.text().startswith("aura://pair/"))
        self.assertIn("完整", self.panel.pair_code.accessibleName())
        self.assertIn("05:00", self.panel.countdown_label.text())
        self.assertFalse(self.panel.pair_button.isEnabled())
        self.panel.copy_pair_button.click()
        copied = QApplication.clipboard().text()
        self.assertEqual(copied, self.panel.pair_code.text())
        self.assertEqual(self.panel.copy_pair_button.text(), "复制配对信息")
        encoded = copied.removeprefix("aura://pair/")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(payload["code"], "314159")
        self.assertEqual(payload["ticket"], "temporary-ticket")
        self.assertEqual(payload["endpoint_id"], "test-endpoint")
        self.assertIn("完整配对链接已复制", self.panel.feedback_label.text())
        self.panel.cancel_pair_button.click()
        self.assertEqual(self.controller.cancel_calls, 1)
        self.assertEqual(self.panel.pair_code.text(), "")
        self.assertEqual(QApplication.clipboard().text(), "")
        self.assertFalse(self.panel.copy_pair_button.isEnabled())
        self.assertTrue(self.panel.pair_button.isEnabled())

    def test_pairing_without_complete_uri_never_presents_standalone_code(self):
        self.enable()
        for uri in (None, "314159", "aura://pair?code=314159"):
            self.controller.pairing_changed.emit({"code": "314159", "ticket": "temporary-ticket",
                                                  "expires_at": time.time() + 300, "uri": uri})
            self.assertEqual(self.panel.pair_code.text(), "")
            self.assertFalse(self.panel.copy_pair_button.isEnabled())
            self.assertIn("完整配对链接不可用", self.panel.countdown_label.text())

    def test_expiration_clears_pairing_and_prevents_copy_of_stale_code(self):
        self.enable()
        self.panel.pair_button.click()
        self.panel.copy_pair_button.click()
        expiry = self.controller.pairing["expires_at"]
        with patch("desktop_pet.ui.remote_panel.time.time", return_value=expiry + 1):
            self.panel._copy_pairing()
        self.assertEqual(self.controller.cancel_calls, 1)
        self.assertEqual(self.panel.pair_code.text(), "")
        self.assertEqual(QApplication.clipboard().text(), "")
        self.assertIn("已失效", self.panel.countdown_label.text())
        self.assertFalse(self.panel._timer.isActive())
        self.assertTrue(self.panel.pair_button.isEnabled())

    def test_clearing_pairing_preserves_unrelated_clipboard(self):
        self.enable()
        self.panel.pair_button.click()
        self.panel.copy_pair_button.click()
        QApplication.clipboard().setText("my own note")
        self.controller.cancel_pairing()
        self.assertEqual(QApplication.clipboard().text(), "my own note")
        self.assertEqual(self.panel.pair_code.text(), "")

    def test_list_updates_and_revokes_only_selected_device(self):
        self.assertFalse(self.panel.revoke_button.isEnabled())
        self.controller.devices = [
            {"id": "phone-a", "name": "我的手机", "paired_at": "2026-10-31"},
            {"id": "phone-b", "name": "备用手机", "paired_at": "2026-10-30"},
        ]
        self.controller.devices_changed.emit(self.controller.devices)
        self.assertEqual(self.panel.devices_list.count(), 2)
        self.panel.devices_list.setCurrentRow(1)
        self.panel.revoke_button.click()
        self.assertEqual(self.controller.revoked, ["phone-b"])
        self.assertEqual(self.panel.devices_list.count(), 1)
        self.assertEqual(self.panel.devices_list.item(0).text(), "我的手机")
        self.assertFalse(self.panel.revoke_button.isEnabled())

    def test_missing_optional_dependency_exposes_copyable_install_command(self):
        self.controller.publish_status("disabled", dependency_available=False)
        self.app.processEvents()
        self.assertTrue(self.panel.dependency_group.isVisible())
        self.assertIn("可选通信组件未安装", self.panel.dependency_group.title())
        self.assertEqual(self.panel.install_command.text(), INSTALL_COMMAND)
        self.panel.copy_install_button.click()
        self.assertEqual(QApplication.clipboard().text(), INSTALL_COMMAND)
        self.assertFalse(self.panel.retry_button.isEnabled())
        self.assertEqual(self.controller.enabled_calls, [])

    def test_error_status_immediately_clears_pairing_and_plain_text_cannot_render_html(self):
        self.enable()
        self.panel.pair_button.click()
        self.controller.publish_status("error", message="<b>网络中断</b>")
        self.assertEqual(self.panel.pair_code.text(), "")
        self.assertEqual(self.panel.error_label.text(), "<b>网络中断</b>")
        self.assertFalse(self.panel.copy_pair_button.isEnabled())

    def test_failed_revocation_remains_visible_while_connection_is_healthy(self):
        self.enable()
        self.controller.devices = [{"id": "phone-a", "name": "我的手机", "paired_at": "2026-10-31"}]
        self.controller.devices_changed.emit(self.controller.devices)
        self.panel.devices_list.setCurrentRow(0)

        def fail_revocation(peer_id):
            self.controller.publish_status("listening", endpoint_id="test-endpoint", ticket="test-ticket",
                                           message="无法保存远程连接设置，请检查本地目录权限。")
            return False

        with patch.object(self.controller, "revoke_device", side_effect=fail_revocation):
            self.panel.revoke_button.click()
        self.app.processEvents()
        self.assertTrue(self.panel.error_label.isVisible())
        self.assertIn("无法保存", self.panel.error_label.text())
        self.assertIn("未能撤销", self.panel.feedback_label.text())
        self.assertEqual(self.panel.devices_list.count(), 1)
        self.assertIn("已开启", self.panel.status_label.text())

    def test_copy_connection_excludes_temporary_pairing_secret(self):
        self.enable()
        self.panel.pair_button.click()
        self.panel.copy_connection_button.click()
        payload = json.loads(QApplication.clipboard().text())
        self.assertEqual(payload, {"endpoint_id": "test-endpoint", "ticket": "test-ticket"})
        self.assertNotIn("code", payload)

    def test_invalid_and_ambiguous_expiry_are_never_presented_as_usable(self):
        self.enable()
        for expiry in (None, "not-a-date", "2026-10-31T14:00:00", float("inf")):
            self.controller.pairing_changed.emit({"code": "expired", "expires_at": expiry})
            self.assertEqual(self.panel.pair_code.text(), "")
            self.assertFalse(self.panel.copy_pair_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
