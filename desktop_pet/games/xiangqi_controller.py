"""Asynchronous local matches, durable replay, and optional character commentary."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, QStandardPaths, Signal

from ..async_tasks import TaskRunner
from .pikafish import EngineCancelled, EngineError, PikafishEngine
from .xiangqi import XiangqiBoard


DIFFICULTIES = {
    "casual": {"label": "轻松陪玩", "movetime_ms": 180, "multipv": 6, "loss_cp": 180},
    "standard": {"label": "日常切磋", "movetime_ms": 500, "multipv": 3, "loss_cp": 60},
    "serious": {"label": "认真对弈", "movetime_ms": 1200, "multipv": 1, "loss_cp": 0},
}


class _SaveState:
    """A Qt-free, versioned single writer; a late save cannot undo a restart."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.latest = 0
        self.blocked = False

    def write(self, revision, payload):
        with self.lock:
            if self.blocked or revision != self.latest:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass


class XiangqiController(QObject):
    changed = Signal(dict)
    speech = Signal(str, str)
    commentary_requested = Signal(str, dict)

    def __init__(self, settings=None, parent=None, storage_path=None, engine_path=None):
        super().__init__(parent)
        self.settings = settings
        if storage_path is None:
            configuration = getattr(settings, "config_path", None)
            directory = (Path(configuration).parent if configuration else
                         Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)))
            storage_path = directory / "games" / "xiangqi.json"
        self.storage_path = Path(storage_path)
        self._save_state = _SaveState(self.storage_path)
        self._save_revision = 0
        self.runner = TaskRunner(self)
        self.engine = PikafishEngine(engine_path)
        # A GUI-thread factory may capture validated API settings and return a
        # Qt-free worker selector. Absence/failure retains the local engine.
        self.move_selector_factory = None
        self.board = XiangqiBoard()
        self.human_side = "red"
        self.difficulty = "standard"
        self.busy = False
        self.commentary_busy = False
        self.status = "准备和鲸鱼娘下一盘棋。"
        self.error = ""
        self.engine_ready = False
        self.engine_label = "皮卡鱼 · 本地计算"
        self._records = []
        self._commentary = []
        self._hint = None
        self._analysis = None
        self._result = None
        self._generation = 0
        self._job_cancel = None
        self._closed = False
        self._loaded_storage = False
        self._storage_warning = ""
        self._random = random.Random()

    @property
    def revision(self):
        return self._generation

    def snapshot(self):
        outcome = self._result or self.board.outcome()
        legal = self.board.legal_moves() if outcome is None else []
        checked_side = self.board.turn if outcome is None and self.board.in_check() else None
        latest_move = self.board.history[-1] if self.board.history else None
        return {
            "board": [list(row) for row in self.board.squares], "fen": self.board.fen,
            "initial_fen": self.board.initial_fen, "turn": self.board.turn,
            "human_side": self.human_side, "difficulty": self.difficulty,
            "busy": self.busy, "status": self.status, "error": self.error,
            "commentary_busy": self.commentary_busy,
            "engine_ready": self.engine_ready, "engine_label": self.engine_label,
            "last_move": self.board.moves[-1] if self.board.moves else None,
            "last_move_details": {key: getattr(latest_move, key) for key in
                                  ("move", "label", "side", "piece", "captured", "check")} if latest_move else None,
            "checked_side": checked_side,
            "legal_moves": list(legal), "moves": [dict(item) for item in self._records],
            "outcome": dict(outcome) if outcome else None,
            "commentary": [dict(item) for item in self._commentary],
            "hint": self._hint, "can_undo": any(item["side"] == self.human_side for item in self._records),
            "can_new": True, "saved": not bool(self._storage_warning),
            "storage_warning": self._storage_warning, "revision": self._generation,
            "analysis": self._analysis, "difficulty_label": DIFFICULTIES[self.difficulty]["label"],
        }

    def _emit(self):
        if not self._closed:
            self.changed.emit(self.snapshot())

    def _payload(self):
        return {"schema_version": 1, "board": self.board.to_payload(),
                "human_side": self.human_side, "difficulty": self.difficulty,
                "commentary": self._commentary[-80:], "result": self._result,
                "updated_at": datetime.now(timezone.utc).isoformat()}

    def _save(self):
        # Until first restore finishes, this board is only a placeholder. Chat
        # and shutdown must not replace a valid previous game with that board.
        # An explicit new_game marks storage loaded before it reaches here.
        if not self._loaded_storage or self._save_state.blocked or self._closed:
            return
        self._save_revision += 1
        version, payload, state = self._save_revision, self._payload(), self._save_state
        state.latest = version
        def complete(_result, error):
            if error and not self._closed:
                self._storage_warning = "棋局暂未保存成功，请检查保存目录是否可写。"
                self._emit()
        self.runner.submit(lambda: state.write(version, payload), complete)

    def _read_saved(self, cancellation_event=None):
        # Match readers and atomic writers share ownership of the save file.
        # In particular a cancelled old startup must not quarantine a newer
        # game's save after that game has already replaced the old bad file.
        with self._save_state.lock:
            if cancellation_event is not None and cancellation_event.is_set():
                return None, ""
            return self._read_saved_locked(cancellation_event)

    def _read_saved_locked(self, cancellation_event):
        if not self.storage_path.is_file():
            return None, ""
        original_stat = original_text = None
        try:
            original_stat = self.storage_path.stat()
            if original_stat.st_size > 2 * 1024 * 1024:
                raise ValueError
            original_text = self.storage_path.read_text(encoding="utf-8-sig")
            value = json.loads(original_text)
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError
            if value.get("human_side") not in {"red", "black"} or value.get("difficulty") not in DIFFICULTIES:
                raise ValueError
            board = XiangqiBoard.from_payload(value["board"])
            commentary = value.get("commentary", [])
            if not isinstance(commentary, list) or len(commentary) > 80:
                raise ValueError
            for item in commentary:
                if (not isinstance(item, dict) or item.get("role") not in {"assistant", "user"}
                        or not isinstance(item.get("text"), str) or len(item["text"]) > 4000):
                    raise ValueError
            result = value.get("result")
            if result is not None and (not isinstance(result, dict) or result.get("winner") not in {"red", "black", "draw"}
                    or result.get("reason") not in {"resign", "checkmate", "stalemate", "repetition", "perpetual_check"}):
                raise ValueError
            return (value, board), ""
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            if cancellation_event is not None and cancellation_event.is_set():
                return None, ""
            # The shared lock excludes this controller's writers. Recheck the
            # identity/content as well in case another process edited the file.
            try:
                current = self.storage_path.stat()
                identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                if original_stat is None or identity(current) != identity(original_stat):
                    return None, "存档在读取期间发生变化，已保留原文件；请重新打开对局。"
                if original_text is not None and self.storage_path.read_text(encoding="utf-8-sig") != original_text:
                    return None, "存档在读取期间发生变化，已保留原文件；请重新打开对局。"
            except (OSError, UnicodeError):
                return None, "存档暂时无法读取，原文件已保留。"
            backup = self.storage_path.with_name(
                self.storage_path.stem + ".unreadable-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6] + ".json")
            try:
                if cancellation_event is not None and cancellation_event.is_set():
                    return None, ""
                self.storage_path.replace(backup)
                return None, "原存档无法读取，已另存保留；可以开始新对局。"
            except OSError:
                if cancellation_event is not None and cancellation_event.is_set():
                    return None, ""
                self._save_state.blocked = True
                return None, "原存档无法读取，文件已保留；本次对局暂不覆盖它。"

    def _rebuild_records(self):
        replay = XiangqiBoard(self.board.initial_fen)
        self._records = []
        for move in self.board.moves:
            self._records.append({"move": move, "label": replay.move_label(move), "side": replay.turn})
            replay.push(move)

    def start(self):
        if self._closed or self.busy:
            return False
        if self.engine_ready:
            self._start_ai_if_needed()
            self._emit()
            return True
        self.busy, self.error, self.status = True, "", "正在准备本地棋力…"
        generation = self._generation
        event = threading.Event()
        self._job_cancel = event
        load_storage = not self._loaded_storage
        self._emit()
        def prepare():
            restored, warning = self._read_saved(event) if load_storage else (None, "")
            name = self.engine.start(cancellation_event=event)
            return restored, warning, name
        def ready(result, error):
            if self._closed or generation != self._generation:
                return
            self.busy = False
            if error:
                self.error = error
                self.status = "本地引擎暂未就绪；点重试继续。"
                self._emit()
                return
            restored, warning, name = result
            self._loaded_storage = True
            self._storage_warning = warning
            if restored:
                value, self.board = restored
                self.human_side, self.difficulty = value["human_side"], value["difficulty"]
                self._commentary = [dict(item) for item in value.get("commentary", [])]
                self._result = value.get("result")
                self._rebuild_records()
                # Restoring is a real board replacement, even during first
                # startup. Invalidate commentary captured from the empty board.
                self._generation += 1
                self.status = "上次的棋局已恢复。"
            else:
                self.status = "你执红先行，想好第一步了吗？" if self.human_side == "red" else "我先来，你慢慢想。"
            self.engine_ready, self.engine_label = True, name + " · 本地计算"
            self._emit()
            self._start_ai_if_needed()
        self.runner.submit(prepare, ready)
        return True

    def _invalidate(self):
        self._generation += 1
        if self._job_cancel is not None:
            self._job_cancel.set()
        self.engine.cancel()
        self.busy = False
        self.error = ""

    def new_game(self, human_side="red", difficulty="standard"):
        if self._closed or human_side not in {"red", "black"} or difficulty not in DIFFICULTIES:
            return False
        self._invalidate()
        self._loaded_storage = True
        self.board = XiangqiBoard()
        self.human_side, self.difficulty = human_side, difficulty
        self._records, self._commentary = [], []
        self._result = self._hint = self._analysis = None
        self.status = "你执红先行。" if human_side == "red" else "这盘我执红先行。"
        self._save()
        self.post_commentary("你先走，我接招。" if human_side == "red" else "这盘我先。", "happy")
        if self.engine_ready:
            self._start_ai_if_needed()
        else:
            self.start()
        return True

    def play_move(self, move):
        if self._closed or self.busy or not self.engine_ready or self._result or self.board.outcome():
            return False
        if self.board.turn != self.human_side or not self.board.is_legal(move):
            return False
        label, side = self.board.move_label(move), self.board.turn
        self.board.push(move)
        self._records.append({"move": move, "label": label, "side": side})
        self._generation += 1
        self._hint = None
        self._analysis = None
        self._finish_position()
        self._save()
        self._emit()
        self._start_ai_if_needed()
        return True

    def _finish_position(self):
        result = self._result or self.board.outcome()
        if result:
            self._result = result
            if result["winner"] == "draw":
                self.status = "和棋，要不要再来一盘？"
            elif result["winner"] == self.human_side:
                self.status = "这盘你赢了，下得不错！"
            else:
                self.status = "这盘我赢啦，再来挑战我吧。"
        elif self.board.in_check():
            self.status = "将军，先应将。" if self.board.turn == self.human_side else "轮到我应将。"
        else:
            self.status = "轮到你了，慢慢想。" if self.board.turn == self.human_side else "鲸鱼娘正在想下一步…"

    def _start_ai_if_needed(self):
        if (self._closed or self.busy or not self.engine_ready or self._result
                or self.board.outcome() or self.board.turn == self.human_side):
            return
        self._calculate(hint=False)

    def _calculate(self, *, hint):
        generation, fen = self._generation, self.board.fen
        side_to_move = self.board.turn
        legal_moves = frozenset(self.board.legal_moves())
        start_fen, moves = self.board.initial_fen, tuple(self.board.moves)
        tuning = DIFFICULTIES[self.difficulty]
        options = {"movetime_ms": 700, "multipv": 1} if hint else {
            "movetime_ms": tuning["movetime_ms"], "multipv": tuning["multipv"]}
        event = threading.Event()
        selector = None
        if not hint and self.difficulty == "casual" and callable(self.move_selector_factory):
            try:
                candidate_selector = self.move_selector_factory(self.snapshot())
                if callable(candidate_selector):
                    selector = candidate_selector
            except Exception:
                pass  # Optional conversation setup cannot disable local chess.
        self._job_cancel = event
        self.busy, self.error = True, ""
        self.status = "正在帮你看这一步…" if hint else "鲸鱼娘正在思考…"
        self._emit()
        def work():
            if event.is_set():
                raise EngineCancelled("本次计算已取消。")
            analysis = self.engine.analyse(start_fen, moves, cancellation_event=event, **options)
            candidates = self._candidate_pool(analysis, legal_moves, tuning["loss_cp"])
            selection = None
            if selector is not None and candidates and not event.is_set():
                try:
                    proposed = selector([dict(item) for item in candidates], cancellation_event=event)
                    allowed = {item["move"] for item in candidates}
                    if isinstance(proposed, dict) and proposed.get("move") in allowed:
                        reply = proposed.get("reply", "")
                        emotion = proposed.get("emotion", "neutral")
                        selection = {"move": proposed["move"],
                                     "reply": reply.strip()[:4000] if isinstance(reply, str) else "",
                                     "emotion": emotion if emotion in {"neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking"} else "neutral"}
                except Exception:
                    pass  # API failures and untrusted moves fall back locally.
            return analysis, selection, candidates
        def complete(result, error):
            if self._closed or generation != self._generation or fen != self.board.fen:
                return
            self.busy = False
            if error:
                self.error = error
                self.status = "这次计算没有完成，棋局已保留；点重试继续。"
                self.engine_ready = False
                self._emit()
                return
            self.engine_ready = True
            analysis, selection, candidates = result
            self._analysis = analysis.to_dict()
            self._analysis.update(source_fen=fen, side_to_move=side_to_move,
                                  score_perspective=side_to_move,
                                  purpose="hint" if hint else "ai_move")
            if hint:
                if not self.board.is_legal(analysis.bestmove):
                    self.error = "提示与当前棋局不一致，请重试。"
                else:
                    self._hint = analysis.bestmove
                    self.status = "可以考虑这一步，棋盘上已经标出来了。"
                    self.post_commentary("可以考虑「" + self.board.move_label(analysis.bestmove) + "」。", "thinking")
                    self.commentary_requested.emit("请解释刚刚标出的提示走法。", self.snapshot())
                self._emit()
                return
            move = analysis.bestmove
            if candidates:
                move = self._random.choice(candidates)["move"]
            if selection is not None:
                move = selection["move"]
            if not self.board.is_legal(move):
                self.error = "引擎走法未通过当前棋局校验，请重试。"
                self.engine_ready = False
                self._emit()
                return
            label, side = self.board.move_label(move), self.board.turn
            self.board.push(move)
            self._records.append({"move": move, "label": label, "side": side})
            self._generation += 1
            self._analysis["played_move"] = move
            self._analysis["move_source"] = "model" if selection is not None else "engine"
            self._hint = None
            self._finish_position()
            self._save()
            self._emit()
            # An empty model reply intentionally stays quiet on routine moves.
            if selection is not None and selection["reply"]:
                self.post_commentary(selection["reply"], selection["emotion"])
            elif self._result:
                self.post_commentary(self.status, "happy" if self._result["winner"] != self.human_side else "surprised")
            elif self.board.in_check():
                self.post_commentary("将军。", "happy")
            if selection is None and (self._result or self.board.in_check() or len(self.board.moves) % 4 == 0):
                self.commentary_requested.emit("", self.snapshot())
        self.runner.submit(work, complete)

    @staticmethod
    def _candidate_pool(analysis, legal_moves, loss_cp):
        """The model and local sampler receive exactly the same bounded pool."""
        reference = next((line for line in analysis.lines if line.move == analysis.bestmove), None)
        if reference is None:
            reference_dict = {"move": analysis.bestmove, "score_cp": None, "mate": None, "depth": 0, "pv": []}
        else:
            reference_dict = reference.to_dict()
        if analysis.bestmove not in legal_moves:
            return []
        if not loss_cp or reference is None or reference.mate is not None or reference.score_cp is None:
            return [reference_dict]
        candidates = []
        seen = set()
        for line in analysis.lines:
            if (line.move in legal_moves and line.move not in seen and line.mate is None
                    and line.score_cp is not None and reference.score_cp-line.score_cp <= loss_cp):
                candidates.append(line.to_dict())
                seen.add(line.move)
        if analysis.bestmove not in seen:
            candidates.insert(0, reference_dict)
        return candidates

    def undo(self):
        if self._closed:
            return False
        human_indices = [index for index, item in enumerate(self._records) if item["side"] == self.human_side]
        if not human_indices:
            return False
        self._invalidate()
        target = human_indices[-1]
        while len(self._records) > target:
            self.board.undo()
            self._records.pop()
        self._result = self._hint = self._analysis = None
        self.status = "已退回到你上一步之前。"
        self._save()
        self.post_commentary("这步收回，重新想想。", "neutral")
        self._emit()
        if not self.engine_ready:
            self.start()
        return True

    def request_hint(self):
        if (self._closed or self.busy or not self.engine_ready or self._result
                or self.board.outcome() or self.board.turn != self.human_side):
            return False
        self._calculate(hint=True)
        return True

    def resign(self):
        if self._closed or self._result or self.board.outcome():
            return False
        self._invalidate()
        self._result = {"winner": "black" if self.human_side == "red" else "red", "reason": "resign"}
        self.status = "这盘先到这里，随时可以重新挑战。"
        self._save()
        self.post_commentary("没关系，歇一下，再来一盘也可以。", "neutral")
        return True

    def set_difficulty(self, code):
        if self._closed or code not in DIFFICULTIES:
            return False
        was_busy = self.busy
        self._invalidate()
        self.difficulty = code
        self.status = "已切换为" + DIFFICULTIES[code]["label"] + "。"
        self._save()
        self._emit()
        if not self.engine_ready:
            self.start()
        elif was_busy or self.board.turn != self.human_side:
            self._start_ai_if_needed()
        return True

    def ask(self, question):
        if self._closed or not isinstance(question, str) or not question.strip() or len(question) > 2000:
            return False
        self._commentary.append({"role": "user", "text": question.strip()})
        self._commentary = self._commentary[-80:]
        self._save()
        self._emit()
        self.commentary_requested.emit(question.strip(), self.snapshot())
        return True

    def set_commentary_busy(self, busy):
        self.commentary_busy = bool(busy)
        self._emit()

    def post_commentary(self, text, emotion="neutral"):
        if self._closed or not isinstance(text, str) or not text.strip():
            return
        self._commentary.append({"role": "assistant", "text": text.strip()[:4000]})
        self._commentary = self._commentary[-80:]
        self._save()
        self._emit()
        self.speech.emit(text.strip()[:4000], emotion)

    def export_record(self, path):
        target = Path(path)
        payload = "鲸鱼娘象棋棋谱\n" + "\n".join(
            f"{i//2+1}. {'红' if item['side']=='red' else '黑'}：{item['label']}"
            for i, item in enumerate(self._records))
        payload += "\n\n当前局面：" + self.board.fen + "\n"
        self.runner.submit(lambda: target.write_text(payload, encoding="utf-8"),
                           lambda _result, error: self._export_finished(error))

    def _export_finished(self, error):
        self.error = error
        self.status = "棋谱导出失败，请检查目标目录。" if error else "棋谱已导出。"
        self._emit()

    def shutdown(self):
        if self._closed:
            return
        self._save()
        self._closed = True
        self._generation += 1
        if self._job_cancel is not None:
            self._job_cancel.set()
        self.engine.cancel()
        self.runner.shutdown(on_drained=self.engine.shutdown)
