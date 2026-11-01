"""Verify the installed expression art through the real, isolated Qt controls.

Run with the project's .venv Python. No production settings, credentials,
desktop screenshots, or network connections are used.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
BATCH_DIRECTORY = SCRIPT_DIRECTORY.parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QEvent, QPoint, QThreadPool, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.expression_manager import EXPRESSIONS
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pixel_hash(image: QImage) -> str:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return hashlib.sha256(bytes(rgba.constBits())).hexdigest()


def capture_widget(widget, destination: Path) -> None:
    canvas = QImage(widget.size(), QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        widget.render(painter, QPoint())
    finally:
        painter.end()
    if not canvas.save(str(destination), "PNG"):
        raise RuntimeError(f"Could not save widget render: {destination.name}")


def main() -> None:
    output = BATCH_DIRECTORY / "预览" / "程序渲染"
    output.mkdir(parents=True, exist_ok=True)
    report_path = SCRIPT_DIRECTORY / "menu_verification.json"
    report = {
        "platform": "offscreen",
        "production_ui_touched": False,
        "desktop_captured": False,
        "credentials": "test-only in-memory backend",
        "settings": "temporary INI",
        "network": "socket connect guarded; no API action invoked",
        "states": [],
        "status": "running",
    }
    originals = [PROJECT_ROOT / name for name in (
        "assets/whale.png", "assets/character/master.png",
        "assets/character/static/tsundere.png",
    )]
    original_hashes = {str(path.relative_to(PROJECT_ROOT)): file_hash(path)
                       for path in originals if path.is_file()}
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Microsoft YaHei", 10))
    try:
        # All session paths come from the temporary INI location. The test
        # backend prevents any interaction with the Windows credential vault.
        with tempfile.TemporaryDirectory(prefix="aura-expression-menu-") as directory:
            settings = isolated_settings(Path(directory) / "settings.ini", MemoryCredentialBackend())
            with patch("socket.socket.connect", side_effect=RuntimeError("Network disabled in menu verification")), \
                    patch("socket.socket.connect_ex", side_effect=RuntimeError("Network disabled in menu verification")):
                session = ApplicationController(PetConfig(), settings=settings)
                try:
                    session.show()
                    session.pet.refresh_timer.stop()
                    session.open_debug_panel()
                    pet, controller, panel = session.pet, session.pet_controller, session.debug_panel
                    panel.setStyleSheet(panel.styleSheet() + "\nQWidget { font-family: 'Microsoft YaHei'; }")
                    panel.resize(860, 1000)
                    panel.tabs.setCurrentIndex(0)
                    app.processEvents()
                    assert panel.expression_combo.count() == len(EXPRESSIONS) == 7
                    test_button = panel.findChild(QPushButton, "testExpressionButton")
                    assert test_button is not None
                    for code, (label, _) in EXPRESSIONS.items():
                        index = panel.expression_combo.findData(code)
                        assert index >= 0, f"Menu option missing: {code}"
                        panel.expression_combo.setCurrentIndex(index)
                        # Also cover explicit hot reload for the initially
                        # selected neutral option and unchanged menu selections.
                        test_button.click()
                        app.processEvents()
                        expected = controller.expressions.resolve(code)
                        assert expected.pixmap is not None, f"Expression art missing: {code}"
                        actual_image = pet._source.toImage()
                        expected_image = expected.pixmap.toImage()
                        assert actual_image.size().width() == actual_image.size().height() == 1024, code
                        assert actual_image.hasAlphaChannel(), code
                        actual_rgba = actual_image.convertToFormat(QImage.Format.Format_RGBA8888)
                        expected_rgba = expected_image.convertToFormat(QImage.Format.Format_RGBA8888)
                        assert actual_rgba == expected_rgba, f"Displayed art differs from resolved {code} PNG"
                        assert controller.expression == code
                        assert panel.expression_combo.currentData() == code
                        assert "已加载" in controller.expression_status
                        assert panel.expression_status.text() == controller.expression_status
                        assert controller.asset_player._clip is None, f"Expression replaced by video: {code}"
                        assert not controller.asset_player._loading, f"Video autoload started: {code}"
                        assert not controller.asset_player._installed, f"Video frames installed: {code}"
                        image_path = output / f"{code}_pet.png"
                        capture_widget(pet, image_path)
                        report["states"].append({
                            "code": code, "label": label,
                            "menu_label": panel.expression_combo.currentText(),
                            "status": controller.expression_status,
                            "source_size": [actual_image.width(), actual_image.height()],
                            "source_has_alpha": actual_image.hasAlphaChannel(),
                            "source_rgba_pixels_sha256": pixel_hash(actual_image),
                            "resolved_rgba_pixels_sha256": pixel_hash(expected_image),
                            "exact_bitmap_match": True,
                            "animation_autoplay": False,
                            "pet_render": str(image_path.relative_to(BATCH_DIRECTORY)),
                        })
                    panel.expression_combo.setCurrentIndex(panel.expression_combo.findData("happy"))
                    test_button.click()
                    scroll = panel.findChild(QScrollArea, "petControlsScroll")
                    assert scroll is not None
                    scroll.ensureWidgetVisible(panel.expression_combo)
                    app.processEvents()
                    panel_image = output / "happy_debug_panel.png"
                    capture_widget(panel, panel_image)
                    report["happy_debug_panel"] = str(panel_image.relative_to(BATCH_DIRECTORY))
                    report["thinking_png_prevents_autoplay"] = True
                    report["transparent_pet_window"] = pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                    assert report["transparent_pet_window"]
                finally:
                    session.shutdown()
                    spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                    for widget in (session.pet, session.debug_panel, session.chat_window, session.memory_panel):
                        if widget is not None:
                            widget.deleteLater()
                    session.deleteLater()
                    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    app.processEvents()
        report["original_assets_unchanged"] = all(
            file_hash(PROJECT_ROOT / path) == digest for path, digest in original_hashes.items()
        )
        assert report["original_assets_unchanged"]
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
