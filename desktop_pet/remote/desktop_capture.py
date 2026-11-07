"""One explicit, consent-gated foreground capture; no automatic observation."""

import base64

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter

from .errors import RemoteError


class DesktopCaptureUnavailable(RemoteError):
    """A private, observer-generated reason; never derived from RPC fields."""

    def __init__(self, blocked_code=""):
        self.blocked_code = "own_window" if blocked_code == "own_window" else ""
        message = ("前台是鲸伴自身窗口，未读取画面。可发送明确的电脑任务，或先切换到需要查看的窗口后重新获取。"
                   if self.blocked_code == "own_window" else
                   "当前窗口因隐私过滤、窗口变化或截图限制无法读取，请在电脑端确认前台窗口后重试。")
        super().__init__("desktop_capture_unavailable", message)


def capture_desktop_image(observer, settings):
    """Worker boundary, injectable in isolated QA; never logs foreground data."""
    observe = getattr(observer, "observe_explicit", None)
    observation = (observe if callable(observe) else observer.observe)(settings)
    image = observation.image
    if observation.blocked_reason or not isinstance(image, QImage) or image.isNull():
        raise DesktopCaptureUnavailable(getattr(observation, "blocked_code", ""))
    if max(image.width(), image.height()) > 640:
        image = image.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    clean = QImage(image.size(), QImage.Format.Format_RGBA8888)
    clean.fill(0)
    painter = QPainter(clean)
    painter.drawImage(0, 0, image)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not clean.save(buffer, "PNG") or data.size() > 2 * 1024 * 1024:
        raise RemoteError("desktop_capture_unavailable", "电脑画面暂不可用，请在电脑端确认后重试。")
    is_current = getattr(observer, "is_current_explicit", None)
    if not (is_current if callable(is_current) else observer.is_current)(observation, settings):
        raise RemoteError("desktop_capture_unavailable", "截图期间前台窗口发生变化，画面已丢弃，请重新获取。")
    return {"filename": "desktop-snapshot.png", "mime_type": "image/png",
            "data_base64": base64.b64encode(bytes(data)).decode("ascii")}
