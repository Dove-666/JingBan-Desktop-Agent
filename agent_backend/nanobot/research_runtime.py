"""Host-injected research helper, explicitly registered by the scoped parent.

The model-facing method accepts only task/path assignments. Parent identities,
revision/authority/cancel controls, provider config and call identity are supplied
by trusted worker code. Results are reference data, never parent completion.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from uuid import UUID

_ROOT = Path(__file__).resolve().parents[2]
_CODES = frozenset({"RESEARCH_PARENT_INACTIVE", "RESEARCH_SOURCE_CHANGED", "RESEARCH_SOURCE_UNAVAILABLE",
    "RESEARCH_READ_DENIED", "RESEARCH_SOURCE_BUDGET", "RESEARCH_SCOPE_INVALID", "RESEARCH_BATCH_INVALID",
    "RESEARCH_CLEANUP_UNCONFIRMED", "RESEARCH_LAUNCH_UNKNOWN", "RESEARCH_OWNER_UNCONFIRMED_OR_BUSY",
    "RESEARCH_CONFIGURATION_INVALID", "RESEARCH_GROUP_NOT_REPLAYABLE", "RESEARCH_CITATION_INVALID",
    "RESEARCH_REPLY_INVALID", "RESEARCH_MODEL_FAILED", "RESEARCH_AUTHORITY_UNAVAILABLE"})


class ResearchRuntimeError(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)


def _uuid(value):
    try: return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError, AttributeError): return False


def _host_types():
    # Only the trusted repository root is added. This is needed in the isolated
    # SDK interpreter, whose -I entry omits the application package directory.
    if str(_ROOT) not in sys.path:
        if os.environ.get("JINGBAN_PACKAGED") == "1": sys.path.append(str(_ROOT))
        else: sys.path.insert(0, str(_ROOT))
    from desktop_pet.agents.research_delegation import ResearchAuthority, ResearchGroup, ResearchError
    return ResearchAuthority, ResearchGroup, ResearchError


def _outcome(code, *, status="unverified", duplicate=False):
    return {"schema": "aura.research_batch_reference.v1", "status": status, "code": code,
        "reference_only": True, "claims_verified": False, "parent_goal_completed": False,
        "children": [], "duplicate": duplicate}


@dataclass
class _Batch:
    fingerprint: str
    revision: str
    epoch: int
    authority: object = None
    group: object = None
    scopes: object = None
    result: dict | None = None


class ResearchRuntime:
    def __init__(self, *, files, parent_run_id, conversation_id, current_goal_revision,
                 authorization_current, parent_cancellation, call_identity, config,
                 python_executable=None, authority_guard=None, known_secrets=(), deadline_seconds=None):
        if (not _uuid(parent_run_id) or not _uuid(conversation_id) or not callable(current_goal_revision)
                or not callable(authorization_current) or not callable(call_identity)
                or not callable(getattr(parent_cancellation, "is_set", None))
                or authority_guard is not None and not callable(authority_guard)
                or not isinstance(config, dict) or set(config) != {"base_url", "model", "api_key"}):
            raise ResearchRuntimeError("RESEARCH_HOST_CONTEXT_INVALID")
        self._Authority, self._Group, self._Error = _host_types()
        self._files, self._parent, self._cid = files, parent_run_id, conversation_id
        self._revision, self._authorized, self._parent_cancel = current_goal_revision, authorization_current, parent_cancellation
        self._call_identity, self._config, self._python = call_identity, dict(config), python_executable or sys.executable
        self._guard, self._secrets = authority_guard or nullcontext, tuple(known_secrets)
        self._deadline = deadline_seconds
        self._lock, self._serial = threading.RLock(), asyncio.Lock()
        self._epoch, self._closed, self._cleanup_barrier = 0, False, False
        self._unknown_groups = set()
        self._entries, self._seen, self._tasks = OrderedDict(), {}, set()

    def __repr__(self): return "ResearchRuntime([host-bound read-only integration])"

    def _current(self, revision, epoch):
        try:
            return (not self._closed and not self._parent_cancel.is_set() and self._epoch == epoch
                and self._revision() == revision and self._authorized() is True)
        except Exception: return False

    def _context(self):
        try:
            with self._guard():
                with self._lock:
                    revision, call = self._revision(), self._call_identity()
                    if (not isinstance(revision, str) or not 1 <= len(revision) <= 128
                            or not isinstance(call, str) or not 1 <= len(call) <= 256
                            or any(ord(char) < 32 for char in call)
                            or not self._current(revision, self._epoch)):
                        raise ResearchRuntimeError("RESEARCH_PARENT_INACTIVE")
                    key = hashlib.sha256(json.dumps([self._parent, self._cid, revision, self._epoch, call]).encode()).hexdigest()
                    return revision, self._epoch, key
        except ResearchRuntimeError: raise
        except Exception: raise ResearchRuntimeError("RESEARCH_HOST_CONTEXT_INVALID") from None

    def invalidate(self):
        """Host-only notification on user correction/permission invalidation."""
        with self._lock:
            self._epoch += 1
            entries = list(self._entries.values())
        for entry in entries:
            if entry.authority is not None: entry.authority.close()
            if entry.group is not None: entry.group.cancel()

    @staticmethod
    def _assignments(value):
        if not isinstance(value, list) or not 1 <= len(value) <= 8:
            raise ResearchRuntimeError("RESEARCH_ASSIGNMENTS_INVALID")
        prepared = []
        for row in value:
            if (not isinstance(row, dict) or set(row) != {"task", "paths"}
                    or not isinstance(row["task"], str) or not 1 <= len(row["task"].strip()) <= 4000
                    or not isinstance(row["paths"], list) or not 1 <= len(row["paths"]) <= 8
                    or any(not isinstance(path, str) or not 1 <= len(path) <= 4096 or "\0" in path for path in row["paths"])):
                raise ResearchRuntimeError("RESEARCH_ASSIGNMENTS_INVALID")
            prepared.append({"task": row["task"], "paths": list(row["paths"])})
        return prepared

    def _prepare(self, entry, assignments):
        authority = self._Authority(self._files, parent_id=self._parent, conversation_id=self._cid,
            revision_id=entry.revision, authorization_current=lambda: self._current(entry.revision, entry.epoch),
            cancellation_event=self._parent_cancel, known_secrets=(*self._secrets, self._config["api_key"]), authority_guard=self._guard)
        entry.authority = authority
        canonical, unique = [], []
        try:
            for assignment in assignments:
                paths = [str(self._files.resolve(path)) for path in assignment["paths"]]
                canonical.append(paths)
                for path in paths:
                    if path not in unique: unique.append(path)
        except Exception: raise self._Error("RESEARCH_SOURCE_UNAVAILABLE") from None
        if len(unique) > 8: raise self._Error("RESEARCH_SOURCE_BUDGET")
        parent = authority.open_scope(unique)
        manifest = authority.manifest(parent)
        refs = {path: row["source_id"] for path, row in zip(unique, manifest)}
        specs = [{"scope": authority.derive(parent, list(dict.fromkeys(refs[path] for path in paths))), "task": assignment["task"]}
                 for assignment, paths in zip(assignments, canonical)]
        group = self._Group(authority, python_executable=self._python, config=self._config, deadline_seconds=self._deadline)
        entry.group, entry.scopes = group, [row["scope"] for row in specs]
        return parent, specs

    async def _join(self, task, entry, *, on_cancel=None):
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # An already-cancelled child task is not cancellation of the
                # host's close operation. Still join its actual owner below.
                if on_cancel is not None and not task.cancelled(): on_cancel(exc)
                if entry.authority is not None: entry.authority.close()
                if entry.group is not None: entry.group.cancel()
                continue
            except Exception: break
        return task.result()

    async def _background(self, callback, entry, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
        self._tasks.add(task)
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            if entry.authority is not None: entry.authority.close()
            if entry.group is not None: entry.group.cancel()
            try: await self._join(task, entry)
            except (asyncio.CancelledError, Exception): pass
            raise
        finally:
            if task.done(): self._tasks.discard(task)

    def _references(self, entry, result, *, duplicate=False):
        if not self._current(entry.revision, entry.epoch): return _outcome("RESEARCH_PARENT_INACTIVE", duplicate=duplicate)
        if not result.get("children"):
            return {**result, "duplicate": duplicate}
        children = []
        checked = 0
        for scope, child in zip(entry.scopes, result["children"]):
            if child.get("status") != "reference_ready": children.append(child); continue
            raw = json.dumps({"conclusion": child["conclusion"], "citations": [
                {"read_id": row["read_id"], "quote": row["quote"]} for row in child["citations"]]})
            fresh = entry.authority.validate_conclusion(scope, child["child_id"], raw)
            children.append({**child, **fresh})
            checked += 1
        if not self._current(entry.revision, entry.epoch): return _outcome("RESEARCH_PARENT_INACTIVE", duplicate=duplicate)
        return {**result, "children": children, "duplicate": duplicate,
                "original_observations_reused": duplicate, "source_versions_rechecked": bool(checked),
                "verified_reference_children": checked}

    def _mark_unknown(self, group):
        with self._lock:
            self._unknown_groups.add(group)
            self._cleanup_barrier = True

    def _refresh_barrier(self):
        with self._lock:
            for group in tuple(self._unknown_groups):
                if group.cleanup_confirmed(): self._unknown_groups.remove(group)
            self._cleanup_barrier = bool(self._unknown_groups)
            return self._cleanup_barrier

    def _adopt(self, entry, result):
        # Source checks run off the event loop. Their completed future alone
        # does not authorize adoption after a correction received meanwhile.
        try:
            with self._guard():
                with self._lock:
                    if self._parent_cancel.is_set(): raise asyncio.CancelledError
                    if not self._current(entry.revision, entry.epoch):
                        return _outcome("RESEARCH_PARENT_INACTIVE", duplicate=result.get("duplicate", False))
                    return result
        except asyncio.CancelledError: raise
        except Exception: raise ResearchRuntimeError("RESEARCH_AUTHORITY_UNAVAILABLE") from None

    async def delegate_research(self, assignments):
        if self._parent_cancel.is_set(): raise asyncio.CancelledError
        assignments = self._assignments(assignments)
        fingerprint = hashlib.sha256(json.dumps(assignments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self._serial:
            if self._parent_cancel.is_set(): raise asyncio.CancelledError
            revision, epoch, key = self._context()
            if self._refresh_barrier(): raise ResearchRuntimeError("RESEARCH_CLEANUP_UNCONFIRMED")
            if key in self._seen:
                if self._seen[key] != fingerprint: return _outcome("RESEARCH_CALL_IDENTITY_CONFLICT", duplicate=True)
                entry = self._entries.get(key)
                if entry is None: return _outcome("RESEARCH_REFERENCE_EVICTED", duplicate=True)
                if entry.result is None: return _outcome("RESEARCH_CALL_OUTCOME_UNCONFIRMED", duplicate=True)
                try:
                    fresh = await self._background(self._references, entry, entry, entry.result, duplicate=True)
                    return self._adopt(entry, fresh)
                except self._Error as exc:
                    if self._parent_cancel.is_set(): raise asyncio.CancelledError
                    return _outcome(exc.code if exc.code in _CODES else "RESEARCH_RUNTIME_INTERRUPTED", duplicate=True)
            entry = _Batch(fingerprint, revision, epoch)
            self._seen[key], self._entries[key] = fingerprint, entry
            task = asyncio.create_task(asyncio.to_thread(self._prepare, entry, assignments))
            self._tasks.add(task)
            try:
                parent, specs = await asyncio.shield(task)
                self._tasks.discard(task)
                if not self._current(revision, epoch): raise self._Error("RESEARCH_PARENT_INACTIVE")
                task = asyncio.create_task(entry.group.arun(parent, specs))
                self._tasks.add(task)
                while not task.done():
                    if not self._current(revision, epoch):
                        entry.authority.close(); entry.group.cancel()
                    await asyncio.wait({task}, timeout=.02)
                children = await self._join(task, entry)
                if self._parent_cancel.is_set(): raise asyncio.CancelledError
                if not entry.group.cleanup_confirmed():
                    self._mark_unknown(entry.group)
                    entry.result = _outcome("RESEARCH_CLEANUP_UNCONFIRMED")
                    raise ResearchRuntimeError("RESEARCH_CLEANUP_UNCONFIRMED")
                if not self._current(revision, epoch):
                    result = _outcome("RESEARCH_PARENT_INACTIVE")
                else:
                    result = {"schema": "aura.research_batch_reference.v1", "status": "reference_ready" if all(
                        row.get("status") == "reference_ready" for row in children) else "unverified",
                        "reference_only": True, "claims_verified": False, "parent_goal_completed": False,
                        "children": children, "duplicate": False}
                    result = await self._background(self._references, entry, entry, result)
                result = self._adopt(entry, result)
                entry.result = result
                return result
            except asyncio.CancelledError:
                if entry.authority is not None: entry.authority.close()
                if entry.group is not None: entry.group.cancel()
                try: await self._join(task, entry)
                except (asyncio.CancelledError, Exception): pass
                if entry.group is not None and not entry.group.cleanup_confirmed(): self._mark_unknown(entry.group)
                entry.result = _outcome("RESEARCH_PARENT_CANCELLED", status="cancelled")
                raise
            except ResearchRuntimeError:
                raise
            except Exception as exc:
                if entry.group is not None and not entry.group.cleanup_confirmed():
                    self._mark_unknown(entry.group)
                    entry.result = _outcome("RESEARCH_CLEANUP_UNCONFIRMED")
                    raise ResearchRuntimeError("RESEARCH_CLEANUP_UNCONFIRMED") from None
                if self._parent_cancel.is_set():
                    if entry.authority is not None: entry.authority.close()
                    if entry.group is not None: entry.group.cancel()
                    entry.result = _outcome("RESEARCH_PARENT_CANCELLED", status="cancelled")
                    raise asyncio.CancelledError
                code = exc.code if isinstance(exc, self._Error) and exc.code in _CODES else "RESEARCH_RUNTIME_INTERRUPTED"
                entry.result = _outcome(code)
                return entry.result
            finally:
                self._tasks.discard(task)
                if entry.group is not None and not entry.group.cleanup_confirmed(): self._mark_unknown(entry.group)
                # Keep unresolved native owners. Only terminal, confirmed groups
                # may leave the payload cache; their identity tombstones remain.
                while len(self._entries) > 16:
                    expired_key, expired = next(iter(self._entries.items()))
                    if expired.group is not None and not expired.group.cleanup_confirmed(): break
                    if expired.authority is not None: expired.authority.close()
                    if expired.group is not None: expired.group.close()
                    self._entries.pop(expired_key)

    async def aclose(self):
        self._closed = True
        self.invalidate()
        interruption = None
        def remember(exc):
            nonlocal interruption
            if interruption is None: interruption = exc
        for task in list(self._tasks):
            dummy = _Batch("", "", 0)
            try: await self._join(task, dummy, on_cancel=remember)
            except asyncio.CancelledError: pass
            except Exception: pass
        for entry in list(self._entries.values()):
            if entry.group is not None:
                task = asyncio.create_task(asyncio.to_thread(entry.group.close))
                self._tasks.add(task)
                try: await self._join(task, entry, on_cancel=remember)
                except asyncio.CancelledError: pass
                except Exception: pass
                finally:
                    if task.done(): self._tasks.discard(task)
                if not entry.group.cleanup_confirmed(): self._mark_unknown(entry.group)
        barrier = self._refresh_barrier()
        if interruption is not None: raise interruption
        if barrier: raise ResearchRuntimeError("RESEARCH_CLEANUP_UNCONFIRMED")
        return {"schema": "aura.research_runtime_closure.v1", "children_closed": True}

    def assert_parent_ready(self):
        """Host before-iteration/final gate; never a model permission field."""
        if self._refresh_barrier():
            raise ResearchRuntimeError("RESEARCH_CLEANUP_UNCONFIRMED")
        if any(not task.done() for task in self._tasks):
            raise ResearchRuntimeError("RESEARCH_CHILDREN_ACTIVE")

    async def reconcile_children(self):
        """Host-only cleanup retry; never reissue a model/read task or adopt its answer."""
        for group in tuple(self._unknown_groups):
            entry = _Batch("", "", 0, group=group)
            await self._background(group.close, entry)
        self.assert_parent_ready()
        return {"schema": "aura.research_runtime_closure.v1", "children_closed": True}
