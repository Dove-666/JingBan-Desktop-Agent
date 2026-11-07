"""Theme state and local QSS are reversible without production preferences."""

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QWidget

from desktop_pet.ui.theme import (
    ThemeManager, apply_titlebar, color, current_theme, style_widget, theme_manager, themed_stylesheet,
)


class ThemeCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        previous = theme_manager()
        if previous is not None:
            previous.dispose()
        self.original_palette = QPalette(self.app.palette())
        self.widgets = []
        self.manager = None

    def tearDown(self):
        if self.manager is not None:
            self.manager.dispose()
        for widget in self.widgets:
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.setPalette(self.original_palette)

    def manager_for(self, name="dark"):
        settings = Mock()
        settings.load_theme.return_value = name
        self.manager = ThemeManager(settings)
        return settings, self.manager

    def test_no_manager_keeps_light_sources_and_does_not_style_the_application(self):
        self.assertEqual(current_theme(), "light")
        source = "QWidget { background: white; color: #344054; }"
        widget = QWidget()
        self.widgets.append(widget)
        style_widget(widget, source)
        self.assertEqual(widget.styleSheet(), source)
        self.assertEqual(self.app.styleSheet(), "")

    def test_missing_failed_or_invalid_preferences_start_light_without_saving(self):
        settings = Mock()
        settings.load_theme.side_effect = OSError("synthetic settings unavailable")
        self.manager = ThemeManager(settings)
        self.assertEqual(current_theme(), "light")
        settings.save_theme.assert_not_called()
        self.manager.dispose()
        for invalid in (None, "sepia", 17):
            with self.subTest(invalid=invalid):
                settings, self.manager = self.manager_for(invalid)
                self.assertEqual(current_theme(), "light")
                settings.save_theme.assert_not_called()
                self.manager.dispose()
        self.manager = ThemeManager(object())
        self.assertEqual(current_theme(), "light")

    def test_theme_switch_saves_then_notifies_and_restores_exact_light_styles(self):
        source = "QWidget { background: white; color: #344054; border: 1px solid #e6eaf0; }"
        widget = QWidget()
        self.widgets.append(widget)
        style_widget(widget, source)
        settings, manager = self.manager_for()
        spy = QSignalSpy(manager.theme_changed)
        self.assertEqual(current_theme(), "dark")
        self.assertIn("#151c26", widget.styleSheet())
        self.assertIn("#e6edf5", widget.styleSheet())
        self.assertTrue(manager.set_theme("light"))
        settings.save_theme.assert_called_once_with("light")
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0), ["light"])
        self.assertEqual(widget.styleSheet(), source)
        self.assertEqual(self.app.palette(), self.original_palette)
        self.assertFalse(manager.set_theme("light"))
        self.assertEqual(spy.count(), 1)

    def test_failed_save_does_not_change_widgets_palette_or_signal(self):
        settings, manager = self.manager_for()
        settings.save_theme.side_effect = OSError("synthetic write denied")
        palette = QPalette(self.app.palette())
        spy = QSignalSpy(manager.theme_changed)
        with self.assertRaises(OSError):
            manager.set_theme("light")
        self.assertEqual(current_theme(), "dark")
        self.assertEqual(self.app.palette(), palette)
        self.assertEqual(spy.count(), 0)

    def test_preview_change_does_not_save_and_invalid_names_are_rejected(self):
        settings, manager = self.manager_for("light")
        self.assertTrue(manager.set_theme("dark", persist=False))
        settings.save_theme.assert_not_called()
        self.assertEqual(self.app.palette().color(QPalette.ColorRole.Window), QColor(color("window")))
        with self.assertRaises(ValueError):
            manager.set_theme("synthetic-theme")
        self.assertEqual(current_theme(), "dark")

    def test_semantic_error_success_and_white_button_text_remain_readable(self):
        source = ("QLabel#error { background: #fff0ed; color: #9b3d30; } "
                  "QLabel#ok { background: #edf7f2; color: #315b4c; } "
                  "QPushButton { background: #253449; color: white; }")
        result = themed_stylesheet(source, "dark")
        self.assertIn("#342225", result)
        self.assertIn("#f1a3a3", result)
        self.assertIn("#172c26", result)
        self.assertIn("#8acdae", result)
        self.assertIn("color: #f2f6fc", result)
        self.assertEqual(themed_stylesheet(source, "light"), source)

    def test_pseudo_selectors_gradients_and_asset_urls_are_not_rewritten(self):
        source = ('QListWidget#nav::item:selected:focus { color: #344054; }\n'
                  'QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,'
                  'stop:0 #ffffff,stop:1 #f8fafc); image: url("C:/assets/white.svg"); }')
        result = themed_stylesheet(source, "dark")
        self.assertIn("QListWidget#nav::item:selected:focus {", result)
        self.assertIn("QPushButton:hover {", result)
        self.assertIn('url("C:/assets/white.svg")', result)
        self.assertIn("stop:0 #151c26,stop:1 #1a2431", result)

    def test_inline_colors_and_font_styles_are_supported_without_changing_selectors(self):
        source = "color: #2b4661; font-size: 13px; font-weight: 600;"
        result = themed_stylesheet(source, "dark")
        self.assertIn("color: #e6edf5;", result)
        self.assertIn("font-size: 13px; font-weight: 600;", result)
        self.assertEqual(themed_stylesheet(source, "light"), source)
        self.assertEqual(themed_stylesheet("QPushButton:hover", "dark"), "QPushButton:hover")
        self.assertIn("#d5e3f5", themed_stylesheet("color: #344b69;", "dark"))

    def test_explicit_dark_material_blocks_remain_exact_and_theme_property_updates(self):
        widget = QWidget()
        self.widgets.append(widget)
        source = ('QWidget { color: #344054; } '
                  'QWidget[auraTheme="dark"] { color: #e6edf5; background: #10151d; }')
        style_widget(widget, source)
        self.assertEqual(widget.property("auraTheme"), "light")
        _settings, manager = self.manager_for()
        self.assertEqual(widget.property("auraTheme"), "dark")
        self.assertIn('QWidget[auraTheme="dark"] { color: #e6edf5; background: #10151d; }', widget.styleSheet())
        manager.set_theme("light")
        self.assertEqual(widget.property("auraTheme"), "light")
        self.assertEqual(widget.styleSheet(), source)

    def test_hidden_windows_do_not_acquire_a_native_handle_for_titlebar_styling(self):
        widget = QWidget()
        self.widgets.append(widget)
        with patch.object(widget, "winId", side_effect=AssertionError("Unrequested native handle")):
            self.assertFalse(apply_titlebar(widget))
            style_widget(widget, "color: #344054;")

    def test_new_standard_windows_receive_the_theme_on_show_and_frameless_pet_is_skipped(self):
        _settings, _manager = self.manager_for()
        window, pet = QWidget(), QWidget()
        pet.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.widgets.extend((window, pet))
        with patch("desktop_pet.ui.theme.apply_titlebar") as titlebar:
            window.show()
            pet.show()
            self.app.processEvents()
            self.assertIn(window, [call.args[0] for call in titlebar.call_args_list])
            self.assertNotIn(pet, [call.args[0] for call in titlebar.call_args_list])

    def test_deleting_registered_widgets_before_theme_change_is_safe(self):
        widget = QWidget()
        style_widget(widget, "QWidget { background: #ffffff; }")
        widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _settings, manager = self.manager_for()
        self.assertTrue(manager.set_theme("light"))

    def test_replacing_a_local_stylesheet_keeps_its_latest_source(self):
        widget = QWidget()
        self.widgets.append(widget)
        _settings, manager = self.manager_for()
        style_widget(widget, "QWidget { background: #ffffff; }")
        source = "QWidget { color: #43556a; padding: 11px; }"
        style_widget(widget, source)
        manager.set_theme("light")
        self.assertEqual(widget.styleSheet(), source)
        manager.set_theme("dark")
        self.assertIn("padding: 11px", widget.styleSheet())
        self.assertNotIn("background:", widget.styleSheet())

    def test_dispose_detaches_manager_and_restores_native_palette(self):
        _settings, manager = self.manager_for()
        manager.dispose()
        self.assertIsNone(theme_manager())
        self.assertEqual(current_theme(), "light")
        self.assertEqual(self.app.palette(), self.original_palette)


if __name__ == "__main__":
    unittest.main()
