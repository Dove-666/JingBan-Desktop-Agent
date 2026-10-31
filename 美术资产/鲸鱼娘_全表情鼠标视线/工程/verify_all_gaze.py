"""Verify all expression eye rigs through actual Qt widgets.

--native uses a temporary Windows window and restores the physical pointer.
The default uses offscreen Qt and injected logical cursor coordinates. All
artwork is read-only; generated PNG/GIF files are QA compositions, not sprites.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

NATIVE = "--native" in sys.argv
os.environ["QT_QPA_PLATFORM"] = "windows" if NATIVE else "offscreen"
WORK = Path(__file__).resolve().parent
BATCH = WORK.parent
ROOT = BATCH.parent.parent
PREVIEW = BATCH / "预览"
EYES = ROOT / "assets" / "character" / "eyes"
EXPRESSIONS = ROOT / "assets" / "expressions"
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt
from PySide6.QtGui import QCursor, QImage, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.eye_tracking import EyeTracker, image_fingerprint
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.settings_manager import CredentialStore, SettingsManager

STATES = (("neutral", "neutral", "正常"), ("happy", "happy", "开心 · 闭眼静态"),
          ("surprised", "surprised", "惊讶"), ("sleepy", "sleepy", "困倦"),
          ("angry", "angry", "生气"), ("thinking", "thinking", "思考"),
          ("talking", "talk_open", "说话"))
TRACKED = tuple(stem for _code, stem, _label in STATES if stem != "happy")
POSITIONS = (("up_left", -1, -1), ("up", 0, -1), ("up_right", 1, -1),
             ("left", -1, 0), ("center", 0, 0), ("right", 1, 0),
             ("down_left", -1, 1), ("down", 0, 1), ("down_right", 1, 1))


class MemoryVault:
    def __init__(self): self.values = {}
    def get_password(self, service, account): return self.values.get((service, account))
    def set_password(self, service, account, value): self.values[(service, account)] = value
    def delete_password(self, service, account): self.values.pop((service, account), None)


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def rgba(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = np.frombuffer(image.constBits(), np.uint8, count=image.sizeInBytes())
    return raw.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def render(pet):
    image = QImage(pet.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try: pet.render(painter, QPoint())
    finally: painter.end()
    return rgba(image)


def global_source(pet, point):
    rig = pet._eye_tracking.rig
    rect = QRectF(pet.sprite_rect)
    local = QPointF(rect.x() + point.x() * rect.width() / rig.canvas.width(),
                    rect.y() + point.y() * rect.height() / rig.canvas.height())
    return QPointF(pet.mapToGlobal(QPoint())) + pet.character_transform.map(local)


def look(pet, x, y, seconds=2):
    rig = pet._eye_tracking.rig
    point = rig.face_center + QPointF(x * rig.response_radius.x() * 4, y * rig.response_radius.y() * 4)
    pet._advance_eye_tracking(seconds, global_source(pet, point))
    return tuple(pet._eye_tracking.gaze)


def activate(controller, code):
    controller.set_expression(code, feedback=False)
    controller.pet.animation.advance(2)


@lru_cache(maxsize=8)
def font(size):
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def face_crop(mask):
    ys, xs = np.nonzero(mask)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    width = x1 - x0
    return (max(0, x0 - round(width * .55)), max(0, y0 - round(width * .45)),
            min(1024, x1 + round(width * .55)), min(1024, y1 + round(width * .45)))


def tile(array, crop, size):
    face = Image.fromarray(array, "RGBA").crop(crop)
    scale = min(size[0] / face.width, size[1] / face.height)
    face = face.resize((round(face.width * scale), round(face.height * scale)), Image.Resampling.LANCZOS)
    image = Image.new("RGB", size, "#252B39")
    image.paste(face, ((size[0] - face.width) // 2, (size[1] - face.height) // 2), face)
    return image


def blue(array):
    rgb = array[:, :, :3].astype(np.int16)
    return (rgb[:, :, 2] > rgb[:, :, 0] + 25) & (rgb[:, :, 2] > rgb[:, :, 1] + 12) & (array[:, :, 3] > 200)


def inspect_art(stem, require):
    directory = EYES / stem
    metadata = json.loads((directory / "rig.json").read_text(encoding="utf-8"))
    original = np.array(Image.open(EXPRESSIONS / (stem + ".png")).convert("RGBA"))
    base_image = Image.open(directory / metadata["base"]).convert("RGBA")
    base = np.array(base_image)
    require(metadata["canvas"] == [1024, 1024] and base_image.size == (1024, 1024), stem + ": wrong canvas")
    require(metadata["source_sha256"] == sha(EXPRESSIONS / (stem + ".png")), stem + ": wrong source file SHA256")
    mask = np.zeros((1024, 1024), bool)
    result = {"stem": stem, "source_sha256": metadata["source_sha256"], "eyes": []}
    for eye in metadata["eyes"]:
        aperture = np.array(Image.open(directory / eye["aperture"]).convert("RGBA"))[:, :, 3]
        ys, xs = np.nonzero(aperture)
        opening = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
        require(opening[2] - opening[0] < 150 and opening[3] - opening[1] < 150, stem + ": oversized aperture")
        require(opening[0] <= eye["center"][0] <= opening[2] and opening[1] <= eye["center"][1] <= opening[3], stem + ": aperture misses eye center")
        image = Image.open(directory / eye["iris"]).convert("RGBA")
        bounds = image.getchannel("A").getbbox()
        require(image.width < 150 and image.height < 150 and bounds is not None, stem + ": iris must be small isolated sprite")
        if bounds is None: raise ValueError("Empty iris: " + stem)
        ox, oy = eye["iris_origin"]
        global_bounds = [bounds[0] + ox, bounds[1] + oy, bounds[2] + ox, bounds[3] + oy]
        tx, ty = eye["travel"]
        near_aperture = (global_bounds[0] >= opening[0] - tx - 3 and global_bounds[2] <= opening[2] + tx + 3 and
                         global_bounds[1] >= opening[1] - ty - 3 and global_bounds[3] <= opening[3] + ty + 3)
        # A sleepy eye needs a complete iris behind its half-closed upper lid.
        # That intentionally hidden cap extends ~17px beyond the aperture. Its
        # whole sprite must still fit the same small eye diameter and center;
        # rendered changes remain subject to the exact aperture-only check.
        cx, cy = eye["center"]
        near_full_eye = (image.width < 100 and image.height < 100 and
                         global_bounds[0] >= cx - 35 and global_bounds[2] <= cx + 35 and
                         global_bounds[1] >= cy - 35 and global_bounds[3] <= cy + 35 and
                         max(global_bounds[0], opening[0]) < min(global_bounds[2], opening[2]) and
                         max(global_bounds[1], opening[1]) < min(global_bounds[3], opening[3]))
        near = near_aperture if stem == "neutral" else near_full_eye
        require(near, stem + ": iris includes distant/body art")
        require(abs((global_bounds[0] + global_bounds[2]) / 2 - eye["center"][0]) <= 8 and
                abs((global_bounds[1] + global_bounds[3]) / 2 - eye["center"][1]) <= 8, stem + ": iris misaligned")
        interior = aperture > 200
        old_blue, residue = int(np.count_nonzero(blue(original) & interior)), int(np.count_nonzero(blue(base) & interior))
        require(old_blue > 0 and residue <= max(3, old_blue * .02), stem + ": old iris remains in base")
        opaque_interior = aperture > 250
        dark_residue = int(np.count_nonzero((base[:, :, :3].min(axis=2) < 225) & opaque_interior))
        require(dark_residue == 0, stem + ": old dark/gray iris outline remains inside clean sclera")
        require(not np.any(mask & (aperture > 0)), stem + ": apertures overlap")
        mask |= aperture > 0
        result["eyes"].append({"name": eye["name"], "iris_size": list(image.size), "iris_global_bounds": global_bounds,
                               "aperture_bounds": opening, "original_blue_pixels": old_blue, "old_blue_residue": residue,
                               "dark_or_gray_residue_inside_opaque_aperture": dark_residue,
                               "confined_near_own_eye": near, "complete_iris_with_hidden_cap_allowed": stem != "neutral"})
    require(len(result["eyes"]) == 2, stem + ": need exactly two eyes")
    return result, mask


def contact(frames, crops):
    width, height, header = 390, 260, 78
    image = Image.new("RGB", (width * 3 + 160, height * 7 + header), "#111722")
    draw = ImageDraw.Draw(image)
    draw.text((18, 14), "鲸鱼娘 · 全表情鼠标视线 / 真实运行时对照", font=font(26), fill="white")
    for col, label in enumerate(("向左", "正中", "向右")):
        draw.text((160 + col * width + width / 2, 48), label, font=font(19), fill="#C1CBDC", anchor="mt")
    for row, (_code, stem, label) in enumerate(STATES):
        y = header + row * height
        draw.multiline_text((12, y + 90), label.replace(" · ", "\n"), font=font(20), fill="white", spacing=8)
        for col, direction in enumerate(("left", "center", "right")):
            image.paste(tile(frames[stem][direction], crops[stem], (width - 8, height - 8)), (160 + col * width, y))
    path = PREVIEW / "全表情_左中右对照.png"
    image.save(path)
    return str(path.relative_to(ROOT))


def direction_grid(stem, label, frames, crop):
    width, height = 360, 255
    image = Image.new("RGB", (width * 3, height * 3 + 52), "#111722")
    draw = ImageDraw.Draw(image)
    draw.text((14, 12), label + " / " + stem + " · 真实运行时九方向", font=font(22), fill="white")
    for index, (name, _x, _y) in enumerate(POSITIONS):
        x, y = (index % 3) * width, 52 + (index // 3) * height
        image.paste(tile(frames[name], crop, (width - 8, height - 37)), (x, y))
        draw.text((x + 14, y + height - 30), name, font=font(16), fill="white")
    path = PREVIEW / stem / "九方向_实际运行时.png"
    image.save(path)
    return str(path.relative_to(ROOT))


def gif_preview(pet, controller, canonical, masks, crops, require):
    count, series, max_outside = 96, {}, 0
    for code, stem, _label in STATES:
        activate(controller, code)
        series[stem] = []
        if stem == "happy":
            still = tile(render(pet), crops[stem], (340, 220))
            series[stem] = [still] * count
            continue
        rig = pet._eye_tracking.rig
        def advance(index):
            phase = math.tau * index / count
            point = rig.face_center + QPointF(-math.cos(phase) * rig.response_radius.x() * 1.5,
                                              -math.sin(phase) * rig.response_radius.y() * 1.5)
            pet._advance_eye_tracking(1 / 30, global_source(pet, point))
        for index in range(count): advance(index)
        for index in range(count):
            advance(index)
            frame = render(pet)
            changed = np.any(frame != canonical[stem], axis=2)
            outside = int(np.count_nonzero(changed & ~masks[stem]))
            max_outside = max(max_outside, outside)
            require(outside == 0, stem + ": GIF paint changed pixels outside its own eyes")
            require(np.array_equal(frame[:, :, 3], canonical[stem][:, :, 3]), stem + ": GIF changed alpha silhouette")
            series[stem].append(tile(frame, crops[stem], (340, 220)))
    composites = []
    for index in range(count):
        image = Image.new("RGB", (700, 1100), "#111722")
        draw = ImageDraw.Draw(image)
        draw.text((16, 12), "全表情鼠标视线 · 实际 Qt 绘制", font=font(23), fill="white")
        for position, (_code, stem, label) in enumerate(STATES):
            x, y = 10 + (position % 2) * 350, 64 + (position // 2) * 255
            image.paste(series[stem][index], (x, y))
            draw.text((x + 8, y + 223), label, font=font(18), fill="white")
        draw.multiline_text((376, 890), "六种睁眼表情\n跟随鼠标方向\n\n开心闭眼保留原表情", font=font(20), fill="#C1CBDC", spacing=9)
        composites.append(image)
    palette = composites[0].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    paletted = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in composites]
    path = PREVIEW / "全表情鼠标视线效果.gif"
    paletted[0].save(path, save_all=True, append_images=paletted[1:], duration=[30, 30, 40] * 32,
                     loop=0, optimize=False, disposal=1)
    duration = 0
    with Image.open(path) as decoded:
        encoded_count = decoded.n_frames
        for index in range(encoded_count):
            decoded.seek(index); decoded.load(); duration += decoded.info.get("duration", 0)
    require(encoded_count >= 8 and duration == 3200, "GIF full decode/duration failed")
    return {"path": str(path.relative_to(ROOT)), "captured_frames_per_open_state": count,
            "encoded_frames": encoded_count, "duration_seconds": duration / 1000,
            "changed_pixels_outside_aperture_maximum": max_outside, "happy_closed_eyes_static": True,
            "shared_palette": True, "actual_widget_rendering": True}


def mouse(pet, kind, global_pos, pressed=False):
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    event = QMouseEvent(kind, QPointF(pet.mapFromGlobal(global_pos.toPoint())), global_pos, button,
                        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pet, event)


def main():
    PREVIEW.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    report = {"checked_utc": datetime.now(timezone.utc).isoformat(), "status": "failed", "errors": [],
              "platform": "offscreen", "production_settings_touched": False, "physical_cursor_moved": False,
              "credential_backend": "temporary memory-only", "states": [], "scales": [], "artwork": []}
    def require(condition, message):
        if not condition: report["errors"].append(message)
    pet = None
    initial = {Path(path).as_posix(): digest for path, digest in
               json.loads((WORK / "immutable_before.json").read_text(encoding="utf-8")).items()}
    root_protected = {}
    all_art = {p.relative_to(ROOT).as_posix(): sha(p) for stem in TRACKED for p in (EYES / stem).glob("*") if p.is_file()}
    try:
        root_baseline_path = WORK / "protected_assets_before.json"
        if root_baseline_path.exists():
            root_baseline = json.loads(root_baseline_path.read_text(encoding="utf-8"))
            report["root_baseline_checks"] = []
            entries = root_baseline.items() if isinstance(root_baseline, dict) else ((item["path"], item["sha256"]) for item in root_baseline)
            for relative, digest in entries:
                relative = Path(relative).as_posix()
                root_protected[relative] = digest
                protected_path = ROOT / relative
                # Accept absolute paths in the root audit as well as repository paths.
                unchanged = protected_path.exists() and sha(protected_path) == digest
                report["root_baseline_checks"].append({"path": relative, "unchanged": unchanged})
                require(unchanged, "Root protected-asset baseline changed: " + relative)
                require(relative not in initial or initial[relative] == digest, "Independent baselines disagree: " + relative)
        masks = {}
        for stem in TRACKED:
            art, mask = inspect_art(stem, require)
            report["artwork"].append(art); masks[stem] = mask
        with tempfile.TemporaryDirectory(prefix="aura-all-gaze-qa-") as temp, ExitStack() as stack:
            directory = Path(temp)
            for name in ("screen_for_rect", "screen_for_point"):
                stack.enter_context(patch("desktop_pet.pet_window." + name, return_value=None))
            stack.enter_context(patch("desktop_pet.pet_window.work_area", return_value=QRect(0, 0, 4096, 4096)))
            settings = SettingsManager(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat), CredentialStore(backend=MemoryVault()))
            animation = AnimationConfig(idle_amplitude=0, breathing_amplitude=0, happy_amplitude=0, surprised_amplitude=0)
            pet = PetWindow(PetConfig(window_width=1024, window_height=1024, character_size=1024, padding=0, bubble_height=0, animation=animation))
            pet.refresh_timer.stop()
            controller = PetController(pet, settings, animation_directory=directory / "no-video-clips")
            tracker = pet._eye_tracking
            require(set(tracker.rigs) == set(TRACKED), "Not all six trusted rigs were cached")
            require(pet.sprite_rect == QRect(0, 0, 1024, 1024), "QA canvas mapping is not 1:1")
            frames, canonical, crops = {}, {}, {}
            for code, stem, label in STATES:
                activate(controller, code)
                pet.set_eye_tracking_enabled(False)
                canonical[stem] = render(pet)
                pet.set_eye_tracking_enabled(True)
                frame_set = frames[stem] = {}
                (PREVIEW / stem).mkdir(exist_ok=True)
                result = {"code": code, "stem": stem, "label": label, "selected_rig": tracker.rig_name, "directions": []}
                if stem == "happy":
                    require(not pet.eye_tracking_active and tracker.rig is None, "Closed happy eyes must stay static")
                    for name, _x, _y in POSITIONS:
                        pet._advance_eye_tracking(2, QPointF(-10000 if "left" in name else 10000, 10000))
                        frame_set[name] = render(pet)
                        require(np.array_equal(frame_set[name], canonical[stem]), "Happy artwork moved with cursor")
                    crops[stem] = face_crop(masks["neutral"])
                    result["closed_eyes_static"] = True
                else:
                    require(pet.eye_tracking_active and tracker.rig_name == stem and tracker.rig is tracker.rigs[stem], stem + ": wrong cached rig selected")
                    require(tracker.rig.source_pixels_sha256 == image_fingerprint(pet._source.toImage()), stem + ": wrong source pixel fingerprint")
                    source, hit, bounds = image_fingerprint(pet._source.toImage()), image_fingerprint(pet._hit_image), QRectF(pet.character_placement_bounds)
                    with patch("desktop_pet.eye_tracking.load_eye_rig", side_effect=AssertionError("Per-frame rig reload")), \
                            patch("desktop_pet.eye_tracking._read_image", side_effect=AssertionError("Per-frame eye file read")):
                        for name, x, y in POSITIONS:
                            gaze = look(pet, x, y)
                            frame = frame_set[name] = render(pet)
                            changed = np.any(frame != canonical[stem], axis=2)
                            outside = int(np.count_nonzero(changed & ~masks[stem]))
                            require(outside == 0, stem + ": changed eyelashes/skin/body outside own aperture: " + name)
                            require(np.array_equal(frame[:, :, 3], canonical[stem][:, :, 3]), stem + ": alpha silhouette changed: " + name)
                            length = math.hypot(x, y)
                            target = (x / length, y / length) if length else (0, 0)
                            require(max(abs(gaze[i] - target[i]) for i in range(2)) < 1e-6, stem + ": wrong gaze vector")
                            require(pet.character_placement_bounds == bounds, stem + ": gaze changed placement bounds")
                            result["directions"].append({"name": name, "gaze": gaze, "changed_pixels": int(changed.sum()), "changed_pixels_outside_aperture": outside})
                    require(image_fingerprint(pet._source.toImage()) == source and image_fingerprint(pet._hit_image) == hit, stem + ": mutated original or hit image")
                    require(len({hashlib.sha256(a.tobytes()).hexdigest() for a in frame_set.values()}) == 9, stem + ": gaze directions not visually distinct")
                    crops[stem] = face_crop(masks[stem])
                    result["alpha_silhouette_preserved"] = True
                    result["source_hit_and_bounds_preserved"] = True
                for name, frame in frame_set.items():
                    Image.fromarray(frame, "RGBA").save(PREVIEW / stem / ("runtime_" + name + ".png"))
                result["nine_direction_grid"] = direction_grid(stem, label, frame_set, crops[stem])
                report["states"].append(result)

            report["contact_grid"] = contact(frames, crops)
            report["animated_preview"] = gif_preview(pet, controller, canonical, masks, crops, require)
            report["no_per_frame_eye_asset_reads"] = True

            # Enable/disable is global and works across open and closed states.
            pet.set_eye_tracking_enabled(False)
            for code, stem, _label in STATES:
                activate(controller, code)
                require(not pet.eye_tracking_active and not tracker.enabled and np.array_equal(render(pet), canonical[stem]), "Disabled toggle lost after switch: " + stem)
            pet.set_eye_tracking_enabled(True)
            for code, stem, _label in STATES:
                activate(controller, code)
                require(pet.eye_tracking_active == (stem != "happy"), "Enabled toggle lost after switch: " + stem)
            activate(controller, "neutral"); look(pet, -1, 0)
            continuous = tuple(tracker.gaze)
            for code in ("sleepy", "happy", "angry"):
                activate(controller, code)
                require(tuple(tracker.gaze) == continuous, "Normalized gaze jumped across state switch: " + code)
            report["global_toggle_and_cross_state_gaze_continuity"] = True

            for code, stem, _label in STATES:
                if stem == "happy": continue
                activate(controller, code)
                for percent in (50, 75, 100, 125, 150, 200):
                    controller.set_scale(percent)
                    rig = tracker.rig
                    point = rig.face_center + QPointF(rig.response_radius.x() * .5, rig.response_radius.y() * -.2)
                    pet._advance_eye_tracking(2, global_source(pet, point))
                    gaze = tuple(tracker.gaze)
                    require(abs(gaze[0] - .5) < 1e-6 and abs(gaze[1] + .2) < 1e-6, stem + ": logical coordinates drift with scaling")
                    report["scales"].append({"stem": stem, "percent": percent, "gaze": gaze})
                controller.set_scale(100)

            # Both newly supplied assets and old neutral share drag/clip policy.
            for code, stem, _label in STATES:
                if stem == "happy": continue
                activate(controller, code); look(pet, -1, 0)
                frozen, frozen_gaze = render(pet), tuple(tracker.gaze)
                press = global_source(pet, QPointF(500, 745))
                require(pet.character_contains(pet.mapFromGlobal(press.toPoint())), stem + ": drag point invisible")
                mouse(pet, QEvent.Type.MouseButtonPress, press, True)
                controller.set_expression("happy", feedback=False)
                destination = press + QPointF(35, -20)
                mouse(pet, QEvent.Type.MouseMove, destination, True)
                pet._advance_eye_tracking(2, QPointF(100000, -100000))
                require(pet.animation.state == AnimationState.DRAGGING and tracker.gaze == frozen_gaze and np.array_equal(render(pet), frozen), stem + ": drag eyes/expression jump")
                mouse(pet, QEvent.Type.MouseButtonRelease, destination)
                pet._advance_eye_tracking(2, QPointF(100000, -100000))
                require(pet.animation.state == AnimationState.RETURNING and tracker.gaze == frozen_gaze, stem + ": return did not freeze eyes")
                pet.animation.advance(2); pet.frame_tick.emit(2)
                require(controller.expression == "happy" and not pet.eye_tracking_active, stem + ": deferred happy lost after drag")
                activate(controller, code)
                pet.set_frame_sequence((QImage(str(EXPRESSIONS / (stem + ".png"))), QImage(str(EXPRESSIONS / "happy.png"))))
                before = render(pet)
                pet._advance_eye_tracking(2, QPointF(-10000, 10000))
                require(not pet.eye_tracking_active and np.array_equal(render(pet), before), stem + ": sequence did not bypass rig")
                pet.set_frame_index(1)
                require(not np.array_equal(render(pet), before), stem + ": sequence frame did not switch")
                pet.clear_frame_sequence()
                require(pet.eye_tracking_active and tracker.rig_name == stem, stem + ": sequence clear lost rig")
            report["drag_return_and_sequence_policy_all_six"] = True

            closed = QPixmap(str(EXPRESSIONS / "talk_closed.png"))
            pet.set_character_source(closed)
            require(pet.eye_tracking_active and tracker.rig_name == "neutral" and tracker.rig is tracker.rigs["neutral"], "Closed-mouth pixel alias did not reuse neutral rig")
            report["talk_closed_reuses_neutral_by_pixels"] = True

            # Corrupt one optional rig in a temporary tree; five others survive.
            broken = directory / "broken-assets"
            shutil.copytree(EYES, broken / "eyes")
            shutil.copytree(EXPRESSIONS, broken / "expressions")
            (broken / "eyes" / "sleepy" / "rig.json").write_text("{invalid json", encoding="utf-8")
            with patch("desktop_pet.eye_tracking.DEFAULT_RIG_PATH", broken / "eyes" / "neutral" / "rig.json"), \
                    patch("desktop_pet.eye_tracking.DEFAULT_NEUTRAL_PATH", broken / "expressions" / "neutral.png"):
                damaged_tracker = EyeTracker()
            require(set(damaged_tracker.rigs) == set(TRACKED) - {"sleepy"}, "One corrupt rig disabled healthy rigs")
            pet._eye_tracking = damaged_tracker
            for code, stem, _label in STATES:
                activate(controller, code)
                expected = stem not in {"happy", "sleepy"}
                require(pet.eye_tracking_active == expected, "Bad-rig fallback isolation failed: " + stem)
                if stem == "sleepy": require(np.array_equal(render(pet), canonical[stem]), "Bad rig did not preserve original sleepy art")
            report["corrupt_one_rig_isolated_and_static_fallback"] = True
    except Exception as exc:
        import traceback
        report["errors"].append(type(exc).__name__ + ": " + str(exc)); report["traceback"] = traceback.format_exc()
    finally:
        if pet is not None: pet.close(); pet.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
        report["immutable_originals"] = []
        for path, digest in {**initial, **root_protected, **all_art}.items():
            unchanged = (ROOT / path).exists() and sha(ROOT / path) == digest
            report["immutable_originals"].append({"path": path, "sha256": digest, "unchanged": unchanged})
            require(unchanged, "Artwork or historical neutral QA changed: " + path)
        report["protected_unique_file_count"] = len(report["immutable_originals"])
    report["status"] = "failed" if report["errors"] else "passed"
    path = WORK / "verification.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "report": str(path)}, ensure_ascii=True))
    return 1 if report["errors"] else 0


def native_main():
    from PySide6.QtTest import QTest
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    output = PREVIEW / "原生窗口"; output.mkdir(parents=True, exist_ok=True)
    original_pointer = QCursor.pos()
    report = {"checked_utc": datetime.now(timezone.utc).isoformat(), "status": "failed", "errors": [],
              "platform": "windows", "physical_cursor_moved": True, "production_settings_touched": False,
              "injected_gaze_coordinates": False, "existing_frame_timer_used": True, "states": []}
    def require(condition, message):
        if not condition: report["errors"].append(message)
    pet = None
    try:
        with tempfile.TemporaryDirectory(prefix="aura-all-gaze-native-") as temp:
            directory = Path(temp)
            settings = SettingsManager(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat), CredentialStore(backend=MemoryVault()))
            animation = AnimationConfig(idle_amplitude=0, breathing_amplitude=0, happy_amplitude=0, surprised_amplitude=0)
            pet = PetWindow(PetConfig(animation=animation)); pet.setWindowTitle("AURA · 全表情视线验收（临时）")
            controller = PetController(pet, settings, animation_directory=directory / "no-video-clips")
            area = app.primaryScreen().availableGeometry(); pet.move(area.center() - pet.rect().center())
            ticks = []; pet.frame_tick.connect(lambda dt: ticks.append(dt))
            pet.show(); QTest.qWait(200)
            report["device_pixel_ratio"] = pet.devicePixelRatioF(); report["timer_interval_ms"] = pet.refresh_timer.interval()
            require(pet.refresh_timer.isActive() and pet.refresh_timer.interval() <= 34, "30fps frame timer inactive")
            last_targets = []
            for code, stem, _label in STATES:
                if stem in {"neutral", "happy"}: continue
                activate(controller, code)
                require(pet.eye_tracking_active and pet._eye_tracking.rig_name == stem, stem + ": wrong native rig")
                result = {"code": code, "stem": stem, "directions": []}
                last_targets = []
                for name, x, y in (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1), ("center", 0, 0)):
                    rig = pet._eye_tracking.rig
                    point = rig.face_center + QPointF(x * rig.response_radius.x() * 1.5, y * rig.response_radius.y() * 1.5)
                    target = global_source(pet, point).toPoint()
                    target.setX(max(area.left() + 16, min(target.x(), area.right() - 16)))
                    target.setY(max(area.top() + 16, min(target.y(), area.bottom() - 16)))
                    count = len(ticks); QCursor.setPos(target); QTest.qWait(550)
                    gaze = tuple(pet._eye_tracking.gaze); sampled = QCursor.pos()
                    require((sampled - target).manhattanLength() <= 2 and len(ticks) - count >= 8, stem + ": native pointer/timer did not sample")
                    require((x == 0 or gaze[0] * x > .25) and (y == 0 or gaze[1] * y > .25), stem + ": native gaze wrong sign")
                    if name == "center": require(max(abs(v) for v in gaze) < .025, stem + ": native center did not settle")
                    path = output / (stem + "_" + name + ".png"); pet.grab().save(str(path), "PNG")
                    result["directions"].append({"name": name, "gaze": gaze, "timer_ticks": len(ticks) - count,
                                                 "cursor_logical": [sampled.x(), sampled.y()], "capture": str(path.relative_to(ROOT))})
                    last_targets.append(target)
                report["states"].append(result)
            activate(controller, "happy")
            before = rgba(pet.grab().toImage())
            for target in last_targets:
                QCursor.setPos(target); QTest.qWait(160)
                require(not pet.eye_tracking_active and np.array_equal(rgba(pet.grab().toImage()), before), "Closed happy eyes moved in native cursor test")
            report["happy_closed_eyes_static"] = True; report["total_timer_ticks"] = len(ticks)
    except Exception as exc:
        import traceback
        report["errors"].append(type(exc).__name__ + ": " + str(exc)); report["traceback"] = traceback.format_exc()
    finally:
        QCursor.setPos(original_pointer); report["pointer_restored"] = (QCursor.pos() - original_pointer).manhattanLength() <= 2
        require(report["pointer_restored"], "Original pointer was not restored")
        if pet is not None: pet.close(); pet.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
        report["temporary_window_closed"] = True
    report["status"] = "failed" if report["errors"] else "passed"
    path = WORK / "native_smoke.json"; path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "report": str(path)}, ensure_ascii=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(native_main() if NATIVE else main())
