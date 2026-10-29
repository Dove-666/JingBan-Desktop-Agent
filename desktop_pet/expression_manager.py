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
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or PROJECT_ROOT / "assets" / "expressions"
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

