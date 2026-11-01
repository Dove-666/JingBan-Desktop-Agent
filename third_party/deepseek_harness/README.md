# DeepSeek Harness reference and attribution

AURA's Agent task-plan validation is adapted into Python from
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness), maintained
by DeepSeek and distributed under the MIT License. The original notice is
preserved in [LICENSE](LICENSE).

Reviewed revision: `ce7073d51254c26cd63b22a60835b8f948deb447` (2026-11-01).

## Adapted algorithm

The `update_plan` implementation in
[`agent_backend/nanobot/task_harness.py`](../../agent_backend/nanobot/task_harness.py)
adapts the `toTodoList` validation and whole-list snapshot/projection design in
[`packages/todo/tool-todo/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/todo/tool-todo/src/index.ts).

The adapted rules trim plan text, reject empty or duplicate entries, validate
the `pending` / `in_progress` / `completed` lifecycle, allow at most one active
step for AURA's sequential executor, and replace the complete plan snapshot
on an accepted update. The local implementation integrates these rules with
AURA's existing nanobot worker and task state rather than loading Cordis.

## Additional design references

The upstream
[`session-checkpoint-policy`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/session/session-checkpoint-policy/src/index.ts)
and
[`SessionHandle` persistence contract](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/session/session-persistence/src/handle.ts)
inform the semantic checkpoint ordering in AURA's
[`desktop_pet/agents/checkpoints.py`](../../desktop_pet/agents/checkpoints.py),
[`desktop_pet/agents/service.py`](../../desktop_pet/agents/service.py), and
[`agent_backend/nanobot/worker.py`](../../agent_backend/nanobot/worker.py).
The Python implementation uses SQLite commit plus host acknowledgement before
downstream model/tool dispatch; it is a local implementation of the reviewed
durability-barrier design rather than a copy of the TypeScript persistence
backend.
The
[`agent-loop`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts)
was reviewed for structured turn outcomes and tool-call settlement.

These references do not provide a general artifact-correctness verifier.
AURA's deterministic calculations, output verification, sensitive-data
projection and Windows integration are separate local implementations.

The reviewed source copies and revision manifest are retained under
`artifacts/agent_upgrade_20261101/upstream_reference/` for local audit.
