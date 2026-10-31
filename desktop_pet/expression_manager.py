"""Resolve optional expression art without changing or regenerating the original."""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImageReader, QPixmap

from .config import PROJECT_ROOT


EXPRESSIONS = {
    "neutral": ("正常", ("neutral.png",)),
    "happy": ("开心", ("happy.png",)),
    "surprised": ("惊讶", ("surprised.png",)),
    "sleepy": ("困倦", ("sleepy.png",)),
    "angry": ("生气", ("angry.png",)),
    "thinking": ("思考", ("thinking.png",)),
    "talking": ("说话", ("talk_open.png", "talk_closed.png")),
}


@dataclass(frozen=True)
class Expression:
    code: str
    label: str
    pixmap: QPixmap | None
    status: str


class ExpressionManager:
    def __init__(self, directory: Path | None = None, *, character_directory: Path | None = None) -> None:
        self.directory = directory or PROJECT_ROOT / "assets" / "expressions"
        self.character_directory = character_directory or PROJECT_ROOT / "assets" / "character"
        self._cache: dict[Path, tuple[tuple[int, int], QPixmap]] = {}

    def resolve(self, code: str) -> Expression:
        if code not in EXPRESSIONS:
            raise ValueError("未知表情状态。")
        label, filenames = EXPRESSIONS[code]
        invalid = False
        for filename in filenames:
            path = self.directory / filename
            try:
                stat = path.stat()
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                cached = self._cache.get(path)
                if cached is not None and cached[0] == fingerprint:
                    pixmap = cached[1]
                else:
                    reader = QImageReader(str(path))
                    reader.setAutoTransform(True)
                    image = reader.read()
                    if image.isNull():
                        invalid = True
                        continue
                    pixmap = QPixmap.fromImage(image)
                    self._cache[path] = (fingerprint, pixmap)
                extra = "（静态素材）" if code == "talking" else ""
                return Expression(code, label, pixmap, f"已加载 {filename}{extra}。")
            except FileNotFoundError:
                continue
            except OSError:
                invalid = True
        reason = "素材无法读取" if invalid else "表情素材尚未提供"
        feedback = "原始角色图片" if code == "neutral" else "原图与轻量动作或气泡反馈"
        return Expression(code, label, None, f"{reason}：{' / '.join(filenames)}；使用{feedback}，未改变面部表情。")

    def catalog(self) -> list[tuple[str, str, str]]:
        return [(code, EXPRESSIONS[code][0], self.resolve(code).status) for code in EXPRESSIONS]

    def missing_assets(self) -> list[str]:
        return [filename for _, names in EXPRESSIONS.values() for filename in names
                if not (self.directory / filename).is_file()]

    def resolve_static(self, code: str) -> Expression:
        """An appearance choice is separate from the seven API emotion values."""
        if code not in {"original", "tsundere"}:
            raise ValueError("未知静态角色素材。")
        original = code == "original"
        label = "原始鲸鱼娘" if original else "傲娇 · 静态"
        path = (self.character_directory / "master.png" if original
                else self.character_directory / "static" / "tsundere.png")
        try:
            stat = path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            cached = self._cache.get(path)
            if cached is not None and cached[0] == fingerprint:
                pixmap = cached[1]
            else:
                reader = QImageReader(str(path), b"png")
                image = reader.read()
                if image.isNull() or not image.hasAlphaChannel():
                    return Expression(code, label, None, "静态 PNG 无效或没有透明通道；原始模式将回退 whale.png，其他模式保留当前角色。")
                pixmap = QPixmap.fromImage(image)
                self._cache[path] = (fingerprint, pixmap)
            status = ("已加载统一 1024 画布的 master.png；原始 whale.png 保持不变。" if original else
                      "已加载傲娇静态角色 PNG；此素材不是视频动作，也不是新增 API 表情。")
            return Expression(code, label, pixmap, status)
        except FileNotFoundError:
            status = ("统一画布 master.png 缺失，安全回退原始 whale.png。" if original else
                      "傲娇静态素材尚未就绪：assets/character/static/tsundere.png。")
            return Expression(code, label, None, status)
        except OSError:
            return Expression(code, label, None, "无法读取静态 PNG；原始模式将回退 whale.png，其他模式保留当前角色。")

    def static_catalog(self) -> list[tuple[str, str, str]]:
        return [(code, item.label, item.status) for code in ("original", "tsundere")
                for item in (self.resolve_static(code),)]

