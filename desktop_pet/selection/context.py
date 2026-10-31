"""Bounded plain-text snapshots supplied by the user's explicit selection.

This module never reads the clipboard, fetches a URL, opens a browser or calls
an API. A selection is reference material, not an instruction or a user fact.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit, urlunsplit


MAX_SELECTION_CHARACTERS = 8_000
MAX_PAGE_CHARACTERS = 16_000
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SelectionContextError(ValueError):
    """A safe, static validation error for the selection interface."""


def _plain(value: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SelectionContextError("选中内容资料格式无效，请重新选择后再试。")
    return _CONTROLS.sub("", value).strip()[:limit]


def _public_url(value: str) -> str:
    """Retain a useful page address without credentials or query tokens."""
    value = _plain(value, 2048)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port is not None:
            host += ":" + str(parsed.port)
        return urlunsplit((parsed.scheme.casefold(), host, parsed.path, "", ""))
    except (ValueError, UnicodeError):
        return ""


@dataclass(frozen=True)
class SelectionContext:
    text: str
    title: str = ""
    url: str = ""
    page_text: str = ""
    captured_at: str = ""
    app_name: str = ""
    text_truncated: bool = False
    page_text_truncated: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text_truncated, bool) or not isinstance(self.page_text_truncated, bool):
            raise SelectionContextError("选中内容资料格式无效，请重新选择后再试。")
        selected = _plain(self.text, MAX_SELECTION_CHARACTERS)
        page = _plain(self.page_text, MAX_PAGE_CHARACTERS)
        if not selected:
            raise SelectionContextError("没有获取到选中的文字，请先选择文字再问问鲸鱼娘。")
        object.__setattr__(self, "text_truncated", self.text_truncated or len(self.text) > MAX_SELECTION_CHARACTERS)
        object.__setattr__(self, "page_text_truncated", self.page_text_truncated or len(self.page_text) > MAX_PAGE_CHARACTERS)
        object.__setattr__(self, "text", selected)
        object.__setattr__(self, "page_text", page)
        object.__setattr__(self, "title", _plain(self.title, 300))
        object.__setattr__(self, "url", _public_url(self.url))
        object.__setattr__(self, "app_name", _plain(self.app_name, 100))
        if not isinstance(self.notes, (tuple, list)):
            raise SelectionContextError("选中内容资料格式无效，请重新选择后再试。")
        notes = tuple(note for note in (_plain(item, 200) for item in self.notes[:8]) if note)
        if not page and not notes:
            notes = ("仅获取选中文字，未读取页面正文。",)
        object.__setattr__(self, "notes", notes)
        captured = _plain(self.captured_at, 80)
        if captured:
            try:
                stamp = datetime.fromisoformat(captured.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError
                captured = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
            except ValueError:
                raise SelectionContextError("选中内容的时间格式无效，请重新选择后再试。") from None
        else:
            captured = datetime.now(timezone.utc).isoformat(timespec="seconds")
        object.__setattr__(self, "captured_at", captured)

    @classmethod
    def from_payload(cls, value: SelectionContext | dict, *, api_key: str = "") -> SelectionContext:
        if isinstance(value, cls):
            value = value.to_dict()
        if not isinstance(value, dict):
            raise SelectionContextError("选中内容资料格式无效，请重新选择后再试。")
        fields = cls.__dataclass_fields__
        payload = {name: value[name] for name in fields if name in value}
        if "text" not in payload:
            raise SelectionContextError("没有获取到选中的文字，请先选择文字再问问鲸鱼娘。")
        # Validate the accepted shallow schema before recursively redacting. This
        # also rejects a hostile cyclic/nested Python payload without recursion.
        if (any(name in payload and not isinstance(payload[name], str)
                for name in ("text", "title", "url", "page_text", "captured_at", "app_name"))
                or "notes" in payload and (not isinstance(payload["notes"], (list, tuple))
                                            or any(not isinstance(note, str) for note in payload["notes"]))):
            raise SelectionContextError("选中内容资料格式无效，请重新选择后再试。")
        if api_key:
            def redact(item):
                if isinstance(item, str):
                    return item.replace(api_key, "[已隐藏密钥]")
                if isinstance(item, (list, tuple)):
                    return [redact(child) for child in item]
                if isinstance(item, dict):
                    return {name: redact(child) for name, child in item.items()}
                return item
            payload = redact(payload)
        return cls(**payload)

    @classmethod
    def from_dict(cls, value: dict, *, api_key: str = "") -> SelectionContext:
        """Stable browser/IPC entry point; accepts only known plain-data fields."""
        return cls.from_payload(value, api_key=api_key)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        return payload
