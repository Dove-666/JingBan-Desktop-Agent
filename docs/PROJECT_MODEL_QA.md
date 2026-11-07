# Controlled offline Python project repair QA

This runner is independent QA, with no production source changes. The real
model mode is **not enabled or run in this phase**. The host project-execution
opt-in remains missing/off. Native desktop metadata isolation is under review;
the existing MXC boundary is not represented here as a general model-safe
container, terminal, repository agent, or Claude Code equivalent.

Default invocation prints `not_run`. It does not read credentials or the
execution opt-in, create a task directory/report, start a model, or execute a
candidate. Explicit modes are separate:

```powershell
.venv/Scripts/python.exe tools/qa_project_model.py
.venv/Scripts/python.exe -m unittest tests.test_qa_project_model -v
.venv/Scripts/python.exe tools/qa_project_model.py --provider fixed-selftest --allow-fixed-native --output artifacts/project-model-qa/fixed-selftest-new.json
```

The fixed selftest is an explicit QA-only `live_enabled=True` call over trusted
code contained in the runner. It does not create or alter the machine opt-in
and is not model-generated-code evidence. Existing report paths are refused
so failures are retained. The initial report is
`artifacts/project-model-qa/fixed-selftest-initial.json`; its source hashes and
result remain historical evidence when later code changes.

## Task and independent acceptance

The fixture seeds three owned files: buggy `solution.py`, `SPEC.md`, and an
`input.csv` containing Chinese/emoji categories and precise amounts. The model
must repair the actual saved `group_totals(rows)` function. Paid amounts add,
refunds subtract, pending rows create no group, zero is `0.00`, and negative
net totals are valid. Categories stay unchanged and use Python Unicode
codepoint sort order. Input amounts are nonnegative strings with exactly two
fractional digits, up to 30 integer digits, with at most 100 rows per case.

Both the trusted host Decimal reference and known fixed candidate use an
explicit local precision of 80; the default Decimal precision of 28 cannot
correctly represent the declared largest inputs. Cases cover amounts beyond
`2**53`, thirty integer digits, refunds, `0.10 + 0.20`, empty input, pending-only
categories, zero, negative totals, distinct composed/decomposed Unicode, and
random input permutations.

The repair must be saved in the workspace before `compute_project_artifact`
gets current SHA-256 read-only snapshots of the three files. That tool must
publish `fixed.zip`, containing exactly the current `solution.py` bytes and
strict `results.json` for the visible CSV. No extra workspace files, modified
SPEC/input, duplicate ZIP names, traversal names, symlink entries, encrypted or
oversized members, stale packaged source, duplicate JSON fields, or numeric
totals are accepted.

Assistant summaries cannot supply publication evidence. The oracle pairs an
actual `compute_project_artifact` tool result with its call, checks the three
current source hashes, and matches the published artifact's hash/size to the
actual ZIP. Process exit, stdout, or a publication receipt does not verify the
functional task by itself.

After the parent worker is confirmed closed, a separate actual MXC execution
compiles the same current source bytes and calls its function on 16 fresh
hidden cases. The host-created wrapper stages the candidate read-only, sends
the hidden inputs and nonce, and receives actual outputs plus source hash and
input-immutability observations. **Expected answers and the host reference
algorithm are not sent into MXC.** Trusted host Decimal calculations compare
all returned structures and strings. Candidate `exec`/`compile` occurs only
inside MXC; the host never imports, runs, or compiles model Python.

The hidden checks are finite functional evidence, not a formal proof against
arbitrary adversarial Python. MXC uses a shared Windows kernel, read access to
the pinned Python/System32 runtime and registry, soft scratch monitoring and
bounded native process resources. The default native step budget is 30 seconds,
with stdlib only and no network/package install. The configured project route
must remain disabled until the separate native isolation review is complete.

Unknown native ownership or cleanup retains exact owners and scratch, blocks
new QA work in that process and fails acceptance. There is no automatic replay
or claim of durable restart recovery. Actual native tree/resource closure and
input/artifact facts remain distinct from task success.

## Future configured mode

Only after independent review and an explicit host enable can the runner's
`--provider configured` mode proceed. Missing/off is checked before credentials
are read or any workspace is created. It uses actual `AgentService` with
`kind=tools`, the real SDK and registered compute tool; GUI/application tools
are not registered for that request kind. API loading uses the existing
configured loader behind a getter-only settings/vault facade. No profile or
enable-file save is performed.

A QA-only worker entry observes fixed native cleanup metadata and leaves the
production policy, configuration, scope guards, execution and goal checks
intact. It stores no provider body, hidden reasoning or native transcript.
Reports separate the functional oracle, current execution-policy identity,
captured public API-scope hash, complete INI byte stability and source fences.
A changed global INI file must not be attributed to a specific setting or
writer without evidence. Overall QA is not accepted on an audit failure even
when the finite functional task facts pass.

Any future real-model task is separately authorized and run once over frozen
source. This phase's fixed native test and pure oracle mutants are never
substituted for that real-model result.
