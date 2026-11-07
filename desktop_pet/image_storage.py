"""In-memory lifetime gate shared by image work and storage maintenance.

The application registers image work with this gate. An empty snapshot is not
proof that an external Agent worker or process has stopped. Callers must also
establish those resource lifetimes before permitting maintenance.

No lock is held while user work runs. A pending token may be cancelled before
entry, but running work remains a blocker until ``run`` actually returns. An
image import can retain that blocker until its GUI callback atomically hands it
to a fresh stage token. There is no timeout-based release.
"""

from __future__ import annotations

from threading import RLock
from typing import Callable, TypeVar


_Result = TypeVar("_Result")


class ImageStorageGateError(ValueError):
    """A rejection containing a fixed code and no caller-supplied details."""

    _CODES = frozenset({
        "IMAGE_STORAGE_BUSY",
        "IMAGE_STORAGE_IN_USE",
        "IMAGE_STORAGE_OWNER_CLOSED",
        "IMAGE_STORAGE_INVALID_OWNER",
        "IMAGE_STORAGE_INVALID_TOKEN",
        "IMAGE_STORAGE_TOKEN_USED",
        "IMAGE_STORAGE_NOT_READY",
        "IMAGE_STORAGE_CANCELLED",
        "IMAGE_STORAGE_INVALID_OPERATION",
    })

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in self._CODES else "IMAGE_STORAGE_INVALID_OPERATION"
        super().__init__(self.code)


class _Owner:
    __slots__ = ("_gate", "_canonical", "_closed", "_tokens")

    def __init__(self, gate: ImageStorageGate) -> None:
        self._gate = gate
        self._canonical = self
        self._closed = False
        self._tokens: set[_Token] = set()

    def __repr__(self) -> str:
        return "<image-storage owner>"


class _Token:
    __slots__ = ("_gate", "_record")

    def __init__(self, gate: ImageStorageGate, record: _Record) -> None:
        self._gate = gate
        self._record = record

    def __repr__(self) -> str:
        return "<image-storage token>"


class _Record:
    __slots__ = ("owner", "kind", "stage_on_success", "phase", "discard", "token")

    def __init__(self, owner: _Owner, kind: str, stage_on_success: bool = False) -> None:
        self.owner = owner
        self.kind = kind
        self.stage_on_success = stage_on_success
        self.phase = "stage" if kind == "stage" else "pending"
        self.discard = False
        self.token: _Token


class ImageStorageGate:
    """Shared activity and exclusive maintenance, using opaque local tokens.

    Activity covers a real physical operation, not merely a GUI busy flag.
    Callers submit ``lambda: gate.run(token, operation)`` to their worker. If
    submission fails, ``release(token)`` safely cancels a not-yet-entered
    runnable; if it already entered, the same call only requests discard and
    its actual finally still owns retirement. No result/exception is retained.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._active: set[_Token] = set()
        self._activity_count = 0
        self._stage_count = 0
        self._maintenance: _Token | None = None

    def create_owner(self) -> _Owner:
        """Create an identity without storing a CID, image ID, or owner name."""
        return _Owner(self)

    def activity(self, owner: object, *, stage_on_success: bool = False) -> _Token:
        """Reserve before submission; optionally await an explicit stage handoff."""
        with self._lock:
            checked = self._owner(owner)
            if checked._closed:
                raise ImageStorageGateError("IMAGE_STORAGE_OWNER_CLOSED")
            if type(stage_on_success) is not bool:
                raise ImageStorageGateError("IMAGE_STORAGE_INVALID_OPERATION")
            if self._maintenance is not None:
                raise ImageStorageGateError("IMAGE_STORAGE_BUSY")
            return self._add(_Record(checked, "activity", stage_on_success))

    def maintenance(self, owner: object) -> _Token:
        """Reserve an exclusive operation; this does not grant delete authority."""
        with self._lock:
            checked = self._owner(owner)
            if checked._closed:
                raise ImageStorageGateError("IMAGE_STORAGE_OWNER_CLOSED")
            if self._maintenance is not None:
                raise ImageStorageGateError("IMAGE_STORAGE_BUSY")
            if self._activity_count or self._stage_count:
                raise ImageStorageGateError("IMAGE_STORAGE_IN_USE")
            return self._add(_Record(checked, "maintenance"))

    def run(self, token: object, operation: Callable[[], _Result]) -> _Result:
        """Run synchronous work once; retire only from its physical finally."""
        if not callable(operation):
            raise ImageStorageGateError("IMAGE_STORAGE_INVALID_OPERATION")
        with self._lock:
            record = self._token(token)
            if record.kind == "stage":
                raise ImageStorageGateError("IMAGE_STORAGE_INVALID_OPERATION")
            if record.phase == "cancelled":
                raise ImageStorageGateError("IMAGE_STORAGE_CANCELLED")
            if record.phase != "pending":
                raise ImageStorageGateError("IMAGE_STORAGE_TOKEN_USED")
            record.phase = "running"
        succeeded = False
        try:
            result = operation()
            succeeded = True
            return result
        finally:
            with self._lock:
                if record.discard or record.owner._closed:
                    self._retire(record, "cancelled")
                elif succeeded and record.kind == "activity" and record.stage_on_success:
                    record.phase = "awaiting_stage"
                else:
                    self._retire(record, "finished" if succeeded else "failed")

    def handoff_to_stage(self, token: object) -> _Token | None:
        """Replace a successful import with a fresh stage in one locked step.

        An invalidated/closed-owner late callback receives None and cannot
        resurrect its image. A previously handed-off token cannot mint another
        stage or release the successor.
        """
        with self._lock:
            record = self._token(token)
            if record.kind != "activity" or not record.stage_on_success:
                raise ImageStorageGateError("IMAGE_STORAGE_INVALID_OPERATION")
            if record.phase in {"cancelled", "failed"}:
                return None
            if record.phase == "handed_off":
                raise ImageStorageGateError("IMAGE_STORAGE_TOKEN_USED")
            if record.phase != "awaiting_stage":
                raise ImageStorageGateError("IMAGE_STORAGE_NOT_READY")
            successor = self._add(_Record(record.owner, "stage"))
            self._retire(record, "handed_off")
            return successor

    def release(self, token: object) -> bool:
        """Idempotently retire, or request discard without releasing running IO.

        False means no blocker was removed by this call. In particular, it is
        not proof of drain; running operations retain their token until finally.
        """
        with self._lock:
            record = self._token(token)
            if record.phase == "running":
                record.discard = True
                return False
            if record.token not in self._active:
                return False
            self._retire(record, "cancelled")
            return True

    def close_owner(self, owner: object) -> None:
        """Invalidate callbacks; already-running activity still owns its drain."""
        with self._lock:
            checked = self._owner(owner)
            checked._closed = True
            for token in tuple(checked._tokens):
                record = token._record
                if record.phase == "running":
                    record.discard = True
                else:
                    self._retire(record, "cancelled")

    def snapshot(self) -> dict[str, int | bool | str]:
        """Return counts only; never expose owners, image identities, or payloads."""
        with self._lock:
            status = ("IMAGE_STORAGE_MAINTENANCE" if self._maintenance is not None
                      else "IMAGE_STORAGE_ACTIVE" if self._active else "IMAGE_STORAGE_READY")
            return {
                "activity_count": self._activity_count,
                "stage_count": self._stage_count,
                "maintenance_active": self._maintenance is not None,
                "status_code": status,
            }

    def _owner(self, owner: object) -> _Owner:
        if (type(owner) is not _Owner or owner._gate is not self
                or owner._canonical is not owner):
            raise ImageStorageGateError("IMAGE_STORAGE_INVALID_OWNER")
        return owner

    def _token(self, token: object) -> _Record:
        if (type(token) is not _Token or token._gate is not self
                or token._record.token is not token):
            raise ImageStorageGateError("IMAGE_STORAGE_INVALID_TOKEN")
        return token._record

    def _add(self, record: _Record) -> _Token:
        token = _Token(self, record)
        record.token = token
        self._active.add(token)
        record.owner._tokens.add(token)
        if record.kind == "activity":
            self._activity_count += 1
        elif record.kind == "stage":
            self._stage_count += 1
        else:
            self._maintenance = token
        return token

    def _retire(self, record: _Record, phase: str) -> None:
        token = record.token
        self._active.remove(token)
        record.owner._tokens.remove(token)
        if record.kind == "activity":
            self._activity_count -= 1
        elif record.kind == "stage":
            self._stage_count -= 1
        else:
            self._maintenance = None
        record.phase = phase
