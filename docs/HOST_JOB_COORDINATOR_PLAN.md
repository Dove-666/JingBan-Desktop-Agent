# Host coordinator integration

The reference ledger is now connected to authenticated remote admission and the
existing ChatController execution path. `HostAgentCoordinator` does not call a
model or spawn a worker; it serially selects a saved request and calls
`_begin_chat(..., job_context=...)`. Ordinary phone/direct-API chat is not
automatically enrolled as a computer Agent task.

## Admission and identity

- `agent.send` may acknowledge a durable queued job while another task is busy.
  It first saves the genuine user message with private host origin, then commits
  the queue reference before returning an ACK. The queue stores references and
  hashes, not prompts, credentials, file paths or raw receipts.
- Stable message/idempotency UUIDs bind the authenticated peer and original RPC
  request UUID. Duplicate requests recover the same job. An uncertain queue
  commit is read back by this exact identity; a failed or unreadable enrollment
  is not automatically replayed or silently reauthorized.
- The origin binds peer, pairing incarnation, actual CID generation and a
  complete settings/credential-identity digest. Imported phone messages and
  public sync metadata cannot mint it. Revoke/re-pair of the same peer does not
  revive its old consent.
- A fresh authenticated submission may create a canonical phone CID without
  switching the selected desktop conversation. Persistent tombstones still
  reject a deleted CID after restart, independently of in-memory deleted sets.
- Only a desktop ACK means the task reached the host queue. Offline phone
  outbox retention remains a separate transport stage. `agent.queue.v1`
  negotiates busy admission; old peers keep their previous busy behavior.

## Fresh dispatch and cancellation

The coordinator shares one short RLock with admission, settings publication,
pairing revocation, conversation invalidation and cancellation. Qt-owned
settings/credential reads publish an immutable in-memory policy under that lock;
worker-thread guards never read QSettings, the credential vault or UI objects.
An uncertain settings read disables the policy; an unchanged actual policy does
not unnecessarily cancel work.

For each claim the host reloads the real message and checks origin, current
pairing/enable switches, CID generation, unreserved user identity and settings
digest. The existing `reserve_agent_execution` marker remains authoritative.
The lifecycle callback binds the exact opaque WorkerRunHandle as soon as the
owned Popen is registered. A separate synchronous dispatch guard checks fresh
authority and the exact live lease before the first LF and each durable tool
checkpoint ACK. Expired leases fail closed even before a recovery scan.

Startup waits for the actual worker's versioned ready frame. The escaped run
JSON is staged without CR/LF outside the authority lock. Only a fresh guarded
single-byte LF commits it. EOF without LF is rejected by the worker reader.
One owned watchdog bounds startup writes and can stop only that run. Incomplete
writes are poisoned and never completed/replayed. Both watchdog termination and
active writer exit are required before a closure receipt can be issued.

`agent.cancel(cid)` cancels every still-queued job for that CID in one ledger
transaction, and invokes the existing commit-ordered cancellation path for its
current task. It returns active and queued cancellation separately. It never
claims that a task already committing its reply was stopped. Other CIDs remain
queued and start only after exact closure and the UI request's final release.

## Context and final proof

A task's model history is bounded through its own user message, while retaining
already committed replies to earlier users. Future queued tasks are excluded;
their texts remain intact in the archive. A shared summary without turn
provenance is not attached to queued jobs. Default memory lookup also excludes
pending/queued/failed messages; an explicit phone query remains explicit.

Only an actual saved full reply for the exact CID/generation/user-message,
accepted whole-task result, validated artifact references and exact worker
closure may create `SavedReplyProof`. Partial/needs-user output remains
interrupted in the queue rather than completed. Errors in queue reconciliation
cannot erase an already committed chat reply, but block further Agent effects.
An empty registry, busy=false, Stop response or final worker JSON is never
closure evidence.

Dispatch is event-driven. A five-second heartbeat exists only while an active
lease or unresolved cleanup needs attention; an idle queue has no periodic DB
poll. Worker-finally and every UI request release both wake the queue, including
stale callbacks after cancellation.

## Conservative restart boundary

Startup recovers claimed/running work to interrupted/unknown and never replays
it. Any recovered job whose worker closure is unproved blocks **all** computer
Agent dispatch, including desktop automatic routing. Ordinary model chat stays
available. Current opaque handles are process-local; neither a missing PID nor
an empty new registry reconstructs their proof.

Durable orphan containment/guardian identity recovery and its user-facing
reconciliation flow are **not implemented**. A service call that fails before
any Popen attempt may release only its own job using a separate host-issued
NoWorkerLaunchReceipt, bound to the exact finished invocation and opaque request
owner. A missing handle or public error code alone still proves nothing. Once
Popen was attempted, even if it raised, this receipt is unavailable and existing
exact-worker/unknown protections remain. See [no-launch evidence](HOST_NO_LAUNCH_PROOF_PLAN.md).
This MVP provides safe in-process serial
execution and persistent admission, not automatic recovery of unknown effects.

## Evidence and remaining validation

The ledger suite uses real SQLite with injected clocks. Remote queue integration
tests additionally use actual authenticated RPC, Qt ChatController, isolated SDK
workers and synthetic localhost planner/judge channels. Independent review tests
cover cancellation ordering, restart, stale authority, large histories, settings
thread boundaries and real cross-CID handoff after Stop. They do not contact a
real model, Docker or the user's desktop and do not count toward benchmark model
success rates. Native Android/network end-to-end and real-model task results
must be recorded separately against their own source/build snapshots.
