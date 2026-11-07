"""Follow settled native message layouts while preserving deliberate reading."""
import unittest
import time

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests import test_xiangqi_ui as ui_fixture


class XiangqiScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    setUp = ui_fixture.XiangqiUiTests.setUp
    tearDown = ui_fixture.XiangqiUiTests.tearDown

    def messages(self, count=12):
        return [{"role": "user" if index % 2 else "assistant",
                 "text": f"第{index}条合成棋局消息，测试中文换行和完整显示。" * 10}
                for index in range(count)]

    def settle(self):
        # Wait for wrapped content to acquire a real range before a test
        # performs a user scroll. A fixed 100 ms can expire while another build
        # keeps the machine busy, before Qt has handled the queued layout.
        deadline = time.monotonic() + 2.0
        scrollbar = self.window.chat_scroll.verticalScrollBar()
        previous = None
        stable = 0
        while time.monotonic() < deadline:
            self.app.processEvents()
            geometry = (scrollbar.maximum(), self.window.messages_widget.size(),
                        self.window.chat_scroll.viewport().size())
            stable = stable + 1 if geometry == previous and scrollbar.maximum() > 0 else 0
            if stable >= 4:
                return
            previous = geometry
            QTest.qWait(10)
        self.fail("The wrapped message layout did not acquire a stable scroll range")

    def assert_at_end(self):
        scrollbar = self.window.chat_scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        layout = self.window.messages_layout
        row = layout.itemAt(layout.count()-2).widget()
        label = row.layout().itemAt(row.layout().count()-1).widget()
        bottom = label.mapTo(self.window.chat_scroll.viewport(), label.rect().bottomRight())
        self.assertLessEqual(bottom.y(), self.window.chat_scroll.viewport().height())
        self.assertGreaterEqual(bottom.y(), 0)

    def populate_at_end(self):
        self.controller.update(commentary=self.messages())
        self.settle()
        scrollbar = self.window.chat_scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        return scrollbar

    def test_initial_history_and_long_replies_follow_final_wrapped_layout(self):
        self.controller.update(commentary=self.messages())
        self.settle()
        self.assert_at_end()
        for number in range(3):
            self.controller.update(commentary=self.controller.state["commentary"] + [
                {"role": "assistant", "text": f"第{number}次较长的中文回复需要完整地出现在可见区域。" * 25}])
            self.settle()
            self.assert_at_end()

    def test_return_submission_and_reply_reveal_last_line_after_layout_expands(self):
        self.populate_at_end()
        question = "请分析这一局，我想知道马和炮应该怎样配合？" * 8
        self.window.question_input.setText(question)
        QTest.keyClick(self.window.question_input, Qt.Key.Key_Return)
        self.settle()
        self.assertEqual(self.window.question_input.text(), "")
        self.assertIn(("ask", question), self.controller.calls)
        self.assert_at_end()
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "可以先整理自己的阵形，再考虑把马调到更活跃的位置。" * 25}])
        self.settle()
        self.assert_at_end()

    def test_reply_while_record_tab_is_open_follows_when_chat_returns(self):
        self.populate_at_end()
        self.window.tabs.setCurrentIndex(1)
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "切换到棋谱时收到的这段回复，返回聊天应自动显示末尾。" * 25}])
        self.settle()
        self.window.tabs.setCurrentIndex(0)
        self.settle()
        self.assert_at_end()

    def test_reading_old_messages_pauses_follow_until_user_sends_again(self):
        scrollbar = self.populate_at_end()
        scrollbar.setValue(0)
        self.window.question_input.setText("这是一段尚未发送的草稿")
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "阅读旧消息期间追加的自动评论。" * 15}])
        self.settle()
        self.assertEqual(scrollbar.value(), 0)
        self.assertEqual(self.window.question_input.text(), "这是一段尚未发送的草稿")
        self.window.question_input.setText("现在继续问下一步怎么走？")
        QTest.keyClick(self.window.question_input, Qt.Key.Key_Return)
        self.settle()
        self.assert_at_end()
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "新的回复应继续跟随到聊天最底部。" * 20}])
        self.settle()
        self.assert_at_end()
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "布局尚未完成时，用户也可以主动返回旧消息。" * 10}])
        scrollbar.setValue(0)
        self.settle()
        self.assertEqual(scrollbar.value(), 0)

    def test_trimming_eighty_message_history_does_not_force_reader_to_latest(self):
        messages = self.messages(80)
        self.controller.update(commentary=messages)
        self.settle()
        scrollbar = self.window.chat_scroll.verticalScrollBar()
        scrollbar.setValue(0)
        self.controller.update(commentary=messages[1:] + [
            {"role": "assistant", "text": "历史截断后新追加的回复。" * 15}])
        self.settle()
        self.assertEqual(scrollbar.value(), 0)

    def test_viewport_wheel_pauses_follow_and_returning_to_bottom_resumes_it(self):
        scrollbar = self.populate_at_end()
        viewport = self.window.chat_scroll.viewport()
        position = viewport.rect().center()

        def wheel(delta):
            event = QWheelEvent(QPointF(position), QPointF(viewport.mapToGlobal(position)),
                                QPoint(), QPoint(0, delta), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(viewport, event)

        wheel(360)
        self.assertGreater(scrollbar.maximum()-scrollbar.value(), 35)
        reading_position = scrollbar.value()
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "真实滚轮上翻后追加消息应保持阅读位置。" * 15}])
        self.settle()
        self.assertEqual(scrollbar.value(), reading_position)
        for _ in range(30):
            if scrollbar.value() == scrollbar.maximum():
                break
            wheel(-360)
        self.assert_at_end()
        self.controller.update(commentary=self.controller.state["commentary"] + [
            {"role": "assistant", "text": "用滚轮回到底部之后，新回复继续自动显示。" * 15}])
        self.settle()
        self.assert_at_end()


if __name__ == "__main__":
    unittest.main()
