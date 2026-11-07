"""Optional host-owned project copies and single-artifact publication.

No tool registration, process launch, filesystem IO or permission changes occur
on import. This is not a generic repo/build agent. Execution uses the separately
reviewed, disabled-by-default MXC backend and its explicit residual boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import unicodedata

if __package__:
    from . import mxc_execution as mxc, mxc_native
    from .container_execution import HostExecutionScope, _no_aliases
    from .safe_files import MAX_DOCUMENT_BYTES
else:
    import mxc_execution as mxc
    import mxc_native
    from container_execution import HostExecutionScope, _no_aliases
    from safe_files import MAX_DOCUMENT_BYTES


_HASH = re.compile(r"[a-f0-9]{64}\Z")


class ProjectSnapshotError(ValueError):
    """Only bounded fixed codes leave this host helper."""
    effect_state = "none"
    publication_attempted = False


class ProjectPublicationUnknown(ProjectSnapshotError):
    """Host write entered without a reliable acknowledgement; never replay."""
    effect_state = "unknown"
    publication_attempted = True

    def __init__(self):
        super().__init__("PROJECT_PUBLICATION_UNCONFIRMED")


def _error(code):
    raise ProjectSnapshotError(code) from None


def _relative(value):
    if (type(value) is not str or not 1 <= len(value) <= 512 or "\\" in value
            or unicodedata.normalize("NFC", value) != value):
        _error("PROJECT_INVALID_RELATIVE_PATH")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        _error("PROJECT_INVALID_RELATIVE_PATH")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or not 1 <= len(path.parts) <= 12:
        _error("PROJECT_INVALID_RELATIVE_PATH")
    for part in path.parts:
        if (part in {".", ".."} or not 1 <= len(part) <= 96 or part.endswith((".", " "))
                or any(ord(char) < 32 or char in '<>:"|?*\x7f' for char in part)
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
                    *(f"COM{i}" for i in "¹²³"), *(f"LPT{i}" for i in "¹²³")}):
            _error("PROJECT_INVALID_RELATIVE_PATH")
    return value


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        _error("PROJECT_INVALID_EXPECTED_HASH")
    return value


def _physical(path, *, regular=False):
    try:
        path = _no_aliases(path)
        info = path.lstat()
        if regular and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            _error("PROJECT_FILE_ALIAS_REJECTED")
        return path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except ProjectSnapshotError:
        raise
    except Exception:
        _error("PROJECT_PATH_ALIAS_OR_UNAVAILABLE")


@contextmanager
def _owned_stream(path, mode):
    stream = Path(path).open(mode)
    descriptor, failed = None, False
    try:
        descriptor = stream.fileno()
        yield stream
    except BaseException:
        failed = True
        raise
    finally:
        try:
            stream.close()
        except BaseException:
            # FileIO.closed can become true before an OS close error. Keep the
            # exact owner/descriptor permanently; no retry of a reused number.
            mxc_native.retain_failed_close(stream, descriptor)
            if not failed:
                _error("PROJECT_CLEANUP_UNCONFIRMED")


@dataclass(frozen=True)
class ProjectLimits:
    input_files: int = 16
    input_bytes: int = 4 * 1024 * 1024
    output_bytes: int = 4 * 1024 * 1024

    def validate(self):
        for value, maximum in ((self.input_files, 16), (self.input_bytes, 16 * 1024 * 1024),
                                (self.output_bytes, 16 * 1024 * 1024)):
            if type(value) is not int or not 1 <= value <= maximum:
                _error("PROJECT_INVALID_LIMITS")


@dataclass(frozen=True)
class ProjectInput:
    path: str
    expected_sha256: str


@dataclass(frozen=True)
class ProjectOutput:
    path: str
    target: str
    expected_sha256: str | None = None


@dataclass(frozen=True, repr=False)
class _SnapshotFile:
    path: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, repr=False)
class _CapturedArtifact:
    owner: object = field(repr=False)
    data: bytes = field(repr=False)
    sha256: str


class ProjectSnapshot:
    """Immutable inputs, one host-authorized target and one execution attempt.

    Call prepare with trusted host scope/goal/guard callbacks. The same guard
    must serialize actual Stop/goal/grant mutations, not a newly invented lock.
    Slow staging/capture run outside it; final bounded freshness checks and the
    WorkspaceFiles atomic single-file commit run inside it. No multi-file
    transaction or task/goal verification is claimed.
    """
    def __repr__(self):
        return "ProjectSnapshot([host-owned])"

    def __reduce__(self):
        raise TypeError("PROJECT_AUTHORITY_NOT_SERIALIZABLE")

    @classmethod
    def prepare(cls, scope, inputs, output, *, goal_revision, goal_is_current,
                authority_guard, limits=None):
        if (not isinstance(scope, HostExecutionScope) or type(inputs) not in (list, tuple)
                or type(output) is not ProjectOutput or type(goal_revision) is not str
                or not _HASH.fullmatch(goal_revision) or not callable(goal_is_current)
                or not callable(authority_guard)):
            _error("PROJECT_INVALID_AUTHORITY")
        limits = limits or ProjectLimits()
        if type(limits) is not ProjectLimits:
            _error("PROJECT_INVALID_LIMITS")
        limits.validate()
        if not 1 <= len(inputs) <= limits.input_files:
            _error("PROJECT_INPUT_LIMIT")
        self = cls()
        self._scope, self._goal, self._goal_current = scope, goal_revision, goal_is_current
        self._guard, self._limits = authority_guard, limits
        self._state_lock = threading.RLock()
        self._state, self._captured, self._receipt = "preparing", None, None
        self._publication_attempted = False
        self._staged_root, self._staged_identity, self._cancel = None, None, None
        self._output = ProjectOutput(_relative(output.path), _relative(output.target),
            None if output.expected_sha256 is None else _hash(output.expected_sha256))
        self._root_identity = mxc.MxcPythonRunner._scope(scope)
        self._current()
        names, seen_paths, total, captured = set(), set(), 0, []
        for row in inputs:
            if type(row) is not ProjectInput:
                _error("PROJECT_INVALID_INPUT")
            relative, digest = _relative(row.path), _hash(row.expected_sha256)
            key = relative.casefold()
            if (key in names or any(key.startswith(other + "/") or other.startswith(key + "/")
                                    for other in names)):
                _error("PROJECT_INPUT_ALIAS_REJECTED")
            target, raw, actual = self._read_input(relative)
            identity = target.stat().st_dev, target.stat().st_ino
            if identity in seen_paths:
                _error("PROJECT_INPUT_ALIAS_REJECTED")
            if actual != digest:
                _error("PROJECT_INPUT_CHANGED")
            total += len(raw)
            if total > limits.input_bytes:
                _error("PROJECT_INPUT_LIMIT")
            captured.append(_SnapshotFile(relative, actual, raw))
            seen_paths.add(identity); names.add(key)
        self._inputs = tuple(captured)
        self._current(); self._target_current()
        self._identity = hashlib.sha256(json.dumps({"scope": scope.identity, "goal": goal_revision,
            "inputs": [{"path": row.path, "sha256": row.sha256, "bytes": len(row.data)} for row in self._inputs],
            "output": {"path": self._output.path, "target": self._output.target,
                       "expected_sha256": self._output.expected_sha256}},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        self._state = "prepared"
        return self

    @property
    def manifest(self):
        return {"schema": "aura.project_snapshot.v1", "sha256": self._identity,
            "inputs": [{"path": row.path, "sha256": row.sha256, "bytes": len(row.data)} for row in self._inputs],
            "output": self._output.path, "task_verified": False}

    @property
    def publication_attempted(self):
        return self._publication_attempted

    def _current(self):
        try:
            mxc.MxcPythonRunner._scope(self._scope, self._root_identity)
        except Exception:
            _error("PROJECT_SCOPE_REVOKED")
        try:
            current = self._goal_current(self._goal) is True
        except Exception:
            current = False
        if not current:
            _error("PROJECT_GOAL_CHANGED")
        if self._cancel is not None and self._cancel.is_set():
            _error("PROJECT_CANCELLED")

    def _live_path(self, relative, *, write=False):
        lexical = self._scope.files.root.joinpath(*PurePosixPath(relative).parts)
        try:
            actual = self._scope.files.resolve(relative, write=write)
            if actual != lexical:
                _error("PROJECT_FILE_ALIAS_REJECTED")
            parent = lexical
            while not parent.exists():
                if parent.is_symlink():
                    _error("PROJECT_FILE_ALIAS_REJECTED")
                parent = parent.parent
            _physical(parent, regular=parent == lexical)
            return lexical
        except ProjectSnapshotError:
            raise
        except Exception:
            _error("PROJECT_PATH_ALIAS_OR_UNAVAILABLE")

    def _read_input(self, relative):
        target = self._live_path(relative)
        _, before = _physical(target, regular=True)
        try:
            actual, raw, digest = self._scope.files.read_bytes(relative, maximum=self._limits.input_bytes)
        except Exception:
            _error("PROJECT_INPUT_UNAVAILABLE")
        _, after = _physical(target, regular=True)
        if actual != target or before != after:
            _error("PROJECT_INPUT_CHANGED")
        return target, raw, digest

    def _sources_current(self):
        self._current()
        for row in self._inputs:
            _, _, actual = self._read_input(row.path)
            if actual != row.sha256:
                _error("PROJECT_INPUT_CHANGED")
        self._current()

    def _target_current(self):
        target = self._live_path(self._output.target, write=True)
        expected = self._output.expected_sha256
        if target.exists():
            if expected is None:
                _error("PROJECT_TARGET_EXISTS")
            _, before = _physical(target, regular=True)
            try:
                actual, _, digest = self._scope.files.read_bytes(self._output.target, maximum=MAX_DOCUMENT_BYTES)
            except Exception:
                _error("PROJECT_TARGET_UNAVAILABLE")
            _, after = _physical(target, regular=True)
            if actual != target or before != after or digest != expected:
                _error("PROJECT_TARGET_CHANGED")
        elif expected is not None:
            _error("PROJECT_TARGET_CHANGED")

    def _stage_inputs(self, directory, scope):
        if scope is not self._scope:
            _error("PROJECT_SCOPE_MISMATCH")
        self._sources_current()
        directory, _ = _physical(directory)
        if any(directory.iterdir()):
            _error("PROJECT_STAGING_NOT_EMPTY")
        root, identity = _physical(directory.parent.parent)
        self._staged_root, self._staged_identity = root, identity[:2]
        for row in self._inputs:
            target = directory.joinpath(*PurePosixPath(row.path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with _owned_stream(target, "xb") as stream:
                stream.write(row.data)
            _, raw, digest = self._read_staged(target, len(row.data))
            if raw != row.data or digest != row.sha256:
                _error("PROJECT_STAGING_CHANGED")
        self._sources_current()

    @staticmethod
    def _read_staged(path, maximum):
        target, before = _physical(path, regular=True)
        if before[2] > maximum:
            _error("PROJECT_OUTPUT_LIMIT")
        try:
            with _owned_stream(target, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != before[:2] or opened.st_nlink != 1:
                    _error("PROJECT_FILE_ALIAS_REJECTED")
                raw = stream.read(maximum + 1)
                after_fd = os.fstat(stream.fileno())
                if (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns) != before:
                    _error("PROJECT_STAGING_CHANGED")
        except ProjectSnapshotError:
            raise
        except Exception:
            _error("PROJECT_FILE_UNAVAILABLE")
        _, after = _physical(target, regular=True)
        if len(raw) > maximum or before != after:
            _error("PROJECT_OUTPUT_LIMIT" if len(raw) > maximum else "PROJECT_STAGING_CHANGED")
        return target, raw, hashlib.sha256(raw).hexdigest()

    def _capture_output(self, directory, scope):
        self._current()
        if scope is not self._scope or self._staged_root is None:
            _error("PROJECT_SCOPE_MISMATCH")
        root, identity = _physical(Path(directory).parent)
        if root != self._staged_root or identity[:2] != self._staged_identity:
            _error("PROJECT_STAGING_CHANGED")
        _, raw, digest = self._read_staged(Path(directory).joinpath(*PurePosixPath(self._output.path).parts),
            self._limits.output_bytes)
        self._current()
        captured = _CapturedArtifact(self, raw, digest)
        self._captured = captured
        return captured

    def execute(self, runner, code, input_json=None, *, cancel_event=None, launch_attempt_callback=None):
        if type(runner) is not mxc.MxcPythonRunner:
            _error("PROJECT_INVALID_EXECUTOR")
        with self._state_lock:
            if self._state != "prepared":
                _error("PROJECT_ALREADY_EXECUTED")
            self._state = "executing"
        self._cancel = cancel_event if cancel_event is not None else threading.Event()
        if not isinstance(self._cancel, threading.Event):
            self._state = "failed"; _error("PROJECT_INVALID_CANCEL_TOKEN")
        try:
            if sum(len(row.data) for row in self._inputs) > runner.policy.input_limit:
                _error("PROJECT_INPUT_LIMIT")
            self._sources_current()
            def before_resume(scope):
                if scope is not self._scope:
                    raise mxc_native.NativeFailure("MXC_SCOPE_REVOKED")
                try:
                    self._sources_current()
                except ProjectSnapshotError:
                    raise mxc_native.NativeFailure("MXC_SCOPE_REVOKED") from None
            result = runner.execute(code, input_json, scope=self._scope, cancel_event=self._cancel,
                authority_guard=self._guard, before_resume=before_resume,
                launch_attempt_callback=launch_attempt_callback, stage_inputs=self._stage_inputs,
                capture_output=self._capture_output)
            self._current()
            receipt = result._capture_receipt
            captured = self._captured
            if (type(receipt) is mxc.CaptureReceipt and type(captured) is _CapturedArtifact
                    and captured.owner is self and receipt.matches(self._scope, captured)
                    and result.state == "exited" and result.code == "MXC_OK"
                    and result.scratch_removed is True and result.resources_closed is True
                    and result.tree_reaped is True and result.limits_verified is True
                    and not mxc_native.has_pending_cleanup()):
                self._receipt, self._state = receipt, "captured"
            else:
                self._captured, self._state = None, "failed"
            return result
        except BaseException:
            self._captured, self._receipt, self._state = None, None, "failed"
            raise

    def publish(self):
        with self._state_lock:
            if self._state != "captured":
                _error("PROJECT_CAPTURE_REQUIRED")
            self._state = "publishing"
        try:
            with self._guard():
                with self._scope.files._lock:
                    self._sources_current(); self._target_current()
                    if (mxc_native.has_pending_cleanup() or type(self._receipt) is not mxc.CaptureReceipt
                            or type(self._captured) is not _CapturedArtifact
                            or self._captured.owner is not self
                            or not self._receipt.matches(self._scope, self._captured)):
                        _error("PROJECT_CLEANUP_UNCONFIRMED")
                    self._publication_attempted = True
                    raw = self._scope.files.write_bytes(self._output.target, self._captured.data,
                        overwrite=self._output.expected_sha256 is not None,
                        expected_hash=self._output.expected_sha256)
                    value = json.loads(raw)
                    artifact = value["artifact"]
                    if (type(artifact) is not dict or artifact.get("path") != self._output.target
                            or artifact.get("sha256") != self._captured.sha256
                            or type(artifact.get("bytes")) is not int
                            or artifact["bytes"] != len(self._captured.data)):
                        _error("PROJECT_INVALID_PUBLICATION_RECEIPT")
                    self._state = "published"
                    return {"schema": "aura.project_publication.v1", "snapshot_sha256": self._identity,
                        "artifact": artifact, "task_verified": False}
        except BaseException as failure:
            self._state = "publication_unknown" if self._publication_attempted else "publication_failed"
            if self._publication_attempted:
                if not isinstance(failure, Exception):
                    # Preserve cancellation/interrupt propagation while adding
                    # fixed host-origin uncertainty, never an error-string rule.
                    failure.effect_state, failure.publication_attempted = "unknown", True
                    raise
                raise ProjectPublicationUnknown() from None
            if isinstance(failure, ProjectSnapshotError) or not isinstance(failure, Exception):
                raise
            _error("PROJECT_PUBLICATION_FAILED")
        finally:
            self._captured, self._receipt = None, None
