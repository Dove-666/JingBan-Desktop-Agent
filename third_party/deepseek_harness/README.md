# DeepSeek Harness reference and attribution

## Current runtime integration (Windows 1.1.0)

Whale Companion now executes the official Harness SDK profile. The npm packages
`@deepseek-ai/dsh` and `@deepseek-ai/dsh-sdk-client` are pinned to `0.2.2-rc.3`
in `agent_backend/deepseek_harness/package.json`; npm lockfile integrity identifies
the actual published tarballs. The upstream MIT notice is retained in [LICENSE](LICENSE).
The host adapter does not reimplement the Agent loop or tool executor.

For a fresh Windows source checkout, use Node.js 24.9 or later and the .NET 10
SDK, then prepare the local CUA package before installing the locked adapter:

```powershell
npm --prefix third_party/deepseek_harness/cua-windows install
npm --prefix third_party/deepseek_harness/cua-windows run build
./third_party/deepseek_harness/cua-windows/build-native.ps1
npm --prefix agent_backend/deepseek_harness ci
npm --prefix agent_backend/deepseek_harness test
```

Run the native build script from PowerShell 7. Application users receive these
compiled components in the installer and do not run the build commands.

The Windows CUA package is not yet available on npm. Its sources are vendored in
[cua-windows](cua-windows/) from upstream commit
`39163dd30f703a79079e2d2446545cff3c453738`. See its source manifest, build script
and package README for exact files and the native helper build. Its self-contained
Windows helper is generated for distribution; the .NET SDK is a build input only.

The published npm modules and this later source revision share a version label
but are **not one identical snapshot**. The adapter selects the published shared
CUA plugin's raw `cua` tool (`agentPreset: false`). The Windows provider build also
provides the shared plugin's static `guidance` property alongside upstream's newer
`getGuidance` callback. These are explicit compatibility adaptations; upstream
source hashes are retained separately from generated output hashes.

Native testing also exposed a response-flush race in the upstream Windows
server: a fast sequential request could be mistaken for a concurrent operation
and disconnect the client. The local fix and original source are retained in
`cua-windows/patches/`; concurrent operations remain rejected. The patched helper
passes 250 consecutive requests without sleeps and an owned-window input/capture
test. Rebuilding the helper does not require changing the SDK tool loop.

The Python host retains the product's paired-device protocol, credential vault,
process shutdown, activity UI and downloadable file copies. Previous local
nanobot/GUI execution code is not loaded or shipped by the new integration.

## Historical Python adaptation (before Windows 1.1.0)

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
