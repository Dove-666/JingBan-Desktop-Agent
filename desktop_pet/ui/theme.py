"""Application-scoped native colors and reversible, local Qt stylesheets.

Only registered utility widgets receive a stylesheet. In particular, the pet's
transparent window is never covered by an application-wide QWidget background.
The material here is static; theme changes do not add blur/compositor effects.
"""

from __future__ import annotations

import re
import sys
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


THEMES = ("light", "dark")
_PALETTES = {
    "light": {
        "window": "#ffffff", "surface": "#ffffff", "sidebar": "#f6f7f9",
        "raised": "#f8fafc", "text": "#344054", "muted": "#667386",
        "disabled": "#8a96a5", "accent": "#4d82b8", "border": "#e6eaf0",
        "focus": "#87a8cb", "toggle_off": "#c8d0da", "thumb": "#ffffff",
        "selected": "#e6edf5", "danger": "#a12622", "success": "#315b4c",
        "input": "#ffffff", "hover": "#edf2f8",
    },
    "dark": {
        "window": "#10151d", "surface": "#151c26", "sidebar": "#111923",
        "raised": "#1a2431", "text": "#e6edf5", "muted": "#a7b5c8",
        "disabled": "#6f7f94", "accent": "#8cb8e8", "border": "#2c3b4f",
        "focus": "#7399bf", "toggle_off": "#3b4b60", "thumb": "#f2f6fc",
        "selected": "#26384b", "danger": "#f1a3a3", "success": "#8acdae",
        "input": "#151f2c", "hover": "#223144",
    },
}

# Existing screens already have carefully scoped styles. Adapt their exact
# palette rather than replacing selectors, dimensions or widget behavior.
_COLOR_MAP = {}


def _colors(source, target):
    for value in source.split():
        _COLOR_MAP[value] = target


_colors("#ffffff", "#151c26")
_colors("#f6f7f9 #f7f8fa #f5f7fa #f3f5f8 #f5f7fb #f6f8fa", "#111923")
_colors("#f8fafc #f7fafc #f5f8fb #f3f6f9", "#1a2431")
_colors("#ededed #edf0f4 #e4e8ee #e6eaf0 #eceff3 #dfe5ec #dce3ec #dce5ee #e1e6ed #e1e6ec #e1e7ee #e8edf3 #e1e7ee #e6eef6", "#2c3b4f")
_colors("#cbd5e2 #d3dce7 #d5e3f0 #cfdee9 #bed1df #b9c7d8 #bdcfe1 #bac", "#384b62")
_colors("#d0d8e2 #c3cbd7 #aebccd #acbacb #9ca8b8 #c8d0da #ccd7e2 #aebfd2", "#3b4b60")
_colors("#e6edf5 #e6ecf3 #e9eef5 #dcecf4 #dceafa #d5e5f8 #e6f0f6 #e7f1fb #dfedfb #deb", "#26384b")
_colors("#e9edf2 #edf2f8 #eef2f6 #e3ebf4 #e5eaf1 #eaf0f6 #eaf2fa #eaf3f8 #e9f3f9 #edf5fd", "#223144")
_colors("#202020 #202734 #243c55 #263445 #2b4661 #334155 #344054 #283d50", "#e6edf5")
_colors("#43556a #485467 #52637a #526477 #52677a #526e88 #586778 #586b80 #596575 #607489 #60758c #62768c #647d90 #666666 #667386", "#a7b5c8")
_colors("#70859a #718294 #71859b #738196 #8490a1 #8a96a5 #8c98a7", "#7f90a6")
_colors("#183b65 #203e5b #244766 #285d8e #2d567c #315b6d #315b73 #36536d #38566f #416078 #17687f", "#bed8f3")
_colors("#5278a8 #7389a5 #749cc5 #7fa3c8 #83add4 #87a8cb #a8b2bf", "#7399bf")
_colors("#253449 #172334 #344b69", "#315c83")
_colors("#356da7 #4d82b8 #3d70a4 #2b8eaa", "#3d6f9e")
_colors("#fff0ed #fff2ef", "#342225")
_colors("#f0d3ca", "#684044")
_colors("#9b3d30 #9b493e #a12622 #a44a4a #c50f1f #e81123", "#f1a3a3")
_colors("#edf7f2 #e8f5ef", "#172c26")
_colors("#315b4c", "#8acdae")
_colors("#cfe4da", "#315d4b")

# A few source colors serve both dark buttons and text in the light UI. Their
# text form needs a lighter result than their background form in dark mode.
_TEXT_COLOR_MAP = dict(_COLOR_MAP)
for _source in ("#172334", "#253449", "#344b69"):
    _TEXT_COLOR_MAP[_source] = "#d5e3f5"
for _source in ("#356da7", "#4d82b8", "#3d70a4", "#2b8eaa"):
    _TEXT_COLOR_MAP[_source] = "#8cb8e8"

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_DECLARATION = re.compile(r"(?P<property>[\w-]+)\s*:\s*(?P<value>[^;{}]+)")
_BLOCK = re.compile(r"\{(?P<declarations>[^{}]*)\}")
_NAMED = re.compile(r"(?<![\w#./-])(white|black)(?![\w./-])", re.IGNORECASE)
_STYLE_SOURCES = weakref.WeakKeyDictionary()


def theme_manager():
    """Return the live application manager, or None for unthemed test widgets."""
    app = QApplication.instance()
    manager = getattr(app, "_aura_theme_manager", None) if app is not None else None
    return manager if manager is not None and isValid(manager) else None


def current_theme():
    manager = theme_manager()
    return manager.theme if manager is not None else "light"


def color(role, theme=None):
    """A semantic QColor-ready color string for native custom painters."""
    name = current_theme() if theme is None else theme
    if name not in THEMES:
        raise ValueError("Unknown theme")
    return _PALETTES[name][role]


def _dark_value(value, *, text=False):
    def replace_hex(match):
        raw = match.group().lower()
        if text and raw in ("#fff", "#ffffff"):
            return "#f2f6fc"
        if raw == "#fff":
            raw = "#ffffff"
        elif raw in ("#000", "#000000"):
            return "#e6edf5" if text else "#10151d"
        return (_TEXT_COLOR_MAP if text else _COLOR_MAP).get(raw, raw)

    def replace_named(match):
        if match.group().casefold() == "white":
            return "#f2f6fc" if text else "#151c26"
        return "#e6edf5" if text else "#10151d"

    return _NAMED.sub(replace_named, _HEX.sub(replace_hex, value))


def themed_stylesheet(light_css, theme=None):
    """Translate known color values; preserve the original light CSS exactly."""
    name = current_theme() if theme is None else theme
    if name not in THEMES:
        raise ValueError("Unknown theme")
    if name == "light":
        return light_css

    def declaration(match):
        prop = match.group("property").casefold()
        value = _dark_value(match.group("value"), text=prop in ("color", "selection-color"))
        if value == match.group("value"):
            return match.group()
        return match.group("property") + ": " + value

    if "{" not in light_css:
        # Native labels also use small, declaration-only styles, e.g. inline
        # status colors and slider fonts. Their non-color values remain exact.
        return _DECLARATION.sub(declaration, light_css)

    def block(match):
        previous_end = light_css.rfind("}", 0, match.start()) + 1
        selector = light_css[previous_end:match.start()]
        if re.search(r"\[\s*auraTheme\s*=\s*['\"]?dark['\"]?\s*\]", selector):
            return match.group()
        return "{" + _DECLARATION.sub(declaration, match.group("declarations")) + "}"

    # Only declaration bodies are transformed. Matching the full stylesheet
    # would also rewrite ``QPushButton:hover`` and break focus/hover selectors.
    return _BLOCK.sub(block, light_css)


def style_widget(widget, light_css):
    """Apply a local style and retain its light source for live theme changes."""
    _STYLE_SOURCES[widget] = light_css
    widget.setProperty("auraTheme", current_theme())
    widget.setStyleSheet(themed_stylesheet(light_css))
    if widget.isWindow():
        apply_titlebar(widget)
    return widget


def apply_titlebar(widget):
    """Theme standard Windows title bars without enabling native blur effects."""
    if sys.platform != "win32" or not widget.isWindow() or not widget.isVisible():
        return False
    if widget.windowFlags() & Qt.WindowType.FramelessWindowHint:
        return False
    try:
        import ctypes
        enabled = ctypes.c_int(current_theme() == "dark")
        handle = ctypes.c_void_p(int(widget.winId()))
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                handle, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled))
            if result == 0:
                return True
    except (AttributeError, OSError, RuntimeError):
        pass
    return False


class ThemeManager(QObject):
    """One reversible native theme, synchronized through an explicit signal."""

    theme_changed = Signal(str)

    def __init__(self, settings_manager, parent=None):
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("A QApplication is required for native themes")
        super().__init__(parent)
        self.settings_manager = settings_manager
        previous = theme_manager()
        self._light_palette = QPalette(previous._light_palette if previous is not None else app.palette())
        if previous is not None:
            previous.dispose()
        try:
            loader = getattr(settings_manager, "load_theme", None)
            name = loader() if callable(loader) else "light"
        except Exception:
            name = "light"
        self._theme = name if name in THEMES else "light"
        app._aura_theme_manager = self
        app.installEventFilter(self)
        self._apply()

    @property
    def theme(self):
        return self._theme

    def set_theme(self, name, persist=True):
        if name not in THEMES:
            raise ValueError("Unknown theme")
        if name == self._theme:
            return False
        # Persist before changing any palette or local widget. A failed write
        # leaves the effective theme and unsaved forms entirely intact.
        if persist:
            saver = getattr(self.settings_manager, "save_theme", None)
            if not callable(saver):
                raise RuntimeError("Theme preferences cannot be saved")
            saver(name)
        self._theme = name
        self._apply()
        self.theme_changed.emit(name)
        return True

    def _apply(self):
        app = QApplication.instance()
        if app is None:
            return
        if self._theme == "light":
            palette = QPalette(self._light_palette)
        else:
            palette = QPalette(self._light_palette)
            roles = {
                QPalette.ColorRole.Window: "window", QPalette.ColorRole.WindowText: "text",
                QPalette.ColorRole.Base: "surface", QPalette.ColorRole.AlternateBase: "raised",
                QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "raised",
                QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.ToolTipBase: "raised",
                QPalette.ColorRole.ToolTipText: "text", QPalette.ColorRole.Highlight: "selected",
                QPalette.ColorRole.HighlightedText: "text", QPalette.ColorRole.Link: "accent",
                QPalette.ColorRole.PlaceholderText: "muted",
            }
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                for role, token in roles.items():
                    palette.setColor(group, role, QColor(color(token, "dark")))
            for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                         QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
                palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color("disabled", "dark")))
            for role, token in ((QPalette.ColorRole.Window, "window"),
                                (QPalette.ColorRole.Base, "surface"),
                                (QPalette.ColorRole.Button, "raised")):
                palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color(token, "dark")))
        app.setPalette(palette)
        for widget, source in list(_STYLE_SOURCES.items()):
            if isValid(widget):
                widget.setProperty("auraTheme", self._theme)
                widget.setStyleSheet(themed_stylesheet(source, self._theme))
        for window in app.topLevelWidgets():
            apply_titlebar(window)
            window.update()

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.Show and isinstance(watched, QWidget)
                and watched.isWindow()
                and not watched.windowFlags() & Qt.WindowType.FramelessWindowHint):
            # A newly opened standard dialog receives the current theme once
            # it is visible. Never force a native handle during construction.
            apply_titlebar(watched)
        return False

    def dispose(self):
        """Detach a controller cleanly, restoring light/native defaults for tests."""
        app = QApplication.instance()
        if app is not None and getattr(app, "_aura_theme_manager", None) is self:
            self._theme = "light"
            self._apply()
            app.removeEventFilter(self)
            app._aura_theme_manager = None

    shutdown = dispose
    close = dispose
