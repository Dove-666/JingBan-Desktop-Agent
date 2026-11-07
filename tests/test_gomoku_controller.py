"""Asynchronous match lifetimes and saved-game protection with a local engine fixture."""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.games.gomoku import GomokuBoard
from desktop_pet.games.gomoku_controller import GomokuController
from desktop_pet.games.pikafish import AnalysisLine, EngineCancelled, EngineError
from desktop_pet.games.rapfi import EngineAnalysis


class LocalEngine:
    def __init__(self, _path=None):
        self.running = False
        self.calls = []
        self.start_error = False
        self.analyse_error = False
        self.delay = .015
        self.started = None
        self.release = None
        self.ignore_cancellation = False
        self.forced_move = None

    def start(self, *, cancellation_event=None):
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(5)
        if self.start_error:
            raise EngineError("引擎准备测试故障。")
        if cancellation_event and cancellation_event.is_set():
            raise EngineCancelled("已取消。")
        self.running = True
        return "Local Rapfi fixture"

    def analyse(self, rows, *, turn="black", rule="freestyle", movetime_ms=500, multipv=1,
                cancellation_event=None):
        self.calls.append({"board": rows, "turn": turn, "rule": rule, "multipv": multipv})
        time.sleep(self.delay)
        if not self.ignore_cancellation and cancellation_event.is_set():
            raise EngineCancelled("已取消。")
        if self.analyse_error:
            raise EngineError("引擎计算测试故障。")
        moves = [f"{chr(65+column)}{row+1}" for row in range(15) for column in range(15)
                 if rows[row][column] == "."]
        best = self.forced_move or ("H8" if "H8" in moves else moves[0])
        moves = [best]+[move for move in moves if move != best]
        lines = tuple(AnalysisLine(move, 100-30*index, depth=5, pv=(move,))
                      for index, move in enumerate(moves[:multipv]))
        return EngineAnalysis(best, lines, "Local Rapfi fixture", turn)

    def cancel(self):
        pass

    def shutdown(self):
        self.running = False


class GomokuControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-gomoku-controller-")
        self.path = Path(self.temp.name)/"saved.json"
        self.controllers = []
        self.engine_patch = patch("desktop_pet.games.gomoku_controller.RapfiEngine", LocalEngine)
        self.engine_patch.start()
        self.game = self.make_game()

    def tearDown(self):
        for game in self.controllers:
            game.shutdown()
        self.wait(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for game in self.controllers:
            game.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.engine_patch.stop()
        self.temp.cleanup()

    def make_game(self):
        game = GomokuController(storage_path=self.path)
        self.controllers.append(game)
        return game

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            QTest.qWait(5)
        self.assertTrue(predicate(), "Gomoku operation did not settle")

    def start(self, game=None):
        game = game or self.game
        self.assertTrue(game.start())
        self.wait(lambda: not game.busy)
        self.assertTrue(game.engine_ready, game.error)

    def settle(self):
        self.wait(lambda: not self.game.busy and not self.game.runner._tasks)

    def round(self, move="H8"):
        self.assertTrue(self.game.play_move(move))
        self.settle()

    def test_human_engine_round_undo_and_hint_have_consistent_turns(self):
        self.start()
        self.assertEqual(len(self.game.snapshot()["legal_moves"]), 225)
        self.round()
        self.assertEqual(self.game.board.moves[0], "H8")
        self.assertEqual(len(self.game.board.moves), 2)
        before = self.game.board.position_key
        self.assertTrue(self.game.request_hint())
        self.settle()
        self.assertEqual(self.game.board.position_key, before)
        self.assertTrue(self.game.board.is_legal(self.game.snapshot()["hint"]))
        self.assertEqual(self.game.snapshot()["analysis"]["score_unit"], "rapfi")
        self.assertTrue(self.game.undo())
        self.assertEqual(self.game.board.moves, [])
        self.assertEqual(self.game.board.turn, "black")

    def test_white_human_waits_for_black_opening_and_undo_keeps_opening(self):
        self.start()
        self.game.new_game("white", "casual", "standard")
        self.settle()
        opening = list(self.game.board.moves)
        self.assertEqual(len(opening), 1)
        self.assertEqual(self.game.board.turn, "white")
        self.assertFalse(self.game.undo())
        self.round("H9")
        self.assertEqual(len(self.game.board.moves), 3)
        self.game.undo()
        self.assertEqual(self.game.board.moves, opening)

    def test_undo_and_new_game_ignore_late_search_results(self):
        self.start()
        self.game.engine.delay = .12
        self.game.engine.ignore_cancellation = True
        for action in ("undo", "new"):
            with self.subTest(action=action):
                self.assertTrue(self.game.play_move("H8"))
                self.assertTrue(self.game.busy)
                if action == "undo":
                    self.game.undo()
                else:
                    self.game.new_game("black", "casual", "standard")
                self.wait(lambda: not self.game.runner._tasks)
                self.assertEqual(self.game.board.moves, [])
                self.assertFalse(self.game.busy)
        self.assertEqual(self.game.rule, "standard")

    def test_restore_preserves_position_rule_side_commentary_and_changes_identity(self):
        self.start()
        self.game.new_game("black", "casual", "standard")
        self.round()
        self.game.ask("这一步我还想再看看。")
        self.game.post_commentary("可以，先看看中间的连接。", "thinking")
        self.settle()
        expected = self.game.snapshot()
        self.game.shutdown()
        self.wait(lambda: not self.game.engine.running)
        restored = self.make_game()
        revision = restored.revision
        self.start(restored)
        actual = restored.snapshot()
        for key in ("position_key", "human_side", "difficulty", "rule", "commentary", "moves"):
            self.assertEqual(actual[key], expected[key])
        self.assertGreater(actual["revision"], revision)

    def test_selector_may_stay_quiet_and_cannot_escape_the_candidate_pool(self):
        self.start()
        self.game.set_difficulty("casual")
        replies = []
        self.game.speech.connect(lambda text, emotion: replies.append(text))
        choices = []

        def factory(state):
            self.assertEqual(state["turn"], "white")
            def select(candidates, *, cancellation_event):
                choices.extend(row["move"] for row in candidates)
                return {"move": candidates[-1]["move"], "reply": "", "emotion": "happy"}
            return select
        self.game.move_selector_factory = factory
        self.round()
        self.assertEqual(self.game.board.moves[-1], choices[-1])
        self.assertEqual(self.game.snapshot()["analysis"]["move_source"], "model")
        self.assertEqual(replies, [])
        self.game.undo()
        self.game.move_selector_factory = lambda state: lambda *args, **kwargs: {
            "move": "O15", "reply": "非法候选不能发布。", "emotion": "angry"}
        self.round()
        self.assertNotEqual(self.game.board.moves[-1], "O15")
        self.assertEqual(self.game.snapshot()["analysis"]["move_source"], "engine")
        self.assertNotIn("非法候选不能发布。", replies)

    def test_engine_failure_recovers_by_undo_or_difficulty_change(self):
        self.start()
        self.game.engine.analyse_error = True
        self.round()
        self.assertFalse(self.game.engine_ready)
        self.assertEqual(self.game.board.moves, ["H8"])
        self.game.engine.analyse_error = False
        self.game.undo()
        self.settle()
        self.assertTrue(self.game.engine_ready)
        self.round()
        self.game.engine.analyse_error = True
        self.assertTrue(self.game.request_hint())
        self.settle()
        self.game.engine.analyse_error = False
        self.game.set_difficulty("casual")
        self.settle()
        self.assertTrue(self.game.engine_ready)
        self.assertEqual(len(self.game.board.moves), 2)

    def test_occupied_or_wrong_turn_move_does_not_change_the_position(self):
        self.start()
        self.game.engine.delay = .1
        self.assertTrue(self.game.play_move("H8"))
        before = self.game.board.position_key
        self.assertFalse(self.game.play_move("I8"))
        self.assertEqual(self.game.board.position_key, before)
        self.settle()
        before = self.game.board.position_key
        self.assertFalse(self.game.play_move("H8"))
        self.assertFalse(self.game.play_move("Q1"))
        self.assertEqual(self.game.board.position_key, before)

    def test_true_threat_and_winning_line_come_from_rules_not_status_text(self):
        self.start()
        replies = []
        self.game.speech.connect(lambda text, emotion: replies.append((text, emotion)))
        self.game.board = GomokuBoard(moves=("A1", "A15", "B1", "C15", "C1", "E15", "D1", "G15"))
        snapshot = self.game.snapshot()
        self.assertEqual(snapshot["threat"], {"side": "black", "moves": ["E1"]})
        self.assertTrue(self.game.play_move("E1"))
        snapshot = self.game.snapshot()
        self.assertEqual(snapshot["outcome"], {"winner": "black", "reason": "five"})
        self.assertEqual(snapshot["winning_line"], ["A1", "B1", "C1", "D1", "E1"])
        self.assertFalse(snapshot["legal_moves"])
        self.assertIsNone(snapshot["threat"])
        self.assertFalse(self.game.request_hint())
        self.assertFalse(self.game.play_move("F1"))
        self.assertEqual(replies[-1][1], "surprised")
        self.assertIn("你赢了", replies[-1][0])
        self.assertTrue(self.game.undo())
        self.assertIsNone(self.game.snapshot()["outcome"])

    def test_resign_and_rule_change_create_explicit_supported_games(self):
        self.start()
        self.round()
        self.assertTrue(self.game.resign())
        self.assertEqual(self.game.snapshot()["outcome"], {"winner": "white", "reason": "resign"})
        self.assertFalse(self.game.play_move("I8"))
        self.assertTrue(self.game.set_rule("standard"))
        self.assertEqual(self.game.rule, "standard")
        self.assertEqual(self.game.board.moves, [])
        self.assertFalse(self.game.set_rule("renju"))
        self.assertEqual(self.game.rule, "standard")

    def test_restored_resignation_does_not_claim_five_stones_were_connected(self):
        self.start()
        self.round()
        self.game.resign()
        self.settle()
        self.game.shutdown()
        self.wait(lambda: not self.game.engine.running)
        restored = self.make_game()
        self.start(restored)
        state = restored.snapshot()
        self.assertEqual(state["outcome"], {"winner": "white", "reason": "resign"})
        self.assertIn("认输", state["status"])
        self.assertIn("结束", state["status"])
        self.assertNotIn("连成", state["status"])
        self.assertNotIn("五子", state["status"])
        self.assertEqual(state["winning_line"], [])
        self.assertFalse(state["legal_moves"])

    def test_chat_and_shutdown_before_initial_restore_cannot_replace_existing_save(self):
        original = json.dumps({"schema_version": 1, "board": GomokuBoard(moves=("H8", "I8")).to_payload(),
                               "human_side": "black", "difficulty": "standard", "commentary": [], "result": None})
        self.path.write_text(original, encoding="utf-8")
        self.game.engine.started, self.game.engine.release = threading.Event(), threading.Event()
        self.game.engine.start_error = True
        self.game.start()
        self.wait(self.game.engine.started.is_set)
        self.game.ask("上次的棋局还在吗？")
        self.game.post_commentary("正在准备棋盘。")
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.game.engine.release.set()
        self.settle()
        self.game.shutdown()
        self.wait(lambda: not self.game.runner._tasks)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_corrupt_save_is_preserved_separately_and_can_start_new_match(self):
        self.path.write_text('{"bad": "old game"}', encoding="utf-8")
        self.start()
        backups = list(self.path.parent.glob("saved.unreadable-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"bad": "old game"}')
        self.game.new_game("black", "casual")
        self.settle()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["difficulty"], "casual")
        self.assertFalse(self.game._save_state.blocked)

    def test_cancelled_corrupt_restore_cannot_quarantine_a_new_match_save(self):
        original = '{"bad": "old game"}'
        self.path.write_text(original, encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        loads = json.loads

        def delayed_parse(content, *args, **kwargs):
            if content == original:
                entered.set()
                release.wait(5)
            return loads(content, *args, **kwargs)

        with patch("desktop_pet.games.gomoku_controller.json.loads", side_effect=delayed_parse):
            self.game.start()
            self.wait(entered.is_set)
            try:
                self.game.new_game("black", "casual", "standard")
            finally:
                release.set()
            self.settle()
        self.assertEqual(list(self.path.parent.glob("saved.unreadable-*.json")), [])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["board"]["rule"], "standard")
        self.assertEqual(saved["board"]["moves"], [])
        self.assertTrue(self.game.engine_ready)

    def test_illegal_engine_move_does_not_overwrite_an_occupied_intersection(self):
        self.start()
        self.game.engine.forced_move = "H8"
        self.round("H8")
        self.assertEqual(self.game.board.moves, ["H8"])
        self.assertFalse(self.game.engine_ready)
        self.assertTrue(self.game.error)
        self.assertEqual(self.game.board.squares[7][7], "X")

    def test_changed_external_save_is_not_quarantined_or_replaced_by_placeholder_chat(self):
        original = '{"bad": "old game"}'
        self.path.write_text(original, encoding="utf-8")
        valid = json.dumps({"schema_version": 1, "board": GomokuBoard(moves=("H8", "I8")).to_payload(),
                            "human_side": "black", "difficulty": "standard", "commentary": [], "result": None})
        loads = json.loads
        def external_replacement(content, *args, **kwargs):
            if content == original:
                self.path.write_text(valid, encoding="utf-8")
            return loads(content, *args, **kwargs)
        with patch("desktop_pet.games.gomoku_controller.json.loads", side_effect=external_replacement):
            self.start()
        self.assertTrue(self.game._save_state.blocked)
        self.game.ask("等会儿再继续。")
        self.game.post_commentary("好。")
        self.settle()
        self.assertEqual(self.path.read_text(encoding="utf-8"), valid)
        self.assertEqual(list(self.path.parent.glob("saved.unreadable-*.json")), [])

    def test_white_human_also_receives_periodic_commentary_after_ai_turns(self):
        self.start()
        requests = []
        self.game.commentary_requested.connect(lambda question, state: requests.append((question, state)))
        self.game.new_game("white", "standard")
        self.settle()
        for move in ("H9", "I9"):
            self.round(move)
        self.assertEqual(len(self.game.board.moves), 5)
        self.assertTrue(any(question == "" for question, state in requests))

    def test_export_includes_portable_moves_and_final_position(self):
        self.start()
        self.round()
        destination = self.path.parent/"record.txt"
        self.game.export_record(destination)
        self.wait(destination.exists)
        self.wait(lambda: not self.game.runner._tasks)
        text = destination.read_text(encoding="utf-8")
        self.assertIn("H8", text)
        self.assertIn(self.game.board.position_key, text)
        self.assertIn('"game": "gomoku"', text)


if __name__ == "__main__":
    unittest.main()
