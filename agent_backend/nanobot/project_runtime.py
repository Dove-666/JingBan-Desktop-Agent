"""Host-bound offline project tool. No IO, native launch or enable on import.

Only trusted worker code supplies scope/config/identity/guard callbacks. The
model supplies bounded Python/data and explicit relative input/output versions.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from uuid import UUID

if __package__:
    from . import mxc_execution as mxc, mxc_native
    from .container_execution import HostExecutionScope
    from .project_snapshot import ProjectSnapshot, ProjectInput, ProjectOutput, ProjectSnapshotError
else:
    import mxc_execution as mxc
    import mxc_native
    from container_execution import HostExecutionScope
    from project_snapshot import ProjectSnapshot, ProjectInput, ProjectOutput, ProjectSnapshotError

_ROOT = Path(__file__).resolve().parents[2]
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_RESULT_ISSUER = object()
_OWNERS = set()
_OWNERS_LOCK = threading.RLock()


class ProjectRuntimeError(ValueError):
    def __init__(self, code, *, unknown=False, safe_no_publication=False):
        self.code, self.effect_state = code, "unknown" if unknown else "none"
        self.safe_no_publication = safe_no_publication is True and not unknown
        super().__init__(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uuid(value):
    try: return type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError, AttributeError): return False


def _host_modules():
    if str(_ROOT) not in sys.path:
        if os.environ.get("JINGBAN_PACKAGED") == "1": sys.path.append(str(_ROOT))
        else: sys.path.insert(0, str(_ROOT))
    from desktop_pet.agents import execution_runtime
    from desktop_pet.agents.execution_attempt import ExecutionAttempts
    return execution_runtime, ExecutionAttempts


def checked_configuration(value, identity):
    if (type(value) is not dict or set(value) != {"schema", "engine", "enabled", "policy", "runtime_pins"}
            or type(value.get("enabled")) is not bool or type(identity) is not str or not _HASH.fullmatch(identity)):
        raise ProjectRuntimeError("PROJECT_HOST_CONFIGURATION_INVALID")
    fixed = {"schema": 1, "engine": "mxc", "enabled": value["enabled"], "policy": asdict(mxc.MxcPolicy()),
        "runtime_pins": {"codex_sha256": mxc.CODEX_SHA256, "codex_version": mxc.CODEX_VERSION,
                         "python_sha256": mxc.PYTHON_SHA256}}
    try:
        encoded = _canonical(value)
        if encoded != _canonical(fixed) or _sha(encoded) != identity: raise ValueError()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ProjectRuntimeError("PROJECT_HOST_CONFIGURATION_INVALID") from None
    return json.loads(encoded)


def checked_authority(value, *, conversation_id, request_id):
    if (type(value) is not dict or set(value) != {"version", "conversation_id", "generation", "request_id", "settings_scope"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not all(_uuid(value.get(key)) for key in ("conversation_id", "generation", "request_id"))
            or type(value.get("settings_scope")) is not str or not _HASH.fullmatch(value["settings_scope"])
            or value["conversation_id"] != conversation_id):
        raise ProjectRuntimeError("PROJECT_HOST_AUTHORITY_INVALID")
    try:
        if str(UUID(request_id)) != value["request_id"]: raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ProjectRuntimeError("PROJECT_HOST_AUTHORITY_INVALID") from None
    return dict(value)


def project_scope_conflicts(paths, *, host_private_root, host_configuration, private_paths=()):
    """Pure path-overlap rule shared by execution and capability metadata.

    Resolving paths neither creates a runtime owner nor grants access. Callers
    supply their own already-authorized workspace/grant snapshot; a False
    result is not native-readiness evidence or an execution capability.
    """
    protected = [Path(host_private_root).resolve(), Path(host_configuration).resolve(),
        Path(__file__).resolve().parent, _ROOT / "desktop_pet/agents",
        mxc.CODEX.parent.resolve(), mxc.PYTHON_ROOT.resolve(), *private_paths]
    return any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
        for left in (Path(path).resolve() for path in paths) for right in protected)


class ProjectArtifactResult(str):
    """A real host publication, still JSON-compatible with native SDK tools."""
    def __new__(cls, issuer, publication):
        if issuer is not _RESULT_ISSUER:
            raise TypeError("PROJECT_PUBLICATION_RECEIPT_REQUIRED")
        artifact = publication["artifact"]
        value = {"schema": "aura.project_artifact.v1", "written": artifact["path"],
            "sha256": artifact["sha256"], "bytes": artifact["bytes"], "artifact": dict(artifact),
            "snapshot_sha256": publication["snapshot_sha256"], "task_verified": False}
        result = str.__new__(cls, json.dumps(value, ensure_ascii=False, allow_nan=False))
        result._issuer, result._encoded = issuer, _canonical(value)
        return result

    def __setattr__(self, name, value):
        if hasattr(self, name): raise AttributeError("PROJECT_PUBLICATION_RECEIPT_IMMUTABLE")
        super().__setattr__(name, value)

    def __reduce__(self): raise TypeError("PROJECT_PUBLICATION_RECEIPT_NOT_SERIALIZABLE")


def published_artifact(value):
    if type(value) is not ProjectArtifactResult or getattr(value, "_issuer", None) is not _RESULT_ISSUER:
        return None
    return json.loads(value._encoded)


@dataclass
class _Run:
    key: str
    fingerprint: str
    revision: str
    epoch: int
    grants: str
    cancel: threading.Event = field(default_factory=threading.Event)
    scope: object = None
    runner: object = None
    plan: object = None
    private: Path | None = None
    private_identity: tuple | None = None
    attempts: list = field(default_factory=list)
    attempt_owner: object = field(default_factory=object)
    result: object = None
    cleanup_unknown: bool = False
    finished: bool = False
    work: object = None
    cleanup_work: object = None


class _OwnedWork:
    """An async wrapper/Future cannot prove its underlying thread terminated."""
    def __init__(self, callback):
        self.value, self.failure = None, None
        self.started, self.closed, self.unknown = False, False, False
        def run():
            try: self.value = callback()
            except BaseException as failure: self.failure = failure
        self.thread = threading.Thread(target=run, name="aura-owned-project", daemon=False)

    def start(self):
        try:
            self.thread.start()
            self.started = True
        except BaseException:
            # A host failure around Thread.start can happen after creation.
            # Retain its exact object; absence of an ident is not a receipt.
            self.unknown = True
            raise ProjectRuntimeError("PROJECT_EXECUTION_CLEANUP_UNCONFIRMED", unknown=True) from None

    def confirm_closed(self):
        if self.closed: return True
        if self.unknown or not self.started: return False
        try:
            if self.thread.is_alive(): return False
            self.thread.join(timeout=0)
            if self.thread.is_alive(): return False
            self.closed = True
            return True
        except BaseException:
            self.unknown = True
            return False

    async def wait(self, cancel):
        interruption = None
        while not self.confirm_closed():
            if self.unknown:
                raise ProjectRuntimeError("PROJECT_EXECUTION_CLEANUP_UNCONFIRMED", unknown=True)
            try: await asyncio.sleep(.005)
            except asyncio.CancelledError as failure:
                cancel.set()
                if interruption is None: interruption = failure
        if interruption is not None: raise interruption
        if self.failure is not None: raise self.failure
        return self.value


class ProjectRuntime:
    def __init__(self, *, files, authority, configuration, configuration_identity,
                 current_goal_revision, authorization_current, parent_cancellation,
                 call_identity, authority_guard, known_secrets=()):
        if (not all(callable(item) for item in (current_goal_revision, authorization_current, call_identity, authority_guard))
                or not isinstance(parent_cancellation, threading.Event)):
            raise ProjectRuntimeError("PROJECT_HOST_CONTEXT_INVALID")
        self._configuration = checked_configuration(configuration, configuration_identity)
        if self._configuration["enabled"] is not True:
            raise ProjectRuntimeError("PROJECT_EXECUTION_DISABLED")
        self._host, attempts = _host_modules()
        self._authority = checked_authority(authority, conversation_id=authority.get("conversation_id") if isinstance(authority, dict) else None,
            request_id=authority.get("request_id") if isinstance(authority, dict) else None)
        self._files, self._identity = files, configuration_identity
        self._revision, self._authorized = current_goal_revision, authorization_current
        self._parent_cancel, self._call = parent_cancellation, call_identity
        self._guard, self._secrets = authority_guard, tuple(known_secrets)
        self._lock, self._serial = threading.RLock(), asyncio.Lock()
        self._epoch, self._closed, self._publication_unknown = 0, False, False
        self._entries, self._seen, self._tasks = OrderedDict(), {}, set()
        self._attempts = attempts()
        if not self._configuration_current():
            raise ProjectRuntimeError("PROJECT_HOST_CONFIGURATION_CHANGED")
        with _OWNERS_LOCK:
            if any(owner._has_unknown() for owner in _OWNERS):
                raise ProjectRuntimeError("PROJECT_EXECUTION_CLEANUP_UNCONFIRMED", unknown=True)
            _OWNERS.add(self)

    def __repr__(self): return "ProjectRuntime([host-bound offline execution])"

    def _configuration_current(self):
        try:
            value = self._host.capture_execution_runtime()
            return (self._host.valid_execution_runtime_snapshot(value) and value.identity == self._identity
                    and value.configuration == self._configuration and value.configuration["enabled"] is True)
        except Exception: return False

    def assert_scopes(self, *, extra_grant=None):
        with self._lock:
            private = [entry.private for entry in self._entries.values() if entry.private is not None]
        try:
            with self._files._lock:
                paths = [self._files.root, *[path for path, _ in (*self._files._read_roots, *self._files._write_roots)]]
                if extra_grant is not None:
                    if type(extra_grant) is not dict or type(extra_grant.get("path")) is not str: raise ValueError()
                    paths.append(Path(extra_grant["path"]).resolve())
                if project_scope_conflicts(paths, host_private_root=self._host._ROOT,
                        host_configuration=self._host._CONFIG, private_paths=private): raise ValueError()
        except Exception:
            raise ProjectRuntimeError("PROJECT_PRIVATE_SCOPE_CONFLICT", safe_no_publication=True) from None

    def _grants(self):
        with self._files._lock:
            grants = [(access, str(path), directory)
                for access, roots in (("read", self._files._read_roots), ("write", self._files._write_roots))
                for path, directory in roots]
        return _sha(_canonical([self._authority["settings_scope"], self._identity, str(self._files.root), sorted(grants)]))

    def _current(self, entry):
        try:
            with self._guard():
                with self._lock:
                    if (self._closed or self._parent_cancel.is_set() or entry.cancel.is_set()
                            or entry.epoch != self._epoch or self._revision() != entry.revision
                            or self._authorized() is not True): return False
                self.assert_scopes()
                return self._configuration_current() and self._grants() == entry.grants
        except Exception: return False

    def invalidate(self):
        with self._lock:
            self._epoch += 1
            entries = list(self._entries.values())
        for entry in entries: entry.cancel.set()

    def _has_unknown(self):
        with self._lock:
            return self._publication_unknown or any(entry.cleanup_unknown or
                any(work is not None and work.unknown for work in (entry.work, entry.cleanup_work))
                for entry in self._entries.values())

    def assert_parent_ready(self):
        with _OWNERS_LOCK:
            if any(owner._has_unknown() for owner in _OWNERS) or mxc_native.has_pending_cleanup():
                code = "PROJECT_PUBLICATION_UNCONFIRMED" if self._publication_unknown else "PROJECT_EXECUTION_CLEANUP_UNCONFIRMED"
                raise ProjectRuntimeError(code, unknown=True)
        if any(work is not None and not work.confirm_closed()
               for entry in self._entries.values() for work in (entry.work, entry.cleanup_work)):
            raise ProjectRuntimeError("PROJECT_EXECUTION_ACTIVE")

    def _launch_attempt(self, entry):
        if not self._current(entry): raise ProjectRuntimeError("PROJECT_PARENT_INACTIVE")
        attempt = self._attempts.begin(self._authority["conversation_id"])
        entry.attempts.append(attempt)
        if not self._attempts.bind(attempt, entry.attempt_owner): raise ProjectRuntimeError("PROJECT_LAUNCH_ACCOUNTING_FAILED")
        self._attempts.mark_launch_attempted(attempt)

    def _close_private(self, entry):
        if entry.runner is not None and (entry.runner._pending or mxc_native.has_pending_cleanup()):
            entry.cleanup_unknown = True
            return False
        if entry.private is not None:
            try:
                path, identity = mxc._physical(entry.private)
                if identity != entry.private_identity: raise ValueError()
                os.rmdir(path)
                entry.private = None
            except Exception:
                entry.cleanup_unknown = True
                return False
        entry.cleanup_unknown = False
        for attempt in entry.attempts: self._attempts.finish(attempt)
        return True

    def _compute(self, entry, code, input_json, inputs, output):
        try:
            entry.scope = HostExecutionScope(self._authority["conversation_id"], self._authority["generation"],
                self._authority["request_id"], self._files, grant_revision=entry.grants,
                is_current=lambda: self._current(entry))
            entry.plan = ProjectSnapshot.prepare(entry.scope, inputs, output, goal_revision=_sha(entry.revision),
                goal_is_current=lambda revision: revision == _sha(entry.revision) and self._current(entry),
                authority_guard=self._guard)
            entry.private = Path(tempfile.mkdtemp(prefix="aura-project-runtime-")).absolute()
            _, entry.private_identity = mxc._physical(entry.private)
            self.assert_scopes()
            entry.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=entry.private, known_secrets=self._secrets)
            result = entry.plan.execute(entry.runner, code, input_json, cancel_event=entry.cancel,
                launch_attempt_callback=lambda: self._launch_attempt(entry))
            if not self._close_private(entry):
                raise ProjectRuntimeError("PROJECT_EXECUTION_CLEANUP_UNCONFIRMED", unknown=True)
            if result.code != "MXC_OK" or result._capture_receipt is None:
                code = result.code if type(result.code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", result.code) else "PROJECT_EXECUTION_FAILED"
                raise ProjectRuntimeError(code, safe_no_publication=(
                    code not in {"MXC_SCOPE_REVOKED", "MXC_CANCELLED", "MXC_CLEANUP_PENDING", "MXC_CLEANUP_UNCONFIRMED"}
                    and result.tree_reaped is True and result.resources_closed is True
                    and result.scratch_removed is True and not mxc_native.has_pending_cleanup()
                    and not entry.plan.publication_attempted))
            publication = entry.plan.publish()
            return ProjectArtifactResult(_RESULT_ISSUER, publication)
        except BaseException as failure:
            if (getattr(failure, "effect_state", None) == "unknown" and getattr(failure, "publication_attempted", False) is True
                    or entry.plan is not None and entry.plan.publication_attempted):
                self._publication_unknown = True
                if not isinstance(failure, Exception):
                    failure.effect_state, failure.publication_attempted = "unknown", True
                    raise
                raise ProjectRuntimeError("PROJECT_PUBLICATION_UNCONFIRMED", unknown=True) from None
            raise
        finally:
            self._close_private(entry)
            entry.finished = True
            if not entry.cleanup_unknown: entry.plan = None

    @staticmethod
    def _arguments(code, input_json, inputs, output):
        try:
            payload = mxc._payload(code, input_json)
            if type(inputs) is not list or not 1 <= len(inputs) <= 16: raise ValueError()
            if any(type(row) is not dict or set(row) != {"path", "expected_sha256"} for row in inputs): raise ValueError()
            if (type(output) is not dict or not {"path", "target"} <= set(output)
                    or set(output) - {"path", "target", "expected_sha256"}): raise ValueError()
            if any(type(row["path"]) is not str or not 1 <= len(row["path"]) <= 512
                   or type(row["expected_sha256"]) is not str or not _HASH.fullmatch(row["expected_sha256"])
                   for row in inputs): raise ValueError()
            if any(type(output[key]) is not str or not 1 <= len(output[key]) <= 512 for key in ("path", "target")):
                raise ValueError()
            if ("expected_sha256" in output and (type(output["expected_sha256"]) is not str
                    or not _HASH.fullmatch(output["expected_sha256"]))): raise ValueError()
            rows = [ProjectInput(**row) for row in inputs]
            target = ProjectOutput(**output)
            fingerprint = hashlib.sha256(payload + _canonical([inputs, output]).encode()).hexdigest()
            return rows, target, fingerprint
        except Exception:
            raise ProjectRuntimeError("PROJECT_ARGUMENTS_INVALID") from None

    async def compute_project_artifact(self, code, inputs, output, input_json=None):
        rows, target, fingerprint = self._arguments(code, input_json, inputs, output)
        async with self._serial:
            self.assert_parent_ready()
            with self._guard():
                self.assert_scopes()
                revision, action = self._revision(), self._call()
                if (type(revision) is not str or not 1 <= len(revision) <= 128
                        or type(action) is not str or not _HASH.fullmatch(action)):
                    raise ProjectRuntimeError("PROJECT_HOST_CONTEXT_INVALID")
                key = _sha(_canonical([self._authority, revision, action, self._epoch]))
                if key in self._seen:
                    if self._seen[key] != fingerprint: raise ProjectRuntimeError("PROJECT_CALL_IDENTITY_CONFLICT")
                    entry = self._entries.get(key)
                    if entry is None or entry.result is None: raise ProjectRuntimeError("PROJECT_RESULT_NOT_REPLAYABLE")
                    if not self._current(entry): raise ProjectRuntimeError("PROJECT_PARENT_INACTIVE")
                    return entry.result
                entry = _Run(key, fingerprint, revision, self._epoch, self._grants())
                if not self._current(entry): raise ProjectRuntimeError("PROJECT_PARENT_INACTIVE")
                self._entries[key], self._seen[key] = entry, fingerprint
            owner = asyncio.current_task()
            work = entry.work = _OwnedWork(lambda: self._compute(entry, code, input_json, rows, target))
            self._tasks.add(owner)
            try:
                work.start()
                entry.result = await work.wait(entry.cancel)
                return entry.result
            except asyncio.CancelledError:
                entry.cancel.set()
                raise
            except ProjectSnapshotError as failure:
                if self._parent_cancel.is_set(): raise asyncio.CancelledError from None
                if getattr(failure, "publication_attempted", False) is True:
                    raise ProjectRuntimeError("PROJECT_PUBLICATION_UNCONFIRMED", unknown=True) from None
                raise ProjectRuntimeError(str(failure)) from None
            except ProjectRuntimeError: raise
            except Exception:
                raise ProjectRuntimeError("PROJECT_EXECUTION_FAILED") from None
            finally:
                if work.confirm_closed(): self._tasks.discard(owner)
                else: entry.cleanup_unknown = True
                if entry.cleanup_unknown:
                    # Retained in _entries and _OWNERS until exact host cleanup.
                    pass
                while len(self._entries) > 16:
                    key, old = next(iter(self._entries.items()))
                    if not old.finished or old.cleanup_unknown: break
                    self._entries.pop(key)

    async def aclose(self):
        self._closed = True
        self.invalidate()
        interruption = None
        for entry in list(self._entries.values()):
            if entry.work is not None:
                try:
                    await entry.work.wait(entry.cancel)
                except asyncio.CancelledError as failure:
                    if interruption is None: interruption = failure
                except BaseException:
                    if not entry.work.confirm_closed(): entry.cleanup_unknown = True
            if entry.cleanup_unknown and entry.runner is not None and entry.work.confirm_closed():
                try:
                    if entry.cleanup_work is None:
                        entry.cleanup_work = _OwnedWork(lambda entry=entry: entry.runner.reconcile_cleanup(scope=entry.scope))
                        entry.cleanup_work.start()
                    await entry.cleanup_work.wait(entry.cancel)
                    self._close_private(entry)
                except asyncio.CancelledError as failure:
                    if interruption is None: interruption = failure
                    if entry.cleanup_work is not None and entry.cleanup_work.confirm_closed(): self._close_private(entry)
                except BaseException:
                    entry.cleanup_unknown = True
        self.assert_parent_ready()
        self._tasks.clear()
        with _OWNERS_LOCK: _OWNERS.discard(self)
        if interruption is not None: raise interruption
        return {"schema": "aura.project_runtime_closure.v1", "closed": True}
