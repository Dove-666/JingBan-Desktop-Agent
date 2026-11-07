# Web pages in archived working context

Large tool results remain recoverable from the task-private observation store.
Archival does not execute a tool, change the persisted transcript, grant access,
or create a new `GoalEvidence` observation.

For the known `web_fetch` result envelope, the working projection now preserves
a small `web_page` locator: original public URL, source SHA-256, body offset,
next body offset, total source characters, and source/page truncation flags.
Malformed or duplicate fields and inconsistent ranges get the generic archival
projection instead. The locator is parsed metadata, not independent proof that
the page is relevant or that a task is complete.

The excerpt has an explicit body offset and character count. `read_page` and
`read_next_page` suggest calls to the existing `web_fetch` tool. Their suggested
4000-character range is not a new tool or runtime limit.

Two reference spaces remain distinct:

- `context_observation.reference` retrieves serialized tool-result JSON.
- An original `web_source` reference retrieves decoded source-body text.

Neither `recall_context` path creates a new authoritative goal observation.
When a required fact is located in an archive, a small actual `web_fetch` body
read can record that source range through the normal host callback. Arbitrary
assistant text, archive entries and invented IDs never become source evidence.

The independent judge policy now states the existing host constraints exactly:
`answer_only` needs a nonempty answer, no artifact/GUI requirement, zero recorded
effects, and no pending effects. Browser navigation/reading may count as browser
effects even for an informational question. A genuinely completed source-based
reading with those effects therefore needs `satisfied` plus actual supporting
evidence IDs. Host effect, GUI, provenance and requirement checks are unchanged.

The controlled SDK regressions use original AgentService/worker/tool registry,
context projection, evidence ledger and goal guards. Only public-source network
and browser callback boundaries are fixed owned test doubles; planner and judge
HTTP run on localhost. These tests verify contracts, not real-model ability or
rendered browser behavior. The exhausted 100-request real run remains failed,
and its reports/checkpoints are not reclassified by these offline checks.
