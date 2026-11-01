"""Read-only verification of the real neutral-art eye renderer.

All cursor positions are simulated in logical Qt coordinates. This process uses
offscreen Qt, temporary preferences, and a memory-only credential backend.
Pillow writes inspection images only; Photoshop owns the delivered eye artwork.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

NATIVE_MODE = "--native" in sys.argv
os.environ["QT_QPA_PLATFORM"] = "windows" if NATIVE_MODE else "offscreen"
WORK = Path(__file__).resolve().parent
BATCH = WORK.parent
ROOT = BATCH.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt
from PySide6.QtGui import QCursor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.eye_tracking import EyeTracker, image_fingerprint
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.settings_manager import CredentialStore, SettingsManager

RIG_DIRECTORY = ROOT / "assets" / "character" / "eyes" / "neutral"
RIG_PATH = RIG_DIRECTORY / "rig.json"
NEUTRAL_PATH = ROOT / "assets" / "expressions" / "neutral.png"
PREVIEW = BATCH / "预览"
DIRECTIONS = (
    ("up_left", "左上", -1, -1), ("up", "上", 0, -1), ("up_right", "右上", 1, -1),
    ("left", "左", -1, 0), ("center", "正中", 0, 0), ("right", "右", 1, 0),
    ("down_left", "左下", -1, 1), ("down", "下", 0, 1), ("down_right", "右下", 1, 1),
)


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qarray(image):
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = np.frombuffer(converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes())
    return data.reshape(converted.height(), converted.bytesPerLine())[:, :converted.width() * 4].reshape(converted.height(), converted.width(), 4).copy()


def snapshot(pet):
    image = QImage(pet.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        pet.render(painter, QPoint())
    finally:
        painter.end()
    return qarray(image)


def source_global(pet, source):
    rig = pet._eye_tracking.rig
    rect = QRectF(pet.sprite_rect)
    local = QPointF(rect.x() + source.x() * rect.width() / rig.canvas.width(),
                    rect.y() + source.y() * rect.height() / rig.canvas.height())
    local = pet.character_transform.map(local)
    return QPointF(pet.mapToGlobal(QPoint())) + local


def settle(pet, x, y):
    rig = pet._eye_tracking.rig
    source = rig.face_center + QPointF(x * rig.response_radius.x() * 4,
                                      y * rig.response_radius.y() * 4)
    pet._advance_eye_tracking(2.0, source_global(pet, source))
    return tuple(pet._eye_tracking.gaze)


def send_mouse(pet, kind, global_pos, pressed=False):
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    event = QMouseEvent(kind, QPointF(pet.mapFromGlobal(global_pos.toPoint())), global_pos,
                        button, Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pet, event)


def blue_pixels(array):
    rgb = array[:, :, :3].astype(np.int16)
    return ((rgb[:, :, 2] > rgb[:, :, 0] + 25) &
            (rgb[:, :, 2] > rgb[:, :, 1] + 12) & (array[:, :, 3] > 200))


def eye_crop(aperture_union):
    ys, xs = np.nonzero(aperture_union)
    xmin, ymin, xmax, ymax = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    width = xmax - xmin
    return (max(0, xmin - int(width * .55)), max(0, ymin - int(width * .45)),
            min(1024, xmax + int(width * .55)), min(1024, ymax + int(width * .45)))


def contacts(frames, aperture_union):
    crop = eye_crop(aperture_union)
    cell_w, cell_h, header, margin = 450, 360, 84, 18
    canvas = Image.new("RGB", (cell_w * 3 + margin * 2, cell_h * 3 + header + margin), "#111722")
    draw = ImageDraw.Draw(canvas)
    fontpath = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    font = ImageFont.truetype(str(fontpath), 24) if fontpath.exists() else ImageFont.load_default()
    small = ImageFont.truetype(str(fontpath), 18) if fontpath.exists() else ImageFont.load_default()
    draw.text((margin, 14), "鲸鱼娘 · 鼠标视线试作 / 九宫格实测", font=font, fill="white")
    draw.text((margin, 50), "真实 PetWindow 离屏绘制 · 模拟鼠标方向 · 原图和命中区域保持不变", font=small, fill="#BAC3D3")
    for index, (code, label, _x, _y) in enumerate(DIRECTIONS):
        x = margin + (index % 3) * cell_w
        y = header + (index // 3) * cell_h
        draw.rectangle((x, y, x + cell_w - 8, y + cell_h - 8), fill="#252B39")
        image = Image.fromarray(frames[code], "RGBA").crop(crop)
        zoom = min((cell_w - 26) / image.width, (cell_h - 65) / image.height)
        image = image.resize((round(image.width * zoom), round(image.height * zoom)), Image.Resampling.LANCZOS)
        canvas.paste(image, (x + (cell_w - 8 - image.width) // 2, y + 10), image)
        draw.text((x + 18, y + cell_h - 46), label + " / " + code, font=small, fill="white")
    path = PREVIEW / "九宫格_视线跟随.png"
    canvas.save(path)
    return str(path.relative_to(ROOT))


def animated_preview(pet, aperture_union, canonical_frame):
    """QA-only GIF of actual widget paints; a shared palette prevents flicker."""
    count = 96
    rig = pet._eye_tracking.rig
    crop = eye_crop(aperture_union)

    def advance(index):
        angle = math.tau * index / count
        target = rig.face_center + QPointF(-math.cos(angle) * rig.response_radius.x() * 1.5,
                                           -math.sin(angle) * rig.response_radius.y() * 1.5)
        pet._advance_eye_tracking(1 / 30, source_global(pet, target))

    # Warm up a complete cycle so the first/last GIF frames join smoothly.
    for index in range(count):
        advance(index)
    images, maximum_outside_changes = [], 0
    for index in range(count):
        advance(index)
        frame = snapshot(pet)
        changed = np.any(frame != canonical_frame, axis=2)
        maximum_outside_changes = max(maximum_outside_changes, int(np.count_nonzero(changed & ~aperture_union)))
        face = Image.fromarray(frame, "RGBA").crop(crop)
        zoom = 500 / face.width
        face = face.resize((500, round(face.height * zoom)), Image.Resampling.LANCZOS)
        image = Image.new("RGB", face.size, "#252B39")
        image.paste(face, (0, 0), face)
        images.append(image)
    palette = images[0].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    frames = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    destination = PREVIEW / "鼠标视线效果.gif"
    frames[0].save(destination, save_all=True, append_images=frames[1:], loop=0,
                   duration=[30, 30, 40] * (count // 3), optimize=False, disposal=1)
    with Image.open(destination) as encoded:
        encoded_count = encoded.n_frames
        duration = 0
        for index in range(encoded_count):
            encoded.seek(index)
            encoded.load()
            duration += encoded.info.get("duration", 0)
    return {"path": str(destination.relative_to(ROOT)), "captured_frame_count": count,
            "encoded_frame_count": encoded_count, "decoded_duration_seconds": duration / 1000,
            "actual_widget_rendering": True,
            "changed_pixels_outside_aperture_maximum": maximum_outside_changes,
            "shared_palette": True}


def main():
    PREVIEW.mkdir(parents=True, exist_ok=True)
    report = {"checked_utc": datetime.now(timezone.utc).isoformat(), "status": "failed", "errors": [],
              "platform": "offscreen", "production_settings_touched": False,
              "credential_backend": "temporary memory-only", "physical_cursor_moved": False,
              "native_windows_shown": False, "directions": [], "scales": [], "unsupported_expressions": []}

    def require(condition, message):
        if not condition:
            report["errors"].append(message)

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    windows = []
    protected = [ROOT / "assets" / "whale.png", ROOT / "assets" / "character" / "master.png"]
    protected += sorted((ROOT / "assets" / "expressions").glob("*.png"))
    protected += sorted(RIG_DIRECTORY.glob("*"))
    baseline = {str(p.relative_to(ROOT)): sha256(p) for p in protected if p.is_file()}
    try:
        edge_report = WORK / "edge_drag_regression.json"
        if edge_report.exists():
            edge = json.loads(edge_report.read_text(encoding="utf-8"))
            report["edge_drag_regression"] = {"tests_run": edge["tests_run"], "passed": edge["passed"],
                                               "evidence": str(edge_report.relative_to(ROOT))}
            require(edge["passed"], "Existing edge-drag regression failed")
        metadata = json.loads(RIG_PATH.read_text(encoding="utf-8"))
        report["rig_metadata"] = metadata
        require(metadata["canvas"] == [1024, 1024], "Rig canvas must be 1024")
        require(metadata["source_sha256"] == sha256(NEUTRAL_PATH), "Rig is not bound to canonical neutral PNG")
        aperture_union = np.zeros((1024, 1024), dtype=bool)
        apertures = []
        report["iris_geometry_checks"] = []
        for eye in metadata["eyes"]:
            aperture = np.array(Image.open(RIG_DIRECTORY / eye["aperture"]).convert("RGBA"))[:, :, 3]
            require(aperture.shape == (1024, 1024) and np.any(aperture), "Invalid aperture: " + eye["name"])
            ay, ax = np.nonzero(aperture)
            opening = [int(ax.min()), int(ay.min()), int(ax.max()) + 1, int(ay.max()) + 1]
            require(opening[2] - opening[0] < 150 and opening[3] - opening[1] < 150,
                    "Aperture is larger than an eye: " + eye["name"])
            require(opening[0] <= eye["center"][0] <= opening[2] and opening[1] <= eye["center"][1] <= opening[3],
                    "Aperture does not contain eye center: " + eye["name"])
            iris_image = Image.open(RIG_DIRECTORY / eye["iris"]).convert("RGBA")
            iris_bounds = iris_image.getchannel("A").getbbox()
            require(iris_image.width < 150 and iris_image.height < 150 and iris_bounds is not None,
                    "Iris export must be a small nonempty eye crop: " + eye["name"])
            if iris_bounds is None:
                raise ValueError("Empty iris sprite: " + eye["name"])
            ox, oy = eye["iris_origin"]
            global_bounds = [iris_bounds[0] + ox, iris_bounds[1] + oy,
                             iris_bounds[2] + ox, iris_bounds[3] + oy]
            tx, ty = eye["travel"]
            near_opening = (global_bounds[0] >= opening[0] - tx - 3 and global_bounds[2] <= opening[2] + tx + 3 and
                            global_bounds[1] >= opening[1] - ty - 3 and global_bounds[3] <= opening[3] + ty + 3)
            require(near_opening, "Iris contains artwork away from its own eye aperture: " + eye["name"])
            require(abs((global_bounds[0] + global_bounds[2]) / 2 - eye["center"][0]) <= 8 and
                    abs((global_bounds[1] + global_bounds[3]) / 2 - eye["center"][1]) <= 8,
                    "Iris crop is misaligned with eye center: " + eye["name"])
            report["iris_geometry_checks"].append({"name": eye["name"], "sprite_size": list(iris_image.size),
                                                    "iris_global_alpha_bounds": global_bounds,
                                                    "aperture_alpha_bounds": opening, "confined_near_own_eye": near_opening})
            require(not np.any(aperture_union & (aperture > 0)), "Eye apertures overlap")
            aperture_union |= aperture > 0
            apertures.append((eye["name"], aperture))
        require(len(apertures) == 2, "Expected two independently clipped eyes")
        original = np.array(Image.open(NEUTRAL_PATH).convert("RGBA"))
        base = np.array(Image.open(RIG_DIRECTORY / metadata["base"]).convert("RGBA"))
        report["base_sclera_checks"] = []
        for name, aperture in apertures:
            interior = aperture > 200
            old_blue = int(np.count_nonzero(blue_pixels(original) & interior))
            remaining_blue = int(np.count_nonzero(blue_pixels(base) & interior))
            report["base_sclera_checks"].append({"name": name, "original_blue_iris_pixels": old_blue,
                                                "blue_pixels_left_in_base": remaining_blue})
            require(old_blue > 0 and remaining_blue <= max(3, old_blue * .02), "Old pupil remains in neutral base: " + name)

        with tempfile.TemporaryDirectory(prefix="aura-gaze-qa-") as temp, ExitStack() as stack:
            directory = Path(temp)
            stack.enter_context(patch("desktop_pet.pet_window.work_area", return_value=QRect(0, 0, 4096, 4096)))
            stack.enter_context(patch("desktop_pet.pet_window.screen_for_rect", return_value=None))
            stack.enter_context(patch("desktop_pet.pet_window.screen_for_point", return_value=None))
            settings = SettingsManager(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat),
                                       CredentialStore(backend=MemoryCredentials()))
            animation = AnimationConfig(idle_amplitude=0, breathing_amplitude=0, happy_amplitude=0, surprised_amplitude=0)
            config = PetConfig(window_width=1024, window_height=1024, character_size=1024,
                               padding=0, bubble_height=0, animation=animation)
            pet = PetWindow(config)
            pet.refresh_timer.stop()
            windows.append(pet)
            controller = PetController(pet, settings, animation_directory=directory / "no-video-clips")
            controller.set_expression("neutral", feedback=False)
            require(pet.eye_tracking_active and pet._eye_tracking.rig is not None, "Installed neutral rig did not activate")
            require(pet.sprite_rect == QRect(0, 0, 1024, 1024), "QA source-to-widget mapping must be exactly 1:1")
            source_before = image_fingerprint(pet._source.toImage())
            hit_before = image_fingerprint(pet._hit_image)
            envelope_before = QRectF(pet.character_placement_bounds)

            pet.set_eye_tracking_enabled(False)
            canonical_frame = snapshot(pet)
            Image.fromarray(canonical_frame, "RGBA").save(PREVIEW / "原始正常表情_运行时.png")
            pet.set_eye_tracking_enabled(True)
            frames = {}
            with patch("desktop_pet.eye_tracking.load_eye_rig", side_effect=AssertionError("Per-frame rig reload")), \
                    patch("desktop_pet.eye_tracking._read_image", side_effect=AssertionError("Per-frame image read")):
                for code, label, x, y in DIRECTIONS:
                    gaze = settle(pet, x, y)
                    frame = snapshot(pet)
                    frames[code] = frame
                    Image.fromarray(frame, "RGBA").save(PREVIEW / (code + ".png"))
                    changed = np.any(frame != canonical_frame, axis=2)
                    outside = int(np.count_nonzero(changed & ~aperture_union))
                    magnitude = math.hypot(x, y)
                    target = (x / magnitude, y / magnitude) if magnitude else (0, 0)
                    require(max(abs(gaze[i] - target[i]) for i in range(2)) < 1e-6, "Wrong logical gaze direction: " + code)
                    require(outside == 0, "Pixels changed outside eye aperture: " + code)
                    require(pet.character_placement_bounds == envelope_before, "Gaze changed placement bounds")
                    report["directions"].append({"code": code, "label": label, "gaze": gaze,
                                                 "changed_pixels": int(np.count_nonzero(changed)),
                                                 "changed_pixels_outside_aperture": outside,
                                                 "runtime_frame_sha256": hashlib.sha256(frame.tobytes()).hexdigest()})
            require(len({hashlib.sha256(a.tobytes()).hexdigest() for a in frames.values()}) == 9, "Some gaze directions produced identical frames")
            require(image_fingerprint(pet._source.toImage()) == source_before, "Gaze mutated source sprite")
            require(image_fingerprint(pet._hit_image) == hit_before, "Gaze mutated hit image")
            report["source_pixels_preserved"] = True
            report["hit_image_preserved"] = True
            report["placement_bounds_preserved"] = True
            report["no_per_frame_asset_reads"] = True
            report["contact_sheet"] = contacts(frames, aperture_union)
            report["animated_preview"] = animated_preview(pet, aperture_union, canonical_frame)
            require(report["animated_preview"]["changed_pixels_outside_aperture_maximum"] == 0, "GIF runtime sweep changed pixels outside eyes")
            require(report["animated_preview"]["encoded_frame_count"] >= 8 and abs(report["animated_preview"]["decoded_duration_seconds"] - 3.2) < .01,
                    "GIF failed full-frame duration/decode verification")

            # Exercise the same no-argument cursor path used by the real timer.
            import desktop_pet.pet_window as pet_module
            cursor = source_global(pet, pet._eye_tracking.rig.face_center)
            with patch.object(pet_module, "QCursor") as fake_cursor:
                fake_cursor.pos.return_value = cursor.toPoint()
                pet._advance_eye_tracking(2.0)
                require(fake_cursor.pos.call_count == 1 and max(abs(v) for v in pet._eye_tracking.gaze) < .003, "Timer cursor polling did not use logical coordinates")
            report["timer_cursor_polling_checked"] = True

            for scale in (50, 75, 100, 125, 150, 200):
                controller.set_scale(scale)
                pet.set_eye_tracking_enabled(True)
                rig = pet._eye_tracking.rig
                target = rig.face_center + QPointF(rig.response_radius.x() * .5, rig.response_radius.y() * -.2)
                pet._advance_eye_tracking(2.0, source_global(pet, target))
                gaze = tuple(pet._eye_tracking.gaze)
                require(abs(gaze[0] - .5) < 1e-6 and abs(gaze[1] + .2) < 1e-6, "Gaze mapping drift at scale " + str(scale))
                require(pet.eye_tracking_active, "Scaling deactivated rig")
                report["scales"].append({"percent": scale, "window": [pet.width(), pet.height()],
                                         "sprite": [pet.sprite_rect.width(), pet.sprite_rect.height()], "gaze": gaze})
            controller.set_scale(100)

            for expression in ("happy", "surprised", "sleepy", "angry", "thinking", "talking"):
                controller.set_expression(expression, feedback=False)
                active = bool(pet.eye_tracking_active)
                require(not active, "Neutral eye rig incorrectly enabled for " + expression)
                report["unsupported_expressions"].append({"expression": expression, "rig_active": active})
            controller.set_expression("neutral", feedback=False)
            pet.animation.advance(2)
            require(pet.eye_tracking_active, "Neutral did not reactivate gaze")

            pet.set_frame_sequence((QImage(str(NEUTRAL_PATH)), QImage(str(ROOT / "assets" / "expressions" / "happy.png"))))
            require(not pet.eye_tracking_active, "Sequence did not suspend gaze renderer")
            sequence_before = snapshot(pet)
            pet._advance_eye_tracking(2.0, QPointF(-10000, 10000))
            require(np.array_equal(sequence_before, snapshot(pet)), "Gaze altered sequence frame")
            pet.set_frame_index(1)
            require(not np.array_equal(sequence_before, snapshot(pet)), "Sequence frame switch failed")
            pet.clear_frame_sequence()
            require(pet.eye_tracking_active, "Sequence clear did not restore neutral gaze")
            report["sequence_gating"] = True

            # Real synthetic Qt events freeze eyes and preserve deferred emotion.
            settle(pet, -1, 0)
            frozen_gaze = tuple(pet._eye_tracking.gaze)
            frozen_frame = snapshot(pet)
            press = source_global(pet, QPointF(500, 745))
            require(pet.character_contains(pet.mapFromGlobal(press.toPoint())), "Drag point is not visible art")
            send_mouse(pet, QEvent.Type.MouseButtonPress, press, pressed=True)
            require(pet.animation.state == AnimationState.DRAGGING, "Qt press did not start drag")
            controller.set_expression("happy", feedback=False)
            destination = press + QPointF(35, -20)
            send_mouse(pet, QEvent.Type.MouseMove, destination, pressed=True)
            pet._advance_eye_tracking(2.0, QPointF(100000, -100000))
            require(tuple(pet._eye_tracking.gaze) == frozen_gaze and np.array_equal(snapshot(pet), frozen_frame), "Eyes or expression jumped during drag")
            send_mouse(pet, QEvent.Type.MouseButtonRelease, destination)
            require(pet.animation.state == AnimationState.RETURNING, "Drag release did not return smoothly")
            pet._advance_eye_tracking(1.0, QPointF(100000, -100000))
            require(tuple(pet._eye_tracking.gaze) == frozen_gaze, "Eyes moved during return transition")
            pet.animation.advance(2)
            pet.frame_tick.emit(2)
            require(controller.expression == "happy" and not pet.eye_tracking_active, "Deferred happy expression was lost after drag")
            report["drag_freeze_and_deferred_expression"] = True

            # Missing/stale optional rig must preserve ordinary neutral rendering.
            controller.set_expression("neutral", feedback=False)
            pet.animation.advance(2)
            pet.set_eye_tracking_enabled(False)
            ordinary = snapshot(pet)
            missing = EyeTracker(directory / "missing-rig.json", NEUTRAL_PATH)
            missing.bind_source(pet._source.toImage())
            pet._eye_tracking = missing
            pet._advance_eye_tracking(2, QPointF(-10000, -10000))
            require(not pet.eye_tracking_active and np.array_equal(ordinary, snapshot(pet)), "Missing rig did not preserve neutral sprite")
            stale_data = dict(metadata)
            stale_data["source_sha256"] = "0" * 64
            stale = directory / "stale-rig.json"
            stale.write_text(json.dumps(stale_data), encoding="utf-8")
            require(EyeTracker(stale, NEUTRAL_PATH).rig is None, "Stale source hash was accepted")
            bad = directory / "invalid-rig.json"
            bad.write_text("{invalid json", encoding="utf-8")
            require(EyeTracker(bad, NEUTRAL_PATH).rig is None, "Invalid JSON rig was accepted")
            report["missing_stale_corrupt_rig_fallback"] = True
    except Exception as exc:
        import traceback
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
        report["traceback"] = traceback.format_exc()
    finally:
        for pet in windows:
            pet.close()
            pet.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        report["protected_files"] = []
        for rel, digest in baseline.items():
            path = ROOT / rel
            unchanged = path.exists() and sha256(path) == digest
            report["protected_files"].append({"path": rel, "sha256": digest, "unchanged": unchanged})
            require(unchanged, "Protected artwork changed: " + rel)
    report["status"] = "failed" if report["errors"] else "passed"
    destination = WORK / "verification.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "report": str(destination)}, ensure_ascii=True))
    return 1 if report["errors"] else 0


def native_main():
    """Opt-in native cursor/timer smoke; restore pointer and close our window."""
    from PySide6.QtTest import QTest
    PREVIEW.mkdir(parents=True, exist_ok=True)
    output = PREVIEW / "原生窗口"
    output.mkdir(exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    original_cursor = QCursor.pos()
    report = {"checked_utc": datetime.now(timezone.utc).isoformat(), "status": "failed", "errors": [],
              "platform": "windows", "production_settings_touched": False,
              "credential_backend": "temporary memory-only", "network_used": False,
              "physical_cursor_moved": True, "original_cursor": [original_cursor.x(), original_cursor.y()],
              "existing_frame_timer_used": True, "injected_gaze_coordinates": False,
              "directions": []}
    pet = None

    def require(condition, message):
        if not condition:
            report["errors"].append(message)

    try:
        with tempfile.TemporaryDirectory(prefix="aura-native-gaze-qa-") as temp:
            directory = Path(temp)
            settings = SettingsManager(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat),
                                       CredentialStore(backend=MemoryCredentials()))
            animation = AnimationConfig(idle_amplitude=0, breathing_amplitude=0, happy_amplitude=0, surprised_amplitude=0)
            pet = PetWindow(PetConfig(animation=animation))
            pet.setWindowTitle("AURA · 鼠标视线原生验收（临时）")
            controller = PetController(pet, settings, animation_directory=directory / "no-video-clips")
            controller.set_expression("neutral", feedback=False)
            require(pet.eye_tracking_active, "Neutral rig failed to activate in native Qt")
            area = app.primaryScreen().availableGeometry()
            pet.move(area.center() - pet.rect().center())
            ticks = []
            pet.frame_tick.connect(lambda dt: ticks.append(dt))
            pet.show()
            QTest.qWait(250)
            report["timer_interval_ms"] = pet.refresh_timer.interval()
            report["device_pixel_ratio"] = pet.devicePixelRatioF()
            report["window_logical_size"] = [pet.width(), pet.height()]
            report["transparent_window"] = pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            require(pet.refresh_timer.isActive() and pet.refresh_timer.interval() <= 34, "Existing 30fps frame timer is not running")
            for code, x, y in (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1), ("center", 0, 0)):
                rig = pet._eye_tracking.rig
                source = rig.face_center + QPointF(x * rig.response_radius.x() * 1.5,
                                                  y * rig.response_radius.y() * 1.5)
                target = source_global(pet, source).toPoint()
                target.setX(max(area.left() + 16, min(target.x(), area.right() - 16)))
                target.setY(max(area.top() + 16, min(target.y(), area.bottom() - 16)))
                first_tick = len(ticks)
                QCursor.setPos(target)
                QTest.qWait(600)
                gaze = tuple(pet._eye_tracking.gaze)
                sampled = QCursor.pos()
                frame = pet.grab()
                path = output / ("native_" + code + ".png")
                frame.save(str(path), "PNG")
                require((sampled - target).manhattanLength() <= 2, "Native pointer did not reach target: " + code)
                require(len(ticks) - first_tick >= 8, "Existing timer did not sample cursor: " + code)
                require((x == 0 or gaze[0] * x > .25) and (y == 0 or gaze[1] * y > .25), "Wrong real-pointer gaze sign: " + code)
                if code == "center":
                    require(max(abs(v) for v in gaze) < .025, "Native cursor center did not settle")
                report["directions"].append({"code": code, "target_logical": [target.x(), target.y()],
                                             "actual_cursor_logical": [sampled.x(), sampled.y()],
                                             "gaze": gaze, "timer_ticks": len(ticks) - first_tick,
                                             "own_window_capture": str(path.relative_to(ROOT))})
            report["total_timer_ticks"] = len(ticks)
    except Exception as exc:
        import traceback
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
        report["traceback"] = traceback.format_exc()
    finally:
        QCursor.setPos(original_cursor)
        restored = QCursor.pos()
        report["pointer_restored"] = (restored - original_cursor).manhattanLength() <= 2
        require(report["pointer_restored"], "Original pointer position was not restored")
        if pet is not None:
            pet.close()
            pet.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        report["temporary_window_closed"] = True
    report["status"] = "failed" if report["errors"] else "passed"
    path = WORK / "native_smoke.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "report": str(path)}, ensure_ascii=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(native_main() if NATIVE_MODE else main())
