"""Ordered visual moves, independent from the controller's authoritative board.

The painter can keep a captured piece on ``active.before`` until ``finish``
lands the moving piece. Animation time and easing belong to the Qt widget.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..games.xiangqi import coordinates


_PIECES = frozenset("KABNRCPkabnrcp")
BoardRows = tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class MoveAnimation:
    move: str
    piece: str
    captured: str
    before: BoardRows
    after: BoardRows

    @property
    def source(self):
        return self.move[:2]

    @property
    def target(self):
        return self.move[2:]


def _board_rows(state):
    rows = state.get("board", ())
    if (not isinstance(rows, (tuple, list)) or len(rows) != 10
            or any(not isinstance(row, (tuple, list)) or len(row) != 9 for row in rows)):
        return ()
    board = tuple(tuple(row) for row in rows)
    if any(piece not in _PIECES and piece != "." for row in board for piece in row):
        return ()
    return board


def _move_history(state):
    entries = state.get("moves")
    if not isinstance(entries, (tuple, list)):
        return None
    moves = []
    for entry in entries:
        move = entry.get("move") if isinstance(entry, dict) else entry
        if not isinstance(move, str) or len(move) != 4:
            return None
        try:
            coordinates(move[:2])
            coordinates(move[2:])
        except ValueError:
            return None
        moves.append(move)
    return tuple(moves)


def _transition(board, move):
    source_row, source_column = coordinates(move[:2])
    target_row, target_column = coordinates(move[2:])
    piece = board[source_row][source_column]
    captured = board[target_row][target_column]
    if piece not in _PIECES or move[:2] == move[2:]:
        return None
    if captured in _PIECES and captured.isupper() == piece.isupper():
        return None
    rows = [list(row) for row in board]
    rows[source_row][source_column] = "."
    rows[target_row][target_column] = piece
    after = tuple(tuple(row) for row in rows)
    return MoveAnimation(move, piece, captured, board, after)


class XiangqiMotion:
    """Queue only validated history extensions within the same visible game."""

    def __init__(self):
        self.board: BoardRows = ()
        self.last_move = None
        self.active: MoveAnimation | None = None
        self._queue = deque()
        self._target_board: BoardRows = ()
        self._target_last_move = None
        self._history = None
        self._identity = None
        self._revision = None

    @property
    def pending_count(self):
        return len(self._queue) + (self.active is not None)

    def set_state(self, state, *, animate=True):
        """Return whether the active animation changed, preserving its clock.

        Initial/restored boards are immediate. Missing history identity also
        snaps, so older callers that only provide board rows remain supported.
        """
        previous_active = self.active
        board = _board_rows(state)
        history = _move_history(state)
        initial_fen = state.get("initial_fen")
        revision = state.get("revision")
        identity = (initial_fen, state.get("human_side", "red"))
        identified = isinstance(initial_fen, str) and bool(initial_fen) and type(revision) is int
        same_game = (identified and bool(self._target_board) and identity == self._identity and self._revision is not None
                     and revision >= self._revision)
        steps = None
        if (animate and same_game and board and history is not None and self._history is not None
                and len(history) >= len(self._history) and history[:len(self._history)] == self._history):
            steps = []
            current = self._target_board
            for move in history[len(self._history):]:
                step = _transition(current, move)
                if step is None:
                    break
                steps.append(step)
                current = step.after
            if len(steps) != len(history)-len(self._history) or current != board:
                steps = None

        self._target_board = board
        self._target_last_move = history[-1] if history else state.get("last_move")
        self._history = history
        self._identity = identity if identified else None
        self._revision = revision if identified else None
        if steps is None:
            self.snap()
        else:
            self._queue.extend(steps)
            if self.active is None and self._queue:
                self.active = self._queue.popleft()
        return self.active is not previous_active

    def finish(self):
        """Land one move, then expose the next move without skipping a frame."""
        if self.active is not None:
            self.board = self.active.after
            self.last_move = self.active.move
        self.active = self._queue.popleft() if self._queue else None
        return self.active is not None

    def snap(self, state=None):
        """Cancel visual work and immediately show the latest actual board."""
        if state is not None:
            self.set_state(state, animate=False)
            return
        self._queue.clear()
        self.active = None
        self.board = self._target_board
        self.last_move = self._target_last_move
