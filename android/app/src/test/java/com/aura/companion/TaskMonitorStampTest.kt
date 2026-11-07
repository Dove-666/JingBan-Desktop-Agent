package com.aura.companion

import com.aura.companion.data.*
import org.junit.Assert.*
import org.junit.Test

class TaskMonitorStampTest {
    @Test fun phoneStreamingChunksDoNotTriggerTaskDatabaseReads() {
        val assistant = Message("reply", "phone-chat", "assistant", "first chunk", "", source = "mobile_direct")
        val original = AppState(messages = listOf(assistant), directChatBusy = true)
        assertEquals(original.monitorStamp(), original.copy(messages = listOf(assistant.copy(content = "more chunks"))).monitorStamp())
        assertEquals(original.monitorStamp(), original.copy(messages = listOf(assistant.copy(content = "finished")), directChatBusy = false).monitorStamp())
    }

    @Test fun taskTerminalStateChangesTheKeyButProgressAnimationDoesNot() {
        val user = Message("request", "pc-chat", "user", "private", "", executionMode = "agent", requestStatus = "pending",
            execution = ExecutionProcess(state = "running", startedAt = "", durationMs = 0, events = emptyList()))
        val original = AppState(messages = listOf(user))
        assertEquals(original.monitorStamp(), original.copy(messages = listOf(user.copy(agentActivity = "读取文件",
            execution = user.execution!!.copy(durationMs = 1000)))).monitorStamp())
        assertNotEquals(original.monitorStamp(), original.copy(messages = listOf(user.copy(requestStatus = "complete",
            execution = user.execution!!.copy(state = "completed")))).monitorStamp())
    }

    @Test fun backgroundPullStillRefreshesNonSelectedTaskRecords() {
        val original = AppState(lastSyncedAt = 1)
        assertNotEquals(original.monitorStamp(), original.copy(lastSyncedAt = 2).monitorStamp())
        assertNotEquals(original.monitorStamp(), original.copy(connectionState = ConnectionState.UNAUTHORIZED).monitorStamp())
    }
}
