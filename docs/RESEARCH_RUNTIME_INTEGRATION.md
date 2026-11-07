# Read-only research runtime and parent registration

`agent_backend/nanobot/research_runtime.py` is a host-injected helper. The helper
phase was tested before registration and its original source-fenced evidence
remains unchanged. The next phase registers it in the actual parent worker and
catalog when read tools, Windows native ownership, a UUID host task ID and a
canonical conversation ID are available. It adds no GUI control or step limit.
The standalone child and its ownership boundary remain described in
`READ_ONLY_RESEARCH_PROTOTYPE.md`. The original helper and registration tests
used local controlled providers; the separate configured-model check below is
not retroactively included in those reports.

## Configured-model task check, 2026-11-03

A fresh source copy ran one explicitly requested research task with the existing
configured `deepseek-flash` model. Two actual isolated children independently
read generated UTF-8 files; both returned source-bound quotations. The parent
read both originals again and wrote a JSON summary. Independent checks verified
the two values, their sum, distinct source identifiers, quote contents/hashes,
child closure and absence of image data from parent history. This task completed
in 23.266 seconds; all 193 declared runtime, runner and renderer-configuration
files in that copy remained unchanged, and existing chat settings were unchanged.

The earlier failed task and later 300-second timeout are preserved separately.
Two concrete child defects were independently reproduced and fixed:

- Isolated Python ignores `PYTHONIOENCODING`; IPC now explicitly writes UTF-8
  bytes, preserving Chinese and emoji independently of the console locale.
- The installed provider SDK clamps an output budget of zero to one token. 鲸伴
  uses zero to request the model default, so the child now removes the output
  budget field on the actual wire, matching the parent. The narrow override
  adds no history manager, provider fallback or automatic retry. Truncated and
  empty responses remain unverified.

The corrected scope/SDK/Stop/Unicode/wire regression run passed 23 tests without
skips, with eight declared source hashes unchanged. One earlier test-selection
command contained a nonexistent module; that log is retained as a failed command,
not reported as a passing suite. Reports are local artifacts:

- `artifacts/research-output-contract-final.json`
- `artifacts/research-output-budget-before-fix.log`
- `artifacts/research-unicode-ipc-before-fix.log`
- `artifacts/agent-parity-20261102/registered-real-research-output-default.json`
- `artifacts/agent-parity-20261102/frozen-research-output-default-20261103/post-run-source-proof.json`

This is one controlled, explicitly delegated task, not a stability rate,
autonomous routing benchmark, browser-research evaluation or GPT parity claim.

## Implemented helper

The only model-facing method is:

```python
await runtime.delegate_research([
    {"task": "Summarize the first source with exact citations.", "paths": ["a.txt"]},
    {"task": "Check the second source independently.", "paths": ["b.txt"]},
])
```

Each row has exactly `task` and `paths`. The list has one to eight assignments;
one batch selects at most eight distinct files in total. Sources must already
be readable through the exact parent `WorkspaceFiles` object, be UTF-8, and fit
the existing 256 KiB per-file budget. The helper never adds a grant. The model
cannot supply parent/CID/revision/call identity, credentials, executable, source
hash, cancellation, guard or child scope. Each actual child registry contains
only `read_source`, with random child identity and independent context; there is
no write, application, GUI, code, delegation, ask-user, plugin or MCP tool.

Parent run UUID, conversation UUID, current revision getter, current authority
getter, parent cancellation event, call-identity getter, shared host guard and
the three-field provider configuration are supplied by trusted host code. The
default Python is the current SDK interpreter. Importing and running the helper
inside the actual isolated SDK environment does not require or import Qt.

One runtime serializes its batches, while one batch runs at most two actual
children concurrently. This is a resource bound, not an Agent step limit. The
default has no whole-task deadline. An optional explicit QA deadline does not
change production unlimited-iteration settings. The native ownership adapter
currently has verified Windows behavior; do not advertise general platform
availability without an equivalent exact process-tree closure proof.

## References, retries and closure

The result has `reference_only:true`, `claims_verified:false`, and
`parent_goal_completed:false`, including when every child is `reference_ready`.
Verified citations bind the exact issued redacted excerpt, child, source ID and
raw source SHA-256. This verifies citation integrity and file freshness, not the
truth of the whole conclusion. All labels, source views, task text, conclusion
and quotes use the same known-secret policy; provider keys travel over private
stdin and join that policy before any excerpt is issued.

Before a result is returned, each accepted child reference is checked again
against its original issued read and the current source hash. This also happens
for valid references inside a mixed failed batch. A duplicate host call reuses
only its original observation: the original source hashes/read IDs remain, the
result marks `duplicate` and `original_observations_reused`, and no new child or
model request is sent. Modified arguments under the same host call are rejected.
Sixteen payloads are retained; per-parent thin identity tombstones prevent an
evicted call from being replayed. A new explicit host call may run after exact
cleanup, but an old failed/cancelled call never silently restarts.

Call `invalidate()` promptly when the host receives a correction/revocation.
The callback checks revision, parent cancellation, authority and an invalidation
epoch before subsequent payload/read/adoption. A watcher also stops active
children on these changes. Task cancellation, including repeated cancellation,
awaits actual execution and cleanup before preserving `CancelledError`.
`aclose()` likewise joins active source preparation, reference checks and child
cleanup; repeated Stop does not turn a pending cleanup into a receipt.

Unconfirmed cleanup is strongly retained by exact native group identity and
blocks new delegation and `assert_parent_ready()`. `reconcile_children()` retries
only cleanup on those exact retained objects. It sends no task/model/read again
and clears a barrier only when their actual cleanup proof becomes true. An
attempted Popen that raised remains unknown, rather than being reconstructed
from a PID or an absent process list. Cancellation cannot prove that a remote
HTTP model request was undone; its request outcome can remain unknown even when
the native child is actually closed.

The optional `authority_guard` must return the same reentrant host guard used by
permission/revision/cancel mutations. A private runtime lock or unrelated host
lock cannot make those mutations atomic with final IPC LF/read/merge. Omitting
it provides callback-time checks only. No network/model request or full input
staging is awaited under that guard. Bounded filesystem checks still use it;
uncooperative filesystem IO can delay the async join and is never declared
closed merely because cancellation was requested.

## Parent worker wiring

The following describes the implemented worker integration. The generic names
in the example stand for the actual worker-owned timeline, cancellation event,
ScopedTool action identity and common reentrant authority guard.

1. Construct one runtime per actual parent request after validating the host
   request UUID/CID and existing file capability. Service task IDs use UUID hex;
   normalize that exact host ID once with `str(UUID(request_id))`. A legacy
   non-UUID QA task ID or absent CID does not acquire this tool. Use an authoritative goal
   revision identifier. A pending correction must make authorization false
   immediately, before resolving its new goal contract. Do not accept a revision
   or user confirmation supplied by planner/tool/web text.

```python
research = ResearchRuntime(
    files=files,
    parent_run_id=host_parent_run_uuid,
    conversation_id=host_conversation_uuid,
    current_goal_revision=host_current_goal_revision,
    authorization_current=host_research_authorization_current,
    parent_cancellation=host_parent_cancel_event,
    call_identity=lambda: _current_action.get(),  # trusted ScopedTool identity
    authority_guard=host_shared_authority_guard,
    config={name: config[name] for name in ("base_url", "model", "api_key")},
)
```

2. Only when read tools and the verified native owner are available, register a
   read-only, no-effect tool callback through the existing scoped wrapper:

```python
async def delegate_research(assignments):
    return json.dumps(await research.delegate_research(assignments), ensure_ascii=False)
registry.register(ScopedTool("delegate_research", delegate_research))
```

   The catalog entry is `permission="tools"`, `read_only=True`,
   `effect="none"`, `windows_only=True`, `requires_gui=False`, with this strict
   input shape (the helper independently validates it too):

```json
{
  "type": "object",
  "properties": {
    "assignments": {
      "type": "array", "minItems": 1, "maxItems": 8,
      "items": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "task": {"type": "string", "minLength": 1, "maxLength": 4000},
          "paths": {"type": "array", "minItems": 1, "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 4096}}
        },
        "required": ["task", "paths"]
      }
    }
  },
  "required": ["assignments"], "additionalProperties": false
}
```

3. Authenticated steering/revision/cancel handling calls `invalidate()` using
   the shared authority ordering. A guarded GoalTimeline subclass locks only
   synchronous append/apply/unresolved/snapshot methods; resolver HTTP waits do
   not hold that guard. Real host grant installation uses the same lock. Before
   the next parent iteration, planner call, finish tool and final judge, the
   worker calls `assert_parent_ready()`. The outer result wrapper **awaits**
   `aclose()` before choosing its returned result. Cleanup failure overrides
   candidate delivery to needs-user with fixed `RESEARCH_CLEANUP_UNCONFIRMED`
   and `acceptance.passed=false`, preserving eligible partial artifacts. The
   normal final path also closes children before publishing the final assistant
   text. Cancellation remains cancellation after confirmed closure; unknown
   cleanup returns the scoped blocker instead. No tool/model call is replayed.

4. Do not insert synthetic `read_file` observations into GoalEvidence on behalf
   of the child. A child summary is reference data, not parent goal evidence or
   permission. The parent may read the cited source directly for a required
   deterministic read check; any future trusted child-observation evidence
   adapter must separately bind actual bytes/hash/revision and be reviewed.
   Keep the independent parent judge, unknown-effect ledger and GUI parent
   verification unchanged. Do not count child references as a completed task.

5. Safe fixed tool labels/history vocabulary are registered, with
   no provider exception body, raw reasoning, configuration or credential in
   history. Existing child result text is untrusted reference material. The
   runtime is not reconstructed for the same action to bypass its replay
   ledger, or registered recursively inside `research_worker.py`.

## Evidence and scope

`tests/test_research_runtime.py` uses actual pinned SDK child processes and owned
localhost HTTP fixtures. It covers source/read/citation return, separate child
scopes, isolated SDK parent without Qt, cached-source invalidation, mixed-child
freshness, revision changes before/after dispatch, cancellation and repeated
close, actual unknown process-tree proof/reconciliation, argument/identity
rejection, permission denial and eviction without replay. The original
prototype suites additionally prove concurrent requests, the two-child cap,
forged references, privacy/redaction, no raw SDK scratch history and the hard
tool registry. These are control-chain tests, not cloud-model task accuracy,
GPT parity. `tests/test_research_parent_worker.py` separately runs an actual SDK
parent through delegate→two actual read-only SDK children→parent source reads→
finish and independent goal checking. It also covers real parent Stop, host goal
correction, no recursive/write child tools, missing parent artifacts, unavailable
legacy/CID scopes, and unknown exact child cleanup blocking the next planner.
It also runs the real host AgentService→SDK parent→two SDK children under nested
native ownership and validates the exact host parent closure receipt, plus a
real file-access host reply whose grant installation and research scope use the
identical worker authority guard.

The same parent stage registers `inspect_document_page` only when an explicit
host Office runtime exists. That read-only callback uses the plain unobserved
judge provider and checks the current parent revision/cancel/steering before and
after its single request. `tests/test_document_review_parent_worker.py` exercises
actual LibreOffice render→private cached PNG→actual SDK vision request→parent
PDF readback and finish. The receiver checks one exact PNG/hash with no parent
history/tools, and native transcripts never contain its data URL. The response
remains `reference_only` with whole-document/visual verification false. These
localhost responses are scripted wire evidence, not model visual quality.

A later visual-review phase enables explicit host derivation for large pages.
Small PNGs stay exact; larger pages use a labeled bounded JPEG with separate
original/sent hashes and actual transport dimensions. The callback remains
reference-only and is exclusive per worker. See `DOCUMENT_PAGE_VISUAL_REVIEW.md`
for conversion ownership, cancellation, current-source fences and image-echo
protection. The earlier parent source-fenced report remains its original stage
evidence rather than being relabeled to cover this later route.
