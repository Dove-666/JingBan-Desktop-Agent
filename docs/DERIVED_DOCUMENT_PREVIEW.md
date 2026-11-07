# Explicit host-derived document preview prototype

`agent_backend/nanobot/derived_document_preview.py` is the pure host image
converter. Its original standalone prototype evidence is retained unchanged.
A subsequent reviewed integration explicitly enables it in the registered
visual-review callback using a host-only `allow_derived=True`. Standalone review
calls still default to strict exact PNG with the original transport limit.
No PDF/PNG source, task permissions or goal-verification rules are changed.

## API and immutable result

`derive_document_preview(png_bytes, cancel=None)` takes immutable PNG bytes,
not a path, model program, encoder name, environment or executable. Pillow is
loaded lazily from the existing environment. A missing/broken dependency fails
with a fixed error; no package installation or SDK fallback occurs.

`DerivedDocumentPreview` is frozen host data, not an authorization/closure proof.
Its metadata includes:

- `original_png_sha256` and actual `sent_image_sha256`.
- Source and derived width/height, actual content type and `derived` boolean.
- A concrete fixed transform label and bounded actual encode-attempt count.
- `reference_only=true` and `visual_layout_verified=false`.

`image_bytes` is host-only. `data_url` is an immediate computed property, not a
stored field. `metadata()` includes neither image bytes nor base64. Do not return
the whole dataclass through a model tool or serialize it with `asdict`.

## Fixed conversion policy

Inputs are limited to 8 MiB, 1800 pixels per side and 3,240,000 pixels. The
existing bounded PNG header/chunk/CRC check runs before Pillow, followed by
actual pixel decode. Damaged/unsupported/animated PNGs are rejected. The module
does not add support for other source image formats.

Animation refusal examines actual PNG chunk types after structural/CRC
validation: any `acTL`, `fcTL` or `fdAT` chunk is rejected, including malformed
declared frame counts of zero/one. Identical words in a static PNG's text-chunk
payload do not imply animation. This does not change the page cache parser.

If the original PNG data URL is at most 1,000,000 characters, those exact original
bytes are returned with `derived=false` and `original_png_exact_v1`. No metadata,
alpha, resolution or pixels are rewritten in this branch.

Otherwise, pixels are composited onto an explicit white matte, converted to RGB,
and encoded as JPEG using a fixed ordered set of maximum-edge/quality tiers:
1800/85, 1800/75, 1400/85, 1400/75, 1100/85, 1100/75, 900/80, 900/65,
700/75 and 512/60. Duplicate effective dimension/quality pairs are skipped.
Subsampling is fixed to 4:4:4, progressive/optimize are disabled, and source
orientation/metadata is not guessed or propagated. Scaling preserves the entire
canvas and aspect ratio to integer-pixel rounding; it never crops or enlarges.

Every encoder output uses a bounded in-memory buffer sized to keep the final
single JPEG data URL within 1,000,000 characters. Oversized candidates are
discarded, never sent to a model. At most ten actual candidates are attempted.
Failure to meet the bound is a fixed refusal, not implicit SDK recompression.
The transform label records the selected fixed maximum edge and quality, for
example `white_matte_full_canvas_fit1800_jpeg_q75_444_v1`; actual dimensions and
both hashes remain explicit. A derived JPEG is lossy and must never be described
as the original PNG's bytes or as Microsoft Office fidelity.

The original PDF and PNG are never mutated. Conversion uses BytesIO only,
starts no process/network/model call, accepts no model-selected options, and
writes no image file. This is bounded host codec processing, not a new OS
isolation boundary or a general image-editing/execution capability.

## Freshness and cancellation responsibilities

This converter intentionally grants no file/image access. Its caller must obtain
the original PNG from the authorized page cache, check that its hash equals
`original_png_sha256`, and keep the current helper's PDF capability/version checks
before and after the single vision request. Repeated cached hashes or a derived
metadata dictionary cannot replace those permission checks.

Stop is checked before decode, after decode/matting, and before/after each bounded
encode attempt. It returns no image after a detected cancellation. A synchronous
codec operation is not interrupted mid-instruction. The asynchronous review
integration sets the event and drains a strongly owned concurrent future plus
executor shutdown before continuing. A cancelled asyncio facade does not prove
that its thread finished. The converter itself adds no model call or retry.
Visual descriptions must remain reference-only observations with visual/goal
verification false. No automatic acceptance or replay is introduced.

## Validation scope

`tests/test_derived_document_preview.py` checks exact small PNGs; a genuinely
high-entropy large PNG; original/sent hashes and byte limits; source preservation;
aspect ratio and visible markers at all four corners; white transparent areas;
bad CRC/compressed pixels; animation; input/output/dependency limits; bounded
tiers; cooperative Stop; and absence of file/network/process operations.

`tools/qa_derived_document_preview.py` generates a fixed noise-picture DOCX,
renders it through actual LibreOffice into a real PDF and cached large PNG,
then sends an explicit derived JPEG through an actual ordinary SDK provider to
a quiet localhost fixture. The receiver checks the single sent JPEG's exact
hash/dimensions, proving no second SDK rewrite occurred. Two explicit wire calls
cover a normal return and Stop, with exactly two requests. PDF, DOCX and cached
original PNG hashes stay unchanged; no PNG/JPEG file is published and no raw
stdout/stderr/base64 payload is retained in reports.

This is a **direct prototype wire**, not the registered visual-review tool. The
localhost response is canned; no cloud model, production document, real user
data, visual-quality benchmark or Microsoft fidelity claim is involved.
Reports: `artifacts/qa-derived-document-preview*` and
`artifacts/derived-document-preview-*.log`. The frozen earlier helper/adapter
reports retain their separate source scope.

The registered route is described in `DOCUMENT_PAGE_VISUAL_REVIEW.md`. It uses
the existing plain provider and private page cache, rechecks the PDF and parent
scope before/after conversion and after HTTP, and validates the actual sent
bytes/hash/decoded dimensions and format. Original `png_sha256` semantics are
preserved; separate `sent_*` fields identify the transport image. Both original
PNG and actual sent-image complete encodings are refused as model observation
text. The model cannot select derivation, quality, dimensions or image data.

`tests/test_derived_document_preview_independent_review.py` found the malformed
APNG declared-frame-count bypass and adds independent metadata/palette/cancel
controls. The converter now rejects structural `acTL`/`fcTL`/`fdAT` chunks after
CRC/structure validation, while ordinary chunk payload text containing those
words remains allowed. Original failing evidence and post-fix source manifests
are retained under `artifacts/derived-preview-*`.
