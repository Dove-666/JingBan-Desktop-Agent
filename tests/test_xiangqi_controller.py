"""Real local matches without network requests or the user's preferences."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.games.pikafish import AnalysisLine, EngineAnalysis, EngineError, resolve_engine_path
from desktop_pet.games.xiangqi import START_FEN
from desktop_pet.games.xiangqi import XiangqiBoard
from desktop_pet.games.xiangqi_controller import XiangqiController


class XiangqiControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        try: resolve_engine_path()
        except EngineError: raise unittest.SkipTest("Official engine package is not installed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-xiangqi-controller-")
        self.path = Path(self.temp.name) / "saved.json"
        self.controllers = []
        self.controller = self.make_controller()

    def make_controller(self):
        controller = XiangqiController(storage_path=self.path)
        self.controllers.append(controller)
        return controller

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        self.wait(lambda: all(not c.engine.running for c in self.controllers), timeout=5)
        for controller in self.controllers:
            controller.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self, predicate, timeout=8):
        end = time.monotonic()+timeout
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= end:
                self.fail("Asynchronous chess operation did not finish")
            time.sleep(.005)
        self.app.processEvents()

    def start(self, controller=None):
        controller = controller or self.controller
        self.assertTrue(controller.start())
        self.wait(lambda: not controller.busy)
        self.assertTrue(controller.engine_ready, controller.error)

    def turn(self, move=None):
        controller = self.controller
        move = move or controller.board.legal_moves()[0]
        self.assertTrue(controller.play_move(move))
        self.wait(lambda: not controller.busy)
        self.assertFalse(controller.error)

    def test_human_and_engine_play_a_real_round_and_undo_restores_both_plies(self):
        self.start()
        self.turn("h2e2")
        self.assertEqual(len(self.controller.board.moves), 2)
        self.assertEqual(self.controller.board.turn, "red")
        self.assertTrue(self.controller.snapshot()["can_undo"])
        self.assertTrue(self.controller.undo())
        self.assertEqual(self.controller.board.fen, START_FEN)
        self.assertEqual(self.controller.snapshot()["moves"], [])

    def test_undo_during_engine_search_rejects_the_late_answer(self):
        self.start()
        self.controller.set_difficulty("serious")
        self.controller.play_move("h2e2")
        self.assertTrue(self.controller.busy)
        self.assertTrue(self.controller.undo())
        self.wait(lambda: not self.controller.engine.running or not self.controller.runner._tasks)
        self.assertEqual(self.controller.board.fen, START_FEN)
        self.assertEqual(self.controller.board.moves, [])
        self.assertFalse(self.controller.busy)
        self.turn("b2e2")

    def test_new_game_cancels_old_search_without_applying_a_stale_move(self):
        self.start()
        self.controller.play_move("h2e2")
        self.assertTrue(self.controller.new_game("red", "casual"))
        self.wait(lambda: not self.controller.runner._tasks)
        self.assertEqual(self.controller.board.fen, START_FEN)
        self.assertEqual(self.controller.difficulty, "casual")
        self.assertFalse(self.controller.busy)

    def test_black_human_starts_after_one_engine_move_and_cannot_undo_the_opening(self):
        self.start()
        self.controller.new_game("black", "casual")
        self.wait(lambda: not self.controller.busy)
        self.assertEqual(len(self.controller.board.moves), 1)
        self.assertEqual(self.controller.board.turn, "black")
        self.assertFalse(self.controller.undo())
        self.turn()
        self.assertEqual(len(self.controller.board.moves), 3)
        self.controller.undo()
        self.assertEqual(len(self.controller.board.moves), 1)
        self.assertEqual(self.controller.board.turn, "black")

    def test_hint_does_not_move_pieces_and_commentary_is_separate_from_engine_busy(self):
        self.start()
        before = self.controller.board.fen
        requests = []
        self.controller.commentary_requested.connect(lambda question, state: requests.append((question, state)))
        self.assertTrue(self.controller.request_hint())
        self.wait(lambda: not self.controller.busy)
        self.assertEqual(self.controller.board.fen, before)
        self.assertTrue(self.controller.board.is_legal(self.controller.snapshot()["hint"]))
        self.assertTrue(requests)
        self.controller.set_commentary_busy(True)
        self.assertFalse(self.controller.busy)
        self.assertTrue(self.controller.snapshot()["commentary_busy"])
        self.assertTrue(self.controller.ask("这一步为什么这样走？"))
        self.assertEqual(requests[-1][0], "这一步为什么这样走？")

    def test_shutdown_and_restart_restore_exact_game_side_and_dialogue(self):
        self.start()
        self.turn("h2e2")
        expected = self.controller.board.fen
        self.controller.post_commentary("这一手很有意思。", "happy")
        self.controller.shutdown()
        self.wait(lambda: not self.controller.engine.running)
        restored = self.make_controller()
        self.start(restored)
        self.assertEqual(restored.board.fen, expected)
        self.assertEqual(len(restored.board.moves), 2)
        self.assertEqual(restored.snapshot()["commentary"][-1]["text"], "这一手很有意思。")

    def test_corrupt_save_is_preserved_and_new_game_remains_playable(self):
        self.path.write_text('{"broken":', encoding="utf-8")
        self.start()
        backups = list(self.path.parent.glob("saved.unreadable-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"broken":')
        self.assertEqual(self.controller.board.fen, START_FEN)
        self.turn("h2e2")

    def test_engine_failure_preserves_human_move_and_retry_can_finish_the_round(self):
        self.start()
        with patch.object(self.controller.engine, "analyse", side_effect=EngineError("计算测试故障。")):
            self.controller.play_move("h2e2")
            self.wait(lambda: not self.controller.busy)
        self.assertEqual(self.controller.board.moves, ["h2e2"])
        self.assertFalse(self.controller.engine_ready)
        self.controller.start()
        self.wait(lambda: self.controller.engine_ready and not self.controller.busy)
        self.assertEqual(len(self.controller.board.moves), 2)

    def test_analysis_keeps_source_fen_and_score_side_after_the_ai_move(self):
        self.start()
        source = XiangqiBoard()
        source.push("h2e2")
        self.turn("h2e2")
        analysis = self.controller.snapshot()["analysis"]
        self.assertEqual(analysis["source_fen"], source.fen)
        self.assertEqual(analysis["side_to_move"], "black")
        self.assertEqual(analysis["score_perspective"], "black")
        self.assertEqual(analysis["purpose"], "ai_move")
        self.assertNotEqual(analysis["source_fen"], self.controller.board.fen)
        self.controller.request_hint()
        self.wait(lambda: not self.controller.busy)
        analysis = self.controller.snapshot()["analysis"]
        self.assertEqual(analysis["source_fen"], self.controller.board.fen)
        self.assertEqual((analysis["side_to_move"], analysis["score_perspective"], analysis["purpose"]),
                         ("red", "red", "hint"))

    def test_casual_selector_captures_settings_on_gui_thread_and_picks_from_the_pool(self):
        self.start()
        self.controller.set_difficulty("casual")
        main_thread = threading.current_thread()
        calls, requests, speech = [], [], []
        self.controller.commentary_requested.connect(lambda question, state: requests.append(question))
        self.controller.speech.connect(lambda text, emotion: speech.append((text, emotion)))
        def factory(state):
            self.assertIs(threading.current_thread(), main_thread)
            self.assertEqual(state["turn"], "black")
            def select(candidates, *, cancellation_event):
                self.assertIsNot(threading.current_thread(), main_thread)
                calls.append(candidates)
                return {"move": candidates[-1]["move"], "reply": "我选这一手，轮到你啦。", "emotion": "happy"}
            return select
        self.controller.move_selector_factory = factory
        self.turn("h2e2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.controller.board.moves[-1], calls[0][-1]["move"])
        self.assertEqual(self.controller.snapshot()["analysis"]["move_source"], "model")
        self.assertEqual(speech, [("我选这一手，轮到你啦。", "happy")])
        self.assertEqual(requests, [], "A selected reply must not trigger a second paid commentary")

    def test_selector_illegal_choice_and_api_failure_fall_back_without_engine_error(self):
        self.start()
        for fail in (False, True):
            with self.subTest(exception=fail):
                self.controller.new_game("red", "casual")
                observed = []
                def factory(state):
                    legal = set(state["legal_moves"])
                    def select(candidates, *, cancellation_event):
                        observed.append({candidate["move"] for candidate in candidates})
                        if fail:
                            raise RuntimeError("Provider request failed with private details")
                        outside = next(iter(legal-observed[-1]), "a0a9")
                        return {"move": outside, "reply": "untrusted reply must not appear", "emotion": "happy"}
                    return select
                self.controller.move_selector_factory = factory
                self.turn("h2e2")
                self.assertIn(self.controller.board.moves[-1], observed[0])
                self.assertEqual(self.controller.snapshot()["analysis"]["move_source"], "engine")
                self.assertTrue(self.controller.engine_ready)
                self.assertEqual(self.controller.error, "")
                self.assertFalse(any("untrusted" in item["text"] for item in self.controller.snapshot()["commentary"]))

    def test_new_game_discards_a_slow_selector_reply_and_observes_cancellation(self):
        self.start()
        self.controller.set_difficulty("casual")
        entered, release, cancelled = threading.Event(), threading.Event(), []
        def factory(_state):
            def select(candidates, *, cancellation_event):
                entered.set()
                release.wait(timeout=5)
                cancelled.append(cancellation_event.is_set())
                return {"move": candidates[0]["move"], "reply": "stale model reply", "emotion": "happy"}
            return select
        self.controller.move_selector_factory = factory
        self.controller.play_move("h2e2")
        try:
            self.wait(entered.is_set)
            self.controller.new_game("red", "standard")
        finally:
            release.set()
        self.wait(lambda: not self.controller.runner._tasks)
        self.assertEqual(cancelled, [True])
        self.assertEqual(self.controller.board.fen, START_FEN)
        self.assertFalse(any("stale" in item["text"] for item in self.controller.snapshot()["commentary"]))

    def test_standard_serious_and_hint_do_not_call_the_model_selector(self):
        self.start()
        calls = []
        self.controller.move_selector_factory = lambda state: calls.append(state)
        for difficulty in ("standard", "serious"):
            self.controller.new_game("red", difficulty)
            self.turn("h2e2")
        self.controller.new_game("red", "casual")
        self.controller.request_hint()
        self.wait(lambda: not self.controller.busy)
        self.assertEqual(calls, [])

    def test_cancelled_old_bad_save_reader_cannot_quarantine_the_new_game_save(self):
        self.path.write_text('{"broken":', encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        original_loads = json.loads
        def delayed_parse(content, *args, **kwargs):
            if content == '{"broken":':
                entered.set()
                release.wait(timeout=5)
            return original_loads(content, *args, **kwargs)
        with patch("desktop_pet.games.xiangqi_controller.json.loads", side_effect=delayed_parse):
            self.controller.start()
            try:
                self.wait(entered.is_set)
                self.controller.new_game("red", "casual")
            finally:
                release.set()
            self.wait(lambda: not self.controller.busy and not self.controller.runner._tasks)
        self.assertTrue(self.controller.engine_ready, self.controller.error)
        self.assertEqual(list(self.path.parent.glob("saved.unreadable-*.json")), [])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["difficulty"], "casual")
        self.assertEqual(saved["board"]["moves"], [])
        self.assertFalse(self.controller._save_state.blocked)

    def test_undo_and_difficulty_change_can_recover_a_failed_engine_without_reopening(self):
        self.start()
        for operation in ("undo", "difficulty"):
            with self.subTest(operation=operation):
                self.controller.new_game("red", "standard")
                with patch.object(self.controller.engine, "analyse", side_effect=EngineError("计算测试故障。")):
                    self.controller.play_move("h2e2")
                    self.wait(lambda: not self.controller.busy)
                self.assertFalse(self.controller.engine_ready)
                if operation == "undo":
                    self.controller.undo()
                else:
                    self.controller.set_difficulty("casual")
                self.wait(lambda: self.controller.engine_ready and not self.controller.busy)
                self.assertEqual(self.controller.error, "")
                if operation == "undo":
                    self.assertEqual(self.controller.board.fen, START_FEN)
                else:
                    self.assertEqual(self.controller.difficulty, "casual")
                    self.assertEqual(len(self.controller.board.moves), 2)

    def test_restoring_saved_board_changes_revision_before_publishing_it(self):
        board = XiangqiBoard()
        board.push("h2e2")
        board.push("h9g7")
        self.path.write_text(json.dumps({
            "schema_version": 1, "board": board.to_payload(), "human_side": "red",
            "difficulty": "standard", "commentary": [], "result": None,
        }), encoding="utf-8")
        old_snapshot = self.controller.snapshot()
        published = []
        self.controller.changed.connect(published.append)
        self.start()
        restored = self.controller.snapshot()
        self.assertEqual(restored["fen"], board.fen)
        self.assertGreater(restored["revision"], old_snapshot["revision"])
        self.assertTrue(all(state["revision"] != old_snapshot["revision"]
                            for state in published if state["fen"] == board.fen))

    def test_chat_and_shutdown_before_restore_do_not_overwrite_the_existing_game(self):
        board = XiangqiBoard()
        board.push("h2e2")
        board.push("h9g7")
        original = json.dumps({
            "schema_version": 1, "board": board.to_payload(), "human_side": "red",
            "difficulty": "standard", "commentary": [], "result": None,
        })
        self.path.write_text(original, encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        def delayed_start(**_kwargs):
            entered.set()
            release.wait(timeout=5)
            raise EngineError("启动测试故障。")
        with patch.object(self.controller.engine, "start", side_effect=delayed_start):
            self.controller.start()
            try:
                self.wait(entered.is_set)
                self.assertTrue(self.controller.ask("上一盘我走到哪里了？"))
                self.controller.post_commentary("正在准备棋局。")
                self.wait(lambda: len(self.controller.runner._tasks) == 1)
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)
            finally:
                release.set()
                self.wait(lambda: not self.controller.busy)
        self.controller.shutdown()
        self.wait(lambda: not self.controller.runner._tasks)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_explicit_new_game_can_save_before_the_first_restore(self):
        self.path.write_text('{"old": "save"}', encoding="utf-8")
        with patch.object(self.controller.engine, "start", side_effect=EngineError("启动测试故障。")):
            self.controller.new_game("red", "casual")
            self.wait(lambda: not self.controller.busy and not self.controller.runner._tasks)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["board"]["moves"], [])
        self.assertEqual(saved["difficulty"], "casual")
        self.controller.shutdown()
        self.wait(lambda: not self.controller.runner._tasks)

    def test_restored_check_remains_explicit_after_hint_and_difficulty_status_changes(self):
        board = XiangqiBoard("3k5/9/9/9/4r4/9/9/9/9/R3K4 w")
        self.path.write_text(json.dumps({
            "schema_version": 1, "board": board.to_payload(), "human_side": "red",
            "difficulty": "standard", "commentary": [], "result": None,
        }), encoding="utf-8")
        self.start()
        self.assertEqual(self.controller.snapshot()["checked_side"], "red")
        self.assertEqual(self.controller.status, "上次的棋局已恢复。")
        self.assertTrue(self.controller.request_hint())
        self.wait(lambda: not self.controller.busy)
        self.assertIsNotNone(self.controller.snapshot()["hint"])
        self.assertEqual(self.controller.snapshot()["checked_side"], "red")
        self.controller.set_difficulty("casual")
        self.assertEqual(self.controller.snapshot()["checked_side"], "red")
        self.controller.board.push("e0f0")
        self.assertIsNone(self.controller.snapshot()["checked_side"])

    def test_last_move_details_come_from_the_actual_capture_and_clear_on_undo(self):
        self.controller.board = XiangqiBoard("3k5/9/9/9/9/9/p8/9/9/R3K4 w")
        self.assertIsNone(self.controller.snapshot()["last_move_details"])
        record = self.controller.board.push("a0a3")
        self.assertEqual(self.controller.snapshot()["last_move_details"], {
            "move": "a0a3", "label": record.label, "side": "red", "piece": "R",
            "captured": "p", "check": False,
        })
        self.controller.board.undo()
        self.assertIsNone(self.controller.snapshot()["last_move_details"])

    def test_quiet_model_choice_does_not_create_a_canned_move_announcement(self):
        self.start()
        self.controller.set_difficulty("casual")
        replies, requests = [], []
        self.controller.speech.connect(lambda text, emotion: replies.append(text))
        self.controller.commentary_requested.connect(lambda question, state: requests.append(question))
        def factory(state):
            source = XiangqiBoard(state["fen"])
            def select(candidates, *, cancellation_event):
                for candidate in candidates:
                    child = source.copy()
                    child.push(candidate["move"])
                    if not child.in_check() and child.outcome() is None:
                        return {"move": candidate["move"], "reply": "", "emotion": "neutral"}
                self.fail("Expected a routine legal opening candidate")
            return select
        self.controller.move_selector_factory = factory
        self.turn("h2e2")
        self.assertEqual(self.controller.snapshot()["analysis"]["move_source"], "model")
        self.assertEqual(self.controller.snapshot()["commentary"], [])
        self.assertEqual(replies, [])
        self.assertEqual(requests, [])

    def test_quiet_checking_move_still_has_a_short_alert_and_finished_games_clear_it(self):
        self.controller.board = XiangqiBoard("3k5/9/9/9/5r3/9/9/9/9/4K4 b")
        self.controller.engine_ready = True
        self.controller.difficulty = "casual"
        self.controller.move_selector_factory = lambda state: (
            lambda candidates, cancellation_event: {"move": "f5e5", "reply": "", "emotion": "neutral"})
        analysis = EngineAnalysis("f5e5", (AnalysisLine("f5e5", 100),), "test engine")
        with patch.object(self.controller.engine, "analyse", return_value=analysis):
            self.controller._start_ai_if_needed()
            self.wait(lambda: not self.controller.busy)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["checked_side"], "red")
        self.assertTrue(snapshot["last_move_details"]["check"])
        self.assertEqual(snapshot["commentary"][-1]["text"], "将军。")
        self.controller.resign()
        self.assertIsNone(self.controller.snapshot()["checked_side"])


if __name__ == "__main__":
    unittest.main()
