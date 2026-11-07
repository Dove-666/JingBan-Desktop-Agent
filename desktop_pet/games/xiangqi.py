"""Dependency-free Xiangqi rules, reversible moves and validated saved games.

Coordinates use ICCS/UCI: a0 is the red player's bottom-left intersection.
Rows run from black's back rank (0) to red's back rank (9).  Repetition is
adjudicated as a draw except for a sole perpetual-checking side, which loses;
this module does not claim the full WXF perpetual-chase arbitration rules.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re


START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
_PIECES = frozenset("rnbakcpRNBAKCP")
_SQUARE = re.compile(r"^[a-i][0-9]$")
_MOVE = re.compile(r"^[a-i][0-9][a-i][0-9]$")
_NUMERALS = "零一二三四五六七八九"


def square(row: int, col: int) -> str:
    if (type(row) is not int or type(col) is not int
            or not 0 <= row < 10 or not 0 <= col < 9):
        raise ValueError("棋盘坐标无效。")
    return f"{chr(97 + col)}{9-row}"


def coordinates(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not _SQUARE.fullmatch(value):
        raise ValueError("棋盘坐标无效。")
    return 9-int(value[1]), ord(value[0])-97


def _side(piece: str) -> str | None:
    return None if piece == "." else ("red" if piece.isupper() else "black")


def _other(side: str) -> str:
    return "black" if side == "red" else "red"


def _inside(row: int, col: int) -> bool:
    return 0 <= row < 10 and 0 <= col < 9


def _palace(side: str, row: int, col: int) -> bool:
    return 3 <= col <= 5 and (7 <= row <= 9 if side == "red" else 0 <= row <= 2)


@dataclass(frozen=True)
class MoveRecord:
    move: str
    label: str
    side: str
    piece: str
    captured: str
    check: bool
    fen_before: str
    fen_after: str

    @property
    def notation(self) -> str:
        return self.label


class XiangqiBoard:
    def __init__(self, fen: str = START_FEN):
        self._board, self._turn, self._halfmove, self._fullmove = self._parse_fen(fen)
        self.initial_fen = self.fen
        self.moves: list[str] = []
        self.history: list[MoveRecord] = []
        self._positions = [self._position_key()]

    @staticmethod
    def _parse_fen(fen):
        if not isinstance(fen, str) or len(fen) > 200:
            raise ValueError("象棋局面格式无效。")
        parts = fen.split()
        if len(parts) not in (2, 6) or parts[1] not in ("w", "b"):
            raise ValueError("象棋局面格式无效。")
        if len(parts) == 6:
            if (parts[2:4] != ["-", "-"] or not parts[4].isascii()
                    or not parts[4].isdigit() or not parts[5].isascii()
                    or not parts[5].isdigit() or len(parts[4]) > 9 or len(parts[5]) > 9):
                raise ValueError("象棋局面计数无效。")
            halfmove, fullmove = int(parts[4]), int(parts[5])
            if fullmove < 1:
                raise ValueError("象棋局面计数无效。")
        else:
            halfmove, fullmove = 0, 1
        ranks = parts[0].split("/")
        if len(ranks) != 10:
            raise ValueError("象棋局面必须有十行。")
        board = []
        for rank in ranks:
            row = []
            for char in rank:
                if char in "123456789":
                    row.extend("." * int(char))
                elif char in _PIECES:
                    row.append(char)
                else:
                    raise ValueError("象棋局面含有未知棋子。")
            if len(row) != 9:
                raise ValueError("象棋局面每行必须有九列。")
            board.append(row)
        counts = Counter(piece for row in board for piece in row if piece != ".")
        if counts["K"] != 1 or counts["k"] != 1:
            raise ValueError("象棋局面必须包含双方将帅。")
        for piece, maximum in {"R": 2, "N": 2, "B": 2, "A": 2, "C": 2, "P": 5}.items():
            if counts[piece] > maximum or counts[piece.lower()] > maximum:
                raise ValueError("象棋局面的棋子数量无效。")
        return board, "red" if parts[1] == "w" else "black", halfmove, fullmove

    @property
    def squares(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(row) for row in self._board)

    def to_rows(self) -> list[list[str]]:
        return [list(row) for row in self._board]

    @property
    def turn(self) -> str:
        return self._turn

    def _placement(self) -> str:
        ranks = []
        for row in self._board:
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
        return "/".join(ranks)

    @property
    def fen(self) -> str:
        return f"{self._placement()} {'w' if self.turn == 'red' else 'b'} - - {self._halfmove} {self._fullmove}"

    def _position_key(self) -> str:
        return f"{self._placement()} {self.turn}"

    def _destinations(self, row, col):
        piece = self._board[row][col]
        side, kind = _side(piece), piece.upper()
        if side is None:
            return

        def available(r, c):
            return _inside(r, c) and _side(self._board[r][c]) != side

        if kind in ("R", "C"):
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                r, c, screen = row+dr, col+dc, False
                while _inside(r, c):
                    target = self._board[r][c]
                    if kind == "R":
                        if target == ".":
                            yield r, c
                        else:
                            if _side(target) != side:
                                yield r, c
                            break
                    elif not screen:
                        if target == ".":
                            yield r, c
                        else:
                            screen = True
                    elif target != ".":
                        if _side(target) != side:
                            yield r, c
                        break
                    r, c = r+dr, c+dc
        elif kind == "N":
            for dr, dc in ((-2, -1), (-2, 1), (2, -1), (2, 1),
                           (-1, -2), (1, -2), (-1, 2), (1, 2)):
                leg_r = row + (dr//2 if abs(dr) == 2 else 0)
                leg_c = col + (dc//2 if abs(dc) == 2 else 0)
                if available(row+dr, col+dc) and self._board[leg_r][leg_c] == ".":
                    yield row+dr, col+dc
        elif kind == "B":
            for dr, dc in ((-2, -2), (-2, 2), (2, -2), (2, 2)):
                r, c = row+dr, col+dc
                if (available(r, c) and (r >= 5 if side == "red" else r <= 4)
                        and self._board[row+dr//2][col+dc//2] == "."):
                    yield r, c
        elif kind == "A":
            for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                r, c = row+dr, col+dc
                if _palace(side, r, c) and available(r, c):
                    yield r, c
        elif kind == "K":
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                r, c = row+dr, col+dc
                if _palace(side, r, c) and available(r, c):
                    yield r, c
            for dr in (-1, 1):
                r = row+dr
                while _inside(r, col):
                    target = self._board[r][col]
                    if target != ".":
                        if target.upper() == "K" and _side(target) != side:
                            yield r, col
                        break
                    r += dr
        elif kind == "P":
            candidates = [(row + (-1 if side == "red" else 1), col)]
            if (row <= 4 if side == "red" else row >= 5):
                candidates.extend(((row, col-1), (row, col+1)))
            for r, c in candidates:
                if available(r, c):
                    yield r, c

    def in_check(self, side: str | None = None) -> bool:
        side = self.turn if side is None else side
        if side not in ("red", "black"):
            raise ValueError("未知棋方。")
        king = "K" if side == "red" else "k"
        target = next(((r, c) for r, row in enumerate(self._board)
                       for c, piece in enumerate(row) if piece == king), None)
        if target is None:
            return True
        for r, row in enumerate(self._board):
            for c, piece in enumerate(row):
                if _side(piece) == _other(side) and target in self._destinations(r, c):
                    return True
        return False

    def _repetition_outcome(self):
        current = self._positions[-1]
        occurrences = [i for i, position in enumerate(self._positions) if position == current]
        if len(occurrences) < 3:
            return None
        cycle = self.history[occurrences[-3]:occurrences[-1]]
        checking = []
        for side in ("red", "black"):
            own = [move for move in cycle if move.side == side]
            if own and all(move.check for move in own):
                checking.append(side)
        if len(checking) == 1:
            return {"winner": _other(checking[0]), "reason": "perpetual_check"}
        return {"winner": "draw", "reason": "repetition"}

    def legal_moves(self) -> list[str]:
        if self._repetition_outcome() is not None:
            return []
        if not any("K" in row for row in self._board) or not any("k" in row for row in self._board):
            return []
        result = []
        side = self.turn
        for r, row in enumerate(self._board):
            for c, piece in enumerate(row):
                if _side(piece) != side:
                    continue
                for rr, cc in list(self._destinations(r, c)):
                    captured = self._board[rr][cc]
                    self._board[r][c], self._board[rr][cc] = ".", piece
                    try:
                        safe = not self.in_check(side)
                    finally:
                        self._board[r][c], self._board[rr][cc] = piece, captured
                    if safe:
                        result.append(square(r, c)+square(rr, cc))
        return result

    def is_legal(self, move: str) -> bool:
        return isinstance(move, str) and bool(_MOVE.fullmatch(move)) and move in self.legal_moves()

    def move_label(self, move: str) -> str:
        if not isinstance(move, str) or not _MOVE.fullmatch(move):
            raise ValueError("走法格式无效。")
        row, col = coordinates(move[:2])
        dest_row, dest_col = coordinates(move[2:])
        piece = self._board[row][col]
        side = _side(piece)
        if side is None or (row, col) == (dest_row, dest_col):
            raise ValueError("走法没有对应棋子。")
        names = {"R": "车", "N": "马", "B": "相" if side == "red" else "象",
                 "A": "仕" if side == "red" else "士", "K": "帅" if side == "red" else "将",
                 "C": "炮", "P": "兵" if side == "red" else "卒"}
        number = (lambda n: _NUMERALS[n]) if side == "red" else str
        file_number = lambda c: 9-c if side == "red" else c+1
        peers = [r for r in range(10) if self._board[r][col] == piece]
        peers.sort(reverse=side == "black")
        if len(peers) > 1:
            index = peers.index(row)
            if len(peers) == 2:
                prefix = ("前" if index == 0 else "后") + names[piece.upper()]
            elif len(peers) == 3:
                prefix = ("前", "中", "后")[index] + names[piece.upper()]
            else:
                prefix = ("前" if index == 0 else "后" if index == len(peers)-1
                          else _NUMERALS[index+1]) + names[piece.upper()]
        else:
            prefix = names[piece.upper()] + number(file_number(col))
        if row == dest_row:
            return prefix + "平" + number(file_number(dest_col))
        forward = dest_row < row if side == "red" else dest_row > row
        target = (file_number(dest_col) if piece.upper() in ("N", "A", "B")
                  else abs(dest_row-row))
        return prefix + ("进" if forward else "退") + number(target)

    def push(self, move: str) -> MoveRecord:
        if not self.is_legal(move):
            raise ValueError("这步棋不符合当前局面的走法，请重新选择。")
        before, label, side = self.fen, self.move_label(move), self.turn
        r, c = coordinates(move[:2])
        rr, cc = coordinates(move[2:])
        piece, captured = self._board[r][c], self._board[rr][cc]
        self._board[r][c], self._board[rr][cc] = ".", piece
        self._halfmove = 0 if captured != "." or piece.upper() == "P" else self._halfmove+1
        self._fullmove += side == "black"
        self._turn = _other(side)
        record = MoveRecord(move, label, side, piece, captured, self.in_check(), before, self.fen)
        self.moves.append(move)
        self.history.append(record)
        self._positions.append(self._position_key())
        return record

    def undo(self) -> MoveRecord | None:
        if not self.history:
            return None
        record = self.history.pop()
        self.moves.pop()
        self._positions.pop()
        self._board, self._turn, self._halfmove, self._fullmove = self._parse_fen(record.fen_before)
        return record

    def outcome(self) -> dict[str, str] | None:
        repetition = self._repetition_outcome()
        if repetition is not None:
            return repetition
        for side, king in (("red", "K"), ("black", "k")):
            if not any(king in row for row in self._board):
                return {"winner": _other(side), "reason": "checkmate"}
        if not self.legal_moves():
            return {"winner": _other(self.turn), "reason": "checkmate" if self.in_check() else "stalemate"}
        return None

    def copy(self) -> "XiangqiBoard":
        board = object.__new__(XiangqiBoard)
        board._board = [list(row) for row in self._board]
        board._turn, board._halfmove, board._fullmove = self.turn, self._halfmove, self._fullmove
        board.initial_fen = self.initial_fen
        board.moves, board.history, board._positions = list(self.moves), list(self.history), list(self._positions)
        return board

    def to_payload(self) -> dict:
        return {"schema_version": 1, "initial_fen": self.initial_fen, "moves": list(self.moves)}

    @classmethod
    def from_payload(cls, value) -> "XiangqiBoard":
        if (not isinstance(value, dict) or set(value) != {"schema_version", "initial_fen", "moves"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["moves"], list) or len(value["moves"]) > 4096
                or any(not isinstance(move, str) or not _MOVE.fullmatch(move) for move in value["moves"])):
            raise ValueError("保存的棋局格式无效。")
        board = cls(value["initial_fen"])
        for move in value["moves"]:
            board.push(move)
        return board
