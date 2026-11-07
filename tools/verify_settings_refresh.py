"""Render real settings windows using temporary preferences and inert services.

Run with the project's desktop Python. No camera, model, registry write or
production preference change is performed. Images are Qt window captures.
"""

import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea

from tests.test_startup_settings_ui import StartupSettingsUiTests


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def preference_snapshot(settings):
    store = settings._settings
    store.sync()
    # Moving/resizing the preview deliberately exercises normal placement saves.
    return {key: store.value(key) for key in store.allKeys() if not key.startswith("ui/windows/")}


def main():
    output = ROOT / "artifacts" / "settings_refresh_20261102"
    output.mkdir(parents=True, exist_ok=True)
    production = Path(os.environ.get("LOCALAPPDATA", "")) / "AURA Desktop Pet" / "settings.ini"
    production_before = file_hash(production)
    StartupSettingsUiTests.setUpClass()
    fixture = StartupSettingsUiTests()
    fixture.setUp()
    results = []
    try:
        # Match the screenshot's checked startup state using a memory-only service.
        fixture.startup.enabled = True
        isolated_before = preference_snapshot(fixture.settings)
        fixture.session.open_settings("general")
        window = fixture.session.chat_window
        window.setWindowTitle("AURA settings preview - isolated preferences")
        page = window.settings_page
        for width, height, sections in (
            (1060, 760, [key for key, _title, _terms in page.SECTIONS]),
            (580, 650, [key for key, _title, _terms in page.SECTIONS]),
            (420, 650, [key for key, _title, _terms in page.SECTIONS]),
        ):
            window.resize(width, height)
            for section in sections:
                page.show_section(section)
                window.show()
                window.raise_()
                QTest.qWait(220)
                panel = page.section_stack.currentWidget()
                scrolls = [scroll for scroll in panel.findChildren(QScrollArea)
                           if scroll.isVisibleTo(panel)]
                if isinstance(panel, QScrollArea):
                    scrolls.append(panel)
                overflows = [scroll.objectName() or "scroll" for scroll in scrolls
                             if scroll.horizontalScrollBar().maximum() > 0]
                name = f"{section}-{width}.png"
                assert window.grab().save(str(output / name))
                results.append({"section": section, "width": window.width(),
                                "height": window.height(), "horizontal_overflow": overflows,
                                "title_y": page.section_title.mapTo(page, QPoint()).y(),
                                "title_height": page.section_title.height(),
                                "image": name})
        fixture.session.open_settings("general")
        QTest.qWait(100)
        assert page._legacy_context_controls.isHidden()
        assert isolated_before == preference_snapshot(fixture.settings), "Browsing changed preferences"
        assert not fixture.startup.changes, "Browsing changed startup"
        assert not fixture.camera_start.called, "Browsing started a camera"
        assert production_before == file_hash(production), "Production preferences changed"
        summary = {"platform": QGuiApplication.platformName(), "windows": results,
                   "production_settings_unchanged": True, "browsing_preferences_unchanged": True,
                   "startup_unchanged": True, "camera_not_started": True,
                   "scope": "Real native Qt settings; isolated configuration and inert services"}
        (output / "render-check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=True))
        return int(any(row["horizontal_overflow"] for row in results))
    finally:
        fixture.tearDown()
        fixture.doCleanups()


if __name__ == "__main__":
    raise SystemExit(main())
