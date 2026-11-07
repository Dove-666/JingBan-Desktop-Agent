"""Actual WebEngine owners retire naturally in page-before-profile order."""
import gc
import unittest
import weakref
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool, qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from desktop_pet.ui.assistant_panel import AssistantChatPanel
from tests.test_assistant_ui import AssistantUiChat, eval_js, set_dom_text, wait_until


class LifecycleChat(AssistantUiChat):
    def send(self, *args, **kwargs):
        raise AssertionError("LIFECYCLE_TEST_MUST_NOT_SEND")


class AssistantWebLifecycleTests(unittest.TestCase):
    observations = []

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.observations = []

    def setUp(self):
        self.panels, self.chats, self.orders, self.qt_messages = [], [], [], []

        def message(kind, context, text):
            self.qt_messages.append({"kind": str(kind), "category": context.category, "message": text})
            if self.previous_handler is not None:
                self.previous_handler(kind, context, text)

        self.previous_handler = qInstallMessageHandler(message)

    def flush_deletions(self):
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        QTest.qWait(20)
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def tearDown(self):
        try:
            for panel in self.panels:
                if isValid(panel):
                    for timer in (panel._web_start_timer, panel._web_timeout,
                                  panel._web_publish_timer, panel._web_stream_timer,
                                  panel._web_execution_timer):
                        timer.stop()
                    panel.close()
                    panel.deleteLater()
            self.flush_deletions()
            for chat in self.chats:
                if isValid(chat):
                    owner = getattr(chat, "_local_draft_owner", None)
                    if owner is not None:
                        self.assertTrue(owner.shutdown(2))
                    chat.deleteLater()
            self.flush_deletions()
            gc.collect()
            profile_warnings = [item for item in self.qt_messages
                if "Release of profile requested" in item["message"]]
            self.observations.append({"test": self.id(), "destruction_orders": self.orders,
                "qt_messages": self.qt_messages, "profile_warning_count": len(profile_warnings)})
            self.assertEqual(profile_warnings, [])
            self.assertFalse(any(widget.isVisible() for widget in self.app.topLevelWidgets()))
            self.assertEqual(QThreadPool.globalInstance().activeThreadCount(), 0)
        finally:
            qInstallMessageHandler(self.previous_handler)

    def panel(self, chat=None, *, ready=True):
        if chat is None:
            chat = LifecycleChat()
            self.chats.append(chat)
        panel = AssistantChatPanel(chat)
        self.panels.append(panel)
        panel.resize(700, 560)
        panel.setWindowTitle("AURA owned WebEngine lifecycle test")
        if ready:
            panel.show()
            wait_until(lambda: panel._frontend_ready, timeout=15)
        return chat, panel

    def observe(self, panel, label):
        page, profile, view = panel.web_view.page(), panel._web_profile, panel.web_view
        order = []
        record = {"label": label, "order": order}
        self.orders.append(record)
        for obj, name in ((page, "page"), (profile, "profile"), (view, "view")):
            obj.destroyed.connect(lambda *args, name=name: order.append(name))
        return page, profile, view, order

    def assert_page_before_profile(self, order):
        self.assertEqual([item for item in order if item in {"page", "profile"}], ["page", "profile"])

    def test_natural_panel_and_view_deletion_keep_close_and_drafts(self):
        chat, first = self.panel()
        _, second = self.panel(chat)
        draft = "OWNED UNSENT LIFECYCLE DRAFT"
        set_dom_text(first, "#message-input", draft)
        wait_until(lambda: chat._local_draft_owner.snapshot(chat.current_conversation_id).text == draft)
        for panel, delete_view in ((first, False), (second, True)):
            page, profile, view, order = self.observe(panel, "view" if delete_view else "panel")
            self.assertIs(page.parent(), view)
            self.assertIs(profile.parent(), view)
            panel.close()
            self.app.processEvents()
            self.assertTrue(all(isValid(obj) for obj in (panel, view, page, profile)))
            self.assertFalse(order)
            self.assertEqual(chat._local_draft_owner.snapshot(chat.current_conversation_id).text, draft)
            panel.show()
            self.app.processEvents()
            self.assertTrue(panel._frontend_ready)
            wait_until(lambda: eval_js(panel, "document.querySelector('#message-input')?.value") == draft)
            if delete_view:
                view.deleteLater()
            else:
                panel.deleteLater()
            self.flush_deletions()
            self.assertFalse(isValid(view))
            self.assertFalse(isValid(page))
            self.assertFalse(isValid(profile))
            self.assert_page_before_profile(order)
        self.assertEqual(chat._local_draft_owner.snapshot(chat.current_conversation_id).text, draft)

    def test_partial_web_discard_and_recreation_preserve_draft(self):
        chat, panel = self.panel()
        draft = "OWNED FALLBACK LIFECYCLE DRAFT"
        set_dom_text(panel, "#message-input", draft)
        wait_until(lambda: chat._local_draft_owner.snapshot(chat.current_conversation_id).text == draft)
        page, profile, view, order = self.observe(panel, "discard-partial-web")
        panel._discard_partial_web()
        panel._web_failed()
        self.assertIsNone(panel.web_view)
        self.flush_deletions()
        self.assertFalse(any(isValid(obj) for obj in (view, page, profile)))
        self.assert_page_before_profile(order)
        self.assertTrue(panel.reload_button.isVisible())
        self.assertTrue(panel.compatibility_button.isVisible())
        panel._ensure_web()
        wait_until(lambda: panel._frontend_ready, timeout=15)
        wait_until(lambda: eval_js(panel, "document.querySelector('#message-input')?.value") == draft)
        self.assertEqual(chat._local_draft_owner.snapshot(chat.current_conversation_id).text, draft)
        self.observe(panel, "recreated-panel")

    def test_page_constructor_and_bootstrap_failures_release_actual_owners(self):
        from PySide6 import QtWebEngineCore
        actual_profile = QtWebEngineCore.QWebEngineProfile
        actual_page = QtWebEngineCore.QWebEnginePage
        for stage in ("page-constructor", "bootstrap"):
            with self.subTest(stage=stage):
                _, panel = self.panel(ready=False)
                order, profiles, pages = [], [], []
                self.orders.append({"label": stage, "order": order})

                class ObservedProfile(actual_profile):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        profiles.append(weakref.ref(self))
                        self.destroyed.connect(lambda *args: order.append("profile"))

                class ObservedPage(actual_page):
                    def __init__(self, *args, **kwargs):
                        if stage == "page-constructor":
                            raise RuntimeError("OWNED_PAGE_CONSTRUCTOR_FAILURE")
                        super().__init__(*args, **kwargs)
                        pages.append(weakref.ref(self))
                        self.destroyed.connect(lambda *args: order.append("page"))

                def bootstrap(page):
                    raise RuntimeError("OWNED_BOOTSTRAP_FAILURE")

                with patch.object(QtWebEngineCore, "QWebEngineProfile", ObservedProfile), \
                     patch.object(QtWebEngineCore, "QWebEnginePage", ObservedPage), \
                     patch.object(panel, "_install_theme_bootstrap", side_effect=bootstrap):
                    panel._ensure_web()
                    self.assertIsNone(panel.web_view)
                    self.assertIsNone(panel._web_profile)
                    self.assertFalse(panel._web_initialized)
                    self.flush_deletions()
                    gc.collect()
                self.assertEqual(len(profiles), 1)
                self.assertTrue(all(reference() is None or not isValid(reference()) for reference in (*profiles, *pages)))
                self.assertFalse(panel.reload_button.isHidden())
                if stage == "bootstrap":
                    self.assert_page_before_profile(order)
                else:
                    self.assertEqual(order, ["profile"])


if __name__ == "__main__":
    unittest.main()
