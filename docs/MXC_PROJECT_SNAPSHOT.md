# Optional offline project snapshot and single-artifact publication

This host-only primitive does not itself register a tool or enable
`MxcPythonRunner`. The runner remains disabled by default. The separate
[host-gated worker adapter](PROJECT_EXECUTION_TOOL.md) registers the artifact
tool only for an explicitly enabled, validated private host configuration.
The primitive adds
bounded file computation and a small-project artifact path to the optional MXC
backend; it does not promise arbitrary shells, package installation, networking,
or commercial-agent equivalence. Tests execute fixed trusted code only.

## API and authority

`ProjectSnapshot.prepare(scope, inputs, output, goal_revision=...,
goal_is_current=..., authority_guard=...)` requires the actual host-created
`HostExecutionScope`, typed `ProjectInput`/`ProjectOutput`, a goal identity and
trusted freshness callbacks. A model dictionary cannot supply these objects.
The guard must be the same object used by real Stop/grant/goal mutations.

Inputs are explicit canonical relative paths and expected SHA-256 values, not
directory mounts. Names reject traversal, Windows devices/ADS, alternate
separators, case aliases, file/ancestor collisions and non-NFC names. Reparse
paths and hardlinked files are rejected even if another grant would authorize
the aliased destination. Source bytes come through `WorkspaceFiles`; the
snapshot retains only immutable bounded bytes and relative names. Default
limits are 16 files, 4 MiB total input and 4 MiB for the one output; configured
limits cannot exceed 16 MiB. No whole home/repository is copied.

`snapshot.execute(runner, code, input_json=...)` is one attempt. Its trusted
`stage_inputs` hook creates the nested layout in the runner's owned `input/files`.
MXC's existing policy grants that input root read-only access and only the owned
task root write access. Read-only behavior comes from the OS policy, not a DOS
attribute or a Python-language restriction. The subprocess gets no live-workspace
mount. Staging and output capture do not hold the authority guard. Fresh checks
before Resume include the original goal, scope and all source hashes.

Only the explicitly selected relative output is captured, after strict stdout,
normal exit, verified limits, exact tree-zero and native-resource closure. The
capture is a bounded, single-link regular file read into host memory. Other
scratch files are never implicitly published. The runner issues an opaque
`CaptureReceipt` only after runtime pins and scratch are also closed/removed,
Stop/scope remain current, and the global cleanup gate is clear. It binds one
exact invocation, scope, job, native result and typed capture object.
The receipt is absent from `to_json()` and rejects serialization. A copied
result dictionary, exit code, boolean or another session's capture cannot
authorize publication.

`snapshot.publish()` holds the shared host guard and `WorkspaceFiles` lock while
rechecking current scope/goal/Stop, every live input hash and the destination's
expected version. It then uses the existing atomic `WorkspaceFiles.write_bytes`
commit. This is one file, not a multi-file transaction. The resulting artifact
receipt retains `task_verified=false`; task success still requires independent
readback and the normal original-goal acceptance path.

## Uncertainty and replay

A freshness or target check rejected before calling `write_bytes` has
`effect_state="none"` and `publication_attempted=false`. Once the real write
method has been entered, an exception or invalid acknowledgement may follow an
actual committed namespace change. The host then raises the typed
`ProjectPublicationUnknown`, with `effect_state="unknown"` and
`publication_attempted=true`, without copying the underlying error text.
Cancellation/interrupts retain their original exception and receive those same
fixed host-origin attributes if a write had started. This does not infer state
from a model error string. The session discards its capture and refuses another
publish or execution; no automatic retry occurs.

A future registered adapter must enter its effect ledger before publication and
carry this typed uncertainty across new sessions. This module does not yet
register that cross-session ledger or claim that a new snapshot alone resolves
an uncertain old write. Reconciliation must use host-owned identity and real
readback, never silently replay an effect.

Native tree/pin/handle/scratch uncertainty yields no capture receipt and no
publication. A captured-file close failure keeps the exact file owner and
descriptor in the existing permanent sticky native cleanup registry. A later
`closed=True`, numeric-handle reuse or a second no-op close is not proof. Owned
failure fixtures exit normally in a separate process rather than clearing the
registry for testing.

## Evidence and limits

`tests/test_project_snapshot.py` covers host-only positive/negative/CAS behavior,
including a real atomic write followed by a lost acknowledgement. Fake native
results in those tests are explicitly not OS-isolation evidence.
`tests/test_project_snapshot_native.py` opt-in fixed actual MXC cases cover:

- Nested UTF-8 inputs, exact Decimal aggregation, denied writes to staged inputs,
  exact cleanup and a JSON artifact published atomically.
- A seeded Python project's Unicode fix, compilation and fixed cases inside MXC,
  then a single ZIP artifact; live source and README remain unchanged.
- Changed live input after staging preventing even the suspended version root
  from resuming.
- Stop after an output file exists, exact owned tree/resource closure, no capture
  receipt, no publication and no replay.

The tests do not call a cloud model or enable production execution. The Windows
backend's registry-read capability, whole trusted Python/System32 read access,
shared Windows kernel and soft rather than hard disk quota remain unchanged.
Workspace file checks are cooperative version checks; atomic rename/CAS is not
a claim of exclusion against hostile unrelated host software racing file paths.

Before production registration, bind a host-only captured execution-policy
identity through queue policy/prepared/context and fresh LF/ACK/Resume guards,
exclude its private configuration from all model scopes/grants, connect launch
attempts and exact cleanup barriers, and validate the actual SDK parent-to-MXC
ownership chain. Do not let model arguments select executables, policy, runtime
roots, network permissions or the enable switch.
