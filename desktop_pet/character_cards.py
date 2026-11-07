"""Bounded, data-only import of Tavern character cards from JSON and PNG.

The V2 and V3 schemas retain optional and application-specific JSON fields.
PNG import reads the text metadata and validates chunk framing and checksums.
It does not decode image pixels or fetch resources mentioned by a card.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import struct
import zlib


MAX_CARD_FILE_BYTES = 16 * 1024 * 1024
MAX_CARD_JSON_BYTES = 1024 * 1024
MAX_CARD_TEXT_CHARACTERS = 24_000
MAX_CARD_NAME_CHARACTERS = 128
MAX_CARD_JSON_DEPTH = 32
MAX_CARD_JSON_NODES = 50_000
MAX_ALTERNATE_GREETINGS = 100
MAX_ENCODED_CARD_BYTES = ((MAX_CARD_JSON_BYTES + 2) // 3) * 4

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TEXT_FIELDS = (
    "description", "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions",
)
_INVALID_CARD = "角色卡内容无效，请选择有效的酒馆 PNG 或 JSON 角色卡。"
_TOO_LARGE = "角色卡超过大小限制，请使用不超过 16 MiB 的文件和 1 MiB 的角色数据。"
_TOO_MUCH_TEXT = "角色设定文字超过 24000 字符，请精简后重新导入。"
_INVALID_PNG = "PNG 角色卡已损坏或格式无效，请重新导出角色卡。"


class CharacterCardError(ValueError):
    """A static import error suitable for display without card contents."""


@dataclass(frozen=True)
class CharacterCard:
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: tuple[str, ...] = ()
    creator_notes: str = ""
    spec: str = "chara_card_v2"
    spec_version: str = "2.0"
    warnings: tuple[str, ...] = ()
    _source_json: str = field(default="", repr=False, compare=True)

    @classmethod
    def from_dict(cls, value: dict) -> CharacterCard:
        """Validate a native card document and detach it from its caller."""
        return _card_from_document(value)

    def to_dict(self) -> dict:
        """Return a fresh native document, preserving imported unknown data."""
        if self._source_json:
            return json.loads(self._source_json)
        data = {name: getattr(self, name) for name in _TEXT_FIELDS}
        data.update(name=self.name, creator_notes=self.creator_notes,
                    alternate_greetings=list(self.alternate_greetings), extensions={})
        return {"spec": self.spec, "spec_version": self.spec_version, "data": data}

    @property
    def character_book(self) -> dict | None:
        document = self.to_dict()
        data = document.get("data", document)
        return data.get("character_book")

    @property
    def extensions(self) -> dict:
        document = self.to_dict()
        data = document.get("data", document)
        return data.get("extensions", {})

    @property
    def nickname(self) -> str:
        if self.spec != "chara_card_v3":
            return ""
        return self.to_dict()["data"].get("nickname", "").strip()

    @property
    def prompt_name(self) -> str:
        return self.nickname or self.name


def _validate_json_tree(value) -> None:
    pending = [(value, 0)]
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if count > MAX_CARD_JSON_NODES or depth > MAX_CARD_JSON_DEPTH:
            raise CharacterCardError(_INVALID_CARD)
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str:
                    raise CharacterCardError(_INVALID_CARD)
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
        elif type(current) is list:
            pending.extend((item, depth + 1) for item in current)
        elif type(current) is str:
            if "\x00" in current:
                raise CharacterCardError(_INVALID_CARD)
            try:
                current.encode("utf-8")
            except UnicodeError:
                raise CharacterCardError(_INVALID_CARD) from None
        elif current is None or type(current) in (bool, int):
            continue
        elif type(current) is float and math.isfinite(current):
            continue
        else:
            raise CharacterCardError(_INVALID_CARD)


def _card_from_document(value) -> CharacterCard:
    if type(value) is not dict:
        raise CharacterCardError(_INVALID_CARD)
    _validate_json_tree(value)
    try:
        source = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(source.encode("utf-8")) > MAX_CARD_JSON_BYTES:
            raise CharacterCardError(_TOO_LARGE)
    except (TypeError, UnicodeError, RecursionError, OverflowError):
        raise CharacterCardError(_INVALID_CARD) from None

    spec = value.get("spec")
    if spec is None:
        data, spec, version = value, "chara_card_v1", "1.0"
    elif spec in ("chara_card_v2", "chara_card_v3"):
        data = value.get("data")
        version = value.get("spec_version", "2.0" if spec == "chara_card_v2" else "3.0")
    else:
        raise CharacterCardError("角色卡规格暂不支持，请导出为酒馆 V1、V2 或 V3 角色卡。")
    if (type(data) is not dict or type(version) is not str
            or not version.strip() or len(version) > 32):
        raise CharacterCardError(_INVALID_CARD)
    name = data.get("name")
    if type(name) is not str or not name.strip() or len(name) > MAX_CARD_NAME_CHARACTERS:
        raise CharacterCardError("角色名称为空或超过 128 字符，请检查角色卡。")
    fields = {}
    for key in (*_TEXT_FIELDS, "creator_notes"):
        text = data.get(key, "")
        if type(text) is not str:
            raise CharacterCardError(_INVALID_CARD)
        fields[key] = text
    greetings = data.get("alternate_greetings", [])
    if (type(greetings) is not list or len(greetings) > MAX_ALTERNATE_GREETINGS
            or any(type(item) is not str for item in greetings)):
        raise CharacterCardError(_INVALID_CARD)
    if len(name) + sum(len(fields[key]) for key in _TEXT_FIELDS) + sum(map(len, greetings)) > MAX_CARD_TEXT_CHARACTERS:
        raise CharacterCardError(_TOO_MUCH_TEXT)
    book = data.get("character_book")
    if book is not None and type(book) is not dict:
        raise CharacterCardError(_INVALID_CARD)
    if "extensions" in data and type(data["extensions"]) is not dict:
        raise CharacterCardError(_INVALID_CARD)
    if spec == "chara_card_v3" and "nickname" in data:
        nickname = data["nickname"]
        if type(nickname) is not str or len(nickname) > MAX_CARD_NAME_CHARACTERS:
            raise CharacterCardError("角色昵称格式无效或超过 128 字符，请检查角色卡。")

    warnings = []
    expected_version = {"chara_card_v1": "1.0", "chara_card_v2": "2.0", "chara_card_v3": "3.0"}[spec]
    if version != expected_version:
        warnings.append("此角色卡使用了不同版本的规格，部分新字段可能尚未启用。")
    if book:
        warnings.append("已保留世界书，当前仅应用基础人设。")
    if data.get("extensions") or data.get("assets"):
        warnings.append("角色卡的扩展和素材引用已保留为数据，导入时不会运行或下载。")
    return CharacterCard(name=name.strip(), **fields, alternate_greetings=tuple(greetings),
                         spec=spec, spec_version=version, warnings=tuple(warnings), _source_json=source)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CharacterCardError(_INVALID_CARD)
        result[key] = value
    return result


def _invalid_constant(_value):
    raise CharacterCardError(_INVALID_CARD)


def _json_document(raw: bytes) -> dict:
    if len(raw) > MAX_CARD_JSON_BYTES:
        raise CharacterCardError(_TOO_LARGE)
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise CharacterCardError(_INVALID_CARD) from None


def _decompress_text(raw: bytes) -> bytes:
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(raw, MAX_ENCODED_CARD_BYTES + 1)
        if len(decoded) > MAX_ENCODED_CARD_BYTES or decoder.unconsumed_tail:
            raise CharacterCardError(_TOO_LARGE)
        if not decoder.eof or decoder.unused_data:
            raise CharacterCardError(_INVALID_PNG)
        return decoded
    except zlib.error:
        raise CharacterCardError(_INVALID_PNG) from None


def _card_text(kind: bytes, data: bytes) -> tuple[bytes, bytes] | None:
    end = data.find(b"\0")
    if not 1 <= end <= 79:
        raise CharacterCardError(_INVALID_PNG)
    keyword, remaining = data[:end], data[end + 1:]
    if keyword not in (b"chara", b"ccv3"):
        return None
    if kind == b"tEXt":
        return keyword, remaining
    if kind == b"zTXt":
        if not remaining or remaining[0] != 0:
            raise CharacterCardError(_INVALID_PNG)
        return keyword, _decompress_text(remaining[1:])
    if len(remaining) < 2 or remaining[0] not in (0, 1) or remaining[1] != 0:
        raise CharacterCardError(_INVALID_PNG)
    pieces = remaining[2:].split(b"\0", 2)
    if len(pieces) != 3:
        raise CharacterCardError(_INVALID_PNG)
    text = _decompress_text(pieces[2]) if remaining[0] else pieces[2]
    return keyword, text


def _png_document(raw: bytes) -> dict:
    if not raw.startswith(PNG_SIGNATURE):
        raise CharacterCardError(_INVALID_PNG)
    offset, saw_header, saw_data, saw_end = 8, False, False, False
    card_text = {}
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise CharacterCardError(_INVALID_PNG)
        length, kind = struct.unpack_from(">I4s", raw, offset)
        end = offset + 12 + length
        if end > len(raw) or not all(65 <= value <= 90 or 97 <= value <= 122 for value in kind):
            raise CharacterCardError(_INVALID_PNG)
        data = raw[offset + 8:end - 4]
        checksum = struct.unpack_from(">I", raw, end - 4)[0]
        if zlib.crc32(kind + data) & 0xffffffff != checksum:
            raise CharacterCardError(_INVALID_PNG)
        if not saw_header and kind != b"IHDR":
            raise CharacterCardError(_INVALID_PNG)
        if kind == b"IHDR":
            if saw_header or length != 13:
                raise CharacterCardError(_INVALID_PNG)
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            valid_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if (not 0 < width <= 0x7fffffff or not 0 < height <= 0x7fffffff
                    or depth not in valid_depths.get(color, ()) or compression or filtering or interlace not in (0, 1)):
                raise CharacterCardError(_INVALID_PNG)
            saw_header = True
        elif kind == b"IDAT":
            saw_data = saw_data or bool(data)
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            entry = _card_text(kind, data)
            if entry is not None:
                keyword, encoded = entry
                if keyword in card_text:
                    raise CharacterCardError("PNG 中包含重复的角色数据，请重新导出角色卡。")
                if len(encoded) > MAX_ENCODED_CARD_BYTES:
                    raise CharacterCardError(_TOO_LARGE)
                card_text[keyword] = encoded
        elif kind == b"IEND":
            if length or not saw_data or end != len(raw):
                raise CharacterCardError(_INVALID_PNG)
            saw_end = True
            break
        offset = end
    if not saw_end:
        raise CharacterCardError(_INVALID_PNG)
    encoded = card_text.get(b"ccv3", card_text.get(b"chara"))
    if encoded is None:
        raise CharacterCardError("这张 PNG 没有内嵌角色设定，请导入角色卡原文件或 JSON 文件。")
    try:
        document = base64.b64decode(b"".join(encoded.split()), validate=True)
    except (binascii.Error, ValueError):
        raise CharacterCardError(_INVALID_CARD) from None
    return _json_document(document)


def parse_card_bytes(raw: bytes, suffix: str = "") -> CharacterCard:
    """Import bounded JSON or PNG bytes without evaluating their contents."""
    if type(raw) is not bytes or type(suffix) is not str:
        raise CharacterCardError(_INVALID_CARD)
    if len(raw) > MAX_CARD_FILE_BYTES:
        raise CharacterCardError(_TOO_LARGE)
    extension = suffix.lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    if extension not in ("", ".json", ".png", ".apng"):
        raise CharacterCardError("只支持 PNG 或 JSON 角色卡，请先从酒馆导出对应格式。")
    is_png = extension in (".png", ".apng") or not extension and raw.startswith(PNG_SIGNATURE)
    document = _png_document(raw) if is_png else _json_document(raw)
    return CharacterCard.from_dict(document)


def load_character_card(path: str | Path) -> CharacterCard:
    """Read one explicit file using a bounded read and static I/O failures."""
    try:
        source = Path(path)
        with source.open("rb") as stream:
            raw = stream.read(MAX_CARD_FILE_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise CharacterCardError("无法读取角色卡文件，请检查文件是否存在及读取权限。") from None
    return parse_card_bytes(raw, source.suffix)
