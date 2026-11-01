"""Native UI changes use synthetic controllers/artwork, never the live desktop."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.config import PetConfig
from desktop_pet.pet_window import PetWindow
from desktop_pet.tray import DesktopTray
from desktop_pet.ui.debug_panel import DebugPanel
from desktop_pet.ui.settings_page import SettingsPage
from tests.test_debug_ui import FakeChat, FakePet, FakeSettings


class FakeObserver(QObject):
    active_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.active = False


class FakeObservation(QObject):
    paused_changed = Signal(bool)
    settings_changed = Signal(object)

    def __init__(self, enabled=False, allow_remote=False):
        super().__init__()
        self.settings = SimpleNamespace(enabled=enabled, allow_remote=allow_remote)
        self.paused = False
        self.observer = FakeObserver()
        self.saved_changes = []

    def set_paused(self, paused):
        self.paused = paused
        self.paused_changed.emit(paused)

    def set_enabled(self, enabled):
        self.saved_changes.append(enabled)
        self.settings.enabled = enabled
        self.settings_changed.emit(self.settings)


class NativeUiPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.widgets = []
        self.trays = []

    def tearDown(self):
        for tray in self.trays:
            tray.shutdown()
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def api_panel(self):
        self.settings = FakeSettings()
        self.settings.config = replace(self.settings.config, reasoning_effort="high")
        panel = DebugPanel(FakePet(), self.settings, FakeChat())
        self.widgets.append(panel)
        panel.tabs.setCurrentIndex(1)
        panel.show()
        self.app.processEvents()
        return panel

    def tray(self):
        pet = QWidget()
        self.widgets.append(pet)
        session = SimpleNamespace(pet=pet, perception=FakeObservation(True), camera=FakeObservation(),
                                  open_chat_window=Mock(), open_settings=Mock(), open_memory_window=Mock(),
                                  open_camera_settings=Mock(), toggle_pet_visibility=Mock(), shutdown=Mock())
        with patch("desktop_pet.tray.QSystemTrayIcon.isSystemTrayAvailable", return_value=False):
            tray = DesktopTray(session, "")
        self.trays.append(tray)
        return session, tray

    def test_advanced_settings_start_collapsed_and_show_the_saved_value(self):
        panel = self.api_panel()
        self.assertTrue(panel.api_advanced_content.isHidden())
        self.assertFalse(panel.api_advanced_button.isChecked())
        self.assertIn("高", panel.api_advanced_button.text())
        self.assertEqual(panel.reasoning_effort_combo.currentData(), "high")
        panel.api_advanced_button.click()
        self.assertFalse(panel.api_advanced_content.isHidden())
        self.assertEqual(panel.api_advanced_button.accessibleName(), "收起高级模型设置")

    def test_collapsing_keeps_form_drafts_and_saves_the_selected_effort(self):
        panel = self.api_panel()
        panel.key_input.setText("synthetic-new-key")
        panel.model_input.setText("synthetic-model-draft")
        panel.api_advanced_button.click()
        panel.reasoning_effort_combo.setCurrentIndex(panel.reasoning_effort_combo.findData("low"))
        panel.api_advanced_button.click()
        self.assertEqual(panel.key_input.text(), "synthetic-new-key")
        self.assertEqual(panel.model_input.text(), "synthetic-model-draft")
        self.assertEqual(self.settings.config.reasoning_effort, "high")
        panel.save_api_button.click()
        self.assertEqual(self.settings.config.reasoning_effort, "low")
        self.assertEqual(self.settings.config.model, "synthetic-model-draft")
        self.assertEqual(self.settings.key, "synthetic-new-key")

    def test_category_search_is_honest_and_agent_section_key_stays_stable(self):
        page = SettingsPage(None, None, None)
        self.widgets.append(page)
        self.assertEqual(page.search_input.placeholderText(), "搜索分类")
        page.search_input.setText("任务")
        self.assertFalse(page._items["agent"].isHidden())
        page.show_section("agent")
        self.assertEqual(page.section_key, "agent")
        self.assertEqual(page.section_title.text(), "电脑 Agent")

    def test_tray_refresh_and_temporary_pause_never_change_saved_camera_consent(self):
        session, tray = self.tray()
        self.assertIn("屏幕感知：已启用 · 仅本地", tray.icon.toolTip())
        self.assertIn("摄像头：已关闭", tray.icon.toolTip())
        self.assertFalse(tray.camera_pause_action.isEnabled())
        tray.refresh()
        self.assertEqual(session.camera.saved_changes, [])
        tray.camera_action.trigger()
        self.assertEqual(session.camera.saved_changes, [True])
        self.assertIn("准备中", tray.camera_status_action.text())
        session.camera.observer.active = True
        session.camera.observer.active_changed.emit(True)
        self.assertIn("使用中 · 仅本地", tray.camera_status_action.text())
        tray.camera_pause_action.trigger()
        self.assertTrue(session.camera.paused)
        self.assertTrue(session.camera.settings.enabled)
        self.assertIn("摄像头：已暂停", tray.icon.toolTip())
        tray.camera_pause_action.trigger()
        self.assertFalse(session.camera.paused)
        self.assertEqual(session.camera.saved_changes, [True])

    def test_screen_pause_and_saved_shutdown_have_distinct_truthful_states(self):
        session, tray = self.tray()
        tray.pause_action.trigger()
        self.assertTrue(session.perception.paused)
        self.assertIn("屏幕感知：已暂停", tray.icon.toolTip())
        session.perception.settings.enabled = False
        session.perception.settings_changed.emit(session.perception.settings)
        self.assertIn("屏幕感知：已关闭", tray.icon.toolTip())
        tray.pause_action.trigger()
        self.assertFalse(session.perception.paused)
        self.assertFalse(tray.pause_action.isEnabled())
        self.assertFalse(session.camera.settings.enabled)
        self.assertEqual(session.perception.saved_changes, [])

    def test_pet_hover_explains_existing_hits_without_changing_interaction(self):
        with tempfile.TemporaryDirectory(prefix="aura-native-hover-") as directory:
            path = Path(directory) / "synthetic.png"
            image = QImage(80, 120, QImage.Format.Format_ARGB32)
            image.fill(QColor("#658fba"))
            self.assertTrue(image.save(str(path)))
            pet = PetWindow(PetConfig(image_path=path))
            self.widgets.append(pet)
            pet.refresh_timer.stop()
            handled = Mock()
            pet.click_handler = handled
            point = QPointF(pet.sprite_rect.center())
            with patch.object(pet, "character_contains", return_value=True), \
                    patch.object(pet, "_part_at", return_value=None) as part_at:
                event = QMouseEvent(QEvent.Type.MouseMove, point, point,
                                    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
                QApplication.sendEvent(pet, event)
                self.assertIn("单击聊天", pet.toolTip())
                part_at.return_value = "left_ear"
                QApplication.sendEvent(pet, event)
                self.assertIn("摸摸耳朵", pet.toolTip())
                part_at.return_value = "tail"
                QApplication.sendEvent(pet, event)
                self.assertIn("戳戳尾巴", pet.toolTip())
                handled.assert_not_called()
                self.assertEqual(pet.cursor().shape(), Qt.CursorShape.OpenHandCursor)
                pet._press_global = pet.pos()
                pet.setToolTip("")
                QApplication.sendEvent(pet, event)
                self.assertEqual(pet.toolTip(), "")
                pet._press_global = None
            QApplication.sendEvent(pet, QEvent(QEvent.Type.Leave))
            self.assertEqual(pet.toolTip(), "")
            self.assertIn("右键", pet.accessibleDescription())


if __name__ == "__main__":
    unittest.main()
