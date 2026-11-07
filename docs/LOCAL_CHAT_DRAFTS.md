# Local desktop text drafts

Desktop chat views share one `LocalDraftOwner` on
`controller._local_draft_owner`. `SharedDraftState` relays notifications to Qt;
the native and WebEngine views use the same in-memory text authority. Hidden
native editors do not write over the web editor. No new UI text or controls are
added, and the chat styles are unchanged.

## Storage and identity

The owner writes `<conversation archive filename>.drafts.sqlite3` alongside the
archive. This is a separate database: drafts never become chat messages, remote
sync payloads, model context, API settings or automatically sent requests. It
stores text, exact conversation identity, an archive-generation anchor, an
independent random editor epoch, a monotonic revision and a timestamp. Staged
images, attachment references, provider data and credentials are not part of
the schema. User-entered text is preserved exactly, including whitespace.

Existing archive identifiers are exact strings (including legacy identifiers);
this confers no queued-Agent authority. New unarchived-session recovery is
stricter: the host validates one opaque candidate, its source epoch/revision and
the original blank session epoch/revision. It adopts the original canonical CID
only while the current view remains a new blank session. It does not open an
archived conversation, create a message, or send anything. New input, even if
subsequently erased, wins over a pending recovery offer. An archive/tombstone
change revokes the offer.

The identity reader completes the archive's existing idempotent initialization
in its background thread, then uses a query-only snapshot of existence, the
first message ID and conversation tombstones. It fetches no titles, message
content, summaries or images. Draft storage introduces no archive tables.

Runtime empty-to-first-message binding is explicit. A live owner that has
successfully saved an empty archive anchor, or activated an already validated
unarchived draft, can bind the first actual message after a host archive event.
That first message may be a user send or a proactive screen/camera comment.
Startup never accepts a persisted anchor that disagrees with the archive.

## Writer and editor concurrency

Keystrokes update memory immediately. A single-shot 350 ms timer queues writes
to one bounded daemon writer; no disk/network work happens in a keypress
handler. Hide/close queues a flush. At application shutdown, the controller's
`TaskRunner` tracks real accepted function completion, independently of queued
GUI callbacks. Only after those archive functions finish does the finalizer
refresh the first-message anchor and finish the draft writer.

SQLite writes compare epoch/revision, so old jobs cannot replace a new edit.
Accepted-send cleanup compares the exact original owner revision; a rejected
send leaves it alone. The web bridge additionally carries:

- `draftEpoch`: the conversation's editor generation.
- `draftBaseVersion`: the actual owner version underlying an editing chain.
- `draftSequence`: monotonic sequence within that pane and epoch.
- `draftDocument`: a host nonce replaced when that web document reloads.

One pane can send a rapid chain without waiting for per-key state snapshots.
Its original base remains valid only while the owner version is exactly its
last applied version. A change from another pane breaks the old chain. Rejected
sequences are acknowledged with a fresh snapshot, allowing convergence before
new genuine input. Old documents, old CIDs, old epochs and out-of-order events
cannot overwrite current text. Normal typing does not publish global state or
rebuild the hidden native document for every keypress.

The web editor remeasures its hidden mirror when the reading font changes or
finishes loading, as well as when text or available width changes. Existing
text and the live editor node remain intact. The follow-up WebEngine batch
passed 10 checks for font changes, bounded long-text scrolling, ordinary typing
and incremental replies; its source fence is
`artifacts/composer-font-reflow-final-source.json`. Two separately rendered
24px-to-12px views retained the same text/node and removed the extra height.
The original font-change failure is retained rather than folded into the old
draft-feature batch.

## Clear/delete and failure handling

Generic `conversation_invalidated` events do not delete drafts: those events
also describe Agent-context cancellation. Explicit user clear/delete instead
uses three host methods:

1. `begin_invalidation(cid, kind=...)` replaces the memory epoch and queues a
   durable empty fence. The ticket captures the complete before-archive identity.
2. The real background archive function calls
   `wait_invalidation(ticket, timeout=1)` before its mutator. A failed barrier
   prevents the archive mutation.
3. The background mutator calls `finish_invalidation(ticket, success=...)` at
   the actual mutation outcome, even if the app is closing and GUI callbacks
   will be discarded. A later readback failure cannot undo a confirmed clear.

Failure rollback performs a fresh background identity check. It restores the
previous text only when the complete identity still matches and no newer edit
exists. A confirmed commit followed by an error, a changed/deleted identity or
an unreadable identity leaves the durable fence; it does not guess that the
mutation failed. The prior text remains private memory for that owner rather
than reappearing as a draft. No automatic operation replay is introduced.

The visible archived transcript has its own reconciliation: a mutator exception
does not prove rollback. The background function re-reads the actual conversation
and messages. The GUI installs that readback exactly; if readback fails, it shows
an empty unavailable view and retains a nonempty error. It never reinstalls the
old optimistic transcript or cached retry rows. A real pre-commit failure can
therefore show the still-existing records, while an applied commit cannot
resurrect deleted rows. Matching history fallbacks are replaced, and stale
same-conversation loads are invalidated without cancelling a different
conversation's pending load. Confirmed clears retain this same stale-load fence.

An unreadable delete result retains the conversation's admission guard. If the
user later explicitly reopens or reloads that conversation and the actual
archive read succeeds, a host-only token for that exact uncertain delete can
release the guard for new messages. An older history read, a generic snapshot,
a failed read or a new delete cannot release it. This never retries the delete
or recreates a tombstoned conversation identifier.

Storage failures preserve current memory. A timeout joining the writer reports
false rather than claiming data was saved. The feature cannot guarantee the
last debounce interval after an abrupt process kill, an unavailable disk or a
write that never completed. It makes no secure-erasure or full OS-isolation claim.

## Bounds and verification

Limits: 256 KiB UTF-8 per saved draft, 4 MiB total live text, 256 live drafts,
16,384 metadata/fence records, 32 MiB main side-database size, 32 queued writer
jobs, and 250 ms SQLite contention waits. Oversized existing files are refused
before SQLite parses them. Unexpected schemas, links and stale versions fail
closed. These are local storage bounds, not a general disk quota guarantee.

The tests use temporary archives/settings, controlled readers and synthetic
text. They cover storage CAS, two connections, writer shutdown, late load,
first-message binding, proactive first comments, explicit clear/delete,
post-commit errors, rollback after window shutdown, shared panes and stale web
events. Existing native/history/recovery and WebEngine typing tests are run
separately from the feature's tests.

`tools/qa_local_chat_drafts.py` is offline by default. `--run` opens only
controlled QA windows and verifies real WebEngine restart, native/web switching,
an actual first archive write held across shutdown, and the next unsent draft
visible in a newly opened page. It saves synthetic-window screenshots and
fixed checks/hashes. It does not read the running user's draft/profile or call
a model. The production app is not restarted by this QA tool.
