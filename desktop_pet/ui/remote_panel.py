"""Desktop pairing controls; transport and authorization live in the controller."""

import json
import math
import time
from datetime import datetime

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .styles import PANEL_STYLE


INSTALL_COMMAND = r".\.venv\Scripts\python.exe -m pip install -r requirements-remote.txt"


def _label(text="", name="hint"):
    label = QLabel(str(text))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _expiry_timestamp(value):
    """Accept wire timestamps without depending on the machine's locale."""
    try:
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # A timestamp without a timezone is ambiguous; refuse it.
            return parsed.timestamp() if parsed.tzinfo is not None else 0.0
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


class RemotePanel(QWidget):
    """Small controller-backed settings page, with no network work on the UI thread."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._status = {}
        self._pairing = {}
        self._expires_at = 0.0
        self._copied_pairing_text = ""
        self.setObjectName("remotePanel")
        self.setStyleSheet(PANEL_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(self.scroll)
        contents = QWidget()
        self.scroll.setWidget(contents)
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        layout.addWidget(_label("手机远程连接", "sectionTitle"))
        layout.addWidget(_label("连接已配对设备，用于传递图片、桌宠指令和同步对话记录。手机端接入后即可使用。"))

        self.enabled_checkbox = QCheckBox("允许手机连接这台电脑")
        self.enabled_checkbox.setObjectName("remoteEnabled")
        self.enabled_checkbox.setToolTip("开启后随桌宠启动自动恢复连接；网络未就绪时会自动重试。")
        self.enabled_checkbox.toggled.connect(self._enabled_toggled)
        layout.addWidget(self.enabled_checkbox)
        self.status_label = _label("远程连接已关闭", "status")
        self.status_label.setAccessibleName("远程连接状态")
        layout.addWidget(self.status_label)
        self.error_label = _label("", "error")
        self.error_label.setAccessibleName("远程连接错误")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.feedback_label = _label()
        self.feedback_label.setAccessibleName("远程操作提示")
        self.feedback_label.hide()
        layout.addWidget(self.feedback_label)

        actions = QHBoxLayout()
        self.retry_button = QPushButton("开启连接")
        self.retry_button.setObjectName("primaryButton")
        self.retry_button.clicked.connect(self._retry)
        actions.addWidget(self.retry_button)
        self.copy_connection_button = QPushButton("复制连接信息")
        self.copy_connection_button.clicked.connect(self._copy_connection)
        actions.addWidget(self.copy_connection_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.dependency_group = QGroupBox("可选通信组件未安装")
        dependency_layout = QVBoxLayout(self.dependency_group)
        dependency_layout.addWidget(_label("在项目目录运行下面的命令，安装后重启 AURA。"))
        self.install_command = QLineEdit(INSTALL_COMMAND)
        self.install_command.setReadOnly(True)
        self.install_command.setAccessibleName("安装可选通信组件命令")
        dependency_layout.addWidget(self.install_command)
        self.copy_install_button = QPushButton("复制安装命令")
        self.copy_install_button.clicked.connect(self._copy_install)
        dependency_layout.addWidget(self.copy_install_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.dependency_group)

        pairing_group = QGroupBox("配对新设备")
        pairing_layout = QVBoxLayout(pairing_group)
        pairing_layout.addWidget(_label("先开启连接，再生成配对链接。点击“复制配对信息”，将完整链接粘贴到手机端；无需手输短码。链接仅交给你自己的设备，5 分钟后失效。"))
        self.pair_code = QLineEdit()
        self.pair_code.setObjectName("remotePairCode")
        self.pair_code.setReadOnly(True)
        self.pair_code.setPlaceholderText("尚未生成完整配对链接")
        self.pair_code.setAccessibleName("完整一次性配对链接")
        self.pair_code.setToolTip("此处包含完整配对链接。复制后直接粘贴到手机端，不要只输入短码。")
        pairing_layout.addWidget(self.pair_code)
        self.countdown_label = _label("未开放配对")
        self.countdown_label.setAccessibleName("配对链接有效时间")
        pairing_layout.addWidget(self.countdown_label)
        pair_actions = QHBoxLayout()
        self.pair_button = QPushButton("生成配对链接")
        self.pair_button.clicked.connect(self._begin_pairing)
        pair_actions.addWidget(self.pair_button)
        self.copy_pair_button = QPushButton("复制配对信息")
        self.copy_pair_button.setToolTip("复制包含电脑地址和一次性配对码的完整链接，然后粘贴到手机端。")
        self.copy_pair_button.clicked.connect(self._copy_pairing)
        pair_actions.addWidget(self.copy_pair_button)
        self.cancel_pair_button = QPushButton("取消配对")
        self.cancel_pair_button.clicked.connect(self._cancel_pairing)
        pair_actions.addWidget(self.cancel_pair_button)
        pair_actions.addStretch()
        pairing_layout.addLayout(pair_actions)
        layout.addWidget(pairing_group)

        devices_group = QGroupBox("已配对设备")
        devices_layout = QVBoxLayout(devices_group)
        self.devices_list = QListWidget()
        self.devices_list.setObjectName("remoteDevices")
        self.devices_list.setAccessibleName("已配对设备")
        self.devices_list.setMinimumHeight(110)
        self.devices_list.setMaximumHeight(180)
        self.devices_list.currentItemChanged.connect(self._selection_changed)
        devices_layout.addWidget(self.devices_list)
        self.empty_devices_label = _label("还没有已配对的设备。")
        devices_layout.addWidget(self.empty_devices_label)
        self.revoke_button = QPushButton("撤销选中设备")
        self.revoke_button.setToolTip("撤销后该设备需重新配对，才能继续连接。")
        self.revoke_button.clicked.connect(self._revoke_device)
        devices_layout.addWidget(self.revoke_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(devices_group)
        layout.addWidget(_label("开启后随桌宠启动自动恢复；同一 Wi-Fi 可局域网直连，网络未就绪时会自动重试。"))
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_countdown)
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setInterval(1000)
        self._recovery_timer.timeout.connect(lambda: self._status_changed(self.controller.status))
        controller.status_changed.connect(self._status_changed)
        controller.pairing_changed.connect(self._pairing_changed)
        controller.devices_changed.connect(self._devices_changed)
        self._status_changed(controller.status)
        self._pairing_changed(controller.pairing)
        self._devices_changed(controller.devices)

    def _feedback(self, text):
        self.feedback_label.setText(str(text))
        self.feedback_label.setVisible(bool(text))

    def _enabled_toggled(self, enabled):
        self._feedback("")
        if not enabled:
            self._clear_pairing()
        try:
            self.controller.set_enabled(bool(enabled))
        except Exception:
            self._feedback("无法更改连接状态，请重试。")
        self._status_changed(self.controller.status)

    def _status_changed(self, status):
        self._status = dict(status or {})
        state = self._status.get("state", "disabled")
        enabled = bool(self.controller.enabled)
        self.enabled_checkbox.blockSignals(True)
        self.enabled_checkbox.setChecked(enabled)
        self.enabled_checkbox.blockSignals(False)
        summaries = {
            "disabled": "远程连接已关闭",
            "starting": "正在建立远程连接…",
            "listening": "远程连接已开启 · 等待已配对设备连接",
            "error": "远程连接暂不可用",
        }
        summary = summaries.get(state, "正在更新连接状态…")
        if state == "listening":
            if self._status.get("local_available") is True and self._status.get("relay_available") is False:
                summary = "局域网已就绪 · 公网中继未连接"
            elif self._status.get("local_available") is False and self._status.get("relay_available") is True:
                summary = "公网中继已就绪 · 等待已配对设备连接"
        retry_seconds = self._status.get("retry_in_seconds")
        recovering = state == "error" and self._status.get("auto_retry") is True
        if recovering and isinstance(retry_seconds, (int, float)) and math.isfinite(retry_seconds):
            summary = f"正在恢复远程连接 · {max(0, math.ceil(retry_seconds))} 秒后自动重试"
        if recovering:
            if not self._recovery_timer.isActive():
                self._recovery_timer.start()
        else:
            self._recovery_timer.stop()
        self.status_label.setText(summary)
        message = str(self._status.get("message") or "").strip()
        # A failed preference write can leave a healthy connection running.
        # Preserve that operation error even while the transport is listening.
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        dependency_available = self._status.get("dependency_available", True) is not False
        self.dependency_group.setVisible(not dependency_available)
        self.retry_button.setText("立即重试" if recovering else "开启连接" if not enabled else "重试连接")
        self.retry_button.setEnabled(state != "starting" and dependency_available)
        self.retry_button.setVisible(state != "listening")
        ready = enabled and state == "listening"
        self.copy_connection_button.setEnabled(ready and bool(self._status.get("ticket") or self._status.get("endpoint_id")))
        self.pair_button.setEnabled(ready and not bool(self._pairing))
        if not ready:
            self._clear_pairing()

    def _retry(self):
        self._feedback("")
        try:
            if not self.controller.enabled:
                self.controller.set_enabled(True)
            else:
                self.controller.retry()
        except Exception:
            self._feedback("连接未能启动，请重试。")
        self._status_changed(self.controller.status)

    def _begin_pairing(self):
        if not self.pair_button.isEnabled():
            return
        self._feedback("")
        try:
            pairing = self.controller.begin_pairing()
            if isinstance(pairing, dict):
                self._pairing_changed(pairing)
        except Exception:
            self._clear_pairing()
            self._feedback("未能生成完整配对链接，请重试。")

    def _pairing_changed(self, pairing):
        self._clear_pairing()
        if not pairing or self._status.get("state") != "listening" or not self.controller.enabled:
            return
        expires_at = _expiry_timestamp(pairing.get("expires_at"))
        if not pairing.get("code") or expires_at <= time.time():
            self.countdown_label.setText("配对链接已失效，请重新生成。")
            return
        uri = pairing.get("uri")
        if not isinstance(uri, str) or not uri.startswith("aura://pair/"):
            self.countdown_label.setText("完整配对链接不可用，请重新生成。")
            return
        self._pairing = dict(pairing)
        self._expires_at = expires_at
        self.pair_code.setText(uri)
        self.pair_code.setCursorPosition(0)
        self.pair_button.setEnabled(False)
        self.copy_pair_button.setEnabled(True)
        self.cancel_pair_button.setEnabled(True)
        self._timer.start()
        self._update_countdown()

    def _clear_pairing(self):
        self._timer.stop()
        if self._copied_pairing_text and QApplication.clipboard().text() == self._copied_pairing_text:
            QApplication.clipboard().clear()
        self._copied_pairing_text = ""
        self._pairing = {}
        self._expires_at = 0.0
        self.pair_code.clear()
        self.countdown_label.setText("未开放配对")
        self.copy_pair_button.setEnabled(False)
        self.cancel_pair_button.setEnabled(False)
        self.pair_button.setEnabled(bool(self.controller.enabled) and self._status.get("state") == "listening")

    def _update_countdown(self):
        if not self._pairing:
            return
        seconds = max(0, math.ceil(self._expires_at - time.time()))
        if not seconds:
            self._clear_pairing()
            try:
                self.controller.cancel_pairing()
            except Exception:
                pass  # Transport must enforce its own expiry even if unavailable.
            self.countdown_label.setText("配对链接已失效，请重新生成。")
            return
        minutes, seconds = divmod(seconds, 60)
        self.countdown_label.setText(f"有效时间 {minutes:02d}:{seconds:02d} · 仅可配对一次")

    def _cancel_pairing(self):
        self._clear_pairing()
        try:
            self.controller.cancel_pairing()
        except Exception:
            self._feedback("连接暂不可用，配对信息已从本页清除。")

    def _copy_connection(self):
        if not self.copy_connection_button.isEnabled():
            return
        payload = {key: self._status[key] for key in ("endpoint_id", "ticket") if self._status.get(key)}
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False))
        self._feedback("连接信息已复制。配对新设备请使用临时配对信息。")

    def _copy_pairing(self):
        self._update_countdown()
        if not self._pairing:
            return
        self._copied_pairing_text = self._pairing["uri"]
        QApplication.clipboard().setText(self._copied_pairing_text)
        self._feedback("完整配对链接已复制，请粘贴到手机端；无需手输短码。")

    def _copy_install(self):
        QApplication.clipboard().setText(INSTALL_COMMAND)
        self._feedback("安装命令已复制。")

    def _devices_changed(self, devices):
        selected = self.devices_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.devices_list.clear()
        for device in devices or []:
            peer_id = str(device.get("id") or "")
            if not peer_id:
                continue
            name = str(device.get("name") or "未命名设备")
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, peer_id)
            item.setToolTip(f"配对时间：{device.get('paired_at') or '未知'}")
            self.devices_list.addItem(item)
            if peer_id == selected_id:
                self.devices_list.setCurrentItem(item)
        self.empty_devices_label.setVisible(self.devices_list.count() == 0)
        self._selection_changed()

    def _selection_changed(self, *args):
        self.revoke_button.setEnabled(self.devices_list.currentItem() is not None)

    def _revoke_device(self):
        item = self.devices_list.currentItem()
        if item is None:
            return
        try:
            result = self.controller.revoke_device(item.data(Qt.ItemDataRole.UserRole))
            self._feedback("未能撤销设备，请重试。" if result is False else "设备配对已撤销。")
        except Exception:
            self._feedback("未能撤销设备，请重试。")
