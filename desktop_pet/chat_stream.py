"""Project a compatible provider's wire output into safe, visible reply text.

The UI sees cumulative text snapshots, never the JSON envelope or SDK deltas.
Incomplete escapes and credential prefixes stay buffered between network reads.
"""
from __future__ import annotations

import json
import re

from desktop_pet.redaction import _spellings, redact_text


MAX_STREAM_CHARACTERS = 1_048_576
REDACTED = "[已隐藏密钥]"
_HIDDEN_TAG = re.compile(r"</?(?:think|analysis|reasoning)>", re.I)
_HIDDEN_TAGS = ("<think>", "</think>", "<analysis>", "</analysis>", "<reasoning>", "</reasoning>")


class StreamFormatError(ValueError):
    """No provider text is included in this exception."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StreamFormatError("duplicate response field")
        result[key] = value
    return result


def _string_prefix(text: str, start: int) -> str:
    """Decode complete JSON string units, keeping partial surrogate pairs back."""
    cursor, safe_end = start + 1, start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == '"':
            break
        if ord(char) < 32:
            raise StreamFormatError("invalid response string")
        if char == "\\":
            if cursor + 1 >= len(text):
                break
            escape = text[cursor + 1]
            if escape == "u":
                if cursor + 6 > len(text):
                    break
                digits = text[cursor + 2:cursor + 6]
                if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    raise StreamFormatError("invalid unicode escape")
                unit = int(digits, 16)
                if 0xD800 <= unit <= 0xDBFF:
                    if cursor + 12 > len(text):
                        break
                    following = text[cursor + 6:cursor + 12]
                    if not re.fullmatch(r"\\u[dD][c-fC-F][0-9a-fA-F]{2}", following):
                        raise StreamFormatError("invalid unicode pair")
                    cursor += 12
                elif 0xDC00 <= unit <= 0xDFFF:
                    raise StreamFormatError("invalid unicode pair")
                else:
                    cursor += 6
            elif escape in '"\\/bfnrt':
                cursor += 2
            else:
                raise StreamFormatError("invalid response escape")
        elif 0xD800 <= ord(char) <= 0xDFFF:
            # A transport decoder must never introduce unpaired UTF-16 units.
            raise StreamFormatError("invalid unicode unit")
        else:
            cursor += 1
        safe_end = cursor
    try:
        return json.loads('"' + text[start + 1:safe_end] + '"')
    except (ValueError, TypeError):
        raise StreamFormatError("invalid response string") from None


def _reply_prefix(text: str) -> str:
    """Visit only top-level fields; nested tools/reasoning are never projected."""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    cursor = 1
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] == "}":
            return ""
        try:
            key, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            return ""
        if not isinstance(key, str):
            raise StreamFormatError("invalid response field")
        cursor = end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            return ""
        if text[cursor] != ":":
            raise StreamFormatError("invalid response field")
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            return ""
        if key == "reply":
            if text[cursor] != '"':
                raise StreamFormatError("invalid reply field")
            return _string_prefix(text, cursor)
        try:
            _, cursor = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            return ""
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] == "}":
            return ""
        if text[cursor] != ",":
            raise StreamFormatError("invalid response separator")
        cursor += 1
    return ""


def _without_hidden_text(text: str, *, final=False) -> str:
    """Some compatible servers put reasoning tags in content, not a separate delta."""
    output, cursor, stack = [], 0, []
    for match in _HIDDEN_TAG.finditer(text):
        if not stack:
            output.append(text[cursor:match.start()])
        tag = match.group().lower()
        if not tag.startswith("</"):
            stack.append(tag[1:-1])
        elif stack and tag == f"</{stack[-1]}>":
            stack.pop()
        cursor = match.end()
    if not stack:
        output.append(text[cursor:])
    result = "".join(output)
    if not final:
        boundary = result.rfind("<")
        if boundary >= 0 and any(tag.startswith(result[boundary:].lower()) for tag in _HIDDEN_TAGS):
            result = result[:boundary]
    return result


class ReplyStream:
    """Accumulate one HTTP response and expose its safe, visible projection."""

    def __init__(self, secret: str, *, defer_plain=False):
        self.raw = ""
        self.text = ""
        self.structured = False
        self.received_output = False
        self._defer_plain = defer_plain
        self._secret = secret
        self._spellings = tuple(_spellings(secret, MAX_STREAM_CHARACTERS)) if secret else ()

    def _candidate(self, *, final=False):
        text = _without_hidden_text(self.raw, final=final).lstrip()
        if not text:
            return None, False
        if text.startswith("{"):
            return text, True
        # Hold a possible JSON fence until its language/body is identifiable.
        if "```".startswith(text) and not final:
            return None, False
        if text.startswith("```"):
            line, separator, body = text.partition("\n")
            if not separator:
                if not final and ("```json".startswith(line.lower()) or line.lower() == "```json"):
                    return None, False
            elif line.strip().lower() in ("```", "```json"):
                if line.strip().lower() == "```json" or body.lstrip().startswith("{"):
                    return body.rsplit("```", 1)[0] if final and body.rstrip().endswith("```") else body, True
                if not body.strip() and not final:
                    return None, False
        return text, False

    def _redact(self, text: str, *, final=False) -> str:
        # Prefix matching holds only a possible credential suffix, rather than
        # delaying every token by the length of the longest escaped spelling.
        text = redact_text(text, (self._secret,), REDACTED)
        if not final:
            held = 0
            for spelling in self._spellings:
                start = max(0, len(text) - len(spelling) + 1)
                while start < len(text) - held:
                    start = text.find(spelling[0], start, len(text) - held)
                    if start < 0:
                        break
                    if spelling.startswith(text[start:]):
                        held = len(text) - start
                        break
                    start += 1
            if held:
                text = text[:-held]
        return text

    def feed(self, delta: str) -> str:
        if not isinstance(delta, str):
            raise StreamFormatError("invalid content delta")
        self.received_output = True
        if len(self.raw) + len(delta) > MAX_STREAM_CHARACTERS:
            raise StreamFormatError("response exceeds stream limit")
        self.raw += delta
        candidate, structured = self._candidate()
        self.structured = self.structured or structured
        if candidate is not None and (structured or not self._defer_plain):
            visible = _reply_prefix(candidate) if structured else candidate
            self.text = self._redact(_without_hidden_text(visible))
        return self.text

    def finish(self) -> str:
        candidate, structured = self._candidate(final=True)
        if candidate is None:
            raise StreamFormatError("empty response")
        if structured:
            try:
                value = json.loads(candidate, object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                raise StreamFormatError("invalid response envelope") from None
            if not isinstance(value, dict) or not isinstance(value.get("reply"), str) or not value["reply"].strip():
                raise StreamFormatError("invalid reply field")
            visible = value["reply"]
        else:
            visible = candidate
        if structured or not self._defer_plain:
            self.text = self._redact(_without_hidden_text(visible, final=True).strip(), final=True)
        return candidate

    def final_reply(self, reply: str) -> str:
        """Apply the existing strict routing/parser contract before final display."""
        self.text = self._redact(_without_hidden_text(reply, final=True).strip(), final=True)
        return self.text
