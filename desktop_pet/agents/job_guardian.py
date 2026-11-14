"""Owned Job guardian evidence for remote-queue stopped-only recovery.

Only explicit trusted-host calls start a helper. No model or remote messages,
PID scans or absent processes can create closure evidence. This evidence type
is deliberately incompatible with WorkerLifecycle's in-memory receipts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
from uuid import UUID

try:
    from .guardian_native import GuardianError, Native, OwnedProcess, SecurityAttributes, has_pending_cleanup, retain_failed_launch
except ImportError:
    from guardian_native import GuardianError, Native, OwnedProcess, SecurityAttributes, has_pending_cleanup, retain_failed_launch

import ctypes as C

_HEX = re.compile(r"[a-f0-9]{64}\Z")
_MANIFEST_KEYS = frozenset({"schema", "job_id", "conversation_id", "generation", "operation_id",
    "run_nonce", "launch_epoch", "installation_id", "process_creation_time"})
_MANIFEST_V2_KEYS = _MANIFEST_KEYS | {"user_message_id", "settings_scope", "origin_peer_id"}
_RECEIPT_KEYS = frozenset({"schema", "manifest_sha256", "run_nonce", "job_empty", "root_signaled",
    "status", "trigger", "hmac_sha256"})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GuardianError("GUARDIAN_JSON_DUPLICATE")
        result[key] = value
    return result


def canonical(value):
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
    except Exception:
        raise GuardianError("GUARDIAN_JSON_INVALID") from None
    if not 0 < len(raw) <= 4096:
        raise GuardianError("GUARDIAN_RECORD_LIMIT")
    return raw


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 4096:
        raise GuardianError("GUARDIAN_RECORD_LIMIT")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise GuardianError("GUARDIAN_JSON_INVALID") from None
    if not isinstance(value, dict) or canonical(value) != raw:
        raise GuardianError("GUARDIAN_JSON_NOT_CANONICAL")
    return value


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise GuardianError("GUARDIAN_IDENTITY_INVALID")


def validate_manifest(value):
    if not isinstance(value, dict):
        raise GuardianError("GUARDIAN_IDENTITY_INVALID")
    version = value.get("schema")
    if not (version == "aura.host_job_manifest.v1" and set(value) == _MANIFEST_KEYS
            or version == "aura.host_job_manifest.v2" and set(value) == _MANIFEST_V2_KEYS):
        raise GuardianError("GUARDIAN_IDENTITY_INVALID")
    for key in ("job_id", "conversation_id", "generation", "operation_id"):
        _uuid(value[key])
    for key in ("run_nonce", "launch_epoch", "installation_id"):
        if not isinstance(value[key], str) or not _HEX.fullmatch(value[key]):
            raise GuardianError("GUARDIAN_IDENTITY_INVALID")
    if version == "aura.host_job_manifest.v2":
        _uuid(value["user_message_id"])
        for key in ("settings_scope", "origin_peer_id"):
            if not isinstance(value[key], str) or not _HEX.fullmatch(value[key]):
                raise GuardianError("GUARDIAN_IDENTITY_INVALID")
    if type(value["process_creation_time"]) is not int or not 0 < value["process_creation_time"] < 2**64:
        raise GuardianError("GUARDIAN_IDENTITY_INVALID")
    canonical(value)
    return value


def new_manifest(*, job_id, conversation_id, generation, operation_id, installation_id,
                 launch_epoch, process_creation_time, user_message_id=None, settings_scope=None, origin_peer_id=None):
    value = {"schema": "aura.host_job_manifest.v1", "job_id": job_id,
        "conversation_id": conversation_id, "generation": generation, "operation_id": operation_id,
        "run_nonce": secrets.token_hex(32), "installation_id": installation_id,
        "launch_epoch": launch_epoch, "process_creation_time": process_creation_time}
    if any(item is not None for item in (user_message_id, settings_scope, origin_peer_id)):
        value.update(schema="aura.host_job_manifest.v2", user_message_id=user_message_id,
                     settings_scope=settings_scope, origin_peer_id=origin_peer_id)
    return validate_manifest(value)


def _mac(key, value):
    return hmac.new(key, canonical(value), hashlib.sha256).hexdigest()


class GuardianClosureEvidence:
    """Authenticated stopped-only evidence. Never an agent completion verdict."""
    __slots__ = ("_manifest_sha256", "_run_nonce", "_trigger")

    def __new__(cls, *args, **kwargs):
        raise TypeError("HOST_GUARDIAN_EVIDENCE_REQUIRED")

    def __setattr__(self, *args):
        raise AttributeError("HOST_GUARDIAN_EVIDENCE_IMMUTABLE")

    @property
    def manifest_sha256(self): return self._manifest_sha256

    @property
    def run_nonce(self): return self._run_nonce

    @property
    def trigger(self): return self._trigger

    @property
    def worker_stopped(self): return True

    def __repr__(self): return "GuardianClosureEvidence([host-verified stopped-only])"

    def __reduce__(self):
        raise TypeError("HOST_GUARDIAN_EVIDENCE_NOT_SERIALIZABLE")


def _closure_evidence(digest, nonce, trigger):
    value = object.__new__(GuardianClosureEvidence)
    for name, item in (("_manifest_sha256", digest), ("_run_nonce", nonce), ("_trigger", trigger)):
        object.__setattr__(value, name, item)
    return value


class PrivateRunStore:
    """New owner-only directory, ordinary bounded files, current-user DPAPI.

    Same-user host compromise/memory access remains outside this boundary.
    Callers must keep this location outside delegated model file scopes.
    """
    def __init__(self, path, native=None):
        self.native = native or Native()
        self.path = Path(path).absolute()
        self.directory = 0
        self._pins = []
        self._open_directory()

    @classmethod
    def create(cls, parent, manifest, native=None):
        native = native or Native()
        validate_manifest(manifest)
        parent = Path(parent).absolute()
        target = parent / manifest["run_nonce"]
        descriptor = native.descriptor()
        try:
            attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
            native.check(native.k.CreateDirectoryW(str(target), C.byref(attributes)), "GUARDIAN_PRIVATE_CREATE_FAILED")
        finally:
            native.k.LocalFree(descriptor)
        store = cls(target, native)
        try:
            raw = canonical(manifest)
            key = secrets.token_bytes(32)
            store._write_new("manifest.json", raw)
            record = canonical({"schema": "aura.guardian_key.v1", "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                                "key": base64.b64encode(key).decode("ascii")})
            store._write_new("key.dpapi", native.protected_key(record))
            store.identity()  # Verify what was durably read, not write intent.
            return store
        except BaseException:
            store.close()
            raise

    def _open_directory(self):
        # Atomic child rename on NTFS requires delete sharing on the parent
        # directory. Recheck the held directory's file identity around IO;
        # same-user host compromise is explicitly outside this ACL boundary.
        self.directory = self.native.k.CreateFileW(str(self.path), 0x20081, 7, None, 3, 0x02200000, None)
        if not self.directory or self.directory == C.c_void_p(-1).value:
            self.directory = 0
            raise GuardianError("GUARDIAN_PRIVATE_OPEN_FAILED")
        try:
            info = self.native.file_info(self.directory)
            if info.attributes & 0x400 or not info.attributes & 0x10 or not self.native.owner_only(self.directory):
                raise GuardianError("GUARDIAN_PRIVATE_SCOPE_INVALID")
            self._directory_identity = (info.volume, info.index_hi, info.index_lo)
        except BaseException:
            self.close()
            raise

    def _assert_directory(self):
        handle = self.native.k.CreateFileW(str(self.path), 0x20081, 7, None, 3, 0x02200000, None)
        if not handle or handle == C.c_void_p(-1).value:
            raise GuardianError("GUARDIAN_PRIVATE_SCOPE_CHANGED")
        try:
            info = self.native.file_info(handle)
            if (info.attributes & 0x400 or not info.attributes & 0x10
                    or (info.volume, info.index_hi, info.index_lo) != self._directory_identity
                    or not self.native.owner_only(handle)):
                raise GuardianError("GUARDIAN_PRIVATE_SCOPE_CHANGED")
        finally:
            self.native.close(handle)

    def _file(self, name, *, create=False):
        self._assert_directory()
        if name not in {"manifest.json", "key.dpapi", "receipt.json", "receipt.tmp", "consumed.json",
                        "external-required.json", "external-closed.json"}:
            raise GuardianError("GUARDIAN_RECORD_NAME_INVALID")
        descriptor = self.native.descriptor() if create else None
        try:
            attributes = None
            if create:
                attrs = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
                attributes = C.byref(attrs)
            handle = self.native.k.CreateFileW(str(self.path / name), 0x20000 | (0x40000000 if create else 0x80000000),
                1, attributes, 1 if create else 3, 0x00200000, None)
            if not handle or handle == C.c_void_p(-1).value:
                raise GuardianError("GUARDIAN_RECORD_CREATE_FAILED" if create else "GUARDIAN_RECORD_UNAVAILABLE")
            try:
                info = self.native.file_info(handle)
                size = info.size_hi * 2**32 + info.size_lo
                if info.attributes & 0x410 or info.links != 1 or size > 4096 or not self.native.owner_only(handle):
                    raise GuardianError("GUARDIAN_RECORD_INVALID")
                self._assert_directory()
            except BaseException:
                self.native.close(handle)
                raise
            return handle
        finally:
            if descriptor:
                self.native.k.LocalFree(descriptor)

    def _write_new(self, name, raw):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 4096:
            raise GuardianError("GUARDIAN_RECORD_LIMIT")
        handle = self._file(name, create=True)
        try:
            self.native.write(handle, raw)
            self.native.check(self.native.k.FlushFileBuffers(handle), "GUARDIAN_FLUSH_FAILED")
        finally:
            self.native.close(handle)

    def _read(self, name):
        handle = self._file(name)
        try:
            buffer, count = C.create_string_buffer(4097), C.c_uint32()
            self.native.check(self.native.k.ReadFile(handle, buffer, 4097, C.byref(count), None), "GUARDIAN_RECORD_READ_FAILED")
            if not 0 < count.value <= 4096:
                raise GuardianError("GUARDIAN_RECORD_LIMIT")
            return buffer.raw[:count.value]
        finally:
            self.native.close(handle)

    def identity(self):
        raw = self._read("manifest.json")
        manifest = validate_manifest(decode(raw))
        digest = hashlib.sha256(raw).hexdigest()
        key_record = decode(self.native.protected_key(self._read("key.dpapi"), decrypt=True))
        if (set(key_record) != {"schema", "manifest_sha256", "key"}
                or key_record.get("schema") != "aura.guardian_key.v1"
                or key_record.get("manifest_sha256") != digest or not isinstance(key_record.get("key"), str)):
            raise GuardianError("GUARDIAN_KEY_BINDING_INVALID")
        try:
            key = base64.b64decode(key_record["key"], validate=True)
        except Exception:
            raise GuardianError("GUARDIAN_KEY_BINDING_INVALID") from None
        if len(key) != 32:
            raise GuardianError("GUARDIAN_KEY_BINDING_INVALID")
        return manifest, digest, key

    def write_closure(self, manifest, digest, key, *, trigger):
        # Only the helper calls this after both exact Win32 observations. It
        # is a trusted-host method, never a model/file-import capability.
        if trigger not in {"app_exit", "stop"} or self.identity() != (manifest, digest, key):
            raise GuardianError("GUARDIAN_RECEIPT_BINDING_INVALID")
        value = {"schema": "aura.host_job_closure.v1", "manifest_sha256": digest,
            "run_nonce": manifest["run_nonce"], "job_empty": True, "root_signaled": True,
            "status": "worker_stopped", "trigger": trigger}
        value["hmac_sha256"] = _mac(key, value)
        self._write_new("receipt.tmp", canonical(value))
        self._assert_directory()
        # No replacement of a previous receipt, and write-through rename.
        self.native.check(self.native.k.MoveFileExW(str(self.path / "receipt.tmp"),
            str(self.path / "receipt.json"), 0x8), "GUARDIAN_RECEIPT_PUBLISH_FAILED")
        self._assert_directory()

    def _external_record(self, name, state):
        """Authenticate an immutable, run-bound external-worker lifecycle record."""
        self._assert_directory()
        try:
            (self.path / name).lstat()
        except FileNotFoundError:
            return None
        manifest, digest, key = self.identity()
        value = decode(self._read(name))
        expected = {"schema": "aura.external_cleanup.v1", "manifest_sha256": digest,
                    "run_nonce": manifest["run_nonce"], "state": state}
        expected["hmac_sha256"] = _mac(key, expected)
        if not hmac.compare_digest(canonical(value), canonical(expected)):
            raise GuardianError("GUARDIAN_EXTERNAL_CLEANUP_INVALID")
        return value

    def _write_external_record(self, name, state):
        if self._external_record(name, state) is not None:
            return
        manifest, digest, key = self.identity()
        value = {"schema": "aura.external_cleanup.v1", "manifest_sha256": digest,
                 "run_nonce": manifest["run_nonce"], "state": state}
        value["hmac_sha256"] = _mac(key, value)
        self._write_new(name, canonical(value))
        if self._external_record(name, state) != value:
            raise GuardianError("GUARDIAN_EXTERNAL_CLEANUP_INVALID")

    def mark_external_cleanup_required(self):
        """Host must commit this before dispatching any out-of-Job computer work."""
        self._write_external_record("external-required.json", "required")

    def mark_external_cleanup_confirmed(self):
        """Trusted host calls only after the SDK/native channel closed ACK."""
        if self._external_record("external-required.json", "required") is None:
            raise GuardianError("GUARDIAN_EXTERNAL_CLEANUP_NOT_REQUIRED")
        self._write_external_record("external-closed.json", "closed")

    def external_cleanup_confirmed(self):
        if self._external_record("external-required.json", "required") is None:
            return True  # Historical/in-Job-only run; never dispatched external work.
        return self._external_record("external-closed.json", "closed") is not None

    def read_closure(self, *, expected_manifest):
        try:
            manifest, digest, key = self.identity()
            if manifest != validate_manifest(expected_manifest):
                return None
            if not self.external_cleanup_confirmed():
                return None
            value = decode(self._read("receipt.json"))
            if (set(value) != _RECEIPT_KEYS or value.get("schema") != "aura.host_job_closure.v1"
                    or value.get("manifest_sha256") != digest or value.get("run_nonce") != manifest["run_nonce"]
                    or value.get("job_empty") is not True or value.get("root_signaled") is not True
                    or value.get("status") != "worker_stopped" or value.get("trigger") not in {"app_exit", "stop"}
                    or not isinstance(value.get("hmac_sha256"), str)):
                return None
            signature = value.pop("hmac_sha256")
            if not hmac.compare_digest(signature, _mac(key, value)):
                return None
            return _closure_evidence(digest, manifest["run_nonce"], value["trigger"])
        except (GuardianError, OSError, ValueError, TypeError):
            return None

    def consume_closure(self, *, expected_manifest):
        evidence = self.read_closure(expected_manifest=expected_manifest)
        if evidence is None:
            return None
        try:
            # Cross-process exclusive creation is a single-use CAS for this
            # isolated store. It is not yet a queue/database state change.
            self._write_new("consumed.json", canonical({"schema": "aura.guardian_consumed.v1",
                "manifest_sha256": evidence.manifest_sha256, "run_nonce": evidence.run_nonce}))
            return evidence
        except GuardianError:
            return None

    def close(self):
        for handle in self._pins:
            self.native.close(handle)
        self._pins = []
        self.native.close(self.directory)
        self.directory = 0

    def __repr__(self):
        return "PrivateRunStore([host-private])"


class GuardianSession:
    """ARMED is required before caller may Resume or dispatch worker stdin."""
    def __init__(self, process, stop_event, *, native, manifest, digest, external_job):
        self.process, self.stop_event, self.native = process, stop_event, native
        self.manifest, self.manifest_sha256 = manifest, digest
        self.guardian_in_external_job = external_job
        self.armed = True

    @classmethod
    def arm(cls, store, *, job, root, python=None):
        native = store.native
        if has_pending_cleanup():
            raise GuardianError("GUARDIAN_CLEANUP_UNCONFIRMED")
        manifest, digest, key = store.identity()
        if (root.resumed or getattr(root, "_guardian_bound", False) or not native.in_job(root.handle, job.handle)
                or native.creation_time(root.handle) != manifest["process_creation_time"]
                or native.in_job(native.k.GetCurrentProcess(), job.handle)):
            raise GuardianError("GUARDIAN_ROOT_BINDING_INVALID")
        root._guardian_bound = True
        if python is None:
            try:
                from ..bundle_runtime import worker_python_executable
                python = worker_python_executable()
            except ImportError:
                # This fixed helper can also run as a plain trusted script.
                python = sys.executable
        runtime = Path(python).resolve(strict=True)
        helper = Path(__file__).with_name("job_guardian_helper.py").resolve(strict=True)
        # Fixed trusted helper/runtime are an explicit host dependency. No
        # delegated source, cwd module, environment or model payload is used.
        handles, duplicates, process, stop_event = [], [], None, 0
        try:
            boot_read, boot_write = native.pipe()
            handles.extend([boot_read, boot_write])
            ack_read, ack_write = native.pipe()
            handles.extend([ack_read, ack_write])
            stop_event = native.check(native.k.CreateEventW(None, True, False, None), "GUARDIAN_EVENT_FAILED")
            for handle, access in ((job.handle, 0x4 | 0x8), (root.handle, 0x100000 | 0x1000),
                                   (native.k.GetCurrentProcess(), 0x100000), (stop_event, 0x100000)):
                duplicates.append(native.duplicate(handle, access))
            null = native.k.CreateFileW("NUL", 0xC0000000, 3, None, 3, 0, None)
            if not null or null == C.c_void_p(-1).value:
                raise GuardianError("GUARDIAN_NULL_FAILED")
            handles.append(null)
            argv = [str(runtime), "-I", "-u", str(helper), *(str(item) for item in [boot_read, ack_write, *duplicates])]
            process = OwnedProcess.create(native, argv, cwd=helper.parent,
                inherited=[boot_read, ack_write, *duplicates, null], stdio=(null, null, null))
            process.resume()
            # Private bootstrap alone carries the key and private location.
            bootstrap = {"schema": "aura.guardian_bootstrap.v1", "manifest": manifest,
                "manifest_sha256": digest, "key": base64.b64encode(key).decode("ascii"), "private_directory": str(store.path)}
            native.write(boot_write, canonical(bootstrap) + b"\n")
            ack = decode(native.read_bounded(ack_read, maximum=4096, timeout=5))
            if (set(ack) != {"schema", "manifest_sha256", "run_nonce", "guardian_in_external_job", "hmac_sha256"}
                    or ack.get("schema") != "aura.guardian_armed.v1" or ack.get("manifest_sha256") != digest
                    or ack.get("run_nonce") != manifest["run_nonce"] or type(ack.get("guardian_in_external_job")) is not bool
                    or not isinstance(ack.get("hmac_sha256"), str) or not _HEX.fullmatch(ack["hmac_sha256"])):
                raise GuardianError("GUARDIAN_ARM_UNCONFIRMED")
            signature = ack.pop("hmac_sha256")
            if not hmac.compare_digest(signature, _mac(key, ack)):
                raise GuardianError("GUARDIAN_ARM_UNCONFIRMED")
            return cls(process, stop_event, native=native, manifest=manifest, digest=digest,
                       external_job=ack["guardian_in_external_job"])
        except BaseException:
            if process is not None:
                retain_failed_launch(process)
            native.close(stop_event)
            raise
        finally:
            for handle in [*handles, *duplicates]:
                native.close(handle)

    def stop(self):
        self.native.check(self.native.k.SetEvent(self.stop_event), "GUARDIAN_STOP_SIGNAL_FAILED")

    def close(self):
        # Closing host references is not a Stop and never implies closure.
        self.native.close(self.stop_event)
        self.stop_event = 0
        self.process.close()
