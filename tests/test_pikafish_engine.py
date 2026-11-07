"""Official local engine, owned subprocess lifecycle and rule agreement."""
import threading
import time
import unittest

from desktop_pet.games.pikafish import EngineCancelled, EngineError, PikafishEngine, resolve_engine_path
from desktop_pet.games.xiangqi import START_FEN, XiangqiBoard


class PikafishEngineTests(unittest.TestCase):
    def setUp(self):
        try:
            resolve_engine_path()
        except EngineError:
            self.skipTest("Official engine package is not installed")
        self.engine = PikafishEngine(hash_mb=16)
        self.addCleanup(self.engine.shutdown)

    def test_handshake_and_initial_perft_agree_with_local_rules(self):
        self.assertIn("Pikafish", self.engine.start())
        moves = self.engine.perft(START_FEN)
        self.assertEqual(len(moves), 44)
        self.assertEqual(set(moves), set(XiangqiBoard().legal_moves()))

    def test_multipv_contains_legal_candidates_and_keeps_the_process(self):
        self.engine.start()
        owned_process = self.engine._process
        board = XiangqiBoard()
        result = self.engine.analyse(START_FEN, movetime_ms=120, multipv=3)
        self.assertTrue(board.is_legal(result.bestmove))
        self.assertGreaterEqual(len(result.lines), 2)
        self.assertTrue(all(board.is_legal(line.move) for line in result.lines))
        board.push(result.bestmove)
        reply = self.engine.analyse(START_FEN, board.moves, movetime_ms=80)
        self.assertTrue(board.is_legal(reply.bestmove))
        self.assertIs(self.engine._process, owned_process)

    def test_later_position_perft_matches_and_uses_complete_history(self):
        board = XiangqiBoard()
        for index in range(8):
            expected = set(board.legal_moves())
            self.assertEqual(set(self.engine.perft(START_FEN, board.moves)), expected)
            board.push(sorted(expected)[(index*7) % len(expected)])

    def test_cancelled_search_does_not_poison_a_later_search(self):
        self.engine.start()
        cancel = threading.Event()
        outcome = []
        def search():
            try:
                self.engine.analyse(START_FEN, movetime_ms=5000, cancellation_event=cancel)
            except EngineCancelled:
                outcome.append("cancelled")
        worker = threading.Thread(target=search)
        worker.start()
        time.sleep(.12)
        cancel.set()
        self.engine.cancel()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["cancelled"])
        self.assertTrue(XiangqiBoard().is_legal(self.engine.analyse(START_FEN, movetime_ms=60).bestmove))

    def test_command_injection_is_rejected_before_process_creation(self):
        with self.assertRaises(EngineError):
            self.engine.analyse(START_FEN+"\nquit", movetime_ms=30)
        self.assertFalse(self.engine.running)
        with self.assertRaises(EngineError):
            self.engine.analyse(START_FEN, ["h2e2\nquit"])

    def test_shutdown_reaps_only_the_owned_process(self):
        self.engine.start()
        process = self.engine._process
        self.engine.shutdown()
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.engine.running)


if __name__ == "__main__":
    unittest.main()
