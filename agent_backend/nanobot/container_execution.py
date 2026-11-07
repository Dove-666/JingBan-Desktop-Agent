"""Optional fail-closed container execution, deliberately not wired to tools.

No daemon discovery, environment-selected endpoint, image pull, container or
filesystem IO occurs on import or construction. Live execution remains off by
default until separately verified on the host. Fake transport tests are not
proof that the Docker Desktop/VM/kernel isolation works.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID

if __package__:
    from .safe_files import WorkspaceFiles
    from .redaction import redact_text
else:
    # worker.py runs with -I and explicitly inserts HERE. Match its unqualified
    # imports so HostExecutionScope sees exactly the worker's WorkspaceFiles.
    from safe_files import WorkspaceFiles
    from redaction import redact_text


DOCKER_ENDPOINT = "npipe:////./pipe/dockerDesktopLinuxEngine"
DOCKER_CLI = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
_DIGEST = re.compile(r"[a-z0-9][a-z0-9./:_-]{0,200}@sha256:[a-f0-9]{64}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"aura-py-[a-f0-9]{32}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_IMAGE_ENV = frozenset({"PATH", "LANG", "LC_ALL", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256", "GPG_KEY"})
_OWNER_LABEL = "com.aura.container.owner"
_SCOPE_LABEL = "com.aura.container.scope"
_MAX_STDIN = 2 * 1024 * 1024


class ContainerExecutionError(ValueError):
    """Only fixed machine codes may cross the tool boundary."""


def _error(code):
    raise ContainerExecutionError(code) from None


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError, AttributeError):
        return False


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _no_aliases(path):
    path = Path(path).absolute()
    try:
        for part in (path, *path.parents):
            if _reparse(part.lstat()): _error("CONTAINER_PATH_ALIAS_REJECTED")
        if path.resolve(strict=True) != path: _error("CONTAINER_PATH_ALIAS_REJECTED")
    except OSError:
        _error("CONTAINER_FILE_UNAVAILABLE")
    return path


def _safe_name(name):
    if not isinstance(name, str) or not _ALIAS.fullmatch(name) or name.endswith((".", " ")):
        return False
    stem = name.split(".")[0].upper()
    return stem not in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _mount_options(value):
    if not isinstance(value, str) or len(value) > 1024: _error("CONTAINER_POLICY_NOT_ENFORCED")
    result = {}
    for token in value.split(","):
        key, separator, setting = token.partition("=")
        if not key or key in result: _error("CONTAINER_POLICY_NOT_ENFORCED")
        result[key] = setting if separator else None
    return result


def _payload(code, value):
    if not isinstance(code, str) or not 1 <= len(code) <= 65536 or "\0" in code:
        _error("CONTAINER_INVALID_CODE")
    try:
        code_size = len(code.encode("utf-8"))
        if code_size > 65536: _error("CONTAINER_INVALID_CODE")
        # Bound data before serialization. A giant string/list or a cycle must
        # not allocate an unbounded host buffer ahead of the size check.
        pending, nodes, size = [(value, 0)], 0, code_size + 128
        while pending:
            item, depth = pending.pop(); nodes += 1
            if depth > 64 or nodes > 100000: _error("CONTAINER_INPUT_LIMIT")
            if type(item) is str:
                if len(item) > _MAX_STDIN: _error("CONTAINER_INPUT_LIMIT")
                size += len(item.encode("utf-8")) + 2 + sum(
                    1 if char in '\\"\b\f\n\r\t' else 5 if ord(char) < 32 else 0 for char in item)
            elif item is None or type(item) is bool: size += 6
            elif type(item) is int:
                if item.bit_length() > 10000: _error("CONTAINER_INPUT_LIMIT")
                size += item.bit_length() * 30103 // 100000 + 3
            elif type(item) is float:
                if not math.isfinite(item): _error("CONTAINER_INVALID_INPUT")
                size += 32
            elif type(item) is list:
                if len(item) > 100000: _error("CONTAINER_INPUT_LIMIT")
                size += len(item) + 2
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is dict:
                if len(item) > 100000 or any(type(key) is not str for key in item): _error("CONTAINER_INVALID_INPUT")
                size += len(item) * 2 + 2
                pending.extend((child, depth + 1) for pair in item.items() for child in pair)
            else: _error("CONTAINER_INVALID_INPUT")
            if size > _MAX_STDIN: _error("CONTAINER_INPUT_LIMIT")
        raw = json.dumps({"version": 1, "code": code, "input": value}, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except ContainerExecutionError: raise
    except (ValueError, TypeError, UnicodeError, RecursionError): _error("CONTAINER_INVALID_INPUT")
    if len(raw) > _MAX_STDIN: _error("CONTAINER_INPUT_LIMIT")
    return raw


@dataclass(frozen=True)
class ContainerPolicy:
    image_digest: str | None = field(default=None, repr=False)
    memory_mb: int = 256
    cpus: float = 1.0
    pids_limit: int = 32
    timeout_seconds: float = 30.0
    stdout_limit: int = 262144
    stderr_limit: int = 65536
    input_limit: int = 16 * 1024 * 1024
    output_file_limit: int = 4 * 1024 * 1024
    output_total_limit: int = 16 * 1024 * 1024

    def validate(self):
        if not isinstance(self.image_digest, str) or not _DIGEST.fullmatch(self.image_digest) or ".." in self.image_digest:
            _error("CONTAINER_PINNED_IMAGE_REQUIRED")
        for value, lower, upper in ((self.memory_mb, 64, 2048), (self.pids_limit, 8, 128),
            (self.stdout_limit, 1024, 1048576), (self.stderr_limit, 1024, 262144),
            (self.input_limit, 1, 16 * 1024 * 1024), (self.output_file_limit, 1, 16 * 1024 * 1024),
            (self.output_total_limit, 1, 32 * 1024 * 1024)):
            if type(value) is not int or not lower <= value <= upper: _error("CONTAINER_INVALID_POLICY")
        for value, lower, upper in ((self.cpus, .1, 2), (self.timeout_seconds, .1, 120)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                _error("CONTAINER_INVALID_POLICY")


@dataclass(frozen=True, init=False, repr=False)
class HostExecutionScope:
    """Mint only on the host after authorization, never from model arguments."""
    conversation_id: str
    generation: str
    request_id: str
    files: WorkspaceFiles
    grant_revision: str
    identity: str
    _nonce: str
    _root: Path
    _is_current: object

    def __init__(self, conversation_id, generation, request_id, files, *, grant_revision, is_current):
        if not all(_uuid(value) for value in (conversation_id, generation, request_id)) or \
            not isinstance(files, WorkspaceFiles) or not isinstance(grant_revision, str) or not _HASH.fullmatch(grant_revision) or not callable(is_current):
            _error("CONTAINER_INVALID_SCOPE")
        nonce = secrets.token_hex(32)
        identity = hashlib.sha256(json.dumps([conversation_id, generation, request_id,
            str(files.root), grant_revision, nonce]).encode()).hexdigest()
        for key, value in {"conversation_id": conversation_id, "generation": generation, "request_id": request_id,
            "files": files, "grant_revision": grant_revision, "_nonce": nonce, "_root": files.root,
            "identity": identity, "_is_current": is_current}.items(): object.__setattr__(self, key, value)

    def __repr__(self):
        return "HostExecutionScope([host-owned])"


@dataclass(frozen=True)
class AuthorizedInput:
    path: str = field(repr=False)
    expected_sha256: str
    alias: str


@dataclass(frozen=True)
class ContainerExecutionResult:
    state: str
    code: str
    job_id: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    artifacts: tuple = ()  # File export is deliberately unavailable in this phase.
    container_removed: bool = False
    task_verified: bool = False


@dataclass(frozen=True)
class DockerCommandResult:
    state: str
    returncode: int | None = None
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    client_reaped: bool = True


def _read_available(pipe):
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        available = wintypes.DWORD()
        kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        if not kernel.PeekNamedPipe(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(available), None): return b""
        return os.read(pipe.fileno(), min(available.value, 65536)) if available.value else b""
    import select
    readable, _, _ = select.select([pipe], [], [], 0)
    return os.read(pipe.fileno(), 65536) if readable else b""


class DockerCLITransport:
    """Native transport is inert until a host explicitly enables live calls."""
    def __init__(self, *, live_enabled=False, docker_cli_path=None):
        self.live_enabled = live_enabled is True
        selected = str(DOCKER_CLI) if docker_cli_path is None else os.fspath(docker_cli_path)
        physical = PureWindowsPath(selected)
        if not physical.is_absolute() or not re.fullmatch(r"[A-Za-z]:", physical.drive) or physical.name.casefold() != "docker.exe" or \
            ".." in physical.parts or any(ord(char) < 32 or char == '"' for char in selected):
            _error("CONTAINER_INVALID_DOCKER_CLI")
        self.docker_cli_path = Path(selected)
        self._pending_clients = []
        self._pending_lock = threading.Lock()

    def capability(self):
        return {"available": False, "runtime_verified": False, "code": "CONTAINER_RUNTIME_UNVERIFIED",
                "configured_transport": "windows_docker_desktop_linux_pipe"}

    def call(self, argv, *, stdin, environment, cwd, timeout, cancel_event, stdout_limit, stderr_limit, watch=None):
        if not self.live_enabled or sys.platform != "win32":
            return DockerCommandResult("UNAVAILABLE")
        if not isinstance(argv, list) or len(argv) < 7 or any(not isinstance(token, str) for token in argv) or \
            argv[0] != str(self.docker_cli_path) or argv[1] != "--config" or \
            argv[3:5] != ["--host", DOCKER_ENDPOINT] or argv[5:7] not in (["image", "inspect"], ["container", "create"],
                ["container", "inspect"], ["container", "start"], ["container", "rm"], ["container", "ls"]) or \
            argv.count("--host") != 1 or argv.count("--config") != 1 or any(token in ("--context", "-c", "-H") or
                token.startswith(("--host=", "--context=", "--config=", "-H")) for token in argv[5:]) or \
            not isinstance(environment, dict) or any(key not in {"SystemRoot", "WINDIR", "SystemDrive", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"} for key in environment):
            return DockerCommandResult("CLIENT_FAILED")
        process = writer = None
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            if stdin is not None:
                def send():
                    try: process.stdin.write(stdin); process.stdin.flush()
                    except Exception: pass
                    finally:
                        try: process.stdin.close()
                        except Exception: pass
                writer = threading.Thread(target=send, name="aura-container-stdin", daemon=True); writer.start()
            while True:
                if cancel_event.is_set(): return DockerCommandResult("CANCELLED")
                if time.monotonic() >= deadline: return DockerCommandResult("TIMEOUT")
                if watch is not None and not watch(): return DockerCommandResult("OUTPUT_LIMIT")
                output.extend(_read_available(process.stdout)); errors.extend(_read_available(process.stderr))
                if len(output) > stdout_limit or len(errors) > stderr_limit: return DockerCommandResult("OUTPUT_LIMIT")
                if process.poll() is not None:
                    for pipe, target, limit in ((process.stdout, output, stdout_limit), (process.stderr, errors, stderr_limit)):
                        while True:
                            chunk = _read_available(pipe)
                            if not chunk: break
                            target.extend(chunk)
                            if len(target) > limit: return DockerCommandResult("OUTPUT_LIMIT")
                    return DockerCommandResult("OK", process.returncode, bytes(output), bytes(errors))
                cancel_event.wait(.02)
        except Exception:
            return DockerCommandResult("CLIENT_FAILED")
        finally:
            if process is not None:
                try: running = process.poll() is None
                except Exception: running = True
                if running:
                    try: process.kill()
                    except Exception: pass
                try: process.wait(timeout=2)
                except Exception: pass
                if writer is not None:
                    try: writer.join(timeout=2)
                    except Exception: pass
                # Register before closing any pipe. A close failure must not
                # lose an unreaped client's ownership or return a benign code.
                try: unreaped = process.poll() is None
                except Exception: unreaped = True
                if unreaped:
                    with self._pending_lock: self._pending_clients.append(process)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        try: pipe.close()
                        except Exception: pass
                if unreaped:
                    return DockerCommandResult("CLIENT_REAP_UNCONFIRMED", client_reaped=False)

    def reap_pending(self, *, timeout=1):
        deadline = time.monotonic() + timeout
        with self._pending_lock:
            pending, self._pending_clients = self._pending_clients, []
        remaining = []
        for process in pending:
            if time.monotonic() >= deadline:
                remaining.append(process); continue
            try:
                if process.poll() is None: process.kill()
                process.wait(timeout=max(.01, deadline - time.monotonic()))
            except Exception: pass
            try: unreaped = process.poll() is None
            except Exception: unreaped = True
            if unreaped: remaining.append(process)
        with self._pending_lock:
            self._pending_clients.extend(remaining)
            return not self._pending_clients


@dataclass
class _Job:
    name: str
    owner: str
    scope: HostExecutionScope
    root: Path
    root_identity: tuple
    container_id: str | None = None
    reported_id: str | None = None
    creation_attempted: bool = False
    creation_confirmed: bool = False
    clients_reaped: bool = True
    removed: bool = False


class ContainerPythonRunner:
    def __init__(self, policy=None, *, transport=None, known_secrets=()):
        self.policy = policy or ContainerPolicy()
        self.transport = transport or DockerCLITransport()
        self._secrets = tuple(value for value in known_secrets if isinstance(value, str) and value)
        self._jobs = {}
        self._lock = threading.RLock()

    def capability(self):
        # A fake transport or a successful CLI launch cannot certify isolation.
        return {"available": False, "runtime_verified": False, "file_exports": False, "code": "CONTAINER_RUNTIME_UNVERIFIED"}

    @staticmethod
    def _environment(job):
        values = {}
        for key in ("SystemRoot", "WINDIR", "SystemDrive"):
            value = os.environ.get(key)
            if isinstance(value, str): values[key] = value
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            values[key] = str(job.root / "client")
        return values

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, HostExecutionScope) or scope.files.root != scope._root:
            _error("CONTAINER_SCOPE_MISMATCH")
        try: current = scope._is_current() is True
        except Exception: current = False
        if not current: _error("CONTAINER_SCOPE_REVOKED")
        _no_aliases(scope._root)

    def _job(self, scope):
        parent = _no_aliases(Path(tempfile.gettempdir()))
        root = Path(tempfile.mkdtemp(prefix="aura-container-", dir=parent)).absolute()
        info = root.lstat()
        job = _Job("aura-py-" + secrets.token_hex(16), secrets.token_hex(32), scope, root, (info.st_dev, info.st_ino))
        for name in ("input", "client"):
            (root / name).mkdir(mode=0o700)
        (root / "client" / "config.json").write_text('{"auths":{},"HttpHeaders":{},"credsStore":"","credHelpers":{}}', encoding="utf-8")
        (root / "owner.json").write_text(json.dumps({"name": job.name, "owner": job.owner, "scope": scope.identity}), encoding="utf-8")
        with self._lock: self._jobs[job.name] = job
        return job

    @staticmethod
    def _owned(job):
        root = _no_aliases(job.root)
        info = root.lstat()
        if (info.st_dev, info.st_ino) != job.root_identity: _error("CONTAINER_SCRATCH_CHANGED")
        marker = root / "owner.json"
        if _reparse(marker.lstat()) or marker.stat().st_size > 2048: _error("CONTAINER_SCRATCH_CHANGED")
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value != {"name": job.name, "owner": job.owner, "scope": job.scope.identity}: _error("CONTAINER_SCRATCH_CHANGED")
        return root

    def _call(self, job, args, *, stdin=None, timeout=5, cancel=None, watch=None, stdout_limit=65536, stderr_limit=8192):
        self._owned(job)
        argv = [str(getattr(self.transport, "docker_cli_path", DOCKER_CLI)), "--config", str(job.root / "client"), "--host", DOCKER_ENDPOINT, *args]
        try:
            result = self.transport.call(argv, stdin=stdin, environment=self._environment(job), cwd=str(job.root / "client"),
                timeout=max(.01, timeout), cancel_event=cancel or threading.Event(), stdout_limit=stdout_limit,
                stderr_limit=stderr_limit, watch=watch)
        except Exception:
            return DockerCommandResult("CLIENT_FAILED")
        if isinstance(result, DockerCommandResult) and (result.client_reaped is not True or result.state == "CLIENT_REAP_UNCONFIRMED"):
            job.clients_reaped = False
            return DockerCommandResult("CLIENT_REAP_UNCONFIRMED", client_reaped=False)
        if not isinstance(result, DockerCommandResult) or result.state not in {"OK", "TIMEOUT", "CANCELLED", "OUTPUT_LIMIT", "CLIENT_FAILED", "UNAVAILABLE", "CLIENT_REAP_UNCONFIRMED"}:
            return DockerCommandResult("CLIENT_FAILED")
        if not isinstance(result.stdout, bytes) or not isinstance(result.stderr, bytes) or len(result.stdout) > stdout_limit or len(result.stderr) > stderr_limit:
            return DockerCommandResult("OUTPUT_LIMIT")
        return result

    @staticmethod
    def _object(result):
        if result.state != "OK" or result.returncode != 0: return None
        try:
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else None
        except Exception: return None

    def _stage(self, job, inputs):
        if not isinstance(inputs, (list, tuple)) or len(inputs) > 32: _error("CONTAINER_INVALID_INPUTS")
        used, names = 0, set()
        for item in inputs:
            self._scope(job.scope)
            if not isinstance(item, AuthorizedInput) or not isinstance(item.path, str) or not _safe_name(item.alias) or item.alias.casefold() in names or \
                not isinstance(item.expected_sha256, str) or not _HASH.fullmatch(item.expected_sha256):
                _error("CONTAINER_INVALID_INPUTS")
            try: job.scope.files.resolve(item.path)
            except (ValueError, OSError): _error("CONTAINER_INPUT_NOT_AUTHORIZED")
            # Check the named path before WorkspaceFiles canonicalizes it.
            requested = Path(item.path)
            requested = requested if requested.is_absolute() else job.scope.files.root / requested
            _no_aliases(requested)
            target, raw, digest = job.scope.files.read_bytes(item.path, maximum=self.policy.input_limit)
            info = target.lstat()
            if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: _error("CONTAINER_INPUT_LINK_REJECTED")
            if digest != item.expected_sha256: _error("CONTAINER_INPUT_CHANGED")
            used += len(raw)
            if used > self.policy.input_limit: _error("CONTAINER_INPUT_LIMIT")
            (job.root / "input" / item.alias).write_bytes(raw)
            names.add(item.alias.casefold())
        helper = Path(__file__).with_name("container_entry.py")
        (job.root / "input" / ".aura_container_entry.py").write_bytes(helper.read_bytes())

    def _create_args(self, job):
        policy = self.policy
        source = job.root / "input"
        if any(char in str(job.root) for char in ',"\r\n'): _error("CONTAINER_SCRATCH_UNSAFE")
        return ["container", "create", "--pull=never", "--name", job.name,
            "--label", _OWNER_LABEL + "=" + job.owner, "--label", _SCOPE_LABEL + "=" + job.scope.identity,
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--pids-limit", str(policy.pids_limit), "--memory", f"{policy.memory_mb}m", "--memory-swap", f"{policy.memory_mb}m",
            "--cpus", str(float(policy.cpus)), "--user", "10001:10001", "--restart", "no", "--no-healthcheck",
            "--log-driver", "none", "--ipc", "none", "--cgroupns", "private", "--hostname", "aura-python",
            "--ulimit", f"fsize={policy.output_file_limit}:{policy.output_file_limit}", "--ulimit", "nofile=64:64",
            "--ulimit", f"cpu={math.ceil(policy.timeout_seconds)}:{math.ceil(policy.timeout_seconds) + 1}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16777216,uid=10001,gid=10001,mode=1770",
            "--tmpfs", f"/aura/output:rw,noexec,nosuid,nodev,size={policy.output_total_limit},nr_inodes=128,uid=10001,gid=10001,mode=0700",
            "--mount", f"type=bind,source={source},target=/aura/input,readonly,bind-propagation=rprivate",
            "--workdir", "/aura/output", "--entrypoint", "/usr/local/bin/python", "--interactive",
            policy.image_digest, "-I", "-B", "/aura/input/.aura_container_entry.py"]

    def _image(self, job, *, timeout=5, cancel=None):
        response = self._call(job, ["image", "inspect", "--format", "{{json .}}", self.policy.image_digest], timeout=timeout, cancel=cancel)
        if response.state in {"TIMEOUT", "CANCELLED"}: _error("CONTAINER_" + response.state)
        image = self._object(response)
        if not image or image.get("Os") != "linux" or self.policy.image_digest not in image.get("RepoDigests", []):
            _error("CONTAINER_IMAGE_UNAVAILABLE")
        config = image.get("Config")
        if not isinstance(config, dict) or config.get("Volumes") not in (None, {}): _error("CONTAINER_IMAGE_REJECTED")
        environment = config.get("Env", [])
        if not isinstance(environment, list) or any(not isinstance(value, str) or value.split("=", 1)[0] not in _IMAGE_ENV for value in environment):
            _error("CONTAINER_IMAGE_ENV_REJECTED")

    def _inspect(self, job, *, timeout=5, cancel=None, identifier=None):
        return self._object(self._call(job, ["container", "inspect", "--format", "{{json .}}", identifier or job.name], timeout=timeout, cancel=cancel))

    def _identity(self, job, value, *, check_name=True):
        config = value.get("Config") if isinstance(value, dict) else None
        labels = config.get("Labels", {}) if isinstance(config, dict) else {}
        return (isinstance(value, dict) and (not check_name or value.get("Name") == "/" + job.name) and
            isinstance(labels, dict) and labels.get(_OWNER_LABEL) == job.owner and labels.get(_SCOPE_LABEL) == job.scope.identity and
            isinstance(value.get("Id"), str) and _HASH.fullmatch(value["Id"]) and
            (job.container_id is None or value["Id"] == job.container_id))

    def _configuration(self, job, value):
        if not self._identity(job, value): _error("CONTAINER_IDENTITY_MISMATCH")
        job.container_id = value["Id"]
        if job.reported_id != job.container_id: _error("CONTAINER_IDENTITY_MISMATCH")
        host, config, mounts = value.get("HostConfig", {}), value.get("Config", {}), value.get("Mounts", [])
        if not isinstance(host, dict) or not isinstance(config, dict) or not isinstance(mounts, list): _error("CONTAINER_POLICY_NOT_ENFORCED")
        if not isinstance(host.get("RestartPolicy"), dict) or not isinstance(host.get("LogConfig"), dict): _error("CONTAINER_POLICY_NOT_ENFORCED")
        required = (host.get("NetworkMode") == "none", host.get("ReadonlyRootfs") is True,
            host.get("CapDrop") == ["ALL"], host.get("CapAdd") in (None, []), host.get("Privileged") is False,
            host.get("PidMode") == "", host.get("Devices") in (None, []), host.get("DeviceRequests") in (None, []),
            host.get("DeviceCgroupRules") in (None, []), host.get("Binds") in (None, []), host.get("Links") in (None, []),
            host.get("Runtime") == "runc", host.get("VolumesFrom") in (None, []),
            host.get("PortBindings") in (None, {}), host.get("PublishAllPorts") is False,
            host.get("IpcMode") == "none", host.get("CgroupnsMode") == "private",
            host.get("RestartPolicy", {}).get("Name") == "no", host.get("LogConfig", {}).get("Type") == "none",
            host.get("PidsLimit") == self.policy.pids_limit, host.get("Memory") == self.policy.memory_mb * 1024 * 1024,
            host.get("MemorySwap") == self.policy.memory_mb * 1024 * 1024,
            host.get("NanoCpus") == int(self.policy.cpus * 1_000_000_000),
            host.get("SecurityOpt") == ["no-new-privileges=true"], config.get("User") == "10001:10001",
            config.get("Entrypoint") == ["/usr/local/bin/python"], config.get("Image") == self.policy.image_digest,
            config.get("Cmd") == ["-I", "-B", "/aura/input/.aura_container_entry.py"], config.get("WorkingDir") == "/aura/output",
            config.get("OpenStdin") is True)
        temporary = host.get("Tmpfs")
        temporary_expected = {"/tmp": {"rw": None, "noexec": None, "nosuid": None, "nodev": None, "size": "16777216", "uid": "10001", "gid": "10001", "mode": "1770"},
            "/aura/output": {"rw": None, "noexec": None, "nosuid": None, "nodev": None, "size": str(self.policy.output_total_limit), "nr_inodes": "128", "uid": "10001", "gid": "10001", "mode": "0700"}}
        if not isinstance(temporary, dict) or set(temporary) != set(temporary_expected) or any(
            _mount_options(temporary[name]) != values for name, values in temporary_expected.items()):
            _error("CONTAINER_POLICY_NOT_ENFORCED")
        limits = host.get("Ulimits")
        required_limits = {"fsize": (self.policy.output_file_limit, self.policy.output_file_limit),
            "nofile": (64, 64), "cpu": (math.ceil(self.policy.timeout_seconds), math.ceil(self.policy.timeout_seconds) + 1)}
        if not isinstance(limits, list) or len(limits) != len(required_limits) or any(not isinstance(item, dict) or
                set(item) != {"Name", "Soft", "Hard"} or not isinstance(item["Name"], str) or type(item["Soft"]) is not int or
                type(item["Hard"]) is not int for item in limits) or len({item["Name"] for item in limits}) != len(limits) or \
            {item["Name"]: (item["Soft"], item["Hard"]) for item in limits} != required_limits:
            _error("CONTAINER_POLICY_NOT_ENFORCED")
        image_environment = config.get("Env", [])
        if not isinstance(image_environment, list) or any(not isinstance(value, str) or value.split("=", 1)[0] not in _IMAGE_ENV for value in image_environment):
            _error("CONTAINER_IMAGE_ENV_REJECTED")
        expected = {"/aura/input": (str(job.root / "input"), False)}
        actual = {}
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("Type") != "bind" or not isinstance(mount.get("Destination"), str) or \
                not isinstance(mount.get("Source"), str) or type(mount.get("RW")) is not bool or mount.get("Propagation") != "rprivate":
                _error("CONTAINER_POLICY_NOT_ENFORCED")
            actual[mount.get("Destination")] = (mount.get("Source"), mount.get("RW"))
        if not all(required) or len(mounts) != 1 or actual != expected: _error("CONTAINER_POLICY_NOT_ENFORCED")

    def _cleanup(self, job):
        """Never equate killing an attached client with stopping its container."""
        def clients_reaped():
            if not job.clients_reaped:
                try: job.clients_reaped = self.transport.reap_pending(timeout=1) is True
                except Exception: return False
            return job.clients_reaped
        if not job.creation_attempted:
            job.removed = clients_reaped()
            return job.removed
        deadline = time.monotonic() + 5
        verified_id = job.container_id
        value = self._inspect(job, timeout=2, identifier=verified_id or job.name)
        if value is not None:
            if not self._identity(job, value, check_name=verified_id is None): return False
            verified_id = value["Id"]
            job.container_id = verified_id
            # Name+nonce discovers the owned instance; the immutable inspected
            # ID avoids deleting a same-name replacement in an admin race.
            removed = self._call(job, ["container", "rm", "--force", "--volumes", value["Id"]], timeout=max(.01, min(2, deadline - time.monotonic())))
            if removed.state != "OK" or removed.returncode != 0: return False
            job.creation_confirmed = True
        elif verified_id is None:
            # Creation may still be in flight after a client cancellation.
            # Empty ls alone cannot prove a late daemon-side create won't land.
            return False
        # Failed inspect is not absence. Verify the immutable owned instance
        # as well as the original name: an admin rename cannot count as rm.
        for selected in ("id=" + verified_id, "name=^/" + job.name + "$"):
            result = self._call(job, ["container", "ls", "--all", "--no-trunc", "--filter", selected, "--format", "{{.ID}}"], timeout=max(.01, deadline - time.monotonic()))
            if result.state != "OK" or result.returncode != 0 or result.stdout.strip() != b"": return False
        job.removed = clients_reaped()
        return job.removed

    def execute(self, code, input_json, *, scope, authorized_inputs=(), cancel_event=None):
        # No native Docker call happens without the separate host gate.
        if isinstance(self.transport, DockerCLITransport) and not self.transport.live_enabled:
            return ContainerExecutionResult("unavailable", "CONTAINER_RUNTIME_UNVERIFIED")
        if not isinstance(self.policy, ContainerPolicy): _error("CONTAINER_INVALID_POLICY")
        self.policy.validate(); self._scope(scope)
        payload = _payload(code, input_json)
        if cancel_event is not None and not isinstance(cancel_event, threading.Event): _error("CONTAINER_INVALID_CANCEL_TOKEN")
        cancel = cancel_event or threading.Event()
        if cancel.is_set(): return ContainerExecutionResult("cancelled", "CONTAINER_CANCELLED", container_removed=True)
        job = self._job(scope)
        started = time.monotonic()
        code_result, state, exit_code, stdout, stderr = "CONTAINER_FAILED", "failed", None, "", ""
        try:
            self._stage(job, authorized_inputs)
            self._image(job, timeout=min(5, self.policy.timeout_seconds - (time.monotonic() - started)), cancel=cancel)
            self._scope(scope)
            if cancel.is_set(): _error("CONTAINER_CANCELLED")
            create_args = self._create_args(job)
            job.creation_attempted = True
            created = self._call(job, create_args, timeout=self.policy.timeout_seconds - (time.monotonic() - started), cancel=cancel)
            if created.state != "OK" or created.returncode != 0: _error("CONTAINER_CREATE_" + created.state if created.state != "OK" else "CONTAINER_CREATE_FAILED")
            if not _HASH.fullmatch(created.stdout.decode("ascii", "ignore").strip()): _error("CONTAINER_CREATE_PROTOCOL")
            job.reported_id = created.stdout.decode("ascii").strip()
            job.creation_confirmed = True
            self._configuration(job, self._inspect(job, timeout=min(5, self.policy.timeout_seconds - (time.monotonic() - started)), cancel=cancel))
            self._scope(scope)
            remaining = self.policy.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0: _error("CONTAINER_TIMEOUT")
            def running_watch():
                self._scope(scope)
                return True
            result = self._call(job, ["container", "start", "--attach", "--interactive", job.name], stdin=payload,
                timeout=max(.01, remaining), cancel=cancel, stdout_limit=self.policy.stdout_limit, stderr_limit=self.policy.stderr_limit,
                watch=running_watch)
            self._scope(scope)
            if result.state != "OK":
                code_result = "CONTAINER_" + result.state
                state = "cancelled" if result.state == "CANCELLED" else "timeout" if result.state == "TIMEOUT" else "failed"
            else:
                self._scope(scope)
                terminal = self._inspect(job)
                if not self._identity(job, terminal): _error("CONTAINER_IDENTITY_MISMATCH")
                metadata = terminal.get("State", {})
                if metadata.get("Running") is not False or type(metadata.get("ExitCode")) is not int: _error("CONTAINER_EXIT_UNCONFIRMED")
                exit_code = metadata["ExitCode"]
                state, code_result = "exited", "CONTAINER_EXITED"
                def clean(raw, limit):
                    text = redact_text(raw.decode("utf-8", "replace"), self._secrets, "[redacted]")
                    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
                    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")
                stdout = clean(result.stdout, self.policy.stdout_limit)
                stderr = clean(result.stderr, self.policy.stderr_limit)
        except ContainerExecutionError as failure:
            code_result = str(failure)
            if cancel.is_set() or "CANCELLED" in code_result: state, code_result = "cancelled", "CONTAINER_CANCELLED"
            elif "TIMEOUT" in code_result: state, code_result = "timeout", "CONTAINER_TIMEOUT"
        except Exception:
            code_result = "CONTAINER_FAILED"
        finally:
            try: removed = self._cleanup(job)
            except Exception: removed = False
        if not removed:
            return ContainerExecutionResult("cleanup_unconfirmed", "CONTAINER_CLEANUP_UNCONFIRMED", job.name)
        try: self._scope(scope)
        except ContainerExecutionError:
            return ContainerExecutionResult("failed", "CONTAINER_SCOPE_REVOKED", job.name, container_removed=True)
        # Bounded tmpfs output is ephemeral. No host-RW bind or Docker cp is
        # allowed. A separately verified freeze/TAR boundary is future work.
        return ContainerExecutionResult(state, code_result, job.name, exit_code, stdout, stderr, (), True)

    def publish(self, job_id, reference, destination, *, scope, overwrite=False, expected_destination_hash=None):
        # No unverified paused-tmpfs/TAR export, and no host write fallback.
        del job_id, reference, destination, scope, overwrite, expected_destination_hash
        _error("CONTAINER_FILE_EXPORT_UNAVAILABLE")

    def dispose(self, job_id, *, scope):
        if not isinstance(job_id, str) or not _NAME.fullmatch(job_id): _error("CONTAINER_SCOPE_MISMATCH")
        job = self._jobs.get(job_id)
        if job is None or job.scope is not scope: _error("CONTAINER_SCOPE_MISMATCH")
        if not job.removed: _error("CONTAINER_CLEANUP_UNCONFIRMED")
        root = self._owned(job)
        # Delete only this exact, physically checked, randomly owned directory.
        shutil.rmtree(root)
        with self._lock: self._jobs.pop(job_id, None)

    def retry_cleanup(self, job_id, *, scope):
        if not isinstance(job_id, str) or not _NAME.fullmatch(job_id): _error("CONTAINER_SCOPE_MISMATCH")
        job = self._jobs.get(job_id)
        if job is None or job.scope is not scope: _error("CONTAINER_SCOPE_MISMATCH")
        # Revocation denies reads/publication, but cannot revoke the host's
        # authority to stop its already-owned container.
        return self._cleanup(job)
