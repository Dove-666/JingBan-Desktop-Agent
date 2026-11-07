"""Owned, cancellable UCI subprocess. All blocking calls belong in a worker."""
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


class EngineError(RuntimeError):
    pass


class EngineCancelled(EngineError):
    pass


@dataclass(frozen=True)
class AnalysisLine:
    move: str
    score_cp: int | None = None
    mate: int | None = None
    depth: int = 0
    pv: tuple[str, ...] = ()

    @property
    def value(self):
        if self.mate is not None:
            return 100000-self.mate if self.mate > 0 else -100000-self.mate
        return self.score_cp if self.score_cp is not None else -100001

    def to_dict(self):
        return {"move": self.move, "score_cp": self.score_cp, "mate": self.mate,
                "depth": self.depth, "pv": list(self.pv)}


@dataclass(frozen=True)
class EngineAnalysis:
    bestmove: str
    lines: tuple[AnalysisLine, ...]
    engine_name: str

    def to_dict(self):
        return {"bestmove": self.bestmove, "lines": [line.to_dict() for line in self.lines],
                "engine_name": self.engine_name}


def resolve_engine_path(path=None) -> Path:
    if path is not None:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file():
            raise EngineError("没有找到皮卡鱼引擎，请检查本地引擎文件后重试。")
        return candidate
    root = PROJECT_ROOT / "assets" / "engines" / "pikafish"
    manifest = root / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            entry = data.get("entry", "pikafish.exe" if os.name == "nt" else "pikafish")
            if not isinstance(entry, str):
                raise ValueError
            candidate = (root / entry).resolve()
            if not candidate.is_relative_to(root.resolve()):
                raise ValueError
            if candidate.is_file():
                return candidate
        except (OSError, ValueError, TypeError):
            pass
    candidate = root / ("pikafish.exe" if os.name == "nt" else "pikafish")
    if candidate.is_file():
        return candidate.resolve()
    raise EngineError("皮卡鱼引擎尚未就绪，请运行项目提供的引擎安装工具后重试。")


class PikafishEngine:
    """One process and one command stream; no shell, visible console or GUI I/O."""
    def __init__(self, path=None, *, threads=1, hash_mb=128, startup_timeout=15):
        self.path = path
        self.threads = max(1, min(4, int(threads)))
        self.hash_mb = max(16, min(512, int(hash_mb)))
        self.startup_timeout = startup_timeout
        self.name = "皮卡鱼"
        self._command_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._process = None
        self._output = None
        self._cancel = threading.Event()

    @property
    def running(self):
        process = self._process
        return process is not None and process.poll() is None

    def _send(self, command):
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise EngineError("皮卡鱼已退出，请点重试恢复，当前棋局已保留。")
            try:
                process.stdin.write(command + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                raise EngineError("无法连接皮卡鱼，请点重试，当前棋局已保留。") from None

    def _read(self, deadline, cancellation_event=None):
        while True:
            if self._cancel.is_set() or (cancellation_event is not None and cancellation_event.is_set()):
                raise EngineCancelled("本次计算已取消。")
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise EngineError("皮卡鱼本次计算没有及时完成，请重试；棋局未改变。")
            try:
                line = self._output.get(timeout=min(.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise EngineError("皮卡鱼意外退出，请重试；棋局已保留。")
            if "CRITICAL ERROR" in line:
                raise EngineError("引擎未能接受当前棋局，请重开或恢复存档。")
            return line

    def start(self, *, cancellation_event=None):
        with self._command_lock:
            if cancellation_event is not None and cancellation_event.is_set():
                raise EngineCancelled("本次计算已取消。")
            if self.running and not self._cancel.is_set():
                return self.name
            self._close_process()
            executable = resolve_engine_path(self.path)
            self._cancel.clear()
            output = queue.Queue()
            try:
                process = subprocess.Popen(
                    [str(executable)], cwd=str(executable.parent), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
            except OSError:
                raise EngineError("皮卡鱼无法启动，请检查引擎文件是否完整。") from None
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
            threading.Thread(target=read_output, name="aura-xiangqi-output", daemon=True).start()
            try:
                deadline = time.monotonic()+self.startup_timeout
                self._send("uci")
                while True:
                    line = self._read(deadline, cancellation_event)
                    if line.startswith("id name "):
                        self.name = line[8:].strip()[:120]
                    if line == "uciok":
                        break
                self._send(f"setoption name Threads value {self.threads}")
                self._send(f"setoption name Hash value {self.hash_mb}")
                self._send("setoption name Ponder value false")
                self._send("isready")
                while self._read(deadline, cancellation_event) != "readyok":
                    pass
                return self.name
            except Exception:
                self._close_process()
                raise

    @staticmethod
    def _position(fen, moves):
        if not isinstance(fen, str) or len(fen) > 256 or any(c in fen for c in "\r\n\x00"):
            raise EngineError("棋局格式无效。")
        moves = tuple(moves or ())
        if len(moves) > 2000 or any(not isinstance(m, str) or re.fullmatch(r"[a-i][0-9][a-i][0-9]", m) is None for m in moves):
            raise EngineError("棋谱格式无效。")
        return "position fen " + fen + (" moves " + " ".join(moves) if moves else "")

    def analyse(self, fen, moves=(), *, movetime_ms=500, multipv=1, cancellation_event=None):
        command = self._position(fen, moves)
        milliseconds = max(30, min(60000, int(movetime_ms)))
        variants = max(1, min(12, int(multipv)))
        with self._command_lock:
            try:
                self.start(cancellation_event=cancellation_event)
                self._send(f"setoption name MultiPV value {variants}")
                self._send(command)
                self._send(f"go movetime {milliseconds}")
                deadline = time.monotonic()+milliseconds/1000+8
                lines = {}
                while True:
                    text = self._read(deadline, cancellation_event)
                    if text.startswith("info ") and " pv " in text:
                        tokens = text.split()
                        try:
                            pv = tuple(tokens[tokens.index("pv")+1:])
                            if not pv or not re.fullmatch(r"[a-i][0-9][a-i][0-9]", pv[0]):
                                continue
                            depth = int(tokens[tokens.index("depth")+1]) if "depth" in tokens else 0
                            number = int(tokens[tokens.index("multipv")+1]) if "multipv" in tokens else 1
                            cp = mate = None
                            if "score" in tokens:
                                i = tokens.index("score")
                                if tokens[i+1] == "cp": cp = int(tokens[i+2])
                                if tokens[i+1] == "mate": mate = int(tokens[i+2])
                            candidate = AnalysisLine(pv[0], cp, mate, depth, pv[:20])
                            if number not in lines or depth >= lines[number].depth:
                                lines[number] = candidate
                        except (ValueError, IndexError):
                            continue
                    if text.startswith("bestmove "):
                        best = text.split()[1]
                        if best in {"(none)", "0000"}:
                            raise EngineError("当前局面没有可走的棋。")
                        if re.fullmatch(r"[a-i][0-9][a-i][0-9]", best) is None:
                            raise EngineError("引擎返回的走法无效，请重试。")
                        ordered = tuple(lines[n] for n in sorted(lines))
                        if not any(line.move == best for line in ordered):
                            ordered = (AnalysisLine(best),) + ordered
                        return EngineAnalysis(best, ordered, self.name)
            except Exception:
                self._close_process()
                raise

    def perft(self, fen, moves=(), *, depth=1, timeout=15):
        """Read authoritative engine legality for rules regression checks."""
        with self._command_lock:
            self.start()
            self._send(self._position(fen, moves))
            self._send(f"go perft {max(1, min(3, int(depth)))}")
            deadline, result = time.monotonic()+timeout, {}
            while True:
                line = self._read(deadline)
                match = re.fullmatch(r"([a-i][0-9][a-i][0-9]):\s*(\d+)", line)
                if match:
                    result[match.group(1)] = int(match.group(2))
                if line.startswith("Nodes searched:"):
                    return result

    def cancel(self):
        self._cancel.set()
        if self.running:
            try: self._send("stop")
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
                    process.stdin.write("quit\n")
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
