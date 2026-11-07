# Host-gated offline project artifact tool

`compute_project_artifact` is explicitly registered by the scoped worker only
when a valid host execution snapshot is enabled, the host supplies a matching
canonical task/CID authority, Windows is available and tools are enabled.
The real machine's enable file is still absent/off during this implementation
and QA. Tests do not create that file or call a cloud model.

**2026-11-04: bootstrap remains blocked; the real machine is still off.** The
original fixed desktop canary could read the QA host's own hidden-window title;
its report remains `artifacts/mxc-owned-desktop-fixed-canary.json`. The new
trusted broker creates the actual Python worker with verified Win32k ON before
Resume, blocking that exact caption path. However a fixed mitigated helper can
open its live, unmitigated parent broker with CREATE_PROCESS, VM_WRITE|VM_OPERATION,
DUP_HANDLE, TERMINATE and WRITE_DAC access. All acquired handles were closed;
no memory IO, injection, parent spoofing, user-window or credential access was
attempted. This concrete ancestor-access failure is preserved in
`artifacts/mxc-production-broker-pinned-access-probe.json` and still prevents
enabling generated code. Read-only staged inputs, current-hash publication and
passing fixed compatibility checks do not close this boundary. No actual
configured-model Python repair was run. The official runtime was copied to a
stable private path after the app removed its former pinned directory; see
`artifacts/mxc-runtime-pinned-bundle-20261104.json` and
[MXC backend details](MXC_EXECUTION_BACKEND.md). Native isolation must be repaired
and independently checked before model-generated Python is enabled.

The host snapshot comes from `desktop_pet.agents.execution_runtime`, under its
private `%LOCALAPPDATA%/AURA Desktop Pet/project_execution` directory. Model
arguments cannot supply the enable switch, executable, runtime, network policy,
authority, budget or configuration identity. The worker independently compares
the fixed policy/pins, captured identity and fresh opaque host snapshot. Scope
checks exclude the private configuration, trusted runtime/source and each owned
scratch parent, including newly requested grants.
An initial isolation-scope conflict rejects the compute route before creating
native authority; it does not disable unrelated ordinary file/chat tools. Only
host-confirmed failures with no publication are exploratory routes that may be
replaced by another authorized, independently verified method. Configuration or
grant revocation and every unknown effect keep their existing hard barriers.

The private worker frame carries `config.project_execution`,
`config.project_execution_identity` and `execution_authority` with exactly:

```json
{"version":1,"conversation_id":"canonical UUID","generation":"canonical UUID","request_id":"canonical UUID","settings_scope":"64 lowercase SHA-256 characters"}
```

CID/request are checked against the actual outer frame. Grant revision is
computed from the real `WorkspaceFiles` grants, settings scope and execution
identity; goal revision comes from the actual goal timeline. Native action
identity comes from the active SDK tool call. These are not parsed from model
messages. Before each native creation the worker mints and marks a separate
host execution attempt; it does not reuse the service's once-only outer-worker
attempt as proof of a nested launch or non-launch.

The model tool supplies only bounded Python, JSON input, explicit relative
input paths with current SHA-256 values, and one relative output/target with an
overwrite hash when needed. The nested snapshot is read-only in MXC, scratch
alone is writable, and the live workspace is not mounted. Code prints one strict
JSON value but stdout has no file-publication authority. The runtime returns
only standard metadata from a real typed host publication.

All slow snapshot, native execution, capture and publication work runs off the
async control loop. Stop, steering, grant/goal changes and stdin EOF revoke the
same host authority used by the outer native/host dispatcher Resume and final
publication. The nested broker's Resume does not share that host's cross-process
authority lock; it is covered by outer owned-Job termination on Stop/EOF and the
fresh final-publication guard, not an atomic host check around nested Resume.
Trusted reader
EOF revokes connection liveness before queueing its control event, so a delayed
event-loop handler cannot leave an orphan authorized to publish. Cancellation
sets the native stop event and retains an exact owned `threading.Thread` until
its real join confirms drain. The async task is only a notification/waiter;
its cancellation or `done()` flag is not thread-closure evidence. Native cleanup
reconciliation uses the same owned-thread rule instead of the global executor.
Normal process exit never substitutes for a private resource closure.

Native/pin/scratch uncertainty retains the exact runtime owners and blocks the
next tool in the same SDK batch, the next model iteration, judge, finish and
final success. A publication entered without reliable acknowledgement is
`PROJECT_PUBLICATION_UNCONFIRMED`, including failure while projecting a committed
receipt. It is not retried or converted into a successful task. The outer worker
chooses `needs_user` only after cleanup has been attempted/drained, so a previous
completion draft cannot escape a later uncertainty gate.

`ProjectArtifactResult` is host-minted and temporarily typed. TaskHarness and
GoalEvidence accept its write witness only after exact type/issuer checks.
Plain strings, dictionaries and model stdout cannot create that witness. Once
checked, the worker projects ordinary text into the SDK, ledger and replay
archive; opaque authority never enters SDK deepcopy or session serialization.
Large code previews can be clipped while preserving the small exact host file
write witness. The trace remains `compute_project_artifact`, not a fabricated
`write_file` event. The artifact still has `task_verified=false`; independent
file readback and the original-goal judge remain necessary.

Generic ZIP readback now uses `verify_artifact(kind="zip")`, also selected by
`auto` for a `.zip` suffix. Its scope is `zip_structure_crc_only`, with
`task_fully_verified=false`. It checks the current file version, bounded
single-disk ZIP/ZIP64 metadata, safe distinct Windows member paths and every
stored/deflated member's CRC. Stored bytes and raw DEFLATE are read independently
of `ZipExtFile`'s declared-size truncation: the compressed region must end at a
real decoder EOF, without unread input or trailing compressed-region bytes.
Actual output size and CRC must match, with each decoder allocation capped by
remaining declared/global output plus one rejection byte. Local headers,
ZIP64 sizes and streaming data descriptors must agree with the observed
metadata. It never extracts, imports or executes a member.
The compressed file and total expanded bytes are each capped at 16 MiB, with
at most 2,000 entries; the actual central directory is bounded before Python
allocates its filelist, so forged EOCD entry counts cannot bypass that limit.
Only the first 64 members' names, lengths and SHA-256 values are returned, with
an explicit omitted count. An empty ZIP is a valid empty container. These facts
do not prove that code works, that the package contains the requested files or
that results match their sources; task-specific independent checks remain
necessary. Requesting another `kind` cannot bypass the harness's minimum
automatic check of a ZIP artifact.

Decoder semantics follow the official [Python zlib documentation](https://docs.python.org/3/library/zlib.html#zlib.Decompress.eof),
including `eof`, `unconsumed_tail`, `unused_data` and bounded `max_length`.

Fixed QA covers the actual SDK-to-native computation/readback/goal path and a
real stdin-EOF stop. Controlled fake-native failures separately cover same-batch
cleanup barriers, real host commit followed by ACK loss, and post-commit receipt
projection failure. These do not enable production or establish arbitrary
shell/build support. MXC's registry-read residual, trusted System32/Python reads,
shared Windows kernel and soft disk quota remain documented limitations.
