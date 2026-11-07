"""Private paired-device role catalog and selection, independent of model APIs.

Read responses contain the role prompt only on the authenticated wire. Mutation
acknowledgements are deliberately small enough for the durable operation journal
and never include a card document, prompt, credential or native path.
"""

from __future__ import annotations

import hashlib
import json
import re

from ..api_client import redact_secret
from ..character_cards import CharacterCard, CharacterCardError
from ..character_library import CharacterLibraryError, character_card_digest
from ..character_prompts import character_system_prompt
from ..settings_manager import SettingsError
from .errors import RemoteError


_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _identity(value):
    if not isinstance(value, str) or value != "default" and not _ID.fullmatch(value):
        raise RemoteError("invalid_request", "角色标识无效，请重新选择。")
    return value


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RemoteError("invalid_request", "角色版本无效，请重新同步。")
    return value


def _persona(card):
    values = {"version": 1, "name": card.name if card is not None else "鲸鱼娘",
              "system_prompt": character_system_prompt(card)}
    return {**values, "revision": _hash(values)}


def _snapshot(settings):
    try:
        entries = settings.list_character_cards()
        card = settings.load_character_card()
        if card is not None and not isinstance(card, CharacterCard):
            raise CharacterCardError("Invalid card")
        persona = _persona(card)
        active_digest = character_card_digest(card) if card is not None else persona["revision"]
        active_id = "default" if card is None else next(
            (entry.id for entry in entries if entry.digest == active_digest), None)
        items = [{"id": entry.id, "label": entry.label, "name": entry.name, "digest": entry.digest}
                 for entry in entries]
        identity = {"version": 1, "items": items, "active_id": active_id,
                    "active_digest": active_digest, "persona_revision": persona["revision"]}
        return {"version": 1, "revision": _hash(identity), "items": items,
                "active": {"id": active_id, "name": persona["name"], "digest": active_digest,
                           "persona": persona}}
    except (SettingsError, CharacterLibraryError, CharacterCardError, ValueError, TypeError, UnicodeError):
        raise RemoteError("character_unavailable", "角色库暂时无法读取，请稍后重新同步。") from None


def _secrets(settings):
    getter = getattr(settings, "get_known_api_keys", None)
    try:
        values = getter() if callable(getter) else ()
        return tuple(value for value in values if isinstance(value, str) and value)
    except Exception:
        return ()


def _redacted(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = redact_secret(value, secret)
        return value
    if isinstance(value, dict):
        return {key: _redacted(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, secrets) for item in value]
    return value


def character_library_summary(settings):
    """Small optional config envelope; no prompts, labels or credentials."""
    state = _snapshot(settings)
    return {"version": 1, "revision": state["revision"], "active_id": state["active"]["id"]}


def list_characters(settings, params):
    if not isinstance(params, dict) or set(params) - {"offset", "limit", "revision"}:
        raise RemoteError("invalid_request", "角色库分页参数无效。")
    offset, limit = params.get("offset", 0), params.get("limit", _PAGE_SIZE)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise RemoteError("invalid_request", "角色库分页参数无效。")
    expected = _digest(params["revision"]) if "revision" in params else None
    state = _snapshot(settings)
    if expected is not None and expected != state["revision"]:
        raise RemoteError("character_conflict", "电脑角色库已更新，请重新同步后选择。")
    total = len(state["items"])
    if offset > total:
        raise RemoteError("invalid_request", "角色库分页位置无效，请重新同步。")
    end = min(offset + limit, total)
    return _redacted({"version": 1, "revision": state["revision"], "total": total,
                     "items": state["items"][offset:end],
                     "next_offset": end if end < total else None,
                     "active": state["active"]}, _secrets(settings))


def get_character(settings, params):
    if not isinstance(params, dict) or set(params) - {"id", "digest"} or "id" not in params:
        raise RemoteError("invalid_request", "角色读取参数无效。")
    identity = _identity(params["id"])
    expected = _digest(params["digest"]) if "digest" in params else None
    state = _snapshot(settings)
    if identity == "default":
        card, persona = None, _persona(None)
        entry = {"id": "default", "label": "鲸鱼娘", "name": "鲸鱼娘", "digest": persona["revision"]}
    else:
        entry = next((item for item in state["items"] if item["id"] == identity), None)
        if entry is None:
            raise RemoteError("character_missing", "这张角色卡已不在电脑角色库，请重新同步。")
        try:
            card = settings.load_library_character_card(identity)
            loaded_digest = character_card_digest(card)
            persona = _persona(card)
        except (SettingsError, CharacterLibraryError, CharacterCardError, ValueError, TypeError):
            raise RemoteError("character_unavailable", "这张角色卡暂时无法读取，请重新同步。") from None
        if loaded_digest != entry["digest"]:
            raise RemoteError("character_conflict", "这张角色卡已更新，请重新同步后选择。")
    if expected is not None and expected != entry["digest"]:
        raise RemoteError("character_conflict", "这张角色卡已更新，请重新同步后选择。")
    return _redacted({"version": 1, **entry, "persona": persona}, _secrets(settings))


def validate_character_selection(params):
    """Reject all extra input before a mutation request reaches its journal."""
    if not isinstance(params, dict) or set(params) != {"id", "digest", "expected_revision"}:
        raise RemoteError("invalid_request", "角色切换只接受角色标识和已同步的版本。")
    return {"id": _identity(params["id"]), "digest": _digest(params["digest"]),
            "expected_revision": _digest(params["expected_revision"])}


def select_character(settings, chat, params):
    params = validate_character_selection(params)
    state = _snapshot(settings)
    if state["revision"] != params["expected_revision"]:
        raise RemoteError("character_conflict", "电脑角色已更新，请同步后重新选择。")
    identity = params["id"]
    if identity == "default":
        card, digest = None, _persona(None)["revision"]
    else:
        entry = next((item for item in state["items"] if item["id"] == identity), None)
        if entry is None:
            raise RemoteError("character_missing", "这张角色卡已不在电脑角色库，请重新同步。")
        try:
            card = settings.load_library_character_card(identity)
            digest = character_card_digest(card)
        except (SettingsError, CharacterLibraryError, CharacterCardError, ValueError, TypeError):
            raise RemoteError("character_unavailable", "这张角色卡暂时无法读取，请重新同步。") from None
    if digest != params["digest"]:
        raise RemoteError("character_conflict", "这张角色卡已更新，请同步后重新选择。")
    # ChatController persists first, then changes its effective in-memory role
    # and emits character_changed. Existing requests keep their captured card.
    if not chat.set_character_card(card):
        raise RemoteError("character_unavailable", "电脑暂时无法切换角色，请检查电脑端状态后重试。")
    current = _snapshot(settings)
    if current["active"]["id"] != identity or current["active"]["digest"] != digest:
        raise RemoteError("character_conflict", "角色状态已变化，请同步后核对当前角色。")
    return {"version": 1, "applied": True, "active_id": identity, "revision": current["revision"]}
