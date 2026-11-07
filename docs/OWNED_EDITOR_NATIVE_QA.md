# Owned native editor roundtrip QA

This opt-in harness checks one real Windows Qt editor against native UI
Automation. It changes only a host-created temporary `agent_workspace` child
and its fixed `report.txt`. It is not registered in the production Agent and
does not start a model, read settings or credentials, open a user application,
enumerate the desktop, capture an image, or use the clipboard.

The goal is: enter a newly generated UTF-8 Chinese/emoji marker, save it through
the native Save button, close the document, reopen that saved document through
the native Reopen button, and read the current value from a newly created
native editing control. “Close” means closing the document in the same owned
editor process. This phase does not test restarting an application.

## Running it

Without `--native`, the command prints `not_run`, creates no output file, and
does not start any UI or model. A Windows interactive session is required for
the explicit native command:

```powershell
.venv/Scripts/python.exe tools/qa_owned_editor.py --native --case positive --output artifacts/owned-editor-native/positive.json
.venv/Scripts/python.exe -m unittest tests.test_owned_editor_qa -v
```

Use a time window without another visible QA task. The editor activates only
once at startup. Esc, physical input to the editor, a foreground mismatch, or
the watchdog stops the run; it does not reclaim focus after the user switches
windows. The previous foreground window is restored only if its HWND, PID and
process start time still match and no new external choice would be overridden.

## Scope and ownership

`qa_owned_editor_fixture.py` creates the native Qt widgets. Its private stdin
protocol accepts setup, Stop, and two deliberately restricted negative-test
controls: replacing the edit control and showing a same-process decoy window.
There is no private type, save, or reopen shortcut. Save uses the existing
versioned `WorkspaceFiles` implementation. The expected marker is never part
of fixture setup; it must reach the document through UIA `ValuePattern`.

The fixture is launched with the host's real Python executable, rather than
the Windows venv redirector. The fixed repository Qt dependency directory is
added explicitly by the fixture. This preserves the requirement that the
exact returned `Popen` PID owns the native window; no child PID taken from an
IPC message can substitute for it.

`qa_owned_editor_uia.ps1` starts from only that HWND. It requires the exact PID,
process creation time, nonce title and native root RuntimeID. It never starts
from `AutomationElement.RootElement`, discovers general application windows,
or reads another root. Its action allowlist is `set_value` on `Document text`
and `invoke` on the three named fixture buttons. Host and native transport
both check ownership and foreground, including immediately before the native
pattern operation. A separate guard monitors foreground while a helper runs.

The host records exact fixture/helper `Popen` instances in `WorkerLifecycle`
with owned Windows Job Objects. Cleanup means reaping those instances and
confirming their own job trees empty. A Stop request, empty PID lookup, or
closed window is insufficient. Unconfirmed ownership/cleanup retains strong
references, blocks another editor run in that process, and detaches temporary
directory automatic deletion. This is a standalone QA boundary, not a new
production authority or a general process-killing capability.

## Independent goal check

`DesktopControls` supplies actual native snapshot/ref/action/readback behavior.
The host oracle independently reads the fixed saved file and requires all of:

- A stable regular single-link file, with no symlink/reparse directory or
  redirected parent; bounded UTF-8 bytes exactly equal the expected marker.
- The actual save hash is still current when the goal is checked.
- An intermediate fresh native snapshot has no editor control and has Closed
  status; the reopened control has a different actual RuntimeID.
- A final native readback matches the saved bytes and Reopened status.
- The exact owned native identity remains valid and no Stop or foreground
  violation has occurred.

Saved labels, button dispatch, fixture counters, and model prose cannot satisfy
the goal. Reports contain ordered host-observed milestones, safe action counts,
file hashes and closure results, with no screenshots or clipboard contents.

The cases `save_noop`, `save_wrong`, `reopen_noop`, `stale`, `foreground`,
`cancel`, and `timeout` are guard tests. They pass only when the intended goal
is rejected or an invalid action is blocked. They are never counted as native
task successes or model successes. The timeout case deliberately bounds the
native action helper startup/dispatch and does not assert that an application
became unresponsive.

The initial native pass and seven guard cases are in `artifacts/owned-editor-native/`.
The retained `positive-initial.json` records the original venv-PID rejection;
`workspace-junction-before-fix.log` records the independent oracle's original
directory-redirection defect. Later fixed-source verification is a separate
report and does not replace either failure.

## Remaining model integration

This phase uses a fixed scripted choice of native controls. Its success proves
the bounded fixture, control chain and independent checks, not model autonomy,
general editing skill, or GPT Agent parity.

A later opt-in QA adapter can give an actual SDK planner only the owned UIA
observation/action tools and this marker goal, with private host ownership
kept outside the model schema. It must choose its own actions and pass the
same external file/version/close/reopen oracle. It must have no direct file
write, arbitrary code, general application, screenshot or recursive delegation
tool, and must not reuse production user state or browser profiles. Real
configured-model runs are scheduled separately and reported separately from
scripted localhost/provider controls.
