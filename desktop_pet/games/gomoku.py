"""Replayable 15-by-15 Gomoku rules with explicit freestyle/standard wins."""
from __future__ import annotations

from dataclasses import dataclass
import re


SIZE = 15
RULES = {"freestyle": "自由五子棋", "standard": "标准五子棋（恰好五子）"}
_MOVE = re.compile(r"([A-Oa-o])([1-9]|1[0-5])\Z")
_DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


def coordinates(move):
    """Return (row, column); A1 is the top-left intersection."""
    match = _MOVE.fullmatch(move) if isinstance(move, str) else None
    if match is None:
        raise ValueError("五子棋坐标无效。")
    return int(match[2])-1, ord(match[1].upper())-ord("A")


def square(row, column):
    if type(row) is not int or type(column) is not int or not (0 <= row < SIZE and 0 <= column < SIZE):
        raise ValueError("五子棋坐标无效。")
    return f"{chr(ord('A')+column)}{row+1}"


def other_side(side):
    if side not in {"black", "white"}:
        raise ValueError("棋子颜色无效。")
    return "white" if side == "black" else "black"


@dataclass(frozen=True)
class MoveRecord:
    move: str
    label: str
    side: str
    piece: str
    winning_line: tuple[str, ...]
    position_before: str
    position_after: str


class GomokuBoard:
    """All stored games are validated by replaying legal moves from an empty board."""
    size = SIZE

    def __init__(self, rule="freestyle", moves=()):
        if rule not in RULES:
            raise ValueError("暂不支持这套五子棋规则。")
        self.rule = rule
        self._board = [["."]*SIZE for _ in range(SIZE)]
        self.moves = []
        self.history = []
        self._outcome = None
        self._winning_line = ()
        self._winning_cache = {}
        for move in moves:
            self.push(move)

    @property
    def turn(self):
        return "black" if len(self.moves) % 2 == 0 else "white"

    @property
    def squares(self):
        return tuple(tuple(row) for row in self._board)

    @property
    def position_key(self):
        return self.rule+"|"+self.turn+"|"+"/".join("".join(row) for row in self._board)

    @property
    def winning_line(self):
        return list(self._winning_line)

    def outcome(self):
        return dict(self._outcome) if self._outcome else None

    def legal_moves(self):
        if self._outcome:
            return ()
        return tuple(square(row, column) for row in range(SIZE) for column in range(SIZE)
                     if self._board[row][column] == ".")

    def is_legal(self, move):
        try:
            row, column = coordinates(move)
        except ValueError:
            return False
        return self._outcome is None and self._board[row][column] == "."

    def move_label(self, move):
        row, column = coordinates(move)
        return square(row, column)

    def _line_through(self, row, column, dr, dc, piece):
        first_row, first_column = row, column
        while (0 <= first_row-dr < SIZE and 0 <= first_column-dc < SIZE
               and self._board[first_row-dr][first_column-dc] == piece):
            first_row -= dr
            first_column -= dc
        line = []
        rr, cc = first_row, first_column
        while 0 <= rr < SIZE and 0 <= cc < SIZE:
            if (rr, cc) != (row, column) and self._board[rr][cc] != piece:
                break
            line.append(square(rr, cc))
            rr += dr
            cc += dc
        return tuple(line)

    def _winning_at(self, row, column, piece):
        for dr, dc in _DIRECTIONS:
            line = self._line_through(row, column, dr, dc, piece)
            if len(line) == 5 or (self.rule == "freestyle" and len(line) > 5):
                return line
        return ()

    def winning_moves(self, side):
        """Find actual one-move wins, without changing the board or its history."""
        if self._outcome:
            return ()
        if side not in {"black", "white"}:
            raise ValueError("棋子颜色无效。")
        if side not in self._winning_cache:
            piece = "X" if side == "black" else "O"
            self._winning_cache[side] = tuple(
                square(row, column) for row in range(SIZE) for column in range(SIZE)
                if self._board[row][column] == "." and self._winning_at(row, column, piece))
        return self._winning_cache[side]

    def push(self, move):
        if not self.is_legal(move):
            raise ValueError("这个位置不能落子。")
        row, column = coordinates(move)
        move = square(row, column)
        side = self.turn
        piece = "X" if side == "black" else "O"
        before = self.position_key
        self._board[row][column] = piece
        self.moves.append(move)
        self._winning_line = self._winning_at(row, column, piece)
        self._outcome = ({"winner": side, "reason": "five"} if self._winning_line else
                         {"winner": "draw", "reason": "full_board"} if len(self.moves) == SIZE*SIZE else None)
        self._winning_cache.clear()
        record = MoveRecord(move, move, side, piece, self._winning_line, before, self.position_key)
        self.history.append(record)
        return record

    def undo(self):
        if not self.history:
            return None
        record = self.history.pop()
        self.moves.pop()
        row, column = coordinates(record.move)
        self._board[row][column] = "."
        self._outcome = None
        self._winning_line = ()
        self._winning_cache.clear()
        return record

    def copy(self):
        return GomokuBoard(self.rule, self.moves)

    def to_payload(self):
        return {"size": SIZE, "rule": self.rule, "moves": list(self.moves)}

    @classmethod
    def from_payload(cls, value):
        if (not isinstance(value, dict) or type(value.get("size")) is not int or value.get("size") != SIZE
                or not isinstance(value.get("rule"), str) or value.get("rule") not in RULES
                or not isinstance(value.get("moves"), list) or len(value["moves"]) > SIZE*SIZE):
            raise ValueError("五子棋存档格式无效。")
        return cls(value["rule"], value["moves"])
