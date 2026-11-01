"""Explicit, local-first controls for the optional perception controller.

This widget never captures the desktop, calls an API, or persists an image.
The controller supplies already filtered details and session events.
"""

from dataclasses import replace
import re

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)


def _label(text: str, name: str = "hint") -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _terms(text: str) -> tuple[str, ...]:
    """Accept comma/newline lists while keeping user order and useful casing."""
    result = []
    seen = set()
    for value in re.split(r"[,，\r\n]+", text):
        value = value.strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return tuple(result)


def _one_line(value: object, maximum: int = 600) -> str:
    return " ".join(str(value).split())[:maximum]


_ACTIONS = {"quiet": "保持安静", "expression": "更新表情", "motion": "只做动作", "speak": "发表评论", "saving": "正在保存评论"}
_REASONS = {
    "no_actionable_event": "模型未发现适合互动的内容",
    "low_importance": "内容重要度较低",
    "low_confidence": "识别置信度不足",
    "no_reply": "模型没有提供评论内容",
    "cooldown": "主动发言冷却中",
    "hourly_limit": "已达到每小时主动发言上限",
    "important_event": "发现重要情境",
    "companion_event": "发现适合陪伴评论的内容",
    "user_busy": "正在聊天或拖动，暂不打扰",
    "user_idle": "用户长时间无操作，暂停互动",
    "disabled": "屏幕感知尚未启用",
    "invalid_clock": "计时信息异常",
    "invalid_settings": "感知设置无效",
    "invalid_event": "模型分析结果无效",
    "manual_comment": "用户请求评论当前画面",
    "save_failed": "评论保存失败，未消耗发言次数",
    "saved_only": "已存入对话，当前操作优先未弹气泡",
    "analysis_failed": "识别请求失败",
    "analysis_recovered": "识别已恢复",
    "permission_save_failed": "设置保存失败",
    "device_error": "摄像头设备错误",
    "context_expression": "按语境更新表情",
    "auto_expression_disabled": "自动表情已关闭",
}


def _event_value(key: object, value: object) -> str:
    text = _one_line(value)
    labels = _ACTIONS if key in ("动作", "action") else _REASONS if key in ("原因", "reason") else {}
    return f"{labels[text]}（{text}）" if text in labels else text


class PerceptionPanel(QWidget):
    """An embeddable fourth tab; settings remain owned by the controller."""

    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._preview_image = QImage()
        self._preview_retained = False
        self._countdown_remaining = 0
        self._revoking_permissions = False
        self._observation_timer = QTimer(self)
        self._observation_timer.setInterval(1000)
        self._observation_timer.timeout.connect(self._advance_observation_countdown)
        self.setObjectName("perceptionPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._runtime_info = {}
        runtime_row = QHBoxLayout()
        self.runtime_label = _label("屏幕感知状态由控制器实时提供。", "status")
        self.runtime_label.setObjectName("perceptionRuntimeSummary")
        runtime_row.addWidget(self.runtime_label, 1)
        self.pause_button = QPushButton("暂停感知")
        self.pause_button.setObjectName("togglePerceptionPause")
        setter = getattr(controller, "set_paused", None)
        self.pause_button.setEnabled(callable(setter))
        if callable(setter):
            self.pause_button.clicked.connect(lambda: setter(not getattr(controller, "paused", False)))
        runtime_row.addWidget(self.pause_button)
        outer.addLayout(runtime_row)
        self.runtime_error_label = _label("", "error")
        self.runtime_error_label.setObjectName("perceptionRuntimeError")
        self.runtime_error_label.hide()
        outer.addWidget(self.runtime_error_label)
        self.pause_notice = QWidget()
        pause_row = QHBoxLayout(self.pause_notice)
        pause_row.setContentsMargins(12, 6, 12, 6)
        pause_row.addWidget(_label("本次运行已暂停屏幕感知，不截图、不发起分析。", "status"), 1)
        self.resume_button = QPushButton("恢复感知")
        self.resume_button.setObjectName("resumePerceptionButton")
        self.resume_button.clicked.connect(lambda: self.controller.set_paused(False))
        pause_row.addWidget(self.resume_button)
        outer.addWidget(self.pause_notice)
        self.pause_notice.setVisible(bool(getattr(controller, "paused", False)))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(self.scroll)
        contents = QWidget()
        self.scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        layout.addWidget(_label(
            "本地观察默认关闭。仅处理前台窗口，预览只保留在内存。"
            "“3 秒后本地观察”不会调用 API；远程分析需要单独允许。"
            "“评论当前画面”会将最近安全预览发给 API，请先检查预览。"
            "点击后请切到目标应用，观察完成后返回面板查看上次预览；面板在前台时不观察自身。"
        ))

        consent = QGroupBox("观察与远程发送")
        consent_layout = QVBoxLayout(consent)
        self.enabled_checkbox = QCheckBox("启用本地前台窗口观察")
        self.enabled_checkbox.setObjectName("perceptionEnabled")
        self.enabled_checkbox.toggled.connect(self._enabled_toggled)
        consent_layout.addWidget(self.enabled_checkbox)
        self.remote_checkbox = QCheckBox("允许向已配置 API 发送低分辨率前台画面和窗口标题")
        self.remote_checkbox.setObjectName("perceptionAllowRemote")
        self.remote_checkbox.toggled.connect(self._confirm_remote)
        consent_layout.addWidget(self.remote_checkbox)
        consent_layout.addWidget(_label("取消勾选立即停止对应权限并尝试保存；重新开启需保存参数，远程发送仍需单独确认。"))
        consent_layout.addWidget(_label("隐私过滤无法保证识别所有敏感内容；请按需要补充关键词及排除进程。"))
        layout.addWidget(consent)

        parameters = QGroupBox("频率与主动发言限制")
        form = QFormLayout(parameters)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.commentary_mode_combo = QComboBox()
        self.commentary_mode_combo.setObjectName("perceptionCommentaryMode")
        self.commentary_mode_combo.addItem("陪伴评论", "companion")
        self.commentary_mode_combo.addItem("只在重要时说话", "quiet")
        form.addRow("互动风格", self.commentary_mode_combo)
        form.addRow(_label("陪伴评论可回应视频等日常内容，仍受发言冷却、次数与去重限制；不会每帧都评论。"))
        form.addRow(_label("屏幕与摄像头的主动评论共享发言次数和最近发言记录；本页频率限制适用于屏幕感知。手动请求评论不消耗主动发言次数。"))
        self.observation_interval_input = self._spin("observationInterval", 5, 30)
        form.addRow("本地观察间隔（秒）", self.observation_interval_input)
        self.analysis_interval_input = self._spin("analysisInterval", 30, 600)
        form.addRow("远程分析最短间隔（秒）", self.analysis_interval_input)
        self.change_threshold_input = QDoubleSpinBox()
        self.change_threshold_input.setObjectName("perceptionChangeThreshold")
        self.change_threshold_input.setRange(0.01, 1.0)
        self.change_threshold_input.setDecimals(2)
        self.change_threshold_input.setSingleStep(0.01)
        form.addRow("画面变化阈值", self.change_threshold_input)
        self.speech_cooldown_input = self._spin("speechCooldown", 30, 1800)
        form.addRow("主动发言冷却（秒）", self.speech_cooldown_input)
        self.max_speeches_input = self._spin("maxSpeechesPerHour", 1, 30)
        form.addRow("每小时主动发言上限", self.max_speeches_input)
        self.idle_pause_input = self._spin("idlePause", 60, 7200)
        form.addRow("用户无操作暂停阈值（秒）", self.idle_pause_input)
        layout.addWidget(parameters)

        privacy = QGroupBox("隐私排除")
        privacy_form = QFormLayout(privacy)
        privacy_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.privacy_keywords_input = QPlainTextEdit()
        self.privacy_keywords_input.setObjectName("perceptionPrivacyKeywords")
        self.privacy_keywords_input.setFixedHeight(70)
        self.privacy_keywords_input.setPlaceholderText("敏感窗口标题关键词，用逗号或换行分隔")
        privacy_form.addRow("敏感关键词", self.privacy_keywords_input)
        self.excluded_apps_input = QPlainTextEdit()
        self.excluded_apps_input.setObjectName("perceptionExcludedApps")
        self.excluded_apps_input.setFixedHeight(70)
        self.excluded_apps_input.setPlaceholderText("进程名称，例如 password-manager.exe；用逗号或换行分隔")
        privacy_form.addRow("排除进程", self.excluded_apps_input)
        layout.addWidget(privacy)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("保存参数")
        self.save_button.setObjectName("savePerceptionSettings")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        self.observe_button = QPushButton("3 秒后本地观察")
        self.observe_button.setObjectName("observeLocallyNow")
        self.observe_button.clicked.connect(self._observe_now)
        buttons.addWidget(self.observe_button)
        self.comment_button = QPushButton("评论当前画面")
        self.comment_button.setObjectName("commentCurrentPreview")
        self.comment_button.clicked.connect(self._comment_current_preview)
        buttons.addWidget(self.comment_button)
        self.reset_button = QPushButton("清空本次事件")
        self.reset_button.setObjectName("resetPerceptionSession")
        self.reset_button.clicked.connect(self._reset_session)
        buttons.addWidget(self.reset_button)
        layout.addLayout(buttons)
        self.feedback_label = _label("", "error")
        self.feedback_label.hide()
        layout.addWidget(self.feedback_label)
        self.status_label = _label("", "status")
        self.status_label.setAccessibleName("屏幕感知状态")
        layout.addWidget(self.status_label)

        self.preview_label = _label("尚无本地预览。敏感或排除窗口不会显示画面。")
        self.preview_label.setObjectName("perceptionPreview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(160)
        self.preview_label.setMaximumHeight(250)
        self.preview_label.setMinimumWidth(1)
        self.preview_label.installEventFilter(self)
        layout.addWidget(self.preview_label)
        self.preview_status_label = _label("")
        self.preview_status_label.setObjectName("perceptionPreviewStatus")
        layout.addWidget(self.preview_status_label)
        layout.addWidget(_label("当前观察详情（经隐私过滤）"))
        self.details_view = QPlainTextEdit()
        self.details_view.setObjectName("perceptionDetails")
        self.details_view.setReadOnly(True)
        self.details_view.setFixedHeight(110)
        layout.addWidget(self.details_view)
        layout.addWidget(_label("本次事件（最近 30 条，不写入聊天历史）"))
        self.history_view = QPlainTextEdit()
        self.history_view.setObjectName("perceptionHistory")
        self.history_view.setReadOnly(True)
        self.history_view.setFixedHeight(150)
        layout.addWidget(self.history_view)
        layout.addStretch()

        controller.settings_changed.connect(self._load_settings)
        controller.status_changed.connect(self._status_changed)
        controller.details_changed.connect(self._details_changed)
        controller.preview_changed.connect(self._preview_changed)
        controller.history_changed.connect(self._history_changed)
        self._load_settings(controller.settings)
        paused_signal = getattr(controller, "paused_changed", None)
        if paused_signal is not None:
            paused_signal.connect(self._paused_changed)
        self._status_changed(controller.status)
        self._preview_changed(getattr(controller, "preview", QImage()))
        self._details_changed(controller.details)
        self._history_changed(controller.history)
        runtime_signal = getattr(controller, "runtime_changed", None)
        if runtime_signal is not None:
            runtime_signal.connect(self._runtime_changed)
        snapshot = getattr(controller, "runtime_snapshot", None)
        if callable(snapshot):
            self._runtime_changed(snapshot())

    def _runtime_changed(self, info):
        if not isinstance(info, dict):
            return
        self._runtime_info = info
        messages = [str(info.get("persistenceWarning") or "")]
        if info.get("analysisError"):
            messages.append("上次识别失败：" + str(info["analysisError"]) + " 请检查 API 设置后重新评论；成功识别后会解除提示。")
        self.runtime_error_label.setText("\n".join(message for message in messages if message))
        self.runtime_error_label.setVisible(any(messages))
        mode = "已暂停" if info.get("paused") else ("观察中" if info.get("enabled") else "已关闭")
        remote = "允许远程" if info.get("allowRemote") else "仅本地"
        next_observation = info.get("nextObservationSeconds")
        observation = f"下次观察 {next_observation} 秒" if next_observation is not None else "观察计时已停止"
        last = info.get("lastAnalysisAgeSeconds")
        success = f"上次分析 {last} 秒前" if last is not None else "尚无成功分析"
        self.runtime_label.setText(
            f"{mode} · {remote} · {observation}\n"
            f"发言冷却 {info.get('cooldownSeconds', 0)} 秒 · 本小时剩余 {info.get('hourlyRemaining', 0)} 次 · "
            f"分析间隔剩余 {info.get('nextAnalysisSeconds', 0)} 秒 · {success}"
        )
        self.pause_button.setText("恢复感知" if info.get("paused") else "暂停感知")
        self._update_preview_status()

    @staticmethod
    def _spin(name: str, minimum: int, maximum: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setObjectName(name)
        spin.setRange(minimum, maximum)
        return spin

    def _load_settings(self, settings) -> None:
        if self._revoking_permissions:
            # The checkbox that initiated the revocation is already off.
            # Keep unrelated, unsaved form edits while synchronizing runtime
            # permission changes through the controller's normal signal.
            self._refresh_observe_button()
            return
        self._cancel_pending_observation()
        self.enabled_checkbox.blockSignals(True)
        self.enabled_checkbox.setChecked(settings.enabled)
        self.enabled_checkbox.blockSignals(False)
        self.remote_checkbox.blockSignals(True)
        self.remote_checkbox.setChecked(settings.allow_remote)
        self.remote_checkbox.blockSignals(False)
        self.observation_interval_input.setValue(settings.observation_interval_seconds)
        self.analysis_interval_input.setValue(settings.analysis_interval_seconds)
        self.change_threshold_input.setValue(settings.change_threshold)
        self.speech_cooldown_input.setValue(settings.speech_cooldown_seconds)
        self.max_speeches_input.setValue(settings.max_speeches_per_hour)
        self.idle_pause_input.setValue(settings.idle_pause_seconds)
        index = self.commentary_mode_combo.findData(getattr(settings, "commentary_mode", "companion"))
        self.commentary_mode_combo.setCurrentIndex(max(0, index))
        self.privacy_keywords_input.setPlainText("\n".join(settings.privacy_keywords))
        self.excluded_apps_input.setPlainText("\n".join(settings.excluded_apps))
        self._refresh_observe_button()

    def _enabled_toggled(self, enabled: bool) -> None:
        if not enabled:
            self._cancel_pending_observation()
            self._revoke_permissions(local=True)
        self._refresh_observe_button()

    def _confirm_remote(self, enabled: bool) -> None:
        if not enabled:
            self._revoke_permissions(remote=True)
            self._refresh_comment_button()
            return
        result = QMessageBox.warning(
            self, "允许发送前台画面？",
            "保存后，应用可以将低分辨率前台窗口截图及窗口标题发送到 API 设置中配置的服务商。\n\n"
            "隐私过滤无法保证识别所有敏感内容，画面中可能包含个人信息。"
            "请确认你信任该服务商并愿意发送这些内容。是否允许？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            self.remote_checkbox.blockSignals(True)
            self.remote_checkbox.setChecked(False)
            self.remote_checkbox.blockSignals(False)
        self._refresh_comment_button()

    def _revoke_permissions(self, *, local=False, remote=False) -> None:
        self._cancel_pending_observation()
        self._revoking_permissions = True
        try:
            self.controller.revoke_permissions(local=local, remote=remote)
            self._feedback(self.controller.status)
        except Exception:
            # Avoid exposing transport/backend exception contents in the UI.
            # The real controller denies before attempting persistence.
            self._feedback("权限关闭状态未能保存，请检查当前感知状态；本次操作未开启任何权限。")
        finally:
            self._revoking_permissions = False

    def _feedback(self, text: str) -> None:
        self.feedback_label.setText(text)
        self.feedback_label.setVisible(bool(text))

    def _save(self) -> None:
        self._cancel_pending_observation()
        self._feedback("")
        try:
            settings = replace(
                self.controller.settings,
                enabled=self.enabled_checkbox.isChecked(),
                allow_remote=self.remote_checkbox.isChecked(),
                commentary_mode=self.commentary_mode_combo.currentData(),
                observation_interval_seconds=self.observation_interval_input.value(),
                analysis_interval_seconds=self.analysis_interval_input.value(),
                change_threshold=self.change_threshold_input.value(),
                speech_cooldown_seconds=self.speech_cooldown_input.value(),
                max_speeches_per_hour=self.max_speeches_input.value(),
                idle_pause_seconds=self.idle_pause_input.value(),
                privacy_keywords=_terms(self.privacy_keywords_input.toPlainText()),
                excluded_apps=_terms(self.excluded_apps_input.toPlainText()),
            )
            if not self.controller.update_settings(settings):
                self._feedback("参数未保存，请检查状态提示和本地设置权限。")
            self._status_changed(self.controller.status)
        except Exception:
            self._feedback("参数未保存，请检查输入及本地设置权限后重试。")

    def _observe_now(self) -> None:
        if self._observation_timer.isActive():
            return
        if self.controller.busy:
            self._feedback("屏幕分析正在进行，请等待完成后再启动本地观察。")
            return
        if not self.controller.settings.enabled or not self.enabled_checkbox.isChecked():
            self._feedback("请先启用本地观察并保存参数。此按钮不会自动启用观察或远程发送。")
            return
        self._feedback("")
        if not self.controller.begin_local_observation():
            self._feedback(self.controller.status)
            return
        self._countdown_remaining = 3
        self._observation_timer.start()
        self._refresh_observe_button()

    def _advance_observation_countdown(self) -> None:
        if not self._observation_timer.isActive():
            return
        if not self.controller.settings.enabled or not self.enabled_checkbox.isChecked() or self.controller.busy:
            self._cancel_pending_observation()
            if self.controller.busy:
                self._feedback("屏幕分析正在进行，本地观察已取消；请稍后重试。")
            return
        self._countdown_remaining -= 1
        if self._countdown_remaining > 0:
            self._refresh_observe_button()
            return
        self._cancel_pending_observation()
        try:
            # This is deliberately the preview-only entry point, never poll().
            # The controller also rejects disabled/shutdown observations.
            self.controller.observe_now()
            self._status_changed(self.controller.status)
        except Exception:
            self._feedback("本地观察失败，请检查窗口访问权限后重试。")

    def _cancel_pending_observation(self) -> None:
        self._observation_timer.stop()
        self._countdown_remaining = 0
        self.controller.cancel_local_observation()
        if hasattr(self, "observe_button"):
            self._refresh_observe_button()

    def _refresh_observe_button(self) -> None:
        pending = self._observation_timer.isActive()
        self.observe_button.setText(
            f"{self._countdown_remaining} 秒后观察（请切换窗口）" if pending else "3 秒后本地观察"
        )
        paused = bool(getattr(self.controller, "paused", False))
        enabled = self.controller.settings.enabled and self.enabled_checkbox.isChecked() and not paused
        self.observe_button.setEnabled(enabled and not self.controller.busy and not pending)
        self.observe_button.setToolTip(
            "请先恢复本次运行的屏幕感知。" if paused else
            "请先启用本地观察并保存参数。" if not enabled else
            "屏幕分析正在进行，请等待完成后再启动本地观察。" if self.controller.busy else
            "只在倒计时结束后更新本地预览，不调用 API。请切换到目标应用。"
        )
        self._refresh_comment_button()

    def _paused_changed(self, paused):
        self.pause_notice.setVisible(paused)
        if paused:
            self._cancel_pending_observation()
        self._refresh_observe_button()

    def _refresh_comment_button(self) -> None:
        available = (
            bool(getattr(self.controller, "comment_available", False))
            and self.enabled_checkbox.isChecked() and self.remote_checkbox.isChecked()
            and not self.controller.busy
            and not bool(getattr(self.controller, "paused", False))
            and not self._observation_timer.isActive()
        )
        self.comment_button.setEnabled(available)
        self.comment_button.setToolTip(
            "调用已配置 API 评论最近一次安全预览，不重新截图。需要先启用并保存本地观察及远程发送，"
            "取得安全预览，并等待当前请求或本地观察倒计时结束。"
        )

    def _comment_current_preview(self) -> None:
        self._refresh_comment_button()
        if not self.comment_button.isEnabled():
            self._feedback("暂不能评论：请检查观察与远程发送权限、当前预览及请求状态。")
            return
        self._feedback("")
        try:
            if not self.controller.comment_current_preview():
                self._feedback(self.controller.status)
            self._status_changed(self.controller.status)
        except Exception:
            self._feedback("未能请求画面评论，请检查 API 配置和当前状态后重试。")
        self._refresh_comment_button()

    def _reset_session(self) -> None:
        answer = QMessageBox.question(
            self, "清空本次感知事件", "确定清空本次事件和内存中的观察预览吗？这不会清空聊天记录。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._cancel_pending_observation()
        self._feedback("")
        try:
            self.controller.reset_session()
            self._preview_changed(QImage())
            self._details_changed(self.controller.details)
            self._history_changed(self.controller.history)
            self._status_changed(self.controller.status)
        except Exception:
            self._feedback("未能清空本次事件，请稍后重试。")

    def _status_changed(self, status: str) -> None:
        self.status_label.setText(status)
        if self.controller.busy or not self.controller.settings.enabled:
            self._cancel_pending_observation()
        self._refresh_observe_button()

    def _details_changed(self, details: dict) -> None:
        self._preview_retained = isinstance(details, dict) and details.get("preview_retained") is True
        self._update_preview_status()
        labels = {
            "process_name": "前台进程", "process": "前台进程", "title": "窗口标题",
            "window_title": "窗口标题", "reason": "说明", "status": "状态",
            "change_score": "画面变化", "image_size": "预览尺寸", "bounds": "窗口区域",
            "observed_at": "采集时间",
        }
        if not isinstance(details, dict) or not details:
            self.details_view.setPlainText("暂无可显示的观察详情。")
            return
        self.details_view.setPlainText("\n".join(
            f"{labels.get(str(key), str(key))}：{_one_line(value)}"
            for key, value in list(details.items())[:20] if key != "preview_retained"
        ))

    def _history_changed(self, history: list) -> None:
        lines = []
        for event in list(history or [])[-30:]:
            if isinstance(event, dict):
                line = " | ".join(f"{_one_line(key, 60)}: {_event_value(key, value)}" for key, value in event.items())
            else:
                line = _one_line(event)
            lines.append(line[:1000])
        self.history_view.setPlainText("\n".join(lines))
        bar = self.history_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _preview_changed(self, image: QImage) -> None:
        self._preview_image = QImage(image) if isinstance(image, QImage) else QImage()
        if self._preview_image.isNull():
            self._preview_retained = False
        self._update_preview()
        self._update_preview_status()
        self._refresh_comment_button()

    def _update_preview_status(self) -> None:
        if self._preview_image.isNull():
            text = "尚无本地预览。启用并保存后，点击“3 秒后本地观察”，切到目标应用等待采集。"
        elif self._preview_retained:
            text = "显示上次本地预览。当前 AURA 自身窗口已跳过，预览不是面板的实时画面。"
        else:
            text = "显示最近一次通过隐私过滤的本地预览；返回面板查看时不会观察 AURA 自身。"
        age = self._runtime_info.get("previewAgeSeconds")
        if not self._preview_image.isNull() and age is not None:
            text += f" 采集于 {self._runtime_info.get('previewObservedAt', '')}，距今 {age} 秒。"
            if age >= 60:
                text += " 这是旧预览；评论前可重新本地观察。"
        self.preview_status_label.setText(text)

    def _update_preview(self) -> None:
        if self._preview_image.isNull():
            self.preview_label.clear()
            self.preview_label.setText("尚无本地预览。敏感或排除窗口不会显示画面。")
            return
        self.preview_label.setPixmap(QPixmap.fromImage(self._preview_image).scaled(
            self.preview_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def eventFilter(self, watched, event) -> bool:
        if watched is self.preview_label and event.type() == QEvent.Type.Resize:
            self._update_preview()
        return super().eventFilter(watched, event)
