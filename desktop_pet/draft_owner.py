"""Shared in-memory editor authority with one bounded background draft writer.

No Qt, network, model, attachment or conversation-message writes. The UI supplies
an archive identity reader and marshals notifications to its own thread.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import secrets
import threading
import time

from .draft_store import DraftSnapshot, DraftStorageError, MAX_TEXT_BYTES


@dataclass(frozen=True)
class ArchiveDraftIdentity:
    exists: bool
    generation: str
    deleted: bool = False


@dataclass(frozen=True)
class StartupDraftCandidate:
    conversation_id: str
    epoch: str
    revision: int
    blank_conversation_id: str
    blank_epoch: str
    blank_revision: int
    _issuer: object = field(repr=False, compare=False)


class DraftWriteTicket:
    def __init__(self, issuer):
        self._issuer, self._done, self._ok = issuer, threading.Event(), False

    def __repr__(self): return "DraftWriteTicket([host-owned local write])"


class DraftInvalidation(DraftWriteTicket):
    def __init__(self, issuer, previous, current, kind):
        super().__init__(issuer)
        self.conversation_id = current.conversation_id
        self.epoch = current.epoch
        self.kind = kind
        self._previous = previous
        self._before_identity = None

    def __repr__(self): return "DraftInvalidation([host-owned local clear])"


class LocalDraftOwner:
    """One owner per chat controller, shared by every panel.

    set_text/accepted/begin_invalidation do memory work only. flush queues a
    snapshot; wait_invalidation is for the controller's background clear/delete
    closure. shutdown waits only a caller-supplied bound and reports false if
    writes or the real daemon writer have not ended. It never claims unsaved
    memory is on disk and never sends a draft as a message.
    """
    def __init__(self, store=None, *, identity_reader=None, changed=None, loaded=None, clock=time.time):
        if store is not None and not callable(identity_reader): raise ValueError("DRAFT_IDENTITY_READER_REQUIRED")
        self._store, self._identity_reader, self._clock = store, identity_reader, clock
        self._changed, self._loaded_callback = changed, loaded
        self._issuer = object()
        self._condition = threading.Condition(threading.RLock())
        self._records, self._base, self._dirty = {}, {}, set()
        self._observed, self._accepted_empty = {}, set()
        self._live_empty = set()
        self._orphans = {}
        self._invalidations = {}
        self._candidate_taken = False
        self._jobs = deque()
        self._closing = False
        self._loaded = threading.Event()
        self._stopped = threading.Event()
        self.last_error = ""
        self._thread = threading.Thread(target=self._work, daemon=True, name="aura-local-drafts")
        self._thread.start()
        self._enqueue(self._load)

    def _fresh(self, cid, text="", *, generation="", epoch=None, revision=1):
        return DraftSnapshot(cid, epoch or secrets.token_hex(16), revision, generation, text, self._clock())

    def _notify(self, cid):
        if callable(self._changed):
            try: self._changed(cid)
            except Exception: pass

    def snapshot(self, cid):
        if type(cid) is not str or not cid or len(cid) > 200: return None
        with self._condition:
            if cid not in self._records: self._records[cid] = self._fresh(cid)
            return self._records[cid]

    def snapshots(self):
        with self._condition: return dict(self._records)

    def set_text(self, cid, text, *, epoch=None):
        if type(text) is not str: return None
        with self._condition:
            before = self.snapshot(cid)
            if before is None or epoch is not None and before.epoch != epoch: return None
            if before.text == text: return before
            value = replace(before, text=text, revision=before.revision+1, updated_at=self._clock())
            self._records[cid] = value; self._dirty.add(cid)
        self._notify(cid)
        return value

    def accepted(self, cid, *, epoch, revision):
        """Clear only the exact in-memory draft that the controller accepted."""
        with self._condition:
            current = self._records.get(cid)
            if current is None or (current.epoch, current.revision) != (epoch, revision): return False
            self._records[cid] = replace(current, text="", revision=current.revision+1, updated_at=self._clock())
            self._accepted_empty.add(cid); self._dirty.add(cid)
        self._notify(cid)
        self.flush()
        return True

    def _enqueue(self, callback, ticket=None):
        ticket = ticket or DraftWriteTicket(self._issuer)
        with self._condition:
            if self._closing or len(self._jobs) >= 32:
                ticket._done.set()
                return ticket
            self._jobs.append((callback, ticket)); self._condition.notify_all()
        return ticket

    def _work(self):
        try:
            while True:
                with self._condition:
                    while not self._jobs and not self._closing: self._condition.wait()
                    if not self._jobs: return
                    callback, ticket = self._jobs.popleft()
                try: ticket._ok = callback() is True
                except Exception:
                    ticket._ok = False
                    with self._condition: self.last_error = "DRAFT_STORAGE_UNAVAILABLE"
                finally:
                    ticket._done.set()
                    with self._condition: self._condition.notify_all()
        finally: self._stopped.set()

    def _identity(self, cid):
        value = self._identity_reader(cid) if self._store is not None else ArchiveDraftIdentity(False, "")
        if (type(value) is not ArchiveDraftIdentity or type(value.exists) is not bool
                or type(value.deleted) is not bool or type(value.generation) is not str):
            raise DraftStorageError()
        return value

    def _load(self):
        ok = True
        try:
            rows = self._store.load() if self._store is not None else ()
            for row in rows:
                identity = self._identity(row.conversation_id)
                valid = not identity.deleted and row.generation == identity.generation
                with self._condition:
                    self._base[row.conversation_id] = row
                    self._observed[row.conversation_id] = identity.generation
                    pending = self._invalidations.get(row.conversation_id)
                    if pending is not None and not pending._previous_dirty:
                        pending._previous = row
                    if not valid: continue
                    if not identity.exists:
                        self._orphans[row.conversation_id] = row
                        continue
                    if not identity.generation: self._live_empty.add(row.conversation_id)
                    current = self._records.get(row.conversation_id)
                    if row.conversation_id not in self._dirty:
                        self._records[row.conversation_id] = row
                    elif current is not None:
                        self._records[row.conversation_id] = replace(current, generation=identity.generation)
                self._notify(row.conversation_id)
        except Exception:
            ok = False
            with self._condition: self.last_error = "DRAFT_STORAGE_UNAVAILABLE"
        finally:
            self._loaded.set()
            if callable(self._loaded_callback):
                try: self._loaded_callback()
                except Exception: pass
        return ok

    def startup_candidate(self, current_cid):
        """One host-only offer, only before any new edit in the blank session."""
        with self._condition:
            current = self.snapshot(current_cid)
            if (not self._loaded.is_set() or self._candidate_taken or not current or current.text
                    or current_cid in self._dirty or not self._orphans): return None
            row = max(self._orphans.values(), key=lambda item: (item.updated_at, item.conversation_id))
            self._candidate_taken = True
            return StartupDraftCandidate(row.conversation_id, row.epoch, row.revision,
                current_cid, current.epoch, current.revision, self._issuer)

    def activate_candidate(self, candidate):
        with self._condition:
            if type(candidate) is not StartupDraftCandidate or candidate._issuer is not self._issuer: return False
            blank = self._records.get(candidate.blank_conversation_id)
            if (blank is None or blank.text or candidate.blank_conversation_id in self._dirty
                    or (blank.epoch, blank.revision) != (candidate.blank_epoch, candidate.blank_revision)):
                return False
            row = self._orphans.get(candidate.conversation_id)
            if row is None or (row.epoch, row.revision) != (candidate.epoch, candidate.revision): return False
            current = self._records.get(row.conversation_id)
            if current is not None and row.conversation_id in self._dirty: return False
            self._records[row.conversation_id] = row
            if not row.generation: self._live_empty.add(row.conversation_id)
            self._orphans.pop(row.conversation_id, None)
        self._notify(row.conversation_id)
        return True

    def revoke_candidate(self, cid):
        with self._condition: self._orphans.pop(cid, None)

    def _persist(self, value, *, invalidation=False):
        # An old debounce job must not write text that a later accepted send or
        # edit already superseded while that job was waiting in the queue.
        with self._condition:
            current = self._records.get(value.conversation_id)
            if current is not None and (current.epoch != value.epoch or current.revision > value.revision):
                value = current
        if self._store is None:
            with self._condition:
                self._base[value.conversation_id] = value
                if self._records.get(value.conversation_id) == value: self._dirty.discard(value.conversation_id)
            return True
        identity = self._identity(value.conversation_id)
        with self._condition:
            current = self._records.get(value.conversation_id)
            if current is not None and (current.epoch != value.epoch or current.revision > value.revision):
                value = current
            base = self._base.get(value.conversation_id)
            observed = self._observed.get(value.conversation_id)
            bind_empty = observed == "" and value.conversation_id in (self._accepted_empty | self._live_empty)
        if not invalidation:
            if identity.deleted: raise DraftStorageError("DRAFT_IDENTITY_CHANGED")
            if observed is not None and identity.generation != observed and not (bind_empty and identity.generation):
                raise DraftStorageError("DRAFT_IDENTITY_CHANGED")
        value = replace(value, generation=identity.generation)
        if base is None: base = self._store.lookup(value.conversation_id)
        if base is None:
            applied = self._store.save(value)
        elif base.epoch == value.epoch:
            applied = self._store.save(value, expected_empty_generation=bind_empty)
        else:
            applied = self._store.rotate(value, expected_epoch=base.epoch, expected_revision=base.revision)
        if not applied: raise DraftStorageError("DRAFT_VERSION_CONFLICT")
        with self._condition:
            self._base[value.conversation_id] = value
            self._observed[value.conversation_id] = identity.generation
            if not identity.generation: self._live_empty.add(value.conversation_id)
            else: self._live_empty.discard(value.conversation_id)
            current = self._records.get(value.conversation_id)
            if current is not None and (current.epoch, current.revision) == (value.epoch, value.revision):
                self._records[value.conversation_id] = value
                self._dirty.discard(value.conversation_id)
            self.last_error = ""
        return True

    def flush(self):
        with self._condition:
            values = [self._records[cid] for cid in self._dirty if cid in self._records]
        oversized = any(len(value.text) > MAX_TEXT_BYTES for value in values)
        values = [value for value in values if len(value.text) <= MAX_TEXT_BYTES]
        def persist():
            ok = not oversized
            if oversized:
                with self._condition: self.last_error = "DRAFT_INPUT_LIMIT"
            for value in values:
                try: ok = self._persist(value) and ok
                except Exception:
                    ok = False
                    with self._condition: self.last_error = "DRAFT_STORAGE_UNAVAILABLE"
            return ok
        return self._enqueue(persist)

    def archive_changed(self):
        """Bind a running owner's known-empty archive after its first commit.

        This narrow transition is never inferred at startup: a stale empty
        anchor loaded from disk cannot revive cleared/deleted text.
        """
        def refresh():
            with self._condition:
                orphans = tuple(self._orphans.values())
                candidates = [cid for cid in self._accepted_empty | self._live_empty if self._observed.get(cid) == ""
                              and cid in self._records]
            ok = True
            for row in orphans:
                try:
                    identity = self._identity(row.conversation_id)
                    if identity.exists or identity.deleted or identity.generation != row.generation:
                        self.revoke_candidate(row.conversation_id)
                except Exception:
                    # A failed freshness check is not permission to adopt a
                    # former blank session as an archived conversation.
                    self.revoke_candidate(row.conversation_id)
                    ok = False
            for cid in candidates:
                try:
                    identity = self._identity(cid)
                    if identity.deleted or not identity.exists or not identity.generation: continue
                    with self._condition:
                        current = self._records.get(cid)
                        if current is None or current.generation: continue
                        current = replace(current, generation=identity.generation, revision=current.revision+1,
                                          updated_at=self._clock())
                        self._records[cid] = current; self._dirty.add(cid)
                    ok = self._persist(current) and ok
                except Exception:
                    ok = False
                    with self._condition: self.last_error = "DRAFT_STORAGE_UNAVAILABLE"
            return ok
        return self._enqueue(refresh)

    def begin_invalidation(self, cid, *, kind="clear"):
        if kind not in {"clear", "delete"}: raise ValueError("DRAFT_INVALID_INVALIDATION")
        with self._condition:
            previous = self.snapshot(cid)
            if previous is None: raise ValueError("DRAFT_INVALID_IDENTITY")
            previous_dirty = cid in self._dirty
            current = self._fresh(cid, generation=previous.generation)
            self._records[cid] = current; self._dirty.add(cid)
            self._orphans.pop(cid, None)
            ticket = DraftInvalidation(self._issuer, previous, current, kind)
            ticket._previous_dirty = previous_dirty
            self._invalidations[cid] = ticket
        self._notify(cid)
        def invalidate():
            # Captured in the same single writer before the durable fence and
            # before the controller is allowed to attempt its main-DB mutator.
            ticket._before_identity = self._identity(cid)
            return self._persist(current, invalidation=True)
        return self._enqueue(invalidate, ticket)

    def wait_invalidation(self, ticket, timeout=1.0):
        """Background-only barrier, before changing the conversation archive."""
        if type(ticket) is not DraftInvalidation or ticket._issuer is not self._issuer: return False
        if not ticket._done.wait(max(0., min(float(timeout), 3.))): return False
        if not ticket._ok: return False
        if self._store is None: return True
        try:
            current = self._store.lookup(ticket.conversation_id)
            return current is not None and current.epoch == ticket.epoch
        except Exception: return False

    def finish_invalidation(self, ticket, *, success):
        """Background mutator outcome; fresh identity is required to restore.

        Call beside the actual archive mutator, including when GUI callbacks
        are suppressed during shutdown. Failure validation performs I/O and
        must not run in the GUI thread. Uncertain identity keeps the durable
        empty fence and privately retains the previous snapshot for this run.
        """
        if type(ticket) is not DraftInvalidation or ticket._issuer is not self._issuer or type(success) is not bool: return False
        if not success:
            try:
                after = self._identity(ticket.conversation_id)
                unchanged = ticket._before_identity is not None and after == ticket._before_identity and not after.deleted
            except Exception: unchanged = False
            if not unchanged:
                with self._condition: self.last_error = "DRAFT_ROLLBACK_UNCONFIRMED"
                return False
        with self._condition:
            current = self._records.get(ticket.conversation_id)
            if current is None or current.epoch != ticket.epoch: return False
            if not success and current.revision == 1 and not current.text:
                current = replace(current, text=ticket._previous.text, revision=2, updated_at=self._clock())
            elif success and ticket.kind == "clear" and current.generation:
                current = self._fresh(current.conversation_id, current.text)
                self._observed[current.conversation_id] = ""
            self._records[current.conversation_id] = current
            self._dirty.add(current.conversation_id)
            self._invalidations.pop(current.conversation_id, None)
        self._notify(current.conversation_id)
        self.flush()
        return True

    def wait(self, ticket, timeout=1.0):
        if type(ticket) is not DraftWriteTicket or ticket._issuer is not self._issuer: return False
        return ticket._done.wait(max(0., min(float(timeout), 3.))) and ticket._ok

    def shutdown(self, timeout=.35):
        with self._condition:
            already_closing = self._closing
        if already_closing:
            self._thread.join(max(0., min(float(timeout), 3.)))
            with self._condition: return self._stopped.is_set() and not self._dirty and not self.last_error
        self.archive_changed()
        ticket = self.flush()
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(max(0., min(float(timeout), 3.)))
        return ticket._done.is_set() and ticket._ok and self._stopped.is_set()
