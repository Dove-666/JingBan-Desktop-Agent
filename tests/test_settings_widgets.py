"""Native interaction tests for the shared settings controls."""

import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from desktop_pet.ui.settings_widgets import CollapsibleSection, SettingsToggle


class SettingsWidgetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            if widget.objectName() == "settingsWidgetTest":
                widget.close()
                widget.deleteLater()
        self.app.processEvents()

    def toggle(self):
        widget = SettingsToggle("开机自动启动")
        widget.setObjectName("settingsWidgetTest")
        widget.resize(280, 40)
        widget.show()
        self.app.processEvents()
        return widget

    def test_whole_row_and_keyboard_toggle_native_checked_state(self):
        widget = self.toggle()
        events = QSignalSpy(widget.toggled)
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(10, 20))
        self.assertTrue(widget.isChecked())
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_Space)
        self.assertFalse(widget.isChecked())
        self.assertEqual(events.count(), 2)
        interface = QAccessible.queryAccessibleInterface(widget)
        self.assertEqual(interface.role(), QAccessible.Role.CheckBox)
        self.assertEqual(interface.text(QAccessible.Text.Name), "开机自动启动")

    def test_disabled_row_does_not_change_and_remains_renderable(self):
        widget = self.toggle()
        widget.setChecked(True)
        widget.setEnabled(False)
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(264, 20))
        QTest.keyClick(widget, Qt.Key.Key_Space)
        self.assertTrue(widget.isChecked())
        self.assertFalse(widget.grab().isNull())

    def test_rtl_switch_can_be_clicked_on_the_leading_edge(self):
        widget = self.toggle()
        widget.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(18, 20))
        self.assertTrue(widget.isChecked())
        self.assertFalse(widget.grab().isNull())

    def test_disclosure_keyboard_preserves_unsaved_content_and_signals_once(self):
        content = QWidget()
        layout = QVBoxLayout(content)
        field = QLineEdit()
        layout.addWidget(field)
        widget = CollapsibleSection("高级设置", content=content)
        widget.setObjectName("settingsWidgetTest")
        widget.resize(320, 160)
        widget.show()
        self.app.processEvents()
        self.assertFalse(widget.isExpanded())
        self.assertTrue(content.isHidden())
        events = QSignalSpy(widget.expandedChanged)
        widget.button.setFocus()
        QTest.keyClick(widget.button, Qt.Key.Key_Space)
        self.assertTrue(widget.isExpanded())
        self.assertFalse(content.isHidden())
        field.setText("保留未保存的设置")
        widget.setExpanded(False)
        widget.setExpanded(False)
        widget.setExpanded(True)
        self.assertEqual(field.text(), "保留未保存的设置")
        self.assertEqual(events.count(), 3)
        widget.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        widget.setExpanded(False)
        self.assertEqual(widget.button.arrowType(), Qt.ArrowType.LeftArrow)


if __name__ == "__main__":
    unittest.main()
