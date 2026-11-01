package com.aura.companion

import com.aura.companion.data.AppState
import com.aura.companion.data.ExecutionProcess
import com.aura.companion.data.ExecutionStep
import com.aura.companion.data.Message
import com.aura.companion.ui.MessageRole
import com.aura.companion.ui.MessageStatus
import org.junit.Assert.*
import org.junit.Test

class ConversationPresentationTest {
    private fun request(state: String = "running") = Message("user", "conversation", "user", "Inspect the drive",
        "2026-11-01T00:00:00Z", requestStatus = if (state == "running") "pending" else "complete", executionMode = "agent",
        agentActivity = "检查磁盘占用", execution = ExecutionProcess(state = state,
            startedAt = "2026-11-01T00:00:00Z", durationMs = 1000,
            events = listOf(ExecutionStep("step-0", "tool", "检查磁盘占用", "completed", 500))))

    @Test fun pendingProcessIsAssistantSlotAndNeverDuplicatedOnUserBubble() {
        val messages = conversationPresentation(AppState(messages = listOf(request())))
        assertEquals(2, messages.size)
        assertEquals(MessageRole.USER, messages[0].role)
        assertEquals(MessageStatus.SENT, messages[0].status)
        assertFalse(messages[0].desktopAgent); assertEquals("", messages[0].detail); assertNull(messages[0].execution)
        assertEquals("process:user", messages[1].id)
        assertEquals(MessageRole.ASSISTANT, messages[1].role)
        assertEquals("", messages[1].content)
        assertEquals("running", messages[1].execution!!.state)
        assertEquals("", messages[1].detail)
    }

    @Test fun completedProcessAttachesOnceToLinkedReplyAndKeepsActualAnswer() {
        val first = request("completed")
        val reply = Message("answer", "conversation", "assistant", "Actual drive observation", "2026-11-01T00:00:01Z", replyTo = first.id)
        val messages = conversationPresentation(AppState(messages = listOf(first, reply)))
        assertEquals(2, messages.size)
        assertEquals(1, messages.count { it.execution != null })
        assertEquals("answer", messages.last().id)
        assertEquals("Actual drive observation", messages.last().content)
        assertTrue(messages.last().desktopAgent)
        assertEquals("", messages.last().detail)
    }

    @Test fun actualFailureMovesWithProcessAndPrivateActivityNeverReplacesError() {
        val first = request("failed").copy(requestStatus = "failed", agentError = "Safe error")
        val messages = conversationPresentation(AppState(messages = listOf(first)))
        assertEquals("", messages.first().detail)
        assertEquals("Safe error", messages.last().detail)
        assertEquals(MessageStatus.FAILED, messages.last().status)
        assertEquals("failed", messages.last().execution!!.state)
    }

    @Test fun oldDesktopWithoutTraceKeepsLegacyStatusAndPhoneTraceDoesNotGainAgentBrand() {
        val legacy = request().copy(execution = null)
        val original = conversationPresentation(AppState(messages = listOf(legacy))).single()
        assertEquals(MessageStatus.PENDING, original.status)
        assertEquals("检查磁盘占用", original.detail)
        val phone = request().copy(executionMode = null, source = "mobile_direct", localState = "generating")
        val messages = conversationPresentation(AppState(messages = listOf(phone)))
        assertFalse(messages.last().desktopAgent)
        assertNotNull(messages.last().execution)
    }

    @Test fun processDoesNotAttachToAnotherRequestsAnswer() {
        val first = request()
        val unrelated = Message("other-answer", "conversation", "assistant", "Other result", "2026-11-01T00:00:01Z", replyTo = "another-user")
        val messages = conversationPresentation(AppState(messages = listOf(first, unrelated)))
        assertEquals(3, messages.size)
        assertEquals("process:user", messages[1].id)
        assertNull(messages.last().execution)
    }
}
