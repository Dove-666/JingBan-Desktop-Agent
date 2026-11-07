# Browser shutdown ownership checks, 2026-11-05

An offline SDK regression produced `BROWSER_CLEANUP_UNCONFIRMED` during an actual isolated browser's teardown. The failed full run is retained in `artifacts/user-testing-20261105/sdk-regression-after-contracts.json`; a later standalone pass did not resolve that failure.

## Reproduced cause

The ownership diagnostic ran the existing shutdown tests followed by the actual local-fixture browser tests. It reproduced the failure with no listener, handler, writer or protocol left open, but two **finished** `_pipe` tasks remained in `_relay_tasks`. Waiting another event-loop turn or explicitly retrying cleanup did not remove them. See `browser-proxy-residue-diagnostic/verification.json` in the same artifact directory.

The duplex owner can be cancelled again while joining its cancelled children. Its old `finally` block then skipped ownership bookkeeping. The correction removes only children whose `done()` state is confirmed, including after an interrupted join. Proxy close also releases finished relay tasks after joining them. Pending tasks remain owned and continue to raise the existing unconfirmed-cleanup error; no browser action is repeated.

An independent cancellation probe also found that an unexpected child exception could disappear behind the cancelled owner. Finished relays now preserve unexpected failures through the existing handler-failure flag, including across repeated close calls. Expected policy failures, connection resets and cancellation retain the existing network-stop behavior.

`test_browser_relay_cancellation.py` deterministically covers both repeated cancellation and a relay that cannot yet finish. Its original pre-fix log contains one assertion failure and one cleanup error. Both checks pass after the correction.

## Separate profile sharing issue

A real Windows exclusive file handle on a synthetic, owned browser profile also reproduced a transient cleanup failure. After process exit, profile cleanup now retries sharing/access/busy/nonempty errors for at most 0.75 seconds. A real `TemporaryDirectory` must have the expected profile prefix and temporary parent and a non-reparse path **before the first deletion and before each retry**. A foreign profile implementation receives only the original single cleanup call. Unknown process exit or a persistent lock still fails closed and retains unresolved ownership.

The real Windows handle tests cover transient release, a persistent lock followed by an explicit cleanup-only retry, and unknown process exit. An additional wrong-prefix directory test confirms that even the first deletion is refused. This sharing issue was established independently; it is not asserted as the cause of the original full-run failure.

## Open-source comparison

The investigation checked Playwright's [process cleanup ordering](https://github.com/microsoft/playwright/blob/71350712ca049af48d50f8d23441c6ac82a21da0/packages/utils/processLauncher.ts) and [bounded folder removal retries](https://github.com/microsoft/playwright/blob/71350712ca049af48d50f8d23441c6ac82a21da0/packages/utils/fileUtils.ts), and Puppeteer's [process close lifecycle](https://github.com/puppeteer/puppeteer/blob/d8cc590f12c30025cff5fae09910de465da3523e/packages/browsers/src/launch.ts). These references informed the bounded post-exit profile retry; no external implementation was copied. The relay cancellation fix follows the locally reproduced asyncio ownership failure.

## Validation boundary

The final targeted run passed all 24 browser tests, including actual headless Chromium against constructor-authorized local fixtures. No production browser profile, user window or model API was used. The first full SDK pass (`sdk-regression-after-browser-cleanup.json`) ran 457 tests with 13 explicit native-integration skips. Subsequent independent-review hardening is covered by the separate `sdk-regression-browser-hardened-final.json` report. Earlier failed reports remain unchanged. These checks do not establish general Agent task parity or remote physical-device reliability.
