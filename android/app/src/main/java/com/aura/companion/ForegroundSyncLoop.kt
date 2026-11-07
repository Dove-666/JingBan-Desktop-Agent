package com.aura.companion

import com.aura.companion.data.AppState
import com.aura.companion.data.ConnectionState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** Background polling requires an explicit, running task-service lease. */
internal class ForegroundSyncLoop(
    private val scope: CoroutineScope,
    private val state: StateFlow<AppState>,
    private val autoConnect: () -> Boolean,
    private val reconnect: suspend () -> Unit,
    private val poll: suspend () -> Unit,
    private val disconnect: suspend () -> Unit,
    private val backgroundGraceMillis: Long = 45_000,
) {
    private var foreground = false
    private var backgroundTaskActive = false
    private var loop: Job? = null
    private var idleClose: Job? = null
    private val wake = Channel<Unit>(Channel.CONFLATED)
    private var retryDelay = 2_000L

    fun setForeground(value: Boolean) {
        if (foreground == value) return
        foreground = value
        reconcile()
    }

    fun setBackgroundTaskActive(value: Boolean) {
        if (backgroundTaskActive == value) return
        backgroundTaskActive = value
        reconcile()
    }

    private fun active(): Boolean = foreground || backgroundTaskActive

    private fun reconcile() {
        idleClose?.cancel()
        if (!active()) {
            idleClose = scope.launch {
                delay(backgroundGraceMillis)
                if (!active()) {
                    val previous = loop
                    previous?.cancelAndJoin()
                    if (loop === previous) loop = null
                    if (!active()) disconnect()
                }
            }
            return
        }
        retryDelay = 2_000
        wake.trySend(Unit)
        if (loop?.isActive != true) loop = scope.launch { runLoop() }
    }

    private suspend fun runLoop() {
        // Local initialization publishes the paired device asynchronously. Wait
        // for that event instead of adding a fixed two-second startup delay.
        state.first { it.device != null }
        while (scope.isActive && active()) {
            while (wake.tryReceive().isSuccess) { /* Coalesce foreground events. */ }
            var wait = 10_000L
            if (autoConnect() && state.value.device != null) {
                try {
                    when (state.value.connectionState) {
                        ConnectionState.CONNECTED -> {
                            poll()
                            retryDelay = 2_000
                            wait = if (hasPendingWork(state.value)) 2_000 else 10_000
                        }
                        ConnectionState.DISCONNECTED, ConnectionState.ERROR -> {
                            reconnect()
                            if (state.value.connectionState == ConnectionState.CONNECTED) {
                                retryDelay = 2_000
                                wait = if (hasPendingWork(state.value)) 2_000 else 10_000
                            } else {
                                wait = retryDelay
                                retryDelay = (retryDelay * 2).coerceAtMost(30_000)
                            }
                        }
                        ConnectionState.CONNECTING -> wait = 500
                        ConnectionState.UNAUTHORIZED -> Unit
                    }
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    wait = retryDelay
                    retryDelay = (retryDelay * 2).coerceAtMost(30_000)
                }
            }
            if (!active()) break
            withTimeoutOrNull(wait) { wake.receive() }
        }
    }

    fun close() {
        foreground = false
        backgroundTaskActive = false
        idleClose?.cancel()
        loop?.cancel()
        wake.close()
    }

    private fun hasPendingWork(value: AppState): Boolean = value.desktopAgentBusy || value.directChatBusy || value.historyPendingCount > 0 ||
        value.outbox.any { it.status in setOf("queued", "sending", "unknown", "pending") } ||
        value.messages.any { it.role == "user" && (it.requestStatus == "pending" ||
            it.source == "user" && it.localState == "sent") }
}
