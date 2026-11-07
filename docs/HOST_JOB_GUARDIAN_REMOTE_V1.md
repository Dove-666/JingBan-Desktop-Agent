# Remote queued guardian v1

Authenticated remote queued Agent tasks now use the native suspended-worker
launch path in the desktop service. Local direct desktop requests keep the
existing Popen path and do **not** gain durable guardian recovery in this
version. No Android/RPC protocol change or model keyword enables this backend.

The integration was validated in an owned Qt host using the actual
RemoteController, ChatController, coordinator, service and isolated SDK with a
synthetic localhost model. It did not operate the user's running desktop App,
phone transport, credentials, documents or desktop. This is control-chain
evidence, not a real-model capability success rate.

## Binding and launch

Only the coordinator's opaque `HostGuardianLaunch` can select the remote
backend. It is tied to the active queued request, immutable HostJobReference,
execution attempt, fresh lease and host authorization. Imported chat/model/RPC
dictionaries cannot construct it. A selected remote backend never falls back
to an unguarded Popen after a validation or launch failure.

Manifest schema v2 binds the durable queue job ID, CID, UUID generation, user
message ID, host idempotency/operation UUID, settings scope and origin peer,
plus run nonce, App launch epoch, installation identity and exact root creation
time. Settings scope already includes the host configuration/credential and
authorization-epoch fingerprint; no API key or raw user payload is recorded.
The original RPC request UUID is distinct from the derived host operation UUID.

Queue schema v3 adds `guardian_runs`, containing only job ID, run nonce,
manifest hash and schema. The full immutable reference must match the job row.
A valid running lease can bind one run; the same manifest is idempotent and a
different nonce/root/epoch cannot overwrite it. Upgrades do not fabricate
bindings or authenticated origins for old rows.

The actual order is native suspended creation, same-object lifecycle
registration, queried Job assignment, durable manifest, SQLite run-binding
COMMIT, authenticated ARMED, fresh host guard, Resume, strict SDK READY,
JSON-without-LF stage, fresh final-LF guard, and one-byte commit. The binding
hook is an authority boundary whose errors propagate; the informational phase
callback is not used to acknowledge a database commit. No second Job is placed
around the adapter.

Worker environment uses the service's existing filtered host snapshot through
a strict explicit Unicode environment block. It rejects malformed names,
embedded NULs, lone surrogates and case-insensitive duplicates. It never falls
back to full App environment inheritance. The guardian helper still receives
only its separate minimal SystemRoot environment. Production worker stderr is
native NUL, matching the old service; it is not retained or logged. A real
8 MiB stderr burst before READY passes, with an undrained PIPE negative control.
Production Job policy preserves the prior kill-on-close-only semantics;
prototype/renderer defaults retain their separate eight-process policy.

## Recovery and resource closure

The private directory is `AppConfig/host_job_guardian`, outside every current
model workspace and authorized file scope. A trusted host callback supplies
the latest scopes. Resume, each executable LF/checkpoint ACK, and grant
approval/delivery reject overlap with the private subtree. Both launch paths
honor unresolved queued-worker and native-resource barriers.

On restart the host reads the queue's exact bound nonce/hash, opens only that
private run, validates schema-v2 full reference, DPAPI/HMAC and stopped-only
closure, then calls `JobQueue.reconcile_worker_stopped` with the opaque binding.
The binding is rechecked in the same SQLite transaction as the stopped update.
Only after COMMIT returns does optional consumed-marker housekeeping occur.
An ambiguous commit leaves the receipt unconsumed. A committed stopped row is
not rolled back when later metadata/marker cleanup fails.

Recovery never creates a reply, marks a job completed, resumes an old worker,
or replays effects. An old job remains unknown/interrupted with its worker
closed; a different newly authorized job can run. Missing binding, receipt,
manifest or pre-ARM handoff proof remains unknown. Root/guardian PID absence
is never evidence. A small bounded startup/admission reconciliation poll can
observe a late receipt; it does not retry a model or task.

Unknown writer, pipe, Job, guardian or metadata closure retains its exact host
owners and blocks new Agent launches. FileIO/legacy wrapper close errors are
sticky: `closed=True`, cleared wrapper fields, a later no-op close, or a failed
constructor returning no object cannot erase uncertainty. The failed prepare
and recovery metadata owners are retained too. These barriers apply even when
the worker's separate stopped-only database proof was already committed.

## Verification records

- `artifacts/guardian-production-v1-actual-crash.json`: actual owned App killed
  after writing/reading A while waiting for another model response. Exact old
  root and guardian handles signaled, unrelated control remained alive. A
  stayed unknown/stopped without a reply; after restart, different B completed
  with unchanged A version. Seven planner requests, one synthetic judge
  request, no replay. Declared copied source and QA extras stayed unchanged.
- `artifacts/guardian-production-independent-final-source.json`: independent
  state/fake-IO authority, lease, scope and identity negatives. This does not
  claim native shutdown or real-model performance.
- `artifacts/guardian-recovery-close-before-fix.log` and
  `artifacts/guardian-private-prepare-close-negative-evidence.log`: preserved
  failing resource-owner evidence; the corresponding independent cases now
  pass.

The actual crash canary uses a source snapshot under `artifacts`, reusing the
existing SDK through a junction. It copies no settings/user data/dependency
tree and attests neither installed binaries nor subsequent producer changes.
Historical isolated-prototype reports remain unchanged. Later Office runtime
integration has its own source scope and verification, and is not implied by
this guardian report.

Same-user host compromise, complete runtime/source pinning, outer Jobs that
kill host and guardian together, reboot/power loss, the complete handoff crash
matrix, and bounded history/disk reclamation remain outside this version.
Missing proof keeps the global barrier. Local direct-task durable admission
and recovery require a separate future queue enrollment design.
