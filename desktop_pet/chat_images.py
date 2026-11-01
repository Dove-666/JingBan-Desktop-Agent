"""Public image-reference rules; no image bytes or local paths are archived."""

import re


IMAGE_ID = re.compile(r"[0-9a-f]{64}\Z")
MAX_CHAT_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_REFERENCE = re.compile(r"图片|照片|图像|截图|图中|图里|上图|这张图|那张图|这幅图|上张图|上一张|附件|上面.{0,8}图|\b(?:image|photo|picture|screenshot|attachment)s?\b", re.I)


def references_previous_image(text):
    return isinstance(text, str) and bool(IMAGE_REFERENCE.search(text))


IMAGE_ERRORS = {
    "image_missing": "引用的图片不存在或已删除，请重新发送图片后重试。",
    "image_unavailable": "无法读取或校验引用的图片，请重新发送图片后重试。",
    "vision_rejected": "图文请求未被模型接口接受，请确认模型支持图片并检查接口配置。",
}
