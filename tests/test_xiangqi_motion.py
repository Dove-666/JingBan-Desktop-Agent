"""Visual queue regressions for fast engine replies and replacement games."""
import unittest

from desktop_pet.games.xiangqi import XiangqiBoard
from desktop_pet.ui.xiangqi_motion import XiangqiMotion


def snapshot(board, revision=None, **changes):
    state = {
        "board": [list(row) for row in board.squares],
        "initial_fen": board.initial_fen,
        "moves": [{"move": move} for move in board.moves],
        "last_move": board.moves[-1] if board.moves else None,
        "human_side": "red",
        "revision": len(board.moves) if revision is None else revision,
    }
    state.update(changes)
    return state


class XiangqiMotionTests(unittest.TestCase):
    def setUp(self):
        self.board = XiangqiBoard()
        self.motion = XiangqiMotion()
        self.motion.set_state(snapshot(self.board))

    def push(self, move, **changes):
        self.board.push(move)
        state = snapshot(self.board, **changes)
        self.motion.set_state(state)
        return state

    def test_fast_engine_reply_queues_after_human_animation(self):
        original = self.board.squares
        self.push("h2e2")
        first = self.motion.active
        first_position = self.board.squares
        self.push("h9g7")
        self.assertIs(self.motion.active, first)
        self.assertEqual(self.motion.pending_count, 2)
        self.assertEqual(self.motion.board, original)
        self.assertTrue(self.motion.finish())
        self.assertEqual(self.motion.board, first_position)
        self.assertEqual(self.motion.last_move, "h2e2")
        self.assertEqual(self.motion.active.move, "h9g7")
        self.assertFalse(self.motion.finish())
        self.assertEqual(self.motion.board, self.board.squares)
        self.assertEqual(self.motion.last_move, "h9g7")

    def test_busy_and_commentary_updates_do_not_restart_or_repeat(self):
        state = self.push("h2e2")
        active = self.motion.active
        for values in ({"busy": True}, {"commentary_busy": True}, {"revision": 2}, {"busy": False}):
            state.update(values)
            self.assertFalse(self.motion.set_state(state))
            self.assertIs(self.motion.active, active)
            self.assertEqual(self.motion.pending_count, 1)
        self.motion.finish()
        self.motion.set_state(state)
        self.assertIsNone(self.motion.active)

    def test_capture_stays_on_destination_until_the_move_lands(self):
        self.board = XiangqiBoard("4k4/9/9/9/4p4/4R4/9/9/9/3K5 w - - 0 1")
        self.motion.snap(snapshot(self.board))
        self.push("e4e5")
        active = self.motion.active
        self.assertEqual((active.source, active.target, active.piece, active.captured), ("e4", "e5", "R", "p"))
        self.assertEqual(self.motion.board[4][4], "p")
        self.assertEqual(active.before[4][4], "p")
        self.assertEqual(active.after[4][4], "R")
        self.motion.finish()
        self.assertEqual(self.motion.board[4][4], "R")
        self.assertEqual(self.motion.board[5][4], ".")

    def test_hidden_board_snaps_latest_state_and_discards_pending_moves(self):
        self.push("h2e2")
        self.push("h9g7")
        self.motion.snap()
        self.assertIsNone(self.motion.active)
        self.assertEqual(self.motion.pending_count, 0)
        self.assertEqual(self.motion.board, self.board.squares)
        self.motion.set_state(snapshot(self.board))
        self.assertIsNone(self.motion.active)

    def test_undo_new_game_and_orientation_change_cancel_visual_moves(self):
        self.push("h2e2")
        self.push("h9g7")
        self.board.undo()
        self.motion.set_state(snapshot(self.board, revision=3))
        self.assertIsNone(self.motion.active)
        self.assertEqual(self.motion.board, self.board.squares)
        self.board = XiangqiBoard()
        self.motion.set_state(snapshot(self.board, revision=4))
        self.assertIsNone(self.motion.active)
        self.push("h2e2", revision=5)
        self.motion.set_state(snapshot(self.board, revision=6, human_side="black"))
        self.assertIsNone(self.motion.active)
        self.assertEqual(self.motion.board, self.board.squares)

    def test_restored_position_and_legacy_snapshot_do_not_replay(self):
        self.board.push("h2e2")
        self.board.push("h9g7")
        restored = XiangqiMotion()
        restored.set_state(snapshot(self.board))
        self.assertIsNone(restored.active)
        self.assertEqual(restored.board, self.board.squares)
        legacy = snapshot(self.board)
        legacy.pop("initial_fen")
        legacy.pop("revision")
        self.motion.set_state(legacy)
        self.assertIsNone(self.motion.active)

    def test_multiple_arriving_moves_are_replayed_in_history_order(self):
        for move in ("h2e2", "h9g7", "h0g2", "b9c7"):
            self.board.push(move)
        self.motion.set_state(snapshot(self.board))
        moves = []
        while self.motion.active:
            moves.append(self.motion.active.move)
            self.motion.finish()
        self.assertEqual(moves, self.board.moves)
        self.assertEqual(self.motion.board, self.board.squares)

    def test_inconsistent_history_or_replaced_board_snaps_safely(self):
        self.push("h2e2")
        replacement = snapshot(self.board)
        replacement["moves"] = [{"move": "h2h5"}]
        self.motion.set_state(replacement)
        self.assertIsNone(self.motion.active)
        self.assertEqual(self.motion.board, self.board.squares)
        replacement = snapshot(self.board)
        replacement["board"][0][0] = "."
        self.motion.set_state(replacement)
        self.assertIsNone(self.motion.active)
        self.assertEqual(self.motion.board[0][0], ".")

    def test_motion_rows_are_independent_of_mutable_controller_payload(self):
        state = self.push("h2e2")
        active = self.motion.active
        before, after = active.before, active.after
        state["board"][7][4] = "."
        state["moves"].clear()
        self.assertEqual(active.before, before)
        self.assertEqual(active.after, after)
        self.assertEqual(active.after[7][4], "C")

    def test_checkmate_does_not_skip_final_visual_move(self):
        self.push("h2e2", outcome={"winner": "red", "reason": "checkmate"})
        self.assertIsNotNone(self.motion.active)
        self.motion.finish()
        self.assertEqual(self.motion.board, self.board.squares)


if __name__ == "__main__":
    unittest.main()
