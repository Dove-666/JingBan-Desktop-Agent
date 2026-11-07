"""Cancellable local Gomoku matches with independent dialogue and durable replay."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import random
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, QStandardPaths, Signal

from ..async_tasks import TaskRunner
from ..emotion_policy import normalize_emotion
from .gomoku import GomokuBoard, RULES, other_side
from .pikafish import EngineCancelled
from .rapfi import RapfiEngine
from .xiangqi_controller import _SaveState, XiangqiController


DIFFICULTIES = {
    "casual": {"label": "轻松陪玩", "movetime_ms": 180, "multipv": 6, "max_score_drop": 220},
    "standard": {"label": "日常切磋", "movetime_ms": 500, "multipv": 3, "max_score_drop": 60},
    "serious": {"label": "认真对弈", "movetime_ms": 1200, "multipv": 1, "max_score_drop": 0},
}


class GomokuController(QObject):
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
            storage_path = directory / "games" / "gomoku.json"
        self.storage_path = Path(storage_path)
        self._save_state = _SaveState(self.storage_path)
        self._save_revision = self._saved_revision = 0
        self.runner = TaskRunner(self)
        self.engine = RapfiEngine(engine_path)
        self.move_selector_factory = None
        self.board = GomokuBoard()
        self.human_side = "black"
        self.difficulty = "standard"
        self.busy = self.commentary_busy = False
        self.engine_ready = False
        self.engine_label = "Rapfi · 本地计算"
        self.status = "准备和鲸鱼娘下一盘五子棋。"
        self.error = ""
        self._commentary = []
        self._hint = self._analysis = self._result = None
        self._generation = 0
        self._job_cancel = None
        self._closed = self._loaded_storage = False
        self._storage_warning = ""
        self._random = random.Random()

    @property
    def revision(self):
        return self._generation

    @property
    def rule(self):
        return self.board.rule

    def _threat(self):
        if self._result or self.board.outcome():
            return None
        for side in (self.board.turn, other_side(self.board.turn)):
            moves = self.board.winning_moves(side)
            if moves:
                return {"side": side, "moves": list(moves)}
        return None

    def snapshot(self):
        outcome = self._result or self.board.outcome()
        latest = self.board.history[-1] if self.board.history else None
        return {
            "board": [list(row) for row in self.board.squares], "size": self.board.size,
            "position_key": self.board.position_key, "turn": self.board.turn,
            "human_side": self.human_side, "difficulty": self.difficulty,
            "difficulty_label": DIFFICULTIES[self.difficulty]["label"],
            "rule": self.rule, "rule_label": RULES[self.rule],
            "busy": self.busy, "commentary_busy": self.commentary_busy,
            "status": self.status, "error": self.error,
            "engine_ready": self.engine_ready, "engine_label": self.engine_label,
            "last_move": latest.move if latest else None,
            "last_move_details": {key: getattr(latest, key) for key in
                                  ("move", "label", "side", "piece")} if latest else None,
            "legal_moves": list(self.board.legal_moves()) if outcome is None else [],
            "moves": [{"move": record.move, "label": record.label, "side": record.side}
                      for record in self.board.history],
            "outcome": dict(outcome) if outcome else None,
            "winning_line": self.board.winning_line if outcome else [],
            "threat": self._threat(),
            "commentary": [dict(row) for row in self._commentary], "hint": self._hint,
            "can_undo": any(record.side == self.human_side for record in self.board.history),
            "can_new": True, "saved": (self._loaded_storage and not self._storage_warning
                                      and self._saved_revision == self._save_revision),
            "storage_warning": self._storage_warning, "revision": self._generation,
            "analysis": self._analysis,
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
        if self._closed or not self._loaded_storage or self._save_state.blocked:
            return
        self._save_revision += 1
        version, payload, state = self._save_revision, self._payload(), self._save_state
        state.latest = version

        def complete(_result, error):
            if self._closed or version != self._save_revision:
                return
            if error:
                self._storage_warning = "棋局暂未保存成功，请检查保存目录是否可写。"
            else:
                self._saved_revision = version
                self._storage_warning = ""
            self._emit()

        self.runner.submit(lambda: state.write(version, payload), complete)

    def _read_saved(self, cancellation_event):
        with self._save_state.lock:
            if cancellation_event.is_set() or not self.storage_path.is_file():
                return None, ""
            original_stat = original_text = None
            try:
                original_stat = self.storage_path.stat()
                if original_stat.st_size > 2*1024*1024:
                    raise ValueError
                original_text = self.storage_path.read_text(encoding="utf-8-sig")
                value = json.loads(original_text)
                if (not isinstance(value, dict) or value.get("schema_version") != 1
                        or value.get("human_side") not in {"black", "white"}
                        or value.get("difficulty") not in DIFFICULTIES):
                    raise ValueError
                board = GomokuBoard.from_payload(value["board"])
                commentary = value.get("commentary", [])
                if (not isinstance(commentary, list) or len(commentary) > 80
                        or any(not isinstance(row, dict) or row.get("role") not in {"assistant", "user"}
                               or not isinstance(row.get("text"), str) or len(row["text"]) > 4000
                               for row in commentary)):
                    raise ValueError
                result = value.get("result")
                if result is not None:
                    if (not isinstance(result, dict) or result.get("winner") not in {"black", "white", "draw"}
                            or result.get("reason") not in {"resign", "five", "full_board"}):
                        raise ValueError
                    if result["reason"] == "resign":
                        if board.outcome() or result["winner"] != other_side(value["human_side"]):
                            raise ValueError
                    elif result != board.outcome():
                        raise ValueError
                return (value, board), ""
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
                if cancellation_event.is_set():
                    return None, ""
                try:
                    current = self.storage_path.stat()
                    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                    if original_stat is None or identity(current) != identity(original_stat):
                        self._save_state.blocked = True
                        return None, "存档在读取期间发生变化，已保留原文件；请重启桌宠后再恢复。"
                    if original_text is not None and self.storage_path.read_text(encoding="utf-8-sig") != original_text:
                        self._save_state.blocked = True
                        return None, "存档在读取期间发生变化，已保留原文件；请重启桌宠后再恢复。"
                    backup = self.storage_path.with_name(self.storage_path.stem+".unreadable-"+
                        datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid4().hex[:6]+".json")
                    if cancellation_event.is_set():
                        return None, ""
                    self.storage_path.replace(backup)
                    return None, "原棋局无法读取，已另存保留；可以开始新的一局。"
                except (OSError, UnicodeError):
                    self._save_state.blocked = True
                    return None, "原棋局暂时无法读取，文件已保留；本次暂不覆盖它。"

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
                self.status = "本地棋力暂未就绪，点重试继续。"
                self._emit()
                return
            restored, warning, name = result
            self._loaded_storage, self._storage_warning = True, warning
            if restored:
                value, self.board = restored
                self.human_side, self.difficulty = value["human_side"], value["difficulty"]
                self._commentary = [dict(row) for row in value.get("commentary", [])]
                self._result = value.get("result")
                self._generation += 1
                self.status = "上次的棋局已恢复。"
                if self._result or self.board.outcome():
                    self._finish_position()
            else:
                self.status = "你执黑先行，选个位置吧。" if self.human_side == "black" else "这盘我先。"
            self.engine_ready, self.engine_label = True, name+" · 本地计算"
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

    def new_game(self, human_side="black", difficulty="standard", rule="freestyle"):
        if (self._closed or human_side not in {"black", "white"}
                or difficulty not in DIFFICULTIES or rule not in RULES):
            return False
        self._invalidate()
        self._loaded_storage = True
        self.board = GomokuBoard(rule)
        self.human_side, self.difficulty = human_side, difficulty
        self._commentary = []
        self._result = self._hint = self._analysis = None
        self.status = "你执黑先行。" if human_side == "black" else "这盘我执黑先行。"
        self.post_commentary("你先落子，我接招。" if human_side == "black" else "这盘我先。", "happy")
        if self.engine_ready:
            self._start_ai_if_needed()
        else:
            self.start()
        return True

    def set_rule(self, rule):
        if self._closed or rule not in RULES:
            return False
        if rule == self.rule:
            return True
        return self.new_game(self.human_side, self.difficulty, rule)

    def play_move(self, move):
        if (self._closed or self.busy or not self.engine_ready or self._result or self.board.outcome()
                or self.board.turn != self.human_side or not self.board.is_legal(move)):
            return False
        self.board.push(move)
        self._generation += 1
        self._hint = self._analysis = None
        self._finish_position()
        self._save()
        self._emit()
        if self._result:
            self.post_commentary(self.status, "neutral" if self._result["winner"] == "draw" else "surprised")
        self._start_ai_if_needed()
        return True

    def _finish_position(self):
        result = self._result or self.board.outcome()
        if result:
            self._result = result
            if result["reason"] == "resign":
                self.status = ("黑方" if self.human_side == "black" else "白方")+"已认输，本局结束。"
            elif result["winner"] == "draw":
                self.status = "棋盘下满啦，这盘和棋。"
            elif result["winner"] == self.human_side:
                self.status = "连成五子，这盘你赢了！"
            else:
                self.status = "五子连成线，这盘我赢啦。"
        else:
            self.status = "轮到你落子。" if self.board.turn == self.human_side else "鲸鱼娘正在想下一步…"

    def _start_ai_if_needed(self):
        if (self._closed or self.busy or not self.engine_ready or self._result or self.board.outcome()
                or self.board.turn == self.human_side):
            return
        self._calculate(hint=False)

    def _calculate(self, *, hint):
        generation, position = self._generation, self.board.position_key
        rows, side, rule = self.board.squares, self.board.turn, self.rule
        legal = frozenset(self.board.legal_moves())
        tuning = DIFFICULTIES[self.difficulty]
        options = {"movetime_ms": 700, "multipv": 1} if hint else {
            "movetime_ms": tuning["movetime_ms"], "multipv": tuning["multipv"]}
        event = threading.Event()
        selector = None
        if not hint and self.difficulty == "casual" and callable(self.move_selector_factory):
            try:
                candidate = self.move_selector_factory(self.snapshot())
                if callable(candidate):
                    selector = candidate
            except Exception:
                pass
        self._job_cancel = event
        self.busy, self.error = True, ""
        self.status = "正在帮你找落点…" if hint else "鲸鱼娘正在思考…"
        self._emit()

        def work():
            if event.is_set():
                raise EngineCancelled("本次计算已取消。")
            analysis = self.engine.analyse(rows, turn=side, rule=rule, cancellation_event=event, **options)
            candidates = XiangqiController._candidate_pool(analysis, legal, tuning["max_score_drop"])
            selection = None
            if selector is not None and candidates and not event.is_set():
                try:
                    proposed = selector([dict(row) for row in candidates], cancellation_event=event)
                    if isinstance(proposed, dict) and proposed.get("move") in {row["move"] for row in candidates}:
                        reply = proposed.get("reply", "")
                        selection = {"move": proposed["move"],
                                     "reply": reply.strip()[:4000] if isinstance(reply, str) else "",
                                     "emotion": normalize_emotion(proposed.get("emotion"))}
                except Exception:
                    pass
            return analysis, candidates, selection

        def complete(result, error):
            if self._closed or generation != self._generation or position != self.board.position_key:
                return
            self.busy = False
            if error:
                self.error, self.engine_ready = error, False
                self.status = "这次计算没有完成，棋局已保留；点重试继续。"
                self._emit()
                return
            analysis, candidates, selection = result
            self.engine_ready = True
            self._analysis = analysis.to_dict()
            self._analysis.update(source_position=position, side_to_move=side, score_perspective=side,
                                  purpose="hint" if hint else "ai_move")
            if hint:
                if self.board.is_legal(analysis.bestmove):
                    self._hint = self.board.move_label(analysis.bestmove)
                    self.status = "提示落点已标出来了。"
                    self.post_commentary("可以考虑「"+self._hint+"」。", "thinking")
                    self.commentary_requested.emit("请解释刚才标出的提示落点。", self.snapshot())
                else:
                    self.error = "提示与当前棋局不一致，请重试。"
                self._emit()
                return
            move = self._random.choice(candidates)["move"] if candidates else analysis.bestmove
            if selection is not None:
                move = selection["move"]
            if not self.board.is_legal(move):
                self.error, self.engine_ready = "引擎落点未通过当前棋局校验，请重试。", False
                self._emit()
                return
            record = self.board.push(move)
            self._generation += 1
            self._analysis.update(played_move=record.move, move_source="model" if selection else "engine")
            self._hint = None
            self._finish_position()
            self._save()
            self._emit()
            if selection is not None and selection["reply"]:
                self.post_commentary(selection["reply"], selection["emotion"])
            elif self._result:
                self.post_commentary(self.status, "happy" if self._result["winner"] != self.human_side else "surprised")
            ai_turns = sum(record.side != self.human_side for record in self.board.history)
            if selection is None and (self._result or ai_turns % 3 == 0 or self._threat()):
                self.commentary_requested.emit("", self.snapshot())

        self.runner.submit(work, complete)

    def undo(self):
        if self._closed:
            return False
        human_indices = [index for index, record in enumerate(self.board.history) if record.side == self.human_side]
        if not human_indices:
            return False
        self._invalidate()
        target = human_indices[-1]
        while len(self.board.moves) > target:
            self.board.undo()
        self._result = self._hint = self._analysis = None
        self.status = "已退回到你上一步之前。"
        self._save()
        self._emit()
        if not self.engine_ready:
            self.start()
        return True

    def request_hint(self):
        if (self._closed or self.busy or not self.engine_ready or self._result or self.board.outcome()
                or self.board.turn != self.human_side):
            return False
        self._calculate(hint=True)
        return True

    def resign(self):
        if self._closed or self._result or self.board.outcome():
            return False
        self._invalidate()
        self._result = {"winner": other_side(self.human_side), "reason": "resign"}
        self._finish_position()
        self.post_commentary(self.status, "neutral")
        return True

    def set_difficulty(self, code):
        if self._closed or code not in DIFFICULTIES:
            return False
        self._invalidate()
        self.difficulty = code
        self.status = "已切换为"+DIFFICULTIES[code]["label"]+"。"
        self._save()
        self._emit()
        if not self.engine_ready:
            self.start()
        else:
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
        text = text.strip()[:4000]
        self._commentary.append({"role": "assistant", "text": text})
        self._commentary = self._commentary[-80:]
        self._save()
        self._emit()
        self.speech.emit(text, normalize_emotion(emotion))

    def export_record(self, path):
        payload = {"game": "gomoku", "rule": self.rule, "size": self.board.size,
                   "human_side": self.human_side, "moves": list(self.board.moves),
                   "result": self._result or self.board.outcome(), "position": self.board.position_key}
        target = Path(path)
        lines = ["鲸鱼娘五子棋棋谱", RULES[self.rule]]
        lines.extend(f"{index+1}. {'黑' if record.side == 'black' else '白'}：{record.move}"
                     for index, record in enumerate(self.board.history))
        text = "\n".join(lines)+"\n\n"+json.dumps(payload, ensure_ascii=False, indent=2)+"\n"

        def complete(_result, error):
            if self._closed:
                return
            self.error = error
            self.status = "棋谱导出失败，请检查目标目录。" if error else "棋谱已导出。"
            self._emit()

        self.runner.submit(lambda: target.write_text(text, encoding="utf-8"), complete)

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
