"""Camera controls; capture, consent persistence and API work belong to the controller."""

from dataclasses import replace

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from .theme import style_widget
from .perception_panel import _event_value
from .settings_widgets import CollapsibleSection, SettingsToggle


def _label(text="", name="hint"):
    label = QLabel(str(text))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _plain(value, limit=1200):
    return " ".join(str(value).split())[:limit]


class CameraPanel(QWidget):
    """Independent local preview and remote permission, both initially off."""

    def __init__(self, controller, parent=None, *, embedded=False):
        super().__init__(parent)
        self.controller = controller
        self._preview_image = QImage()
        self._runtime_info = {}
        self._permission_changing = False
        self.setObjectName("cameraPanel")
        style_widget(self,
            "QPushButton#cameraEnabled { color: white; background: #253449; "
            "border: 1px solid #253449; padding: 9px 18px; border-radius: 7px; }"
            "QPushButton#cameraEnabled:hover { background: #172334; }"
            "QLabel#cameraPreview { background: #f7f8fa; border: 1px solid #dce3ec; "
            "border-radius: 9px; padding: 6px; color: #596575; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(self.scroll)
        contents = QWidget()
        self.scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(*((0, 2, 6, 12) if embedded else (18, 18, 18, 18)))
        layout.setSpacing(14)

        heading = QHBoxLayout()
        heading.addWidget(_label("本地预览", "sectionTitle"), 1)
        self.enabled_button = QPushButton("开启摄像头")
        self.enabled_button.setObjectName("cameraEnabled")
        self.enabled_button.setCheckable(True)
        self.enabled_button.setAccessibleName("摄像头开关")
        self.enabled_button.toggled.connect(self._enabled_toggled)
        heading.addWidget(self.enabled_button)
        layout.addLayout(heading)
        layout.addWidget(_label("先在本地预览，允许发送后才由模型识别画面。"))

        self.runtime_label = _label("摄像头已关闭 · 不采集画面", "status")
        self.runtime_label.setObjectName("cameraRuntimeSummary")
        self.runtime_label.setAccessibleName("摄像头运行状态")
        layout.addWidget(self.runtime_label)
        self.runtime_error_label = _label("", "error")
        self.runtime_error_label.setObjectName("cameraRuntimeError")
        self.runtime_error_label.hide()
        layout.addWidget(self.runtime_error_label)
        self.retry_button = QPushButton("重试识别")
        self.retry_button.setObjectName("retryCameraAnalysis")
        self.retry_button.clicked.connect(self._comment_current_preview)
        self.retry_button.hide()
        layout.addWidget(self.retry_button)
        self.feedback_label = _label("", "error")
        self.feedback_label.hide()
        layout.addWidget(self.feedback_label)
        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setObjectName("cameraDevice")
        self.device_combo.setAccessibleName("选择摄像头")
        self.device_combo.setMinimumWidth(1)
        self.device_combo.currentIndexChanged.connect(self._device_selected)
        device_row.addWidget(self.device_combo, 1)
        self.refresh_devices_button = QPushButton("刷新设备")
        self.refresh_devices_button.setObjectName("refreshCameraDevices")
        self.refresh_devices_button.clicked.connect(self._refresh_devices)
        device_row.addWidget(self.refresh_devices_button)
        layout.addLayout(device_row)

        self.preview_label = _label("摄像头已关闭，开启后显示本地预览。", "cameraPreview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumWidth(1)
        self.preview_label.setMinimumHeight(170)
        self.preview_label.setMaximumHeight(310)
        self.preview_label.installEventFilter(self)
        layout.addWidget(self.preview_label)
        self.preview_status_label = _label("画面仅保留在内存中；关闭摄像头会停止采集并清除预览。")
        self.preview_status_label.setObjectName("cameraPreviewStatus")
        layout.addWidget(self.preview_status_label)

        self.remote_checkbox = SettingsToggle("允许发送摄像头画面给模型")
        self.remote_checkbox.setObjectName("cameraAllowRemote")
        self.remote_checkbox.setToolTip("只适用于摄像头，独立于屏幕感知。关闭后立即停止远程分析。")
        self.remote_checkbox.toggled.connect(self._remote_toggled)
        layout.addWidget(self.remote_checkbox)
        layout.addWidget(_label("此许可与屏幕感知独立。关闭后立即停止发送。"))

        interaction = QFormLayout()
        self.commentary_mode_combo = QComboBox()
        self.commentary_mode_combo.setObjectName("cameraCommentaryMode")
        self.commentary_mode_combo.setAccessibleName("摄像头互动风格")
        self.commentary_mode_combo.addItem("陪伴评论", "companion")
        self.commentary_mode_combo.addItem("只在重要时说话", "quiet")
        interaction.addRow("互动风格", self.commentary_mode_combo)
        layout.addLayout(interaction)

        actions = QHBoxLayout()
        self.observe_button = QPushButton("刷新本地画面")
        self.observe_button.setObjectName("observeCameraLocally")
        self.observe_button.clicked.connect(self._observe_now)
        actions.addWidget(self.observe_button)
        self.comment_button = QPushButton("评论当前画面")
        self.comment_button.setObjectName("commentCameraPreview")
        self.comment_button.clicked.connect(self._comment_current_preview)
        actions.addWidget(self.comment_button)
        self.pause_button = QPushButton("暂停识别")
        self.pause_button.setObjectName("toggleCameraPause")
        self.pause_button.clicked.connect(self._toggle_pause)
        actions.addWidget(self.pause_button)
        actions.addStretch()
        layout.addLayout(actions)

        parameters = QWidget()
        form = QFormLayout(parameters)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.analysis_interval_input = self._spin("cameraAnalysisInterval", 30, 600)
        self.speech_cooldown_input = self._spin("cameraSpeechCooldown", 30, 1800)
        self.max_speeches_input = self._spin("cameraMaxSpeechesPerHour", 1, 30)
        form.addRow("远程分析最短间隔（秒）", self.analysis_interval_input)
        form.addRow("主动发言冷却（秒）", self.speech_cooldown_input)
        form.addRow("每小时主动发言上限", self.max_speeches_input)
        form.addRow(_label("与屏幕感知共享主动发言次数；手动评论不消耗次数。"))
        self.frequency_section = CollapsibleSection("更多选项", content=parameters)
        self.frequency_section.setObjectName("cameraFrequencyOptions")
        layout.addWidget(self.frequency_section)
        self.save_button = QPushButton("保存参数")
        self.save_button.setObjectName("saveCameraSettings")
        self.save_button.clicked.connect(self._save)
        layout.addWidget(self.save_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.status_label = _label(getattr(controller, "status", "摄像头已关闭。"), "status")
        self.status_label.setAccessibleName("摄像头识别状态")
        layout.addWidget(self.status_label)
        self.diagnostics_section = CollapsibleSection("运行详情")
        self.diagnostics_section.setObjectName("cameraRuntimeDetails")
        diagnostics_layout = QVBoxLayout(self.diagnostics_section.content)
        diagnostics_layout.setContentsMargins(0, 4, 0, 4)
        diagnostics_layout.addWidget(_label("当前画面详情"))
        self.details_view = QPlainTextEdit()
        self.details_view.setObjectName("cameraDetails")
        self.details_view.setReadOnly(True)
        self.details_view.setFixedHeight(95)
        diagnostics_layout.addWidget(self.details_view)
        history_row = QHBoxLayout()
        history_row.addWidget(_label("本次识别事件（最近 30 条）"), 1)
        self.reset_button = QPushButton("清空本次事件")
        self.reset_button.setObjectName("resetCameraSession")
        self.reset_button.clicked.connect(self._reset_session)
        history_row.addWidget(self.reset_button)
        diagnostics_layout.addLayout(history_row)
        self.history_view = QPlainTextEdit()
        self.history_view.setObjectName("cameraHistory")
        self.history_view.setReadOnly(True)
        self.history_view.setFixedHeight(130)
        diagnostics_layout.addWidget(self.history_view)
        layout.addWidget(self.diagnostics_section)
        layout.addStretch()

        for signal, callback in (
            ("settings_changed", self._load_settings), ("status_changed", self._status_changed),
            ("details_changed", self._details_changed), ("preview_changed", self._preview_changed),
            ("history_changed", self._history_changed), ("runtime_changed", self._runtime_changed),
            ("paused_changed", self._paused_changed),
        ):
            getattr(controller, signal).connect(callback)
        self._refresh_devices()
        self._load_settings(controller.settings)
        self._preview_changed(getattr(controller, "preview", QImage()))
        self._details_changed(getattr(controller, "details", {}))
        self._history_changed(getattr(controller, "history", []))
        snapshot = getattr(controller, "runtime_snapshot", None)
        self._runtime_changed(snapshot() if callable(snapshot) else {})

    @staticmethod
    def _spin(name, minimum, maximum):
        widget = QSpinBox()
        widget.setObjectName(name)
        widget.setRange(minimum, maximum)
        return widget

    def _feedback(self, text):
        self.feedback_label.setText(str(text))
        self.feedback_label.setVisible(bool(text))

    def _load_settings(self, settings):
        for widget, checked in ((self.enabled_button, settings.enabled), (self.remote_checkbox, settings.allow_remote)):
            widget.blockSignals(True)
            widget.setChecked(bool(checked))
            widget.blockSignals(False)
        self.enabled_button.setText("关闭摄像头" if settings.enabled else "开启摄像头")
        if not self._permission_changing:
            self.analysis_interval_input.setValue(settings.analysis_interval_seconds)
            self.speech_cooldown_input.setValue(settings.speech_cooldown_seconds)
            self.max_speeches_input.setValue(settings.max_speeches_per_hour)
            self.commentary_mode_combo.setCurrentIndex(max(0, self.commentary_mode_combo.findData(settings.commentary_mode)))
        device_id = str(getattr(settings, "device_id", ""))
        index = self.device_combo.findData(device_id)
        if index >= 0:
            self.device_combo.blockSignals(True)
            self.device_combo.setCurrentIndex(index)
            self.device_combo.blockSignals(False)
        if not settings.enabled:
            self._preview_image = QImage()
        self._update_preview()
        self._refresh_controls()

    def _enabled_toggled(self, enabled):
        self._feedback("")
        self._permission_changing = True
        try:
            accepted = self.controller.set_enabled(bool(enabled))
        except Exception:
            accepted = False
        finally:
            self._permission_changing = False
        self.enabled_button.blockSignals(True)
        self.enabled_button.setChecked(bool(self.controller.settings.enabled) if enabled else False)
        self.enabled_button.blockSignals(False)
        self.enabled_button.setText("关闭摄像头" if self.enabled_button.isChecked() else "开启摄像头")
        if not enabled:
            self._preview_changed(QImage())
        if not accepted:
            self._feedback(getattr(self.controller, "status", "") or ("未能开启摄像头，请检查设备和系统权限。" if enabled else "摄像头已关闭，但设置未能保存。"))
        self._refresh_controls()

    def _remote_toggled(self, allowed):
        if allowed:
            answer = QMessageBox.warning(
                self, "允许发送摄像头画面？",
                "摄像头画面可能包含你本人、其他人及周围环境。开启后，画面会发送给已配置 API 进行识别与评论。\n\n"
                "这项许可仅适用于摄像头，不沿用屏幕感知的许可。是否允许？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.remote_checkbox.blockSignals(True)
                self.remote_checkbox.setChecked(False)
                self.remote_checkbox.blockSignals(False)
                return
        self._feedback("")
        self._permission_changing = True
        try:
            accepted = self.controller.set_remote_enabled(bool(allowed))
        except Exception:
            accepted = False
        finally:
            self._permission_changing = False
        self.remote_checkbox.blockSignals(True)
        self.remote_checkbox.setChecked(bool(self.controller.settings.allow_remote) if allowed else False)
        self.remote_checkbox.blockSignals(False)
        if not accepted:
            self._feedback(getattr(self.controller, "status", "") or "摄像头远程发送权限未能保存。")
        self._refresh_controls()

    def _refresh_devices(self):
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        try:
            devices = self.controller.available_devices()
        except Exception:
            devices = []
            self._feedback("未能读取摄像头列表，请检查系统设备后刷新。")
        current = str(getattr(self.controller.settings, "device_id", ""))
        self.device_combo.addItem("系统默认摄像头", "")
        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = str(device.get("id") or "")
            if not device_id:
                continue
            description = str(device.get("description") or "摄像头")
            self.device_combo.addItem(description + ("（默认）" if device.get("is_default") else ""), device_id)
        if self.device_combo.count() == 1:
            self.device_combo.setItemText(0, "未检测到摄像头 · 可刷新设备")
        index = self.device_combo.findData(current)
        if index < 0:
            self.device_combo.addItem("已选摄像头未连接", current)
            index = self.device_combo.count() - 1
        self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)

    def _device_selected(self, index):
        device_id = self.device_combo.itemData(index)
        if device_id is None:
            return
        self._feedback("")
        try:
            accepted = self.controller.select_device(str(device_id))
        except Exception:
            accepted = False
        if not accepted:
            self._feedback(getattr(self.controller, "status", "") or "未能切换摄像头。")
            self._load_settings(self.controller.settings)

    def _refresh_controls(self):
        enabled = self.enabled_button.isChecked()
        paused = bool(getattr(self.controller, "paused", False))
        busy = bool(getattr(self.controller, "busy", False))
        comment_ready = bool(getattr(self.controller, "comment_available", not self._preview_image.isNull()))
        self.comment_button.setEnabled(enabled and self.remote_checkbox.isChecked() and not paused and not busy and comment_ready)
        self.retry_button.setEnabled(self.comment_button.isEnabled())
        self.retry_button.setToolTip("重新识别最近一次本地画面，不改变任何授权。" if self.retry_button.isEnabled() else
                                     "请先开启摄像头、允许远程发送，并等待可用预览及当前请求结束。")
        self.observe_button.setEnabled(enabled and not paused and not busy)
        self.pause_button.setEnabled(enabled)
        self.pause_button.setText("恢复识别" if paused else "暂停识别")
        mode = ("摄像头已关闭 · 不采集画面" if not enabled else
                "摄像头已暂停 · 不采集画面" if paused else
                "摄像头已开启 · 正在采集" if self._runtime_info.get("cameraActive") else
                "摄像头正在准备")
        remote = "已允许远程识别" if self.remote_checkbox.isChecked() else "仅本地预览 · 不调用 API"
        name = self._runtime_info.get("deviceName")
        self.runtime_label.setText(mode + (f" · {remote}" if enabled else "") +
                                   (f"\n设备：{_plain(name)}" if enabled and name else ""))

    def _status_changed(self, text):
        self.status_label.setText(str(text))
        self._refresh_controls()

    def _runtime_changed(self, info):
        self._runtime_info = info if isinstance(info, dict) else {}
        messages = [str(self._runtime_info.get("persistenceWarning") or "")]
        if self._runtime_info.get("cameraError"):
            messages.append("设备状态：" + str(self._runtime_info["cameraError"]))
        error = str(self._runtime_info.get("analysisError") or "")
        if error:
            messages.append("上次识别失败：" + error + " 请检查 API 设置后重试识别；成功识别后会解除提示。")
        self.runtime_error_label.setText("\n".join(message for message in messages if message))
        self.runtime_error_label.setVisible(any(messages))
        self.retry_button.setVisible(bool(error))
        self._refresh_controls()

    def _paused_changed(self, _paused):
        if bool(getattr(self.controller, "paused", False)):
            self._preview_changed(QImage())
        self._refresh_controls()

    def _toggle_pause(self):
        self.controller.set_paused(not bool(getattr(self.controller, "paused", False)))

    def _observe_now(self):
        if not self.observe_button.isEnabled():
            return
        observer = getattr(self.controller, "observe_local_now", None) or getattr(self.controller, "observe_now", None)
        try:
            accepted = callable(observer) and observer()
        except Exception:
            accepted = False
        self._feedback("" if accepted else getattr(self.controller, "status", "未能刷新本地画面。"))

    def _comment_current_preview(self):
        self._refresh_controls()
        if not self.comment_button.isEnabled():
            self._feedback("暂不能评论：请开启摄像头、允许远程发送，并等待有效预览和当前请求结束。")
            return
        try:
            accepted = self.controller.comment_current_preview()
        except Exception:
            accepted = False
        self._feedback("" if accepted else getattr(self.controller, "status", "未能请求画面评论。"))
        self._refresh_controls()

    def _save(self):
        settings = replace(
            self.controller.settings,
            commentary_mode=self.commentary_mode_combo.currentData(),
            analysis_interval_seconds=self.analysis_interval_input.value(),
            speech_cooldown_seconds=self.speech_cooldown_input.value(),
            max_speeches_per_hour=self.max_speeches_input.value(),
        )
        try:
            accepted = self.controller.update_settings(settings)
        except Exception:
            accepted = False
        self._feedback("" if accepted else getattr(self.controller, "status", "摄像头参数未能保存。"))

    def _reset_session(self):
        answer = QMessageBox.question(
            self, "清空本次摄像头事件", "清空本次识别事件和内存预览？聊天记录会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.reset_session()

    def _preview_changed(self, image):
        allowed = self.enabled_button.isChecked() and not bool(getattr(self.controller, "paused", False))
        self._preview_image = QImage(image) if allowed and isinstance(image, QImage) else QImage()
        self._update_preview()
        self._refresh_controls()

    def _update_preview(self):
        if self._preview_image.isNull():
            self.preview_label.clear()
            self.preview_label.setText(
                "摄像头已关闭，开启后显示本地预览。" if not self.enabled_button.isChecked() else
                "摄像头已暂停，恢复后继续本地预览。" if bool(getattr(self.controller, "paused", False)) else
                "正在等待本地预览…"
            )
            return
        self.preview_label.setPixmap(QPixmap.fromImage(self._preview_image).scaled(
            self.preview_label.contentsRect().size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _details_changed(self, details):
        labels = {"device_name": "摄像头", "title": "摄像头", "source_kind": "来源", "observed_at": "采集时间", "image_size": "预览尺寸", "reason": "说明", "status": "状态", "change_score": "画面变化"}
        values = details.items() if isinstance(details, dict) else []
        self.details_view.setPlainText("\n".join(f"{labels.get(str(key), str(key))}：{_plain(value)}" for key, value in list(values)[:20]) or "暂无摄像头画面详情。")

    def _history_changed(self, history):
        lines = []
        for event in list(history or [])[-30:]:
            if isinstance(event, dict):
                lines.append(" | ".join(f"{_plain(key, 60)}：{_event_value(key, value)}" for key, value in event.items())[:1600])
            else:
                lines.append(_plain(event))
        self.history_view.setPlainText("\n".join(lines))
        scroll = self.history_view.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def eventFilter(self, watched, event):
        if watched is self.preview_label and event.type() == QEvent.Type.Resize:
            self._update_preview()
        return super().eventFilter(watched, event)
