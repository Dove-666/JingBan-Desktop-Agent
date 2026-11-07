"""Optional local host Office configuration. Import never reads that file."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "agent_backend/nanobot/runtime/office_renderer.json"
_LIMIT = 16384
_SNAPSHOT_ISSUER = object()


class OfficeRuntimeConfigurationError(ValueError):
    def __init__(self): super().__init__("HOST_OFFICE_CONFIGURATION_INVALID")


class OfficeRuntimeSnapshot:
    """Immutable host capture; its identity can bind policy and the run frame."""
    __slots__ = ("_issuer", "_encoded", "_identity")
    def __new__(cls, *args, **kwargs): raise TypeError("HOST_OFFICE_SNAPSHOT_REQUIRED")
    def __setattr__(self, *args): raise AttributeError("HOST_OFFICE_SNAPSHOT_IMMUTABLE")
    def __reduce__(self): raise TypeError("HOST_OFFICE_SNAPSHOT_NOT_SERIALIZABLE")
    def __repr__(self): return "OfficeRuntimeSnapshot([host configuration])"
    @property
    def identity(self): return self._identity
    @property
    def configuration(self):
        # The caller receives a fresh plain frame dictionary, never authority
        # to mutate the captured configuration shared with another thread.
        return json.loads(self._encoded) if self._encoded is not None else None


def valid_office_runtime_snapshot(value):
    return type(value) is OfficeRuntimeSnapshot and getattr(value, "_issuer", None) is _SNAPSHOT_ISSUER


def capture_office_runtime():
    config = load_office_runtime()
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True) if config is not None else None
    value = object.__new__(OfficeRuntimeSnapshot)
    for field, item in (("_issuer", _SNAPSHOT_ISSUER), ("_encoded", encoded),
                        ("_identity", hashlib.sha256(encoded.encode("ascii")).hexdigest() if encoded is not None else "")):
        object.__setattr__(value, field, item)
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise OfficeRuntimeConfigurationError()
        result[key] = value
    return result


def load_office_runtime():
    """Missing means legacy COM. Present but invalid is an explicit blocker."""
    try: before = _CONFIG.lstat()
    except FileNotFoundError: return None
    except OSError: raise OfficeRuntimeConfigurationError() from None
    try:
        if (not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400
                or before.st_size > _LIMIT or before.st_nlink != 1):
            raise OfficeRuntimeConfigurationError()
        if _CONFIG.resolve() != _ROOT.resolve() / "agent_backend/nanobot/runtime/office_renderer.json":
            raise OfficeRuntimeConfigurationError()
        with _CONFIG.open("rb") as stream: raw = stream.read(_LIMIT + 1)
        after = _CONFIG.stat()
        if len(raw) > _LIMIT or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise OfficeRuntimeConfigurationError()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(OfficeRuntimeConfigurationError()))
        from agent_backend.nanobot.host_office_runtime import checked_office_renderer
        return checked_office_renderer(value)
    except Exception: raise OfficeRuntimeConfigurationError() from None
