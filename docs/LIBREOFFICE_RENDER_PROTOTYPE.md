# Isolated LibreOffice renderer prototype

This records the initial isolated prototype stage. The subsequent optional
worker route is described in `LIBREOFFICE_WORKER_INTEGRATION.md`; it does not
enable a new default host engine or change the historical validation scope.

This is a working **unregistered, disabled-by-default prototype**, not a new
production Agent capability. `office_render.py`, its Microsoft COM path, worker
and tool registration are unchanged. No production documents, API keys, Office
processes, profiles, file associations, registry installation or administrator
prompts were used by this validation.

## Verified dependency

The [official current download page](https://www.libreoffice.org/download/)
advertised LibreOffice 26.8.2 Windows x64. The [official mirror metadata](https://download.documentfoundation.org/libreoffice/stable/26.8.2/win/x86_64/LibreOffice_26.8.2_Win_x86-64.msi.mirrorlist)
provided these values, which matched the downloaded file:

- Package: `LibreOffice_26.8.2_Win_x86-64.msi`, 375,934,976 bytes.
- SHA-256: `ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a`.
- Authenticode: Valid, signer The Document Foundation, DE.
- Cached at `E:/Tools/AURA/cache`; runtime at
  `E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime`.

The [official alternative-installation page](https://www.libreoffice.org/download-other/)
also endorses PortableApps packaging, but its download page returned HTTP 403
in this session. We instead read the signed MSI **as data**: read-only Windows
Installer database queries, bounded embedded cabinet extraction, Microsoft
`expand.exe`, and File/Component/Directory table mapping. No MSI action or
installation transaction was executed. `tools/prepare_libreoffice_runtime.py`
repeats that process only for the fixed vendor hash into a new E-drive target.
The resulting manifest records hashes for 19,271 files, 1,526,232,897 runtime
bytes. No Java/JRE or global dependency was installed.

## Execution and cleanup

`desktop_pet.agents.libreoffice_renderer.LibreOfficeRenderer` takes explicit
trusted-host runtime, SDK Python, bundled Poppler and scratch paths. It never
discovers a runtime through PATH or accepts a model-selected executable. The
host must place scratch **outside every delegated model workspace**, as this
QA does at `E:/Tools/AURA/render-scratch`.

Each task creates a new owner-only, protected-DACL directory and its own
`UserInstallation` URL. The fixed helper receives only a bounded host request
and a copied `source.docx`, `source.xlsx` or `source.pptx`. Its LibreOffice
environment points HOME/USERPROFILE/APPDATA/LOCALAPPDATA/TEMP/TMP into that
owned directory. No parent credentials, proxy/profile variables or output
handles are inherited. The private profile sets highest macro security and
disables automatic update checking; the existing `inspect_package` check
rejects macros, external resources and unsupported formula/features first.
No unsupported legacy/macro-enabled format is silently converted.

A fresh helper starts suspended, joins a verified no-breakaway owned Windows
Job, and resumes only after membership confirmation. LibreOffice and Poppler
are descendants of that helper. The Job has an eight-process and 768 MiB Job
memory limit; the operation has a bounded host deadline and explicit Stop.
Stdout/stderr go to NUL, with an explicit inherited-handle allowlist.

On success, failure, timeout or cancellation, only this retained exact Job and
root process are stopped. Publication requires Job active-process count zero
**and** the exact root process signaled. A redundant root-stop request can race
Job termination; its return code never replaces those two observations.
Handles are checked individually on release. Genuine effect-closure evidence
is retained separately from failed handle/directory cleanup; retries never
infer closure from handle zero, PID absence, file absence or a `close()` call.
Unconfirmed cleanup retains owned resources and blocks the next render until
an explicit host reconciliation succeeds. This state is process-local, not a
durable crash-recovery receipt or queue reconciliation mechanism.

The sole recursive cleanup target is a minted `libreoffice-owned-*` directory,
with a checked resolved boundary and no reparse descendants. Windows creates a
read-only Documents known-folder inside the private profile; only those owned
read-only flags are cleared for cleanup. An initial probe correctly withheld
its otherwise rendered result when that cleanup was not yet supported.

## What the successful result means

LibreOffice creates a real PDF. A separate pypdf reader checks page count and
extracts actual page text. Bundled Poppler creates one bounded PNG per page.
The host validates bounded files, signatures/hashes and unchanged source bytes,
then returns PDF/PNG **bytes**, text and narrow rendering evidence. No file is
published to a user workspace by this prototype. Future publication must use
the normal WorkspaceFiles permission/hash/atomic-write boundary.

The following are distinct observations:

- Unit tests cover admission, malformed/dependency errors and fake cleanup
  failure ordering. They are not evidence of a real Office renderer.
- The actual fixed synthetic run produced DOCX 2 pages, XLSX 1 page and PPTX
  2 pages, with all expected text, 5 PNGs, unchanged source hashes and confirmed
  cleanup. An assistant image review found no clipping/overlap on those pages.
- Actual timeout and cancellation probes each started one owned helper, then
  returned the expected stopped status with no pending cleanup and restored
  scratch. They used no model or user document.

Evidence is under `artifacts/qa-libreoffice-render`,
`artifacts/qa-libreoffice-failures`, and `artifacts/libreoffice-*.log`.
`tools/qa_libreoffice_render.py` generates the fixed source fixtures, runs real
rendering, and supports `--failures-only` for the two stopped-run probes.
The final unit suite contains 17 admission/cleanup tests. The scope-rejection
negative evidence is retained separately: a prelaunch cleanup scope failure
must retain its owned directory and block new work, including when explicit
reconciliation repeats that failure.

The runtime and signed vendor MSI occupy about 1.90 GB. Cabinet extraction
intermediates occupy another 1.88 GB. Their exact E-drive boundaries and absence
of reparse points were checked, but automatic approval review rejected their
deletion; these intermediates remain and are not needed to render. Earlier
failed synthetic probes also left small private scratch remnants. Final
successful probes restore their initial scratch baseline; that statement does
not retroactively certify process closure for earlier failed invocations.

## Limits before production integration

This verifies LibreOffice rendering, not Microsoft Office rendering, Excel
recalculation fidelity, pixel-identical Microsoft layout, complex document
compatibility, or a completed arbitrary user goal. Five inspected synthetic
pages are not a general layout-quality result. The earlier COM failure remains
an honest separate environment limitation.

The Job is a process-lifetime/resource boundary, **not an OS filesystem or
network sandbox**. Package inspection and a private profile do not eliminate
native-parser vulnerabilities. Arbitrary hostile documents require an
additional suitable isolation decision before enabling a user-facing tool.
Inputs/output bytes and pages have limits; there is no hard disk quota on this
ordinary host scratch volume. Runtime files originate from a pinned signed
package and the three entry binaries are checked before each render, but all
DLLs, Python/Poppler dependencies and the mutable local manifest are not pinned
against an arbitrary same-user attacker during execution. No such guarantee
is claimed. Private scratch/DACLs do not defend against same-user host process
compromise. No durable recovery, production queue integration, automatic retry,
tool registration or deployment is included here.

Official parameter references: [headless conversion and UserInstallation](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html),
[PDF export filters](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html).
