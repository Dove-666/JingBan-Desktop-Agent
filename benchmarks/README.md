# Agent task evidence

`agent_tasks/catalog.json` keeps task definitions and independent checks separate
from model answers. `tools/run_agent_benchmarks.py` defaults to offline catalog
and oracle validation. Only `--provider configured` invokes the configured model;
it uses synthetic workspaces and does not enable desktop input.

Real cases now record the declared host/worker source files and dependency-pin
hashes before and after execution. A changed snapshot invalidates that case,
even when its independent output check passed. This is an on-disk source check,
not attestation of loaded binaries, every dependency, or a commercial product
baseline. Older reports do not gain this guarantee retroactively.

For future runs, `catalog_scope` records the complete catalog's task-type count
alongside the selected subset. `catalog_coverage_pass_rate` remains the legacy
selected-task metric and is now explicitly labelled with that scope;
`full_catalog_verified_task_coverage` keeps every catalog task type in the
denominator. A selected case passing three repetitions verifies one task type,
not three capabilities or the unselected tasks. Offline oracle checks still
produce no real-model coverage.

The runner writes one atomic report after each case. It keeps failed attempts,
marks unimplemented tasks explicitly, and excludes offline checks and negative
guards from model success rates. Reports never store model replies, reasoning,
tool arguments, credentials or real user files. Fixed diagnostic codes identify
observed execution boundaries; an unknown SDK failure does not identify its
root cause.

The retained v2 three-repeat report in
`artifacts/agent-parity-20261102/real-benchmark-v2-three-repeats.json` passed 38 of
39 attempted real cases across 13 implemented task types. Twelve other types
remain unimplemented. One JavaScript case wrote a correct implementation but
then failed during the execution chain; it remains a failed case. GPT Agent,
Claude Code and Workbuddy have not been run on the same task set here, so these
results establish no parity with them.

Catalog v3 appends one explicit CSV-to-XLSX formula subset without changing the
29 v2 task/guard definitions. Its separate stdlib oracle reads actual formula
nodes, Decimal caches, literal cells and numeric styles; it rejects incorrect
caches, removed formulas, changed styles, formula injection and external links.
This is neither general Excel editing nor verification of Excel recalculation.
New v3 real-model runs remain separate from the retained v2 results.

The frozen v3 formula/JavaScript subset ran three repetitions of each of three
tasks: 8/9 completed, with 3/3 formula-budget passes. One interval task produced
the independently correct artifact but exhausted the 180-second QA limit after
an unknown goal-verifier result; it remains an error. The 171 copied source
files and every per-case source snapshot stayed unchanged, and production
settings stayed unchanged. This snapshot predates the final queue protocol
repair; these normal-task results do not verify queue cancellation or recovery.
Report: `artifacts/agent-parity-20261102/real-v3-frozen-formula-js-three-repeats.json`.

A subsequent, separate frozen-source interval-only run passed 3/3 in 70.453,
62.968 and 59.000 seconds. Each had one strict, accepted satisfied receipt and
now records the fixed `GOAL_JUDGE_SATISFIED` diagnostic. All 171 copied sources
and per-case source snapshots remained unchanged; production settings remained
unchanged. This adds observability and three new successful attempts; it does
not explain or erase the earlier timeout, establish a speed improvement, or
change the success rate of either historical report. No automatic retry or
acceptance rule was relaxed.
Report: `artifacts/agent-parity-20261102/real-judge-diagnostics-interval-three-repeats.json`.

Separate controlled real-model browser runs passed the two owned research/form
tasks after the plain-HTTP repair. These are not substituted for the catalog's
dated primary-source research, unrestricted browser tasks, or its unimplemented
types. See `docs/OWNED_BROWSER_QA.md` for fixture authority and source scope.

The actual C-drive read-only investigation produced an independently checked
capacity/category report and accurately retained partial/unscanned states. Two
original reports remain failed because the QA tool allowlist omitted legitimate
read-only metadata tools. A separate post-run review checked the second run's
captured tool names against its frozen production catalog and accepted the
existing observations; it did not rerun a model or rewrite the original result.
Evidence: `artifacts/windows-storage-independent-review.json`. This verifies
inspection and a report, not deletion, full-drive scanning or reclaimable space.

The later frozen r17 full-catalog run attempted 42 real cases across 14
implemented task types: 41 passed. Twelve task types remained unimplemented;
13 of the complete catalog's 26 types passed all three repetitions. The interval
failure had correct independently checked files, but its three verifier calls
ended with `GOAL_JUDGE_FINISH_LENGTH`. It remains a failed, needs-user case.
That copy omitted the browser DOM asset and makes no browser-capability claim;
the declared 177 sources and production settings stayed unchanged. Future
snapshots include that asset as well.
Report: `artifacts/agent-parity-20261102/real-r17-full-three-repeats.json`.

The verifier's single-request response budget was subsequently increased from
2048 to 8192 tokens; truncated receipts remain invalid and no automatic retry
was introduced. A separate frozen 181-source interval subset then passed 3/3
in 59.281, 67.359 and 59.234 seconds, with strict satisfied receipts and unchanged
sources/settings. This confirms three new attempts after the capacity change,
without rewriting the earlier full run or establishing product parity.
Report: `artifacts/agent-parity-20261102/real-judge8192-interval-three-repeats.json`.
