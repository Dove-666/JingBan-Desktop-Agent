"""Host-owned read-only research scopes and isolated child execution.

Host-minted scopes can only narrow existing WorkspaceFiles read capabilities.
Child text is reference data. Only exact issued, redacted excerpts and current
source versions are verified here; factual conclusions still need parent review.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import threading
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from agent_backend.nanobot.redaction import redact_text

_REF = re.compile(r"src_[a-f0-9]{32}\Z")
_CHILD = re.compile(r"research_[a-f0-9]{32}\Z")
_GROUP_OWNERS = set()
_GROUP_OWNERS_LOCK = threading.Lock()


class ResearchError(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)


def _uuid(value):
    try: return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (TypeError, ValueError, AttributeError): return False


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ResearchError("RESEARCH_REPLY_INVALID")
        value[key] = item
    return value


class ResearchScope:
    __slots__ = ("_issuer", "_nonce", "_sources")
    def __new__(cls, *args, **kwargs): raise TypeError("HOST_RESEARCH_SCOPE_REQUIRED")
    def __setattr__(self, *args): raise AttributeError("HOST_RESEARCH_SCOPE_IMMUTABLE")
    def __repr__(self): return "ResearchScope([host-owned read-only references])"


@dataclass(frozen=True)
class _Source:
    path: str
    sha256: str
    label: str
    characters: int
    redacted: bool


@dataclass(frozen=True)
class _Read:
    scope: ResearchScope
    child_id: str
    source_id: str
    sha256: str
    offset: int
    text: str


class ResearchAuthority:
    def __init__(self, files, *, parent_id, conversation_id, revision_id, authorization_current,
                 cancellation_event=None, known_secrets=(), authority_guard=None):
        if (not _uuid(parent_id) or not _uuid(conversation_id) or not isinstance(revision_id, str)
                or not 1 <= len(revision_id) <= 128 or not callable(authorization_current)
                or not callable(getattr(files, "read_bytes", None))
                or authority_guard is not None and not callable(authority_guard)):
            raise ResearchError("RESEARCH_PARENT_INVALID")
        self._files, self._parent_id, self._cid, self._revision = files, parent_id, conversation_id, revision_id
        self._authorized, self._cancel = authorization_current, cancellation_event or threading.Event()
        self._authority_guard = authority_guard or nullcontext
        self._secrets = tuple(value for value in known_secrets if isinstance(value, str) and value)
        self._issuer, self._scopes, self._sources, self._reads = object(), {}, {}, {}
        self._closed = False
        self.lock = threading.RLock()

    def redact(self, text): return redact_text(text, self._secrets)

    def bind_model_secrets(self, known_secrets):
        """Host-only policy narrowing before any excerpt has been issued."""
        with self.guard():
            if self._reads: raise ResearchError("RESEARCH_REDACTION_POLICY_LOCKED")
            self._secrets = tuple(sorted(set(self._secrets) | {value for value in known_secrets if isinstance(value, str) and value}))

    def _active(self):
        try: authorized = self._authorized() is True
        except Exception: authorized = False
        if self._closed or self._cancel.is_set() or not authorized:
            raise ResearchError("RESEARCH_PARENT_INACTIVE")

    @contextmanager
    def guard(self):
        # Production wiring must supply the SAME reentrant host guard used by
        # grant/revision revocation. The default only checks at callback time;
        # an unrelated internal RLock cannot make external authority atomic.
        try:
            with self._authority_guard():
                with self.lock:
                    self._active()
                    yield
                    self._active()
        except ResearchError: raise
        except Exception: raise ResearchError("RESEARCH_AUTHORITY_UNAVAILABLE") from None

    def _valid(self, scope):
        self._active()
        if (type(scope) is not ResearchScope or getattr(scope, "_issuer", None) is not self._issuer
                or self._scopes.get(getattr(scope, "_nonce", None)) is not scope):
            raise ResearchError("RESEARCH_SCOPE_INVALID")
        return scope

    def _mint(self, references):
        scope = object.__new__(ResearchScope)
        for field, value in (("_issuer", self._issuer), ("_nonce", secrets.token_hex(16)), ("_sources", tuple(references))):
            object.__setattr__(scope, field, value)
        self._scopes[scope._nonce] = scope
        return scope

    def open_scope(self, paths):
        """Host-only enrollment; the delegated file capability cannot be expanded."""
        with self.guard():
            if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 8 or len(self._sources) + len(paths) > 32:
                raise ResearchError("RESEARCH_SOURCE_BUDGET")
            prepared = []
            for value in paths:
                try:
                    target, raw, sha = self._files.read_bytes(value, maximum=262144)
                    text = raw.decode("utf-8-sig")
                except Exception: raise ResearchError("RESEARCH_SOURCE_UNAVAILABLE") from None
                safe = self.redact(text)
                prepared.append(("src_" + secrets.token_hex(16), _Source(str(target), sha, self.redact(Path(target).name), len(safe), safe != text)))
            for reference, record in prepared: self._sources[reference] = record
            return self._mint([reference for reference, _ in prepared])

    def derive(self, parent, source_ids):
        with self.guard():
            self._valid(parent)
            if (not isinstance(source_ids, (list, tuple)) or not source_ids
                    or any(not isinstance(value, str) or value not in parent._sources for value in source_ids)
                    or len(set(source_ids)) != len(source_ids)):
                raise ResearchError("RESEARCH_SCOPE_WIDENING_DENIED")
            return self._mint(source_ids)

    def is_subscope(self, parent, child):
        with self.guard():
            self._valid(parent); self._valid(child)
            return set(child._sources) <= set(parent._sources)

    def _current_text(self, reference):
        record = self._sources[reference]
        try:
            _, raw, sha = self._files.read_bytes(record.path, maximum=262144)
            if sha != record.sha256: raise ValueError("changed")
            text = raw.decode("utf-8-sig")
            safe = self.redact(text)
            if safe != text and not record.redacted:
                self._sources[reference] = _Source(record.path, record.sha256, self.redact(Path(record.path).name), len(safe), True)
            return safe
        except Exception: raise ResearchError("RESEARCH_SOURCE_CHANGED") from None

    def manifest(self, scope):
        with self.guard():
            self._valid(scope)
            value = []
            for reference in scope._sources:
                text = self._current_text(reference)
                record = self._sources[reference]
                value.append({"source_id": reference, "sha256": record.sha256, "label": self.redact(Path(record.path).name), "characters": len(text)})
            return value

    def read_excerpt(self, scope, child_id, source_id, offset=0, limit=4000):
        with self.guard():
            self._valid(scope)
            if (not isinstance(child_id, str) or not _CHILD.fullmatch(child_id)
                    or not isinstance(source_id, str) or source_id not in scope._sources
                    or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4000):
                raise ResearchError("RESEARCH_READ_DENIED")
            if len(self._reads) >= 256: raise ResearchError("RESEARCH_READ_BUDGET")
            # Redact the full source BEFORE slicing. Otherwise adjacent reads
            # could reveal two halves of a known secret and reassemble it.
            redacted = self._current_text(source_id)[offset:offset + limit]
            identity = "read_" + secrets.token_hex(16)
            self._reads[identity] = _Read(scope, child_id, source_id, self._sources[source_id].sha256, offset, redacted)
            return {"read_id": identity, "source_id": source_id, "source_sha256": self._sources[source_id].sha256,
                    "offset": offset, "text": redacted, "redacted": self._sources[source_id].redacted, "reference_only": True}

    def validate_conclusion(self, scope, child_id, raw):
        with self.guard():
            self._valid(scope)
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16384: raise ResearchError("RESEARCH_REPLY_INVALID")
            try: value = json.loads(raw, object_pairs_hook=_unique)
            except (ValueError, TypeError, RecursionError): raise ResearchError("RESEARCH_REPLY_INVALID") from None
            if (not isinstance(value, dict) or set(value) != {"conclusion", "citations"}
                    or not isinstance(value["conclusion"], str) or not 1 <= len(value["conclusion"]) <= 4000
                    or not isinstance(value["citations"], list) or not 1 <= len(value["citations"]) <= 8):
                raise ResearchError("RESEARCH_REPLY_INVALID")
            citations = []
            for citation in value["citations"]:
                if (not isinstance(citation, dict) or set(citation) != {"read_id", "quote"}
                        or not isinstance(citation["read_id"], str) or not isinstance(citation["quote"], str)
                        or not 1 <= len(citation["quote"]) <= 2000): raise ResearchError("RESEARCH_CITATION_INVALID")
                quote = self.redact(citation["quote"])
                issued = self._reads.get(citation["read_id"])
                if (issued is None or issued.scope is not scope or issued.child_id != child_id
                        or not quote or quote not in issued.text): raise ResearchError("RESEARCH_CITATION_INVALID")
                self._current_text(issued.source_id)
                citations.append({"source_id": issued.source_id, "source_sha256": issued.sha256,
                    "read_id": citation["read_id"], "excerpt_offset": issued.offset, "quote": quote})
            return {"schema": "aura.research_reference.v1", "child_id": child_id, "status": "reference_ready",
                "reference_only": True, "claims_verified": False, "source_references_verified": True,
                "conclusion": self.redact(value["conclusion"]), "citations": citations}

    def close(self):
        # Revocation must not wait behind a slow host filesystem read. In-flight
        # guards check again before returning; no subsequent LF/merge can adopt
        # their data. Native cleanup remains a separate exact-owner operation.
        self._closed = True


@dataclass(eq=False)
class _ChildRun:
    child_id: str
    state: object
    process: object = None
    handle: object = None
    writers: object = None
    launch_attempted: bool = False
    dispatched: bool = False
    finished: bool = False
    closed: bool = False
    model_requests: int = 0
    model_responses: int = 0


class _CancelFlags:
    def __init__(self, group): self.group = group
    def is_set(self):
        # No authority/Qt callback or other lock from the input watchdog's
        # condition lock. Host mutation ordering uses authority_guard above.
        return self.group._cancel.is_set() or self.group.authority._cancel.is_set() or self.group.authority._closed


class ResearchGroup:
    """Single standalone parent batch, at most two real child workers in flight.

    Parent registry exposure belongs to the host. Unknown launch/cleanup stays strongly
    tracked and blocks another batch for this authority. Cancel never claims a
    remote model request was undone, and no model/tool call is automatically
    replayed after a failure.
    """
    def __init__(self, authority, *, python_executable, config, deadline_seconds=None):
        from .process_lifecycle import WorkerLifecycle
        from .settings import validate_api
        if (type(authority) is not ResearchAuthority or not isinstance(config, dict)
                or set(config) != {"base_url", "model", "api_key"}
                or any(not isinstance(value, str) or not value or len(value) > 4096 for value in config.values())
                or deadline_seconds is not None and (isinstance(deadline_seconds, bool)
                    or not isinstance(deadline_seconds, (int, float)) or not 1 <= deadline_seconds <= 300)):
            raise ResearchError("RESEARCH_CONFIGURATION_INVALID")
        try: validate_api(config["base_url"], config["model"])
        except Exception: raise ResearchError("RESEARCH_CONFIGURATION_INVALID") from None
        executable = Path(python_executable)
        if not executable.is_absolute() or not executable.is_file() or not executable.name.lower().startswith("python"):
            raise ResearchError("RESEARCH_CONFIGURATION_INVALID")
        self.authority, self._python, self._config = authority, executable, dict(config)
        self._deadline, self._lifecycle = deadline_seconds, WorkerLifecycle()
        self._cancel, self._lock = threading.Event(), threading.RLock()
        self._runs, self._used, self._launch_unknown = [], False, False
        with authority.guard():
            if getattr(authority, "_research_group", None) is not None:
                raise ResearchError("RESEARCH_GROUP_ALREADY_BOUND")
            authority.bind_model_secrets([config["api_key"]])
            authority._research_group = self  # Retain unknown owners, never reconstruct them from PIDs.

    def __repr__(self): return "ResearchGroup([host-owned read-only children])"

    def cancel(self):
        self._cancel.set()
        with self._lock: runs = [run for run in self._runs if not run.closed]
        for run in runs:
            if run.handle is not None: self._lifecycle.request_stop(run.handle)
            elif run.process is not None:
                # Exact returned Popen ownership still permits Stop if initial
                # lifecycle registration failed; absence of a receipt stays unknown.
                try: run.process.kill()
                except Exception: pass

    def _active(self):
        if self._cancel.is_set(): raise ResearchError("RESEARCH_CANCELLED")
        self.authority._active()

    def _send(self, run, value, scope):
        from .staged_input import StagedRunInput
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 131072: raise ResearchError("RESEARCH_PROTOCOL_INVALID")
        writer = StagedRunInput(run.process.stdin, stop_owned=lambda: self._lifecycle.request_stop(run.handle), cancellation_event=_CancelFlags(self))
        run.writers.append(writer)
        writer.start()
        try:
            writer.stage(raw)
            with self.authority.guard():
                self._active(); self.authority.manifest(scope)
                self._lifecycle.mark_dispatched(run.handle)
                run.dispatched = True
                writer.commit()
        finally:
            if writer.close(timeout_seconds=1): run.writers.remove(writer)

    def _settle(self, run):
        try: return self._settle_owned(run)
        except Exception: return False

    def _settle_owned(self, run):
        if run.closed: return True
        if run.handle is None:
            if run.process is None or run.dispatched: return False
            # This is cleanup of the exact returned object, never a new launch
            # or a PID reconstruction. No run command could be committed before
            # the first registration, so exact pre-dispatch reaping is enough.
            run.handle = self._lifecycle.register(run.process, run.child_id, self.authority._cid)
        for writer in list(run.writers):
            if not writer.close(timeout_seconds=.2):
                self._lifecycle.request_stop(run.handle); return False
            run.writers.remove(writer)
        receipt = self._lifecycle.settle(run.handle, timeout_seconds=2)
        if receipt is None or not self._lifecycle.validates(run.handle, receipt): return False
        for pipe in (run.process.stdin, run.process.stdout):
            try: pipe.close()
            except Exception: return False
        try: run.state.cleanup()
        except Exception: return False
        run.closed = True
        return True

    def _child(self, parent_scope, scope, task):
        from .service import _ProcessTree, _read_available
        child = "research_" + secrets.token_hex(16)
        run = _ChildRun(child, tempfile.TemporaryDirectory(prefix="aura-research-sdk-"), writers=[])
        with self._lock: self._runs.append(run)
        raw_reply, error = None, None
        begun = time.monotonic()
        worker = Path(__file__).resolve().parents[2] / "agent_backend/nanobot/research_worker.py"
        try:
            with self.authority.guard():
                self._active()
                if not self.authority.is_subscope(parent_scope, scope): raise ResearchError("RESEARCH_SCOPE_WIDENING_DENIED")
                sources = self.authority.manifest(scope)
            environment = {name: value for name, value in os.environ.items()
                if not re.search(r"API_?KEY|TOKEN|SECRET|PASSWORD|PYTHONPATH|OPENAI|ANTHROPIC|NANOBOT", name, re.I)}
            environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", OPENAI_LOG="error")
            with self.authority.guard():
                self._active()
                run.launch_attempted = True
                try:
                    run.process = subprocess.Popen([str(self._python), "-I", "-B", "-u", str(worker)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, bufsize=0,
                        **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}))
                except Exception:
                    self._launch_unknown = True
                    raise ResearchError("RESEARCH_LAUNCH_UNKNOWN") from None
                run.handle = self._lifecycle.register(run.process, child, self.authority._cid)
                self._lifecycle.attach_tree(run.handle, _ProcessTree(run.process))
            buffer, ready = b"", False
            while raw_reply is None:
                with self.authority.guard(): self._active()
                if self._deadline is not None and time.monotonic() - begun > self._deadline:
                    raise ResearchError("RESEARCH_DEADLINE")
                if not ready and time.monotonic() - begun > 15: raise ResearchError("RESEARCH_STARTUP_TIMEOUT")
                chunk = _read_available(run.process.stdout)
                buffer += chunk
                if len(buffer) > 131072: raise ResearchError("RESEARCH_PROTOCOL_INVALID")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try: frame = json.loads(line, object_pairs_hook=_unique)
                    except Exception: raise ResearchError("RESEARCH_PROTOCOL_INVALID") from None
                    if not ready:
                        if frame != {"type": "ready", "protocol": 1, "role": "research"}: raise ResearchError("RESEARCH_PROTOCOL_INVALID")
                        self._send(run, {"type": "research", "child_id": child, "task": self.authority.redact(task),
                            "sources": sources, "config": self._config, "state_dir": run.state.name}, scope)
                        ready = True; continue
                    if not isinstance(frame, dict) or frame.get("child_id") != child: raise ResearchError("RESEARCH_PROTOCOL_INVALID")
                    kind = frame.get("type")
                    if kind in {"model_request", "model_response"} and set(frame) == {"type", "child_id"}:
                        if kind == "model_request": run.model_requests += 1
                        else: run.model_responses += 1
                    elif kind == "read" and set(frame) == {"type", "child_id", "request_id", "source_id", "offset", "limit"}:
                        if not isinstance(frame["request_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", frame["request_id"]):
                            raise ResearchError("RESEARCH_PROTOCOL_INVALID")
                        reply = {"type": "read_reply", "child_id": child, "request_id": frame["request_id"]}
                        try:
                            value = self.authority.read_excerpt(scope, child, frame["source_id"], frame["offset"], frame["limit"])
                            reply.update(ok=True, value=value)
                        except ResearchError: reply.update(ok=False, code="RESEARCH_READ_DENIED")
                        self._send(run, reply, scope)
                    elif kind == "reference" and set(frame) == {"type", "child_id", "content"}:
                        raw_reply = frame["content"]
                    elif kind == "error" and set(frame) == {"type", "child_id", "code"}:
                        raise ResearchError("RESEARCH_MODEL_FAILED")
                    else: raise ResearchError("RESEARCH_PROTOCOL_INVALID")
                if run.process.poll() is not None and not chunk and raw_reply is None: raise ResearchError("RESEARCH_CHILD_EXITED")
                if raw_reply is None: self._cancel.wait(.02)
        except ResearchError as exc: error = exc.code
        except Exception: error = "RESEARCH_EXECUTION_INTERRUPTED"
        finally:
            run.finished = True
            if run.process is not None:
                if not self._settle(run): error = "RESEARCH_CLEANUP_UNCONFIRMED"
            elif not run.launch_attempted:
                try: run.state.cleanup(); run.closed = True
                except Exception: error = "RESEARCH_CLEANUP_UNCONFIRMED"
            else:
                self._launch_unknown = True; error = "RESEARCH_LAUNCH_UNKNOWN"
        if error is None:
            try:
                with self.authority.guard():
                    self._active()
                    value = self.authority.validate_conclusion(scope, child, raw_reply)
                    # Validation can read several source versions. A Stop that
                    # arrived during that IO must still win before adoption.
                    self._active()
            except ResearchError as exc: error = exc.code
        if error is not None:
            value = {"schema": "aura.research_reference.v1", "child_id": child, "reference_only": True,
                     "status": "cancelled" if self._cancel.is_set() else "unverified", "code": error,
                     "claims_verified": False, "source_references_verified": False}
        outcome = "not_started" if not run.dispatched else "unknown" if raw_reply is None or run.model_requests > run.model_responses else "observed"
        value.update(worker_closed=run.closed, model_requests=run.model_requests, model_responses=run.model_responses,
                     model_request_outcome=outcome)
        return value

    def run(self, parent_scope, specs):
        if not isinstance(specs, (list, tuple)) or not 1 <= len(specs) <= 8:
            raise ResearchError("RESEARCH_BATCH_INVALID")
        with self._lock:
            if self._used or self._launch_unknown or any(not run.closed for run in self._runs):
                raise ResearchError("RESEARCH_GROUP_NOT_REPLAYABLE")
            self._used = True
        with self.authority.guard():
            self.authority._valid(parent_scope)
            for spec in specs:
                if (not isinstance(spec, dict) or set(spec) != {"scope", "task"}
                        or not isinstance(spec["task"], str) or not 1 <= len(spec["task"]) <= 4000
                        or not self.authority.is_subscope(parent_scope, spec["scope"])):
                    raise ResearchError("RESEARCH_BATCH_INVALID")
        with _GROUP_OWNERS_LOCK:
            if _GROUP_OWNERS: raise ResearchError("RESEARCH_OWNER_UNCONFIRMED_OR_BUSY")
            _GROUP_OWNERS.add(self)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="aura-readonly-research") as pool:
                futures = [pool.submit(self._child, parent_scope, spec["scope"], spec["task"]) for spec in specs]
                return [future.result() for future in futures]
        finally:
            if self.cleanup_confirmed():
                with _GROUP_OWNERS_LOCK: _GROUP_OWNERS.discard(self)

    async def arun(self, parent_scope, specs):
        import asyncio
        task = asyncio.create_task(asyncio.to_thread(self.run, parent_scope, specs))
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.cancel()
            # Cancellation requests exact Stop; it does not itself prove that
            # the execution thread or worker has finished. Strong owner state
            # survives a repeated cancellation while this join is pending.
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    self.cancel()
                    # Another Stop may interrupt this join, but cannot make a
                    # still-running child/thread count as already settled.
                    if task.done(): break
                except Exception:
                    break
            raise

    def close(self):
        self.cancel()
        self.authority.close()
        with self._lock: runs = [run for run in self._runs if run.finished and not run.closed]
        for run in runs:
            if run.process is not None: self._settle(run)
            elif not run.launch_attempted:
                try: run.state.cleanup(); run.closed = True
                except Exception: pass
        if self.cleanup_confirmed():
            with _GROUP_OWNERS_LOCK: _GROUP_OWNERS.discard(self)

    def cleanup_confirmed(self):
        with self._lock: return not self._launch_unknown and all(run.closed for run in self._runs)
