@file:OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)

package com.aura.companion

import com.aura.companion.data.AppState
import com.aura.companion.data.ConnectionState
import com.aura.companion.data.DeviceInfo
import com.aura.companion.data.Message
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Virtual-time regressions for app switches and asynchronously loaded pairing. */
class ForegroundSyncLoopTest {
    private val device = DeviceInfo("验收电脑", "desktop", "public-ticket")
    private fun connected() = AppState(device = device, connectionState = ConnectionState.CONNECTED)

    @Test fun pairedDevicePublishedAfterTwentyMillisecondsConnectsImmediately() = runTest {
        val state = MutableStateFlow(AppState())
        val connectedAt = mutableListOf<Long>()
        val loop = ForegroundSyncLoop(this, state, { true }, reconnect = {
            connectedAt.add(currentTime)
            state.value = state.value.copy(connectionState = ConnectionState.CONNECTED)
        }, poll = {}, disconnect = {})
        try {
            loop.setForeground(true)
            launch { delay(20); state.value = AppState(device = device) }
            runCurrent()
            assertTrue(connectedAt.isEmpty())
            advanceTimeBy(19); runCurrent()
            assertTrue(connectedAt.isEmpty())
            advanceTimeBy(1); runCurrent()
            assertEquals(listOf(20L), connectedAt)
        } finally { loop.close(); runCurrent() }
    }

    @Test fun fiveSecondAppSwitchReusesConnectionAndImmediatelyPollsOnReturn() = runTest {
        val state = MutableStateFlow(connected())
        val polledAt = mutableListOf<Long>()
        var reconnects = 0
        var disconnects = 0
        val loop = ForegroundSyncLoop(this, state, { true }, reconnect = { reconnects++ },
            poll = { polledAt.add(currentTime) }, disconnect = { disconnects++ })
        try {
            loop.setForeground(true); runCurrent()
            assertEquals(listOf(0L), polledAt)
            loop.setForeground(false); runCurrent()
            advanceTimeBy(5_000); runCurrent()
            assertEquals(listOf(0L), polledAt)
            loop.setForeground(true); runCurrent()
            assertEquals(listOf(0L, 5_000L), polledAt)
            assertEquals(0, reconnects)
            assertEquals(0, disconnects)
            loop.setForeground(true); runCurrent()
            assertEquals(2, polledAt.size)
        } finally { loop.close(); runCurrent() }
    }

    @Test fun shortBackgroundVisitDoesNotCancelOrRecreateAnInFlightConnection() = runTest {
        val state = MutableStateFlow(AppState(device = device))
        val connectionGate = CompletableDeferred<Unit>()
        var reconnects = 0
        var cancellations = 0
        var disconnects = 0
        val loop = ForegroundSyncLoop(this, state, { true }, reconnect = {
            reconnects++
            try {
                connectionGate.await()
                state.value = state.value.copy(connectionState = ConnectionState.CONNECTED)
            } catch (cancelled: CancellationException) { cancellations++; throw cancelled }
        }, poll = {}, disconnect = { disconnects++ })
        try {
            loop.setForeground(true); runCurrent()
            assertEquals(1, reconnects)
            loop.setForeground(false); runCurrent()
            advanceTimeBy(5_000); runCurrent()
            loop.setForeground(true); runCurrent()
            assertEquals(1, reconnects)
            assertEquals(0, cancellations)
            assertEquals(0, disconnects)
            connectionGate.complete(Unit); runCurrent()
            assertEquals(ConnectionState.CONNECTED, state.value.connectionState)
            assertEquals(1, reconnects)
        } finally { loop.close(); runCurrent() }
    }

    @Test fun fortyFiveSecondsInBackgroundDisconnectsOnceWithoutPolling() = runTest {
        val state = MutableStateFlow(connected())
        val polledAt = mutableListOf<Long>()
        val disconnectedAt = mutableListOf<Long>()
        val loop = ForegroundSyncLoop(this, state, { true }, reconnect = {},
            poll = { polledAt.add(currentTime) }, disconnect = {
                disconnectedAt.add(currentTime)
                state.value = state.value.copy(connectionState = ConnectionState.DISCONNECTED)
            })
        try {
            loop.setForeground(true); runCurrent()
            loop.setForeground(false); runCurrent()
            advanceTimeBy(44_999); runCurrent()
            assertTrue(disconnectedAt.isEmpty())
            assertEquals(listOf(0L), polledAt)
            advanceTimeBy(1); runCurrent()
            assertEquals(listOf(45_000L), disconnectedAt)
            advanceTimeBy(60_000); runCurrent()
            assertEquals(listOf(45_000L), disconnectedAt)
            assertEquals(listOf(0L), polledAt)
        } finally { loop.close(); runCurrent() }
    }

    @Test fun idlePollingUsesTenSecondsAndPendingRecordsUseTwoSeconds() = runTest {
        val state = MutableStateFlow(connected())
        val polledAt = mutableListOf<Long>()
        val accepted = Message("legacy-user", "conversation", "user", "电脑已接受的问题", "2026-10-31T00:00:00Z",
            source = "user", localState = "sent")
        val loop = ForegroundSyncLoop(this, state, { true }, reconnect = {}, poll = {
            polledAt.add(currentTime)
            if (polledAt.size == 2) state.value = state.value.copy(historyPendingCount = 1)
            if (polledAt.size == 4) state.value = state.value.copy(historyPendingCount = 0, messages = listOf(accepted))
            if (polledAt.size == 5) state.value = state.value.copy(messages = listOf(
                accepted.copy(localState = null, requestStatus = "pending")))
            if (polledAt.size == 6) state.value = state.value.copy(messages = listOf(
                accepted.copy(localState = null, requestStatus = "complete"),
                // A completed inbox upload is not an outstanding model reply.
                accepted.copy(id = "inbox-file", source = "image_inbox")))
        }, disconnect = {})
        try {
            loop.setForeground(true); runCurrent()
            advanceTimeBy(9_999); runCurrent()
            assertEquals(listOf(0L), polledAt)
            advanceTimeBy(1); runCurrent()
            assertEquals(listOf(0L, 10_000L), polledAt)
            advanceTimeBy(1_999); runCurrent()
            assertEquals(2, polledAt.size)
            advanceTimeBy(1); runCurrent()
            advanceTimeBy(2_000); runCurrent()
            assertEquals(listOf(0L, 10_000L, 12_000L, 14_000L), polledAt)
            // Acceptance can precede the first canonical user-message pull.
            advanceTimeBy(1_999); runCurrent()
            assertEquals(4, polledAt.size)
            advanceTimeBy(1); runCurrent()
            assertEquals(16_000L, polledAt.last())
            // A canonical pending request also keeps the fast reply cadence.
            advanceTimeBy(1_999); runCurrent()
            assertEquals(5, polledAt.size)
            advanceTimeBy(1); runCurrent()
            assertEquals(18_000L, polledAt.last())
            advanceTimeBy(9_999); runCurrent()
            assertEquals(6, polledAt.size)
            advanceTimeBy(1); runCurrent()
            assertEquals(28_000L, polledAt.last())
        } finally { loop.close(); runCurrent() }
    }

    @Test fun revokedAuthorizationNeverReconnectsOrPollsEvenAfterReturningToForeground() = runTest {
        val state = MutableStateFlow(connected().copy(connectionState = ConnectionState.UNAUTHORIZED))
        var reconnects = 0
        var polls = 0
        var disconnects = 0
        val loop = ForegroundSyncLoop(this, state, { true }, reconnect = { reconnects++ },
            poll = { polls++ }, disconnect = { disconnects++ })
        try {
            loop.setForeground(true); runCurrent()
            advanceTimeBy(60_000); runCurrent()
            loop.setForeground(false); runCurrent()
            advanceTimeBy(5_000); runCurrent()
            loop.setForeground(true); runCurrent()
            advanceTimeBy(20_000); runCurrent()
            assertEquals(0, reconnects)
            assertEquals(0, polls)
            assertEquals(0, disconnects)
            assertEquals(ConnectionState.UNAUTHORIZED, state.value.connectionState)
        } finally { loop.close(); runCurrent() }
    }
}
