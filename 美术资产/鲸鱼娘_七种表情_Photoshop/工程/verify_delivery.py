"""Read-only delivery checks; Pillow creates inspection contact sheets only.

Run with the repository .venv Python after the Photoshop exports are installed.
No delivered artwork, PSD, source image, or application code is modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

WORK = Path(__file__).resolve().parent
BATCH = WORK.parent
ROOT = BATCH.parent.parent
STATES = ("neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talk_open")
LABELS = ("正常 · neutral", "开心 · happy", "惊讶 · surprised", "困倦 · sleepy",
          "生气 · angry", "思考 · thinking", "说话 · talking")
SOURCE_BASELINES = {
    "assets/whale.png": "79450abd5d7201fcb7c8e7a5a668bd5550342658d27f49e3f26d0e4235ff801a",
    "assets/character/source/master-white-background-v2.png": "0160d77f0cdce6f5f170ae7ee209430277e272ab3fb1062f8e1549b10abb2738",
    "assets/character/master.png": "14eb9daf26d9de51f39f75bcd103ed7f40ed087c1ce53596f409d030ae5760bb",
}
RAW_BASELINES = {
    "neutral": "0443afb8b3ebd771526efa329e045397f3bafb6f7117e7a65abf6ed669dad679",
    "happy": "943309fd45fc44fc6ac634a7c42d610f82e1eba870365d51f5ef047b14c52457",
    "surprised": "9b5a32633f3e48c929ab4651a36d9fdba82ec9336a49696cfa6c62f0ce5ef690",
    "sleepy": "dcaddb4eed34acaa975712ed7e664a3de532833e040d7f036a515d1f81994488",
    "angry": "6ceaf9ae9219b0b7ac45d29a96ac0c65d2362d79964d98c445a6dbbb2173a156",
    "thinking": "a9799d990922cd8cd91456f0423b9cc47d3b867e12bfa19c00511e456952e7e9",
    "talk_open": "f07433d2b8660eee12c9a600b1f008e77189fa1f0d9ec780b3e968b93eae31ce",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def components(mask):
    """Eight-connected scanline union without an additional dependency."""
    parents, spans, previous = [], [], []

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for y, row in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.r_[False, row, False].astype(np.int8)))
        current = []
        j = 0
        for left, right in zip(edges[::2], edges[1::2]):
            label = len(parents)
            parents.append(label)
            left, right = int(left), int(right)
            while j < len(previous) and previous[j][1] < left:
                j += 1
            k = j
            while k < len(previous) and previous[k][0] <= right:
                parents[find(previous[k][2])] = find(label)
                k += 1
            current.append((left, right, label))
            spans.append((label, left, y, right))
        previous = current
    groups = {}
    for label, left, y, right in spans:
        key = find(label)
        if key not in groups:
            groups[key] = {"pixels": 0, "bounds": [left, y, right, y + 1]}
        item = groups[key]
        item["pixels"] += right - left
        b = item["bounds"]
        item["bounds"] = [min(b[0], left), min(b[1], y), max(b[2], right), max(b[3], y + 1)]
    return sorted(groups.values(), key=lambda item: item["pixels"], reverse=True)


def inspect_png(path):
    with Image.open(path) as image:
        image.load()
        rgba = np.array(image.convert("RGBA"))
        alpha = rgba[:, :, 3]
        bounds = image.convert("RGBA").getchannel("A").getbbox()
        groups = components(alpha > 8)
        all_alpha_groups = components(alpha > 0)
        nonzero = np.count_nonzero(alpha)
        return {
            "path": str(path.relative_to(ROOT)), "sha256": sha256(path),
            "mode": image.mode, "size": list(image.size), "alpha_bounds": bounds,
            "alpha_extrema": [int(alpha.min()), int(alpha.max())],
            "transparent_pixels": int(np.count_nonzero(alpha == 0)),
            "transparent_fraction": float(np.mean(alpha == 0)),
            "partial_alpha_pixels": int(np.count_nonzero((alpha > 0) & (alpha < 255))),
            "nonzero_alpha_pixels": int(nonzero),
            "component_count_all_nonzero_alpha": len(all_alpha_groups),
            "detached_pixels_all_nonzero_alpha": sum(g["pixels"] for g in all_alpha_groups[1:]),
            "border_alpha_clear": not bool(np.any(alpha[0]) or np.any(alpha[-1]) or np.any(alpha[:, 0]) or np.any(alpha[:, -1])),
            "components_alpha_above_8": groups[:30], "component_count_alpha_above_8": len(groups),
            "detached_pixels_alpha_above_8": sum(g["pixels"] for g in groups[1:]),
            "significant_detached_components": [g for g in groups[1:] if g["pixels"] > 16],
        }


def inspect_psd(path):
    """Inspect PSD v1 header, layer visibility, raster channels and native masks."""
    data = path.read_bytes()
    offset = 0

    def take(n):
        nonlocal offset
        value = data[offset:offset + n]
        if len(value) != n:
            raise ValueError("Truncated PSD")
        offset += n
        return value

    def u32():
        return struct.unpack(">I", take(4))[0]

    signature = take(4)
    version = struct.unpack(">H", take(2))[0]
    if signature != b"8BPS" or version != 1:
        raise ValueError("Expected ordinary PSD v1")
    take(6)
    channels, height, width, depth, mode = struct.unpack(">HIIHH", take(14))
    take(u32())  # color mode data
    take(u32())  # image resources
    layer_mask_length = u32()
    layer_info_length = u32()
    count = struct.unpack(">h", take(2))[0]
    layers = []
    for _ in range(abs(count)):
        top, left, bottom, right = struct.unpack(">iiii", take(16))
        channel_count = struct.unpack(">H", take(2))[0]
        layer_channels = []
        for _channel in range(channel_count):
            channel_id, length = struct.unpack(">hI", take(6))
            layer_channels.append({"id": channel_id, "bytes": length})
        blend_signature, blend_mode = take(4), take(4)
        opacity, clipping, flags, _padding = take(4)
        extra_length = u32()
        extra_end = offset + extra_length
        mask_length = u32()
        mask_data = take(mask_length)
        blend_ranges_length = u32()
        take(blend_ranges_length)
        name_length = take(1)[0]
        name = take(name_length).decode("mac_roman", "replace")
        take((-(name_length + 1)) % 4)
        offset = extra_end
        mask_bounds = struct.unpack(">iiii", mask_data[:16]) if mask_length >= 16 else None
        layers.append({
            "name": name, "visible": not bool(flags & 2), "opacity": opacity,
            "bounds": [left, top, right, bottom], "channels": layer_channels,
            "mask_bytes": mask_length,
            "mask_bounds": [mask_bounds[1], mask_bounds[0], mask_bounds[3], mask_bounds[2]] if mask_bounds else None,
            "blend_mode": blend_mode.decode("ascii", "replace"),
        })
    return {
        "path": str(path.relative_to(ROOT)), "sha256": sha256(path), "version": version,
        "size": [width, height], "channels": channels, "depth": depth, "color_mode": mode,
        "layers": layers, "layer_info_bytes": layer_info_length,
        "layer_mask_section_bytes": layer_mask_length,
        "contains_visible_editable_mask": any(l["visible"] and l["mask_bytes"] > 0 and any(c["id"] in (-2, -3) for c in l["channels"]) for l in layers),
        "contains_hidden_original": any(not l["visible"] and "Raw original" in l["name"] for l in layers),
    }


def inspect_qt():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(ROOT))
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QApplication
    from desktop_pet.expression_manager import ExpressionManager

    app = QApplication.instance() or QApplication([])
    manager = ExpressionManager()
    result = []
    for code, state in zip(("neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talking", "talking.closed"), STATES + ("talk_closed",)):
        installed = ROOT / "assets" / "expressions" / (state + ".png")
        expression = manager.resolve(code) if state != "talk_closed" else None
        expected = np.array(Image.open(BATCH / "透明PNG" / (state + ".png")).convert("RGBA"))
        pixmap = expression.pixmap if expression is not None else QPixmap(str(installed))
        status = expression.status if expression is not None else "Qt direct load of optional closed-mouth talking sprite"
        if pixmap is None or pixmap.isNull():
            result.append({"code": code, "loaded": False, "status": status})
            continue
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        packed = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes())
        actual = packed.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(image.height(), image.width(), 4)
        # Qt QPixmap uses premultiplied pixels internally; integer round trips can
        # alter low-alpha RGB. Compare against the same Qt load of final input.
        expected_qt = QPixmap.fromImage(QImage(str(BATCH / "透明PNG" / (state + ".png")))).toImage()
        expected_qt = expected_qt.convertToFormat(QImage.Format.Format_RGBA8888)
        expected_packed = np.frombuffer(expected_qt.constBits(), dtype=np.uint8, count=expected_qt.sizeInBytes())
        expected_actual = expected_packed.reshape(expected_qt.height(), expected_qt.bytesPerLine())[:, :expected_qt.width() * 4].reshape(expected_qt.height(), expected_qt.width(), 4)
        result.append({"code": code, "loaded": True, "status": status,
                       "size": [image.width(), image.height()], "alpha_channel": image.hasAlphaChannel(),
                       "installed_sha256_matches_final": installed.exists() and sha256(installed) == sha256(BATCH / "透明PNG" / (state + ".png")),
                       "qt_pixels_match_final_input": bool(np.array_equal(actual, expected_actual)),
                       "alpha_pixels_match_final_input": bool(np.array_equal(actual[:, :, 3], expected[:, :, 3]))})
    app.processEvents()
    return result


def contact_sheet():
    """Create a clearly labelled QA composition, never a replacement sprite."""
    cell_w, cell_h, margin, header = 300, 500, 20, 82
    sheet = Image.new("RGB", (cell_w * 7 + margin * 2, cell_h * 2 + header + margin), "#111722")
    draw = ImageDraw.Draw(sheet)
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default()
    small = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    draw.text((margin, 15), "鲸鱼娘 · 七种表情 / Photoshop 透明 PNG 检查", fill="white", font=font)
    draw.text((margin, 48), "仅用于检查透明边缘与表情：深灰 / 粉色背景", fill="#B2BAC9", font=small)
    for row, color in enumerate(("#252B39", "#D794AC")):
        for col, (state, label) in enumerate(zip(STATES, LABELS)):
            x, y = margin + col * cell_w, header + row * cell_h
            draw.rectangle((x, y, x + cell_w - 6, y + cell_h - 6), fill=color)
            image = Image.open(BATCH / "透明PNG" / (state + ".png")).convert("RGBA")
            # The inspection crop and thumbnail exist only inside this QA sheet.
            bounds = image.getchannel("A").getbbox()
            image = image.crop((bounds[0] - 12, bounds[1] - 12, bounds[2] + 12, bounds[3] + 12))
            image.thumbnail((cell_w - 24, cell_h - 62), Image.Resampling.LANCZOS)
            sheet.paste(image, (x + (cell_w - 6 - image.width) // 2, y + 18), image)
            draw.text((x + 12, y + cell_h - 43), label, fill="white" if row == 0 else "#25172A", font=small)
    out = BATCH / "预览" / "七种表情总览.png"
    out.parent.mkdir(exist_ok=True)
    sheet.save(out)
    return str(out.relative_to(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-only", action="store_true", help="Read-only early source inspection")
    parser.add_argument("--skip-qt", action="store_true", help="Inspect Photoshop export before installation")
    args = parser.parse_args()
    report = {"checked_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "raw sources only" if args.raw_only else ("Photoshop exports only" if args.skip_qt else "full delivery including installed Qt expressions"),
              "status": "passed", "errors": [], "sources": [], "raw": [], "png": [], "psd": []}

    def require(condition, message):
        if not condition:
            report["errors"].append(message)

    for rel, baseline in SOURCE_BASELINES.items():
        path = ROOT / rel
        actual = sha256(path) if path.exists() else None
        report["sources"].append({"path": rel, "sha256": actual, "baseline": baseline, "unchanged": actual == baseline})
        require(actual == baseline, "Original source changed: " + rel)
    for state in STATES + (() if args.raw_only else ("talk_closed",)):
        if state in RAW_BASELINES:
            path = BATCH / "原始生图" / (state + ".png")
            raw = inspect_png(path)
            raw["baseline_sha256"] = RAW_BASELINES[state]
            raw["unchanged"] = raw["sha256"] == RAW_BASELINES[state]
            report["raw"].append(raw)
            require(raw["unchanged"], "Generated raw source changed: " + state)
        if args.raw_only:
            continue
        try:
            png = inspect_png(BATCH / "透明PNG" / (state + ".png"))
            report["png"].append(png)
            require(png["size"] == [1024, 1024], state + ": expected 1024 canvas")
            require(png["mode"] == "RGBA" and png["alpha_extrema"] == [0, 255] and png["transparent_fraction"] > 0.5, state + ": expected real transparency")
            require(png["border_alpha_clear"], state + ": alpha touches canvas border")
            require(png["component_count_all_nonzero_alpha"] == 1, state + ": detached alpha specks")
            require(not png["significant_detached_components"], state + ": significant detached alpha components")
            bounds = png["alpha_bounds"]
            require(bounds is not None and abs(bounds[3] - bounds[1] - 666) <= 4 and abs(bounds[3] - 960) <= 3 and abs((bounds[0] + bounds[2]) / 2 - 512) <= 3, state + ": unexpected normalized subject bounds")
            psd = inspect_psd(BATCH / "PSD" / (state + ".psd"))
            report["psd"].append(psd)
            require(psd["size"] == [1024, 1024] and psd["depth"] == 8 and psd["color_mode"] == 3, state + ": invalid PSD canvas/mode")
            require(psd["contains_visible_editable_mask"] and psd["contains_hidden_original"], state + ": PSD missing editable mask or preserved original layer")
        except (FileNotFoundError, ValueError, OSError) as exc:
            report["errors"].append(state + ": " + str(exc))
    if not args.raw_only:
        if not args.skip_qt and not report["errors"]:
            report["qt_expressions"] = inspect_qt()
            for check in report["qt_expressions"]:
                require(check.get("loaded") and check.get("installed_sha256_matches_final") and check.get("qt_pixels_match_final_input") and check.get("alpha_pixels_match_final_input"), "Qt expression check failed: " + check["code"])
        if len(report["png"]) == 8:
            report["talk_closed_alias"] = {"source_state": "neutral", "identical_rgba_pixels": bool(np.array_equal(
                np.array(Image.open(BATCH / "透明PNG" / "neutral.png").convert("RGBA")),
                np.array(Image.open(BATCH / "透明PNG" / "talk_closed.png").convert("RGBA"))))}
            require(report["talk_closed_alias"]["identical_rgba_pixels"], "Closed-mouth talking alias differs from neutral pixels")
            report["inspection_contact_sheet"] = contact_sheet()
    report["status"] = "failed" if report["errors"] else "passed"
    destination = WORK / "verification.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "report": str(destination)}, ensure_ascii=True))
    if args.raw_only:
        for item in report["raw"]:
            print(json.dumps({"file": Path(item["path"]).name, "bounds": item["alpha_bounds"], "detached_pixels": item["detached_pixels_alpha_above_8"], "components": item["components_alpha_above_8"][:10]}, ensure_ascii=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
