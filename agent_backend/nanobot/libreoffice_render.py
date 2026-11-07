"""Host-injected LibreOffice adapter; no discovery, launch policy or registration.

The model-facing callable keeps render_document's existing argument schema.
Only the host can construct/bind this adapter and its renderer. No PNG, profile,
runtime executable or extra file capability is exposed to a model.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading

try:
    from .office_render import OfficeRenderError, inspect_package
    from .safe_files import MAX_DOCUMENT_BYTES
    from .document_previews import PagePreviewCache
except ImportError:  # Isolated worker's trusted HERE directory, not model PATH.
    from office_render import OfficeRenderError, inspect_package
    from safe_files import MAX_DOCUMENT_BYTES
    from document_previews import PagePreviewCache

_retained = set()  # Keep unresolved renderer ownership alive after caller loss.
_retained_lock = threading.Lock()
_ERROR_MAP = {
    "LIBREOFFICE_PROTOTYPE_DISABLED": "OFFICE_RUNTIME_UNAVAILABLE",
    "LIBREOFFICE_RUNTIME_UNAVAILABLE": "OFFICE_RUNTIME_UNAVAILABLE",
    "LIBREOFFICE_RUNTIME_CHANGED": "OFFICE_RUNTIME_UNAVAILABLE",
    "LIBREOFFICE_DEPENDENCY_UNAVAILABLE": "OFFICE_RUNTIME_UNAVAILABLE",
    "LIBREOFFICE_BUSY": "OFFICE_BUSY",
    "LIBREOFFICE_INPUT_INVALID": "OFFICE_INVALID_ARGUMENT",
    "LIBREOFFICE_TIMEOUT": "OFFICE_TIMEOUT",
    "LIBREOFFICE_CANCELLED": "OFFICE_CANCELLED",
    "LIBREOFFICE_CLEANUP_UNCONFIRMED": "OFFICE_CLEANUP_UNCONFIRMED",
    "LIBREOFFICE_SOURCE_CHANGED": "OFFICE_SOURCE_CHANGED",
    "LIBREOFFICE_PDF_LIMIT": "OFFICE_PDF_LIMIT",
    "LIBREOFFICE_RESULT_LIMIT": "OFFICE_PDF_LIMIT",
}
_INPUT_CODES = frozenset({"OFFICE_UNSUPPORTED_FORMAT", "OFFICE_UNSAFE_PACKAGE",
    "OFFICE_ACTIVE_CONTENT_REJECTED", "OFFICE_EXTERNAL_LINK_REJECTED", "OFFICE_UNSUPPORTED_FORMULA"})


@dataclass(frozen=True)
class _Outcome:
    result: object = None
    code: str | None = None
    closed: bool = False
    evidence: object = None


def _error(code, *, safe=False, unknown=False):
    error = OfficeRenderError(code)
    error.safe_no_publication = safe is True
    if unknown: error.effect_state = "unknown"
    return error


def _file_error(error):
    # Map only exact fixed codes; never return paths, OS messages or repr(error).
    code = str(error) if type(error) in {ValueError, FileExistsError} else ""
    if code in {"FILE_CHANGED", "FILE_EXISTS"}: return "OFFICE_SOURCE_CHANGED"
    if code == "FILE_UNAVAILABLE": return "OFFICE_OPEN_FAILED"
    if code in {"INVALID_PATH", "OUTSIDE_WORKSPACE", "INVALID_EXPECTED_HASH", "READ_BEFORE_OVERWRITE", "INVALID_WRITE"}:
        return "OFFICE_INVALID_ARGUMENT"
    return "OFFICE_RENDER_FAILED"


def _cache_previews(cache, files, path, digest, result):
    """Optional metadata only; never turn an already published PDF into failure."""
    try:
        if type(cache) is not PagePreviewCache or cache._files is not files: return []
        metadata = cache.publish(str(path), digest, result.previews, pages=result.pages)
        if type(metadata) is not list or len(metadata) != result.pages: return []
        keys = {"reference", "page", "width", "height", "pdf_sha256", "png_sha256"}
        for index, page in enumerate(metadata, 1):
            if (type(page) is not dict or any(type(key) is not str for key in page) or set(page) != keys
                    or type(page["reference"]) is not str or not re.fullmatch(r"page_[a-f0-9]{32}", page["reference"])
                    or type(page["page"]) is not int or page["page"] != index
                    or type(page["width"]) is not int or not 1 <= page["width"] <= 1800
                    or type(page["height"]) is not int or not 1 <= page["height"] <= 1800
                    or type(page["pdf_sha256"]) is not str or page["pdf_sha256"] != digest or type(page["png_sha256"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", page["png_sha256"])):
                return []
        return metadata
    except Exception: return []


class LibreOfficeRenderAdapter:
    """One host-owned renderer binding with a sticky unknown-cleanup barrier."""

    def __init__(self, renderer, *, cleanup_reconciler=None):
        if not callable(getattr(renderer, "render", None)) or not callable(getattr(renderer, "validate_cleanup_evidence", None)):
            raise ValueError("HOST_RENDERER_REQUIRED")
        if cleanup_reconciler is not None and not callable(cleanup_reconciler):
            raise ValueError("HOST_RECONCILER_REQUIRED")
        self._renderer = renderer
        self._reconciler = cleanup_reconciler
        self._lock = threading.Lock()
        self._active = False
        self._blocked = False
        self._future = None
        self._executor = None
        self._invocation = None
        self._source_sha256 = None
        self._cleanup_evidence = None

    def host_status(self):
        """Metadata only. Configured does not claim the runtime was probed."""
        with self._lock:
            return {"configured": True, "busy": self._active, "cleanup_unknown": self._blocked,
                "runtime_probe_performed": False}

    def reconcile_cleanup(self):
        """Explicit host call; never an automatic retry or model-facing tool."""
        with self._lock:
            if self._active or self._future is not None and not self._future.done(): return False
            if not self._blocked: return True
            if self._reconciler is None: return False
            if self._executor is not None:
                try: self._executor.shutdown(wait=True, cancel_futures=False)
                except BaseException: return False
                self._executor = None
            try:
                evidence = self._cleanup_evidence
                if not self._renderer.validate_cleanup_evidence(evidence,
                        invocation=self._invocation, source_sha256=self._source_sha256):
                    evidence = self._reconciler(invocation=self._invocation, source_sha256=self._source_sha256)
                confirmed = self._renderer.validate_cleanup_evidence(evidence,
                    invocation=self._invocation, source_sha256=self._source_sha256) is True
            except BaseException: confirmed = False
            if not confirmed: return False
            self._blocked = False
            self._future = None
            with _retained_lock: _retained.discard(self)
            return True

    def _invoke(self, raw, suffix, digest, event, timeout, identity):
        try:
            result = self._renderer.render(raw, suffix, expected_source_sha256=digest,
                cancel=event, timeout_seconds=timeout, execution_identity=identity)
            evidence = getattr(result, "cleanup_evidence", None)
            closed = self._renderer.validate_cleanup_evidence(evidence, invocation=identity, source_sha256=digest) is True
            return _Outcome(result=result, closed=closed, evidence=evidence if closed else None)
        except BaseException as error:
            try:
                evidence = getattr(error, "cleanup_evidence", None)
                closed = self._renderer.validate_cleanup_evidence(evidence, invocation=identity, source_sha256=digest) is True
            except BaseException: closed = False
            code = getattr(error, "code", None)
            mapped = code if code in _INPUT_CODES else _ERROR_MAP.get(code, "OFFICE_RENDER_FAILED")
            # A failed cleanup is never upgraded by a contradictory receipt.
            if mapped == "OFFICE_CLEANUP_UNCONFIRMED": closed = False
            return _Outcome(code=mapped, closed=closed, evidence=evidence if closed else None)

    async def render_document(self, path, output_path=None, *, files, overwrite=False,
                              expected_hash=None, timeout_seconds=90, host_scope_guard=None, preview_cache=None):
        with self._lock:
            if self._blocked:
                raise _error("OFFICE_CLEANUP_UNCONFIRMED", unknown=True)
            if self._active: raise _error("OFFICE_BUSY", safe=True)
            self._active = True
        publication_started = False
        closed = True  # No renderer has been submitted during preflight.
        cancelled = False
        executor = None
        try:
            if (type(overwrite) is not bool or type(timeout_seconds) not in {int, float}
                    or not math.isfinite(timeout_seconds) or not 5 <= timeout_seconds <= 120):
                raise _error("OFFICE_INVALID_ARGUMENT", safe=True)
            try:
                if host_scope_guard is not None: host_scope_guard()
                target, raw, digest = files.read_bytes(path)
                suffix = target.suffix.casefold()
                inspect_package(raw, suffix)
                destination = output_path if output_path is not None else str(target.with_name(target.stem + ".rendered.pdf"))
                resolved = files.resolve(destination, write=True)
                if resolved.suffix.casefold() != ".pdf" or resolved == target:
                    raise _error("OFFICE_INVALID_ARGUMENT", safe=True)
                with files._lock:
                    initial_version = files._version_for_write(resolved, overwrite, expected_hash)
                    initially_absent = not resolved.exists()
            except OfficeRenderError as error:
                raise _error(error.code, safe=True) from None
            except Exception as error:
                raise _error(_file_error(error), safe=True) from None
            event, identity = threading.Event(), object()
            with self._lock:
                self._invocation, self._source_sha256 = identity, digest
                self._cleanup_evidence = None
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aura-office-render")
            closed = False
            future = executor.submit(self._invoke, raw, suffix, digest, event, timeout_seconds, identity)
            with self._lock: self._future = future
            wrapped = asyncio.wrap_future(future)
            # Shield the actual concurrent future. Repeated Task.cancel() only
            # sets Stop again; it cannot abandon the retained thread/helper.
            while True:
                try:
                    outcome = await asyncio.shield(wrapped)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    event.set()
                    if future.done():
                        outcome = future.result()
                        break
                    if wrapped.cancelled(): wrapped = asyncio.wrap_future(future)
            closed = outcome.closed is True
            if closed:
                with self._lock: self._cleanup_evidence = outcome.evidence
            if not closed:
                with self._lock: self._blocked = True
                with _retained_lock: _retained.add(self)
            if cancelled:
                error = asyncio.CancelledError()
                error.safe_no_publication = closed
                if not closed: error.effect_state = "unknown"
                raise error
            if outcome.code is not None:
                raise _error(outcome.code, safe=closed, unknown=not closed)
            if not closed: raise _error("OFFICE_CLEANUP_UNCONFIRMED", unknown=True)
            result = outcome.result
            try:
                pdf, text = result.pdf, result.text
                if (type(pdf) is not bytes or not pdf.startswith(b"%PDF-") or not 0 < len(pdf) <= MAX_DOCUMENT_BYTES
                        or type(result.pages) is not int or not 1 <= result.pages <= 20
                        or type(text) is not str or len(text) > 200000
                        or result.source_sha256 != digest or result.pdf_sha256 != hashlib.sha256(pdf).hexdigest()):
                    raise _error("OFFICE_PDF_INVALID", safe=True)
                # One WorkspaceFiles lock keeps cooperative scope/version
                # updates out of the readback→CAS publication interval.
                with files._lock:
                    if host_scope_guard is not None: host_scope_guard()
                    current_target, _, current_digest = files.read_bytes(path)
                    if current_target != target or current_digest != digest:
                        raise _error("OFFICE_SOURCE_CHANGED", safe=True)
                    if files.resolve(destination, write=True) != resolved:
                        raise _error("OFFICE_SOURCE_CHANGED", safe=True)
                    if initially_absent and resolved.exists():
                        raise _error("OFFICE_SOURCE_CHANGED", safe=True)
                    files._version_for_write(resolved, overwrite, initial_version)
                    publication_started = True
                    written = json.loads(files.write_bytes(destination, pdf, overwrite, initial_version))
                preview_pages = _cache_previews(preview_cache, files, destination, written["sha256"], result)
                return json.dumps({"status": "rendered", "source": files.display_path(target),
                    "source_sha256": digest, "written": written["written"], "sha256": written["sha256"],
                    "bytes": written["bytes"], "artifact": written["artifact"], "pages": result.pages,
                    "preview_status": "available" if preview_pages else "unavailable", "preview_pages": preview_pages,
                    "engine": "LibreOffice 26.8.2 headless", "pdf_text": text[:24000],
                    "pdf_text_truncated": len(text) > 24000, "native_text": "", "native_text_truncated": False,
                    "verification_scope": "actual_libreoffice_pdf_text_and_raster_pages",
                    "microsoft_office_rendered": False, "visual_layout_verified": False,
                    "previews_published": False, "cleanup_confirmed": True,
                    "scope": "libreoffice_pdf_export; semantic_and_visual_layout_require_separate_checks"}, ensure_ascii=False)
            except OfficeRenderError: raise
            except Exception as error:
                raise _error(_file_error(error), safe=not publication_started, unknown=publication_started) from None
        except (OfficeRenderError, asyncio.CancelledError): raise
        except BaseException:
            if not closed:
                with self._lock: self._blocked = True
                with _retained_lock: _retained.add(self)
            raise _error("OFFICE_RENDER_FAILED", safe=closed and not publication_started,
                         unknown=not closed or publication_started) from None
        finally:
            executor_failed = False
            if executor is not None:
                # Normally already done: never claim native closure merely
                # because the executor has stopped its Python thread.
                try: executor.shutdown(wait=True, cancel_futures=False)
                except BaseException:
                    executor_failed = True
                    with self._lock:
                        self._blocked = True
                        self._executor = executor
                    with _retained_lock: _retained.add(self)
            with self._lock:
                self._active = False
                if closed and not executor_failed: self._future = None
            if executor_failed: raise _error("OFFICE_CLEANUP_UNCONFIRMED", unknown=True) from None


async def render_document(files, path, output_path=None, *, overwrite=False, expected_hash=None,
                          timeout_seconds=90, renderer=None, host_scope_guard=None, preview_cache=None):
    """Host-only injection parameter is excluded from the existing tool schema."""
    if not isinstance(renderer, LibreOfficeRenderAdapter):
        raise _error("OFFICE_RUNTIME_UNAVAILABLE", safe=True)
    return await renderer.render_document(path, output_path, files=files, overwrite=overwrite,
        expected_hash=expected_hash, timeout_seconds=timeout_seconds, host_scope_guard=host_scope_guard,
        preview_cache=preview_cache)
