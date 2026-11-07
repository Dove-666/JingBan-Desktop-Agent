# Host proof for an execution that never attempted launch

Status: implemented in execution_attempt.py, AgentService and the coordinator.
This additional proof releases only a finished invocation that never attempted
launch. Existing exact-worker cleanup and unknown barriers remain unchanged.

## Reproduction

`tests.test_agent_queue_prelaunch_review` uses actual authenticated agent.send,
SQLite admission, ChatController and AgentService.execute. It copies the real
isolated interpreter to an owned temporary python-owned.exe. Readiness succeeds
and the RPC receives a durable queued ACK. Only that copy is removed before the
queue wake. The actual service returns not_configured during filesystem
validation; an observing wrapper records zero Popen attempts and no fake handle
or process result is supplied. Restoring the same file repairs readiness without
changing policy identity. Advancing the injected lease clock leaves the first
job unknown/unclosed and the second queued indefinitely.

Evidence: artifacts/agent-queue-prelaunch-baseline.json contains safe fixed
observations and producer SHA-256 values; agent-queue-prelaunch-observed-failure.log
is the original unwrapped failed desired-behavior assertion. Baseline hashes are
report-time on-disk values, not a before/after stability or loaded-binary
attestation. They do not retrospectively prove which source was imported.

The regression is now an ordinary passing test. Its separate
agent-queue-prelaunch-recovery.json records source hashes at test entry/exit and
invalidates the run if they change. It proves the old request stays interrupted
without a reply and the next new request uses a real SDK worker to write/read
and finish a file. A second actual-RPC fault case proves that attempted Popen
errors remain unknown and are not replayed. No external model is contacted and
no real interpreter is changed; original failure logs are retained.

## Minimal additional host interface

Separate opaque HostExecutionAttempt and NoWorkerLaunchReceipt types are used.
Neither can be built from JSON/dictionaries, supplied by a worker, or equated
with WorkerRunHandle/WorkerClosureReceipt. The issuing service keeps exact object
identity and a private nonce, binding the invocation to canonical CID and its
single coordinator request. Reused handles, foreign services and another job's
receipt fail validation.

An optional synchronous execution_attempt_callback(attempt) runs at the beginning
of AgentService.execute, before any code path can attempt Popen. It binds that
exact invocation to the existing host job context. It does not take the dispatch
lock across validation, staging or model work. A callback exception aborts before
launch; no callback binding means the coordinator remains conservative.

The service privately tracks:

1. CREATED: this invocation exists, with launch_attempted false.
2. LAUNCH_ATTEMPTED: set **before** calling Popen, not after return or after
   worker_lifecycle_callback. This transition is irreversible.
3. WORKER_BOUND: the returned exact Popen has the existing WorkerRunHandle.
4. FINISHED: execute has exited and cannot resume validation/start another
   process under this attempt.

Only FINISHED plus launch_attempted false issues NoWorkerLaunchReceipt at the
end of execute's outer finally; no callback or launch-capable path follows the
seal. It is never minted from an error code,
empty process registry, absent PID, cancelled status or missing lifecycle
callback. A Popen call that raises still crossed LAUNCH_ATTEMPTED and cannot get
this receipt, even for FileNotFoundError. Returned Popen registration failure
also cannot get a no-launch receipt. Existing exact worker/tree/write cleanup
proof remains mandatory whenever launch was attempted.

## Coordinator use

service_entered remains conservative. The coordinator binds an active attempt
once to exact context and a fresh opaque request-owner token. Finished/reused
attempts cannot bind another job, even within the same CID. The registry retains
the lightweight owner token, never the request body or credentials. At job
finalization it accepts either the existing exact closure receipt
or a validated no-launch receipt for this invocation. No-launch may only release
this job's execution slot as interrupted/cancelled; it never creates completed,
accepted goal evidence, artifacts, a fabricated WorkerClosureReceipt, or an
authorization to replay the failed message. The user message retains its fixed
configuration error and original text. A later independently submitted job may
start after normal fresh scope checks.

No-launch cannot clear another job's unknown barrier or unresolved service
registry. It is process-local and cannot recover prior-process unknown rows.
Missing callback, unfinished invocation, service failure before proof issuance,
ambiguous launch and lost attempt history all keep the current fail-closed
behavior. Existing grants, settings, lease, cancel/commit and GUI/effect gates
are unchanged.

All unfinished attempt identities remain tracked. The terminal history retains
at most 128; a finished attempted invocation is terminal only for the no-launch
question (always denied), and pruning it does not prune any unresolved worker.

## Regressions

- Actual RPC reproduction above is a normal passing test: the first job is
  interrupted with a valid no-launch proof, never replayed; after repair a new
  job really writes/reads/finishes and saves its reply.
- Validation/config/cancellation before launch may receive exact no-launch
  proof only after execute exits. While execute is paused mid-validation, proof
  remains unavailable and later jobs cannot start.
- Popen attempted then raised; Popen returned then registration/callback failed;
  real worker created then startup/pipe cleanup failed: no no-launch receipt.
- JSON forgery, foreign issuer, stale/reused attempt and cross-CID/job proof are
  rejected. A no-launch proof never cancels an unrelated unknown barrier.
- Cancellation racing attempt callback/finalization remains commit ordered;
  no automatic model/tool resend, and original safe exceptions remain intact.

`tests.test_execution_attempt` exercises identity, bounded history, unfinished
validation, callback/cancellation preservation, attempted launch errors, actual
owned-process registration/callback faults and cross-job rejection.
`tests.test_agent_queue_prelaunch_review` covers the actual authenticated RPC
recovery and conservative attempted-Popen failure. These local fixture/fault
results do not count toward real-model capability success rates.
