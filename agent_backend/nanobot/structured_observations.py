"""Host-only, bounded UIA evidence for an independent multimodal judge.

Input is a DesktopControls.observe result, never a model argument or action
claim. Content remains untrusted reference data. Capture order proves only
observation order; it is not proof of which action or business object changed.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

MAX_FRAMES = 8
MAX_BYTES = 64 * 1024
_INPUT_BYTES = 192 * 1024
_SNAPSHOT = re.compile(r"[a-f0-9]{16}\Z")
_REF = re.compile(r"uia_[a-f0-9]{16}_[a-f0-9]{16}\Z")
_TYPE = re.compile(r"ControlType\.[A-Za-z]{1,60}\Z")
_SCOPES = {"foreground_window_accessibility_only", "selected_window_accessibility"}
_ACTIONS = {"invoke", "set_value", "check", "uncheck", "select", "focus", "expand", "collapse", "scroll"}
_STATE_BOOLS = {"enabled", "focused", "checked", "selected", "read_only", "expanded"}


class StructuredObservationError(ValueError):
    def __init__(self):
        super().__init__("GUI_STRUCTURED_OBSERVATION_INVALID")


def _reject():
    raise StructuredObservationError()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: _reject()
        value[key] = item
    return value


def _text(value, maximum, secrets, *, multiline=False):
    if not isinstance(value, str): _reject()
    for secret in secrets:
        value = value.replace(secret, "[redacted]")
    if any((ord(c) < 32 and not (multiline and c in "\n\r\t")) or ord(c) == 127 for c in value): _reject()
    try:
        if len(value.encode("utf-16-le")) // 2 > maximum: _reject()
    except UnicodeError:
        _reject()
    return value


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper: _reject()
    return value


def project_observation(raw, *, secrets=()):
    if not isinstance(raw, str): _reject()
    try:
        if len(raw.encode("utf-8")) > _INPUT_BYTES: _reject()
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=lambda _: _reject())
    except (ValueError, UnicodeError, RecursionError):
        _reject()
    required = {"status", "snapshot_id", "window", "controls", "truncated"}
    allowed = required | {"scope", "offset", "query", "next_offset"}
    if (not isinstance(value, dict) or not required <= set(value) or set(value) - allowed
            or value["status"] != "observed" or type(value["truncated"]) is not bool
            or not isinstance(value["snapshot_id"], str) or not _SNAPSHOT.fullmatch(value["snapshot_id"])):
        _reject()
    window = value["window"]
    if not isinstance(window, dict) or set(window) != {"title", "process_id"}: _reject()
    window = {"title": _text(window["title"], 240, secrets), "process_id": _integer(window["process_id"], 1, 2**31 - 1)}
    scope = value.get("scope", "foreground_window_accessibility_only")
    if not isinstance(scope, str) or scope not in _SCOPES: _reject()
    offset = _integer(value.get("offset", 0), 0, 1500)
    following = value.get("next_offset")
    if following is not None: _integer(following, 1, 1500)
    query = _text(value.get("query", ""), 200, secrets)
    controls = value["controls"]
    if not isinstance(controls, list) or len(controls) > 120: _reject()
    projected = []
    for control in controls:
        required_control = {"name", "control_type", "state", "protected"}
        if (not isinstance(control, dict) or not required_control <= set(control)
                or set(control) - (required_control | {"ref", "actions"})
                or type(control["protected"]) is not bool): _reject()
        if "ref" in control and (not isinstance(control["ref"], str) or not _REF.fullmatch(control["ref"])): _reject()
        if "actions" in control and (not isinstance(control["actions"], list) or len(control["actions"]) > len(_ACTIONS)
                or any(not isinstance(action, str) or action not in _ACTIONS for action in control["actions"])): _reject()
        kind = control["control_type"]
        if not isinstance(kind, str) or not _TYPE.fullmatch(kind): _reject()
        state = control["state"]
        if not isinstance(state, dict) or set(state) - (_STATE_BOOLS | {"value"}): _reject()
        if control["protected"]:
            if state: _reject()
            name, state = "[protected control]", {}
        else:
            name = _text(control["name"], 240, secrets)
            clean = {}
            for key, entry in state.items():
                if key == "value": clean[key] = _text(entry, 1024, secrets, multiline=True)
                elif type(entry) is bool: clean[key] = entry
                elif key in {"checked", "selected"} and entry is None: clean[key] = None
                else: _reject()
            state = clean
        projected.append({"name": name, "control_type": kind, "state": state, "protected": control["protected"]})
    return {"source": "windows_uia", "status": "observed", "snapshot_id": value["snapshot_id"], "window": window,
        "controls": projected, "truncated": value["truncated"], "scope": scope, "offset": offset,
        "query": query, "next_offset": following}


def _fingerprint(frame):
    controls = sorted(frame["controls"], key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    semantic = {key: frame[key] for key in ("window", "truncated", "scope", "offset", "query", "next_offset")}
    semantic["controls"] = controls
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _payload(entries, total):
    frames = [frame for frame, fingerprint in entries]
    captures = sum(frame["capture_count"] for frame in frames)
    return {"frames": frames, "summary": {"total_captures": total, "retained_states": len(frames),
        "retained_captures": captures, "omitted_captures": total - captures, "collapsed_repeats": captures - len(frames)}}


def _size(entries, total):
    return len(json.dumps(_payload(entries, total), ensure_ascii=False).encode("utf-8"))


class StructuredObservations:
    def __init__(self, *, secrets=(), clock=None):
        self.secrets = tuple(sorted({item for item in secrets if isinstance(item, str) and item}, key=len, reverse=True))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries = []
        self._sequence = 0
        self._recent_ids = deque(maxlen=256)
        self._last_capture = None

    def remember(self, raw):
        """Reject unsafe/non-observation input without inventing capture facts."""
        try:
            observed = project_observation(raw, secrets=self.secrets)
            if observed["snapshot_id"] in self._recent_ids: return False
            captured = self.clock()
            if not isinstance(captured, datetime) or captured.tzinfo is None: return False
            captured = captured.astimezone(timezone.utc)
            if self._last_capture is not None and captured < self._last_capture: return False
            stamp = captured.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            sequence = self._sequence + 1
            fingerprint = _fingerprint(observed)
            entries = deepcopy(self._entries)
            if entries and entries[-1][1] == fingerprint:
                frame = entries[-1][0]
                frame.update(sequence=sequence, captured_at=stamp, snapshot_id=observed["snapshot_id"], capture_count=frame["capture_count"] + 1)
            else:
                frame = {**observed, "sequence": sequence, "captured_at": stamp, "first_sequence": sequence,
                    "first_captured_at": stamp, "capture_count": 1}
                entries.append((frame, fingerprint))
            # Keep real first and latest states and transition boundaries. A
            # rejected oversized state never appears as an observed interval.
            if len(entries) > MAX_FRAMES or _size(entries, sequence) > MAX_BYTES:
                mandatory = [entries[0]] if len(entries) == 1 else [entries[0], entries[-1]]
                if _size(mandatory, sequence) > MAX_BYTES: return False
                retained = {entries[0][0]["sequence"]: entries[0], entries[-1][0]["sequence"]: entries[-1]}
                priorities = []
                for distance in range(1, len(entries)):
                    priorities += [distance, len(entries) - 1 - distance]
                for index in priorities:
                    if not 0 <= index < len(entries) or len(retained) >= MAX_FRAMES: continue
                    candidate = entries[index]
                    if candidate[0]["sequence"] in retained: continue
                    proposed = sorted([*retained.values(), candidate], key=lambda item: item[0]["sequence"])
                    if _size(proposed, sequence) <= MAX_BYTES: retained[candidate[0]["sequence"]] = candidate
                entries = sorted(retained.values(), key=lambda item: item[0]["sequence"])
            self._entries = entries
            self._sequence = sequence
            self._last_capture = captured
            self._recent_ids.append(observed["snapshot_id"])
            return True
        except (StructuredObservationError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return False

    def payload(self):
        return deepcopy(_payload(self._entries, self._sequence))
