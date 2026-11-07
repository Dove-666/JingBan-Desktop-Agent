# Suspended guardian worker adapter: proposed production wiring

This is the original integration design/isolated-adapter validation baseline.
The subsequently implemented bounded remote-only route and its scope are in
[Remote queued guardian v1](HOST_JOB_GUARDIAN_REMOTE_V1.md). At this baseline no production service,
coordinator, worker protocol, no-launch gate or queue barrier is changed.
The code is in `desktop_pet/agents/guardian_worker.py`; fixed trusted canaries
are in `tools/qa_guardian_worker_fixture.py`. There is no default backend switch
or import-time launch.

## Exact interfaces inspected

The current `AgentService` launches a binary, unbuffered `Popen`, registers the
same process object with `WorkerLifecycle`, constructs `_ProcessTree`, waits
for the strict `nanobot-ai` 0.4.0 READY frame, and stages JSON without LF. A
synchronous `worker_dispatch_guard(handle)` rechecks authority before the final
one-byte LF. Checkpoint acknowledgement has a separate fresh guard.

`WorkerLifecycle` stores the exact registered process object in its opaque
`WorkerRunHandle`; its identity comparison must remain intact. It requires
callable `kill`/`wait`, and its tree requires `close`/`confirm_closed`.
`StagedRunInput.close()` must confirm its watchdog joined and no active write
remains. `AgentService._settle_worker` retains unresolved writers and workers;
neither an empty UI nor a lease timeout releases them.

A CPython `Popen(CREATE_SUSPENDED)` does not retain the primary-thread handle
needed by this protocol. This adapter instead uses owned `CreateProcessW`
with a retained suspended primary thread. It is explicitly **not** a Popen;
the compatibility surface is structural, while native identity remains exact.

## Minimal future service branch

1. Resolve the private guardian directory from the host's existing
   `QStandardPaths.AppConfigLocation`, under `host_job_guardian`. Reject overlap
   with the model workspace or any authorized read/write scope, including a
   grant that contains the private subtree. `PrivateGuardianLocation.prepare`
   requires explicit host-provided paths and performs no Qt configuration.
   Production must pass `model_scopes` as a trusted callback returning the
   latest complete workspace/grant set. Each `validate()` calls it freshly;
   a fixed tuple is only for an explicitly immutable scope or fixed QA. The
   isolated tests pass owned temporary AppConfig/workspace directories.
2. Pass the current `ExecutionAttempts.mark_launch_attempted(attempt)` as the
   mandatory launch-attempt hook. It runs **before native process creation**;
   changing from Popen does not create a no-launch exception. A thrown hook
   propagates unchanged. Phase callbacks are informational fixed codes and
   never authorize launch or dispatch.
3. `GuardianWorker.create` allocates owned stdio, creates a Job, calls the
   attempt hook, then creates the exact root suspended. At the earliest
   successful native creation boundary, its lifecycle callback registers this
   **same adapter object**, attaches `adapter.tree`, and publishes that handle
   to the existing coordinator. This occurs before later Job/manifest/ARM
   failures can lose ownership.
4. Assign and query the already-created Job, close parent copies of child
   endpoints, durably bind the manifest, and obtain authenticated ARMED. Only
   then call `adapter.resume()`. Guardian handles did not exist at root
   creation; its explicit inheritance list includes only root stdio.
5. Use the existing strict READY reader, `StagedRunInput`, cancellation event,
   and synchronous final-LF guard. Bind the exact writer to the adapter too.
   Inside the existing host authority lock, call
   `adapter.validate_private_scope()` on every final-LF and checkpoint-ACK
   guard. Keep the current fresh checkpoint/interaction authority checks. Do not
   turn user prompts into mode switches or bypass those guards.
6. Use `adapter.tree` directly. **Do not call `_ProcessTree(adapter)`**, which
   would create and assign a second Job. Its `close()` requests Stop;
   `confirm_closed()` verifies the original exact root/Job, all writers,
   guardian process and owned stdio before releasing anything.

The adapter exposes binary `stdin`, `stdout`, `stderr`, exact-handle `poll`,
`wait`, `kill`, `terminate`, `pid` and `_handle`. PID is only a lookup/diagnostic
label. `kill` has Popen-like exact-root semantics; `tree.close` requests the
whole Job's termination. Root exit alone is never Job closure. After verified
cleanup, a cached integer return code can still answer `wait`/`poll`, without
pretending a closed native handle remains live.

## Failed launch and cleanup ownership

An in-memory strong registry owns every adapter from construction onward.
Unknown writer join, native Job query, guardian wait, pipe operation or handle
cleanup preserves that exact object and refuses a new adapter. Explicit
`reconcile_cleanup` retries only cleanup observations; it never replays stdin,
model turns, file effects, application launches or queue operations.

If native creation fails before the lifecycle callback exists, the native
launcher retains any unconfirmed exact process handles. The invocation was
still a launch attempt. Missing lifecycle handles/manifests must not be turned
into `NoWorkerLaunchReceipt`. A future service integration must bind this
attempt-level failure to its existing unknown barrier and audit its recovery;
the isolated adapter does not invent a new production receipt for it.

FileIO conversion transfers ownership in steps: raw handle, CRT descriptor,
then FileIO object. Every intermediate owner is strongly retained before a
step can throw. A FileIO close can mark `closed=True` while raising, so its
error is **sticky**. A no-op second close and reconciliation cannot turn that
unknown into success. The adapter similarly audits legacy helper CloseHandle
calls; a failed close that lost wrapper authority stays unknown, without
retrying a possibly reused scalar handle.

The negative was reproduced before repair in
`artifacts/guardian-worker-close-baseline.json`. A bounded separate trusted
host fixture proves the sticky state retains exact root/Job ownership and
keeps the registry busy. It exits normally to let Windows release its already
stopped native resources; no test-only production registry reset is used.

## Restart scope

`consume_restarted_stopped(location, expected_manifest)` authenticates the
existing guardian record against the trusted expected manifest and performs
its single-use exclusive consumption. It yields stopped-only opaque evidence,
never completed, never a saved assistant reply, and never a replay request.
Missing or invalid receipts remain unknown, even when PIDs no longer exist.

During a still-running host, its retained exact root/Job can independently
prove live cleanup after a guardian failure. That in-memory result does not
manufacture a missing durable receipt for a later restart. These two proof
paths remain separate.

Before production integration, the queue's trusted immutable reference must
be mapped to manifest job/CID/generation/operation identity and any additional
required user-message/settings/origin bindings must be audited explicitly.
The current manifest is not a general authorization record. Consumption and
the production queue SQLite CAS also require a reviewed crash-safe handshake;
this prototype performs no queue state change. Saved-reply reconciliation
remains an independent requirement for completion.

Dynamic file grants must continue excluding the private AppConfig subtree
after construction, both when grants are approved and before their delivery.
The fresh callback and adapter method exist, and a late-grant negative blocks
Resume and a staged LF in the isolated fixture. Their production guard/grant
wiring remains a blocking integration task, not an already delivered policy.
Arbitrary same-user host compromise, every runtime/source
file pin, external Jobs that kill App and guardian together, reboot/power loss,
hard disk/history quotas and Qt-thread integration remain outside this
isolated validation. No global settings, OS policies or user credentials are
changed.
