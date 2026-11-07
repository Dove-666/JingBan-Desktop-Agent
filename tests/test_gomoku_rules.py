"""Five-in-a-row geometry, exact-five rules and validated persistent replay."""
import unittest

from desktop_pet.games.gomoku import GomokuBoard, coordinates, square


class GomokuRulesTests(unittest.TestCase):
    def alternate(self, black, white=None, rule="freestyle"):
        white = white or [f"{column}15" for column in "ACEGIKM"]
        board = GomokuBoard(rule)
        for index, move in enumerate(black):
            board.push(move)
            if index < len(black)-1:
                board.push(white[index])
        return board

    def test_all_225_coordinates_round_trip_including_i_and_edges(self):
        for row in range(15):
            for column in range(15):
                self.assertEqual(coordinates(square(row, column)), (row, column))
        self.assertEqual(coordinates("i15"), (14, 8))
        board = GomokuBoard()
        self.assertEqual(len(board.legal_moves()), 225)
        for move in ("A0", "P1", "A16", "A01", " H8", "H8x", "", None):
            self.assertFalse(board.is_legal(move))
        for point in ((-1, 0), (0, 15), (True, 0), (0, 1.5)):
            with self.assertRaises(ValueError):
                square(*point)

    def test_duplicate_occupied_move_cannot_change_board_or_turn(self):
        board = GomokuBoard()
        record = board.push("h8")
        self.assertEqual((record.move, record.side, record.piece), ("H8", "black", "X"))
        before = board.position_key
        with self.assertRaises(ValueError):
            board.push("H8")
        self.assertEqual(board.position_key, before)
        self.assertEqual(board.turn, "white")

    def test_black_five_wins_in_all_four_directions(self):
        for black in (["A1", "B1", "C1", "D1", "E1"],
                      ["A1", "A2", "A3", "A4", "A5"],
                      ["A1", "B2", "C3", "D4", "E5"],
                      ["E1", "D2", "C3", "B4", "A5"]):
            with self.subTest(black=black):
                board = self.alternate(black)
                self.assertEqual(board.outcome(), {"winner": "black", "reason": "five"})
                self.assertEqual(set(board.winning_line), set(black))
                self.assertEqual(board.legal_moves(), ())
                with self.assertRaises(ValueError):
                    board.push("O1")

    def test_white_can_win_at_the_board_edge(self):
        board = GomokuBoard()
        for black, white in zip(("A1", "C1", "E1", "G1", "I1"),
                                ("O11", "O12", "O13", "O14", "O15")):
            board.push(black)
            board.push(white)
        self.assertEqual(board.outcome(), {"winner": "white", "reason": "five"})
        self.assertEqual(board.winning_line, ["O11", "O12", "O13", "O14", "O15"])

    def test_gap_filling_six_wins_freestyle_but_does_not_win_standard(self):
        black = ["A1", "B1", "C1", "E1", "F1", "D1"]
        freestyle = self.alternate(black)
        standard = self.alternate(black, rule="standard")
        self.assertEqual(freestyle.outcome()["winner"], "black")
        self.assertEqual(len(freestyle.winning_line), 6)
        self.assertIsNone(standard.outcome())
        self.assertEqual(standard.winning_line, [])
        self.assertTrue(standard.is_legal("O1"))

    def test_standard_exact_five_and_crossing_an_overline_can_still_win(self):
        board = self.alternate(["A1", "B1", "C1", "D1", "E1"], rule="standard")
        self.assertEqual(board.outcome()["winner"], "black")
        crossing = self.alternate(["B4", "C4", "D4", "F4", "G4", "E1", "E2", "E3", "E5", "E4"],
                                  ["A15", "C15", "E15", "G15", "I15", "K15", "M15", "O15", "A13"],
                                  rule="standard")
        self.assertEqual(crossing.winning_line, ["E1", "E2", "E3", "E4", "E5"])
        # The winning-move query is side-specific and keeps every game field.
        board = self.alternate(["D4", "E4", "F4", "G4"], rule="standard")
        before = (board.position_key, board.to_payload(), list(board.history))
        self.assertEqual(set(board.winning_moves("black")), {"C4", "H4"})
        self.assertEqual((board.position_key, board.to_payload(), list(board.history)), before)
        self.assertEqual(board.winning_moves("white"), ())

    def test_standard_winning_query_does_not_recommend_an_overline(self):
        board = self.alternate(["A1", "B1", "C1", "E1", "F1"], rule="standard")
        self.assertNotIn("D1", board.winning_moves("black"))
        board.push("O15")
        board.push("D1")
        self.assertIsNone(board.outcome())

    def test_full_board_without_five_is_a_draw(self):
        black, white = [], []
        for row in range(15):
            for column in range(15):
                (black if (row//2+column) % 2 == 0 else white).append(square(row, column))
        self.assertEqual((len(black), len(white)), (113, 112))
        board = GomokuBoard()
        for index, move in enumerate(black):
            board.push(move)
            if index < len(white):
                board.push(white[index])
        self.assertEqual(board.outcome(), {"winner": "draw", "reason": "full_board"})

    def test_undo_terminal_move_restores_turn_legal_moves_and_win_queries(self):
        board = self.alternate(["A1", "B1", "C1", "D1", "E1"])
        record = board.undo()
        self.assertEqual(record.move, "E1")
        self.assertIsNone(board.outcome())
        self.assertEqual(board.turn, "black")
        self.assertEqual(board.winning_moves("black"), ("E1",))
        board.push("E1")
        self.assertEqual(board.outcome()["winner"], "black")

    def test_copy_and_replay_preserve_rule_history_and_position_without_aliasing(self):
        board = GomokuBoard("standard", ("H8", "I8", "H9"))
        restored = GomokuBoard.from_payload(board.to_payload())
        self.assertEqual(restored.position_key, board.position_key)
        self.assertEqual(restored.history, board.history)
        clone = board.copy()
        clone.push("I9")
        self.assertNotEqual(clone.position_key, board.position_key)
        with self.assertRaises(TypeError):
            board.squares[0][0] = "X"

    def test_untrusted_saves_reject_bad_rules_duplicates_and_moves_after_a_win(self):
        for value in (None, {}, {"size": 19, "rule": "freestyle", "moves": []},
                      {"size": 15, "rule": "renju", "moves": []},
                      {"size": 15, "rule": "standard", "moves": ["H8", "H8"]},
                      {"size": 15, "rule": "freestyle", "moves": ["Q1"]},
                      {"size": 15, "rule": "freestyle", "moves": ["A1"]*226}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GomokuBoard.from_payload(value)
        terminal = self.alternate(["A1", "B1", "C1", "D1", "E1"]).to_payload()
        terminal["moves"].append("O1")
        with self.assertRaises(ValueError):
            GomokuBoard.from_payload(terminal)


if __name__ == "__main__":
    unittest.main()
