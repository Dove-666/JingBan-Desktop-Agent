# Optional host-selected LibreOffice worker tool

The worker now routes the existing `render_document` tool to LibreOffice only
when its authenticated host run frame has `config.office_renderer`. An absent
key retains the previous COM route. A present but malformed configuration is
rejected before planning. A selected LibreOffice failure never falls back to
COM. Service startup, settings and chat-controller injection are not changed
by this stage, and no local runtime configuration file was created here.

## Fixed host configuration

`agent_backend/nanobot/host_office_runtime.py` accepts exactly four keys:

- `engine`: exactly `libreoffice`.
- `runtime`: absolute local path of the verified extracted runtime.
- `scratch_parent`: absolute local private task-scratch parent.
- `pdftoppm`: absolute local trusted Poppler executable path.

No Python, entrypoint, profile, environment, arbitrary switch or model-selected
engine field is accepted. Python is always `sys.executable` from the worker's
isolated SDK runtime. A lazily constructed binding is reused within that worker.
Importing either host configuration module starts no renderer or background task.

The optional desktop loader is
`desktop_pet.agents.office_runtime.load_office_runtime()`. It reads only the
fixed ignored path `agent_backend/nanobot/runtime/office_renderer.json`, returns
`None` when missing, and otherwise returns the same strictly checked four-key
configuration. It requires a bounded UTF-8 regular file, rejects duplicate JSON
keys, reparse/redirection, hardlinks, unexpected fields and unstable reads.
Errors expose only `HOST_OFFICE_CONFIGURATION_INVALID`, never file content or
private paths. This is an opt-in local host configuration, not model authority
or protection from arbitrary same-user host compromise.

The host service must explicitly decide when to load/inject this configuration.
That future change should also bind its engine/path identity to the existing
fresh queue/settings policy scope. No service hook or policy update is claimed
in this stage. Installed files alone are not runtime-readiness evidence.

## Per-tool isolation guard, not a global file policy

Ordinary Agent read/write operations and user-confirmed file grants continue
to work even when they overlap renderer private paths. Such an overlap makes
only `render_document` unavailable with `OFFICE_ISOLATION_UNAVAILABLE` and
`safe_no_publication=true`; it does not reject an otherwise valid Agent task.
The worker must not remove, narrow or silently rewrite unrelated user grants.

Before creating a native binding, before reading its authorized snapshot, and
again inside the final WorkspaceFiles publication lock, the dedicated renderer
guard checks current workspace/read/write grants against private scratch,
runtime/manifest, Poppler binaries, SDK runtime and trusted helper source.
Both ancestor and descendant overlaps are rejected. A grant added after
conversion began is rechecked before publication; no PDF is published in that
case. Existing source hashing, active-content/external-resource rejection,
destination CAS and WorkspaceFiles atomic writes remain unchanged. No PNG is
published by this tool.

The installed SDK's `_partition_tool_batches` executes non-read-only/exclusive
tools in separate batches. `render_document` and `request_file_access` are
exclusive, so model tool calls cannot install a grant or run a parallel file
operation during a native render. This statement concerns the current SDK
execution chain and the tool preflight/publication fence, not a global grant
framework, filesystem sandbox, or protection from unrelated host processes.

## Cancellation and unknown results

The adapter continues to shield the real executor future on Stop, sets its
event and waits through repeated cancellation until the actual helper thread
settles. Normal Stop with authenticated per-call closure proof can be followed
by a new call; a cancelled result is not published or automatically replayed.
Unknown cleanup strongly retains its state and sets host-produced
`effect_state='unknown'`. The worker's existing generic effect-state handling
keeps the file ledger uncertain, blocks another file-effect tool and prevents
`finish_task(completed)` from overriding that uncertainty. No error string or
model-supplied boolean can clear it.

## Evidence and scope

`tests/test_host_office_runtime.py` exercises strict configuration, private
scope guards, lazy reuse, late-grant publication denial, local-loader failure
cases and compatibility of ordinary file tools. These fake-boundary cases are
separate from actual renderer evidence.

`agent_backend/nanobot/tests/test_libreoffice_worker.py` starts the actual
isolated SDK worker with a loopback scripted planner and separate judge:

- Registered `create_document` → `render_document` → `read_document` produces
  an actual PDF; an independent SDK pypdf reader checks pages/text before the
  goal judge accepts it.
- Real helper private-profile activity is observed before Stop. The cancelled
  PDF is absent and private scratch returns to its baseline.
- A controlled fixed fixture suppresses reporting only **after actual exact
  native cleanup succeeded**. The resulting unknown cleanup blocks the next
  `write_file` and completion. It does not intentionally leave a native process
  live, and does not claim that a simulated failed observation is an OS failure.
- Initial/late broad grants retain ordinary reads/writes while only rendering
  is rejected. Malformed host config and model-supplied engine/path/profile
  overrides are rejected. Route-only stubs confirm absent-config legacy COM
  selection and no COM fallback from a selected missing LibreOffice runtime;
  these stubs are not real COM rendering evidence.

Reports are under `artifacts/qa-libreoffice-worker` and
`artifacts/libreoffice-worker-*.log`. No cloud model, production document,
production API key, Office session or desktop application was used. Real
LibreOffice PDF rendering is not Microsoft Office fidelity or visual-layout
verification; the same limitations in the prototype/adapter documents apply.
