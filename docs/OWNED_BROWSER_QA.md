# Owned browser research and form QA

`tools/qa_owned_browser.py` is an independent acceptance runner. It does not edit
catalog v1/v2/v3 or change any of their unimplemented general task definitions.
The two positive cases are deliberately narrow: read two synthetic source pages
and return their exact facts/URLs; fill one owned form and observe its independent
receipt. This is not a claim to support arbitrary websites or authenticated
browser workflows.

Default execution is offline and never loads keys, starts HTTP fixtures or
launches a browser:

```powershell
.venv/Scripts/python.exe tools/qa_owned_browser.py --provider offline
.venv/Scripts/python.exe tools/qa_owned_browser.py --provider synthetic --output artifacts/owned-browser-qa-synthetic.json
```

`--provider configured` is explicit opt-in to the existing configured model,
using SettingsManager read-only. It must be run separately from the synthetic
control-chain suite; synthetic results never enter a real-model success rate.
The current implementation phase has not run that provider. Each completed case
prints only fixed status and saves the partial report atomically.

## Scope and oracle

Each case gets a new nonce, loopback origin, temporary workspace and CID. The
explicit QA worker entry installs a constructor-only BrowserSession adapter
whose policy permits just `http://127.0.0.1:<port>/<nonce>/...`. Other ports,
non-nonce paths, public origins, credentials and encoded traversal are rejected.
The production PublicBrowserPolicy still rejects localhost. Nothing grants access
to user profiles, the physical desktop, local files outside the workspace or
arbitrary private addresses.

The actual isolated SDK planner uses the existing DOM tools and a separate
independent goal judge. Synthetic planner steps extract facts and opaque refs
from actual observations; they do not replace the browser tools. The server is
independent of planner output. Its oracle checks real page responses and HTML
SHA-256, exact source URLs and JSON output, stored POST fields, request count,
receipt fetched after submission, fresh observed receipt URL/token and exact
host worker closure. A correct-looking answer or echoed form values alone fails.

Reports omit replies, raw model/judge bodies, hidden reasoning and credentials.
They retain safe counts/booleans, owned fixture URLs/document digests, generated
adapter digest and source snapshots before/after. A changed source snapshot or
changed existing settings invalidates the case. These are on-disk snapshots,
not loaded-binary attestations.

## Preserved failure: zero response bytes can replay POST below CDP

The original `pre_headers` fault stores a non-idempotent POST and closes the
server connection without any response bytes. It is retained unchanged.
Both a single-click planner and a deliberate repeated-ref planner produce:

| Observation | Value |
| --- | --- |
| DOM click evaluations | 1 |
| CDP Network POST events | 1 |
| Fetch POST events | 1 |
| Independent server POST requests | 3 |
| Non-idempotent server effects | 3 |
| Agent outcome | needs_user, not accepted |

Evidence is in `artifacts/owned-browser-preheader-loss-attribution.json` and the
original failed `owned-browser-qa-tests.log`. The original single-POST assertion
failed; its attribution and `owned-browser-known-preheader-failure.json` remain
intact. The production HTTP fix has separate evidence rather than overwriting
that failure. The measurements place the replay below one DOM/CDP request; they do not
attest a particular installed Chromium source revision. Chromium's
[HttpNetworkTransaction implementation](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/net/http/http_network_transaction.cc)
contains connection-error retry logic before final response headers, consistent
with this boundary. Preventing another tool click does not prove network or
business-effect exactly-once behavior.

Two distinct controls retain that failure rather than replacing it:

- `started_headers` sends final headers then loses its body. It produces one
  request, remains needs_user and refuses a second input with the consumed ref.
- An application-owned hidden idempotency token stores one logical effect even
  when multiple requests arrive. The report separately counts requests/effects;
  it does not claim the browser prevented retransmission or that the agent knows
  the final result.

## QA-only HTTP mitigation candidate

`tools/qa_http_response.py` remains a separate QA-only candidate. In the explicit
HTTP-only QA adapter it buffers interim/final response headers (32 KiB total,
two-second deadline, at most eight interim responses). It forwards nothing until
valid final headers exist. Empty/partial/interim-only failure gets a fixed 502,
which preserves an unknown effect instead of a silent disconnect. The original
body pipe retains body ownership and transfer limits. Cancellation propagates;
the helper never retries or claims to undo the remote effect.

`owned-browser-http-candidate-preproduction-proof.json` preserves its original
source snapshot. `owned-browser-http-candidate-proof.json` demonstrates separate pre-header EOF,
partial-header EOF and 103-only EOF cases: one DOM/CDP/Fetch request and one
server effect, with explicit gateway failure and overall needs_user. Normal
research/form tasks and denied cross-port/non-nonce redirects are checked both
with and without this candidate. Pure tests cover buffering 100/103 until final
headers, missing final headers, bounds, deadline, cancellation and forwarding
the remaining body once.

The candidate cannot inspect CONNECT/TLS and does not establish general HTTPS
or application exactly-once guarantees. Unsupported/malformed response framing
fails closed. The QA runner defaults to the current production proxy, including
for configured-model runs; the candidate remains an explicit separate path.

## Production plain-HTTP response boundary

`agent_backend/nanobot/http_response.py` is now called only by the proxy's
plain-HTTP branch with a fifteen-second header budget. CONNECT retains its
previous encrypted duplex path. The helper buffers complete final headers with
the same 32 KiB/interim bounds, sends a fixed 502 on pre-final failure and hands
the remaining body to the original bounded pipe. It does not retry requests,
replay inputs or clear unknown-effect ledgers.

Response state is marked before forwarding final headers or a 502. If writing
or relaying the body subsequently times out or reaches its transfer limit, the
outer proxy closes its owned streams and never appends a second 403 into that
response body. Cancellation propagates unchanged. Pure tests check that outer
boundary and the unaffected CONNECT path.

`owned-browser-production-stable-window.json` records one source-stable window:
two completed positive research/form tasks and three production-proxy faults
(pre-header EOF, partial-header EOF, interim-only EOF). Each fault produces one
DOM/CDP/Fetch request and one server request/effect, with overall needs_user.
Faults remain guard results, not completed form tasks. Source snapshots before
and after every case and the whole window match. The old three-effect failure
and preproduction idempotent request/effect distinction remain separately saved.

This is a measured plain-HTTP improvement, not general exactly-once delivery.
The proxy cannot inspect encrypted CONNECT responses, downstream connections
can still fail, and application idempotency/reconciliation remains necessary.

An independent framing review subsequently found that a response with both
Transfer-Encoding and Content-Length, or a transfer coding on an informational
or 204 response, could pass through unchanged. The production relay now rejects
those combinations with the same fixed 502 before forwarding upstream headers.
The original failing checks remain in
`artifacts/http-response-framing-review-before-fix.log`. Four independent checks
and the existing header/ownership suites passed together (26 checks), including
valid chunked-body forwarding and cancellation during header drain. This later
source revision is covered by
`artifacts/http-response-framing-final-validation.json`; it does not rewrite the
earlier source-stable window or extend the claim to encrypted traffic.

A further independent check rejects Content-Length on informational and 204
responses, including a zero length, as specified by
[RFC 9110 section 8.6](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.6).
Its two failing subcases remain in the negative evidence. The correct isolated
SDK runtime passed all 27 related checks with no skips; the earlier host-runtime
run with missing websocket support remains a separate failed run. Evidence:
`artifacts/http-response-nobody-length-validation.json`.

`artifacts/agent-parity-20261102/real-owned-browser-r17-two-cases.json` records
two additional configured-model runs from a separate frozen source copy. Both
research with independently checked facts/source URLs and a submitted form with
independent server fields/receipt passed. The production HTTP proxy was used;
only the exact random loopback fixture origin was admitted by the QA adapter.
These two controlled tasks do not establish unrestricted web-task capability.

## Tests

```powershell
.venv/Scripts/python.exe -m unittest tests.test_qa_http_response tests.test_owned_browser_qa -v
agent_backend/nanobot/.venv/Scripts/python.exe -m unittest tests.test_owned_browser_qa.OwnedBrowserRedirectTests -v
```

The second command runs actual DOM redirect checks in the isolated SDK runtime
when the host runtime lacks websockets. A host-side skip is not counted as an
actual browser pass. The historical pre-header failure remains separate from
the production repair, positive task results and guard results.
