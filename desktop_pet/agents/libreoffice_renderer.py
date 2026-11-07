"""Host-selected headless renderer with private profiles and owned Job.

This is process/profile separation, not an OS filesystem/network sandbox.
Only existing inspect_package-approved docx/xlsx/pptx are admitted. No runtime
discovery, model-provided executable, user Office process, or API key is used.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import secrets
import threading
import time

FORMATS = frozenset({".docx", ".xlsx", ".pptx"})
PACKAGE_SHA256 = "ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a"
_pending = []
_unbound_directories = []
_unbound_handles = []
_lock = threading.Lock()
_execution_lock = threading.Lock()
_EVIDENCE_ISSUER = object()


class CleanupEvidence:
    """Opaque, process-local proof for one retained renderer invocation."""
    __slots__ = ("_issuer", "_owner", "_invocation", "_source_sha256")
    def __new__(cls, *args, **kwargs): raise TypeError("HOST_EVIDENCE_ONLY")
    def __setattr__(self, name, value): raise TypeError("IMMUTABLE_HOST_EVIDENCE")
    def __reduce__(self): raise TypeError("NON_SERIALIZABLE_HOST_EVIDENCE")
    def __copy__(self): raise TypeError("NON_COPYABLE_HOST_EVIDENCE")
    def __deepcopy__(self, memo): raise TypeError("NON_COPYABLE_HOST_EVIDENCE")


def _mint_evidence(owner, invocation, digest):
    evidence = object.__new__(CleanupEvidence)
    for name, value in (("_issuer", _EVIDENCE_ISSUER), ("_owner", owner),
                        ("_invocation", invocation), ("_source_sha256", digest)):
        object.__setattr__(evidence, name, value)
    return evidence


class LibreOfficeError(ValueError):
    def __init__(self, code): self.code = code; super().__init__(code)


def _sha(raw): return hashlib.sha256(raw).hexdigest()


def _read_bounded(path, maximum):
    with Path(path).open("rb") as stream: raw = stream.read(maximum + 1)
    if len(raw) > maximum: raise LibreOfficeError("LIBREOFFICE_RESULT_LIMIT")
    return raw


def _file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""): value.update(block)
    return value.hexdigest()


def _remove_owned_directory(directory):
    """Remove only our minted scratch, including Windows Known Folder flags."""
    directory = Path(directory).absolute()
    if not directory.name.startswith("libreoffice-owned-"):
        raise LibreOfficeError("LIBREOFFICE_CLEANUP_SCOPE_INVALID")
    try: os.lstat(directory)
    except FileNotFoundError: return  # Disk cleanup only; never process evidence.
    expected = directory.parent.resolve(strict=True) / directory.name
    if directory.resolve(strict=True) != expected:
        raise LibreOfficeError("LIBREOFFICE_CLEANUP_SCOPE_INVALID")
    # Never follow a reparse point created by a parser. Root/descendants are
    # inspected after exact Job closure; same-user arbitrary mutation remains
    # outside this prototype's trust boundary.
    for folder, directories, files in os.walk(directory, followlinks=False):
        for name in [*directories, *files]:
            attributes = os.lstat(Path(folder) / name).st_file_attributes
            if attributes & 0x400: raise LibreOfficeError("LIBREOFFICE_CLEANUP_SCOPE_INVALID")
    def readonly_retry(function, path, error):
        candidate = Path(path).absolute()
        if (not isinstance(error[1], PermissionError) or not candidate.is_relative_to(directory)
                or candidate.resolve(strict=True) != expected / candidate.relative_to(directory)
                or os.lstat(candidate).st_file_attributes & 0x400):
            raise error[1]
        os.chmod(candidate, stat.S_IREAD | stat.S_IWRITE)
        function(candidate)
    shutil.rmtree(directory, onerror=readonly_retry)


@dataclass(frozen=True)
class RenderedOffice:
    pdf: bytes
    previews: tuple[bytes, ...]
    pages: int
    text: str
    source_sha256: str
    pdf_sha256: str
    cleanup_confirmed: bool = True
    renderer: str = "LibreOffice 26.8.2 headless"
    verification_scope: str = "actual_libreoffice_pdf_text_and_raster_pages"
    microsoft_office_rendered: bool = False
    visual_layout_verified: bool = False  # Actual human/model inspection is separate.
    cleanup_evidence: object = None


@dataclass
class _OwnedRun:
    native: object
    job: object
    process: object
    directory: Path
    effects_closed: bool = False
    extra_handles: list = field(default_factory=list)
    cleanup_stage: str = "not_started"
    evidence_owner: object = None
    execution_identity: object = None
    source_sha256: str | None = None


def _cleanup(run):
    try:
        native, job, process = run.native, run.job, run.process
        if not run.effects_closed:
            run.cleanup_stage = "request_job_stop"
            job.terminate()
            # An assignment failure leaves a suspended root outside the Job.
            # Stop the retained exact process too; no PID/name discovery.
            if not native.signaled(process.handle):
                run.cleanup_stage = "request_root_stop"
                # Job termination is asynchronous. A second exact-root stop
                # can fail because that process is already terminating; its
                # return value never substitutes for the observations below.
                native.k.TerminateProcess(process.handle, 77)
            run.cleanup_stage = "confirm_exact_tree_closed"
            if not native.confirm_closed(job.handle, process.handle, timeout=5): return False
            run.effects_closed = True
        # Effect closure and successful handle release are separate facts. A
        # failed CloseHandle retains that handle for the explicit retry.
        for owner, name in ((process, "thread"), (process, "handle"), (job, "handle")):
            run.cleanup_stage = "release_exact_handles"
            handle = getattr(owner, name)
            if handle:
                if not native.k.CloseHandle(handle): return False
                setattr(owner, name, 0)
        for handle in tuple(run.extra_handles):
            if not native.k.CloseHandle(handle): return False
            run.extra_handles.remove(handle)
        run.cleanup_stage = "remove_owned_directory"
        _remove_owned_directory(run.directory)
        run.cleanup_stage = "complete"
        return True
    except Exception: return False


def reconcile_cleanup():
    """Explicit host call only; never infer stopped from a PID or absent file."""
    with _lock:
        from .guardian_native import reconcile_cleanup as reconcile_launches
        if not reconcile_launches(): return False
        for item in tuple(_pending):
            if _cleanup(item):
                _pending.remove(item)
                if item.evidence_owner is not None and type(item.execution_identity) is object:
                    item.evidence_owner._reconciled_evidence[item.execution_identity] = _mint_evidence(
                        item.evidence_owner, item.execution_identity, item.source_sha256)
        for directory in tuple(_unbound_directories):
            try: _remove_owned_directory(directory); _unbound_directories.remove(directory)
            except Exception: pass
        for native, handle in tuple(_unbound_handles):
            if native.k.CloseHandle(handle): _unbound_handles.remove((native, handle))
        return not _pending and not _unbound_directories and not _unbound_handles


class LibreOfficeRenderer:
    def __init__(self, *, runtime, python, pdftoppm, scratch_parent, enabled=False):
        self.runtime = Path(runtime).absolute()
        self.python = Path(python).absolute()
        self.pdftoppm = Path(pdftoppm).absolute()
        self.scratch_parent = Path(scratch_parent).absolute()
        self.enabled = enabled
        self._reconciled_evidence = {}

    def _validate_runtime(self):
        if not self.enabled or os.name != "nt": raise LibreOfficeError("LIBREOFFICE_PROTOTYPE_DISABLED")
        manifest = self.runtime.parent / "runtime-manifest.json"
        if not manifest.is_file() or manifest.stat().st_size > 8 * 1024**2:
            raise LibreOfficeError("LIBREOFFICE_RUNTIME_UNAVAILABLE")
        value = json.loads(_read_bounded(manifest, 8 * 1024**2).decode("utf-8"))
        if (value.get("schema") != "aura.libreoffice.extracted_runtime.v1" or value.get("version") != "26.8.2"
                or value.get("package_sha256") != PACKAGE_SHA256 or value.get("signature", {}).get("status") != "Valid"):
            raise LibreOfficeError("LIBREOFFICE_RUNTIME_UNAVAILABLE")
        hashes = {item["file"]: item["sha256"] for item in value["files"]}
        for name in ("program/soffice.com", "program/soffice.bin", "program/soffice.exe"):
            target = self.runtime / name
            if not target.is_file() or _file_sha(target) != hashes.get(name):
                raise LibreOfficeError("LIBREOFFICE_RUNTIME_CHANGED")
        if not self.python.is_file() or not self.pdftoppm.is_file():
            raise LibreOfficeError("LIBREOFFICE_DEPENDENCY_UNAVAILABLE")

    def validate_cleanup_evidence(self, evidence, *, invocation, source_sha256):
        return (type(invocation) is object and type(evidence) is CleanupEvidence
            and getattr(evidence, "_issuer", None) is _EVIDENCE_ISSUER
            and evidence._owner is self and evidence._invocation is invocation
            and evidence._source_sha256 == source_sha256)

    def reconcile_cleanup_evidence(self, *, invocation, source_sha256):
        """Only an actually retained exact run can yield deferred proof."""
        if type(invocation) is not object: return None
        try: reconcile_cleanup()
        except Exception: return None
        with _lock:
            evidence = self._reconciled_evidence.get(invocation)
            if not self.validate_cleanup_evidence(evidence, invocation=invocation, source_sha256=source_sha256):
                return None
            return self._reconciled_evidence.pop(invocation)

    def render(self, raw, suffix, *, expected_source_sha256, cancel=None, timeout_seconds=60,
               execution_identity=None):
        if not _execution_lock.acquire(blocking=False): raise LibreOfficeError("LIBREOFFICE_BUSY")
        execution = {"native_entered": False, "cleanup_confirmed": False, "invocation": execution_identity}
        result = failure = None
        try:
            result = self._render(raw, suffix, expected_source_sha256=expected_source_sha256,
                cancel=cancel, timeout_seconds=timeout_seconds, _execution=execution)
        except LibreOfficeError as exc: failure = exc
        except Exception: failure = LibreOfficeError("LIBREOFFICE_RENDER_FAILED")
        finally: _execution_lock.release()
        # Before entering Native no owned process/handle was attempted. After
        # entering it, only this exact run's successful cleanup may issue proof;
        # empty global registries or an error code are never evidence.
        evidence = None
        if (type(execution_identity) is object
                and (failure is None or failure.code != "LIBREOFFICE_CLEANUP_UNCONFIRMED")
                and (not execution["native_entered"] or execution["cleanup_confirmed"])):
            evidence = _mint_evidence(self, execution_identity, expected_source_sha256)
        if failure is not None:
            if evidence is not None: failure.cleanup_evidence = evidence
            raise failure from None
        return replace(result, cleanup_evidence=evidence) if evidence is not None else result

    def _render(self, raw, suffix, *, expected_source_sha256, cancel=None, timeout_seconds=60, _execution=None):
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= 32 * 1024**2 or suffix not in FORMATS
                or _sha(raw) != expected_source_sha256 or type(timeout_seconds) not in {int, float}
                or not 0 < timeout_seconds <= 120):
            raise LibreOfficeError("LIBREOFFICE_INPUT_INVALID")
        from agent_backend.nanobot.office_render import inspect_package, OfficeRenderError
        try: inspect_package(raw, suffix)
        except OfficeRenderError as exc: raise LibreOfficeError(exc.code) from None
        from .guardian_native import Native, OwnedJob, OwnedProcess, ExtendedLimits, has_pending_cleanup
        with _lock:
            if _pending or _unbound_directories or _unbound_handles or has_pending_cleanup():
                raise LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
        self._validate_runtime()
        if cancel is not None and cancel.is_set(): raise LibreOfficeError("LIBREOFFICE_CANCELLED")
        if _execution is not None: _execution["native_entered"] = True
        native = Native(); job = process = None; directory = None; directory_owned = False; null = 0
        result = None; failure = None
        try:
            self.scratch_parent.mkdir(parents=True, exist_ok=True)
            directory = self.scratch_parent / ("libreoffice-owned-" + secrets.token_hex(16))
            from .guardian_native import SecurityAttributes
            descriptor = native.descriptor()
            try:
                attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
                native.check(native.k.CreateDirectoryW(str(directory), C.byref(attributes)), "LIBREOFFICE_PRIVATE_CREATE_FAILED")
                directory_owned = True
            finally: native.k.LocalFree(descriptor)
            directory_handle = native.k.CreateFileW(str(directory), 0x20081, 7, None, 3, 0x02200000, None)
            if not directory_handle or directory_handle == C.c_void_p(-1).value:
                raise LibreOfficeError("LIBREOFFICE_PRIVATE_OPEN_FAILED")
            try:
                info = native.file_info(directory_handle)
                if info.attributes & 0x400 or not info.attributes & 0x10 or not native.owner_only(directory_handle):
                    raise LibreOfficeError("LIBREOFFICE_PRIVATE_SCOPE_INVALID")
            finally:
                if not native.k.CloseHandle(directory_handle):
                    with _lock: _unbound_handles.append((native, directory_handle))
                    raise LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
            source = directory / ("source" + suffix); source.write_bytes(raw)
            request = directory / "request.json"
            request.write_text(json.dumps({"schema": "aura.libreoffice.request.v1", "runtime": str(self.runtime),
                "pdftoppm": str(self.pdftoppm), "suffix": suffix, "source_sha256": expected_source_sha256}), encoding="utf-8")
            job = OwnedJob(native)
            limits = ExtendedLimits()
            native.check(native.k.QueryInformationJobObject(job.handle, 9, C.byref(limits), C.sizeof(limits), None), "LIBREOFFICE_JOB_LIMIT_FAILED")
            limits.basic.flags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.job_memory = 768 * 1024**2
            native.check(native.k.SetInformationJobObject(job.handle, 9, C.byref(limits), C.sizeof(limits)), "LIBREOFFICE_JOB_LIMIT_FAILED")
            actual = ExtendedLimits()
            native.check(native.k.QueryInformationJobObject(job.handle, 9, C.byref(actual), C.sizeof(actual), None), "LIBREOFFICE_JOB_LIMIT_FAILED")
            if actual.basic.flags != limits.basic.flags or actual.job_memory != limits.job_memory or actual.basic.active_limit != 8:
                raise LibreOfficeError("LIBREOFFICE_JOB_LIMIT_UNCONFIRMED")
            null = native.k.CreateFileW("NUL", 0xC0000000, 3, None, 3, 0, None)
            if not null or null == C.c_void_p(-1).value: raise LibreOfficeError("LIBREOFFICE_LAUNCH_FAILED")
            process = OwnedProcess.create(native, [str(self.python), "-I", "-u",
                str(Path(__file__).with_name("libreoffice_helper.py")), str(request)], cwd=directory,
                inherited=[null], stdio=(null, null, null))
            # Keep the root handle even if assignment fails. OwnedJob.assign's
            # generic failed-launch reaper is deliberately not used here.
            native.check(native.k.AssignProcessToJobObject(job.handle, process.handle), "LIBREOFFICE_JOB_ASSIGN_FAILED")
            if not native.in_job(process.handle, job.handle): raise LibreOfficeError("LIBREOFFICE_JOB_ASSIGN_UNCONFIRMED")
            process.resume()
            deadline = time.monotonic() + timeout_seconds
            while not native.signaled(process.handle):
                if cancel is not None and cancel.is_set(): raise LibreOfficeError("LIBREOFFICE_CANCELLED")
                if time.monotonic() >= deadline: raise LibreOfficeError("LIBREOFFICE_TIMEOUT")
                time.sleep(.025)
            native.k.GetExitCodeProcess.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
            native.k.GetExitCodeProcess.restype = C.c_int
            code = C.c_uint32(); native.check(native.k.GetExitCodeProcess(process.handle, C.byref(code)), "LIBREOFFICE_WAIT_FAILED")
            result_file = directory / "result.json"
            if not result_file.is_file() or result_file.stat().st_size > 2 * 1024**2:
                raise LibreOfficeError("LIBREOFFICE_RESULT_UNAVAILABLE")
            record = json.loads(_read_bounded(result_file, 2 * 1024**2).decode("utf-8"))
            if code.value != 0 or record.get("passed") is not True:
                permitted = {"LIBREOFFICE_CONVERT_FAILED", "LIBREOFFICE_PDF_LIMIT", "LIBREOFFICE_TEXT_LIMIT",
                    "LIBREOFFICE_PREVIEW_FAILED", "LIBREOFFICE_PREVIEW_LIMIT", "LIBREOFFICE_SOURCE_CHANGED"}
                raise LibreOfficeError(record.get("error") if record.get("error") in permitted else "LIBREOFFICE_HELPER_FAILED")
            if (record.get("schema") != "aura.libreoffice.helper.v1" or type(record.get("pages")) is not int
                    or not 1 <= record["pages"] <= 20 or record.get("source_sha256") != expected_source_sha256
                    or record.get("pdf") != "source.pdf" or record.get("previews") != ["page-%02d.png" % (i + 1) for i in range(record["pages"])]
                    or not isinstance(record.get("text"), str) or len(record["text"]) > 200000):
                raise LibreOfficeError("LIBREOFFICE_RESULT_INVALID")
            output = directory / "output"
            pdf = _read_bounded(output / "source.pdf", 32 * 1024**2)
            previews, used = [], 0
            for name in record["previews"]:
                image = _read_bounded(output / name, min(8 * 1024**2, 32 * 1024**2 - used))
                previews.append(image); used += len(image)
            previews = tuple(previews)
            if (not pdf.startswith(b"%PDF-") or len(pdf) > 32 * 1024**2 or _sha(pdf) != record.get("pdf_sha256")
                    or any(not image.startswith(b"\x89PNG\r\n\x1a\n") for image in previews)
                    or sum(map(len, previews)) > 32 * 1024**2 or _sha(source.read_bytes()) != expected_source_sha256):
                raise LibreOfficeError("LIBREOFFICE_RESULT_INVALID")
            result = RenderedOffice(pdf, previews, record["pages"], record["text"], expected_source_sha256, _sha(pdf))
        except LibreOfficeError as exc: failure = exc
        except Exception: failure = LibreOfficeError("LIBREOFFICE_RENDER_FAILED")
        finally:
            if process is not None and job is not None:
                run = _OwnedRun(native, job, process, directory, extra_handles=[null] if null else [],
                    evidence_owner=self if _execution is not None else None,
                    execution_identity=_execution.get("invocation") if _execution is not None else None,
                    source_sha256=expected_source_sha256)
                if not _cleanup(run):
                    with _lock: _pending.append(run)
                    failure = LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
                elif _execution is not None: _execution["cleanup_confirmed"] = True
            else:
                if null and null != C.c_void_p(-1).value and not native.k.CloseHandle(null):
                    with _lock: _unbound_handles.append((native, null))
                    failure = LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
                if job is not None and job.handle:
                    if native.k.CloseHandle(job.handle): job.handle = 0
                    else:
                        with _lock: _unbound_handles.append((native, job.handle))
                        failure = LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
                if directory is not None and directory_owned:
                    if has_pending_cleanup():
                        with _lock: _unbound_directories.append(directory)
                        failure = LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
                    else:
                        try: _remove_owned_directory(directory)
                        except Exception:
                            with _lock: _unbound_directories.append(directory)
                            failure = LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
        if failure is not None: raise failure
        return result
