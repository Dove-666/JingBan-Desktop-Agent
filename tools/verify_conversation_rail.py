"""Render the conversation ruler in a real Qt window using memory-only chat.

No model, user archive, settings, clipboard or desktop screenshot is accessed.
PNG files are captures of the owned QA window only.
"""

import argparse
import ctypes
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.debug_panel import ChatWindow
from desktop_pet.ui.theme import ThemeManager
from tests.test_application import spin_until
from tests.test_assistant_ui import AssistantUiChat, eval_js, set_dom_text, wait_js
from tests.test_glass_ui import GlassInteractions


class Preview(GlassInteractions, unittest.TestCase):
    pass


class ThemePreferences:
    def load_theme(self):
        return "dark"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/conversation_rail_refinement_20261103")
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    foreground = ctypes.windll.user32.GetForegroundWindow() if sys.platform == "win32" else 0
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    theme = ThemeManager(ThemePreferences(), parent=app)
    chat = AssistantUiChat()
    chat.current_title = "鲸鱼娘的设计日记"
    chat.conversations[0]["title"] = chat.current_title
    topics = [
        ("想让界面安静一点", "让常用操作留在手边，其他细节收起来。留白有节奏，读起来才轻松。"),
        ("试试夜晚的颜色", "石墨色的底，配一点冰蓝。文字保持清晰，光感只落在边缘上。"),
        ("旧对话该怎么找？", "沿着左边的小刻度回看就好。停一下能看到内容，点一下就回到那句话。"),
        ("手机发来的任务为什么没执行？", "这条消息走的是手机直连，电脑只收到了同步记录，没有调用电脑任务助手。先切换到电脑协助，再发送一次任务。"),
    ]
    chat.messages = []
    for index in range(24):
        title, response = topics[index % len(topics)]
        chat.messages.extend([
            {"id": f"rail-user-{index}", "role": "user", "source": "user", "content": title},
            {"id": f"rail-assistant-{index}", "role": "assistant", "source": "assistant",
             "content": response + "\n\n" + "草稿会保留，可以回看后接着聊。"},
        ])
    window = ChatWindow(chat)
    helper = Preview()
    helper.app = app
    try:
        window.resize(1060, 760)
        window.show()
        window.raise_()
        window.activateWindow()
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "Boolean(document.querySelector('.conversation-rail'))")
        helper.wait_for_renderer_size(panel)
        set_dom_text(panel, "#message-input", "这段草稿先留着，去看看刚才的对话。")
        helper.click_web(panel, ".close-sidebar")
        QTest.qWait(200)
        results = []
        for name, width, mode in (("dark", 1060, "dark"), ("light", 1060, "light"), ("narrow", 420, "dark")):
            theme.set_theme(mode, persist=False)
            window.resize(width, 760)
            helper.wait_for_renderer_size(panel)
            wait_js(panel, f"document.documentElement.dataset.theme === '{mode}'")
            QTest.qWait(120)
            rectangle = eval_js(panel, """(() => {
                const node=document.querySelector('.conversation-rail [role="slider"], .conversation-rail[role="slider"]');
                const r=node.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height*.49};
            })()""")
            target = helper.keyboard_target(panel)
            point = target.mapFrom(panel.web_view, QPoint(round(rectangle["x"]), round(rectangle["y"])))
            QTest.mouseMove(target, QPoint(target.width() // 2, 12))
            QTest.qWait(200)
            if name == "dark":
                assert window.grab().save(str(output / "rail-dark-idle.png"))
            QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(target, point)
            wait_js(panel, "Boolean(document.querySelector('.conversation-rail-preview'))")
            QTest.qWait(220)
            metrics = eval_js(panel, """(() => {
                const p=document.querySelector('.conversation-rail-preview'), r=p.getBoundingClientRect();
                const v=document.querySelector('.thread-viewport');
                return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:innerWidth,height:innerHeight,
                    scrollTop:v.scrollTop,documentWidth:document.documentElement.scrollWidth,
                    previewText:p.textContent,cardHeight:r.height,
                    titleWeight:getComputedStyle(p.querySelector('.conversation-rail-preview-title')).fontWeight,
                    excerptWeight:getComputedStyle(p.querySelector('.conversation-rail-preview-excerpt')).fontWeight,
                    background:getComputedStyle(p).backgroundColor};
            })()""")
            assert metrics["left"] >= 0 and metrics["right"] <= metrics["width"] + 1
            assert metrics["top"] >= 0 and metrics["bottom"] <= metrics["height"] + 1
            assert metrics["documentWidth"] <= metrics["width"]
            assert metrics["cardHeight"] <= 118
            assert metrics["excerptWeight"] == "400"
            filename = f"rail-{name}.png"
            assert window.grab().save(str(output / filename))
            if name == "dark":
                area_top = max(0, round(metrics["top"] - 155))
                capture_area = QRect(0, area_top, min(390, panel.web_view.width()),
                                     min(480, panel.web_view.height() - area_top))
                assert panel.web_view.grab(capture_area).save(str(output / "rail-detail-dark.png"))
            results.append({"image": filename, **metrics})
        assert chat.sent == []
        assert panel.web_state()["drafts"]["current"] == "这段草稿先留着，去看看刚才的对话。"
        report = {"platform": app.platformName(), "scope": "Actual Qt/WebEngine; synthetic in-memory conversation",
                  "previews": results, "draft_preserved": True, "no_messages_sent": True}
        (output / "native-preview.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"platform": report["platform"], "previews": len(results), "draft_preserved": True}))
    finally:
        window.hide()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        theme.close()
        if foreground:
            ctypes.windll.user32.SetForegroundWindow(foreground)


if __name__ == "__main__":
    main()
