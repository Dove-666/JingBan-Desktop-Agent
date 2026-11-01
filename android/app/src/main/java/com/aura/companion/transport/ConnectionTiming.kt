package com.aura.companion.transport

/** Fixed numeric phases only: a timing sample cannot carry peer data or secrets. */
internal enum class ConnectionPhase(val code: Int) {
    START(0), LOCK_ACQUIRED(1), CONTEXT_READY(2), TICKET_PARSED(3), PREVIOUS_CLOSED(4),
    IDENTITY_LOADED(5), ENDPOINT_BOUND(6), HANDSHAKE_COMPLETE(7), AUTHENTICATED(8),
    READY(9), REUSED(10), FAILED(11), CANCELLED(12), FINISHED(13),
}

internal class ConnectionTiming(
    private val clock: () -> Long = System::nanoTime,
    private val emit: (Int, Long) -> Unit,
) {
    private val started = clock()
    fun mark(phase: ConnectionPhase) {
        emit(phase.code, ((clock() - started) / 1_000_000).coerceAtLeast(0))
    }
}

internal data class NativeConnectionMetrics(
    val connectAttempts: Long,
    val endpointBinds: Long,
    val reusedConnections: Long,
)
