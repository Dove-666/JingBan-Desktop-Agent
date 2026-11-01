"""Authenticated direct-chat configuration and bounded exchange validation.

Configuration is an ephemeral RPC response only. Neither the credential nor the
system prompt belongs in the operation journal or conversation synchronization.
"""

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from ..api_client import MAX_REPLY_CHARACTERS, SYSTEM_PROMPT
from ..chat_images import IMAGE_ID
from ..settings_manager import CredentialsError, SettingsError
from .errors import RemoteError


def direct_chat_configuration(settings_manager) -> dict:
    """Read effective desktop settings on the Qt thread, without changing them."""
    unavailable = {"version": 1, "available": False}
    try:
        settings = settings_manager.load_api_settings()
        settings.validate(require_model=True)
        key = settings_manager.get_api_key()
        if (not isinstance(key, str) or not key.strip() or len(key) > 16384
                or any(ord(character) < 32 or ord(character) == 127 for character in key)):
            raise CredentialsError("Invalid credential")
        effective = {"version": 1, "available": True, "base_url": settings.base_url,
                     "model": settings.model, "api_key": key,
                     "timeout_seconds": settings.timeout_seconds,
                     "reasoning_effort": settings.reasoning_effort,
                     # The first system message used by the desktop chat's
                     # ContextAssembler, including its actual emotion policy.
                     "system_prompt": SYSTEM_PROMPT}
        encoded = json.dumps(effective, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (SettingsError, CredentialsError, ValueError, UnicodeError):
        effective = unavailable
        encoded = json.dumps(effective, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**effective, "revision": hashlib.sha256(encoded).hexdigest()}


def _uuid(value) -> str:
    try:
        if isinstance(value, str) and str(UUID(value)) == value:
            return value
    except (ValueError, AttributeError):
        pass
    raise RemoteError("invalid_request", "会话或消息 UUID 无效。")


def _timestamp(value) -> str:
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
        except (ValueError, OverflowError):
            pass
    raise RemoteError("invalid_request", "消息时间必须是带时区的 ISO 8601 时间。")


def normalize_import(params: dict) -> dict:
    """v1 imports exactly one complete user/assistant exchange, with no metadata."""
    if not isinstance(params, dict) or set(params) - {"conversation_id", "title", "messages"}:
        raise RemoteError("invalid_request", "对话回写参数无效。")
    conversation_id = _uuid(params.get("conversation_id"))
    messages = params.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise RemoteError("invalid_request", "一次回写必须包含一条用户消息和一条完整回复。")
    normalized = []
    for index, role in enumerate(("user", "assistant")):
        value = messages[index]
        allowed = {"id", "role", "content", "created_at", "image_id" if index == 0 else "reply_to"}
        if not isinstance(value, dict) or set(value) - allowed or value.get("role") != role:
            raise RemoteError("invalid_request", "回写消息的角色或字段无效。")
        content = value.get("content")
        maximum = 8000 if index == 0 else MAX_REPLY_CHARACTERS
        if not isinstance(content, str) or not content.strip() or len(content) > maximum or "\x00" in content:
            raise RemoteError("invalid_request", "回写消息文字为空或超过长度限制。")
        message = {"id": _uuid(value.get("id")), "role": role, "content": content,
                   "created_at": _timestamp(value.get("created_at"))}
        if index == 0 and value.get("image_id") is not None:
            image_id = value["image_id"]
            if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
                raise RemoteError("invalid_image", "图片标识无效，请先上传图片再回写对话。")
            message["image_id"] = image_id
        if index == 1:
            if value.get("reply_to", normalized[0]["id"]) != normalized[0]["id"]:
                raise RemoteError("invalid_request", "回复必须关联本次回写的用户消息。")
            if message["id"] == normalized[0]["id"] or message["created_at"] < normalized[0]["created_at"]:
                raise RemoteError("invalid_request", "回写消息标识或时间顺序无效。")
            message["reply_to"] = normalized[0]["id"]
        normalized.append(message)
    result = {"conversation_id": conversation_id, "messages": normalized}
    if "title" in params:
        title = params["title"]
        if (not isinstance(title, str) or not title.strip() or len(title) > 120
                or any(ord(character) < 32 for character in title)):
            raise RemoteError("invalid_request", "回写会话标题无效。")
        result["title"] = " ".join(title.split())
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError):
        raise RemoteError("invalid_request", "回写消息编码无效。") from None
    if len(encoded) > 128 * 1024:
        raise RemoteError("invalid_request", "一次回写对话不能超过 128 KiB。")
    return result
