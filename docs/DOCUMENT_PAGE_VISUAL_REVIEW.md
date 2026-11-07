# Authorized page previews and separate visual observations

This stage adds an optional in-memory page cache to LibreOffice rendering and
a separate read-only model observation helper. It does not register tools,
change worker startup, or promote a visual model's prose into goal acceptance.
Worker/catalog registration is a separately coordinated stage.

## PDF success and optional previews

`LibreOfficeRenderAdapter.render_document(..., preview_cache=host_cache)` first
publishes its PDF through the existing WorkspaceFiles version/permission/atomic
write boundary. Only then may it call the supplied same-WorkspaceFiles
`PagePreviewCache.publish`. A preview capacity, validation, stale-version or
unexpected cache failure yields `preview_status='unavailable'` and an empty
`preview_pages`; it never changes that successful PDF into an unknown/failed
render or silently starts another render.

When available, `preview_pages` contains only random reference, page number,
dimensions, original PDF SHA-256 and PNG SHA-256. Metadata is placed before
lengthy PDF text in the tool result. It contains no data URL or image bytes.
The existing `previews_published=false` means no PNG files were published to
the user workspace. `HostOfficeRuntime` owns one cache and reusable binding
per worker, and its host-only `preview(reference)` returns authorized
`PreviewImage` bytes. That object must never be returned directly as a JSON tool
result or serialized with `dataclasses.asdict`.

The cache is at most 32 MiB / 64 pages, with bounded PNG header/CRC validation
and document eviction. A foreign-worker reference, changed/deleted PDF,
revoked capability or evicted page is unavailable. References are scoped to
the current worker's lifetime; no page cache/data URL is written to disk or
automatically restored from an old conversation. The implementation and its
passive boundary tests are in `document_previews.py` / `test_document_previews.py`.

## Single-page model call

Host-only API:

`await review_document_page(provider, model, cache, reference, question,
timeout_seconds=60, secrets=(), host_scope_guard=None)`

The provider, model, cache, timeout policy, known secrets and optional scope
guard are host inputs, not model-facing arguments. The helper sends exactly
one authorized cached PNG and the bounded question in a fresh two-message
system/user request, without planner history, tools, provider continuation or
reasoning requests. It ignores provider reasoning/thinking fields. A normal
finish, bounded observation text and no returned tool calls are required.
Known host credentials are redacted from question/observation; provider errors
expose fixed codes only. The helper never logs a request, data URL or raw error.

Use a **plain independent OpenAICompatProvider**, such as the existing ordinary
judge provider with `ModelAPICapabilities()` and no request/response trace hooks.
Do not pass `ObservedPlannerProvider`: its wrapper records messages into the
parent context store, which would persist the image data URL. Host providers
must disable raw wire logging and their own automatic retries/fallbacks. The
installed SDK's OpenAI client uses `max_retries=0`; the explicit chat-only
capabilities avoid a Responses-to-Chat fallback. The helper makes one call and
adds no retry.

The current SDK silently re-encodes inline images when total data URL length
exceeds 1,000,000 characters. Standalone calls still default to strict exact PNG
and reject larger input with `DOCUMENT_REVIEW_INPUT_LIMIT` before sending.
The registered worker explicitly opts into host derivation with the private
`allow_derived=True` argument; the model schema remains only reference/question.
Small PNGs remain byte-exact, while larger pages use the bounded white-matte,
full-canvas JPEG policy in `DERIVED_DOCUMENT_PREVIEW.md`. The original cache
entry and PDF never change. Both original PNG and actual sent-image hashes,
dimensions, content type, transform and encode-attempt count are host-generated.
The single actual data URL stays within 1,000,000 characters, preventing an
implicit second SDK transformation.

The helper checks the cached PDF capability/hash before processing, after
derivation, inside the request coroutine immediately before provider invocation,
and after the network wait. The parent revision/cancel/steering guard is checked
at the same boundaries. A changed/revoked/evicted page discards the observation.
This prevents returning stale evidence; it does not retract bytes already sent
before a later revocation. Stop cancels and drains the same provider task through
repeated caller cancellation, returns no late observation and never resends it.

Conversion runs in an exact owned one-thread executor with a Stop event and a
strong retained owner. Repeated cancellation shields and joins the underlying
concurrent future and executor shutdown; cancellation of an asyncio facade
does not prove the codec has returned. A failed submission/cleanup retains an
unknown owner and blocks further parent tool/model dispatch. The parent returns
needs-user with `DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED`, acceptance false and
eligible prior artifacts preserved as partial delivery. This is separate from
research-child or worker-process closure proof. The registered
tool is exclusive, so each worker has one review in flight, including conversion
and HTTP. This is a resource limit, not an Agent step cap. Codec processing has
bounded input/pixels/tiers/output but no separate OS RSS or hard wall-clock
isolation guarantee.

The JSON result contains reference, page, PDF/PNG hashes and observation with:

- `scope='model_visual_observation'`
- `reference_only=true`
- `visual_layout_verified=false`
- `whole_document_verified=false`

`png_sha256` retains its original cached-PNG meaning. Additional transport fields
are `sent_image_sha256`, `sent_content_type`, `sent_width`, `sent_height`,
`derived`, `transform` and `encode_attempts`. These are validated against actual
host bytes/decoded format and dimensions, never extracted from model prose.
Neither data URLs nor complete bare encodings of the original PNG or actual
sent image are accepted as observation text. Full-image comparisons fold
whitespace and first compare encoded lengths, avoiding a needless large PNG
base64 allocation for a short response.

This is fallible reference evidence from a single page. No helper code updates
the task contract, writes files, authorizes an action or changes completion.

## Registered parent route

Keep the existing `render_document(path, output_path?, overwrite?, expected_hash?)`
schema and route to `office_runtime.render_document` as before; it now includes
the optional preview references. A new read-only / effect-none callback can be:

`inspect_document_page(reference, question) -> review_document_page(plain_judge_provider,
config['model'], office_runtime.preview_cache, reference, question, secrets=host_keys,
host_scope_guard=current_parent_scope, allow_derived=True)`.

If no host Office runtime exists, return fixed `DOCUMENT_PREVIEW_UNAVAILABLE`.
Do not expose model-selected provider/model/cache, filesystem paths, executable,
image URLs, PNG publication, goal-verdict flags or a direct PreviewImage result.
The callback closes over the same per-worker cache used by rendering. It is
registered only with read tools and an explicit host Office runtime. Neither
`allow_derived` nor any codec/provider setting is a model tool parameter.

## Evidence scope

`tests/test_document_visual_review.py` covers optional cache failure after PDF
success, wrong cache, source fence, exact single PNG, stale/evicted/revoked
capabilities, provider failure, response limits, no reasoning/credential leakage,
transport image limit, timeout and repeated Stop. Fake providers/native renders
are boundary checks, not real model quality or actual process-closure evidence.

`tools/qa_document_visual_review.py` runs with the isolated SDK Python and fixed
synthetic DOCX. Actual LibreOffice makes the PDF/PNG, the host cache retains the
PNG, and a real SDK OpenAICompatProvider sends it to a quiet localhost fixture.
The receiver checks a single image's exact SHA, two messages, no tools/reasoning,
and retains only safe counters/hashes. Four explicit calls cover positive,
PDF-changed-during-wait, Stop, and HTTP error; they must produce exactly four
requests. Captured stdout/stderr must be empty; raw captures and data URLs are
not written. Reports are under `artifacts/qa-document-visual-review*` and
`artifacts/document-visual-review-*.log`.

The localhost response is deliberately canned. This proves the actual render,
capability and SDK wire chain, not a cloud model's visual/layout accuracy or
Microsoft Office fidelity. Old LibreOffice 52-unit / 8-worker frozen reports
retain their earlier source scope; this stage has a separate hash manifest.

`tests/test_derived_visual_review.py` adds opt-in/default separation, actual
sent-image identity/format/metadata validation, both image-echo boundaries,
conversion-to-HTTP freshness, fixed provider failure and actual codec-thread
drain under repeated cancellation. `tests/test_derived_review_parent_worker.py`
uses a fresh generated noise-picture DOCX and actual LibreOffice inside the
registered SDK parent. The localhost receiver independently hashes the exact
JPEG sent by the SDK. Positive, Stop, changed-PDF and two explicitly requested
same-batch reviews check no image archive, no replay, exact closure and serial
resource ordering. Old exact-PNG and standalone prototype reports are retained
as historical stage evidence rather than rewritten to cover this new route.
