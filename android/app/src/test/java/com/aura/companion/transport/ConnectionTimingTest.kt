package com.aura.companion.transport

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectionTimingTest {
    @Test fun timingReportsElapsedMonotonicClockMillisecondsAndFixedNumericPhases() {
        var clock = 2_000_000_000L
        val samples = mutableListOf<Pair<Int, Long>>()
        val timing = ConnectionTiming(clock = { clock }) { phase, elapsed -> samples += phase to elapsed }
        timing.mark(ConnectionPhase.START)
        clock += 7_900_000
        timing.mark(ConnectionPhase.ENDPOINT_BOUND)
        clock += 23_200_000
        timing.mark(ConnectionPhase.READY)
        assertEquals(listOf(0 to 0L, 6 to 7L, 9 to 31L), samples)
        assertEquals(ConnectionPhase.entries.size, ConnectionPhase.entries.map { it.code }.distinct().size)
    }

    @Test fun diagnosticsCannotProduceNegativeDurationsOrContainInputStrings() {
        var clock = 500_000_000L
        val samples = mutableListOf<Pair<Int, Long>>()
        val timing = ConnectionTiming(clock = { clock }) { phase, elapsed -> samples += phase to elapsed }
        clock -= 2_000_000
        timing.mark(ConnectionPhase.REUSED)
        assertEquals(listOf(10 to 0L), samples)
        assertTrue(samples.all { it.first >= 0 && it.second >= 0 })
    }
}
