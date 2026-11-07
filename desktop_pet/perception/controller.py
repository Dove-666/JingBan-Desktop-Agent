"""Qt orchestration: local observation -> optional analysis -> conservative action."""

import threading
import time
import math
from dataclasses import replace
from uuid import uuid4
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from ..api_client import ApiError, redact_secret
from ..character_cards import CharacterCard
from ..animation_controller import AnimationState
from ..settings_manager import CredentialsError, SettingsError
from .change_detector import ChangeDetector
from .context_analyzer import ContextAnalyzer, ContextEvent
from .event_deduplicator import EventDeduplicator
from .proactive_agent import Decision, MIN_CONFIDENCE, ProactiveAgent
from .screen_observer import ScreenObserver


class _Signals(QObject):
    finished = Signal(int, object, str)


class _AnalysisWorker(QRunnable):
    def __init__(self, request_id, analyzer, observation, settings, key,
                 commentary_mode="companion", requested_comment=False, *, character_card=None):
        super().__init__()
        self.signals = _Signals()
        self.request_id = request_id
        self.analyzer = analyzer
        self.observation = observation
        self._settings = settings
        self._key = key
        self.commentary_mode = commentary_mode
        self.requested_comment = requested_comment
        self.character_card = character_card
        self.cancelled = threading.Event()

    @Slot()
    def run(self):
        event, error = None, ""
        try:
            if not self.cancelled.is_set():
                character_options = ({"character_card": self.character_card}
                                     if self.character_card is not None else {})
                event = self.analyzer.analyze(
                    self.observation, self._settings, self._key,
                    commentary_mode=self.commentary_mode,
                    requested_comment=self.requested_comment,
                    **character_options,
                )
        except (ApiError, SettingsError, CredentialsError) as exc:
            error = redact_secret(str(exc), self._key)
        except Exception:
            error = "屏幕分析失败；请确认服务商和模型支持图片输入。桌宠继续运行。"
        finally:
            self._key = ""
            self.observation = None
        try:
            self.signals.finished.emit(self.request_id, event, error)
        except RuntimeError:
            pass  # Qt may already have disposed its receiver during shutdown.


class PerceptionController(QObject):
    settings_changed = Signal(object)
    status_changed = Signal(str)
    details_changed = Signal(object)
    preview_changed = Signal(QImage)
    history_changed = Signal(object)
    paused_changed = Signal(bool)
    runtime_changed = Signal(object)
    settings_requested = Signal()

    def __init__(self, settings_manager, pet_controller, chat_controller, observer=None,
                 analyzer_factory=None, clock=time.monotonic, parent=None, *,
                 source_kind="screen", settings_loader=None, settings_saver=None):
        super().__init__(parent)
        self._store = settings_manager
        if source_kind not in ("screen", "camera"):
            raise ValueError("未知的视觉来源。")
        self.source_kind = source_kind
        self.source_label = "摄像头" if source_kind == "camera" else "屏幕"
        self._settings_loader = settings_loader or self._store.load_perception_settings
        self._settings_saver = settings_saver or self._store.save_perception_settings
        self.pet_controller = pet_controller
        self.chat = chat_controller
        self.observer = observer if observer is not None else ScreenObserver()
        self._analyzer_factory = analyzer_factory or ContextAnalyzer
        self._clock = clock
        self.settings = self._settings_loader()
        self.detector = ChangeDetector(self.settings.change_threshold)
        self.deduplicator = EventDeduplicator()
        self.agent = ProactiveAgent()
        self.status = "屏幕感知已关闭；未截图、未外发。"
        self.details = {}
        self.preview = QImage()
        self._last_valid_details = {}
        self._last_observation = None
        self.history = []
        self.busy = False
        self.paused = False
        self._local_observation_pending = False
        self._closed = False
        self._worker = None
        self._pending = None
        self._saving_comment = None
        self._saving_comment_revision = None
        self._comment_commit = None
        self._preview_at = None
        self._preview_wall_time = ""
        self._last_successful_analysis_at = None
        self._request_id = 0
        self._last_analysis_at = None
        self._last_user_chat_at = None
        self._chat_revision = 0
        self._analysis_error = ""
        self._permission_save_failed = False
        self._persistence_warning = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self.chat.busy_changed.connect(self._chat_busy)
        if hasattr(self.chat, "screen_comment_finished"):
            self.chat.screen_comment_finished.connect(self._screen_comment_finished)
        if hasattr(self.chat, "conversation_invalidated"):
            self.chat.conversation_invalidated.connect(self._conversation_invalidated)
        self._apply_timer()
        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(1000)
        self._runtime_timer.timeout.connect(self._publish_runtime)
        self._runtime_timer.start()

    def runtime_snapshot(self):
        now = self._clock()
        quota = self.agent.quota_snapshot(now, self.settings)
        remaining = self._timer.remainingTime()
        return {"enabled": self.settings.enabled, "allowRemote": self.settings.allow_remote,
                "paused": self.paused, "busy": self.busy, "status": self.status, **quota,
                "analysisError": self._analysis_error,
                "permissionSaveFailed": self._permission_save_failed,
                "persistenceWarning": self._persistence_warning,
                "nextObservationSeconds": max(0, math.ceil(remaining / 1000)) if remaining >= 0 else None,
                "nextAnalysisSeconds": math.ceil(max(0, self.settings.analysis_interval_seconds -
                                                    (now - self._last_analysis_at)))
                    if self._last_analysis_at is not None else 0,
                "previewAgeSeconds": max(0, int(now - self._preview_at)) if self._preview_at is not None else None,
                "previewObservedAt": self._preview_wall_time,
                "lastAnalysisAgeSeconds": max(0, int(now - self._last_successful_analysis_at))
                    if self._last_successful_analysis_at is not None else None}

    def _publish_runtime(self):
        if not self._closed:
            self.runtime_changed.emit(self.runtime_snapshot())

    def _conversation_invalidated(self, conversation_id):
        if self._pending and self._pending[5] == conversation_id:
            self._cancel_analysis()
            self._set_status("关联会话正在清空或删除，已取消这次画面分析。")

    def _chat_busy(self, busy):
        if busy:
            self._last_user_chat_at = self._clock()
            self._chat_revision += 1
        self.status_changed.emit(self.status)

    def _set_status(self, message):
        if self.source_kind == "camera":
            message = (message.replace("屏幕", "摄像头").replace("前台窗口", "摄像头画面")
                       .replace("低分辨率前台画面", "低分辨率摄像头画面"))
        self.status = message
        self.status_changed.emit(message)
        self._publish_runtime()

    def _append_runtime_event(self, reason, status):
        row = {"时间": datetime.now().astimezone().isoformat(timespec="seconds"),
               "来源": self.source_label, "动作": "quiet", "原因": reason, "状态": status}
        self.history = (self.history + [row])[-30:]
        self.history_changed.emit([dict(item) for item in self.history])

    def _record_analysis_error(self, message):
        message = str(message)
        if message != self._analysis_error:
            self._append_runtime_event("analysis_failed", message)
        self._analysis_error = message

    def _clear_analysis_error(self):
        if self._analysis_error:
            self._analysis_error = ""
            self._append_runtime_event("analysis_recovered", "已成功取得有效识别结果；上次识别错误已解除。")

    def _record_persistence_warning(self, message, *, permission_failed=False):
        if message != self._persistence_warning:
            self._append_runtime_event("permission_save_failed", message)
        self._persistence_warning = message
        self._permission_save_failed = self._permission_save_failed or permission_failed

    def _clear_persistence_warning(self):
        self._persistence_warning = ""
        self._permission_save_failed = False

    def _set_preview(self, image):
        self.preview = QImage(image)
        self.preview_changed.emit(QImage(self.preview))

    def _clear_observation(self, reason=""):
        # A private/unknown foreground must also evict the previous safe image,
        # so reopening AURA cannot accidentally redisplay an earlier title.
        self._last_valid_details = {}
        self._last_observation = None
        self._preview_at = None
        self._preview_wall_time = ""
        self.details = {"reason": reason} if reason else {}
        self.details_changed.emit(dict(self.details))
        self._set_preview(QImage())

    def _show_previous_observation(self, reason):
        self.details = dict(self._last_valid_details)
        self.details["preview_retained"] = not self.preview.isNull()
        self.details["status"] = (
            "上次有效观察（当前前台为鲸伴，已跳过）"
            if not self.preview.isNull() else "当前前台为鲸伴，已跳过；尚无外部窗口预览"
        )
        self.details["reason"] = reason
        self.details_changed.emit(dict(self.details))
        self.preview_changed.emit(QImage(self.preview))
        self._set_status(reason + (" 正在显示上次有效观察；本次观察未截图、未外发。"
                                  if not self.preview.isNull() else " 请先切换到要观察的应用。"))

    def _apply_timer(self):
        self._timer.setInterval(self.settings.observation_interval_seconds * 1000)
        if self.paused:
            self._timer.stop()
            self._set_status("屏幕感知已暂时暂停；不截图、不发起分析。可从托盘恢复，原配置保留。")
        elif self.settings.enabled and not self._closed:
            self._timer.start()
            self._set_status("本地观察已启用；等待下一次定时观察。" + ("远程分析已允许。" if self.settings.allow_remote else "远程发送关闭。"))
        else:
            self._timer.stop()
            self._set_status("屏幕感知已关闭；未截图、未外发。")

    def set_paused(self, paused):
        """A runtime privacy switch; consent and saved preferences stay explicit."""
        if self._closed or self.paused == bool(paused):
            return
        self.paused = bool(paused)
        self.cancel_local_observation()
        self._cancel_analysis()
        self.detector.reset()
        self._clear_observation()
        self._apply_timer()
        self.paused_changed.emit(self.paused)

    def _cancel_analysis(self):
        self._request_id += 1
        worker = self._worker
        self._worker = None
        self._pending = None
        self._saving_comment = None
        self._comment_commit = None
        self.busy = False
        if worker is not None:
            worker.cancelled.set()
            try:
                worker.analyzer.shutdown()
            except Exception:
                # Logical cancellation is already effective. A transport's
                # cleanup failure must never prevent permission revocation.
                pass

    def _apply_runtime_settings(self, settings):
        self.cancel_local_observation()
        self._cancel_analysis()
        self.settings = settings
        self.detector.threshold = settings.change_threshold
        self.detector.reset()
        self._clear_observation()
        self.settings_changed.emit(settings)
        self._apply_timer()

    def revoke_permissions(self, *, local=False, remote=False) -> bool:
        """Deny first, then persist; a failed disk write cannot retain consent."""
        if self._closed or not (local or remote):
            return False
        denied = replace(
            self.settings,
            enabled=False if local else self.settings.enabled,
            allow_remote=False if remote else self.settings.allow_remote,
        )
        self._apply_runtime_settings(denied)
        scope = "本地观察" if local else "远程发送"
        try:
            self._settings_saver(denied)
        except Exception:
            message = (
                f"{scope}已立即关闭，后续请求已停止、晚到结果已禁用。"
                "关闭状态未能保存；本次运行保持关闭，下次启动请先检查权限。"
            )
            self._record_persistence_warning(message, permission_failed=True)
            self._set_status(message)
            return False
        self._clear_persistence_warning()
        self._set_status(f"{scope}已立即关闭，后续请求已停止、晚到结果已禁用；关闭状态已保存。")
        return True

    def update_settings(self, settings) -> bool:
        self.cancel_local_observation()
        if self._closed:
            return False
        try:
            settings.validate()
        except (ValueError, SettingsError, OSError):
            self._set_status("感知参数未保存，请检查范围和本地配置权限。")
            return False
        revoke_local = self.settings.enabled and not settings.enabled
        revoke_remote = self.settings.allow_remote and not settings.allow_remote
        if revoke_local or revoke_remote:
            self._apply_runtime_settings(replace(
                self.settings,
                enabled=False if revoke_local else self.settings.enabled,
                allow_remote=False if revoke_remote else self.settings.allow_remote,
            ))
        try:
            self._settings_saver(settings)
        except Exception:
            message = (
                "感知参数未保存；权限撤销已立即生效，本次运行保持关闭，下次启动请先检查权限。"
                if revoke_local or revoke_remote else "感知参数未保存，请检查范围和本地配置权限。"
            )
            if self._permission_save_failed and not (revoke_local or revoke_remote):
                message = self._persistence_warning
            self._record_persistence_warning(message, permission_failed=bool(revoke_local or revoke_remote))
            self._set_status(message)
            return False
        self._clear_persistence_warning()
        self._apply_runtime_settings(settings)
        return True

    def begin_local_observation(self) -> bool:
        """Reserve a preview-only observation ahead of a UI countdown.

        The automatic timer keeps its cadence, but poll() cannot capture or
        start analysis until the reservation is completed or cancelled.
        Existing analysis workers are never interrupted by this reservation.
        """
        if self._closed or self.paused or not self.settings.enabled:
            self._set_status("请先启用本地观察并保存参数。")
            return False
        if self.busy:
            self._set_status("屏幕分析正在进行，请等待完成后再启动本地观察。")
            return False
        if self._local_observation_pending:
            return False
        self._local_observation_pending = True
        self._set_status("已预约本地观察，请切换到目标应用；倒计时期间暂停自动观察，不调用 API。")
        return True

    def cancel_local_observation(self) -> None:
        """Release only the preview reservation; leave API budgets unchanged."""
        self._local_observation_pending = False

    def observe_now(self) -> bool:
        """Explicit preview only. This button never makes a network request."""
        self.cancel_local_observation()
        return self._observe(allow_analysis=False)

    def poll(self) -> bool:
        if self._local_observation_pending:
            return False
        return self._observe(allow_analysis=True)

    @property
    def comment_available(self) -> bool:
        """Cheap UI availability check; request-time privacy is checked separately."""
        return bool(
            not self._closed and not self.paused and self.settings.enabled and self.settings.allow_remote
            and not self.busy and not self.chat.busy and not self._local_observation_pending
            and self._last_observation is not None and not self.preview.isNull()
            and self.pet_controller.pet.animation.state != AnimationState.DRAGGING
        )

    def _manual_context_allowed(self, observation) -> bool:
        # Explicitly commenting the last safe preview is allowed while viewing
        # AURA. Other foreground changes still invalidate that preview.
        code = self.observer.current_blocked_code(self.settings)
        return code == "own_window" or (
            not code and self.observer.is_current(observation, self.settings)
        )

    def comment_current_preview(self) -> bool:
        """User-requested API comment, without taking another screenshot.

        Automatic significance, deduplication and speech quotas do not apply to
        this explicit request. Their counters are preserved, not reset.
        """
        if not self.comment_available:
            self._set_status("暂不能评论：需要启用并保存观察和远程发送、取得安全预览，并等待当前操作完成。")
            return False
        observation = self._last_observation
        if not self._manual_context_allowed(observation):
            reason = "前台窗口已变化或触发隐私规则，请重新取得安全预览后再评论。"
            self._clear_observation(reason)
            self._set_status(reason)
            return False
        app_id = f"{observation.info.process_name}:{observation.info.hwnd}"
        fingerprint = self.detector.evaluate(observation.image, app_id).fingerprint
        return self._start_analysis(observation, app_id, fingerprint, requested_comment=True)

    def _observe(self, allow_analysis):
        if self._closed or self.paused or not self.settings.enabled:
            return False
        if self.busy:
            self._set_status("屏幕分析进行中，跳过重叠观察。")
            return False
        observation = self.observer.observe(self.settings)
        if observation.blocked_reason or observation.image is None:
            reason = observation.blocked_reason or "没有可用画面。"
            if getattr(observation, "blocked_code", "") == "own_window":
                # Looking at the debug panel is not a new screen context.
                # Keep the accepted detector baseline and the safe preview.
                self._show_previous_observation(reason)
                return False
            self._clear_observation(reason)
            self.detector.reset()
            self._set_status(reason)
            return False
        app_id = f"{observation.info.process_name}:{observation.info.hwnd}"
        change = self.detector.evaluate(observation.image, app_id)
        self.details = {"process_name": observation.info.process_name, "title": observation.info.title,
                        "change_score": round(change.score, 4), "reason": change.reason,
                        "image_size": f"{observation.image.width()} × {observation.image.height()}",
                        "空闲秒数": round(observation.info.idle_seconds, 1)}
        self._preview_at = self._clock()
        self._preview_wall_time = datetime.now().astimezone().isoformat(timespec="seconds")
        self.details["observed_at"] = self._preview_wall_time
        if getattr(observation, "capture_method", ""):
            self.details["截图方式"] = observation.capture_method
        self._last_valid_details = dict(self.details)
        self._last_observation = observation
        self.details_changed.emit(dict(self.details))
        self._set_preview(observation.image)
        if not allow_analysis or not self.settings.allow_remote:
            self._set_status("本地观察完成；没有调用 API。")
            # Keep cumulative reference until remote becomes allowed; changing
            # consent resets it anyway. Local observations compare with a baseline.
            if not self.settings.allow_remote:
                self.detector.accept(observation.image, app_id)
            return True
        if not change.changed:
            self._set_status("画面基本静止，未调用 API。")
            return True
        now = self._clock()
        if self.chat.busy:
            self._set_status("用户正在聊天，暂不发起屏幕分析。")
            return True
        if self._last_analysis_at is not None and now - self._last_analysis_at < self.settings.analysis_interval_seconds:
            self._set_status("画面有变化；尚在模型请求间隔内，未调用 API。")
            return True
        if self.deduplicator.is_duplicate("frame:" + change.fingerprint, now):
            self.detector.accept(observation.image, app_id)
            self._set_status("近期已分析过这个画面，未重复调用 API。")
            return True
        self._start_analysis(observation, app_id, change.fingerprint)
        return True

    def _start_analysis(self, observation, app_id, fingerprint, requested_comment=False):
        other = getattr(self, "other_visual_controller", None)
        if other is not None and other.busy:
            self._set_status("另一视觉识别正在进行，请稍后再试。")
            return False
        try:
            api_settings = self._store.load_api_settings()
            api_settings.validate(require_model=True)
            key = self._store.get_api_key()
            if not key:
                raise SettingsError("请先在 API 设置中保存密钥。")
            loader = getattr(self._store, "load_character_card", None)
            character_card = loader() if callable(loader) else None
            if character_card is not None and not isinstance(character_card, CharacterCard):
                raise SettingsError("角色资料无效，请重新选择角色后再试。")
        except (SettingsError, CredentialsError) as exc:
            message = str(exc)
            self._record_analysis_error(message)
            self._set_status("仅本地观察：" + message)
            return False
        self._request_id += 1
        worker = _AnalysisWorker(
            self._request_id, self._analyzer_factory(), observation, api_settings, key,
            commentary_mode=self.settings.commentary_mode, requested_comment=requested_comment,
            **({"character_card": character_card} if character_card is not None else {}),
        )
        worker.signals.finished.connect(self._finished)
        self._worker = worker
        self._pending = (observation, app_id, fingerprint, requested_comment, self._chat_revision,
                         getattr(self.chat, "current_conversation_id", None),
                         self._preview_wall_time or datetime.now(timezone.utc).isoformat())
        self._last_analysis_at = self._clock()  # Failures are rate-limited as well.
        self.busy = True
        self._set_status("正在请求评论最近一次安全预览；桌宠仍可操作。" if requested_comment
                         else "正在异步分析低分辨率前台画面；桌宠仍可操作。")
        QThreadPool.globalInstance().start(worker)
        return True

    @Slot(int, object, str)
    def _finished(self, request_id, event, error):
        if self._closed or self.paused or request_id != self._request_id:
            return
        pending = self._pending
        self._worker = None
        self._pending = None
        self.busy = False
        if not pending:
            return
        observation, app_id, fingerprint, requested_comment, chat_revision, conversation_id, observed_at = pending
        if not self.settings.enabled or not self.settings.allow_remote:
            return
        context_allowed = (self._manual_context_allowed(observation) if requested_comment
                           else self.observer.is_current(observation, self.settings))
        if not context_allowed:
            current_code = getattr(self.observer, "current_blocked_code", lambda settings: "")(self.settings)
            if current_code == "own_window" and not requested_comment:
                # Keep the successful analysis as a handled frame without
                # playing a stale action while the user is inspecting AURA.
                if not error and isinstance(event, ContextEvent):
                    self.detector.accept(observation.image, app_id)
                    self.deduplicator.remember("frame:" + fingerprint, self._clock())
                reason = "当前前台为鲸伴，跳过观察；晚到分析未触发动作。"
                if error:
                    self._record_analysis_error(error)
                    reason = "屏幕分析未完成：" + error + " " + reason
                self._show_previous_observation(reason)
            else:
                reason = "当前窗口已变化或触发隐私规则，丢弃晚到结果。"
                self._clear_observation(reason)
                self._set_status(reason)
            return
        if error:
            self._record_analysis_error(error)
            self._set_status("屏幕分析未完成：" + error)
            return
        if not isinstance(event, ContextEvent):
            message = "未得到有效的结构化情境，请检查模型是否支持图片与结构化输出后重试。"
            self._record_analysis_error(message)
            self._set_status(message)
            return
        self._clear_analysis_error()
        now = self._clock()
        self._last_successful_analysis_at = now
        self.detector.accept(observation.image, app_id)
        self.deduplicator.remember("frame:" + fingerprint, now)
        if not requested_comment and self.deduplicator.is_duplicate("event:" + event.event_key, now):
            self._set_status("识别为重复事件，保持安静。")
            return
        dragging = self.pet_controller.pet.animation.state == AnimationState.DRAGGING
        recently_chatting = self._last_user_chat_at is not None and now - self._last_user_chat_at < 60
        if requested_comment:
            if self.chat.busy or dragging or chat_revision != self._chat_revision:
                decision = Decision("quiet", "user_busy")
            elif event.kind == "none" and not event.express:
                decision = Decision("quiet", "no_actionable_event")
            elif event.confidence < MIN_CONFIDENCE:
                decision = Decision("quiet", "low_confidence")
            elif event.express and (event.kind == "none" or not event.reply.strip()):
                decision = Decision("expression", "context_expression")
            elif not event.reply.strip():
                decision = Decision("quiet", "no_reply")
            else:
                decision = Decision("speak", "manual_comment")
        else:
            decision = self.agent.decide(event, now, self.settings,
                                         busy=self.chat.busy or dragging or recently_chatting
                                              or chat_revision != self._chat_revision,
                                         idle_seconds=observation.info.idle_seconds)
        row = {"时间": datetime.now().astimezone().isoformat(timespec="seconds"),
               "来源": self.source_label, "情境": event.summary, "动作": decision.action, "原因": decision.reason,
               "分类": event.kind, "重要度": event.importance, "置信度": event.confidence}
        saving = False
        if decision.action == "speak":
            if conversation_id and hasattr(self.chat, "record_screen_comment"):
                try:
                    reference = self.chat.record_screen_comment(
                        conversation_id, event.reply, event_id=str(uuid4()),
                        app_name=observation.info.process_name, context_summary=event.summary,
                        event_type=event.kind, emotion=event.emotion, observed_at=observed_at,
                        **({"source_kind": "camera"} if self.source_kind == "camera" else {}))
                except Exception:
                    reference = None
                if reference:
                    self._saving_comment = reference["message_id"]
                    self._saving_comment_revision = self._chat_revision
                    self._comment_commit = (decision, event.event_key, requested_comment, row)
                    self.busy = True
                    saving = True
                    row["动作"] = "saving"
                    row["状态"] = "评论已生成，等待保存"
                else:
                    row.update({"动作": "quiet", "原因": "save_failed", "状态": "未能提交保存；未消耗发言次数"})
            else:
                self.pet_controller.respond(event.reply, event.emotion)
                if not requested_comment:
                    self.agent.record(decision, now)
                    self.deduplicator.remember("event:" + event.event_key, now)
        elif decision.action == "motion":
            if callable(getattr(self.pet_controller, "apply_context_expression", None)):
                self.pet_controller.play_motion(event.emotion, source=self.source_kind)
            else:
                self.pet_controller.play_motion(event.emotion)
        elif decision.action == "expression":
            apply_expression = getattr(self.pet_controller, "apply_context_expression", None)
            applied = bool(apply_expression and apply_expression(event.emotion, source=self.source_kind))
            if not applied:
                row.update({"动作": "quiet", "原因": "auto_expression_disabled"})
        if not requested_comment and decision.action != "speak":
            self.agent.record(decision, now)
            self.deduplicator.remember("event:" + event.event_key, now)
        if event.reply:
            row["候选评论"] = event.reply
        if decision.action == "speak":
            row["鲸鱼娘"] = event.reply
        self.history = (self.history + [row])[-30:]
        self.history_changed.emit([dict(item) for item in self.history])
        descriptions = {
            "manual_comment": "已按你的请求发表评论", "companion_event": "已发表评论，陪你看看画面",
            "important_event": "发现重要情境，已发表评论", "cooldown": "主动发言冷却中，本次只做动作",
            "hourly_limit": "已达到每小时主动发言上限，本次只做动作",
            "no_actionable_event": "模型未发现适合评论的内容，保持安静",
            "no_reply": "模型未提供评论内容", "low_confidence": "画面识别置信度不足，保持安静",
            "low_importance": "当前模式下内容重要度不足，本次只做动作",
            "user_busy": "正在聊天或拖动，暂不打扰", "user_idle": "长时间无操作，暂不打扰",
            "context_expression": "已按当前语境更新表情，不发表评论",
            "auto_expression_disabled": "自动表情已关闭，保留手动表情",
        }
        if saving:
            self._set_status("评论已生成，正在保存；尚未显示气泡或消耗发言次数。")
        elif row["原因"] == "save_failed":
            self._set_status("屏幕评论未能提交保存；未显示气泡，发言次数与冷却未消耗。")
        else:
            reason = row["原因"]
            description = descriptions.get(reason, "情境分析完成")
            if decision.action == "expression" and row["动作"] == "expression":
                description = "API 已按当前语境选择表情，本次不发表评论"
            self._set_status(f"{description}（{reason}）。")
        self._publish_runtime()

    def _screen_comment_finished(self, reference, error):
        if self._closed or not reference or reference.get("message_id") != self._saving_comment:
            return
        self._saving_comment = None
        commit, self._comment_commit = self._comment_commit, None
        self.busy = False
        row = commit[3] if commit else None
        if error:
            if row is not None:
                row.update({"动作": "quiet", "原因": "save_failed", "状态": "保存失败，未消耗发言次数"})
            self._set_status("屏幕评论未保存，未显示气泡；发言次数与冷却未消耗：" + error)
        elif (not self.paused and self.settings.enabled and self.settings.allow_remote and not self.chat.busy
              and self._saving_comment_revision == self._chat_revision
              and self.pet_controller.pet.animation.state != AnimationState.DRAGGING
              and self.observer.current_blocked_code(self.settings) != "privacy"):
            self.pet_controller.respond(reference["content"], reference["emotion"], conversation_link=reference)
            if commit and not commit[2]:
                self.agent.record(commit[0], self._clock())
            if row is not None:
                row.update({"动作": "speak", "状态": "已保存并显示"})
            self._set_status("评论已保存到对应对话；点击气泡可以围绕这次屏幕情境继续聊天。")
        else:
            if row is not None:
                row.update({"动作": "quiet", "原因": "saved_only", "状态": "已保存；当前操作优先，未弹气泡或消耗次数"})
            self._set_status("评论已保存在关联会话；当前操作优先，暂不弹出气泡。")
        if not error and commit and not commit[2]:
            self.deduplicator.remember("event:" + commit[1], self._clock())
        self.history_changed.emit([dict(item) for item in self.history])
        self._publish_runtime()

    def reset_session(self):
        self.cancel_local_observation()
        self._cancel_analysis()
        self.detector.reset()
        self.deduplicator.reset()
        # Do not reset speech budgets/rate limits; clearing the UI must not
        # provide a way to bypass the configured limits.
        self.history = []
        self._clear_observation()
        self.history_changed.emit([])
        self._set_status("本次情境和预览已清空；发言及请求冷却继续生效。")

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._runtime_timer.stop()
        self.cancel_local_observation()
        self._timer.stop()
        self._cancel_analysis()
        self.history = []
        self._clear_observation()
        self._set_status("屏幕感知已停止。")
