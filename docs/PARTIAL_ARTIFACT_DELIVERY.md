# Interrupted task delivery

An interrupted Agent run may return verified **partial file attachments** while
the task stays `needs_user`. It never reports whole-task success. The reply is a
fixed Chinese explanation; provider text, exceptions, paths, and model reasoning
are not inserted into the error explanation.

Eligibility is deliberately narrow: the host must have recorded a successful
write; current bytes must still match that write's SHA-256; automatic format,
source-derived and explicitly declared checks must pass. Failed deterministic
requirements for a file exclude that file. Unrelated existing files, read-only
inputs, verify-only inputs, and model-declared paths cannot become attachments.
Files saved only through GUI actions without a host-recorded file write are not
eligible for this recovery path.

Pending or unresolved user corrections block recovery. Stop/cancellation keeps
its existing cancellation semantics. Unknown effects, failed required tools,
unfinished plans, and the GUI parent verification gate remain unresolved. A good
individual file may be retained while any of those blockers prevents task
completion. No model request or tool replay is added for recovery. Existing SDK
request handling and its own end-of-budget summary behavior are unchanged.

Reports still go through the ordinary host ArtifactStore: it rechecks current
grants, exact source hash, and conversation identity before saving an immutable
delivery copy. The scope says `whole_task_incomplete`. A late file change or a
revoked grant rejects delivery; the original file is preserved.

The optional AgentService `diagnostic_callback` receives only fixed `phase` and
`code` values. An observed provider failure is distinguished from an otherwise
unknown SDK turn failure (`SDK_TURN_FAILED_UNKNOWN_CAUSE`). This identifies the
observed boundary, not an inferred root cause. Recovered previous provider
failures are cleared so they cannot be blamed for a later SDK exception. The
benchmark runner stores at most 64 of these fixed events per case, without raw
errors, credentials, tool arguments, hidden reasoning, or user data.

Validation uses owned temporary files and localhost HTTP with the actual SDK and
AgentService. Those controlled failure tests are architectural regressions and
are not included in real-model benchmark success rates. Historical benchmark
reports, including the first JS Unicode failure, remain unchanged.
