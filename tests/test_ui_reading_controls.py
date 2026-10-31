"""User-facing reading, retry and independent visual controls in the shipped page."""
import unittest

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from tests.test_assistant_ui import AssistantUiChat, click_dom, eval_js, make_panel, wait_js


class ReadingChat(AssistantUiChat):
    reading_font_changed = Signal(int)
    retry_availability_changed = Signal()

    def __init__(self):
        super().__init__()
        self.reading_font_size = 16
        self.retry_ready = False
        self.error = "已停止等待回复。"
        self.error_action = "retry"
        self.error_conversation_id = self.current_conversation_id
        self.error_message_id = "stopped-user"
        self.messages = [{"id": "stopped-user", "role": "user", "content": "待重试的问题",
                          "metadata": {"request_status": "cancelled"}},
                         {"id": "code-answer", "role": "assistant",
                          "content": "只复制下面的代码。\n\n```python\n    print('鲸鱼娘')\n```\n\n这句不应进入代码剪贴板。"}]

    def message_retry_state(self, *_args):
        return {"available": self.retry_ready,
                "status": "" if self.retry_ready else "正在结束上次检索，结束后可重试。"}

    def set_reading_font_size(self, value):
        if type(value) is not int or not 12 <= value <= 22:
            return False
        self.reading_font_size = value
        self.reading_font_changed.emit(value)
        return True


class VisualState(QObject):
    runtime_changed = Signal(object)
    settings_requested = Signal()

    def __init__(self):
        super().__init__()
        self.paused = False
        self.analysis_error = ""

    def set_paused(self, value):
        self.paused = value
        self.runtime_changed.emit(self.runtime_snapshot())

    def runtime_snapshot(self):
        return {"enabled": True, "allowRemote": True, "paused": self.paused, "busy": False,
                "status": self.analysis_error or "合成观察状态", "analysisError": self.analysis_error,
                "cooldownSeconds": 0, "hourlyRemaining": 6, "nextObservationSeconds": 5,
                "nextAnalysisSeconds": 0, "previewAgeSeconds": 0}


class ReadingControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = ReadingChat()
        self.screen, self.camera = VisualState(), VisualState()
        self.chat.perception_controller = self.screen
        self.chat.camera_controller = self.camera
        self.panel = make_panel(self.chat)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_copy_code_preserves_indent_and_excludes_prose(self):
        click_dom(self.panel, ".code-toolbar button")
        wait_js(self.panel, "document.querySelector('.code-toolbar button').textContent.includes('已复制')")
        self.assertEqual(self.app.clipboard().text(), "    print('鲸鱼娘')\n")

    def test_reading_size_stays_large_in_narrow_window_and_syncs(self):
        self.panel.resize(420, 500)
        wait_js(self.panel, "innerWidth === 420")
        click_dom(self.panel, "[data-testid=tools-button]")
        click_dom(self.panel, "button[aria-label='增大聊天字号']")
        wait_js(self.panel, "getComputedStyle(document.querySelector('.markdown')).fontSize === '17px'")
        self.assertEqual(eval_js(self.panel, "getComputedStyle(document.querySelector('#message-input')).fontSize"), "17px")
        self.chat.set_reading_font_size(22)
        wait_js(self.panel, "getComputedStyle(document.querySelector('.markdown')).fontSize === '22px'")
        self.assertTrue(eval_js(self.panel, "document.querySelector('button[aria-label=\"增大聊天字号\"]').disabled"))
        self.assertEqual(eval_js(self.panel, "document.documentElement.scrollWidth"), 420)

    def test_blocked_retry_explains_itself_and_enables_without_new_message(self):
        wait_js(self.panel, "document.querySelector('.retry-explanation')?.textContent.includes('正在结束')")
        self.assertTrue(eval_js(self.panel, "document.querySelector('.request-message-state button').disabled"))
        self.assertTrue(eval_js(self.panel, "document.querySelector('.error-banner button').disabled"))
        self.chat.retry_ready = True
        self.chat.retry_availability_changed.emit()
        wait_js(self.panel, "!document.querySelector('.request-message-state button').disabled")
        wait_js(self.panel, "!document.querySelector('.error-banner button').disabled")

    def test_pause_all_and_independent_resume_do_not_grant_permissions(self):
        click_dom(self.panel, ".pause-all")
        self.assertTrue(self.screen.paused)
        self.assertTrue(self.camera.paused)
        wait_js(self.panel, "document.querySelector('.camera-runtime').textContent.includes('摄像头已暂停')")
        eval_js(self.panel, "(() => {[...document.querySelectorAll('.camera-runtime button')].find(e=>e.textContent==='恢复摄像头').click();return true;})()")
        self.assertTrue(self.screen.paused)
        self.assertFalse(self.camera.paused)

    def test_camera_analysis_error_is_visible_in_chat_after_runtime_update(self):
        self.camera.analysis_error = "合成服务拒绝图片，请检查模型。"
        self.camera.runtime_changed.emit(self.camera.runtime_snapshot())
        wait_js(self.panel, "document.querySelector('.camera-runtime').textContent.includes('摄像头分析失败')")


if __name__ == "__main__":
    unittest.main()
