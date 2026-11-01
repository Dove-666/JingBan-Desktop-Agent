"""Shareable, read-only local diagnostics without loading the application.

No settings, credentials, conversations, screenshots, memory stores or model
weights are read. Optional SDKs are inspected through package metadata only.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import platform
import struct
import sys
import zlib


REQUIRED_PACKAGES = (("PySide6", "PySide6"), ("openai", "openai"), ("keyring", "keyring"))
OPTIONAL_PACKAGES = (("mem0ai", "mem0"), ("qdrant-client", "qdrant_client"),
                     ("fastembed", "fastembed"), ("onnxruntime", "onnxruntime"))
DEFAULT_IMAGE = Path(__file__).resolve().parents[1] / "assets" / "whale.png"


def _dependency(package, module, finder, version_getter):
    try:
        available = finder(module) is not None
    except Exception:
        available = False
    try:
        version = version_getter(package) if available else None
    except Exception:
        version = None
    return {"package": package, "module": module, "available": available, "version": version}


def _png_header(image_path: Path) -> dict:
    result = {"asset": "character_png", "exists": False, "format": None, "width": None,
              "height": None, "alpha_channel": None, "readable": None, "validation": "not_checked"}
    try:
        result["exists"] = image_path.is_file()
        if not result["exists"]:
            result["validation"] = "missing"
            return result
        with image_path.open("rb") as source:
            header = source.read(33)
        if (len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n"
                or header[8:16] != b"\x00\x00\x00\rIHDR"
                or zlib.crc32(header[12:29]) & 0xFFFFFFFF != struct.unpack(">I", header[29:33])[0]):
            result["validation"] = "invalid_png"
            return result
        width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header[16:29])
        valid_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
        if (width < 1 or height < 1 or width * height > 64_000_000 or color not in valid_depths
                or depth not in valid_depths[color] or compression != 0 or filtering != 0 or interlace not in (0, 1)):
            result["validation"] = "invalid_png"
            return result
        result.update(format="PNG", width=width, height=height,
                      alpha_channel=True if color in (4, 6) else None, validation="header_only")
    except (OSError, ValueError):
        result["validation"] = "unreadable"
    return result


def _probe_qt(image_path: Path) -> dict:
    # QImageReader requires neither QApplication nor an event loop. Importing
    # optional memory/model packages is intentionally never part of this probe.
    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QImageReader

    reader = QImageReader(str(image_path))
    reader.setDecideFormatFromContent(True)
    # Qt resets the handler after read(); ask its detected format beforehand.
    image_format = bytes(reader.format()).decode("ascii", errors="ignore").upper()
    image = reader.read()
    readable = not image.isNull()
    return {"qt_version": qVersion(), "readable": readable,
            "format": image_format,
            "width": image.width() if readable else None,
            "height": image.height() if readable else None,
            "alpha_channel": image.hasAlphaChannel() if readable else None}


def collect_diagnostics(*, image_path=None, module_finder=None, version_getter=None, qt_probe=None) -> dict:
    """Inspect local prerequisites; dependency hooks keep missing-SDK tests safe."""
    finder = module_finder or importlib.util.find_spec
    versions = version_getter or importlib.metadata.version
    required = [_dependency(package, module, finder, versions) for package, module in REQUIRED_PACKAGES]
    optional = [_dependency(package, module, finder, versions) for package, module in OPTIONAL_PACKAGES]
    image_path = Path(image_path) if image_path is not None else DEFAULT_IMAGE
    image = _png_header(image_path)
    errors, warnings, recommendations = [], [], []
    missing = [item["package"] for item in required if not item["available"]]
    if missing:
        errors.append("缺少必需依赖：" + "、".join(missing) + "。")
        recommendations.append("使用项目环境安装：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    bits = struct.calcsize("P") * 8
    if sys.version_info < (3, 10):
        errors.append("Python 版本过低，请使用 Python 3.10 或更新的受支持版本。")
    if bits != 64:
        errors.append("当前不是 64 位 Python，请改用 64 位解释器安装桌宠依赖。")
    if platform.system() != "Windows":
        warnings.append("当前不是 Windows；窗口与前台应用等 Windows 专属功能未验证。")
    qt_version = None
    if image["validation"] == "header_only" and required[0]["available"]:
        try:
            probe = (qt_probe or _probe_qt)(image_path)
            qt_version = probe["qt_version"]
            if probe["readable"] and probe["format"] == "PNG":
                image.update({key: probe[key] for key in ("readable", "format", "width", "height", "alpha_channel")})
                image["validation"] = "decoded"
            else:
                image.update(readable=False, validation="decode_failed")
        except Exception:
            errors.append("Qt 图像组件无法加载，请检查 PySide6 安装和系统运行库。")
    if image["validation"] in ("missing", "unreadable", "invalid_png", "decode_failed"):
        errors.append("角色 PNG 缺失、损坏或无法解码，请恢复原始 assets/whale.png。")
    elif image["validation"] == "header_only":
        warnings.append("PNG 文件头有效，但未完成 Qt 图像解码；不能据此判断图片可正常显示。")
    elif image["alpha_channel"] is False:
        warnings.append("角色 PNG 没有 Alpha 通道，透明背景效果可能不完整。")
    if not all(item["available"] for item in optional[:2]):
        warnings.append("可选 Mem0 组件不完整；桌宠和普通聊天仍可使用。")
        recommendations.append("需要长期记忆时安装：.venv\\Scripts\\python.exe -m pip install -r requirements-memory.txt")
    if not all(item["available"] for item in optional[2:]):
        warnings.append("可选本地 Embedding 组件不完整；这不代表远程 Embedding 或桌宠启动失败。")
        recommendations.append("需要本地向量模型时安装：.venv\\Scripts\\python.exe -m pip install -r requirements-memory-local.txt")
    return {
        "schema": "aura.local_diagnostics", "version": 1, "required_ok": not errors,
        "status": "ok" if not errors else "required_issue",
        "summary": "本地基础检查通过；尚未实际启动桌宠或测试连接。" if not errors else "发现必需项目问题，请按建议修复后重新诊断。",
        "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                    "architecture_bits": bits, "system": platform.system(), "qt": qt_version},
        "dependencies": {"required": required, "optional_memory": optional}, "character_image": image,
        "errors": errors, "warnings": warnings, "recommendations": recommendations,
        "privacy": {"settings_read": False, "credentials_read": False, "chat_history_read": False,
                    "screen_captured": False, "network_requested": False, "runtime_store_initialized": False,
                    "model_loaded": False, "user_paths_included": False},
    }


def format_summary(report: dict) -> str:
    runtime, image = report["runtime"], report["character_image"]
    lines = ["AURA 本地诊断：" + report["summary"],
             f"Python {runtime['python']} · {runtime['architecture_bits']} 位 · {runtime['system']}",
             "Qt：" + (runtime["qt"] or "未完成加载验证")]
    if image["validation"] == "decoded":
        lines.append(f"角色 PNG：可解码，{image['width']} × {image['height']}，Alpha 通道：" + ("有" if image["alpha_channel"] else "无"))
    else:
        lines.append("角色 PNG：" + image["validation"] + "（仅报告实际完成的检查）")
    for scope, label in (("required", "必需依赖"), ("optional_memory", "可选记忆组件")):
        lines.append(label + "：")
        lines.extend("  " + item["package"] + "：" + ((item["version"] or "模块存在，版本未知")
                     if item["available"] else "未找到") for item in report["dependencies"][scope])
    lines.extend("问题：" + message for message in report["errors"])
    lines.extend("提示：" + message for message in report["warnings"])
    lines.extend("建议：" + message for message in report["recommendations"])
    lines.append("本次未读取配置、凭据、聊天或屏幕；未联网、初始化存储或加载模型。")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读本地诊断；不读取凭据/聊天，不联网或启动桌宠。")
    parser.add_argument("--json", action="store_true", help="输出可分享的 JSON；默认不包含用户路径或服务地址")
    parser.add_argument("--image", type=Path, help="检查指定 PNG（报告不包含所选路径）；默认 assets/whale.png")
    arguments = parser.parse_args(argv)
    report = collect_diagnostics(image_path=arguments.image)
    print(json.dumps(report, ensure_ascii=False, indent=2) if arguments.json else format_summary(report))
    return 0 if report["required_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
