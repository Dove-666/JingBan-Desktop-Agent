# Optional native Windows execution backend

`mxc_execution.MxcPythonRunner` is standalone and **disabled by default**. It is
not directly registered as a model tool or exposed in UI. The separate
[project tool adapter](PROJECT_EXECUTION_TOOL.md) can select this fixed backend
only through a validated private host opt-in; the machine remains off during QA.
It does not invoke an OpenAI model, authenticate, use Docker/WSL, provision
administrator accounts, change firewall rules, or edit the user's Codex config.
Passing the fixed QA tests does not enable it or verify a user's task.

**2026-11-04 status: model-generated Python remains blocked/off.** The new
trusted first-stage broker starts the pinned Python worker suspended with
creation-time Win32k ON, then verifies the exact image, inherited Job membership
and effective policy before Resume. The broker never reads or evaluates the
model request. This blocks the earlier exact owned-window caption path, but
does **not** establish a complete isolation boundary: a fixed mitigated helper
could open its still-running, unmitigated parent broker with CREATE_PROCESS,
VM_WRITE|VM_OPERATION, DUP_HANDLE, TERMINATE and WRITE_DAC access. Each handle
was immediately closed; no memory IO, injection, parent spoofing, real user
window or credential access was attempted. The same-token ancestor issue must
be repaired and independently checked before enabling generated code.

`artifacts/mxc-production-broker-pinned-access-probe.json` preserves that failure.
`artifacts/mxc-production-broker-fixed-compatibility-initial.json` separately
records `fixed_compatibility_passed=true`, `security_gate_passed=false` and
`passed=false`. Fixed arithmetic/ZIP/ordinary subprocesses, Win32k inheritance
(including a child requesting ALWAYS_OFF), failed self-clear, suspended-worker
query rejection without a payload marker, Stop of exact owned worker/child
handles, output limits and full cleanup all passed. These fixed trusted cases
do not authorize model-generated Python or prove arbitrary program isolation.

## Host contract

The host supplies the existing `HostExecutionScope`, containing conversation,
generation/request identities, the approved `WorkspaceFiles`, grant revision,
and a current-authorization callback. Dictionaries from model arguments cannot
mint this capability. The workspace's physical identity is checked again during
execution and before returning data. Revocation stops the owned tree.

`execute(code, input_json, scope=..., authorized_inputs=..., cancel_event=...)`
returns `MxcExecutionResult`; `to_json()` produces the tool-facing strict JSON
envelope. `execute_async` handles cancellation during launch and repeated Stop
while waiting for cleanup. Cancellation never retries or restarts a request.

The optional host-only `authority_guard` is a callable returning the **same**
synchronous context manager used for current grant/goal/Stop mutations.
`before_resume(scope)` receives the exact original HostExecutionScope, inside
that guard. Scope and Stop are checked fresh around that callback before
ResumeThread. `launch_attempt_callback()` runs before every native
CreateProcessW, including both the pinned CLI version check and the workload
launch. A future parent must account for each native creation entry; none of
these hooks is supplied by model arguments. Invalid hooks fail before creation.
An omitted guard retains callback-time freshness only, not an atomic guarantee.
Supplying an unrelated lock cannot make external authorization mutations atomic.
No worker/tool registration or new permission UI is introduced by this API.

Code must print **one strict JSON value**. Duplicate keys, nonfinite numbers,
invalid UTF-8/surrogates, extra text, excessive nesting and oversized output are
rejected. No exception traceback or untrusted stderr is returned. Known secrets
are redacted from string values and object keys. The result always keeps
`task_verified=false`: process exit and JSON validity do not prove task success.

Only host-approved `AuthorizedInput` files are copied. Each copy requires the
expected SHA-256, a safe unique alias and the existing file grant. Hardlinks,
reparse paths, outside-grant files, changed bytes and size overruns are rejected.
No source directory, home directory or full original workspace is copied.
Inputs, fixed entrypoint and JSON request are read-only inside MXC. Only a fresh,
owned task directory is writable. The base runner never publishes a file by
itself. The separate [project snapshot primitive](MXC_PROJECT_SNAPSHOT.md)
adds explicit host-only staging/capture and single-artifact CAS publication.
Its opaque capture receipt is minted only after this runner's full cleanup;
ordinary result JSON and exit zero carry no publication authority.

## Native launch and resource checks

The official App CLI path, exact SHA-256 and `--version` response are pinned.
The current reviewed copy is
`E:\Tools\AURA\mxc-runtime\0.166.0-alpha.5.1-9cbcc600be53\codex.exe`, with SHA-256
`9cbcc600be53a3aa18c1d55b36b462ebd2844ac16e24b991654774b0819599d5`.
The app removed the previous version directory during its update. The private
copy includes its three signed helpers; source/destination hashes and official
OpenAI signatures were verified in
`artifacts/mxc-runtime-pinned-bundle-20261104.json`. The copy does not add a PATH
entry or automatically adopt future app updates. The Python executable is
separately hash-pinned. Checked executables
remain locked against replacement through both launches. App updates therefore
fail closed until a host-reviewed repin; the npm PATH CLI is never selected.
The whole installed Python standard library/runtime remains trusted software.

`CreateProcessW` uses an explicit executable, quoted argument vector, minimal
Unicode environment and `STARTUPINFOEX` inheritable-handle allowlist containing
only NUL stdin and owned stdout/stderr pipes. It creates the process suspended.
An anonymous JobObject has kill-on-close, aggregate CPU time, CPU hard rate cap,
process-count, per-process committed-memory and job committed-memory limits.
The limits are queried and compared, then the suspended process is assigned and
its membership confirmed. Only then is the primary thread resumed. Any setup,
assignment, query or verification failure leaves the payload unexecuted.

The final fresh host check and Resume occur within the supplied authority guard.
Stop received during suspended creation prevents Resume even without a guard;
the no-guard form does not promise atomic ordering with other host mutations.
Returned process/thread handles are adopted in a finally block around native
creation, so a host exception cannot discard an already-created suspended root.

The host bounds stdout/stderr, polls the authorization and wall deadline, and
terminates/reaps the whole job on success, error, timeout or Stop. Active-process
count must reach zero before cleanup is confirmed. Failure returns a fixed
cleanup-unconfirmed code and retains scratch for host review. A successful
foreground command cannot leave a background child running.

If termination or exact-zero verification fails, native JobObject/process/pipe
handles stay alive in a host-only strong cleanup registry. The runner retains
its original scope object, scratch identity and runtime pin handles. New work is
blocked, including on other native runner instances. Only explicit
`reconcile_cleanup(scope=the_original_scope)` can retry stopping that exact owned
tree; it never executes code again. Scratch is removed and pins are released
only after cleanup is proved. Revocation cannot withdraw cleanup authority.

Tree closure and handle closure are separate facts. `resources_closed=false`
can accompany a valid `tree_reaped=true`. Any CloseHandle failure or exception
is permanently sticky in this process: the exact original API/handle owners,
lease, pin wrappers and scratch remain retained and new native executions are
blocked. A restored API, later successful numeric close, zeroed wrapper field or
`FileIO.closed=true` is not used to erase that uncertainty. The same policy
applies to scratch-monitor directory handles and pin-close failures. Explicit
reconciliation can resolve a previously unconfirmed tree if no close has failed;
it cannot certify a handle whose close already failed. A real outer worker exit
lets the OS release that worker's resources in another ownership scope; it is
not fabricated success for the old in-process lease.

## Filesystem, network, and residual boundaries

The configuration explicitly requests `windows.sandbox="mxc"`, default-denies
filesystem reads, grants read access to `C:\Windows\System32` and the pinned
Python runtime, read access to staged inputs, and write access only to the owned
task directory. It grants no `:minimal`, ProgramData or full Windows directory.
Network is disabled, including host loopback. There is no legacy, elevated,
unelevated, unrestricted, shell, or Docker fallback. The process-local config
directory contains no login or provider credentials; managed cloud config is
not supplied. The command explicitly includes `--include-managed-config`:
in the pinned CLI, choosing `-P` without that flag otherwise ignores managed
requirements. A device-policy rejection returns a fixed failure; there is no
retry that removes the flag, changes isolation, or modifies device policy.
This does not claim that an absent login can retrieve an account's cloud policy.

Configuration discovery also needs a boundary. Codex TOML tables deep-merge:
even a CLI table does not delete a lower layer's extra filesystem grant. Each
job therefore defines a fresh 256-bit-random profile name only in CLI overrides.
The CLI's cwd is the owned **read-only input** directory, containing its own
explicit project-root marker and `.git/HEAD`. There is no `-C task`; the trusted
entrypoint changes to the writable task directory only after MXC is active.
The pinned loader scans project layers only from cwd through its nearest
project root. A real fixture verifies this using an explicitly trusted hostile
parent whose profile name exactly matches the fresh job: the grant is excluded
with the owned boundary, and becomes active in the control that removes it.
No undocumented sandbox-state protocol or organization-policy bypass is used.

These limitations remain explicit in `capability()`; the unresolved broker
ancestor access described above is an additional bootstrap blocker:

- The official MXC policy still grants **registryRead**. This is not complete
  Windows credential isolation. Tests intentionally do not inspect real registry
  credentials or user documents.
- System32 and the entire trusted Python runtime remain readable. They are not
  merely a handful of loader DLLs.
- The boundary shares the Windows kernel; it is not a separate guest OS.
- JobObject does not provide a per-directory disk hard quota. A bounded,
  non-following scratch scan stops excess file count/size, but a burst can exceed
  the target between scans. This is **soft watch-and-stop**, not hard disk quota.
  The host holds non-reparse directory handles, with write/delete sharing
  denied, for the entire ancestor traversal. Reparse attributes are checked
  again on each held child handle. An incompatible/racing directory is rejected
  rather than followed; this avoids using a stale path-only `lstat` decision.
- Standard-library native modules and subprocesses are permitted inside the OS
  boundary. Python itself is not being claimed as a language sandbox.

These residuals need separate host review before model-generated code is ever
enabled. The backend remains off, even after a successful controlled probe.

The later `artifacts/mxc-owned-desktop-fixed-canary.json` additionally found
that a fixed MXC payload could read an exact owned hidden window's caption.
Host before/after positive controls confirmed that the target remained valid.
The payload could not change the caption or open the clipboard; actual
clipboard content access was not tested. No user windows, enumeration, input,
screen capture or clipboard contents were involved. This was the original
desktop-metadata failure. The later Win32k worker blocks this exact owned-caption
path, while the separate ancestor-access failure still blocks bootstrap.
Neither is covered by filesystem/read-only or native-tree cleanup claims.

## Evidence and reproduction

Run offline contract checks with the backend SDK Python:

```powershell
.\agent_backend\nanobot\.venv\Scripts\python.exe tools\mxc_validate.py
```

Explicit opt-in integration runs **fixed trusted test strings only**:

```powershell
.\agent_backend\nanobot\.venv\Scripts\python.exe tools\mxc_validate.py --real-fixed-payloads
```

The report under `artifacts/mxc-backend-validation/` contains fixed test outcomes,
scopes, hashes and capability flags, not raw stdout/stderr, keys or user content.
The previous `artifacts/qa-mxc-probe-20261103/report.json` is preserved: terminating
only the outer Codex CLI left its payload and descendant alive. The new launcher
fixes that ownership gap with assignment **before resume** and job-wide cleanup.
`failed-fixed-profile-deepmerge.json` preserves the earlier failed configuration
canary rather than relabeling that implementation as safe.
`managed-requirements-before-fix.log` separately retains the failed host argument
contract before the policy flag repair. The argument and rejection checks use a
fake transport; the source behavior was checked against the pinned official
`cli/src/debug_sandbox.rs`. They are not a device-policy injection test.

The real suite checks Unicode/quotes, workspace exclusion, an approved read-only
copy, external/loopback denial, memory and CPU limits, process-count limits,
background-child cleanup, wall timeout, stdout/stderr limits, scratch monitoring,
Stop with a descendant, repeated async Stop, authorization revocation, unrelated
inheritable-handle exclusion, and actual native setup/assignment failures that
must not resume a fixed marker-writing payload. Mock cases certify only host
authorization/protocol, not the native isolation boundary.
The suite also checks a real unconfirmed native cleanup, blocked subsequent
launches, retained handles/pins, and later successful reconciliation without
replay, plus a deterministic scratch-directory junction swap.

The later authority/cleanup revision is recorded separately in
`artifacts/mxc-authority-cleanup-final.json`; the original 28-case report keeps
its original hashes/scope. `artifacts/mxc-authority-close-before-fix.log` keeps
the two reproduced failures. New fixed cases test Stop/revocation before
Resume, guard/callback ordering, exceptional handle adoption, sticky process,
directory and pin closes, and an actual failed Job-handle close in a bounded
owned child fixture. The sticky fixture exits normally without resetting the
cleanup registry; its already-stopped resources are released by OS process exit.

References: [Codex Windows MXC](https://learn.chatgpt.com/docs/windows/windows-sandbox),
[permission profiles](https://learn.chatgpt.com/docs/permissions),
[CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw),
[job committed-memory limits](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information),
[job CPU hard cap](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_cpu_rate_control_information).
Configuration discovery was checked against the pinned
[official loader](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.5.1/codex-rs/config/src/loader/mod.rs):
`discover_project_root` checks the nearest marker (and requires HEAD for a `.git`
directory); `discover_project_layers` scans only cwd through that project root.
`ignore_project_config` exists in Rust `LoaderOverrides` but has no public
`sandbox` CLI flag in this pinned build.
