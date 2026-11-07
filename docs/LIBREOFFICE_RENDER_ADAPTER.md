# Host-injected LibreOffice PDF adapter

The new `agent_backend/nanobot/libreoffice_render.py` is an integration-ready
adapter. A later authorized stage added an optional worker registration;
see `LIBREOFFICE_WORKER_INTEGRATION.md`. Default host startup/settings do not
enable it. The older adapter validation reports describe the earlier direct
API fixture, before worker registration; their source hashes are historical.

## Host API and configuration boundary

The host constructs `LibreOfficeRenderAdapter(renderer,
cleanup_reconciler=renderer.reconcile_cleanup_evidence)`. The renderer is an explicitly
configured `LibreOfficeRenderer` with fixed trusted runtime, SDK Python,
Poppler and private scratch paths. Its `enabled` flag remains false by default.
The adapter neither discovers executables nor reads settings/environment.

The host binds that object into:

`await render_document(files, path, output_path=None, overwrite=False,
expected_hash=None, timeout_seconds=90, renderer=host_adapter)`

Only `path`, `output_path`, `overwrite` and `expected_hash` belong in the
existing model-facing tool schema. `files`, `renderer`, timeout policy and
cleanup reconciliation are host-only. A future isolated-worker bootstrap must
construct the renderer locally from validated host configuration and trusted
code; it must not deserialize an object or import a model-selected project.
The optional worker route reads only the authenticated host run-frame config.
No user-facing runtime-selection setting is introduced here.

`host_status()` is metadata-only: configured/busy/cleanup_unknown. It explicitly
does **not** claim that installed files prove a working runtime, and it does not
launch an application. An enablement decision should separately validate fixed
dependency hashes and run the controlled rendering probe. The existing runtime
pinning and isolation limits in `LIBREOFFICE_RENDER_PROTOTYPE.md` still apply.

## Snapshot, PDF publication and authority

Before starting a renderer, the adapter reads only `WorkspaceFiles.read_bytes`,
uses the existing `inspect_package` checks and verifies output scope, suffix and
overwrite/hash policy. A read grant for an external document does not imply a
write grant for its neighboring PDF. No unsupported macro/legacy format or
external-resource exception is added. The original snapshot bytes and SHA are
the only document content sent to the renderer.

After conversion, the source path/hash and destination scope/version are checked
again under the same WorkspaceFiles cooperative lock. The destination version
captured before rendering is preserved, including an initially absent target;
a later read cannot silently authorize overwriting newly created content.
`WorkspaceFiles.write_bytes` performs its normal final version check and atomic
publication. This remains optimistic protection against cooperative changes,
not a filesystem lock against hostile unrelated host software.

Only one PDF is published through that boundary. PNGs remain private renderer
results and are not published, attached or given model-selected output paths.
The returned ArtifactRef, page count and PDF text identify actual LibreOffice
output. `microsoft_office_rendered=false` and `visual_layout_verified=false`
remain explicit; PDF/text readback does not certify semantic goal completion.
An optional current-worker page cache can now mint metadata references after
successful PDF publication; see `DOCUMENT_PAGE_VISUAL_REVIEW.md`. It still
does not publish PNG files or serialize image data into the tool transcript.

## Stop and per-invocation closure evidence

Each submitted call has a fresh opaque host object identity and input SHA. The
renderer can issue immutable, non-copyable, non-serializable `CleanupEvidence`
only before entering native ownership at all, or after this exact run's
successful `_cleanup`: exact Job active count zero, exact root signaled, checked
handle release and owned scratch removal. Native entry with incomplete cleanup,
failed/unknown creation, or a pending-cleanup gate cannot issue that proof.
The renderer's host validator binds proof to its instance, invocation and input.
An error code, model dictionary, bool or an empty global pending list cannot
replace it.

The adapter awaits an owned executor future through `asyncio.shield`. Every
`Task.cancel()` sets its Stop event, and repeated cancellation keeps waiting
until the real thread finishes. A late successful result after Stop is discarded
without PDF publication. Normal Stop with valid closure proof leaves the adapter
usable for a new request; it never automatically replays the cancelled request.

CancelledError is preserved. Python Tasks may replace that exception while
propagating it, so callers must not treat arbitrary attached cancellation fields
as a durable proof. The adapter consumes authenticated evidence internally;
existing worker cancellation handling may remain conservative.

All ordinary failures expose fixed `OfficeRenderError` codes. Only prelaunch
rejections or a valid closure proof **before publication starts** permit
`safe_no_publication=true`. Unknown native cleanup strongly retains its adapter,
renderer and completed future, blocks new calls, and adds host-produced
`effect_state='unknown'`. Host-only explicit `reconcile_cleanup()` can retry the
trusted renderer's retained cleanup; no model boolean or automatic fallback is
used. Reconciliation must yield the same invocation's opaque evidence, not a
boolean: only a retained exact run that really completes `_cleanup` can mint
deferred proof. An empty registry or an unknown run with no exact ownership
record cannot do so. A previously retained genuine proof also remains valid
if executor-handle release itself needs another explicit attempt.
Publication attempted then failed is also reported unknown and never
labelled safe, even if the PDF may already exist.

## Validation scope

`tests/test_libreoffice_render_adapter.py` uses controlled fake renderers for
concurrency, repeated Stop, missing/forged proof, source changes, destination CAS,
scope, macro/external rejection, bounded PDF data and write-then-throw failure.
The native evidence tests separately verify owner/invocation/SHA binding and
pre-native vs unknown-native issuance. These fake tests are not native rendering
or real process-tree evidence.

`tools/qa_libreoffice_adapter.py` performs actual direct calls to this adapter
with generated-only DOCX, XLSX and PPTX, publishes only declared PDFs, and reads
them independently using the SDK pypdf runtime. It starts a real owned native
helper before cancelling twice, then completes a new real conversion without
host reconciliation. It does not use a cloud model or the worker tool registry;
that later integration requires its own validation.

Evidence: `artifacts/qa-libreoffice-adapter*/verification.json`,
`artifacts/libreoffice-adapter-*.log` and the final source hash manifest. The older
prototype's source-stable report remains a historical snapshot from before the
authorized per-call evidence extension; it is not a hash claim for this revision.
