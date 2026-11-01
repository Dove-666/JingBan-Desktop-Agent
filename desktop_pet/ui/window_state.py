"""Restore utility windows or a transparent pet's visible footprint safely."""

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QSettings, QTimer
from PySide6.QtGui import QGuiApplication

from ..placement import clamp_visible_position, screen_for_rect
from ..windows_work_area import work_area


def bounded_rect(rect, work_areas):
    """Keep the complete rectangle on its best matching monitor, taskbar excluded."""
    areas = [area for area in work_areas if area.isValid()]
    if not areas:
        return QRect(rect)
    area = max(areas, key=lambda candidate: candidate.intersected(rect).width()
               * candidate.intersected(rect).height())
    width, height = min(rect.width(), area.width()), min(rect.height(), area.height())
    x = max(area.left(), min(rect.left(), area.right() - width + 1))
    y = max(area.top(), min(rect.top(), area.bottom() - height + 1))
    return QRect(x, y, width, height)


class WindowPlacement(QObject):
    """Non-secret geometry only; uses an independent QSettings on the GUI thread."""

    def __init__(self, window, config_path, name, *, remember_size=True, fit_to_screen=None,
                 placement_bounds=None):
        super().__init__(window)
        self.window = window
        self.remember_size = remember_size
        self.fit_to_screen = fit_to_screen
        self.placement_bounds = placement_bounds
        self.settings = QSettings(str(config_path), QSettings.Format.IniFormat)
        self.key = f"ui/windows/{name}"
        saved = self.settings.value(self.key)
        if isinstance(saved, QRect) and 80 <= saved.width() <= 32768 and 80 <= saved.height() <= 32768:
            if remember_size:
                window.resize(saved.size().expandedTo(window.minimumSize()))
            window.move(saved.topLeft())
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self.save)
        self._fitting = False
        self.fit()
        window.installEventFilter(self)
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._screen_added)
            app.screenRemoved.connect(self._screen_removed)
            for screen in app.screens():
                self._screen_added(screen)

    def _screen_added(self, screen):
        screen.availableGeometryChanged.connect(self.fit)
        self.fit()

    def _screen_removed(self, _screen):
        QTimer.singleShot(0, self.fit)

    def fit(self, *_args):
        if self._fitting or self.window.isMaximized() or self.window.isMinimized():
            return
        self._fitting = True
        try:
            screens = QGuiApplication.screens()
            if callable(self.placement_bounds) and screens:
                # Transparent pets may legitimately extend their invisible
                # canvas beyond a monitor. Choose and constrain by the stable
                # visible footprint, including when restoring a saved position.
                bounds = QRectF(self.placement_bounds())
                screen = screen_for_rect(bounds.translated(self.window.pos()))
                if callable(self.fit_to_screen):
                    self.fit_to_screen(screen)
                # Fitting may change the sprite scale and therefore its local
                # bounds. Keep the monitor selected before the resize.
                bounds = QRectF(self.placement_bounds())
                self.window.move(clamp_visible_position(
                    self.window.pos(), bounds, work_area(screen),
                ))
                return
            chosen_screen = None
            if callable(self.fit_to_screen) and screens:
                frame = self.window.frameGeometry()
                screen = max(screens, key=lambda candidate:
                             max(0, candidate.availableGeometry().intersected(frame).width())
                             * max(0, candidate.availableGeometry().intersected(frame).height()))
                # The pet owns its scale and cached drawing layout. Reuse its
                # existing screen-fitting hook before bounding the position.
                self.fit_to_screen(screen)
                chosen_screen = screen
            areas = ([chosen_screen.availableGeometry()] if chosen_screen is not None
                     else [screen.availableGeometry() for screen in screens])
            rect = bounded_rect(self.window.frameGeometry(), areas)
            # Native title bars add space outside QWidget's client size.
            frame = self.window.frameGeometry()
            extra_width = max(0, frame.width() - self.window.width())
            extra_height = max(0, frame.height() - self.window.height())
            if self.remember_size:
                width, height = max(80, rect.width() - extra_width), max(80, rect.height() - extra_height)
                minimum = self.window.minimumSize()
                self.window.setMinimumSize(min(minimum.width(), width), min(minimum.height(), height))
                self.window.resize(width, height)
            self.window.move(rect.topLeft())
        finally:
            self._fitting = False

    def save(self):
        if self.window.isMinimized():
            return
        if self.window.isMaximized():
            geometry = self.window.normalGeometry()
            geometry.moveTopLeft(geometry.topLeft() + self.window.pos() - self.window.geometry().topLeft())
        else:
            geometry = QRect(self.window.pos(), self.window.size())
        if geometry.isValid():
            self.settings.setValue(self.key, geometry)
            self.settings.sync()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize) and watched.isVisible():
            self._save_timer.start()
        elif event.type() in (QEvent.Type.Hide, QEvent.Type.Close):
            self._save_timer.stop()
            self.save()
        elif event.type() == QEvent.Type.Show:
            self.fit()
        return False
