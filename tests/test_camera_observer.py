"""Camera lifecycle and pixel tests use injected devices and synthetic frames."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QByteArray, QObject, QSize, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QCamera, QVideoFrame
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.perception.camera_observer import CameraObserver
from desktop_pet.perception.settings import PerceptionSettings


def image(width=1280, height=720, color="#536b83"):
    result = QImage(width, height, QImage.Format.Format_RGB32)
    result.fill(QColor(color))
    return result


class Device:
    def __init__(self, identifier=b"synthetic-camera-a", description="合成摄像头", default=True, formats=()):
        self.identifier, self.name, self.default, self.formats = identifier, description, default, formats

    def id(self):
        return QByteArray(self.identifier)

    def description(self):
        return self.name

    def isDefault(self):
        return self.default

    def videoFormats(self):
        return self.formats


class Format:
    def __init__(self, width, height, fps=30):
        self.size, self.fps = QSize(width, height), fps

    def resolution(self):
        return self.size

    def maxFrameRate(self):
        return self.fps


class Camera(QObject):
    errorOccurred = Signal(QCamera.Error, str)
    activeChanged = Signal(bool)

    def __init__(self, device, parent, *, fail=False):
        super().__init__(parent)
        self.device, self.fail = device, fail
        self.starts = self.stops = 0
        self.format = None

    def setCameraFormat(self, format):
        self.format = format

    def start(self):
        self.starts += 1
        if self.fail:
            self.errorOccurred.emit(QCamera.Error.CameraError, "SYNTHETIC_PRIVATE_DRIVER_ERROR")
        else:
            self.activeChanged.emit(True)

    def stop(self):
        self.stops += 1
        self.activeChanged.emit(False)


class Session(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.camera = self.sink = None
        self.attachments = []

    def setCamera(self, camera):
        self.camera = camera
        self.attachments.append(("camera", camera))

    def setVideoSink(self, sink):
        self.sink = sink
        self.attachments.append(("video", sink))


class Sink(QObject):
    videoFrameChanged = Signal(QVideoFrame)

    def __init__(self, parent):
        super().__init__(parent)
        self.cleared = False

    def setVideoFrame(self, frame):
        self.cleared = not frame.isValid()


class DevicesMonitor(QObject):
    videoInputsChanged = Signal()


class CountedFrame:
    def __init__(self, pixels=None, valid=True, error=False):
        self.pixels = image() if pixels is None else pixels
        self.valid, self.error, self.conversions = valid, error, 0

    def isValid(self):
        return self.valid

    def toImage(self):
        self.conversions += 1
        if self.error:
            raise RuntimeError("SYNTHETIC_PRIVATE_FRAME_ERROR")
        return self.pixels


class CameraObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = PerceptionSettings(enabled=True)
        self.time = 100.0
        self.device_list = [Device()]
        self.devices = Mock(side_effect=lambda: self.device_list)
        self.cameras, self.sessions, self.sinks, self.monitors = [], [], [], []
        self.fail_camera = False

        def camera_factory(device, parent):
            result = Camera(device, parent, fail=self.fail_camera)
            self.cameras.append(result)
            return result

        def remember(factory, values):
            def create(parent):
                result = factory(parent)
                values.append(result)
                return result
            return create

        self.observer = CameraObserver(
            device_provider=self.devices, camera_factory=camera_factory,
            session_factory=remember(Session, self.sessions), sink_factory=remember(Sink, self.sinks),
            devices_factory=remember(DevicesMonitor, self.monitors), clock=lambda: self.time,
        )

    def tearDown(self):
        self.observer.shutdown()
        self.observer.deleteLater()
        self.app.processEvents()

    def start(self):
        self.assertTrue(self.observer.start())
        return self.observer._generation

    def frame(self, pixels=None):
        frame = CountedFrame(pixels)
        self.observer._accept_frame(self.observer._generation, frame)
        return frame

    def test_construct_and_default_off_never_enumerate_or_open(self):
        self.assertFalse(self.observer.active)
        self.assertEqual(self.cameras, [])
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.sinks, [])
        result = self.observer.observe(PerceptionSettings())
        self.assertIsNone(result.image)
        self.assertEqual(result.blocked_code, "disabled")
        self.devices.assert_not_called()

    def test_device_enumeration_is_metadata_only_and_ids_round_trip(self):
        self.device_list.append(Device(b"synthetic-camera-b", "备用摄像头", False))
        choices = self.observer.available_devices()
        self.assertEqual(choices[1], {"id": b"synthetic-camera-b".hex(), "description": "备用摄像头", "is_default": False})
        self.assertEqual(self.cameras, [])
        self.assertTrue(self.observer.start(choices[1]["id"]))
        self.assertIs(self.cameras[0].device, self.device_list[1])

    def test_explicit_start_only_attaches_video_and_is_idempotent(self):
        self.start()
        self.assertTrue(self.observer.running)
        self.assertEqual(self.cameras[0].starts, 1)
        self.assertEqual([name for name, _ in self.sessions[0].attachments], ["camera", "video"])
        self.assertTrue(self.observer.start())
        self.assertEqual(len(self.cameras), 1)
        self.assertEqual(self.observer.observe(self.settings).blocked_code, "camera_no_frame")

    def test_no_device_or_missing_selection_never_opens_another_camera(self):
        self.device_list.clear()
        self.assertFalse(self.observer.start())
        self.assertEqual(self.cameras, [])
        self.assertEqual(self.observer.observe(self.settings).blocked_code, "camera_unavailable")
        self.device_list.append(Device())
        self.assertFalse(self.observer.start("missing-device-id"))
        self.assertEqual(self.cameras, [])
        self.assertIn("重新选择", self.observer.error)

    def test_chooses_bounded_hardware_format_when_available(self):
        formats = [Format(1920, 1080), Format(320, 240), Format(640, 480, 30), Format(640, 480, 15)]
        self.device_list[0].formats = formats
        self.start()
        self.assertIs(self.cameras[0].format, formats[3])

    def test_native_qvideo_frame_signal_produces_detached_bounded_preview(self):
        self.start()
        previews = QSignalSpy(self.observer.frame_ready)
        self.sinks[0].videoFrameChanged.emit(QVideoFrame(image(1600, 900)))
        self.assertEqual(previews.count(), 1)
        result = self.observer.observe(self.settings)
        self.assertEqual((result.image.width(), result.image.height()), (640, 360))
        self.assertEqual(result.blocked_code, "")
        self.assertTrue(result.info.available)
        self.assertTrue(result.info.process_name.startswith("camera:"))
        result.image.fill(QColor("red"))
        self.assertNotEqual(self.observer.observe(self.settings).image.pixelColor(0, 0), QColor("red"))
        smaller = self.observer.observe(SimpleNamespace(enabled=True, max_capture_width=320))
        self.assertEqual((smaller.image.width(), smaller.image.height()), (320, 180))

    def test_high_fps_input_is_throttled_before_frame_conversion(self):
        generation = self.start()
        previews = QSignalSpy(self.observer.frame_ready)
        first = self.frame()
        self.time += 0.1
        skipped = CountedFrame()
        self.observer._accept_frame(generation, skipped)
        self.assertEqual(skipped.conversions, 0)
        self.time += 0.4
        next_frame = self.frame()
        self.assertEqual((first.conversions, next_frame.conversions, previews.count()), (1, 1, 2))

    def test_invalid_null_and_conversion_error_never_produce_observations(self):
        generation = self.start()
        for frame in (CountedFrame(valid=False), CountedFrame(QImage()), CountedFrame(error=True)):
            self.time += 1
            self.observer._accept_frame(generation, frame)
            result = self.observer.observe(self.settings)
            self.assertIsNone(result.image)
            self.assertEqual(result.blocked_code, "camera_no_frame")
            self.assertNotIn("SYNTHETIC_PRIVATE", result.blocked_reason)

    def test_expired_or_black_frames_do_not_call_a_model_or_remain_current(self):
        self.start()
        self.frame()
        valid = self.observer.observe(self.settings)
        self.assertTrue(self.observer.is_current(valid, self.settings))
        self.time += 5.01
        self.assertEqual(self.observer.current_blocked_code(self.settings), "camera_no_frame")
        self.assertFalse(self.observer.is_current(valid, self.settings))
        self.assertIsNone(self.observer.observe(self.settings).image)
        self.frame(image(color="black"))
        black = self.observer.observe(self.settings)
        self.assertIsNone(black.image)
        self.assertEqual(black.blocked_code, "capture_unavailable")

    def test_stop_releases_every_resource_clears_pixels_and_ignores_late_callbacks(self):
        generation = self.start()
        self.frame()
        old = self.observer.observe(self.settings)
        camera, session, sink, callbacks = self.cameras[0], self.sessions[0], self.sinks[0], self.observer._callbacks
        self.observer.stop()
        self.assertFalse(self.observer.running)
        self.assertEqual(camera.stops, 1)
        self.assertIsNone(session.camera)
        self.assertIsNone(session.sink)
        self.assertTrue(sink.cleared)
        self.assertIsNone(self.observer._image)
        late = CountedFrame()
        self.observer._accept_frame(generation, late)
        callbacks.error(QCamera.Error.CameraError, "late backend text")
        self.assertEqual(late.conversions, 0)
        self.assertEqual(self.observer.error, "")
        self.assertFalse(self.observer.is_current(old, self.settings))

    def test_same_device_restart_keeps_fingerprint_but_invalidates_old_requests(self):
        self.start()
        self.frame()
        old = self.observer.observe(self.settings)
        fingerprint = self.observer.source_fingerprint
        self.observer.stop()
        self.start()
        self.frame()
        self.assertEqual(self.observer.source_fingerprint, fingerprint)
        self.assertFalse(self.observer.is_current(old, self.settings))
        self.assertTrue(self.observer.is_current(self.observer.observe(self.settings), self.settings))

    def test_permission_or_driver_error_is_static_emitted_once_and_releases_device(self):
        self.fail_camera = True
        errors = QSignalSpy(self.observer.error_changed)
        self.assertFalse(self.observer.start())
        self.assertEqual(errors.count(), 1)
        self.assertIn("系统相机权限", self.observer.error)
        self.assertNotIn("SYNTHETIC_PRIVATE", self.observer.error)
        self.assertFalse(self.observer.running)
        self.assertEqual(self.cameras[0].stops, 1)
        self.cameras[0].errorOccurred.emit(QCamera.Error.CameraError, "late")
        self.observer.stop()
        self.assertEqual(errors.count(), 1)

    def test_unplug_releases_cache_and_reconnect_requires_explicit_start(self):
        self.start()
        self.frame()
        monitor = self.monitors[0]
        self.device_list.clear()
        monitor.videoInputsChanged.emit()
        self.assertFalse(self.observer.running)
        self.assertIsNone(self.observer._image)
        self.assertEqual(self.observer.observe(self.settings).blocked_code, "camera_disconnected")
        self.device_list.append(Device())
        monitor.videoInputsChanged.emit()
        self.assertFalse(self.observer.running)
        self.assertEqual(len(self.cameras), 1)
        self.assertTrue(self.observer.start())

    def test_backend_stops_after_activation_fail_closed(self):
        self.start()
        self.frame()
        self.cameras[0].activeChanged.emit(False)
        self.assertFalse(self.observer.running)
        self.assertEqual(self.observer.observe(self.settings).blocked_code, "camera_unavailable")

    def test_revoking_consent_and_shutdown_discard_frames_and_prevent_reopen(self):
        self.start()
        self.frame()
        disabled = self.observer.observe(replace(self.settings, enabled=False))
        self.assertEqual(disabled.blocked_code, "disabled")
        self.assertFalse(self.observer.running)
        self.assertIsNone(self.observer._image)
        self.observer.shutdown()
        calls = self.devices.call_count
        self.assertFalse(self.observer.start())
        self.assertEqual(self.devices.call_count, calls)


if __name__ == "__main__":
    unittest.main()
