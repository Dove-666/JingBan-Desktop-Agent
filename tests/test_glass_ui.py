"""Exercise glass surfaces through the shipped WebEngine, not CSS snapshots.

Input events are synthetic Qt events sent to the renderer widget. The fixtures
use deterministic chat doubles or temporary application archives and settings.
No physical cursor, production preferences, registry or remote debugger is used.
"""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWidgets import QApplication

from tests import test_settings_navigation as navigation_fixture
from tests import test_assistant_ui as assistant_fixture
from tests.test_application import spin_until
from tests.test_assistant_ui import eval_js, set_dom_text, wait_js, wait_until


class GlassInteractions:
    def wait_for_renderer_size(self, panel):
        width, height = panel.web_view.width(), panel.web_view.height()
        wait_js(panel, f"Math.abs(innerWidth - {width}) <= 2 && Math.abs(innerHeight - {height}) <= 2")

    def keyboard_target(self, panel):
        return panel.web_view.focusProxy() or panel.web_view

    def click_web(self, panel, selector):
        """Use renderer hit testing and an actual Qt mouse press/release pair."""
        metrics = eval_js(panel, """(() => {
            const node = document.querySelector(SELECTOR);
            if (!node) throw new Error('Missing mouse target');
            const r = node.getBoundingClientRect();
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            const hit = document.elementFromPoint(x, y);
            return {x,y,width:r.width,height:r.height,tag:node.tagName,
                reachable:Boolean(hit && (hit === node || node.contains(hit)))};
        })()""".replace("SELECTOR", json.dumps(selector)))
        self.assertGreater(metrics["width"], 0, selector)
        self.assertGreater(metrics["height"], 0, selector)
        self.assertTrue(metrics["reachable"], f"A decoration or overlay intercepts {selector}")
        web_point = QPoint(round(metrics["x"]), round(metrics["y"]))
        panel.web_view.setFocus()
        target = self.keyboard_target(panel)
        QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                         pos=target.mapFrom(panel.web_view, web_point))
        self.app.processEvents()
        if metrics["tag"] in {"INPUT", "TEXTAREA"}:
            wait_js(panel, "document.activeElement === document.querySelector(" + json.dumps(selector) + ")")

    def ensure_sidebar_open(self, panel):
        visible = """(() => {
            const node=document.querySelector('.sidebar'), r=node.getBoundingClientRect();
            return getComputedStyle(node).visibility !== 'hidden' && r.width>0 && r.left>=-1;
        })()"""
        if not eval_js(panel, visible):
            self.click_web(panel, "[data-testid=history-toggle]")
        wait_js(panel, visible)

    def open_row_menu(self, panel, conversation_id="current"):
        self.ensure_sidebar_open(panel)
        row = "[data-conversation-id=" + json.dumps(conversation_id) + "]"
        eval_js(panel, "(() => {document.querySelector(" + json.dumps(row + " .conversation-link")
                + ").focus(); return true;})()")
        trigger = row + " .row-actions > button"
        wait_js(panel, "document.querySelector(" + json.dumps(trigger) + ").getBoundingClientRect().width > 0")
        self.click_web(panel, trigger)
        wait_js(panel, "Boolean(document.querySelector('.row-popover[data-glass-renderer]'))")
        wait_js(panel, "document.querySelector('.row-popover').contains(document.activeElement)")

    def install_media_preference(self, panel, query):
        """Set a page-local preference before React executes; OS settings stay intact."""
        script = QWebEngineScript()
        script.setName("aura-glass-test-media-preference")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode("""(() => {
            const original = window.matchMedia.bind(window);
            const preferences = new Set(['(prefers-reduced-transparency: reduce)',
                '(prefers-contrast: more)', '(forced-colors: active)',
                '(prefers-reduced-motion: reduce)']);
            window.matchMedia = (query) => {
                if (!preferences.has(query)) return original(query);
                return {matches:query === QUERY, media:query, onchange:null,
                    addListener() {}, removeListener() {},
                    addEventListener() {}, removeEventListener() {},
                    dispatchEvent() { return true; }};
            };
        })();""".replace("QUERY", json.dumps(query)))
        panel.web_view.page().scripts().insert(script)
        panel._reload_web()
        wait_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "Boolean(document.querySelector('.glass-composer[data-glass-renderer]'))")
        self.wait_for_renderer_size(panel)


class GlassUiTests(GlassInteractions, unittest.TestCase):
    setUpClass = assistant_fixture.AssistantUiTests.__dict__["setUpClass"]
    tearDown = assistant_fixture.AssistantUiTests.tearDown

    def setUp(self):
        assistant_fixture.AssistantUiTests.setUp(self)
        # WebChannel can become ready before the initial 100x30 renderer view
        # receives its first resize. Coordinate-based input needs the real size.
        self.panel.activateWindow()
        self.wait_for_renderer_size(self.panel)

    def test_decorated_composer_accepts_real_mouse_keyboard_enter_and_send_click(self):
        self.click_web(self.panel, "#message-input")
        wait_js(self.panel, "document.activeElement === document.querySelector('#message-input')")
        QTest.keyClicks(self.keyboard_target(self.panel), "glass keyboard input")
        wait_js(self.panel, "document.querySelector('#message-input').value === 'glass keyboard input'")
        QTest.keyClick(self.keyboard_target(self.panel), Qt.Key.Key_Return)
        wait_until(lambda: self.chat.sent == ["glass keyboard input"])
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")
        self.click_web(self.panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(self.panel), "glass mouse send")
        wait_js(self.panel, "!document.querySelector('.send-button').disabled")
        self.click_web(self.panel, ".send-button")
        wait_until(lambda: self.chat.sent == ["glass keyboard input", "glass mouse send"])
        wait_js(self.panel, "document.querySelector('#message-input').value === ''")

    def test_decorated_menu_supports_arrow_keys_enter_escape_and_mouse_selection(self):
        self.open_row_menu(self.panel)
        target = self.keyboard_target(self.panel)
        QTest.keyClick(target, Qt.Key.Key_Home)
        wait_js(self.panel, "document.activeElement?.dataset.testid === 'row-action-find'")
        QTest.keyClick(target, Qt.Key.Key_Down)
        wait_js(self.panel, "document.activeElement?.dataset.testid === 'row-action-export'")
        QTest.keyClick(target, Qt.Key.Key_Down)
        wait_js(self.panel, "document.activeElement?.dataset.testid === 'row-action-rename'")
        QTest.keyClick(target, Qt.Key.Key_Return)
        wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")
        QTest.keyClick(target, Qt.Key.Key_Escape)
        wait_js(self.panel, "!document.querySelector('dialog[open]')")
        self.open_row_menu(self.panel)
        self.click_web(self.panel, "[data-testid=row-action-find]")
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=message-search]'))")
        wait_js(self.panel, "document.activeElement === document.querySelector('[data-testid=message-search]')")
        QTest.keyClicks(target, "tasks")
        wait_js(self.panel, "document.querySelector('[data-testid=message-search]').value === 'tasks'")
        self.assertEqual(self.chat.sent, [])

    def test_glass_background_does_not_filter_editor_message_or_menu_text(self):
        self.open_row_menu(self.panel)
        filtered = eval_js(self.panel, """(() => {
            const problems=[];
            for (const text of document.querySelectorAll('#message-input, .markdown, .row-popover button')) {
                for (let node=text; node && node!==document.body; node=node.parentElement) {
                    const s=getComputedStyle(node);
                    if (s.filter !== 'none') problems.push({node:node.className, filter:s.filter});
                }
                const s=getComputedStyle(text);
                if (s.backdropFilter && s.backdropFilter !== 'none')
                    problems.push({node:text.className,backdrop:s.backdropFilter});
            }
            return problems;
        })()""")
        self.assertEqual(filtered, [], "Refraction must affect decorative backgrounds, not readable content")
        # Hit testing is independent of class presence and catches an accidental
        # pointer-events change on a decorative child or pseudo-element.
        self.click_web(self.panel, "[data-testid=row-action-rename]")
        wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")

    def test_glass_composer_and_full_menu_fit_narrow_and_wide_viewports(self):
        for width in (420, 1060):
            with self.subTest(width=width):
                self.panel.resize(width, 720)
                wait_js(self.panel, f"innerWidth <= {width} && innerWidth >= {width - 16}")
                set_dom_text(self.panel, "#message-input", "A long unsent draft " * 50)
                self.open_row_menu(self.panel)
                metrics = eval_js(self.panel, """(() => {
                    const pack=(selector)=>{const r=document.querySelector(selector).getBoundingClientRect();
                        return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
                    return {width:innerWidth,height:innerHeight,
                        body:document.body.scrollWidth,document:document.documentElement.scrollWidth,
                        composer:pack('.glass-composer'),editor:pack('#message-input'),menu:pack('.row-popover')};
                })()""")
                self.assertLessEqual(metrics["body"], metrics["width"] + 1)
                self.assertLessEqual(metrics["document"], metrics["width"] + 1)
                for name in ("composer", "menu"):
                    box = metrics[name]
                    self.assertGreaterEqual(box["left"], -1, (name, metrics))
                    self.assertGreaterEqual(box["top"], -1, (name, metrics))
                    self.assertLessEqual(box["right"], metrics["width"] + 1, (name, metrics))
                    self.assertLessEqual(box["bottom"], metrics["height"] + 1, (name, metrics))
                self.assertGreaterEqual(metrics["editor"]["left"], metrics["composer"]["left"])
                self.assertLessEqual(metrics["editor"]["right"], metrics["composer"]["right"])
                self.assertEqual(eval_js(self.panel, "document.querySelectorAll('.row-popover button').length"), 5)
                self.click_web(self.panel, "[data-testid=row-action-rename]")
                wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")
                QTest.keyClick(self.keyboard_target(self.panel), Qt.Key.Key_Escape)
                wait_js(self.panel, "!document.querySelector('dialog[open]')")

    def test_reduced_transparency_uses_solid_surface_and_preserves_input(self):
        self.install_media_preference(self.panel, "(prefers-reduced-transparency: reduce)")
        wait_js(self.panel, "document.querySelector('.glass-composer').dataset.glassRenderer === 'solid'")
        self.click_web(self.panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(self.panel), "readable solid fallback")
        QTest.keyClick(self.keyboard_target(self.panel), Qt.Key.Key_Return)
        wait_until(lambda: self.chat.sent == ["readable solid fallback"])
        self.open_row_menu(self.panel)
        self.assertEqual(eval_js(self.panel, "document.querySelector('.row-popover').dataset.glassRenderer"), "solid")
        self.click_web(self.panel, "[data-testid=row-action-rename]")
        wait_js(self.panel, "Boolean(document.querySelector('dialog[open] #rename-title'))")

    def test_high_contrast_uses_solid_menu_without_losing_keyboard_navigation(self):
        self.install_media_preference(self.panel, "(prefers-contrast: more)")
        wait_js(self.panel, "document.querySelector('.glass-composer').dataset.glassRenderer === 'solid'")
        self.open_row_menu(self.panel)
        self.assertEqual(eval_js(self.panel, "document.querySelector('.row-popover').dataset.glassRenderer"), "solid")
        QTest.keyClick(self.keyboard_target(self.panel), Qt.Key.Key_Home)
        QTest.keyClick(self.keyboard_target(self.panel), Qt.Key.Key_Return)
        wait_js(self.panel, "Boolean(document.querySelector('[data-testid=message-search]'))")
        self.assertEqual(self.chat.sent, [])

    def test_reduced_motion_uses_static_surface_without_refraction_and_still_sends(self):
        self.install_media_preference(self.panel, "(prefers-reduced-motion: reduce)")
        wait_js(self.panel, "document.querySelector('.glass-composer').dataset.glassRenderer === 'static'")
        self.click_web(self.panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(self.panel), "static fallback")
        self.click_web(self.panel, ".send-button")
        wait_until(lambda: self.chat.sent == ["static fallback"])


class GlassSettingsRoundTripTests(GlassInteractions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        startup = SimpleNamespace(available=False, is_enabled=Mock(return_value=False), set_enabled=Mock())
        replacement = patch("desktop_pet.application_controller.StartupManager", return_value=startup)
        replacement.start()
        self.addCleanup(replacement.stop)
        navigation_fixture.EmbeddedSettingsNavigationTests.setUp(self)

    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    def test_glass_draft_survives_real_settings_navigation_and_same_webengine_returns(self):
        self.session.open_chat_window()
        window = self.session.chat_window
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "Boolean(document.querySelector('.glass-composer[data-glass-renderer]'))")
        self.wait_for_renderer_size(panel)
        original_view = panel.web_view
        self.click_web(panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(panel), "draft before settings")
        cid = self.session.chat.current_conversation_id
        spin_until(lambda: panel.web_state()["drafts"].get(cid) == "draft before settings")
        self.ensure_sidebar_open(panel)
        self.click_web(panel, ".sidebar-setting")
        spin_until(lambda: window.settings_page is not None
                   and window.pages.currentWidget() is window.settings_page)
        QTest.mouseClick(window.settings_page.back_button, Qt.MouseButton.LeftButton)
        spin_until(lambda: window.pages.currentWidget() is panel)
        self.assertIs(panel.web_view, original_view)
        self.assertEqual(self.session.chat.current_conversation_id, cid)
        wait_js(panel, "document.querySelector('#message-input').value === 'draft before settings'")
        self.click_web(panel, "#message-input")
        QTest.keyClicks(self.keyboard_target(panel), " and after")
        wait_js(panel, "document.querySelector('#message-input').value === 'draft before settings and after'")
        self.camera_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
