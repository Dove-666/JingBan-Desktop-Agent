"""Cancellable, owned Piskvork process for a 15 by 15 Rapfi board.

Wire coordinates are zero-based x,y. Public moves are A1..O15, with row 1 at
the top. Evaluation integers are Rapfi units, always for the side to move.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from ..config import PROJECT_ROOT
from .pikafish import AnalysisLine, EngineCancelled, EngineError

RULES = {"freestyle": 0, "standard": 1, "renju": 4}
_COORD = re.compile(r"^(\d{1,2}),(\d{1,2})$")


@dataclass(frozen=True)
class EngineAnalysis:
    bestmove: str
    lines: tuple[AnalysisLine, ...]
    engine_name: str
    score_perspective: str

    def to_dict(self):
        return {"bestmove": self.bestmove, "lines": [line.to_dict() for line in self.lines],
                "engine_name": self.engine_name, "score_perspective": self.score_perspective,
                "score_unit": "rapfi"}


def resolve_engine_path(path=None):
    if path is not None:
        candidate = Path(path).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise EngineError("没有找到 Rapfi 引擎，请检查引擎路径。")
    root = PROJECT_ROOT / "assets/engines/rapfi"
    manifest = root / "manifest.json"
    try:
        entry = json.loads(manifest.read_text(encoding="utf-8-sig")).get("entry")
        if not isinstance(entry, str):
            raise ValueError
        candidate = (root/entry).resolve()
        if candidate.is_relative_to(root.resolve()) and candidate.is_file():
            return candidate
    except (OSError, ValueError, TypeError):
        pass
    raise EngineError("Rapfi 尚未安装，请运行项目的五子棋引擎安装工具。")


def _move(x, y):
    if not 0 <= x < 15 or not 0 <= y < 15:
        raise EngineError("Rapfi 返回了棋盘外的落点。")
    return chr(65+x)+str(y+1)


def _position(board_rows, turn, rule):
    if turn not in {"black", "white"} or rule not in RULES:
        raise EngineError("五子棋规则或轮次无效。")
    if (not isinstance(board_rows, (list, tuple)) or len(board_rows) != 15
            or any(not isinstance(row, (list, tuple, str)) or len(row) != 15 for row in board_rows)):
        raise EngineError("五子棋棋盘必须为 15×15。")
    black, white = [], []
    for y, row in enumerate(board_rows):
        for x, piece in enumerate(row):
            if piece == "X":
                black.append((x, y))
            elif piece == "O":
                white.append((x, y))
            elif piece != ".":
                raise EngineError("五子棋棋盘包含无效棋子。")
    if len(black)-len(white) != (0 if turn == "black" else 1):
        raise EngineError("五子棋棋子数量与轮次不一致。")
    if len(black)+len(white) == 225:
        raise EngineError("五子棋棋盘已满，没有可计算的落点。")
    # A black-first interleaving preserves whose turn it is without protocol
    # pass moves. Gomoku evaluation depends on the resulting stone position.
    records = []
    for index in range(len(black)):
        x, y = black[index]
        records.append(f"{x},{y},{1 if turn == 'black' else 2}")
        if index < len(white):
            x, y = white[index]
            records.append(f"{x},{y},{2 if turn == 'black' else 1}")
    return records, {chr(65+x)+str(y+1) for x, y in black+white}


class RapfiEngine:
    """One serialized command stream. Run blocking methods in a worker."""
    def __init__(self, path=None, *, threads=1, hash_mb=64, startup_timeout=15):
        self.path = path
        self.threads = max(1, min(4, int(threads)))
        self.hash_mb = max(16, min(256, int(hash_mb)))
        self.startup_timeout = startup_timeout
        self.name = "Rapfi"
        self._command_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._cancel = threading.Event()
        self._process = self._output = None

    @property
    def running(self):
        process = self._process
        return process is not None and process.poll() is None

    def _send(self, command):
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise EngineError("Rapfi 已退出，请点重试，棋局已保留。")
            try:
                process.stdin.write(command+"\n")
                process.stdin.flush()
            except (OSError, ValueError):
                raise EngineError("无法连接 Rapfi，棋局已保留。") from None

    def _read(self, deadline, cancellation_event=None, on_idle=None):
        while True:
            if self._cancel.is_set() or cancellation_event is not None and cancellation_event.is_set():
                raise EngineCancelled("本次五子棋计算已取消。")
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise EngineError("Rapfi 本次计算未及时完成，可以重试；棋局已保留。")
            try:
                line = self._output.get(timeout=min(.05, remaining))
            except queue.Empty:
                if on_idle is not None:
                    on_idle()
                continue
            if line is None:
                raise EngineError("Rapfi 意外退出，棋局已保留。")
            if line.startswith("ERROR"):
                raise EngineError("Rapfi 未能接受本次请求，请重试或检查引擎文件。")
            return line

    def _about(self, deadline, cancellation_event):
        # ABOUT is read-only. A final move can reach the pipe immediately before
        # Rapfi clears its thinking flag, when normal commands are ignored.
        # Retry only this handshake until it acknowledges an idle command stream.
        self._send("ABOUT")
        last_sent = time.monotonic()

        def retry():
            nonlocal last_sent
            now = time.monotonic()
            if now-last_sent >= .1:
                self._send("ABOUT")
                last_sent = now

        while True:
            text = self._read(deadline, cancellation_event, on_idle=retry)
            if text.startswith('name="'):
                match = re.search(r'name="([^"]+)"', text)
                self.name = match.group(1)[:80] if match else "Rapfi"
                return

    def start(self, *, cancellation_event=None):
        with self._command_lock:
            if cancellation_event is not None and cancellation_event.is_set():
                raise EngineCancelled("本次五子棋计算已取消。")
            if self.running and not self._cancel.is_set():
                return self.name
            self._close_process()
            executable = resolve_engine_path(self.path)
            self._cancel.clear()
            output = queue.Queue()
            try:
                process = subprocess.Popen([str(executable)], cwd=str(executable.parent),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            except OSError:
                raise EngineError("Rapfi 无法启动，请检查本地引擎是否完整。") from None
            with self._lifecycle_lock:
                self._process, self._output = process, output

            def read_output():
                try:
                    for line in process.stdout:
                        output.put(line.strip())
                except (OSError, ValueError):
                    pass
                finally:
                    output.put(None)
            threading.Thread(target=read_output, name="aura-rapfi-output", daemon=True).start()
            try:
                deadline = time.monotonic()+self.startup_timeout
                self._about(deadline, cancellation_event)
                for command in ("YXSHOWINFO", "INFO PONDERING 0", "INFO USEDATABASE 0",
                                "INFO DATABASE_READONLY 1", f"INFO THREAD_NUM {self.threads}",
                                f"INFO HASH_SIZE {self.hash_mb*1024}", "INFO SHOW_DETAIL 2"):
                    self._send(command)
                self._send("START 15")
                while self._read(deadline, cancellation_event) != "OK":
                    pass
                return self.name
            except Exception:
                self._close_process()
                raise

    def _prepare(self, records, rule, cancellation_event):
        self.start(cancellation_event=cancellation_event)
        deadline = time.monotonic()+self.startup_timeout
        self._about(deadline, cancellation_event)
        self._send(f"INFO RULE {RULES[rule]}")
        self._send("RESTART")
        while self._read(deadline, cancellation_event) != "OK":
            pass
        self._send("YXBOARD")
        for record in records:
            self._send(record)
        self._send("DONE")

    @staticmethod
    def _line(info):
        pv = []
        for token in info.get("BESTLINE", "").split()[:20]:
            match = _COORD.fullmatch(token)
            if not match:
                break
            pv.append(_move(int(match[1]), int(match[2])))
        if not pv:
            return None
        score, mate = None, None
        value = info.get("EVAL", "")
        if re.fullmatch(r"-?\d+", value):
            score = int(value)
        elif re.fullmatch(r"[+-]M\d+", value):
            mate = int(value[2:]) * (-1 if value[0] == "-" else 1)
        try:
            depth = int(info.get("DEPTH", "0"))
        except ValueError:
            depth = 0
        return AnalysisLine(pv[0], score, mate, depth, tuple(pv))

    def analyse(self, board_rows, *, turn="black", rule="freestyle", movetime_ms=500,
                multipv=1, cancellation_event=None):
        records, occupied = _position(board_rows, turn, rule)
        milliseconds = max(30, min(60000, int(movetime_ms)))
        variants = max(1, min(12, int(multipv)))
        with self._command_lock:
            try:
                self._prepare(records, rule, cancellation_event)
                self._send(f"INFO TIMEOUT_TURN {milliseconds}")
                self._send("INFO TIMEOUT_MATCH 2147483647")
                self._send("INFO TIME_LEFT 2147483647")
                self._send(f"YXNBEST {variants}")
                deadline = time.monotonic()+milliseconds/1000+8
                info, pending, complete = {}, {}, ()
                pv_index = 0
                while True:
                    text = self._read(deadline, cancellation_event)
                    if text.startswith("INFO "):
                        fields = text.split(" ", 2)
                        if len(fields) < 3:
                            continue
                        key, value = fields[1:]
                        if key == "PV" and value != "DONE":
                            try: pv_index = int(value)
                            except ValueError: continue
                            info = {}
                            if pv_index == 0:
                                pending = {}
                        elif key == "PV" and value == "DONE":
                            candidate = self._line(info)
                            if candidate and candidate.move not in occupied:
                                pending[pv_index] = candidate
                                try:
                                    expected = int(info.get("NUMPV", "1"))
                                except ValueError:
                                    continue
                                if (1 <= expected <= variants
                                        and all(index in pending for index in range(expected))):
                                    batch = tuple(pending[index] for index in range(expected))
                                    if (len({line.move for line in batch}) == expected
                                            and len({line.depth for line in batch}) == 1):
                                        complete = batch
                        else:
                            info[key] = value
                    match = _COORD.fullmatch(text)
                    if match:
                        best = _move(int(match[1]), int(match[2]))
                        if best in occupied:
                            raise EngineError("Rapfi 的落点已经有棋子，请重试。")
                        # Interrupted searches may publish only half of the next
                        # iteration. Compare candidates from one complete depth;
                        # never combine stale PV slots or duplicate root moves.
                        ordered = complete
                        if not any(line.move == best for line in ordered):
                            ordered = (AnalysisLine(best),)
                        else:
                            ordered = tuple(sorted(ordered, key=lambda line: line.move != best))
                        return EngineAnalysis(best, ordered, self.name, turn)
            except Exception:
                self._close_process()
                raise

    def forbidden_moves(self, board_rows, *, turn="black", cancellation_event=None):
        """Read the engine's Renju forbidden points for future rule validation."""
        records, _ = _position(board_rows, turn, "renju")
        with self._command_lock:
            try:
                self._prepare(records, "renju", cancellation_event)
                self._send("YXSHOWFORBID")
                deadline = time.monotonic()+5
                while True:
                    text = self._read(deadline, cancellation_event)
                    if text.startswith("FORBID ") and text.endswith("."):
                        packed = text[7:-1]
                        if len(packed)%4 or packed and not packed.isdigit():
                            raise EngineError("Rapfi 禁手信息无效。")
                        return tuple(_move(int(packed[n:n+2]), int(packed[n+2:n+4]))
                                     for n in range(0, len(packed), 4))
            except Exception:
                self._close_process()
                raise

    def cancel(self):
        self._cancel.set()
        if self.running:
            try: self._send("STOP")
            except EngineError: pass

    def _close_process(self):
        with self._lifecycle_lock:
            process, self._process = self._process, None
            self._output = None
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    process.stdin.write("END\n")
                    process.stdin.flush()
                except (OSError, ValueError):
                    pass
                try: process.wait(timeout=.35)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
        finally:
            for stream in (process.stdin, process.stdout):
                try: stream.close()
                except (OSError, ValueError): pass

    def shutdown(self):
        self._cancel.set()
        with self._command_lock:
            self._close_process()
