"""Measure real, reviewed animation packs using an isolated offscreen app.

Example (only after real assets are published):
  .venv\\Scripts\\python.exe -m tests.real_animation_smoke \
      --directory assets/character/animations --names think_scratch_head \
      --expect-frames 60 --output artifacts/character_assets/real_playback

This never creates frames, changes manifests, contacts a model or reads the
desktop. QMouseEvents are delivered only to this process's own offscreen pet.
"""
import argparse
import ctypes
import gc
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QThreadPool, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import DEFAULT_IMAGE, PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import ANIMATIONS, FrameAssetError, read_manifest
from tests.test_application import MemoryCredentialBackend, isolated_settings


def memory_snapshot():
    """Query only our process; peak RSS includes native Qt image allocations."""
    if os.name != "nt":
        return {"available": False, "reason": "Windows own-process metrics require Windows"}
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return {"available": False, "winerror": ctypes.get_last_error()}
    return {"available": True, "rss_mib": round(counters.WorkingSetSize / 2**20, 3),
            "peak_rss_mib": round(counters.PeakWorkingSetSize / 2**20, 3),
            "private_mib": round(counters.PrivateUsage / 2**20, 3)}


class Heartbeat:
    def __init__(self):
        self.timestamps = []
        self.timer = QTimer()
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(lambda: self.timestamps.append(time.perf_counter()))
        self.timer.start()

    def measure(self, operation):
        self.timestamps.clear()
        start, cpu = time.perf_counter(), time.process_time()
        operation()
        end = time.perf_counter()
        points = [start, *self.timestamps, end]
        gaps = [max(0.0, b - a) * 1000 for a, b in zip(points, points[1:])]
        ordered = sorted(gaps)
        return {"seconds": round(end - start, 4), "heartbeat_count": len(self.timestamps),
                "heartbeat_gap_max_ms": round(max(gaps, default=0), 3),
                "heartbeat_gap_p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 3),
                "cpu_core_percent": round((time.process_time() - cpu) / max(end - start, 1e-9) * 100, 2),
                "memory": memory_snapshot()}


def pump_until(predicate, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError("Offscreen animation step timed out")


def pump_seconds(seconds):
    end = time.monotonic() + seconds
    pump_until(lambda: time.monotonic() >= end, seconds + 5)


def render_pet(pet, path):
    image = QImage(pet.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        pet.render(painter, QPoint())
    finally:
        painter.end()
    if not image.save(str(path), "PNG"):
        raise AssertionError("Could not save our own offscreen widget image")
    assert image.pixelColor(0, 0).alpha() == 0


def opaque_point(pet):
    image = pet._hit_image
    # Search from the center outward; transparent padding is never a hit target.
    center = QPoint(image.width() // 2, image.height() // 2)
    for radius in range(0, max(image.width(), image.height()), 3):
        for dy in range(-radius, radius + 1, 3):
            for dx in (-radius, radius):
                x, y = center.x() + dx, center.y() + dy
                if 0 <= x < image.width() and 0 <= y < image.height() and image.pixelColor(x, y).alpha() > 128:
                    return pet.character_transform.map(QPointF(pet.sprite_rect.topLeft() + QPoint(x, y))).toPoint()
    raise AssertionError("No opaque character point for synthetic pointer event")


def send_mouse(pet, kind, global_pos, button, buttons):
    event = QMouseEvent(kind, QPointF(pet.mapFromGlobal(global_pos)), QPointF(global_pos),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pet, event)


def check_clip(session, directory, name, expected_count, output, heartbeat):
    controller, pet = session.pet_controller, session.pet
    player = controller.asset_player
    player.directory = directory
    manifest = read_manifest(directory, name)
    if expected_count is not None:
        assert len(manifest.paths) == expected_count, f"Expected {expected_count} real frames, got {len(manifest.paths)}"
    metadata_path = directory / name / "animation.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata.get("complete") is True and metadata.get("qa_passed") is True, "Only reviewed product assets may run"
    assert metadata.get("origin", {}).get("synthetic") is False, "Real origin.synthetic:false must be explicit"
    hashes_before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (metadata_path, *manifest.paths)}
    record = {"name": name, "frame_count": len(manifest.paths), "fps": manifest.fps,
              "duration_seconds": manifest.duration, "origin": metadata.get("origin", {}),
              "raw_rgba_mib": len(manifest.paths) * 1024 * 1024 * 4 / 2**20,
              "before": memory_snapshot(), "assertions": []}
    folder = output / name
    folder.mkdir(parents=True, exist_ok=True)
    controller.set_static_character("tsundere")
    static_key = pet._source.cacheKey()

    def start():
        assert controller.play_asset_animation(name), player.status
        pump_until(lambda: player._installed or (not player._loading and player._clip is None))
        assert player._installed, player.status

    record["load_and_cache"] = heartbeat.measure(start)
    assert len(pet._sequence_frames) == len(manifest.paths)
    assert len(pet._sequence_sprites) == len(manifest.paths)
    record["assertions"].append("real frame count decoded and cached")
    render_pet(pet, folder / "playing.png")

    # A steady playback window avoids completion, so a layout call here would
    # indicate unwanted per-frame rescaling rather than normal stop/restore.
    start_index = pet._sequence_index
    playback_window = min(.9, manifest.duration / 4)
    with patch("desktop_pet.frame_animation.load_clip", side_effect=AssertionError("Playback re-read files")), \
            patch.object(pet, "_layout_character", side_effect=AssertionError("Playback rescaled a frame")):
        record["steady_playback"] = heartbeat.measure(lambda: pump_seconds(playback_window))
    assert pet._sequence_index != start_index, "Real frame index did not advance"
    record["assertions"].append("steady playback advances cached frames without load/layout")

    point = pet.mapToGlobal(opaque_point(pet))
    send_mouse(pet, QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    assert pet.animation.state == AnimationState.DRAGGING
    frozen_frame, frozen_elapsed, frozen_pose = pet._sequence_index, player._clock.elapsed, pet.animation.pose
    destination = point + QPoint(-40, -25)
    old_position = pet.pos()
    send_mouse(pet, QEvent.Type.MouseMove, destination, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    assert pet._dragged and pet.pos() != old_position, "Offscreen drag did not move our own window"
    record["drag_pause"] = heartbeat.measure(lambda: pump_seconds(.45))
    assert pet._sequence_index == frozen_frame and player._clock.elapsed == frozen_elapsed
    assert pet.animation.pose == frozen_pose
    send_mouse(pet, QEvent.Type.MouseButtonRelease, destination, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert pet.animation.state == AnimationState.RETURNING
    pump_seconds(min(.1, pet.config.animation.return_duration / 3))
    assert pet._sequence_index == frozen_frame and player._clock.elapsed == frozen_elapsed
    record["smooth_return"] = heartbeat.measure(lambda: pump_until(lambda: pet.animation.state == AnimationState.IDLE))
    resumed_elapsed = player._clock.elapsed
    pump_until(lambda: player._clock.elapsed > resumed_elapsed + .09)
    record["assertions"].append("pointer drag freezes clock/pose; smooth return precedes frame resume")

    # Freeze the clip clock while checking geometry/caching at every preset.
    # Qt pointer events remain confined to our own offscreen widget.
    point = pet.mapToGlobal(opaque_point(pet))
    send_mouse(pet, QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    frozen_frame = pet._sequence_index
    record["scales"] = []
    for percent in (50, 75, 100, 125, 150, 200):
        metric = heartbeat.measure(lambda value=percent: (controller.set_scale(value), pump_seconds(.05)))
        assert pet._sequence_index == frozen_frame
        assert pet._sprite.width() == pet._sprite.height()
        assert QRectF(pet.rect()).contains(pet.animated_sprite_rect)
        assert pet._drawing_area.adjusted(-1e-8, -1e-8, 1e-8, 1e-8).contains(pet.animated_sprite_rect)
        metric.update({"percent": percent, "sprite": [pet._sprite.width(), pet._sprite.height()],
                       "scaled_image_mib": sum(image.sizeInBytes() for _pixmap, image in pet._sequence_sprites) / 2**20})
        record["scales"].append(metric)
    render_pet(pet, folder / "scale_200.png")
    # Ensure this press counts as a drag so release never opens a chat panel.
    send_mouse(pet, QEvent.Type.MouseMove, point + QPoint(-50, -30), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    controller.stop_asset_animation()
    assert pet._sequence_frames, "Stop replaced the frozen image during drag"
    send_mouse(pet, QEvent.Type.MouseButtonRelease, point + QPoint(-50, -30), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    pump_until(lambda: not pet._sequence_frames)
    assert player._clip is None and not pet._sequence_sprites
    assert pet._source.cacheKey() == static_key
    controller.reset_scale()
    gc.collect()
    record["after_stop"] = heartbeat.measure(lambda: pump_seconds(.15))
    record["assertions"].append("50..200% keep frame/canvas visible; stop frees logical caches and restores chosen static")

    # Replay from the beginning to observe the manifest's real completion rule.
    record["replay_load"] = heartbeat.measure(start)
    if manifest.loop:
        pump_seconds(manifest.duration + .1)
        assert player._installed and not player._clock.finished
        completion = "loop continued"
    elif manifest.completion == "hold_last":
        pump_until(lambda: player._clock is not None and player._clock.finished, manifest.duration + 10)
        assert pet._sequence_index == len(manifest.paths) - 1 and pet._sequence_frames
        completion = "held real final frame"
    else:
        pump_until(lambda: player._clip is None, manifest.duration + 10)
        assert not pet._sequence_frames and pet._source.cacheKey() == static_key
        completion = "returned to selected static and idle"
    render_pet(pet, folder / "completion.png")
    record["completion"] = completion
    controller.stop_asset_animation()
    record["after_completion"] = memory_snapshot()
    record["assets_unchanged"] = all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                                     for path, digest in hashes_before.items())
    assert record["assets_unchanged"]
    # Very loose stall detection catches a truly blocked GUI, while retaining
    # timings so readers can judge their hardware. Offscreen is not compositor QA.
    stages = [record["load_and_cache"], record["steady_playback"], record["drag_pause"], *record["scales"]]
    record["responsiveness_stall_limit_ms"] = 2000
    assert max(stage["heartbeat_gap_max_ms"] for stage in stages) < 2000, "Offscreen GUI stalled over two seconds"
    record["status"] = "passed"
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="Parent directory containing reviewed clip folders")
    parser.add_argument("--names", nargs="+", choices=tuple(ANIMATIONS), required=True)
    parser.add_argument("--expect-frames", type=int, default=60)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "artifacts" / "character_assets" / "real_playback")
    args = parser.parse_args(argv)
    args.directory, args.output = args.directory.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"status": "incomplete", "platform": "offscreen", "pid": os.getpid(),
              "production_ui_touched": False, "system_mouse_used": False,
              "settings": "temporary INI", "credentials": "in-memory test backend",
              "directory": str(args.directory), "clips": [],
              "limits": ["Only own-process offscreen Qt input and rendering were tested.",
                         "RSS includes Qt/native allocations and is not guaranteed to fall immediately after cache release.",
                         "240 MiB for 60 raw frames / 384 MiB for 96 raw frames excludes scaled caches and process baseline.",
                         "This script does not certify visual identity, segmentation edge quality, or native Windows compositor behavior."]}
    session = None
    heartbeat = None
    try:
        for name in args.names:
            read_manifest(args.directory, name)
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
            app.setFont(QFont("Microsoft YaHei", 10))
        original_hash = hashlib.sha256(DEFAULT_IMAGE.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="aura-real-clip-smoke-") as directory:
            settings = isolated_settings(Path(directory) / "settings.ini", MemoryCredentialBackend())
            session = ApplicationController(PetConfig(), settings=settings)
            session.show()
            session.open_debug_panel()
            app.processEvents()
            heartbeat = Heartbeat()
            try:
                for name in args.names:
                    report["clips"].append(check_clip(session, args.directory, name, args.expect_frames, args.output, heartbeat))
                report["original_asset_unchanged"] = hashlib.sha256(DEFAULT_IMAGE.read_bytes()).hexdigest() == original_hash
                assert report["original_asset_unchanged"]
                report["status"] = "passed"
            finally:
                heartbeat.timer.stop()
                session.shutdown()
                pump_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                for widget in (session.pet, session.debug_panel, session.chat_window, session.memory_panel):
                    if widget is not None:
                        widget.deleteLater()
                session.deleteLater()
                app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
    except FrameAssetError as exc:
        report["error"] = str(exc)
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output / "result.json"),
                      "error": report.get("error", "")}, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "incomplete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
