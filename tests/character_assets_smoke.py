"""Offscreen real-application asset check with temporary settings/credentials.

Run: .venv\\Scripts\\python.exe -m tests.character_assets_smoke
Only this process's own widgets are rendered; the user's desktop is never read.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QPoint, QThreadPool, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QScrollArea

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import DEFAULT_IMAGE, PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import FrameAssetError, read_manifest
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until


def main():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # Qt's offscreen Windows font database may omit system fallback fonts.
    # Register an existing font only for this isolated screenshot process.
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Microsoft YaHei", 10))
    output = PROJECT_ROOT / "artifacts" / "character_assets" / "application_smoke"
    output.mkdir(parents=True, exist_ok=True)
    original_hash = hashlib.sha256(DEFAULT_IMAGE.read_bytes()).hexdigest()
    result = {"platform": "offscreen", "production_ui_touched": False,
              "credentials": "test-only in-memory backend", "settings": "temporary INI",
              "live_generated_animation_verified": False}
    with tempfile.TemporaryDirectory(prefix="aura-assets-app-smoke-") as directory:
        settings = isolated_settings(Path(directory) / "settings.ini", MemoryCredentialBackend())
        session = ApplicationController(PetConfig(), settings=settings)
        try:
            session.show()
            session.open_debug_panel()
            app.processEvents()
            pet, controller, panel = session.pet, session.pet_controller, session.debug_panel
            panel.setStyleSheet(panel.styleSheet() + "\nQWidget { font-family: 'Microsoft YaHei'; }")
            panel.resize(860, 1000)
            app.processEvents()
            result["transparent_window"] = pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            result["topmost_window"] = bool(pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            result["original_status"] = controller.appearance_status
            assert pet._source.size().width() == pet._source.size().height() == 1024
            assert panel.findChild(QComboBox, "staticCharacterCombo") is None
            for name, label in (("idle_tsundere", "傲娇"), ("idle_sleepy", "困倦")):
                index = panel.asset_animation_combo.findData(name)
                assert index >= 0 and panel.asset_animation_combo.itemText(index) == label
            assert controller.set_static_character("tsundere")
            assert controller.appearance == "tsundere"
            result["tsundere_status"] = controller.appearance_status
            result["scales"] = []
            for percent in (50, 75, 100, 125, 150, 200):
                controller.set_scale(percent)
                assert pet._sprite.width() == pet._sprite.height()
                assert pet.rect().contains(pet.animated_sprite_rect.toAlignedRect())
                result["scales"].append({"percent": percent, "window": [pet.width(), pet.height()],
                                         "sprite": [pet._sprite.width(), pet._sprite.height()]})
            controller.reset_scale()
            result["animation_catalog"] = controller.animation_catalog()
            result["available_animation_manifests"] = []
            for name, _label, status in result["animation_catalog"]:
                try:
                    read_manifest(controller.asset_player.directory, name)
                except FrameAssetError:
                    assert not controller.play_asset_animation(name)
                    assert "未" in status
                else:
                    # Genuine video playback is measured by real_animation_smoke.
                    # A static-art smoke must stay cheap once clips are installed.
                    result["available_animation_manifests"].append(name)
            before = controller.appearance
            panel.close()
            session.open_debug_panel()
            assert controller.appearance == before and session.debug_panel is panel
            image = QImage(pet.size(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                pet.render(painter, QPoint())
            finally:
                painter.end()
            image.save(str(output / "tsundere_pet.png"), "PNG")
            assert image.pixelColor(0, 0).alpha() == 0
            panel.findChild(QScrollArea, "petControlsScroll").ensureWidgetVisible(
                panel.findChild(QPushButton, "refreshCharacterAssetsButton"))
            app.processEvents()
            panel.grab().save(str(output / "debug_asset_controls.png"), "PNG")
            result["original_asset_unchanged"] = hashlib.sha256(DEFAULT_IMAGE.read_bytes()).hexdigest() == original_hash
            assert result["original_asset_unchanged"]
            result["status"] = "passed"
        finally:
            session.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            for widget in (session.pet, session.debug_panel, session.chat_window, session.memory_panel):
                if widget is not None:
                    widget.deleteLater()
            session.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
