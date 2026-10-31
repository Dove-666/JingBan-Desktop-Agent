"""Explicitly opened, video-only camera observations held solely in memory.

Construction never enumerates or opens hardware.  Qt's video sink delivers
frames to the object's Qt thread; only a throttled, detached thumbnail is kept.
"""

from __future__ import annotations

import hashlib
import math
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoFrame, QVideoSink

from .capture_quality import is_near_black
from .screen_observer import ForegroundInfo, Observation


_OPEN_ERROR = "无法打开摄像头，请检查系统相机权限、设备连接，或是否被其他应用占用。"


class _CameraCallbacks(QObject):
    """Give queued Qt callbacks a receiver and the lifetime of one camera run."""

    def __init__(self, observer, generation):
        super().__init__(observer)
        self.observer = observer
        self.generation = generation

    @Slot(QVideoFrame)
    def frame(self, frame):
        self.observer._accept_frame(self.generation, frame)

    @Slot(QCamera.Error, str)
    def error(self, error, _description):
        if error != QCamera.Error.NoError:
            self.observer._camera_failed(self.generation, _OPEN_ERROR)

    @Slot(bool)
    def active(self, active):
        self.observer._active_changed(self.generation, active)

    @Slot()
    def devices(self):
        self.observer._devices_changed(self.generation)


class CameraObserver(QObject):
    """ScreenObserver-compatible observer with an independent explicit switch.

    ``settings.enabled`` is the camera controller's own consent, independent of
    screen settings.  ``allow_remote`` is deliberately not needed to preview.
    Factories permit synthetic tests without constructing a real camera.
    """

    status_changed = Signal(str)
    error_changed = Signal(str)
    active_changed = Signal(bool)
    frame_ready = Signal(QImage)

    def __init__(self, parent=None, *, device_provider=None, camera_factory=None,
                 session_factory=None, sink_factory=None, devices_factory=None,
                 clock=None, frame_interval_seconds=0.5, max_frame_age_seconds=5.0):
        super().__init__(parent)
        self._device_provider = device_provider or QMediaDevices.videoInputs
        self._camera_factory = camera_factory or QCamera
        self._session_factory = session_factory or QMediaCaptureSession
        self._sink_factory = sink_factory or QVideoSink
        # A synthetic provider never creates a real hardware monitor implicitly.
        self._devices_factory = (devices_factory if devices_factory is not None
                                 else QMediaDevices if device_provider is None else None)
        self._clock = clock or time.monotonic
        self._frame_interval = max(0.1, float(frame_interval_seconds))
        self._max_age = max(self._frame_interval, float(max_frame_age_seconds))
        self._camera = self._session = self._sink = self._monitor = self._callbacks = None
        self._connections = []
        self._running = self._closed = self._was_active = False
        self._generation = 0
        self._device_id = self._description = self._fingerprint = ""
        self._image = None
        self._frame_time = None
        self._last_conversion = -math.inf
        self._status = "摄像头识别已关闭。"
        self._error = ""
        self._failure_code = ""

    @property
    def active(self):
        return self._running

    @property
    def running(self):
        return self._running

    @property
    def device_id(self):
        return self._device_id

    @property
    def source_fingerprint(self):
        return self._fingerprint

    @property
    def status(self):
        return self._status

    @property
    def error(self):
        return self._error

    @staticmethod
    def _id(device):
        return bytes(device.id()).hex()

    def available_devices(self):
        """List device metadata; this method does not open any video input."""
        try:
            return [{"id": self._id(device), "description": str(device.description()),
                     "is_default": bool(device.isDefault())} for device in self._device_provider()]
        except Exception:
            return []

    def devices(self):
        return self.available_devices()

    def _set_status(self, text):
        if text != self._status:
            self._status = text
            self.status_changed.emit(text)

    def _set_error(self, text):
        if text != self._error:
            self._error = text
            if text:
                self.error_changed.emit(text)

    def _connect(self, signal, callback):
        signal.connect(callback)
        self._connections.append((signal, callback))

    def _configure_format(self, device):
        try:
            formats = [item for item in device.videoFormats()
                       if item.resolution().width() > 0 and item.resolution().height() > 0]
            small = [item for item in formats if max(item.resolution().width(), item.resolution().height()) <= 640]
            if small:
                chosen = min(small, key=lambda item: (
                    -item.resolution().width() * item.resolution().height(), item.maxFrameRate()))
            elif formats:
                chosen = min(formats, key=lambda item: (
                    item.resolution().width() * item.resolution().height(), item.maxFrameRate()))
            else:
                return
            self._camera.setCameraFormat(chosen)
        except Exception:
            # Camera defaults remain usable if a backend exposes no formats.
            pass

    def start(self, device_id=""):
        """Open video after explicit consent; never attach audio or a recorder."""
        if self._closed:
            return False
        requested = str(device_id or "")
        if self._running and (not requested or requested == self._device_id):
            return True
        self._release()
        self._failure_code = ""
        self._set_error("")
        try:
            devices = list(self._device_provider())
            if not devices:
                self._failure_code = "camera_unavailable"
                self._set_error("未发现可用摄像头，请连接设备并检查系统相机权限。")
                self._set_status(self._error)
                return False
            if requested:
                device = next((item for item in devices if self._id(item) == requested), None)
                if device is None:
                    self._failure_code = "camera_unavailable"
                    self._set_error("选定摄像头当前不可用，请重新选择设备。")
                    self._set_status(self._error)
                    return False
            else:
                device = next((item for item in devices if item.isDefault()), devices[0])
            self._device_id = self._id(device)
            self._description = " ".join(str(device.description()).split())[:160] or "摄像头"
            self._fingerprint = "camera:" + hashlib.sha256(bytes(device.id())).hexdigest()[:24]
            self._generation += 1
            generation = self._generation
            self._callbacks = _CameraCallbacks(self, generation)
            self._camera = self._camera_factory(device, self)
            self._session = self._session_factory(self)
            self._sink = self._sink_factory(self)
            self._connect(self._sink.videoFrameChanged, self._callbacks.frame)
            self._connect(self._camera.errorOccurred, self._callbacks.error)
            self._connect(self._camera.activeChanged, self._callbacks.active)
            if self._devices_factory is not None:
                self._monitor = self._devices_factory(self)
                self._connect(self._monitor.videoInputsChanged, self._callbacks.devices)
            self._session.setCamera(self._camera)
            self._session.setVideoSink(self._sink)
            self._configure_format(device)
            self._running = True
            self._set_status("正在等待摄像头画面…")
            self.active_changed.emit(True)
            self._camera.start()
            return self._running
        except Exception:
            self._camera_failed(self._generation, _OPEN_ERROR)
            return False

    def _release(self):
        """Invalidate first, then detach and release Qt multimedia resources."""
        was_running = self._running
        self._running = self._was_active = False
        self._generation += 1
        self._image = None
        self._frame_time = None
        self._last_conversion = -math.inf
        camera, session, sink = self._camera, self._session, self._sink
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()
        if camera is not None:
            try:
                camera.stop()
            except Exception:
                pass
        if session is not None:
            for method in ("setVideoSink", "setCamera"):
                try:
                    getattr(session, method)(None)
                except Exception:
                    pass
        if sink is not None:
            try:
                sink.setVideoFrame(QVideoFrame())
            except Exception:
                pass
        for item in (camera, session, sink, self._monitor, self._callbacks):
            if item is not None:
                try:
                    item.deleteLater()
                except RuntimeError:
                    pass
        self._camera = self._session = self._sink = self._monitor = self._callbacks = None
        if was_running:
            self.active_changed.emit(False)

    def stop(self):
        self._release()
        self._failure_code = ""
        self._set_error("")
        self._set_status("摄像头识别已关闭。")

    def shutdown(self):
        self._closed = True
        self.stop()

    def _camera_failed(self, generation, message, code="camera_unavailable"):
        if generation != self._generation or self._closed:
            return
        self._release()
        self._failure_code = code
        self._set_error(message)
        self._set_status(message)

    def _active_changed(self, generation, active):
        if generation != self._generation or not self._running:
            return
        if active:
            self._was_active = True
        elif self._was_active:
            self._camera_failed(generation, "摄像头已停止输出画面，请检查设备连接后重新开启。")

    def _devices_changed(self, generation):
        if generation != self._generation or not self._running:
            return
        try:
            present = any(self._id(device) == self._device_id for device in self._device_provider())
        except Exception:
            present = False
        if not present:
            self._camera_failed(generation, "摄像头已断开，已停止观察并清除预览。", "camera_disconnected")

    def _accept_frame(self, generation, frame):
        if generation != self._generation or not self._running or self._closed:
            return
        now = self._clock()
        if not math.isfinite(now) or now - self._last_conversion < self._frame_interval:
            return
        self._last_conversion = now
        try:
            if not frame.isValid():
                return
            image = frame.toImage()
            if not isinstance(image, QImage) or image.isNull():
                return
            if max(image.width(), image.height()) > 640:
                image = image.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            self._image = image.copy()
            self._frame_time = now
            self._was_active = True
            self._set_status("摄像头本地预览已就绪；画面仅保留在内存。")
            self.frame_ready.emit(self._image.copy())
        except Exception:
            # Conversion errors never expose backend text, pixels, or paths.
            self._image = None
            self._frame_time = None

    def _blocked(self, settings):
        if not bool(getattr(settings, "enabled", False)) or self._closed:
            return "disabled", "摄像头识别已关闭。"
        if not self._running:
            return self._failure_code or "disabled", self._error or "摄像头尚未开启，请先启用摄像头识别。"
        now = self._clock()
        if (self._frame_time is None or self._image is None
                or not math.isfinite(now) or not 0 <= now - self._frame_time <= self._max_age):
            self._image = None
            self._frame_time = None
            return "camera_no_frame", "尚未收到可用的摄像头画面，或画面已过期；本次未调用模型。"
        return "", ""

    def current_safety(self, settings):
        return self._blocked(settings)[1]

    def current_blocked_code(self, settings):
        return self._blocked(settings)[0]

    def _info(self):
        if not self._fingerprint:
            return ForegroundInfo()
        identifier = int(self._fingerprint.split(":", 1)[1][:15], 16) or 1
        return ForegroundInfo(hwnd=identifier, pid=self._generation,
                              process_name=self._fingerprint, title="摄像头 · " + self._description,
                              idle_seconds=0.0, available=self._running)

    @staticmethod
    def _observation(info, **kwargs):
        # Source is an additive field in the shared observation protocol.
        if "source" in Observation.__dataclass_fields__:
            kwargs["source"] = "camera"
        return Observation(info, **kwargs)

    def observe(self, settings):
        if not bool(getattr(settings, "enabled", False)) and self._running:
            self.stop()
        code, reason = self._blocked(settings)
        info = self._info()
        if code:
            return self._observation(info, blocked_reason=reason, blocked_code=code)
        image = self._image
        if image is None or image.isNull():
            return self._observation(info, blocked_reason="摄像头画面不可用，本次未调用模型。", blocked_code="camera_no_frame")
        if is_near_black(image):
            return self._observation(info, blocked_reason="摄像头画面近乎全黑，请检查遮挡或光线；本次未调用模型。",
                                     blocked_code="capture_unavailable")
        try:
            requested_width = int(getattr(settings, "max_capture_width", 640))
        except (ValueError, TypeError):
            requested_width = 640
        limit = min(640, max(1, requested_width))
        if max(image.width(), image.height()) > limit:
            image = image.scaled(limit, limit, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        return self._observation(info, image=image.copy(), capture_method="摄像头本地低分辨率画面")

    def is_current(self, observation, settings):
        if not isinstance(observation, Observation) or self.current_blocked_code(settings):
            return False
        current = self._info()
        return ((current.hwnd, current.pid, current.process_name) ==
                (observation.info.hwnd, observation.info.pid, observation.info.process_name))
