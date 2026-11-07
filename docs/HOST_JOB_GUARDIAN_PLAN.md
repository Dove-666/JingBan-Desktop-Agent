# Durable owned Job guardian proposal

Historical design baseline. The bounded authenticated remote-queued integration
and its actual crash evidence are documented in
[Remote queued guardian v1](HOST_JOB_GUARDIAN_REMOTE_V1.md). The complete proposal
below is not claimed implemented or universally validated. Startup `unknown`
tasks without authenticated closure retain their serial effect barrier. Missing processes, absent named Jobs,
or an exited App do not automatically release that barrier. This proposal does
not change the currently delivered queue or claim crash recovery is available.

## Minimal ownership design

Use one short-lived, stdlib-only, isolated Python helper per active serial task.
Model stdin/stdout remain directly between App and worker: the guardian never
receives model payloads, prompts, API credentials, document names, or raw stderr.
It is outside the worker's Job, with no network or external-service role.

Before the first `run` stdin byte, the host durably creates a private manifest,
then transfers **exact handles**, not PIDs or object names, to the helper:

- The same owned Job handle, limited to query and terminate access.
- The exact root worker process handle, with query and synchronization rights.
- The App process synchronization handle and a host-owned Stop event.
- Private bootstrap and acknowledgement pipe ends and the receipt target.

Launch with an explicit `STARTUPINFOEX` handle allowlist; worker descendants
receive none of the guardian, Job, receipt, or authentication handles. Use a
minimal environment allowlist, isolated Python, an absolute trusted helper, and
fixed command-line options. The guardian must acknowledge `ARMED`, bound to the
manifest digest and run nonce, before first stdin dispatch. Prefer a suspended
worker, verified Job membership/limits, guardian acknowledgement, and only then
Resume. Earlier crashes remain conservatively unknown. An external Job that
also contains the App/helper can defeat helper survival: verify and report this
capability, without changing OS policy or claiming universal crash survival.

The guardian waits on the exact App handle and Stop event. App exit requests
`TerminateJobObject`; that call alone is not closure. Retain the Job handle until
`QueryInformationJobObject` succeeds with `ActiveProcesses == 0` and the exact
worker process handle is signaled. Then write a durable authenticated receipt
and release handles. A root worker exit while descendants remain is insufficient.
No inference from `OpenJobObject` failure, owner PID absence, process enumeration,
Job notifications alone, or last-handle-close behavior is allowed.

## Durable receipt and trust boundary

Keep manifests and receipts outside workspace, model file scope, and remote
sync. Require an owned directory and owner-only ACL, no reparse/hardlink target,
exclusive ordinary-file creation, bounded canonical JSON, flush, and atomic
replacement. A host-generated 256-bit run nonce binds job ID, CID, generation,
operation ID, exact process creation time, installation identity, and launch
epoch. `launch_epoch` means a random App incarnation, **not an OS boot GUID**.

Use a per-job HMAC key, protected on disk by current-user DPAPI with UI disabled
(not machine-wide DPAPI), and passed only through the private bootstrap pipe.
The receipt is at most 4 KiB and includes only fixed schema/version, the exact
manifest/run identity, observed closure flags, fixed status codes, and HMAC.
Do not serialize prompts, paths, raw exceptions, handles, or model secrets.
DPAPI/HMAC does not defend against an attacker already able to read arbitrary
same-user process memory; the host application remains a trusted same-user
boundary. Model file access must exclude this private location.

After restart, validate the manifest and receipt with exact identity and a
single-use CAS. Reconcile **worker stopped only**; never replay the operation or
mark a task completed merely because it closed. Completion still requires the
independent persisted full assistant reply and current host reconciliation.
Missing, truncated, replaced, oversized, mismatched, unauthenticated, or stale
receipts, query failure, and App plus guardian failure all retain `unknown`.
Power loss/reboot cannot be converted into closure by checking that the old
guardian no longer exists. An OS boot identity is an optional future additional
binding; it is not needed to excuse a missing exact closure observation.

## Fixed-crash validation before integration

Use only an owned synthetic App fixture and a worker spawning a waiting
grandchild; no model, network, real documents, production keys, or OS changes.
The independent test retains exact root/grandchild process handles. Inject App
termination before manifest, after Job assignment, before/after ARMED, after
first stdin, after root exit with a surviving child, during receipt write, and
after durable receipt. Only a valid receipt plus signaled retained process
handles can justify releasing the fixture barrier.

Negative cases: kill guardian, kill both processes, fail Job query/termination,
forge or replay HMAC, change CID/generation/run nonce, replace receipt files,
partially write receipts, leak an inheritable handle, and leave a child active.
All must fail closed. Put synthetic secrets in worker output and confirm the
guardian and receipt never contain them. Probe crashes at each handoff; tests of
the Python fixture or fake Win32 API alone are not actual process-tree evidence.

## Resource budget and implementation boundary

No helper exists while idle. With the serial queue, at most one active helper
is expected. A stdlib Python helper may use approximately 15–30 MiB RSS; this is
an estimate requiring measurement, not a measured promise. Limit each manifest,
DPAPI key record, and receipt to 4 KiB, at most 12 KiB per task, with a bounded
confirmed history (for example 128). Keep unresolved evidence. Use OS event
waits while monitoring, bounded checking during cleanup, and no idle DB polling.

Implement and independently validate the helper first. Only after real crash
canaries pass should a later change bind its receipts into `WorkerLifecycle`
and queue reconciliation. The present queue remains honest about unresolved
startup workers.

## Primary references

The ownership design above is an engineering inference using documented Win32
primitives, rather than a claim that Microsoft guarantees this whole protocol.

- [Job objects and process-tree lifetime](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Exact inherited handles and explicit inheritance lists](https://learn.microsoft.com/en-us/windows/win32/procthread/inheritance)
- [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject)
- [Waiting on process handles](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)
- [Current-user DPAPI and integrity protection](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
