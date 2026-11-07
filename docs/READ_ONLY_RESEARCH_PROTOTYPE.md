# Read-only research children: standalone prototype

This is not registered in the production worker or tool catalog. It changes no
backend defaults and implements none of the existing catalog's general
unimplemented task types. No real/cloud provider was used in this phase.

The host may split one parent's research question across independently scoped
children. Each child uses the real pinned SDK AgentRunner, an independent
transcript/random child ID and an actual ToolRegistry containing **only**
`read_source`. It does not instantiate AgentLoop, Nanobot, ContextBuilder,
SessionManager, MemoryStore, plugins or MCP. No write/GUI/application/code/
grant/delegation/ask-user tool exists. A prompt is not the permission boundary.

## Host scope and reference protocol

`ResearchAuthority` receives the parent's existing WorkspaceFiles read
capability, canonical parent/CID, revision identity, cancellation event and a
trusted current-authorization callback. `open_scope` actually reads permitted
UTF-8 files to fix their SHA-256; `derive` accepts only a subset of the resulting
opaque source IDs. Source paths are never child tool arguments. The first
prototype supports at most eight selected files per scope, 256 KiB per file,
4,000 characters per excerpt and 256 issued excerpts per authority.

The child initially gets only source labels/IDs/digests. Its tool requests a
bounded excerpt over typed IPC; the host rechecks scope, current permission and
the actual source version before replying. No child can add a grant, choose an
arbitrary file or open a network source through this tool. A future webpage
source integration remains separate.

One known-secret policy handles labels, task text, entire source views,
conclusions and quotes. Whole sources are redacted **before pagination**, so
adjacent reads cannot reconstruct a known credential. Provider keys join this
policy before any excerpt exists. Citation quotes are checked against the exact
redacted excerpt actually issued, while source freshness still checks the raw
file SHA-256. The provider configuration travels only through private stdin and
is not included in the model transcript or public result.

The child's strict JSON contains only `conclusion` and `citations`, with each
citation limited to an issued `read_id` plus an exact quote. Receipts bind exact
authority/scope/child identity, therefore cannot move between parent, CID,
revision, sibling scopes or children. Forged IDs/quotes, malformed replies and
changed source versions return unverified. An accepted result is only
`reference_ready`, with `reference_only:true`, `claims_verified:false` and actual
source references. This verifies reference integrity, not the truth of the
model's whole conclusion or completion of the parent's goal. The parent must
freshly revalidate source versions and evaluate the actual facts before final
goal acceptance.

## Authority timing and integration limit

The default callback gives a check at callback time. The authority's private
RLock alone cannot make another component's grant/revision update atomic.
Optional `authority_guard` must be a trusted context-manager factory returning
the **same reentrant host guard** used by grant/revision/cancellation commits.
It encloses callbacks, source checks, final IPC LF and merge checks. No model
request or full input staging is awaited inside it. Callers that mutate external
authority outside that guard still have only callback-time protection.

An actual-process race test pauses immediately before the first LF: with only a
boolean callback, an external revoke can occur before the LF; with the shared
host guard, commit and revoke have a single order. Both variants reject further
source reads/adoption once revoked. Production controller/grant wiring is
**not implemented** and must use that common guard; this prototype is not an
atomic production permission integration.

## Native ownership, concurrency and cancellation

`ResearchGroup.run` runs one non-replayable batch (up to eight questions), with
at most two child workers concurrently. A third child waits until an actual
previous worker has closed. This is a resource cap, not a parent Agent step
limit. `arun` supports asynchronous parent cancellation. The optional prototype
deadline is explicit; the default has no whole-task deadline and changes no
production unlimited-iteration setting.

Each child has an exact returned Popen, owned process tree and opaque
WorkerLifecycle identity. It receives no complete command before ready plus a
freshly guarded LF. Every payload uses bounded staged input. Parent cancellation
requests only those owned processes; closure requires exact reaping/tree
confirmation, joined input writers and host-owned scratch cleanup. `close` is a
Stop request plus retry of already finished cleanup, not itself a closure
receipt; use finished run/`cleanup_confirmed()` evidence. SDK scratch is owned by
the host, and an actual live-worker test verifies it contains no files while a
model request is still pending. No raw reasoning/history/provider error file is
created and later deletion is not used as the sole privacy argument.

Host source snapshots are synchronous bounded reads. A slow or uncooperative
filesystem may delay the background broker/async join; this is another reason
the prototype is not directly wired into the UI. Closing the authority marks it
inactive without waiting for that read, and the guard's final check prevents a
late excerpt from escaping. The native owner stays retained until the actual
execution path and cleanup finish; a quick Stop response is never closure proof.

Unknown native cleanup keeps a strong module owner reference and blocks new
prototype delegation. Only exact cleanup can release it. A Popen call that
raises remains launch-unknown: there is no invented no-launch receipt or PID
scan recovery. A returned exact Popen whose initial registration failed may be
registered solely for later cleanup if no command was ever dispatched; that
does not re-run the child. Cross-restart owner recovery is not implemented.

SDK provider failures/length/empty output abort instead of triggering automatic
retry or continuation. The pinned provider's HTTP client also uses zero retries.
Cancellation cannot prove a remote model request was undone; dispatched but
unacknowledged requests remain `model_request_outcome:unknown`. No group is
automatically replayed. Stdout is fixed IPC/typed reference results; stderr is
discarded, logging sinks are removed, and private raw provider errors are never
returned.

## Local evidence

```powershell
.venv/Scripts/python.exe -m unittest tests.test_research_scope tests.test_research_sdk -v
```

The suite uses owned temporary UTF-8 files, actual isolated SDK workers and a
synthetic localhost provider. It checks real overlap via a two-request barrier,
third-child queuing, hard tool definitions, unknown write-tool refusal without
archive failure, cross-child forged citations, current-source invalidation,
redaction at each boundary, one failed/cancelled model request without resend,
asynchronous parent Stop, actual invalid-executable launch failure, and real
owned-tree cleanup uncertainty blocking new groups. These are architecture and
safety fixtures, not real-model research success rates or GPT-level capability
evidence.
