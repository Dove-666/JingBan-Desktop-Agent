"""Opt-in vision check using the existing desktop provider and a public test drawing."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from PySide6.QtCore import QCoreApplication
from desktop_pet.api_client import ApiClient, ApiError
from desktop_pet.settings_manager import SettingsManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-current-provider", action="store_true",
                        help="Make one bounded image request using the configured desktop credential")
    args = parser.parse_args()
    if not args.run_current_provider:
        parser.print_help()
        return 0
    application = QCoreApplication.instance() or QCoreApplication([])
    application.setApplicationName("AURA Desktop Pet")
    settings = SettingsManager()
    provider = settings.load_api_settings()
    provider.validate()
    key = settings.get_api_key()
    if not key:
        print(json.dumps({"passed": False, "reason": "desktop_credential_unavailable"}))
        return 1
    folder = ROOT / "artifacts/android/qa/vision-chat"
    folder.mkdir(parents=True, exist_ok=True)
    drawing = Image.new("RGB", (320, 240), "white")
    pen = ImageDraw.Draw(drawing)
    pen.polygon([(30, 200), (100, 35), (165, 200)], fill=(235, 35, 35))
    pen.ellipse((190, 55, 305, 175), fill=(30, 80, 235))
    output = io.BytesIO()
    drawing.save(output, format="PNG")
    raw = output.getvalue()
    (folder / "vision-test.png").write_bytes(raw)
    messages = [
        {"role": "system", "content": "请根据图片回答，使用中文。返回JSON：{\"reply\":\"简短图片描述\",\"emotion\":\"neutral\"}。"},
        {"role": "user", "content": [
            {"type": "text", "text": "只根据这张图片，说出左右两个图形的颜色和形状。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}},
        ]},
    ]
    report = {"configured_model": provider.model, "provider_host": urlsplit(provider.base_url).hostname,
              "real_provider": True, "synthetic_public_image_only": True,
              "user_photo_sent": False, "user_settings_changed": False,
              "image_sha256": hashlib.sha256(raw).hexdigest()}
    client = ApiClient()
    try:
        reply = client.complete_context(provider, key, messages, output_tokens=1024)
        text = reply.reply.lower()
        checks = {
            "red": any(word in text for word in ("红", "紅", "red")),
            "blue": any(word in text for word in ("蓝", "藍", "blue")),
            "triangle": any(word in text for word in ("三角", "triangle")),
            "circle": any(word in text for word in ("圆", "圓", "circle")),
        }
        report.update(passed=all(checks.values()), recognition_checks=checks, reply=reply.reply)
    except ApiError as error:
        report.update(passed=False, error_code=error.code, error_message=str(error))
    except Exception:
        report.update(passed=False, error_code="smoke_failed")
    finally:
        key = ""
        client.shutdown()
        drawing.close()
    (folder / "real-provider-vision-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
