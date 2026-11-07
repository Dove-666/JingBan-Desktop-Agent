"""Suspended-worker backend for authenticated remote queued Agent tasks.

Explicit trusted-host construction only; direct desktop keeps its Popen path.
This is not subprocess.Popen and
does not reopen a PID, create a second process-tree Job, replay an operation,
or convert stopped-only guardian evidence into an agent completion verdict.
"""
from __future__ import annotations

import ctypes as C
import io
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from .guardian_native import (GuardianError, Native, OwnedJob, OwnedProcess,
    SecurityAttributes, environment_block, has_pending_cleanup as native_cleanup_pending,
    reconcile_cleanup as reconcile_native_cleanup)
from .job_guardian import GuardianSession, PrivateRunStore, new_manifest, validate_manifest
from .job_queue import HostJobReference, GuardianRunBinding

_registry_lock = threading.RLock()
_owned_workers = {}
_failed_locations = {}


class _CloseAuditedNative(Native):
    """Audit legacy helper close calls without changing the frozen prototype.

    Helper APIs sometimes clear their wrapper fields after CloseHandle. Once
    such a close reports failure, do not reinterpret a later absence/no-op as
    proof or retry a possibly reused scalar handle. Keep the host run blocked.
    """
    def __init__(self, base):
        self.k, self.a, self.c = base.k, base.a, base.c
        self._close_lock = threading.Lock()
        self._unconfirmed_closes = set()

    def close(self, handle):
        if not handle:
            return True
        identity = getattr(handle, "value", handle)
        with self._close_lock:
            if identity in self._unconfirmed_closes:
                return False
            try: closed = self.k.CloseHandle(handle) != 0
            except Exception: closed = False
            if not closed:
                self._unconfirmed_closes.add(identity)
            return closed

    @property
    def unconfirmed_close(self):
        with self._close_lock: return bool(self._unconfirmed_closes)


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10:
        raise GuardianError("GUARDIAN_WORKER_TIMEOUT_INVALID")
    return float(value)


class PrivateGuardianLocation:
    """Host-prepared AppConfig child outside every declared model file scope."""
    def __init__(self, *args, **kwargs):
        raise TypeError("HOST_GUARDIAN_LOCATION_REQUIRED")

    @classmethod
    def prepare(cls, app_config_directory, *, model_scopes, native=None, retain_failed_owner=None):
        # A callback is a trusted host capability, not an importable model
        # dictionary. Static scopes require an explicit immutable tuple.
        if not callable(model_scopes) and (not isinstance(model_scopes, tuple) or not model_scopes):
            raise GuardianError("GUARDIAN_MODEL_SCOPES_REQUIRED")
        native = native or Native()
        if not isinstance(native, _CloseAuditedNative):
            native = _CloseAuditedNative(native)
        try:
            config = Path(app_config_directory).resolve(strict=True)
            scopes = cls._resolve_scopes(model_scopes)
            target = config / "host_job_guardian"
        except (ValueError, TypeError, OSError):
            raise GuardianError("GUARDIAN_STORAGE_SCOPE_INVALID") from None
        if not config.is_dir() or any(target == scope or target.is_relative_to(scope) or scope.is_relative_to(target) for scope in scopes):
            raise GuardianError("GUARDIAN_STORAGE_SCOPE_OVERLAP")
        value = object.__new__(cls)
        value._path, value._scopes, value._native = target, scopes, native
        value._scope_provider = model_scopes if callable(model_scopes) else None
        value._directory = None
        try:
            descriptor = native.descriptor()
            try:
                attrs = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
                created = native.k.CreateDirectoryW(str(target), C.byref(attrs))
                if not created and C.get_last_error() != 183:
                    raise GuardianError("GUARDIAN_STORAGE_CREATE_FAILED")
            finally:
                native.k.LocalFree(descriptor)
            # Retain the exact store object even if __init__ must close an
            # invalid scope and that audited close itself fails.
            checked = object.__new__(PrivateRunStore)
            value._directory = checked
            PrivateRunStore.__init__(checked, target, native)
            if native.unconfirmed_close:
                raise GuardianError("GUARDIAN_NATIVE_CLEANUP_UNCONFIRMED")
            return value
        except BaseException:
            if native.unconfirmed_close:
                retained = False
                if callable(retain_failed_owner):
                    try: retained = retain_failed_owner(value) is True
                    except Exception: pass
                if not retained:
                    with _registry_lock: _failed_locations[id(value)] = value
            raise

    @staticmethod
    def _resolve_scopes(source):
        try:
            values = source() if callable(source) else source
            if not isinstance(values, (tuple, list)) or not values:
                raise ValueError()
            return tuple(Path(item).resolve(strict=False) for item in values)
        except Exception:
            raise GuardianError("GUARDIAN_MODEL_SCOPES_UNAVAILABLE") from None

    def validate(self):
        if self._native.unconfirmed_close:
            raise GuardianError("GUARDIAN_NATIVE_CLEANUP_UNCONFIRMED")
        self._directory._assert_directory()
        if self._native.unconfirmed_close:
            raise GuardianError("GUARDIAN_NATIVE_CLEANUP_UNCONFIRMED")
        current = self._path.resolve(strict=True)
        scopes = self._resolve_scopes(self._scope_provider) if self._scope_provider is not None else self._scopes
        if any(current == scope or current.is_relative_to(scope) or scope.is_relative_to(current) for scope in scopes):
            raise GuardianError("GUARDIAN_STORAGE_SCOPE_OVERLAP")

    def close(self):
        self._directory.close()

    def __repr__(self): return "PrivateGuardianLocation([host AppConfig scope])"


class _TrackedPipe:
    """Binary FileIO with owned active-operation evidence for cleanup."""
    def __init__(self, native, handle, mode):
        self._native, self._raw_handle, self._mode = native, handle, mode
        self._lock = threading.Lock()
        self._active = 0
        self._closing = False
        self._close_failure = False
        self._descriptor = self._file = None

    def open(self):
        import msvcrt
        self._descriptor = msvcrt.open_osfhandle(self._raw_handle,
            os.O_BINARY | (os.O_WRONLY if self._mode == "wb" else os.O_RDONLY))
        self._raw_handle = 0  # The descriptor now owns the OS handle.
        self._file = io.FileIO(self._descriptor, mode=self._mode, closefd=True)
        self._descriptor = None  # FileIO now owns the descriptor.

    @property
    def closed(self):
        return self._file.closed if self._file is not None else not self._raw_handle and self._descriptor is None

    def fileno(self):
        if self._file is None:
            raise GuardianError("GUARDIAN_PIPE_NOT_OPEN")
        return self._file.fileno()

    def _operation(self, method, *args):
        with self._lock:
            if self._closing or self._file is None or self._file.closed:
                raise GuardianError("GUARDIAN_PIPE_CLOSED")
            self._active += 1
        try:
            return method(*args)
        finally:
            with self._lock: self._active -= 1

    def _method(self, name):
        if self._file is None:
            raise GuardianError("GUARDIAN_PIPE_NOT_OPEN")
        return getattr(self._file, name)

    def write(self, value): return self._operation(self._method("write"), value)
    def read(self, size=-1): return self._operation(self._method("read"), size)
    def flush(self): return self._operation(self._method("flush"))

    def close_checked(self):
        with self._lock:
            if self._active or self._close_failure:
                return False
            self._closing = True
            try:
                if self._file is not None:
                    self._file.close()
                    return self._file.closed is True
                if self._descriptor is not None:
                    os.close(self._descriptor)
                    self._descriptor = None
                if self._raw_handle:
                    if not self._native.k.CloseHandle(self._raw_handle): return False
                    self._raw_handle = 0
                return True
            except Exception:
                # FileIO.close can set closed=True and discard its descriptor
                # before reporting an OS close failure. There is no remaining
                # exact authority that a no-op second close could verify.
                self._close_failure = True
                return False

    def close(self):
        if not self.close_checked():
            raise GuardianError("GUARDIAN_PIPE_CLEANUP_UNCONFIRMED")

    def __repr__(self): return "OwnedWorkerPipe([binary])"


class OwnedWorkerTree:
    """The adapter's existing Job, never a second Job around a Popen/PID."""
    def __init__(self, process):
        self.process = process

    def close(self):
        self.process.request_stop()

    def confirm_closed(self, *, timeout_seconds=2):
        return self.process.cleanup(timeout_seconds=timeout_seconds)


class GuardianWorker:
    """Popen-shaped host object with a retained native initial thread.

    WorkerLifecycle must register THIS exact object, then attach ``tree``. A
    lifecycle callback may do that before manifest/ARM/resume. The isolated
    adapter itself never registers with AgentService or the production queue.
    """
    def __init__(self, *args, **kwargs):
        raise TypeError("HOST_SUSPENDED_WORKER_REQUIRED")

    @classmethod
    def create(cls, argv, *, cwd, location, job_id, reference, installation_id, launch_epoch,
               launch_attempt_callback, lifecycle_callback=None, phase_callback=None,
               manifest_commit_callback=None, environment=None, owns_location=False,
               stderr_to_null=False, job_active_limit=8):
        if (type(location) is not PrivateGuardianLocation or type(reference) is not HostJobReference
                or not callable(launch_attempt_callback)
                or not callable(lifecycle_callback)):
            raise GuardianError("GUARDIAN_WORKER_HOST_BINDING_INVALID")
        reference.validate()
        location.validate()
        if type(stderr_to_null) is not bool:
            raise GuardianError("GUARDIAN_WORKER_HOST_BINDING_INVALID")
        if reference.origin_peer_id is not None and not callable(manifest_commit_callback):
            raise GuardianError("GUARDIAN_DURABLE_BINDING_REQUIRED")
        if environment is not None:
            environment_block(environment)
            environment = dict(environment)
        identity_fields = ({"user_message_id": reference.user_message_id, "settings_scope": reference.settings_scope,
                            "origin_peer_id": reference.origin_peer_id} if reference.origin_peer_id is not None else {})
        # Validate the identity before allocating resources or attempting any
        # process launch. Actual creation time is substituted after creation.
        new_manifest(job_id=job_id, conversation_id=reference.conversation_id, generation=reference.generation,
            operation_id=reference.idempotency_key, installation_id=installation_id,
            launch_epoch=launch_epoch, process_creation_time=1, **identity_fields)
        with _registry_lock:
            if _owned_workers or _failed_locations or native_cleanup_pending():
                raise GuardianError("GUARDIAN_WORKER_BUSY")
            value = object.__new__(cls)
            value.native, value.location, value.reference = location._native, location, reference
            value._owns_location = owns_location is True
            value._stderr_to_null = stderr_to_null
            value._root = value._job = value.guardian = value.store = None
            value.stdin = value.stdout = value.stderr = None
            value.returncode = None
            value.manifest = None
            value.guardian_binding = None
            value._phase_callback = phase_callback
            value._writers = []
            value._lock, value._cleanup_lock = threading.RLock(), threading.Lock()
            value._confirmed_closed, value._pending = False, False
            value._root_signal_confirmed = value._guardian_signal_confirmed = False
            value._raw_handles = []
            value.tree = OwnedWorkerTree(value)
            _owned_workers[id(value)] = value
        try:
            value._phase("STORAGE_VALIDATED")
            value._prepare_pipes()
            value._job = OwnedJob(value.native, active_limit=job_active_limit)
            # This hook has authorization/attempt semantics and is not a
            # diagnostic callback. Its original exception propagates unchanged.
            launch_attempt_callback()
            value._phase("LAUNCH_ATTEMPT_MARKED")
            value._root = OwnedProcess.create(value.native, list(argv), cwd=cwd,
                inherited=[value._child_stdin, value._child_stdout, value._child_stderr],
                stdio=(value._child_stdin, value._child_stdout, value._child_stderr), suspended=True,
                environment=environment)
            value._phase("ROOT_CREATED_SUSPENDED")
            # Bind the same exact adapter at the earliest successful native
            # creation boundary, even if later Job assignment or ARM fails.
            lifecycle_callback(value)
            value._phase("LIFECYCLE_BOUND")
            value.native.check(value.native.k.AssignProcessToJobObject(value._job.handle, value._root.handle),
                               "GUARDIAN_JOB_ASSIGN_FAILED")
            if not value.native.in_job(value._root.handle, value._job.handle):
                raise GuardianError("GUARDIAN_JOB_ASSIGN_UNCONFIRMED")
            value._phase("JOB_BOUND")
            # No parent copies of child pipe endpoints survive into guardian
            # creation, and no guardian handles existed when root was created.
            value._close_child_ends()
            value.manifest = new_manifest(job_id=job_id, conversation_id=reference.conversation_id,
                generation=reference.generation, operation_id=reference.idempotency_key,
                installation_id=installation_id, launch_epoch=launch_epoch,
                process_creation_time=value.native.creation_time(value._root.handle), **identity_fields)
            value.store = PrivateRunStore.create(location._path, value.manifest, value.native)
            if value.native.unconfirmed_close:
                raise GuardianError("GUARDIAN_NATIVE_CLEANUP_UNCONFIRMED")
            value._phase("MANIFEST_DURABLE")
            if manifest_commit_callback is not None:
                # Authority hook, not progress telemetry: exceptions stop ARM
                # and Resume. A real queue binding is published after COMMIT.
                binding = manifest_commit_callback(dict(value.manifest))
                if type(binding) is not GuardianRunBinding or not binding.matches_manifest(value.manifest):
                    raise GuardianError("GUARDIAN_DURABLE_BINDING_UNCONFIRMED")
                value.guardian_binding = binding
                value._phase("QUEUE_BINDING_DURABLE")
            value.guardian = GuardianSession.arm(value.store, job=value._job, root=value._root)
            if value.native.unconfirmed_close:
                raise GuardianError("GUARDIAN_NATIVE_CLEANUP_UNCONFIRMED")
            value._phase("GUARDIAN_ARMED")
            return value
        except BaseException:
            try: value.cleanup(timeout_seconds=2)
            except BaseException: pass  # Preserve the original launch/hook error.
            raise

    def _phase(self, code):
        if self._phase_callback is not None:
            try: self._phase_callback(code)
            except Exception: pass

    def _worker_pipe(self):
        read, write = self.native.pipe()
        self._raw_handles.extend([read, write])
        # Worker payload may be large: blocking binary FileIO is bounded by
        # the existing StagedRunInput watchdog, which stops this exact Job.
        # Guardian bootstrap/ack pipes keep their separate PIPE_NOWAIT policy.
        mode, actual = C.c_uint32(0), C.c_uint32()
        self.native.check(self.native.k.SetNamedPipeHandleState(write, C.byref(mode), None, None), "GUARDIAN_WORKER_PIPE_POLICY_FAILED")
        self.native.check(self.native.k.GetNamedPipeHandleStateW(write, C.byref(actual), None, None, None, None, 0), "GUARDIAN_WORKER_PIPE_POLICY_FAILED")
        if actual.value != 0:
            raise GuardianError("GUARDIAN_WORKER_PIPE_POLICY_FAILED")
        return read, write

    def _prepare_pipes(self):
        self._child_stdin, host_stdin = self._worker_pipe()
        host_stdout, self._child_stdout = self._worker_pipe()
        targets = [("stdin", host_stdin, "wb"), ("stdout", host_stdout, "rb")]
        if self._stderr_to_null:
            self._child_stderr = self.native.k.CreateFileW("NUL", 0xC0000000, 3, None, 3, 0, None)
            if not self._child_stderr or self._child_stderr == C.c_void_p(-1).value:
                raise GuardianError("GUARDIAN_WORKER_STDERR_FAILED")
            self._raw_handles.append(self._child_stderr)
        else:
            host_stderr, self._child_stderr = self._worker_pipe()
            targets.append(("stderr", host_stderr, "rb"))
        for name, handle, mode in targets:
            # open_osfhandle transfers ownership, including its failure paths.
            self._raw_handles.remove(handle)
            pipe = _TrackedPipe(self.native, handle, mode)
            setattr(self, name, pipe)  # Strong ownership precedes conversion.
            pipe.open()

    def _close_child_ends(self):
        for name in ("_child_stdin", "_child_stdout", "_child_stderr"):
            handle = getattr(self, name, 0)
            if handle:
                if not self.native.k.CloseHandle(handle):
                    raise GuardianError("GUARDIAN_WORKER_PIPE_CLEANUP_UNCONFIRMED")
                self._raw_handles.remove(handle)
                setattr(self, name, 0)

    @property
    def pid(self): return self._root.pid if self._root is not None else None

    @property
    def _handle(self): return self._root.handle if self._root is not None else 0

    def resume(self):
        if self.guardian is None or not self.guardian.armed or self._pending or self._confirmed_closed:
            raise GuardianError("GUARDIAN_WORKER_NOT_ARMED")
        self.validate_private_scope()
        self._root.resume()
        self._phase("ROOT_RESUMED")

    def validate_private_scope(self):
        """Call synchronously INSIDE each existing host dispatch/ACK guard.

        This checks current private-file exclusion only, not the rest of the
        coordinator's authorization. It neither commits LF nor grants tools.
        """
        if self._pending or self._confirmed_closed:
            raise GuardianError("GUARDIAN_WORKER_NOT_ACTIVE")
        self.location.validate()

    def poll(self):
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            if self._root is None or not self._root.handle:
                return None
            if not self.native.signaled(self._root.handle):
                return None
            self._root_signal_confirmed = True
            code = C.c_uint32()
            method = self.native.k.GetExitCodeProcess
            method.argtypes, method.restype = [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int
            self.native.check(method(self._root.handle, C.byref(code)), "GUARDIAN_WORKER_EXIT_QUERY_FAILED")
            self.returncode = int(code.value)
            return self.returncode

    def wait(self, timeout=None):
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("INVALID_WORKER_WAIT_TIMEOUT")
        cached = self.poll()
        if cached is not None:
            return cached
        if self._root is None or not self._root.handle:
            raise GuardianError("GUARDIAN_WORKER_NOT_CREATED")
        milliseconds = 0xFFFFFFFF if timeout is None else min(0xFFFFFFFE, math.ceil(timeout * 1000))
        result = self.native.k.WaitForSingleObject(self._root.handle, milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired("host-owned guardian worker", timeout)
        if result != 0:
            raise GuardianError("GUARDIAN_WORKER_WAIT_FAILED")
        return self.poll()

    def kill(self):
        if self.poll() is None and self._root is not None:
            self.native.check(self.native.k.TerminateProcess(self._root.handle, 77), "GUARDIAN_WORKER_STOP_FAILED")

    terminate = kill

    def request_stop(self):
        # No waits and no registry lock: safe for an existing watchdog callback.
        if self._confirmed_closed:
            return
        if self.guardian is not None:
            try: self.guardian.stop()
            except Exception: pass
        if self._job is not None and self._job.handle:
            try: self._job.terminate()
            except Exception: pass
        try: self.kill()
        except Exception: pass
        self._phase("STOP_REQUESTED")

    def bind_writer(self, writer):
        if not callable(getattr(writer, "close", None)):
            raise GuardianError("GUARDIAN_WORKER_WRITER_INVALID")
        with self._lock:
            if self._pending or self._confirmed_closed or writer in self._writers:
                raise GuardianError("GUARDIAN_WORKER_WRITER_INVALID")
            self._writers.append(writer)

    def read_available(self, pipe=None):
        import msvcrt
        pipe = self.stdout if pipe is None else pipe
        if pipe is None or pipe.closed:
            return b""
        available = C.c_uint32()
        handle = msvcrt.get_osfhandle(pipe.fileno())
        if not self.native.k.PeekNamedPipe(handle, None, 0, None, C.byref(available), None):
            return b""
        return pipe.read(min(available.value, 65536)) if available.value else b""

    def cleanup(self, *, timeout_seconds=2):
        timeout_seconds = _timeout(timeout_seconds)
        if not self._cleanup_lock.acquire(blocking=False):
            return False
        try:
            if self._confirmed_closed:
                return True
            with self._lock:
                self._pending = True
                writers = tuple(self._writers)
            self.request_stop()
            deadline = time.monotonic() + timeout_seconds
            def remaining(): return max(0, deadline - time.monotonic())
            for writer in writers:
                try: joined = writer.close(timeout_seconds=min(1, remaining())) is True
                except Exception: joined = False
                if not joined: return False
            if self.native.unconfirmed_close:
                return False
            if native_cleanup_pending():
                return False
            if self._root is not None and self._root.handle:
                try: self.wait(timeout=remaining())
                except Exception: return False
            elif self._root is not None and not self._root_signal_confirmed:
                return False
            if self._job is not None and self._job.handle:
                try:
                    if self._root is not None and self._root.handle:
                        if not self.native.confirm_closed(self._job.handle, self._root.handle, timeout=remaining()): return False
                    elif not self.native.job_empty(self._job.handle): return False
                except Exception: return False
            if self.guardian is not None and self.guardian.process.handle:
                if self.native.k.WaitForSingleObject(self.guardian.process.handle, math.ceil(remaining() * 1000)) != 0:
                    return False
                self._guardian_signal_confirmed = True
            elif self.guardian is not None and not self._guardian_signal_confirmed:
                return False
            for pipe in (self.stdin, self.stdout, self.stderr):
                if pipe is not None and pipe.close_checked() is not True:
                    return False
            # Close-called is not proof. Clear each reference only after the
            # underlying CloseHandle succeeds; preserve all unknown resources.
            for handle in list(self._raw_handles):
                if not self.native.k.CloseHandle(handle): return False
                self._raw_handles.remove(handle)
            if self.guardian is not None:
                for obj, name in ((self.guardian.process, "thread"), (self.guardian.process, "handle"), (self.guardian, "stop_event")):
                    handle = getattr(obj, name)
                    if handle:
                        if not self.native.k.CloseHandle(handle): return False
                        setattr(obj, name, 0)
            if self.store is not None and self.store.directory:
                if not self.native.k.CloseHandle(self.store.directory): return False
                self.store.directory = 0
            if self._owns_location and self.location._directory.directory:
                if not self.native.k.CloseHandle(self.location._directory.directory): return False
                self.location._directory.directory = 0
            for obj, name in ((self._root, "thread"), (self._root, "handle"), (self._job, "handle")):
                handle = getattr(obj, name, 0)
                if handle:
                    if not self.native.k.CloseHandle(handle): return False
                    setattr(obj, name, 0)
            self._confirmed_closed = True
            with _registry_lock: _owned_workers.pop(id(self), None)
            self._phase("CLEANUP_CONFIRMED")
            return True
        except Exception:
            return False
        finally:
            if not self._confirmed_closed:
                self._phase("CLEANUP_PENDING")
            self._cleanup_lock.release()

    def __repr__(self): return "GuardianWorker([exact host-owned suspended-worker adapter])"


def pending_cleanup_count():
    with _registry_lock:
        return sum(value._pending for value in _owned_workers.values()) + len(_failed_locations)


def owns_location(location):
    """Host failure-path ownership query; no serialized authority returned."""
    with _registry_lock:
        return any(value.location is location for value in _owned_workers.values())


def reconcile_cleanup(*, timeout_seconds=.2):
    _timeout(timeout_seconds)
    # Native failed CreateProcess cleanup is independent of worker tool replay.
    reconcile_native_cleanup()
    with _registry_lock:
        candidates = [value for value in _owned_workers.values() if value._pending]
    confirmed = all(value.cleanup(timeout_seconds=timeout_seconds) for value in candidates)
    with _registry_lock: return confirmed and not _failed_locations


def consume_restarted_stopped(location, expected_manifest):
    """Authenticate/CAS stopped-only after restart. No queue or process launch."""
    if type(location) is not PrivateGuardianLocation:
        raise GuardianError("GUARDIAN_WORKER_HOST_BINDING_INVALID")
    location.validate()
    validate_manifest(expected_manifest)
    store = None
    try:
        store = PrivateRunStore(location._path / expected_manifest["run_nonce"], location._native)
        return store.consume_closure(expected_manifest=expected_manifest)
    except GuardianError:
        return None
    finally:
        if store is not None: store.close()
