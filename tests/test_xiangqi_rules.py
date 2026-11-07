"""Xiangqi-specific legality, terminal rules, notation and safe save replay."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import random
import re
import subprocess
import unittest

from desktop_pet.games.xiangqi import START_FEN, XiangqiBoard, coordinates, square


def position(pieces, *, turn="red"):
    board = [["."]*9 for _ in range(10)]
    for name, piece in pieces.items():
        row, col = coordinates(name)
        board[row][col] = piece
    ranks = []
    for row in board:
        rank, empty = "", 0
        for piece in row:
            if piece == ".":
                empty += 1
            else:
                if empty:
                    rank += str(empty)
                    empty = 0
                rank += piece
        if empty:
            rank += str(empty)
        ranks.append(rank)
    return XiangqiBoard("/".join(ranks) + (" w" if turn == "red" else " b"))


class XiangqiRulesTests(unittest.TestCase):
    def kings(self, pieces, *, turn="red"):
        return position({"d9": "k", "e0": "K", **pieces}, turn=turn)

    def test_initial_board_has_44_legal_moves_and_known_perft_two(self):
        board = XiangqiBoard()
        self.assertEqual(board.fen, START_FEN)
        self.assertEqual(len(board.legal_moves()), 44)
        count = 0
        for move in board.legal_moves():
            child = board.copy()
            child.push(move)
            count += len(child.legal_moves())
        self.assertEqual(count, 1920)
        self.assertEqual(board.fen, START_FEN)

    def test_iccs_coordinates_round_trip_all_90_intersections(self):
        self.assertEqual(coordinates("a0"), (9, 0))
        self.assertEqual(coordinates("i9"), (0, 8))
        for row in range(10):
            for col in range(9):
                self.assertEqual(coordinates(square(row, col)), (row, col))
        for invalid in ("a10", "j0", "A0", "a-1", "a0b1", "a0\n", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                coordinates(invalid)
        for invalid in ((-1, 0), (10, 0), (0, 9), (True, 2), (2.0, 3)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                square(*invalid)

    def test_horse_leg_blocks_both_jumps_on_that_side(self):
        free = self.kings({"b0": "N"})
        self.assertIn("b0a2", free.legal_moves())
        self.assertIn("b0c2", free.legal_moves())
        blocked = self.kings({"b0": "N", "b1": "P"})
        self.assertNotIn("b0a2", blocked.legal_moves())
        self.assertNotIn("b0c2", blocked.legal_moves())
        self.assertIn("b0d1", blocked.legal_moves())

    def test_elephant_eye_and_river(self):
        free = self.kings({"c0": "B"})
        self.assertIn("c0a2", free.legal_moves())
        self.assertIn("c0e2", free.legal_moves())
        blocked = self.kings({"c0": "B", "b1": "P"})
        self.assertNotIn("c0a2", blocked.legal_moves())
        self.assertIn("c0e2", blocked.legal_moves())
        river = self.kings({"c4": "B"})
        self.assertNotIn("c4e6", river.legal_moves())
        self.assertIn("c4e2", river.legal_moves())

    def test_advisor_and_general_stay_in_their_own_palace(self):
        board = self.kings({"d0": "A"})
        self.assertIn("d0e1", board.legal_moves())
        self.assertNotIn("d0c1", board.legal_moves())
        board = position({"d9": "k", "d0": "K", "d5": "P"})
        self.assertIn("d0e0", board.legal_moves())
        self.assertNotIn("d0c0", board.legal_moves())

    def test_soldier_gains_sideways_moves_only_after_crossing_the_river(self):
        before = self.kings({"a3": "P"})
        self.assertIn("a3a4", before.legal_moves())
        self.assertNotIn("a3b3", before.legal_moves())
        crossed = self.kings({"a5": "P"})
        self.assertIn("a5a6", crossed.legal_moves())
        self.assertIn("a5b5", crossed.legal_moves())
        self.assertNotIn("a5a4", crossed.legal_moves())
        black = self.kings({"i4": "p"}, turn="black")
        self.assertIn("i4i3", black.legal_moves())
        self.assertIn("i4h4", black.legal_moves())
        self.assertNotIn("i4i5", black.legal_moves())

    def test_cannon_needs_exactly_one_screen_to_capture(self):
        board = self.kings({"b2": "C", "b3": "p", "b5": "r", "b7": "p"})
        moves = board.legal_moves()
        self.assertNotIn("b2b3", moves)
        self.assertNotIn("b2b4", moves)
        self.assertIn("b2b5", moves)
        self.assertNotIn("b2b7", moves)
        self.assertIn("b2b1", moves)
        no_screen = self.kings({"b2": "C", "b5": "r"})
        self.assertNotIn("b2b5", no_screen.legal_moves())

    def test_rook_cannot_jump_or_capture_own_piece(self):
        friendly = self.kings({"a0": "R", "a3": "P"})
        self.assertIn("a0a2", friendly.legal_moves())
        self.assertNotIn("a0a3", friendly.legal_moves())
        self.assertNotIn("a0a4", friendly.legal_moves())
        hostile = self.kings({"a0": "R", "a3": "p"})
        self.assertIn("a0a3", hostile.legal_moves())
        self.assertNotIn("a0a4", hostile.legal_moves())

    def test_a_blocker_cannot_expose_facing_generals(self):
        board = position({"e0": "K", "e9": "k", "e4": "R"})
        self.assertFalse(board.in_check("red"))
        self.assertNotIn("e4d4", board.legal_moves())
        self.assertIn("e4e5", board.legal_moves())
        facing = position({"e0": "K", "e9": "k"})
        self.assertTrue(facing.in_check("red"))
        self.assertTrue(facing.in_check("black"))

    def test_check_responses_are_legal_and_unrelated_moves_are_rejected(self):
        board = position({"e0": "K", "d9": "k", "e5": "r", "a0": "R"})
        self.assertTrue(board.in_check())
        self.assertNotIn("a0a1", board.legal_moves())
        self.assertIn("e0f0", board.legal_moves())
        before = board.fen
        with self.assertRaises(ValueError):
            board.push("a0a1")
        self.assertEqual(board.fen, before)

    def test_stalemate_is_a_loss_in_xiangqi(self):
        board = position({"e9": "k", "d0": "K", "d8": "R", "f8": "R"}, turn="black")
        self.assertFalse(board.in_check())
        self.assertEqual(board.legal_moves(), [])
        self.assertEqual(board.outcome(), {"winner": "red", "reason": "stalemate"})

    def test_checkmate_ends_the_game(self):
        board = position({"e9": "k", "d0": "K", "e8": "P", "f8": "P", "d8": "R"}, turn="black")
        self.assertTrue(board.in_check())
        self.assertEqual(board.outcome(), {"winner": "red", "reason": "checkmate"})

    def test_nonchecking_threefold_repetition_draw_can_be_undone(self):
        board = XiangqiBoard()
        cycle = ("b0a2", "b9a7", "a2b0", "a7b9")
        for move in cycle:
            board.push(move)
        self.assertIsNone(board.outcome())
        for move in cycle:
            board.push(move)
        self.assertEqual(board.outcome(), {"winner": "draw", "reason": "repetition"})
        self.assertEqual(board.legal_moves(), [])
        self.assertIsNotNone(board.undo())
        self.assertIsNone(board.outcome())

    def test_sole_perpetual_checker_loses(self):
        board = position({"e9": "k", "f0": "K", "e8": "R"}, turn="black")
        cycle = ("e9d9", "e8d8", "d9e9", "d8e8")
        for move in cycle*2:
            board.push(move)
        self.assertEqual(board.outcome(), {"winner": "black", "reason": "perpetual_check"})

    def test_push_undo_capture_counters_and_notation_restore_exact_position(self):
        board = XiangqiBoard()
        record = board.push("h2e2")
        self.assertEqual(record.label, "炮二平五")
        self.assertEqual(record.side, "red")
        self.assertEqual(record.piece, "C")
        self.assertEqual(record.captured, ".")
        self.assertEqual(board.turn, "black")
        self.assertEqual(board.move_label("h9g7"), "马8进7")
        board.push("h9g7")
        self.assertTrue(board.fen.endswith("2 2"))
        board.undo()
        self.assertEqual(board.undo(), record)
        self.assertEqual(board.fen, START_FEN)
        self.assertIsNone(board.undo())
        with self.assertRaises(FrozenInstanceError):
            record.side = "black"

    def test_duplicate_file_notation_uses_front_and_rear(self):
        board = self.kings({"a0": "R", "a2": "R"})
        self.assertEqual(board.move_label("a2b2"), "前车平八")
        self.assertEqual(board.move_label("a0b0"), "后车平八")

    def test_copy_and_saved_game_replay_preserve_history_without_shared_mutation(self):
        board = XiangqiBoard()
        for move in ("h2e2", "h9g7", "h0g2", "b9c7"):
            board.push(move)
        payload = json.loads(json.dumps(board.to_payload()))
        restored = XiangqiBoard.from_payload(payload)
        self.assertEqual(restored.fen, board.fen)
        self.assertEqual(restored.history, board.history)
        self.assertEqual(restored.moves, board.moves)
        copied = board.copy()
        copied.undo()
        self.assertNotEqual(copied.fen, board.fen)
        self.assertEqual(len(board.moves), 4)
        squares = board.squares
        self.assertIsInstance(squares, tuple)
        with self.assertRaises(TypeError):
            squares[0][0] = "."

    def test_malformed_fen_and_forged_saved_games_are_rejected(self):
        for fen in ("", "9/9 w", START_FEN.replace(" w ", " x "),
                    START_FEN.replace("K", "P"), START_FEN.replace(" 0 1", " -1 1"),
                    START_FEN.replace(" 0 1", " 0 0"), START_FEN.replace(" - - ", " KQ - ")):
            with self.subTest(fen=fen), self.assertRaises(ValueError):
                XiangqiBoard(fen)
        good = XiangqiBoard().to_payload()
        for value in ({}, {**good, "schema_version": True}, {**good, "moves": ["a0a9"]},
                      {**good, "moves": ["h2e2\n"]}, {**good, "moves": ["h2e2"]*4097},
                      {**good, "moves": "h2e2"}, {**good, "fen": START_FEN}):
            with self.subTest(value=str(value)[:50]), self.assertRaises(ValueError):
                XiangqiBoard.from_payload(value)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] /
                         "assets/engines/pikafish/pikafish.exe").is_file(),
                        "Optional official Pikafish executable has not been provisioned")
    def test_real_pikafish_agrees_on_80_continuous_random_game_positions(self):
        engine = Path(__file__).resolve().parents[1] / "assets/engines/pikafish/pikafish.exe"
        board, rng, snapshots = XiangqiBoard(), random.Random(2048), []
        for _ in range(80):
            moves = board.legal_moves()
            snapshots.append((board.fen, set(moves)))
            if not moves:
                break
            board.push(rng.choice(moves))
        commands = "uci\nisready\n" + "".join(
            f"position fen {fen}\ngo perft 1\n" for fen, _ in snapshots) + "quit\n"
        process = subprocess.Popen([str(engine)], cwd=str(engine.parent),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            output, _errors = process.communicate(commands, timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()  # This test's owned engine, never the running desktop app.
            process.communicate()
            self.fail("Owned Pikafish process did not finish the perft comparison")
        self.assertEqual(process.returncode, 0)
        blocks = re.split(r"Nodes searched:\s*\d+\s*", output)
        self.assertGreaterEqual(len(blocks), len(snapshots)+1)
        for index, ((fen, expected), block) in enumerate(zip(snapshots, blocks)):
            actual = set(re.findall(r"^([a-i][0-9][a-i][0-9]):\s*\d+\s*$", block, re.M))
            with self.subTest(index=index, fen=fen):
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
