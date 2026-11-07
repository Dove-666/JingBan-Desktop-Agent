# Isolated owned Job guardian validation

This records the **historical isolated prototype** validation, not a claim about
later production sources. Current bounded remote integration is described in
[Remote queued guardian v1](HOST_JOB_GUARDIAN_REMOTE_V1.md). That prototype did
not provide production startup recovery. It
does not change `WorkerLifecycle`, `AgentService`, `JobQueue`, worker dispatch,
or the coordinator. Existing `unknown` production tasks keep their barriers.
The original proposal's remaining integration and crash-validation assumptions
are not treated as established by these tests.

## Implemented boundary

`guardian_native.py` creates an unnamed kill-on-close/no-breakaway Job and
retains the exact suspended process and initial-thread handles. It verifies
Job limits and membership. `GuardianSession.arm()` checks the manifest's exact
root creation time, transfers limited duplicated handles with an explicit
`STARTUPINFOEX` whitelist, and validates a nonce/digest/HMAC-bound ARMED reply.
The trusted caller resumes the worker only after ARMED; the prototype is not
hooked into the production dispatch guard.

The helper receives only bootstrap-read, acknowledgement-write, Job query and
terminate, exact root query/synchronize, exact App synchronize, and Stop-event
synchronize handles. Its standard handles refer to NUL. Worker stdin/stdout
remain separate. Workers do not inherit the guardian's handles, private key,
private directory path, or bootstrap/acknowledgement channels.

After the exact App handle or Stop event signals, the helper calls
`TerminateJobObject`. It writes a closure record only after successful Job
accounting reports zero active processes **and** the exact root handle is
signaled. Root exit, successful termination calls, missing PIDs, an exited
guardian, or a missing Job name are insufficient. A failure emits no successful
receipt; helper exit diagnostics are fixed integer codes with no raw errors.

Private records use a newly created owner-only protected DACL, non-reparse
ordinary single-link files, bounded canonical JSON, a current-user DPAPI key
with UI disabled, and an HMAC over the complete closure record. The manifest
hash binds canonical nonzero UUID job/CID/generation/operation IDs, a random
App launch epoch, installation identity, run nonce, and root creation time.
Generation follows the current queue's UUID semantics, not a counter.

Every record is at most 4 KiB. The helper flushes a newly and exclusively
created temporary receipt and publishes it through a write-through rename that
does not overwrite an existing final receipt. Truncated, oversized, replaced,
unauthenticated, wrong-scope or wrong-generation data cannot produce evidence.
An exclusive consumed-record creation provides a standalone cross-process CAS;
it has **no transaction with the production queue database**.

The new `GuardianClosureEvidence` is opaque, immutable, non-serializable and
deliberately unrelated to `WorkerClosureReceipt`. It means worker-stopped only;
it neither proves task completion nor authorizes effect replay. Future
integration must authenticate persisted records against the exact trusted job
reference and perform its own atomic queue reconciliation.

## Evidence and negative cases

Run the fixed validation with:

```powershell
& .venv/Scripts/python.exe tools/validate_job_guardian.py
```

The report is `artifacts/isolated-job-guardian-validation.json`. It contains
source hashes, fixed test IDs, counts, booleans and measurements, not raw process
output, HMAC keys, bootstrap data, prompts or document paths.

The owned fixture starts only a known synthetic App, worker and waiting
grandchild. The independent test opens root, guardian and grandchild handles
while they are known live, verifies creation times, and retains those exact
handles for final signal checks. PIDs are only a fixture rendezvous, never
closure evidence. Worker and grandchild have bounded self-expiry as an extra
test cleanup measure.

The tests cover abrupt App death after start, ARMED-before-Resume death,
before-manifest/ARM death, root exit while its grandchild remains live, explicit
Stop, guardian death followed by App death, insufficient Job QUERY rights,
QUERY-only Job rights that cannot terminate, failed atomic publication, wrong
identity/HMAC, partial receipts, hardlink rejection and single-use consumption.
An intentionally inheritable ambient event is excluded by the worker's handle
whitelist. Synthetic worker output is absent from guardian receipts and the
fixture's App-facing record.

The full/no-reader private-pipe test uses real Windows handles: a fixed 8 MiB
write returns partial/failure within 0.5 seconds, with no writer thread. Pipe
writes use queried `PIPE_NOWAIT` and fail on a zero/partial write; they are never
retried or concatenated. This is a one-write handshake, not asynchronous I/O.
The setting is documented for anonymous pipes by
[SetNamedPipeHandleState](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-setnamedpipehandlestate),
and the immediate partial-write behavior is described in
[pipe wait modes](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-type-read-and-wait-modes).

## Limits and remaining work

- This host observed the guardian inside an external Job. The fixture proves
  survival of its synthetic App's exit in that environment, not survival if an
  outer Job kills App and guardian together. No OS policy is changed.
- Current working-set samples describe this trusted runtime and short fixed
  fixture only. They are not a production memory guarantee or a hard limit.
- Current-user DPAPI and owner-only ACLs do not defend against arbitrary
  same-user process memory access or a compromised host application. The
  private location must be excluded from every delegated model file scope by
  the future integrating host.
- NTFS child rename requires directory delete sharing here. The store checks
  the held directory's file identity and ACL around IO. This is not a general
  filesystem isolation boundary against arbitrary same-user directory swaps.
- The helper/runtime are trusted explicit local dependencies; this prototype
  does not pin every runtime/source file or authenticate a caller-supplied
  installation hash against an installer identity.
- Native failed-launch cleanup retains unconfirmed exact helper handles,
  refuses another arm while cleanup is pending, and requires explicit
  reconciliation. Kill-called or Close-called is never treated as proof.
- No power-loss/reboot test, all-handoff crash matrix, UI-thread integration,
  protected installation, hard history/disk quota, or production database CAS
  has been validated. Unconfirmed records are retained, not garbage-collected
  into a false stopped state.

The architecture uses documented
[exact duplicated handles](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle),
[explicit inheritance lists](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
and [current-user DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).
The complete protocol is an engineering design, not a Microsoft guarantee of
universal crash recovery.
