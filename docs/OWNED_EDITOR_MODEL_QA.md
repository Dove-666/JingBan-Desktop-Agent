# Standalone SDK planning over the owned editor

This phase adds a separate, opt-in QA planner. It does not register production
tools or change `AgentService`, mobile routing, user configuration, or Windows
applications. Its goal and independent file/close/reopen oracle are those in
`OWNED_EDITOR_NATIVE_QA.md`.

The real nanobot SDK runs in a separately owned process, with exactly these
tools in its actual registry:

- `owned_editor_observe()` returns only the six relevant named controls of the
  exact owned fixture. Every observation refreshes opaque refs.
- `owned_editor_act(ref, action, value)` permits only the native fixture's
  `set_value` or `invoke`, followed by a fresh observation. Native identity and
  current foreground are checked by the host and narrow UIA transport.

There is no default tool loader, `AgentLoop`, session/history persistence,
memory store, filesystem tool, arbitrary code, general application executor,
screen capture, clipboard, permission request, or delegation. The planner
chooses the actual input/Save/Close/Reopen actions; the host never substitutes
those actions or automatically repeats one after an unknown outcome.

Only structured synthetic controls and the generated marker are sent to the
provider. HWND, PID, process start time, nonce title, native RuntimeID and path
ownership remain private to the host. Config is captured in memory from the
existing settings only for explicit configured execution, sent through private
stdin, and never saved or printed. The SDK child emits UTF-8 typed protocol
frames and fixed errors, with stderr disabled. Model final text and hidden
reasoning are not emitted or stored. Tests check that the private SDK state
directory contains no files.

The host tracks the actual Save button target, independently reads current
UTF-8 disk bytes and hash, observes the document absent after Close, and
observes a new native control and exact text after Reopen. A final fresh
read-only observation checks the current state. Model prose, a completed SDK
round, an invoke receipt, and a Saved label cannot complete the task.

Stop, foreground loss, protocol failure, an uncertain native effect, EOF, or
the explicit QA deadline stops the run. The host reaps its exact SDK process
and job tree, native editor and native helpers. Unknown cleanup retains owners
and temporary state and blocks another invocation. Failure stays `needs_user`
even if a partial file happens to exist. This deadline bounds a test session;
it does not change production Agent iteration settings.

## Verification and explicit execution

Default execution performs no credential read, model request, native input or
output-file creation:

```powershell
.venv/Scripts/python.exe tools/qa_owned_editor_model.py
.venv/Scripts/python.exe -m unittest tests.test_owned_editor_model.OwnedEditorModelPureTests -v
```

The interactive architecture suite uses a localhost scripted HTTP provider and
the real SDK/native UIA chain. It tests the correct action flow, wrong order
with a false completion claim, expired refs, Stop during an actual outstanding
HTTP request, actual child EOF after an observation, and an unregistered tool.
Synthetic requests stay in memory. These outcomes are guard/control evidence,
not real-model success-rate data.

```powershell
$env:AURA_OWNED_EDITOR_NATIVE_TEST='1'
.venv/Scripts/python.exe -m unittest tests.test_owned_editor_model -v
```

The separately authorized real case reads the existing configured API in
memory without saving it, then performs one task with the same two tools and
independent oracle. It never retries the whole task or replays effects on
failure. It requires an exclusive visible QA window and preserves source and
configuration hashes before and after:

```powershell
.venv/Scripts/python.exe tools/qa_owned_editor_model.py --native --provider configured --timeout 180 --output artifacts/owned-editor-model/configured.json
```

Its scope is **standalone SDK planning over one owned editor document**.
Success does not establish production AgentService integration, phone control,
application restart, general Windows editing, or GPT Agent parity. The older
scripted native reports and their failures are retained separately.
