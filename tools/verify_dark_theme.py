"""Native Windows previews with isolated preferences, archives and inert services.

Uses real Qt/WebEngine widgets without operating an account, camera or model.
The production INI is hashed read-only and never used as a test configuration.
"""

import ctypes
import configparser
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtTest import QTest
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QScrollArea

from desktop_pet.ui.theme import ThemeManager
from desktop_pet.ui.memory_panel import MemoryEditDialog
from desktop_pet.ui.selection_ask_dialog import SelectionAskDialog
from desktop_pet.selection.context import SelectionContext
from tests.test_application import spin_until
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js
from tests.test_glass_ui import GlassInteractions
from tests.test_startup_settings_ui import StartupSettingsUiTests
from tools.verify_settings_refresh import file_hash, preference_snapshot


class ThemePreferences:
    def load_theme(self):
        return "dark"


def production_hashes(path):
    """Compare key hashes without logging settings values or writing the INI."""
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(path.read_text(encoding="utf-8-sig") if path.exists() else "")
    return {f"{section}/{key}".replace("\\", "/"): hashlib.sha256(value.encode()).hexdigest()
            for section in config.sections() for key, value in config.items(section)}


class Preview(GlassInteractions, StartupSettingsUiTests):
    pass


def main():
    output = ROOT / "artifacts/dark_theme_20261102"
    output.mkdir(parents=True, exist_ok=True)
    production = Path(os.environ.get("LOCALAPPDATA", "")) / "AURA Desktop Pet/settings.ini"
    production_before = file_hash(production)
    production_keys_before = production_hashes(production)
    foreground = ctypes.windll.user32.GetForegroundWindow() if sys.platform == "win32" else 0
    Preview.setUpClass()
    theme = ThemeManager(ThemePreferences(), parent=Preview.app)
    fixture = Preview()
    fixture.setUp()
    assert Path(fixture.settings.config_path).resolve() != production.resolve()
    theme.settings_manager = fixture.settings
    dialogs = []
    renders = []
    try:
        before = preference_snapshot(fixture.settings)
        fixture.startup.enabled = True
        fixture.session.open_settings("general")
        window = fixture.session.chat_window
        page = window.settings_page
        for width, height in ((1060, 760), (580, 650), (420, 650)):
            window.resize(width, height)
            for key, _title, _terms in page.SECTIONS:
                page.show_section(key)
                window.show()
                window.raise_()
                QTest.qWait(160)
                selected = page.section_stack.currentWidget()
                scrolls = selected.findChildren(QScrollArea)
                if isinstance(selected, QScrollArea):
                    scrolls.insert(0, selected)
                overflow = any(s.horizontalScrollBar().maximum() for s in scrolls if s.isVisibleTo(page))
                name = f"settings-{key}-dark-{width}.png"
                assert window.grab().save(str(output / name))
                renders.append({"image": name, "width": window.width(), "horizontal_overflow": bool(overflow)})
        assert before == preference_snapshot(fixture.settings), "Browsing saved preferences"
        window.resize(1060, 760)
        fixture.session.open_chat_window()
        chat = fixture.session.chat
        cid = chat.current_conversation_id
        chat.store.append_message(cid, "user", "今天想让你陪我一起整理一下桌面，可以吗？", "user")
        chat.store.append_message(cid, "assistant", "当然可以呀。我们慢慢来，把桌面整理得像一片安静的海。\n\n### 先从三件小事开始\n\n- 把正在用的资料留在手边\n- 将旧截图放进同一个文件夹\n- 留一点空间，给今天的新想法\n\n你想先整理哪一部分？", "assistant")
        chat.open_conversation(cid)
        spin_until(lambda: not chat.history_loading)
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "document.documentElement.dataset.theme === 'dark'")
        fixture.wait_for_renderer_size(panel)
        set_dom_text(panel, "#message-input", "先帮我把今天的想法整理好吧…")
        QTest.qWait(180)
        assert window.grab().save(str(output / "chat-dark-1060.png"))
        fixture.open_row_menu(panel, cid)
        QTest.qWait(100)
        assert window.grab().save(str(output / "chat-menu-dark.png"))
        eval_js(panel, "(() => {document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));return true;})()")
        fixture.session.open_settings("general")
        page.theme_combo.setCurrentIndex(page.theme_combo.findData("light"))
        QTest.qWait(120)
        assert window.grab().save(str(output / "settings-general-light.png"))
        fixture.session.open_chat_window()
        wait_js(panel, "document.documentElement.dataset.theme === 'light'")
        assert window.grab().save(str(output / "chat-light-1060.png"))
        fixture.session.open_settings("general")
        page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
        QTest.qWait(120)
        source = SelectionContext("玻璃材质的重点，是层次和清晰。", title="选中文字 · 隔离预览")
        selection = SelectionAskDialog(source)
        dialogs.append(selection)
        selection.show()
        QTest.qWait(120)
        assert selection.grab().save(str(output / "selection-dark.png"))
        selection.hide()
        edit = MemoryEditDialog({"id": "preview-memory", "memory": "用户喜欢简洁、安静的深色界面。"}, parent=window)
        dialogs.append(edit)
        edit.show()
        QTest.qWait(120)
        assert edit.grab().save(str(output / "memory-edit-dark.png"))
        edit.hide()
        fixture.session.pet.show()
        fixture.session.pet.bubble.say("夜色刚刚好，鲸鱼娘在这里陪你。", 30000)
        QTest.qWait(100)
        assert fixture.session.pet.bubble.grab().save(str(output / "bubble-dark.png"))
        pet = fixture.session.pet
        point = pet.visible_character_rect.center().toPoint()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, pet.mapToGlobal(point))
        pet.contextMenuEvent(event)
        QTest.qWait(100)
        assert pet._menu is not None and pet._menu.isVisible()
        assert pet._menu.grab().save(str(output / "pet-menu-dark.png"))
        pet._menu.hide()
        assert fixture.settings.load_theme() == "dark"
        assert not fixture.startup.changes and not fixture.camera_start.called
        production_keys_after = production_hashes(production)
        changed_keys = sorted(key for key in production_keys_before.keys() | production_keys_after.keys()
                              if production_keys_before.get(key) != production_keys_after.get(key))
        changed_preferences = [key for key in changed_keys if not key.startswith("ui/windows/")]
        assert panel.web_state()["drafts"][cid] == "先帮我把今天的想法整理好吧…"
        result = {"platform": fixture.app.platformName(), "settings_renders": renders,
                  "production_preferences_unchanged": not changed_preferences,
                  "production_file_hash_unchanged": production_before == file_hash(production),
                  "concurrent_production_changed_keys": changed_keys,
                  "qa_writes_isolated": True, "draft_preserved": True,
                  "light_restored_and_dark_reapplied": True, "camera_not_started": True,
                  "scope": "Real Qt and shipped WebEngine; synthetic messages and isolated preferences"}
        (output / "native-preview.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"platform": result["platform"], "renders": len(renders),
                          "overflow": sum(r["horizontal_overflow"] for r in renders),
                          "production_preferences_unchanged": not changed_preferences,
                          "concurrent_production_changed_keys": changed_keys}))
        return int(any(r["horizontal_overflow"] for r in renders))
    finally:
        for dialog in dialogs:
            dialog.hide()
            dialog.deleteLater()
        fixture.tearDown()
        fixture.doCleanups()
        theme.close()
        if foreground:
            ctypes.windll.user32.SetForegroundWindow(foreground)


if __name__ == "__main__":
    raise SystemExit(main())
