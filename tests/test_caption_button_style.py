"""Caption affordances use the shared neutral palette in both themes."""
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.ui.theme import ThemeManager, color, theme_manager
from desktop_pet.ui.window_chrome import WindowChrome, _CaptionButton


class CaptionButtonStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        previous = theme_manager()
        if previous is not None:
            previous.dispose()
        self.palette = QPalette(self.app.palette())
        settings = Mock()
        settings.load_theme.return_value = "light"
        self.manager = ThemeManager(settings)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.manager.dispose()
        self.manager.deleteLater()
        self.app.setPalette(self.palette)
        self.app.processEvents()

    def button(self):
        button = _CaptionButton("close", "testClose", "关闭窗口")
        button.show()
        self.widgets.append(button)
        self.app.processEvents()
        return button

    def test_hover_and_press_use_inset_rounded_neutral_surfaces(self):
        button = self.button()
        for theme in ("light", "dark"):
            self.manager.set_theme(theme, persist=False)
            for pressed in (False, True):
                with self.subTest(theme=theme, pressed=pressed), \
                        patch.object(button, "underMouse", return_value=True):
                    button.setDown(pressed)
                    image = button.grab().toImage()
                    self.assertEqual(image.pixelColor(8, 18), QColor(color("selected" if pressed else "hover")))
                    self.assertEqual(image.pixelColor(1, 18), QColor(color("window")))
                    self.assertEqual(image.pixelColor(4, 4), QColor(color("window")))
                    self.assertFalse(any(
                        image.pixelColor(x, y).red() > image.pixelColor(x, y).green() * 1.5
                        and image.pixelColor(x, y).red() > 150
                        for y in range(image.height()) for x in range(image.width())
                    ))
        button.setDown(False)

    def test_keyboard_focus_has_a_visible_neutral_surface_and_outline(self):
        button = self.button()
        with patch.object(button, "underMouse", return_value=False), \
                patch.object(button, "hasFocus", return_value=True):
            button._keyboard_focus = False
            normal = button.grab().toImage()
            button._keyboard_focus = True
            focused = button.grab().toImage()
        self.assertNotEqual(normal, focused)
        self.assertEqual(focused.pixelColor(8, 18), QColor(color("hover")))
        self.assertNotEqual(focused.pixelColor(4, 18), normal.pixelColor(4, 18))

    def test_close_keeps_keyboard_activation_and_accessible_name(self):
        button = self.button()
        clicked = []
        button.clicked.connect(lambda: clicked.append(True))
        self.assertEqual(button.accessibleName(), "关闭窗口")
        self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(clicked, [True])

    def test_caption_clicks_still_minimize_maximize_restore_and_close(self):
        window = QWidget()
        window.resize(400, 240)
        self.widgets.append(window)
        chrome = WindowChrome(window)
        window.show()
        self.app.processEvents()
        chrome.minimize_button.click()
        self.assertTrue(window.isMinimized())
        window.showNormal()
        chrome.maximize_button.click()
        self.assertTrue(window.isMaximized())
        chrome.maximize_button.click()
        self.assertFalse(window.isMaximized())
        chrome.close_button.click()
        self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
