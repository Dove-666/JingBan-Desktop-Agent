# Owned browser proxy shutdown

Two deterministic old-code failures are retained in
`artifacts/browser-close-negative-evidence.log`: cancelling a CONNECT duplex left
its pipe tasks alive; completing Windows IOCP accept immediately before close
lost the accepted socket and could attach its transport to an already-closed
asyncio Server (`_attach` assertion). These are resource ownership failures,
not evidence of model capability.

The local proxy now owns its listener and the raw Windows Proactor accept future
directly. It harvests a completed accepted socket even if the accept coroutine
was cancelled before resuming. Accepted connections use
`connect_accepted_socket` without an asyncio Server owner, so late continuation
cannot attach to a closed Server. The selector backend owns its readiness
reader explicitly; unsupported Windows event loops fail closed. The IOCP branch
depends on the project's tested CPython Windows Proactor implementation and is
not an unverified generic event-loop adapter.

Handlers are registered synchronously when the protocol receives a connection.
The proxy tracks protocol close acknowledgements, writers and both duplex pipe
tasks. Close stops accepting before draining those owners; a timeout preserves
unresolved ownership and returns a fixed cleanup error. Unexpected handler
failures reach the existing loop exception handler. Production does not replace
the loop handler, patch CPython, or wait an arbitrary delay to conceal a race.

BrowserSession serializes close and joins an in-progress startup so it cannot
return cleanly while that startup later creates a browser. Failed CDP/process
cleanup still closes the proxy. Failed profile cleanup retains its owned
temporary-directory reference and reports an unconfirmed close; explicit later
close may retry. A failed first close is not hidden behind an early closed flag.

Public URL validation, DNS pinning, private-network rejection, method/header
limits, anonymous browser profile and effect uncertainty rules remain in force.

Validation: 7 shutdown ownership tests plus the original 10 policy/session tests
passed with `ResourceWarning` treated as an error. The original set includes four
actual isolated Edge DOM/form/tab/private-network tests. The final log is
`artifacts/browser-close-final-tests.log`, with no ResourceWarning, traceback or
unexpected loop handler output. Tests use owned localhost fixtures, not model
APIs or a user's browser profile.
