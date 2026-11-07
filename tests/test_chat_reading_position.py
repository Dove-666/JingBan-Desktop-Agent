"""Shipped WebEngine reading intent over deferred history/send snapshots; zero API."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.test_assistant_ui import DeferredHistoryChat, make_panel, eval_js, wait_js, set_dom_text, click_dom


class ChatReadingPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat=DeferredHistoryChat()
        self.chat.histories={cid:[{"id":f"{cid}-{i}","role":"user" if i%2==0 else "assistant",
            "source":"user" if i%2==0 else "assistant", "content":f"{cid} 合成记录 {i}。"+"保留阅读位置。"*8}
            for i in range(60)] for cid in ("current","old")}
        self.chat.messages=deepcopy(self.chat.histories["current"])
        self.preview=None
        self.panel=make_panel(self.chat)
        original=self.panel.web_state
        override=patch.object(self.panel,"web_state",side_effect=lambda:{**original(),"streamingReply":self.preview})
        override.start();self.addCleanup(override.stop)
        wait_js(self.panel,"document.querySelectorAll('[data-message-id]').length===60")

    def tearDown(self):
        self.panel.hide();self.panel.deleteLater();self.app.processEvents()

    def position(self):
        QTest.qWait(70)
        return eval_js(self.panel,"""(() => {
          const v=document.querySelector('.thread-viewport'),r=v.getBoundingClientRect();
          const a=[...v.querySelectorAll('[data-message-id]')].find(n=>n.getBoundingClientRect().bottom>r.top+4);
          return {id:a?.dataset.messageId,offset:a?.getBoundingClientRect().top-r.top,
            gap:v.scrollHeight-v.scrollTop-v.clientHeight,draft:document.querySelector('#message-input').value};
        })()""")

    def scroll_up(self):
        point=eval_js(self.panel,"(() => {const r=document.querySelector('.thread-viewport').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
        target=self.panel.web_view.focusProxy() or self.panel.web_view
        self.panel.web_view.setFocus();target.setFocus()
        local=target.mapFrom(self.panel.web_view,QPoint(round(point['x']),round(point['y'])))
        event=QWheelEvent(QPointF(local),QPointF(target.mapToGlobal(local)),QPoint(),QPoint(0,960),
            Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
        QApplication.sendEvent(target,event);QTest.qWait(80)
        eval_js(self.panel,"(() => {const v=document.querySelector('.thread-viewport');v.scrollTop=(v.scrollHeight-v.clientHeight)*.3;return true;})()")
        position=self.position()
        self.assertGreater(position['gap'],300,'The native wheel fixture must really leave the latest messages')
        return position

    def open(self,cid):
        self.assertTrue(self.chat.open_conversation(cid))
        wait_js(self.panel,"Boolean(document.querySelector('.history-loading'))")
        QTest.qWait(40)
        self.chat.finish_open()
        wait_js(self.panel,f"document.querySelectorAll('[data-message-id]').length===60 && Boolean(document.querySelector('[data-message-id={cid}-0]'))")
        QTest.qWait(80)

    def assertReadingSame(self,before,after):
        self.assertEqual(before['id'],after['id'],"Returning to a conversation must restore the message being read")
        self.assertAlmostEqual(before['offset'],after['offset'],delta=2)

    def test_each_conversation_restores_its_draft_and_reading_anchor_after_loading_placeholder(self):
        set_dom_text(self.panel,'#message-input','A 未发送草稿。\n第二行。')
        a=self.scroll_up()
        self.open('old')
        set_dom_text(self.panel,'#message-input','B 未发送草稿。')
        b=self.scroll_up()
        self.open('current')
        current=self.position()
        self.assertEqual('A 未发送草稿。\n第二行。',current['draft'])
        self.assertReadingSame(a,current)
        self.open('old')
        old=self.position()
        self.assertEqual('B 未发送草稿。',old['draft'])
        self.assertReadingSame(b,old)

    def test_explicit_send_restores_follow_latest_through_deferred_append_and_stream_growth(self):
        def accepted(text):
            self.chat.sent.append(text)
            self.chat.busy=True;self.chat.can_stop=True
            self.chat.busy_changed.emit(True)
            def append():
                self.chat.messages.append({'id':'owned-new-user','role':'user','source':'user','content':text})
                self.preview={'conversationId':'current','requestId':'owned-stream','text':'合成流式开始。','state':'streaming'}
                self.chat.messages_changed.emit();self.panel._queue_web_state()
            QTimer.singleShot(60,append)
            return True
        self.chat.send=accepted
        self.scroll_up()
        self.panel.focus_message('current-12')
        QTest.qWait(250)
        set_dom_text(self.panel,'#message-input','新的合成问题。\n第二行完整保留。')
        click_dom(self.panel,'[data-testid=send-button]')
        wait_js(self.panel,"Boolean(document.querySelector('[data-stream-state=streaming]'))")
        self.assertLessEqual(self.position()['gap'],3,'Explicit send must follow the new message, even from a search/read position')
        self.preview={**self.preview,'text':'合成段落。\n\n'*25}
        self.panel._queue_web_state()
        wait_js(self.panel,"document.querySelector('[data-stream-state=streaming]')?.textContent.includes('合成段落')")
        self.assertLessEqual(self.position()['gap'],3,'Following latest must continue as the reply grows')

    def test_search_focus_requested_during_loading_wins_over_the_cached_anchor(self):
        self.open('old');self.scroll_up()
        self.open('current')
        self.chat.open_conversation('old')
        wait_js(self.panel,"Boolean(document.querySelector('.history-loading'))")
        self.panel.focus_message('old-2')
        QTest.qWait(50)
        self.chat.finish_open()
        wait_js(self.panel,"""(() => {
          const n=document.querySelector('[data-message-id=old-2]'),v=document.querySelector('.thread-viewport');
          if(!n)return false;const r=n.getBoundingClientRect(),b=v.getBoundingClientRect();return r.bottom>b.top&&r.top<b.bottom;
        })()""")
        self.chat.messages_changed.emit();QTest.qWait(100)
        self.assertTrue(eval_js(self.panel,"""(() => {
          const n=document.querySelector('[data-message-id=old-2]'),v=document.querySelector('.thread-viewport');
          const r=n.getBoundingClientRect(),b=v.getBoundingClientRect();return r.bottom>b.top&&r.top<b.bottom;
        })()"""),'A later snapshot must not restore the old reading anchor over the search target')

    def test_explicit_latest_cancels_a_focus_target_that_has_not_arrived(self):
        self.scroll_up()
        self.panel.focus_message('owned-future-target')
        QTest.qWait(50)
        click_dom(self.panel,'.latest-button')
        self.chat.messages.append({'id':'owned-future-target','role':'assistant','source':'assistant','content':'稍后到达的合成消息。'})
        self.chat.messages_changed.emit()
        wait_js(self.panel,"Boolean(document.querySelector('[data-message-id=owned-future-target]'))")
        wait_js(self.panel,"(() => {const v=document.querySelector('.thread-viewport');return v.scrollHeight-v.scrollTop-v.clientHeight<3;})()")
        self.assertLessEqual(self.position()['gap'],3)


class ActualControllerReadingJourneyTests(unittest.TestCase):
    def test_actual_worker_history_search_multiline_stop_and_following_intents(self):
        from tools.qa_user_reading_stream_20261105 import run
        with tempfile.TemporaryDirectory(prefix='aura-owned-reading-journey-') as directory:
            result=run(Path(directory))
        self.assertEqual(0,result['real_model_requests'])
        self.assertEqual(2,len(result['synthetic_submissions']))
        for name,passed in result['checks'].items():
            with self.subTest(check=name):self.assertTrue(passed)


if __name__=='__main__':unittest.main()
