"""Reference-only host task ledger. No scheduler, worker or import-time IO.

The host must initialize/recover explicitly under its application instance
lock. Claiming a lease does not authorize dispatch; a fresh host scope check
must commit first. Unknown workers keep the serial slot until reconciled.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from uuid import UUID, uuid4


SCHEMA_VERSION = 3
_APPLICATION_ID = 0x41555131
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ARTIFACT = re.compile(r"artifact_[a-f0-9]{64}\Z")
STATES = frozenset({"queued", "claimed", "running", "awaiting_user", "cancel_requested",
    "completed", "failed", "cancelled", "timeout", "blocked", "interrupted", "unknown"})
_ACTIVE = frozenset({"claimed", "running", "awaiting_user", "cancel_requested"})
_REASONS = frozenset({"", "USER_CANCELLED", "CONVERSATION_REVOKED", "GENERATION_CHANGED", "SETTINGS_CHANGED",
    "SCOPE_RECHECK_FAILED", "LEASE_EXPIRED", "HOST_RESTART", "WORKER_FAILED", "WORKER_TIMEOUT",
    "WORKER_STOPPED", "WORKER_INTERRUPTED", "WORKER_OUTCOME_UNKNOWN", "SAVED_REPLY_CONFIRMED"})


class JobQueueError(RuntimeError):
    """Fixed safe code only; never expose a driver exception, token or path."""


def _fail(code):
    raise JobQueueError(code) from None


def _uuid(value):
    try: return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError, AttributeError): return False


def _refs(values):
    if not isinstance(values, (tuple, list)) or len(values) > 16 or any(not isinstance(value, str) or not _ARTIFACT.fullmatch(value) for value in values):
        _fail("QUEUE_INVALID_ARTIFACT_REFERENCE")
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class HostJobReference:
    conversation_id: str
    generation: str
    user_message_id: str
    idempotency_key: str
    settings_scope: str
    origin_peer_id: str | None = None

    def validate(self):
        if not all(_uuid(value) for value in (self.conversation_id, self.generation, self.user_message_id, self.idempotency_key)) or \
            not isinstance(self.settings_scope, str) or not _HASH.fullmatch(self.settings_scope) or \
            self.origin_peer_id is not None and (not isinstance(self.origin_peer_id, str) or not _HASH.fullmatch(self.origin_peer_id)):
            _fail("QUEUE_INVALID_REFERENCE")


class GuardianRunBinding:
    """Opaque host DB binding, never accepted from RPC/model dictionaries."""
    __slots__ = ("_issuer", "_job_id", "_reference", "_run_nonce", "_manifest_sha256")

    def __new__(cls, *args, **kwargs): raise TypeError("HOST_GUARDIAN_BINDING_REQUIRED")
    def __setattr__(self, *args): raise AttributeError("HOST_GUARDIAN_BINDING_IMMUTABLE")
    def __reduce__(self): raise TypeError("HOST_GUARDIAN_BINDING_NOT_SERIALIZABLE")
    def __repr__(self): return "GuardianRunBinding([host durable run identity])"
    @property
    def job_id(self): return self._job_id
    @property
    def reference(self): return self._reference
    @property
    def run_nonce(self): return self._run_nonce
    @property
    def manifest_sha256(self): return self._manifest_sha256

    def matches_manifest(self, manifest):
        try:
            from .job_guardian import canonical, validate_manifest
            validate_manifest(manifest)
            ref = self.reference
            return (manifest["schema"] == "aura.host_job_manifest.v2"
                and (manifest["job_id"], manifest["conversation_id"], manifest["generation"], manifest["user_message_id"],
                     manifest["operation_id"], manifest["settings_scope"], manifest["origin_peer_id"], manifest["run_nonce"]) ==
                    (self.job_id, ref.conversation_id, ref.generation, ref.user_message_id, ref.idempotency_key,
                     ref.settings_scope, ref.origin_peer_id, self.run_nonce)
                and hashlib.sha256(canonical(manifest)).hexdigest() == self.manifest_sha256)
        except Exception: return False


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    conversation_id: str
    generation: str
    user_message_id: str
    idempotency_key: str
    settings_scope: str
    state: str
    created_at: int
    updated_at: int
    lease_expires_at: int | None
    started_at: int | None
    cancel_requested_at: int | None
    worker_closed_at: int | None
    finished_at: int | None
    reply_id: str | None
    artifact_refs: tuple[str, ...]
    reason_code: str
    origin_peer_id: str | None = None


@dataclass(frozen=True)
class JobLease:
    job_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ClaimedJob:
    job: JobSnapshot
    lease: JobLease


@dataclass(frozen=True)
class HostDispatchScope:
    conversation_id: str
    generation: str
    settings_scope: str
    authorized: bool
    origin_peer_id: str | None = None


@dataclass(frozen=True)
class DispatchDecision:
    accepted: bool
    job: JobSnapshot
    code: str


@dataclass(frozen=True)
class SavedReplyProof:
    """Return only from a trusted archive/worker-closure verifier."""
    job_id: str
    conversation_id: str
    generation: str
    user_message_id: str
    reply_id: str
    complete: bool
    worker_closed: bool
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerStoppedProof:
    """Return only after the host confirms this exact worker is closed."""
    job_id: str
    conversation_id: str
    generation: str
    user_message_id: str
    worker_closed: bool


class JobQueue:
    def __init__(self, database_path, *, clock=time.time):
        if not isinstance(database_path, (str, Path)) or not str(database_path) or "\0" in str(database_path) or not callable(clock):
            _fail("QUEUE_INVALID_DATABASE")
        self._path = Path(database_path)
        self._clock = clock
        self._initialized = False
        self._guardian_issuer = object()

    def __repr__(self): return "JobQueue([host database])"

    def _now(self):
        try: value = self._clock()
        except Exception: _fail("QUEUE_INVALID_CLOCK")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10_000_000_000:
            _fail("QUEUE_INVALID_CLOCK")
        return int(value * 1000)

    def _physical_path(self):
        path = self._path.absolute()
        try:
            for candidate in (path, *(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")), *path.parents):
                try: value = candidate.lstat()
                except FileNotFoundError: continue
                if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
                    _fail("QUEUE_DATABASE_ALIAS_REJECTED")
        except OSError: _fail("QUEUE_STORAGE_UNAVAILABLE")
        return path

    @contextmanager
    def _connection(self, *, create=False):
        if not create and not self._initialized: _fail("QUEUE_NOT_INITIALIZED")
        db = None
        try:
            path = self._physical_path()
            if create: path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path.as_uri() + ("?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            if not create and (db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID or
                db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION): _fail("QUEUE_SCHEMA_MISMATCH")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except JobQueueError:
            if db is not None: db.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            if db is not None: db.rollback()
            _fail("QUEUE_STORAGE_UNAVAILABLE")
        finally:
            if db is not None: db.close()

    def initialize(self):
        with self._connection(create=True) as db:
            application = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if application == _APPLICATION_ID and version == SCHEMA_VERSION:
                if existing != {"jobs", "revocations", "guardian_runs"}: _fail("QUEUE_SCHEMA_MISMATCH")
            elif application == _APPLICATION_ID and version == 2 and existing == {"jobs", "revocations"}:
                self._create_guardian_table(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif application == _APPLICATION_ID and version == 1 and existing == {"jobs", "revocations"}:
                # Older rows have no authenticated remote provenance. Never
                # infer a peer from a current connection or repurpose a saved
                # idempotency key when resuming the host.
                db.execute("ALTER TABLE jobs ADD COLUMN origin_peer_id TEXT CHECK(origin_peer_id IS NULL OR "
                           "(length(origin_peer_id)=64 AND origin_peer_id NOT GLOB '*[^0-9a-f]*'))")
                self._create_guardian_table(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif application == 0 and version == 0 and not existing:
                state_values = ",".join("'" + value + "'" for value in sorted(STATES))
                reasons = ",".join("'" + value + "'" for value in sorted(_REASONS))
                db.execute(f"""CREATE TABLE jobs(
                    job_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, generation TEXT NOT NULL,
                    user_message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, settings_scope TEXT NOT NULL,
                    origin_peer_id TEXT CHECK(origin_peer_id IS NULL OR (length(origin_peer_id)=64 AND origin_peer_id NOT GLOB '*[^0-9a-f]*')),
                    state TEXT NOT NULL CHECK(state IN ({state_values})), created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    lease_hash TEXT, lease_expires_at INTEGER, started_at INTEGER, cancel_requested_at INTEGER,
                    worker_closed_at INTEGER, finished_at INTEGER, reply_id TEXT, artifact_refs TEXT NOT NULL DEFAULT '[]',
                    reason_code TEXT NOT NULL DEFAULT '' CHECK(reason_code IN ({reasons})),
                    UNIQUE(conversation_id,generation,user_message_id))""")
                db.execute("CREATE INDEX jobs_pending ON jobs(state)")
                db.execute("CREATE INDEX jobs_conversation ON jobs(conversation_id,generation)")
                db.execute("CREATE TABLE revocations(conversation_id TEXT NOT NULL,generation TEXT NOT NULL,revoked_at INTEGER NOT NULL,PRIMARY KEY(conversation_id,generation))")
                self._create_guardian_table(db)
                db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else: _fail("QUEUE_SCHEMA_MISMATCH")
        self._initialized = True

    @staticmethod
    def _create_guardian_table(db):
        db.execute("""CREATE TABLE guardian_runs(job_id TEXT PRIMARY KEY,
            run_nonce TEXT NOT NULL UNIQUE CHECK(length(run_nonce)=64 AND run_nonce NOT GLOB '*[^0-9a-f]*'),
            manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            manifest_schema TEXT NOT NULL CHECK(manifest_schema='aura.host_job_manifest.v2'))""")

    def _guardian_binding(self, db, job):
        row = db.execute("SELECT * FROM guardian_runs WHERE job_id=?", (job.job_id,)).fetchone()
        if row is None: return None
        if (job.origin_peer_id is None or not _HASH.fullmatch(row["run_nonce"])
                or not _HASH.fullmatch(row["manifest_sha256"]) or row["manifest_schema"] != "aura.host_job_manifest.v2"):
            _fail("QUEUE_GUARDIAN_BINDING_INVALID")
        value = object.__new__(GuardianRunBinding)
        reference = HostJobReference(job.conversation_id, job.generation, job.user_message_id, job.idempotency_key,
                                     job.settings_scope, job.origin_peer_id)
        for name, item in (("_issuer", self._guardian_issuer), ("_job_id", job.job_id), ("_reference", reference),
                           ("_run_nonce", row["run_nonce"]), ("_manifest_sha256", row["manifest_sha256"])):
            object.__setattr__(value, name, item)
        return value

    def bind_guardian_run(self, lease, manifest):
        """One immutable remote run, committed before ARM/Resume; no paths/key."""
        try:
            from .job_guardian import canonical, validate_manifest
            validate_manifest(manifest)
            if manifest["schema"] != "aura.host_job_manifest.v2": raise ValueError()
            digest = hashlib.sha256(canonical(manifest)).hexdigest()
        except Exception: _fail("QUEUE_GUARDIAN_BINDING_INVALID")
        with self._connection() as db:
            row = self._leased(db, lease, self._now(), {"running"})
            job = self._snapshot(row)
            ref = HostJobReference(job.conversation_id, job.generation, job.user_message_id, job.idempotency_key,
                                   job.settings_scope, job.origin_peer_id)
            if (ref.origin_peer_id is None or
                (manifest["job_id"], manifest["conversation_id"], manifest["generation"], manifest["user_message_id"],
                 manifest["operation_id"], manifest["settings_scope"], manifest["origin_peer_id"]) !=
                (job.job_id, ref.conversation_id, ref.generation, ref.user_message_id, ref.idempotency_key,
                 ref.settings_scope, ref.origin_peer_id)):
                _fail("QUEUE_GUARDIAN_BINDING_MISMATCH")
            old = self._guardian_binding(db, job)
            if old is not None:
                if not old.matches_manifest(manifest): _fail("QUEUE_GUARDIAN_RUN_ALREADY_BOUND")
                binding = old
            else:
                db.execute("INSERT INTO guardian_runs VALUES(?,?,?,?)", (job.job_id, manifest["run_nonce"], digest, manifest["schema"]))
                binding = self._guardian_binding(db, job)
        return binding  # Never publish the binding before SQLite COMMIT returns.

    def guardian_binding(self, job_id):
        with self._connection() as db:
            return self._guardian_binding(db, self._snapshot(self._row(db, job_id)))

    def unclosed_guardian_bindings(self, *, after_job_id=None, limit=100):
        if after_job_id is not None and not _uuid(after_job_id) or type(limit) is not int or not 1 <= limit <= 500:
            _fail("QUEUE_INVALID_REFERENCE")
        with self._connection() as db:
            rows = db.execute("""SELECT jobs.* FROM jobs JOIN guardian_runs USING(job_id)
                WHERE state IN ('unknown','interrupted','cancel_requested') AND worker_closed_at IS NULL
                AND job_id>? ORDER BY job_id LIMIT ?""", (after_job_id or "", limit)).fetchall()
            return tuple(self._guardian_binding(db, self._snapshot(row)) for row in rows)

    @staticmethod
    def _snapshot(row):
        if row is None: _fail("QUEUE_JOB_NOT_FOUND")
        try:
            refs = _refs(json.loads(row["artifact_refs"]))
            reference = HostJobReference(row["conversation_id"], row["generation"], row["user_message_id"], row["idempotency_key"], row["settings_scope"], row["origin_peer_id"])
            reference.validate()
            if not _uuid(row["job_id"]) or row["state"] not in STATES or row["reason_code"] not in _REASONS or \
                row["reply_id"] is not None and not _uuid(row["reply_id"]): _fail("QUEUE_STORAGE_CORRUPT")
            return JobSnapshot(row["job_id"], row["conversation_id"], row["generation"], row["user_message_id"], row["idempotency_key"],
                row["settings_scope"], row["state"], row["created_at"], row["updated_at"], row["lease_expires_at"], row["started_at"],
                row["cancel_requested_at"], row["worker_closed_at"], row["finished_at"], row["reply_id"], refs, row["reason_code"], row["origin_peer_id"])
        except JobQueueError: raise
        except Exception: _fail("QUEUE_STORAGE_CORRUPT")

    @staticmethod
    def _row(db, job_id):
        if not _uuid(job_id): _fail("QUEUE_INVALID_REFERENCE")
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None: _fail("QUEUE_JOB_NOT_FOUND")
        return row

    @staticmethod
    def _revoked(db, cid, generation):
        return db.execute("SELECT 1 FROM revocations WHERE conversation_id=? AND generation IN (?, '*')", (cid, generation)).fetchone() is not None

    def get(self, job_id):
        with self._connection() as db: return self._snapshot(self._row(db, job_id))

    def list_jobs(self, *, conversation_id=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500 or conversation_id is not None and not _uuid(conversation_id): _fail("QUEUE_INVALID_REFERENCE")
        with self._connection() as db:
            rows = db.execute("SELECT * FROM jobs" + (" WHERE conversation_id=?" if conversation_id else "") + " ORDER BY rowid LIMIT ?",
                (conversation_id, limit) if conversation_id else (limit,)).fetchall()
            return tuple(self._snapshot(row) for row in rows)

    def enqueue(self, reference):
        if not isinstance(reference, HostJobReference): _fail("QUEUE_INVALID_REFERENCE")
        reference.validate(); now = self._now()
        with self._connection() as db:
            now = self._now()
            old = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (reference.idempotency_key,)).fetchone()
            if old is not None:
                if (old["conversation_id"], old["generation"], old["user_message_id"], old["origin_peer_id"]) != (reference.conversation_id, reference.generation, reference.user_message_id, reference.origin_peer_id):
                    _fail("QUEUE_IDEMPOTENCY_CONFLICT")
                return self._snapshot(old)  # Never replace settings or restart a terminal job.
            if self._revoked(db, reference.conversation_id, reference.generation): _fail("QUEUE_SCOPE_REVOKED")
            if db.execute("SELECT 1 FROM jobs WHERE conversation_id=? AND generation=? AND user_message_id=?",
                (reference.conversation_id, reference.generation, reference.user_message_id)).fetchone(): _fail("QUEUE_MESSAGE_ALREADY_QUEUED")
            job_id = str(uuid4())
            db.execute("""INSERT INTO jobs(job_id,conversation_id,generation,user_message_id,idempotency_key,settings_scope,origin_peer_id,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'queued',?,?)""", (job_id, reference.conversation_id, reference.generation, reference.user_message_id,
                    reference.idempotency_key, reference.settings_scope, reference.origin_peer_id, now, now))
            return self._snapshot(self._row(db, job_id))

    def lookup(self, idempotency_key):
        """Host admission reconciliation; no enqueue, replay or scope update."""
        if not _uuid(idempotency_key): _fail("QUEUE_INVALID_REFERENCE")
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return self._snapshot(row) if row is not None else None

    def lookup_message(self, conversation_id, user_message_id):
        if not _uuid(conversation_id) or not _uuid(user_message_id): _fail("QUEUE_INVALID_REFERENCE")
        with self._connection() as db:
            rows = db.execute("SELECT * FROM jobs WHERE conversation_id=? AND user_message_id=? LIMIT 2",
                              (conversation_id, user_message_id)).fetchall()
            if len(rows) > 1: _fail("QUEUE_MESSAGE_IDENTITY_AMBIGUOUS")
            return self._snapshot(rows[0]) if rows else None

    def has_unclosed_worker(self):
        """Global recovery barrier, independent of UI pagination/history size."""
        with self._connection() as db:
            return db.execute("SELECT 1 FROM jobs WHERE state IN ('unknown','interrupted') "
                              "AND worker_closed_at IS NULL LIMIT 1").fetchone() is not None

    def has_image_maintenance_blocker(self):
        """Worker-only full-ledger check; query failure must block maintenance.

        Pending/active jobs block even if a closure timestamp is inconsistent.
        Every other state needs confirmed closure, including unknown outcomes.
        This ledger does not prove closure of unregistered Service resources.
        """
        with self._connection() as db:
            return bool(db.execute("""SELECT EXISTS(SELECT 1 FROM jobs
                WHERE state IN ('queued','claimed','running','awaiting_user','cancel_requested')
                OR worker_closed_at IS NULL)""").fetchone()[0])

    def cancel_queued(self, conversation_id):
        """Cancel all unclaimed jobs for one CID; active work needs real Stop.

        Return only rows changed by this transaction, independent of paginated
        terminal history. Repeated cancellation never invents new changes.
        """
        if not _uuid(conversation_id): _fail("QUEUE_INVALID_REFERENCE")
        with self._connection() as db:
            now = self._now()
            ids = [row[0] for row in db.execute("SELECT job_id FROM jobs WHERE conversation_id=? "
                                               "AND state='queued' ORDER BY rowid", (conversation_id,))]
            if not ids: return ()
            db.execute("""UPDATE jobs SET state='cancelled',reason_code='USER_CANCELLED',
                updated_at=max(updated_at,?),finished_at=?,worker_closed_at=?,lease_hash=NULL,lease_expires_at=NULL
                WHERE conversation_id=? AND state='queued'""", (now, now, now, conversation_id))
            return tuple(self._snapshot(self._row(db, job_id)) for job_id in ids)

    def _recover(self, db, now, *, restart=False):
        clause = "state IN ('claimed','running','awaiting_user','cancel_requested')" + ("" if restart else " AND lease_expires_at<=?")
        reason = "HOST_RESTART" if restart else "LEASE_EXPIRED"
        rows = db.execute("SELECT job_id,started_at FROM jobs WHERE " + clause, () if restart else (now,)).fetchall()
        for row in rows:
            # Claimed is conservative too. No automatic replay/reclaim.
            state = "unknown" if row["started_at"] is not None else "interrupted"
            db.execute("""UPDATE jobs SET state=?,reason_code=?,updated_at=max(updated_at,?),finished_at=?,
                lease_hash=NULL,lease_expires_at=NULL WHERE job_id=?""", (state, reason, now, now, row["job_id"]))
        return len(rows)

    def recover_expired(self):
        now = self._now()
        with self._connection() as db: return self._recover(db, self._now())

    def recover_after_restart(self):
        """Explicit host startup action under the application's instance lock."""
        now = self._now()
        with self._connection() as db: return self._recover(db, self._now(), restart=True)

    @staticmethod
    def _ttl(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 300: _fail("QUEUE_INVALID_LEASE")
        return math.ceil(value * 1000)

    def claim(self, *, ttl_seconds=30):
        ttl, now = self._ttl(ttl_seconds), self._now()
        with self._connection() as db:
            now = self._now()
            self._recover(db, now)
            if db.execute("""SELECT 1 FROM jobs WHERE state IN ('claimed','running','awaiting_user','cancel_requested')
                OR (state IN ('unknown','interrupted') AND worker_closed_at IS NULL) LIMIT 1""").fetchone(): return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None: return None
            if self._revoked(db, row["conversation_id"], row["generation"]): _fail("QUEUE_SCOPE_REVOKED")
            token = secrets.token_hex(32); digest = hashlib.sha256(token.encode()).hexdigest()
            changed = db.execute("""UPDATE jobs SET state='claimed',lease_hash=?,lease_expires_at=?,updated_at=max(updated_at,?)
                WHERE job_id=? AND state='queued'""", (digest, now + ttl, now, row["job_id"])).rowcount
            if changed != 1: _fail("QUEUE_CLAIM_CONFLICT")
            return ClaimedJob(self._snapshot(self._row(db, row["job_id"])), JobLease(row["job_id"], token))

    def _leased(self, db, lease, now, states=_ACTIVE):
        if not isinstance(lease, JobLease) or not _uuid(lease.job_id) or not isinstance(lease.token, str) or not _HASH.fullmatch(lease.token): _fail("QUEUE_INVALID_LEASE")
        row = self._row(db, lease.job_id)
        expected = hashlib.sha256(lease.token.encode()).hexdigest()
        if not isinstance(row["lease_hash"], str) or not secrets.compare_digest(row["lease_hash"], expected): _fail("QUEUE_LEASE_MISMATCH")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= now: _fail("QUEUE_LEASE_EXPIRED")
        if row["state"] not in states: _fail("QUEUE_STATE_CONFLICT")
        return row

    def heartbeat(self, lease, *, ttl_seconds=30):
        ttl, now = self._ttl(ttl_seconds), self._now()
        with self._connection() as db:
            now = self._now()
            row = self._leased(db, lease, now, {"claimed", "running", "awaiting_user"})
            db.execute("UPDATE jobs SET lease_expires_at=max(lease_expires_at,?),updated_at=max(updated_at,?) WHERE job_id=? AND lease_hash=?",
                (now + ttl, now, row["job_id"], row["lease_hash"]))
            return self._snapshot(self._row(db, row["job_id"]))

    def begin_dispatch(self, lease, *, revalidate):
        if not callable(revalidate): _fail("QUEUE_HOST_REVALIDATION_REQUIRED")
        now = self._now()
        with self._connection() as db:
            now = self._now()
            row = self._leased(db, lease, now, {"claimed"}); snapshot = self._snapshot(row)
            try: current = revalidate(snapshot)
            except Exception: current = None
            now = self._now()
            row = self._leased(db, lease, now, {"claimed"})
            reason = ("CONVERSATION_REVOKED" if self._revoked(db, row["conversation_id"], row["generation"]) else
                "SCOPE_RECHECK_FAILED" if not isinstance(current, HostDispatchScope) or current.authorized is not True or current.conversation_id != row["conversation_id"] or current.origin_peer_id != row["origin_peer_id"] else
                "GENERATION_CHANGED" if current.generation != row["generation"] else
                "SETTINGS_CHANGED" if current.settings_scope != row["settings_scope"] else "")
            if reason:
                db.execute("""UPDATE jobs SET state='blocked',reason_code=?,updated_at=max(updated_at,?),finished_at=?,worker_closed_at=?,
                    lease_hash=NULL,lease_expires_at=NULL WHERE job_id=? AND state='claimed' AND lease_hash=?""",
                    (reason, now, now, now, row["job_id"], row["lease_hash"]))
                return DispatchDecision(False, self._snapshot(self._row(db, row["job_id"])), "QUEUE_" + reason)
            db.execute("UPDATE jobs SET state='running',started_at=?,updated_at=max(updated_at,?) WHERE job_id=? AND state='claimed' AND lease_hash=?",
                (now, now, row["job_id"], row["lease_hash"]))
            return DispatchDecision(True, self._snapshot(self._row(db, row["job_id"])), "QUEUE_DISPATCH_CONFIRMED")

    def set_awaiting_user(self, lease, *, awaiting=True):
        if type(awaiting) is not bool: _fail("QUEUE_INVALID_STATE")
        now = self._now()
        with self._connection() as db:
            now = self._now()
            row = self._leased(db, lease, now, {"running"} if awaiting else {"awaiting_user"})
            db.execute("UPDATE jobs SET state=?,updated_at=max(updated_at,?) WHERE job_id=? AND lease_hash=?",
                ("awaiting_user" if awaiting else "running", now, row["job_id"], row["lease_hash"]))
            return self._snapshot(self._row(db, row["job_id"]))

    def request_cancel(self, job_id):
        now = self._now()
        with self._connection() as db:
            now = self._now()
            row = self._row(db, job_id)
            if row["state"] == "queued":
                db.execute("UPDATE jobs SET state='cancelled',reason_code='USER_CANCELLED',updated_at=max(updated_at,?),cancel_requested_at=?,worker_closed_at=?,finished_at=? WHERE job_id=?",
                    (now, now, now, now, job_id))
            elif row["state"] in _ACTIVE:
                db.execute("UPDATE jobs SET state='cancel_requested',reason_code='USER_CANCELLED',updated_at=max(updated_at,?),cancel_requested_at=coalesce(cancel_requested_at,?) WHERE job_id=?",
                    (now, now, job_id))
            return self._snapshot(self._row(db, job_id))

    def revoke_conversation(self, conversation_id, *, generation=None):
        if not _uuid(conversation_id) or generation is not None and not _uuid(generation): _fail("QUEUE_INVALID_REFERENCE")
        now = self._now()
        with self._connection() as db:
            now = self._now()
            db.execute("INSERT OR IGNORE INTO revocations VALUES(?,?,?)", (conversation_id, generation or "*", now))
            where = "conversation_id=?" + (" AND generation=?" if generation else "")
            parameters = (conversation_id, generation) if generation else (conversation_id,)
            db.execute("""UPDATE jobs SET state='cancelled',reason_code='CONVERSATION_REVOKED',updated_at=max(updated_at,?),
                cancel_requested_at=?,worker_closed_at=?,finished_at=? WHERE """ + where + " AND state='queued'", (now, now, now, now, *parameters))
            db.execute("""UPDATE jobs SET state='cancel_requested',reason_code='CONVERSATION_REVOKED',updated_at=max(updated_at,?),
                cancel_requested_at=coalesce(cancel_requested_at,?) WHERE """ + where + " AND state IN ('claimed','running','awaiting_user','cancel_requested')", (now, now, *parameters))

    def host_acknowledge_stopped(self, lease, *, outcome, worker_stopped):
        reasons = {"cancelled": "WORKER_STOPPED", "failed": "WORKER_FAILED", "timeout": "WORKER_TIMEOUT",
                   "interrupted": "WORKER_INTERRUPTED", "unknown": "WORKER_OUTCOME_UNKNOWN"}
        if not isinstance(outcome, str) or outcome not in reasons or worker_stopped is not True: _fail("QUEUE_WORKER_STOP_UNCONFIRMED")
        now = self._now()
        with self._connection() as db:
            now = self._now()
            row = self._leased(db, lease, now)
            db.execute("""UPDATE jobs SET state=?,reason_code=?,updated_at=max(updated_at,?),worker_closed_at=?,finished_at=?,
                lease_hash=NULL,lease_expires_at=NULL WHERE job_id=? AND lease_hash=?""",
                (outcome, reasons[outcome], now, now, now, row["job_id"], row["lease_hash"]))
            return self._snapshot(self._row(db, row["job_id"]))

    @staticmethod
    def _proof_identity(proof, job):
        return (proof.job_id, proof.conversation_id, proof.generation, proof.user_message_id) == \
            (job.job_id, job.conversation_id, job.generation, job.user_message_id)

    def reconcile_worker_stopped(self, job_id, *, verifier, guardian_binding=None):
        if not callable(verifier): _fail("QUEUE_HOST_PROOF_REQUIRED")
        now = self._now()
        with self._connection() as db:
            row = self._row(db, job_id); job = self._snapshot(row)
            if guardian_binding is not None:
                current = self._guardian_binding(db, job)
                if (type(guardian_binding) is not GuardianRunBinding or guardian_binding._issuer is not self._guardian_issuer
                        or current is None or guardian_binding.job_id != current.job_id
                        or guardian_binding.reference != current.reference or guardian_binding.run_nonce != current.run_nonce
                        or guardian_binding.manifest_sha256 != current.manifest_sha256):
                    _fail("QUEUE_GUARDIAN_BINDING_MISMATCH")
            if job.state not in {"unknown", "interrupted", "cancel_requested"}: _fail("QUEUE_STATE_CONFLICT")
            try: proof = verifier(job)
            except Exception: proof = None
            if not isinstance(proof, WorkerStoppedProof) or proof.worker_closed is not True or not self._proof_identity(proof, job): _fail("QUEUE_PROOF_REJECTED")
            now = self._now()
            state = "cancelled" if job.state == "cancel_requested" else job.state
            db.execute("UPDATE jobs SET state=?,worker_closed_at=?,updated_at=max(updated_at,?),finished_at=coalesce(finished_at,?),lease_hash=NULL,lease_expires_at=NULL WHERE job_id=?",
                (state, now, now, now, job_id))
            return self._snapshot(self._row(db, job_id))

    def reconcile_completed(self, job_id, reply_id, *, verifier, lease=None):
        if not _uuid(reply_id) or not callable(verifier): _fail("QUEUE_HOST_PROOF_REQUIRED")
        now = self._now()
        with self._connection() as db:
            now = self._now()
            row = self._row(db, job_id); job = self._snapshot(row)
            if job.state == "completed":
                if job.reply_id != reply_id: _fail("QUEUE_STATE_CONFLICT")
                return job
            if self._revoked(db, job.conversation_id, job.generation): _fail("QUEUE_SCOPE_REVOKED")
            if job.state in _ACTIVE:
                self._leased(db, lease, now, {"running", "awaiting_user", "cancel_requested"})
            elif job.state in {"unknown", "interrupted"}:
                if lease is not None: _fail("QUEUE_LEASE_MISMATCH")
            else: _fail("QUEUE_STATE_CONFLICT")
            try: proof = verifier(job, reply_id)
            except Exception: proof = None
            if not isinstance(proof, SavedReplyProof) or proof.complete is not True or proof.worker_closed is not True or \
                proof.reply_id != reply_id or not self._proof_identity(proof, job): _fail("QUEUE_PROOF_REJECTED")
            now = self._now()
            if job.state in _ACTIVE:
                self._leased(db, lease, now, {"running", "awaiting_user", "cancel_requested"})
            artifacts = _refs(proof.artifact_refs)
            db.execute("""UPDATE jobs SET state='completed',reason_code='SAVED_REPLY_CONFIRMED',reply_id=?,artifact_refs=?,
                updated_at=max(updated_at,?),worker_closed_at=?,finished_at=?,lease_hash=NULL,lease_expires_at=NULL WHERE job_id=?""",
                (reply_id, json.dumps(artifacts), now, now, now, job_id))
            return self._snapshot(self._row(db, job_id))
