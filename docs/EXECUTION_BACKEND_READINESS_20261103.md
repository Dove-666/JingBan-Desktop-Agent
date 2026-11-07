# Python/build execution readiness audit — 2026-11-03

This file preserves the **pre-repair audit snapshot**. The two MXC control-chain
findings below were subsequently addressed in the separately validated
[authority/cleanup revision](MXC_EXECUTION_BACKEND.md). Original audit reports
and the original 28-case hashes were not relabeled. General Python/build tool
registration, project publication and the other residuals remain out of scope.

The current conclusion is **not ready to enable model-generated Python or
general build commands**. MXC is a useful, functioning Windows isolation layer;
it is not the missing project snapshot, publication, lifecycle and task
verification contract. Both optional backends remain unregistered and disabled.
This audit changed no backend source, OS policy, global app configuration or
Docker/WSL engine state. It did not execute application-model-generated code.

## Current evidence

The previous MXC `fixed-real.json` still describes 28 passing fixed cases; its
four source hashes match current files. The exact installed Codex and Python
executable hashes also match their pins. These are limited fixed-payload
proofs, not general task or complete Windows-secret isolation proofs.

New fixed-code probes produced:

| Owned canary | Host control | MXC outcome |
|---|---|---|
| Original authorized workspace file, not staged | Host-created synthetic text | Open refused, errno 13 |
| Sibling private ordinary file | Host-created synthetic text | Open refused, errno 13 |
| Hardlink from that private file into task scratch | Exact synthetic source only | Link refused, errno 13 |
| Unique synthetic HKCU leaf | Host-created marker | KEY_READ, KEY_QUERY_VALUE and KEY_SET_VALUE all refused, Win32 5 |
| One newly spawned canary process, known address/32 bytes | Exact Popen handle read matches marker hash | OpenProcess(VM_READ + QUERY_LIMITED_INFORMATION) refused, Win32 5; no memory read followed |
| One unique session-persistent GenericCredential | Exact target CredRead matches random blob hash | Exact target CredRead refused, Win32 5 |

No process or credential enumeration occurred. The memory reader checks the
exact process creation time before any possible read, so a reused PID cannot
authorize memory access. No other process or existing credential was targeted.
The only credential was deleted successfully and this exact target returned
ERROR_NOT_FOUND afterward. The owned canary process was waited, all MXC jobs
reported queried limits and exact tree reaping, and owned scratch was removed.
Reports contain booleans, fixed codes and hashes; values, addresses, PIDs and
credential target names are omitted.

The synthetic HKCU denial **does not remove the registry residual**. The pinned
[official policy](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.5.1/codex-rs/mxc-sandbox/src/policy.rs#L269)
still grants `registryRead`. The pinned [MXC README](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.5.1/codex-rs/mxc-sandbox/README.md)
also documents independent hardlink-name permissions. One denied link attempt
does not certify every preexisting alias. System32, the trusted Python runtime,
the shared Windows kernel and soft scratch quota remain explicit boundaries.
No Python syntax filter can establish these OS guarantees.

Evidence:

- `artifacts/execution-backend-fixed-canary.json` — final file/registry/link probe.
- `artifacts/mxc-sensitive-fixed-canary.json` — positive controls, memory and
  credential denial, exact cleanup outcomes.
- `tools/audit_execution_backend_boundary.py` and
  `tools/audit_mxc_sensitive_boundary.py` — explicit opt-in, fixed trusted probes.

## Concrete production blockers

1. **Resume freshness:** `mxc_native._run` checks cancellation/current scope
   before `launch`, but `launch` performs CreateProcess → assign/query → Resume
   without the worker's shared authority guard or a fresh pre-Resume callback.
   A pure fake WinAPI fixture sets Stop during suspended creation: Resume still
   occurs; the later result is CANCELLED with `resumed=true`. Polling Stop is
   useful, but is not an atomic dispatch fence.
2. **Handle-close ownership:** the native `close` ignores failed CloseHandle
   returns, clears original fields and does not retain an owner. The fake fixture
   makes all five closes fail and observes zeroed fields with no pending owner.
   A valid Job0/root-exit proof can still stand; it does not prove resource
   release. Pin/pipe closure failures also need a sticky, exact-owner policy.
3. **Container unknown barrier:** a fake daemon controlled to reject removal
   lets two consecutive `execute` calls create/start two containers despite the
   first `cleanup_unconfirmed`. `_jobs` retention alone does not gate later
   launches or other runner instances. This is an unregistered-backend readiness
   gap, not an assertion that the production model currently has that tool.
4. **Project and result support:** MXC currently stages at most 16 individually
   approved files, runs one inline script and returns one strict JSON value.
   It has no project-tree manifest, dependency/build-runtime selection or file
   export. Docker also has no export/publication. Neither is a general repo
   build/test runner. Current Python uses `-I -S`: ordinary installed site
   packages are not an implicitly approved dependency environment.
5. **Host integration:** a real tool needs one exact CID/generation/request and
   settings/grant/goal-revision binding, execution-attempt accounting, existing
   durable dispatch/checkpoint guards, cancellation and an unresolved-resource
   gate shared with the parent. A result or a zero exit code must not become a
   whole-task success verdict. No such tool registration is added by this audit.

Pure, non-OS fault evidence is kept separately in
`artifacts/mxc-production-readiness-fault-canaries.json` and
`artifacts/container-next-launch-audit.json`. These do not pretend to be native
failure injection or live Docker isolation proofs.

## Minimal next stage

Keep both backends off while making the existing MXC path production-ready in
three bounded changes, before adding any build tools or model-generated code:

1. Add a host-only launch contract: mark the exact attempt before native create;
   register the exact root/Job; hold the existing authority guard for fresh
   authorization and Stop checks immediately before Resume. Add sticky original
   handle/pin/pipe cleanup ownership and a host-global unresolved-resource gate.
   Prove revocation during suspended creation never resumes a marker payload.
2. Add an owned project snapshot and publication boundary. Copy only approved,
   bounded relative files with hashes and no link/reparse aliases. Keep source
   inputs read-only and outputs in fresh scratch. After exact closure, publish
   explicitly selected files through existing WorkspaceFiles authorization,
   source/target hashes and atomic expected-hash replacement. Reject unexpected
   links and modified destinations; never give the subprocess a live host-RW
   project or home directory.
3. Define one honest offline Python-project subset with a pinned read-only
   dependency/runtime manifest and independent hidden output/test checks. Keep
   network/package installation off. Only then consider build-tool bundles and
   clearly named benchmark entries; keep the broad unsupported developer task
   unsupported. Reassess registry/OS residuals as a host threat-model decision,
   rather than adding a permission popup or a misleading language filter.

For stronger separation from personal Windows state, an isolated OS account or
guest/container requires a separate, verified provisioning boundary; this audit
neither creates one nor claims the current user's MXC is equivalent to it.

## Docker/WSL refreshed state

Read-only status was refreshed. WSL status/list return 0, default version is 2,
`docker-desktop` is stopped, and no Docker Desktop/backend or WSL VM process is
running. HNS, vmcompute and WslService are running. An explicit, empty owned
Docker client configuration with credential helpers disabled finds no Linux
engine named pipe. No engine, distribution, VM or image was started/pulled.

The actual CLI is the validly signed **per-user installation**; the container
backend's default Program Files path does not exist here. Its explicit host path
override can represent the installation, but selecting a path is not runtime
verification. No image digest is currently supplied by the default policy.

The small existing startup/error files contain no WSL_E/HCS_E code. A frontend
log has `0x40010004`; it is not assigned as a WSL startup root cause. The old
Unexpected error remains **unverified**, not “WSL missing” or “disk full.”
Observed free space was about 5.42 GiB on C and 37.58 GiB on E; low C headroom is
a reason to avoid engine/image provisioning during this audit, not evidence of
the earlier failure's cause. No broad log, credential file, environment or
registry scan was performed. Only fixed error codes/categories were saved in
`artifacts/execution-backend-readonly-status.json`.

Official context: [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
documents explicit MXC selection and fail-on-unavailable behavior. The managed
networking loopback discussion does not override our tested `network=false`
profile. [CredReadW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw)
targets one named credential in the current logon session;
[ReadProcessMemory](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory)
requires a handle with VM-read authority. The specific denials above are live
canary observations, not inferred universal guarantees from those references.
