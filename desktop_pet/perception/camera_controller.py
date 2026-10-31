"""Independent camera consent and device lifecycle over shared visual analysis."""

from dataclasses import replace
from datetime import datetime
import time

from .camera_observer import CameraObserver
from .controller import PerceptionController


class CameraController(PerceptionController):
    def __init__(self, settings_manager, pet_controller, chat_controller, observer=None,
                 analyzer_factory=None, clock=time.monotonic, parent=None):
        self._camera_error = ""
        self._handling_device_error = False
        self._camera_start_timer = None
        selected_observer = observer if observer is not None else CameraObserver()
        super().__init__(
            settings_manager, pet_controller, chat_controller, observer=selected_observer,
            analyzer_factory=analyzer_factory, clock=clock, parent=parent,
            source_kind="camera", settings_loader=settings_manager.load_camera_settings,
            settings_saver=settings_manager.save_camera_settings,
        )
        if isinstance(selected_observer, CameraObserver):
            selected_observer.setParent(self)
        for signal_name, callback in (("frame_ready", self._camera_frame),
                                      ("error_changed", self._device_error),
                                      ("active_changed", self._device_activity)):
            signal = getattr(self.observer, signal_name, None)
            if signal is not None:
                signal.connect(callback)
        # Defer a saved opt-in until all error/frame receivers are connected.
        from PySide6.QtCore import QTimer
        self._camera_start_timer = QTimer(self)
        self._camera_start_timer.setSingleShot(True)
        self._camera_start_timer.timeout.connect(self._sync_camera)
        self._camera_start_timer.start(0)

    def _apply_timer(self):
        super()._apply_timer()
        if self._camera_start_timer is not None:
            self._sync_camera()

    def _sync_camera(self):
        if self._closed or self.paused or not self.settings.enabled:
            self.observer.stop()
            return False
        try:
            started = self.observer.start(self.settings.device_id)
        except Exception:
            started = False
        if not started and self.settings.enabled:
            self._device_error(getattr(self.observer, "error", "") or "无法打开选定摄像头。")
        elif started:
            self._camera_error = ""
            self._publish_runtime()
        return started

    def _device_activity(self, _active):
        self._publish_runtime()

    def _device_error(self, message):
        if self._closed or not message or self._handling_device_error:
            return
        self._handling_device_error = True
        try:
            self._camera_error = str(message)
            saved = self.revoke_permissions(local=True)
            status = self._camera_error + " 当前摄像头已关闭，预览已清除。"
            if not saved:
                status += " 关闭状态保存失败；旧的开启许可可能仍在配置中，重启可能重新开启摄像头。请修复保存权限后再次保存。"
                self._record_persistence_warning(status, permission_failed=True)
            else:
                status += " 关闭状态已保存；可检查设备后重新开启。"
            self._append_runtime_event("device_error", status)
            self._set_status(status)
            self._publish_runtime()
        finally:
            self._handling_device_error = False

    def _camera_frame(self, _image):
        if self._closed or self.paused or not self.settings.enabled:
            return
        observation = self.observer.observe(self.settings)
        if observation.blocked_reason or observation.image is None:
            self._clear_observation(observation.blocked_reason)
            self._set_status(observation.blocked_reason or "摄像头画面不可用。")
            return
        # The manual comment always uses the exact locally displayed preview.
        # This does not commit the change-detector baseline used by timed analysis.
        self._last_observation = observation
        self._preview_at = self._clock()
        self._preview_wall_time = datetime.now().astimezone().isoformat(timespec="seconds")
        self.details = {"source_kind": "camera", "title": observation.info.title,
                        "image_size": f"{observation.image.width()} × {observation.image.height()}",
                        "observed_at": self._preview_wall_time}
        self._last_valid_details = dict(self.details)
        self.details_changed.emit(dict(self.details))
        self._set_preview(observation.image)
        if not self.busy and not self._analysis_error and not self._persistence_warning:
            self._set_status("摄像头预览已开启；" + (
                "远程识别已允许，按画面变化与请求间隔分析。" if self.settings.allow_remote
                else "仅本地预览，没有发送给模型。"))

    def runtime_snapshot(self):
        result = super().runtime_snapshot()
        result.update({"sourceKind": "camera", "cameraActive": bool(getattr(self.observer, "active", False)),
                       "deviceId": str(getattr(self.observer, "device_id", "")),
                       "deviceName": self.details.get("title", ""), "cameraError": self._camera_error})
        return result

    def available_devices(self):
        return self.observer.available_devices()

    def set_enabled(self, enabled, allow_remote=None):
        if not isinstance(enabled, bool) or (allow_remote is not None and not isinstance(allow_remote, bool)):
            return False
        values = {"enabled": enabled}
        if allow_remote is not None:
            values["allow_remote"] = allow_remote
        result = self.update_settings(replace(self.settings, **values))
        return bool(result and self.settings.enabled == enabled)

    def set_remote_enabled(self, enabled):
        if not isinstance(enabled, bool):
            return False
        return self.update_settings(replace(self.settings, allow_remote=enabled))

    def select_device(self, device_id):
        if not isinstance(device_id, str):
            return False
        if device_id and not any(row["id"] == device_id for row in self.available_devices()):
            self._set_status("选定摄像头不可用，请刷新设备列表。")
            return False
        return self.update_settings(replace(self.settings, device_id=device_id))

    def observe_local_now(self):
        return self.observe_now()

    def shutdown(self):
        if self._camera_start_timer is not None:
            self._camera_start_timer.stop()
        super().shutdown()
        self.observer.shutdown()
