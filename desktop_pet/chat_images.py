"""Public image-reference rules; no image bytes or local paths are archived."""

import re


IMAGE_ID = re.compile(r"[0-9a-f]{64}\Z")
MAX_CHAT_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_REFERENCE = re.compile(
    r"上图|上一张|图[中里]|(?:上(?:面)?(?:那|一)?张|[这那]张)(?:图(?:片|像)?|照片|截图)|"
    r"\b(?:(?:this|that|previous|last|above)\s+|(?:in|on)\s+the\s+)(?:image|photo|picture|screenshot|attachment)s?\b", re.I)
_COMPARISON_PREVIOUS = re.compile(
    r"上图|前图|(?:上(?:面)?|前(?:面)?|之前|刚才)(?:的|那|一)?张|"
    r"(?:这|那)?两(?:张|幅)(?:图片|图像|图(?!表|纸)|照片|截图)|"
    r"\b(?:previous|last|earlier|above)\s+(?:image|photo|picture|screenshot|attachment|one)s?\b|"
    r"\b(?:(?:these|the)\s+)?(?:two|2)\s+(?:image|photo|picture|screenshot|attachment)s\b|"
    r"\bboth\s+(?:images|photos|pictures|screenshots|attachments)\b", re.I)
_COMPARISON = re.compile(
    r"对比|比较|相比|比起|比一比|不同|区别|差异|变化|一样|相同|哪个.{0,6}(?:更|好|贵|便宜|清晰|清楚|合适)|"
    r"\b(?:compare(?:d|s)?|comparison|versus|vs|differ(?:ent|ences?|s)?|change(?:d|s)?|same|similar)\b", re.I)


def references_previous_image(text):
    return isinstance(text, str) and bool(IMAGE_REFERENCE.search(text))


def compares_previous_image(text):
    """A new attachment alone does not authorize loading old private pixels."""
    return isinstance(text, str) and bool(_COMPARISON_PREVIOUS.search(text) and _COMPARISON.search(text))


IMAGE_ERRORS = {
    "image_missing": "引用的图片不存在或已删除，请重新发送图片后重试。",
    "image_unavailable": "无法读取或校验引用的图片，请重新发送图片后重试。",
    "vision_rejected": "图文请求未被模型接口接受，请确认模型支持图片并检查接口配置。",
}
