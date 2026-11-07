# Exact worker closure evidence

`AgentService.execute(..., worker_lifecycle_callback=...)` synchronously supplies
an opaque `WorkerRunHandle` immediately after the owned Popen is registered,
before containment attachment and before the first run command is committed.
The connected host coordinator binds its job to that handle. The callback must
finish its host binding before returning; an exception prevents dispatch. This
is a host Python interface, never remote RPC, and receiving the handle alone
does not prove containment or closure.

An optional synchronous `worker_dispatch_guard(handle)` protects only final LF
commit and durable checkpoint ACK. Real worker ready/version validation and
escaped JSON staging occur outside that guard. A failed stage is never completed
or retried; EOF without LF is not a run command. The owned input watchdog must
join and every active pipe write must return before exact closure is accepted.

`service.worker_closure(handle)` returns `None` until the exact Popen instance is
reaped and its owned process tree is confirmed empty. On Windows the service
retains its Job Object handle, requests termination, waits on the exact Popen,
queries that same job's `ActiveProcesses`, and closes the job handle only after
the count is zero. Calling close, seeing a PID disappear, receiving final JSON,
or finding an empty UI/registry is not this proof.

Use `service.validate_worker_closure(handle, receipt)` before constructing a
queue's host completion/stop proof. Handles and receipts cannot be constructed
from JSON, and a receipt for another run, service or Popen identity is rejected
even if a PID is reused. Both types are immutable. Retained completed evidence
is bounded; it is not a durable cross-restart process identity.

When cleanup cannot be confirmed, the service retains the exact process/tree,
blocks another execution with `busy`, and emits fixed
`WORKER_CLEANUP_UNCONFIRMED`. A returned result is downgraded to `needs_user`,
keeping its files and existing goal checks while adding a distinct cleanup
issue. An original exception keeps its original safe error. The Stop API merely
requests stopping; it does not mint a closed receipt.

`reconcile_worker_closures(timeout_seconds=...)` explicitly retries only cleanup
which the execute-finally path already marked pending. It never scans or kills
a currently active task and never replays a task, tool or model request. After
fresh exact closure is confirmed it releases tracking and pipes. Cleanup failure
does not discard the handle. No background scheduler is started by this API.

A pre-dispatch worker without an attached tree can be confirmed by exact reaping
because this entrypoint has received no run command or helper authorization.
After dispatch, absent containment is insufficient. Non-Windows process-group
or PID absence does not mint tree closure evidence. A completed/closed tree will
not send another stop signal on a later call.

An optional `execution_attempt_callback(attempt)` separately binds an opaque
HostExecutionAttempt before validation or launch. `execution_no_launch(attempt)`
returns a NoWorkerLaunchReceipt only after that invocation has exited all
launch-capable paths without ever attempting Popen. The service marks attempted
before calling Popen, so an exception from Popen cannot earn this proof. The
coordinator binds one opaque request owner and validates it again when releasing
the job as interrupted/cancelled. This is not a WorkerClosureReceipt and cannot
complete a task, replay it, clear another unknown worker, or recover a receipt
across restart. All unfinished attempts are retained; terminal no-launch/denied
history is bounded to 128, independently of unresolved WorkerLifecycle records.

Tests use fake owned handles and real SDK workers with localhost HTTP. They
cover failed wait/query/handle-close, retained tracking, PID reuse, serialized
proof forgeries, result downgrade, explicit later cleanup, and Stop winning over
a late model reply. No unrelated process is enumerated or terminated.
