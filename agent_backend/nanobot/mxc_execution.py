"""Optional, host-scoped native Windows Python backend. Disabled by default.

No tool registration, model calls, authentication, global configuration or
native IO on import/construction. Registry read and soft disk monitoring are
explicit residual boundaries, not equivalent to an isolated Linux container.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time

if __package__:
    from .container_execution import HostExecutionScope, AuthorizedInput, _no_aliases, _payload, _safe_name
    from .redaction import redact_text
    from . import mxc_native
else:
    from container_execution import HostExecutionScope, AuthorizedInput, _no_aliases, _payload, _safe_name
    from redaction import redact_text
    import mxc_native


# Private, signature/hash-verified copy: app updates may remove their old bin
# directory. The explicit host pin is never inferred from PATH or auto-updated.
CODEX = Path(r"E:\Tools\AURA\mxc-runtime\0.166.0-alpha.5.1-9cbcc600be53\codex.exe")
CODEX_SHA256 = "9cbcc600be53a3aa18c1d55b36b462ebd2844ac16e24b991654774b0819599d5"
CODEX_VERSION = "codex-cli 0.166.0-alpha.5.1"


def _python_runtime_root():
    """Locate only the fixed managed runtime in this user's roaming directory.

    This chooses no interpreter from PATH and performs no filesystem discovery.
    The existing executable hash pin and explicit host-review gate still apply.
    """
    value = os.environ.get("APPDATA")
    roaming = Path(value) if value and Path(value).is_absolute() else Path.home() / "AppData" / "Roaming"
    return roaming / "uv" / "python" / "cpython-3.12.13-windows-x86_64-none"


PYTHON_ROOT = _python_runtime_root()
PYTHON_SHA256 = "0dfbe445df3da9f94456d4e91d8feda5af587fe3cfb748e4c9fb62544909d385"
SYSTEM32 = Path(r"C:\Windows\System32")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class MxcExecutionError(ValueError):
    pass


def _error(code):
    raise MxcExecutionError(code) from None


def _digest_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _physical(path):
    try:
        target = _no_aliases(path)
        info = target.lstat()
        return target, (info.st_dev, info.st_ino)
    except Exception:
        _error("MXC_PATH_ALIAS_OR_UNAVAILABLE")


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    # Reject lone surrogates, coercions, nonfinite float exponents and deeply
    # nested values using the same bounded strict input contract.
    _payload("pass", value)
    return value


@dataclass(frozen=True)
class MxcPolicy:
    desktop_boundary: str = "win32k_spawn_v1"
    process_memory_mb: int = 256
    job_memory_mb: int = 512
    process_limit: int = 12
    cpu_rate_percent: int = 25
    cpu_seconds: float = 10
    timeout_seconds: float = 30
    stdout_limit: int = 262144
    stderr_limit: int = 16384
    input_limit: int = 4 * 1024 * 1024
    scratch_limit: int = 8 * 1024 * 1024
    scratch_entries: int = 256

    def validate(self):
        if self.desktop_boundary != "win32k_spawn_v1":
            _error("MXC_INVALID_POLICY")
        for value, low, high in ((self.process_memory_mb, 96, 1024), (self.job_memory_mb, 256, 2048), (self.process_limit, 4, 32), (self.cpu_rate_percent, 1, 100), (self.stdout_limit, 128, 1048576), (self.stderr_limit, 128, 65536), (self.input_limit, 1, 16 * 1024 * 1024), (self.scratch_limit, 1024, 32 * 1024 * 1024), (self.scratch_entries, 8, 1024)):
            if type(value) is not int or not low <= value <= high:
                _error("MXC_INVALID_POLICY")
        if self.process_memory_mb > self.job_memory_mb:
            _error("MXC_INVALID_POLICY")
        for value in (self.cpu_seconds, self.timeout_seconds):
            if type(value) not in (int, float) or not .1 <= value <= 120:
                _error("MXC_INVALID_POLICY")


@dataclass(frozen=True)
class MxcExecutionResult:
    state: str
    code: str
    value: object = None
    tree_reaped: bool = True
    scratch_removed: bool = True
    limits_verified: bool = False
    task_verified: bool = False
    resources_closed: bool = True
    _capture_receipt: object = field(default=None, repr=False, compare=False)

    def to_json(self):
        return json.dumps({"state": self.state, "code": self.code, "value": self.value, "tree_reaped": self.tree_reaped, "scratch_removed": self.scratch_removed, "limits_verified": self.limits_verified, "task_verified": False, "resources_closed": self.resources_closed}, ensure_ascii=False, allow_nan=False)


_CAPTURE_ISSUER = object()


@dataclass(frozen=True, init=False, repr=False)
class CaptureReceipt:
    """Host-only proof for one exact in-memory capture, never a model argument.

    Construction occurs only after the owning execute call has closed native
    resources, runtime pins and scratch. A serialized result has no authority.
    """
    _issuer: object
    _invocation: object
    _scope: object
    _job: object
    _native: object
    _captured: object

    def __init__(self, issuer, invocation, scope, job, native, captured):
        if issuer is not _CAPTURE_ISSUER:
            _error("MXC_INVALID_CAPTURE_RECEIPT")
        for name, value in (("_issuer", issuer), ("_invocation", invocation), ("_scope", scope),
                            ("_job", job), ("_native", native), ("_captured", captured)):
            object.__setattr__(self, name, value)

    def matches(self, scope, captured):
        return (type(self) is CaptureReceipt and self._issuer is _CAPTURE_ISSUER
                and self._scope is scope and self._captured is captured
                and self._job.scope is scope and self._native.tree_reaped is True
                and self._native.resources_closed is True and self._native._cleanup_lease is None)

    def __repr__(self):
        return "CaptureReceipt([host-owned])"

    def __reduce__(self):
        raise TypeError("MXC_CAPTURE_RECEIPT_NOT_SERIALIZABLE")


@dataclass(repr=False)
class _Job:
    root: Path
    identity: tuple
    nonce: str
    scope: HostExecutionScope
    workspace_identity: tuple
    task: Path
    task_identity: tuple

    @property
    def profile(self):
        return "aura-mxc-" + self.nonce


@dataclass(repr=False)
class _PendingCleanup:
    job: _Job
    pins: tuple
    lease: object
    tree_reaped: bool


class MxcPythonRunner:
    def __init__(self, policy=None, *, live_enabled=False, scratch_parent=None, known_secrets=()):
        self.policy = policy or MxcPolicy()
        self.live_enabled = live_enabled is True
        self.scratch_parent = Path(scratch_parent) if scratch_parent is not None else None
        self._secrets = tuple(item for item in known_secrets if type(item) is str and item)
        self._native_run = mxc_native.run
        self._pending = []
        self._cleanup_lock = threading.RLock()

    def capability(self):
        return {"available": False, "runtime_verified": False, "enabled": self.live_enabled,
                "code": "MXC_REQUIRES_HOST_REVIEW", "model_calls": False, "file_exports": False,
                "authority_contract": {"shared_host_guard_optional": True, "unguarded_callback_is_atomic": False},
                "residual_boundaries": {"registry_read": True, "system32_readonly": True,
                    "trusted_python_runtime_readonly": True, "hard_disk_quota": False,
                    "scratch_quota": "soft_watch_and_stop", "shared_windows_kernel": True}}

    @staticmethod
    def _scope(scope, expected=None):
        if not isinstance(scope, HostExecutionScope) or scope.files.root != scope._root:
            _error("MXC_SCOPE_MISMATCH")
        try:
            current = scope._is_current() is True
        except Exception:
            current = False
        if not current:
            _error("MXC_SCOPE_REVOKED")
        _, identity = _physical(scope._root)
        if expected is not None and identity != expected:
            _error("MXC_SCOPE_CHANGED")
        return identity

    @staticmethod
    def _pins():
        # Keep read-only, non-delete-sharing handles open through both native
        # launches. An updater cannot replace the checked image in between.
        import ctypes as C
        import msvcrt
        kernel = C.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p]
        kernel.CreateFileW.restype = C.c_void_p
        kernel.CloseHandle.argtypes = [C.c_void_p]
        locks = []
        try:
            for path, expected in ((CODEX, CODEX_SHA256), (PYTHON_ROOT / "python.exe", PYTHON_SHA256)):
                _, before = _physical(path)
                handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
                if not handle or handle == C.c_void_p(-1).value:
                    _error("MXC_RUNTIME_PIN_UNAVAILABLE")
                try:
                    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                except Exception:
                    try: closed = kernel.CloseHandle(handle) != 0
                    except BaseException: closed = False
                    if not closed: mxc_native.retain_failed_close(kernel, handle)
                    raise
                try:
                    stream = os.fdopen(descriptor, "rb")
                except BaseException:
                    try: os.close(descriptor)
                    except BaseException: mxc_native.retain_failed_close(kernel, handle, descriptor)
                    raise
                locks.append(stream)
                info = os.fstat(stream.fileno())
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                if (info.st_dev, info.st_ino) != before or digest.hexdigest() != expected:
                    _error("MXC_RUNTIME_PIN_MISMATCH")
            _physical(PYTHON_ROOT)
            _physical(SYSTEM32)
            return locks
        except BaseException:
            MxcPythonRunner._close_pins(locks)
            raise

    @staticmethod
    def _close_pins(pins):
        failure = None
        for stream in pins:
            try: descriptor = stream.fileno()
            except Exception: descriptor = None
            try: stream.close()
            except BaseException:
                lease = mxc_native.retain_failed_close(stream, descriptor)
                if failure is None: failure = lease
        return failure

    def _job(self, scope, workspace_identity):
        parent, _ = _physical(self.scratch_parent or Path(tempfile.gettempdir()))
        root = Path(tempfile.mkdtemp(prefix="aura-mxc-", dir=parent)).absolute()
        _, identity = _physical(root)
        for name in ("task", "input", "client"):
            (root / name).mkdir()
        (root / "input" / "files").mkdir()
        (root / "input" / ".git").mkdir()
        (root / "input" / ".git" / "HEAD").write_text("ref: refs/heads/aura-owned\n", encoding="utf-8")
        (root / "input" / ".aura-mxc-root").write_text("AURA_OWNED_CONFIG_BOUNDARY", encoding="ascii")
        for name in ("temp", "home", "appdata", "localappdata"):
            (root / "task" / name).mkdir()
        _, task_identity = _physical(root / "task")
        job = _Job(root, identity, secrets.token_hex(32), scope, workspace_identity, root / "task", task_identity)
        (root / "owner.json").write_text(json.dumps({"nonce": job.nonce, "scope": scope.identity}), encoding="utf-8")
        return job

    @staticmethod
    def _owned(job):
        root, identity = _physical(job.root)
        if identity != job.identity:
            _error("MXC_SCRATCH_CHANGED")
        marker = root / "owner.json"
        _physical(marker)
        if marker.stat().st_size > 2048 or json.loads(marker.read_text(encoding="utf-8")) != {"nonce": job.nonce, "scope": job.scope.identity}:
            _error("MXC_SCRATCH_CHANGED")
        return root

    def _stage(self, job, payload, inputs):
        if type(inputs) not in (tuple, list) or len(inputs) > 16:
            _error("MXC_INVALID_INPUTS")
        used, names, manifest = 0, set(), []
        for item in inputs:
            self._scope(job.scope, job.workspace_identity)
            if not isinstance(item, AuthorizedInput) or type(item.path) is not str or not _safe_name(item.alias) or item.alias.casefold() in names or type(item.expected_sha256) is not str or not _HASH.fullmatch(item.expected_sha256):
                _error("MXC_INVALID_INPUTS")
            requested = Path(item.path)
            requested = requested if requested.is_absolute() else job.scope._root / requested
            try:
                job.scope.files.resolve(item.path)
            except Exception:
                _error("MXC_INPUT_NOT_AUTHORIZED")
            _physical(requested)
            try:
                target, raw, digest = job.scope.files.read_bytes(item.path, maximum=self.policy.input_limit)
            except Exception:
                _error("MXC_INPUT_NOT_AUTHORIZED")
            _physical(target)
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _error("MXC_INPUT_LINK_REJECTED")
            if digest != item.expected_sha256:
                _error("MXC_INPUT_CHANGED")
            used += len(raw)
            if used > self.policy.input_limit:
                _error("MXC_INPUT_LIMIT")
            (job.root / "input" / "files" / item.alias).write_bytes(raw)
            names.add(item.alias.casefold())
            manifest.append({"alias": item.alias, "sha256": digest})
        helper = Path(__file__).with_name("mxc_entry.py")
        _physical(helper)
        (job.root / "input" / "entry.py").write_bytes(helper.read_bytes())
        worker = Path(__file__).with_name("mxc_worker.py")
        _physical(worker)
        (job.root / "input" / "worker.py").write_bytes(worker.read_bytes())
        (job.root / "input" / "broker.json").write_text(json.dumps({"version": 1,
            "desktop_boundary": self.policy.desktop_boundary,
            "python_executable": str(PYTHON_ROOT / "python.exe"), "python_sha256": PYTHON_SHA256}), encoding="utf-8")
        (job.root / "input" / "request.json").write_bytes(payload)
        (job.root / "input" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Only staged, host-approved copies are visible; original workspace and
        # host configuration remain outside the payload's filesystem grants.
        # Permissions exist only at CLI precedence under an unpredictable name.
        # The separate user config contains no same-name table to deep-merge.
        (job.root / "client" / "config.toml").write_text('[windows]\nsandbox = "mxc"\n', encoding="utf-8")

    @staticmethod
    def _environment(job):
        values = {"SystemRoot": str(SYSTEM32.parent), "WINDIR": str(SYSTEM32.parent), "SystemDrive": SYSTEM32.drive,
            "COMSPEC": str(SYSTEM32 / "cmd.exe"), "PATH": str(PYTHON_ROOT) + os.pathsep + str(SYSTEM32),
            "CODEX_HOME": str(job.root / "client"), "PYTHONIOENCODING": "utf-8"}
        for key, name in (("TEMP", "temp"), ("TMP", "temp"), ("USERPROFILE", "home"), ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata")):
            values[key] = str(job.task / name)
        return values

    @staticmethod
    def _profile_override(job):
        # TOML tables merge across layers, so use an unpredictable profile only
        # defined here. The owned CLI cwd separately excludes parent layers.
        entries = {":root": "deny", str(SYSTEM32): "read", str(PYTHON_ROOT): "read", str(job.root / "input"): "read", str(job.task): "write"}
        filesystem = "{" + ",".join(json.dumps(key) + "=" + json.dumps(value) for key, value in entries.items()) + "}"
        return "permissions." + job.profile + "={filesystem=" + filesystem + ",network={enabled=false}}"

    def _watch(self, job):
        self._scope(job.scope, job.workspace_identity)
        self._owned(job)
        _, identity = _physical(job.task)
        if identity != job.task_identity:
            raise mxc_native.NativeFailure("MXC_SCRATCH_CHANGED")
        return mxc_native.scan_scratch(job.task, expected_identity=job.task_identity,
            maximum_bytes=self.policy.scratch_limit, maximum_entries=self.policy.scratch_entries)

    def _clean_value(self, value):
        if type(value) is str:
            return redact_text(value, self._secrets)
        if type(value) is list:
            return [self._clean_value(item) for item in value]
        if type(value) is dict:
            result = {}
            for key, item in value.items():
                key = redact_text(key, self._secrets)
                if key in result:
                    _error("MXC_OUTPUT_REDACTION_CONFLICT")
                result[key] = self._clean_value(item)
            return result
        return value

    def execute(self, code, input_json=None, *, scope=None, authorized_inputs=(), cancel_event=None,
                authority_guard=None, before_resume=None, launch_attempt_callback=None,
                stage_inputs=None, capture_output=None):
        """Execute once; all optional callbacks are trusted host inputs.

        before_resume receives this exact scope inside authority_guard. Atomic
        revocation requires the caller to use that SAME guard for its actual
        mutations; omitting it provides callback-time checks only. The attempt
        callback runs before each native CreateProcess (version and workload).
        stage_inputs and capture_output are trusted host callbacks, outside the
        authority guard. They only receive owned scratch paths. A capture is
        not adoptable until the private receipt is returned after full cleanup.
        """
        if not self.live_enabled:
            return MxcExecutionResult("unavailable", "MXC_DISABLED")
        with self._cleanup_lock:
            if self._pending or mxc_native.has_pending_cleanup():
                return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_PENDING", tree_reaped=False, scratch_removed=False, resources_closed=False)
        if os.name != "nt":
            return MxcExecutionResult("unavailable", "MXC_WINDOWS_REQUIRED")
        if any(value is not None and not callable(value) for value in (authority_guard,before_resume,launch_attempt_callback,stage_inputs,capture_output)):
            _error("MXC_INVALID_AUTHORITY")
        if not isinstance(self.policy, MxcPolicy):
            _error("MXC_INVALID_POLICY")
        self.policy.validate()
        identity = self._scope(scope)
        try:
            payload = _payload(code, input_json)
        except Exception:
            _error("MXC_INVALID_REQUEST")
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            _error("MXC_INVALID_CANCEL_TOKEN")
        cancel = cancel_event or threading.Event()
        if cancel.is_set():
            return MxcExecutionResult("cancelled", "MXC_CANCELLED")
        job, native, value, result_code, removed, pins = None, None, None, "MXC_FAILED", True, ()
        invocation, captured = object(), None
        resource_unknown = False
        deadline = time.monotonic() + self.policy.timeout_seconds
        try:
            pins = self._pins() or ()
            job = self._job(scope, identity)
            self._stage(job, payload, authorized_inputs)
            if stage_inputs is not None:
                stage_inputs(job.root / "input" / "files", scope)
                self._scope(scope, identity)
            environment = self._environment(job)
            def watch():
                try:
                    return self._watch(job)
                except MxcExecutionError:
                    return False
            def launch(argv, maximum=None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _error("MXC_TIMEOUT")
                policy = replace(self.policy, timeout_seconds=min(remaining, maximum) if maximum else remaining)
                def fresh_resume():
                    try: self._scope(scope, identity)
                    except MxcExecutionError: raise mxc_native.NativeFailure("MXC_SCOPE_REVOKED") from None
                    if before_resume is not None and before_resume(scope) is False:
                        raise mxc_native.NativeFailure("MXC_SCOPE_REVOKED")
                    try: self._scope(scope, identity)
                    except MxcExecutionError: raise mxc_native.NativeFailure("MXC_SCOPE_REVOKED") from None
                return self._native_run(argv, environment=environment, cwd=job.root / "input", policy=policy, cancel_event=cancel, watch=watch,
                    authority_guard=authority_guard, before_resume=fresh_resume, launch_attempt_callback=launch_attempt_callback)
            version = launch([str(CODEX), "--version"], 3)
            native = version
            if version.code != "MXC_EXITED" or version.exit_code != 0 or version.stdout.decode("utf-8", "strict").strip() != CODEX_VERSION:
                _error(version.code if version.code != "MXC_EXITED" else "MXC_VERSION_MISMATCH")
            # An explicit custom profile otherwise tells the pinned CLI to
            # ignore managed requirements. Keep device policy enforcement;
            # policy rejection must not retry without this flag or MXC.
            native = launch([str(CODEX), "-c", 'windows.sandbox="mxc"', "-c", 'project_root_markers=[".aura-mxc-root"]', "-c", self._profile_override(job), "sandbox", "--include-managed-config", "-P", job.profile, "--", str(PYTHON_ROOT / "python.exe"), "-I", "-S", "-B", str(job.root / "input" / "entry.py")])
            result_code = native.code
            self._scope(scope, identity)
            if native.code == "MXC_EXITED":
                if native.exit_code != 0:
                    result_code = "MXC_PAYLOAD_FAILED"
                else:
                    try:
                        value = self._clean_value(_strict_json(native.stdout))
                        result_code = "MXC_OK"
                    except Exception:
                        result_code = "MXC_OUTPUT_NOT_STRICT_JSON"
                    if capture_output is not None and result_code == "MXC_OK":
                        if (native.tree_reaped is not True or native.resources_closed is not True
                                or native.limits_verified is not True or native._cleanup_lease is not None
                                or mxc_native.has_pending_cleanup() or cancel.is_set()):
                            _error("MXC_CAPTURE_NOT_CLOSED")
                        try:
                            captured = capture_output(job.task, scope)
                            if captured is None:
                                _error("MXC_OUTPUT_CAPTURE_FAILED")
                        except Exception:
                            _error("MXC_OUTPUT_CAPTURE_FAILED")
        except MxcExecutionError as failure:
            result_code = str(failure)
        except Exception:
            result_code = "MXC_FAILED"
        finally:
            if job is not None and native is not None and (native.tree_reaped is not True or not native.resources_closed or native._cleanup_lease is not None):
                with self._cleanup_lock:
                    self._pending.append(_PendingCleanup(job, tuple(pins), native._cleanup_lease, native.tree_reaped))
                removed = False
                resource_unknown = not native.resources_closed
                pins = ()
            elif job is not None:
                lease = self._close_pins(pins)
                if lease is not None or mxc_native.has_pending_cleanup():
                    resource_unknown = True
                    removed = False
                    with self._cleanup_lock:
                        self._pending.append(_PendingCleanup(job, tuple(pins), lease, True))
                    pins = ()
                else:
                    pins = ()
                    try:
                        root = self._owned(job)
                        shutil.rmtree(root)
                    except Exception:
                        removed = False
                        with self._cleanup_lock:
                            self._pending.append(_PendingCleanup(job, (), None, True))
            else:
                resource_unknown = self._close_pins(pins) is not None or mxc_native.has_pending_cleanup()
        if native is not None and (not native.tree_reaped or resource_unknown):
            return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_UNCONFIRMED", tree_reaped=native.tree_reaped,
                scratch_removed=False, resources_closed=False)
        if resource_unknown:
            return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_UNCONFIRMED", scratch_removed=removed, resources_closed=False)
        if not removed:
            return MxcExecutionResult("cleanup_unconfirmed", "MXC_SCRATCH_CLEANUP_UNCONFIRMED", scratch_removed=False)
        try:
            self._scope(scope, identity)
        except MxcExecutionError as failure:
            result_code, value = str(failure), None
        if cancel.is_set():
            result_code, value = "MXC_CANCELLED", None
        state = "exited" if result_code == "MXC_OK" else "cancelled" if result_code == "MXC_CANCELLED" else "timeout" if result_code == "MXC_TIMEOUT" else "failed"
        receipt = None
        if (state == "exited" and captured is not None and native is not None
                and native.limits_verified is True and native.tree_reaped is True
                and native.resources_closed is True and native._cleanup_lease is None
                and not mxc_native.has_pending_cleanup()):
            receipt = CaptureReceipt(_CAPTURE_ISSUER, invocation, scope, job, native, captured)
        return MxcExecutionResult(state, result_code, value if state == "exited" else None,
            limits_verified=native.limits_verified if native else False, _capture_receipt=receipt)

    def reconcile_cleanup(self, *, scope):
        """Host-only cleanup of exact retained authority; never runs code again."""
        with self._cleanup_lock:
            if not isinstance(scope, HostExecutionScope) or any(item.job.scope is not scope for item in self._pending):
                _error("MXC_SCOPE_MISMATCH")
            for item in tuple(self._pending):
                if item.lease is not None:
                    if mxc_native.reconcile_cleanup(item.lease) is not True:
                        return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_UNCONFIRMED", tree_reaped=item.tree_reaped,
                            scratch_removed=False, resources_closed=False)
                    item.lease = None
                    item.tree_reaped = True
                elif not item.tree_reaped:
                    return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_UNCONFIRMED", tree_reaped=False,
                        scratch_removed=False, resources_closed=False)
                if mxc_native.has_pending_cleanup():
                    return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_PENDING", tree_reaped=item.tree_reaped,
                        scratch_removed=False, resources_closed=False)
                lease = self._close_pins(item.pins)
                if lease is not None:
                    item.lease = lease
                    return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_UNCONFIRMED", tree_reaped=item.tree_reaped,
                        scratch_removed=False, resources_closed=False)
                item.pins = ()
                try:
                    root = self._owned(item.job)
                    shutil.rmtree(root)
                except Exception:
                    return MxcExecutionResult("cleanup_unconfirmed", "MXC_SCRATCH_CLEANUP_UNCONFIRMED", scratch_removed=False)
                self._pending.remove(item)
            if mxc_native.has_pending_cleanup():
                return MxcExecutionResult("cleanup_unconfirmed", "MXC_CLEANUP_PENDING", tree_reaped=False, scratch_removed=False, resources_closed=False)
            return MxcExecutionResult("cleanup_complete", "MXC_CLEANUP_RECONCILED")

    async def execute_async(self, code, input_json=None, **options):
        cancel = options.get("cancel_event")
        if cancel is None:
            cancel = options["cancel_event"] = threading.Event()
        if not isinstance(cancel, threading.Event):
            _error("MXC_INVALID_CANCEL_TOKEN")
        operation = asyncio.create_task(asyncio.to_thread(self.execute, code, input_json, **options))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancel.set()
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    pass
            operation.exception()  # Consume a concurrent failure; Stop still cancels.
            raise
