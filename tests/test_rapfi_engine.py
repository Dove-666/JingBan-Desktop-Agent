"""Real official Rapfi rules, candidate consistency and owned-process recovery."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from desktop_pet.games.rapfi import (
    EngineCancelled, EngineError, RapfiEngine, resolve_engine_path,
)
from tools.setup_rapfi import install


def position(black=(), white=()):
    rows = [list("."*15) for _ in range(15)]
    for symbol, moves in (("X", black), ("O", white)):
        for move in moves:
            rows[int(move[1:])-1][ord(move[0])-65] = symbol
    return rows


class RapfiEngineTests(unittest.TestCase):
    def setUp(self):
        try:
            self.path = resolve_engine_path()
        except EngineError:
            self.skipTest("Official Rapfi package is not installed")
        self.engine = RapfiEngine(hash_mb=16)
        self.addCleanup(self.engine.shutdown)

    def assert_available(self, move, rows):
        self.assertRegex(move, r"^[A-O](?:[1-9]|1[0-5])$")
        self.assertEqual(rows[int(move[1:])-1][ord(move[0])-65], ".")

    def test_official_manifest_matches_installed_executable_and_weights(self):
        root = self.path.parent
        manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["version"], "250615")
        self.assertEqual(manifest["archive_sha256"],
                         "1a3e24024062a153ac079060ee9589a37c6bdd1ecc54fed3908793c519594e05")
        self.assertIn("GPL-3.0", manifest["license"])
        self.assertIn("Copying.txt", manifest["files"])
        for name, expected in manifest["files"].items():
            data = (root/name).read_bytes()
            self.assertEqual(len(data), expected["bytes"], name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected["sha256"], name)

    def test_handshake_and_empty_black_opening(self):
        self.assertIn("Rapfi", self.engine.start())
        result = self.engine.analyse(position(), movetime_ms=60, multipv=4)
        self.assertEqual(result.bestmove, "H8")
        self.assertEqual(result.to_dict()["score_unit"], "rapfi")
        self.assertEqual(result.score_perspective, "black")

    def test_black_and_white_immediate_wins_preserve_color_and_top_coordinates(self):
        cases = (
            ("black", position(["D8", "E8", "F8", "G8"], ["D4", "F4", "H4", "J4"])),
            ("white", position(["A1", "C1", "E1", "G1", "I1"], ["D8", "E8", "F8", "G8"])),
        )
        for turn, rows in cases:
            with self.subTest(turn=turn):
                result = self.engine.analyse(rows, turn=turn, rule="standard", movetime_ms=120)
                self.assertIn(result.bestmove, {"C8", "H8"})
                self.assertEqual(result.score_perspective, turn)

    def test_blocks_opponent_four_instead_of_reversing_sides(self):
        rows = position(["C8", "A1", "C1", "E1"], ["D8", "E8", "F8", "G8"])
        result = self.engine.analyse(rows, movetime_ms=250, multipv=4)
        self.assertEqual(result.bestmove, "H8")

    def test_rule_switch_distinguishes_six_from_exactly_five(self):
        rows = position(["C8", "D8", "E8", "G8", "H8"], ["C6", "E6", "G6", "I6", "K6"])
        freestyle = self.engine.analyse(rows, rule="freestyle", movetime_ms=250)
        standard = self.engine.analyse(rows, rule="standard", movetime_ms=250)
        self.assertEqual(freestyle.bestmove, "F8")
        self.assertNotEqual(standard.bestmove, "F8")
        again = self.engine.analyse(rows, rule="freestyle", movetime_ms=100)
        self.assertEqual(again.bestmove, "F8")

    def test_repeated_positions_keep_owned_process_and_coherent_candidates(self):
        rows = position(["H8"])
        turn = "white"
        self.engine.start()
        process = self.engine._process
        for index in range(12):
            result = self.engine.analyse(rows, turn=turn, rule="standard" if index%2 else "freestyle",
                                         movetime_ms=70, multipv=4)
            self.assert_available(result.bestmove, rows)
            self.assertIs(self.engine._process, process)
            self.assertEqual(result.lines[0].move, result.bestmove)
            self.assertEqual(len({line.move for line in result.lines}), len(result.lines))
            self.assertEqual(len({line.depth for line in result.lines}), 1)
            for line in result.lines:
                self.assert_available(line.move, rows)
            rows[int(result.bestmove[1:])-1][ord(result.bestmove[0])-65] = "X" if turn == "black" else "O"
            turn = "white" if turn == "black" else "black"

    def test_cancelled_search_reaps_owned_process_and_cannot_poison_next_search(self):
        self.engine.start()
        old_process = self.engine._process
        search_started = threading.Event()
        cancel = threading.Event()
        outcome = []
        original_send = self.engine._send

        def send(command):
            original_send(command)
            if command.startswith("YXNBEST "):
                search_started.set()
        self.engine._send = send

        def search():
            try:
                self.engine.analyse(position(["H8"]), turn="white", movetime_ms=5000,
                                     multipv=4, cancellation_event=cancel)
                outcome.append("unexpected result")
            except Exception as error:
                outcome.append(error)
        worker = threading.Thread(target=search)
        worker.start()
        self.assertTrue(search_started.wait(timeout=3))
        time.sleep(.06)
        cancel.set()
        self.engine.cancel()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], EngineCancelled)
        self.assertIsNotNone(old_process.poll())
        result = self.engine.analyse(position(), movetime_ms=50)
        self.assertEqual(result.bestmove, "H8")
        self.assertIsNot(self.engine._process, old_process)

    def test_pre_cancel_and_invalid_input_never_launch_process(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(EngineCancelled):
            self.engine.analyse(position(), cancellation_event=cancel)
        cases = (
            (position(), {"rule": "unverified-rule"}),
            (position(), {"turn": "black\nEND"}),
            (position(["H8"]), {"turn": "black"}),
            (["."*15]*14, {}),
            (["."*15]*14+["."*14+"\nEND"], {}),
            ([["X" if (x+y*15)%2 == 0 else "O" for x in range(15)] for y in range(15)],
             {"turn": "white"}),
        )
        for rows, options in cases:
            with self.assertRaises(EngineError):
                self.engine.analyse(rows, **options)
            self.assertFalse(self.engine.running)

    def test_crash_then_restart_and_shutdown_reap_owned_process(self):
        self.engine.start()
        crashed = self.engine._process
        crashed.terminate()
        crashed.wait(timeout=3)
        result = self.engine.analyse(position(), movetime_ms=50)
        self.assertEqual(result.bestmove, "H8")
        replacement = self.engine._process
        self.assertIsNot(replacement, crashed)
        self.engine.shutdown()
        self.assertIsNotNone(replacement.poll())
        self.assertFalse(self.engine.running)

    def test_official_forbidden_query_reports_black_double_three(self):
        rows = position(["H7", "H9", "G8", "I8"], ["A1", "C1", "E1", "G1"])
        self.assertIn("H8", self.engine.forbidden_moves(rows))
        self.assertEqual(self.engine.forbidden_moves(position(["H8"]), turn="white"), ())


@unittest.skipUnless(os.name == "nt", "The installer provisions Windows binaries")
class RapfiInstallerTests(unittest.TestCase):
    def test_refuses_another_engine_directory_without_touching_it(self):
        with tempfile.TemporaryDirectory(prefix="aura-rapfi-install-") as directory:
            target = Path(directory)
            marker = target/"manifest.json"
            marker.write_text('{"name":"Pikafish","entry":"pikafish.exe"}', encoding="utf-8")
            original = marker.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "其他引擎"):
                install(target)
            self.assertEqual(marker.read_bytes(), original)
            self.assertEqual({path.name for path in target.iterdir()}, {"manifest.json"})

    def test_rejects_wrong_archive_before_installing_program(self):
        with tempfile.TemporaryDirectory(prefix="aura-rapfi-install-") as directory:
            root = Path(directory)
            archive = root/"wrong.7z"
            archive.write_bytes(b"not the official release")
            target = root/"rapfi"
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                install(target, archive)
            self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
