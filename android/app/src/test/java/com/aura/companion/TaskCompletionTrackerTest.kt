package com.aura.companion

import com.aura.companion.data.*
import org.junit.Assert.*
import org.junit.Test

class TaskCompletionTrackerTest {
    private val device = DeviceInfo("电脑", "peer", "ticket")
    private fun message(id: String = "message", cid: String = "a", status: String = "pending") =
        Message(id, cid, "user", "private task", "2026-11-07T00:00:00Z", requestStatus = status, executionMode = "agent")
    private fun state(messages: List<Message>, selected: String = "a") = AppState(device = device,
        connectionState = ConnectionState.CONNECTED, messages = messages, selectedConversationId = selected,
        conversations = listOf(Conversation("a", "A", "", ""), Conversation("b", "B", "", "")))

    @Test fun historicalCompletedMessagesDoNotCreateNotifications() {
        val tracker = TaskCompletionTracker()
        assertTrue(tracker.observe(state(listOf(message(status = "complete")))).isEmpty())
        assertEquals(0, tracker.pendingCount)
    }

    @Test fun observedTaskCompletesExactlyOnceAndDoesNotExposeItsContents() {
        val tracker = TaskCompletionTracker()
        tracker.observe(state(listOf(message())))
        val result = tracker.observe(state(listOf(message(status = "complete"))))
        assertEquals(listOf(TaskCompletion("message", "a", "completed")), result)
        assertFalse(result.toString().contains("private task"))
        assertTrue(tracker.observe(state(listOf(message(status = "complete")))).isEmpty())
    }

    @Test fun switchingConversationAndDisconnectingNeverCreatesFalseSuccess() {
        val tracker = TaskCompletionTracker()
        tracker.observe(state(listOf(message())))
        val other = state(listOf(message("other", "b")), "b")
        assertTrue(tracker.observe(other.copy(connectionState = ConnectionState.ERROR)).isEmpty())
        assertEquals(2, tracker.pendingCount)
        val done = tracker.observe(other, listOf(message(status = "complete"), message("other", "b")))
        assertEquals("a", done.single().conversationId)
        assertEquals(1, tracker.pendingCount)
    }

    @Test fun requestIdentitySurvivesCanonicalMessageAssignment() {
        val tracker = TaskCompletionTracker()
        val op = OutboxItem("request", "agent.send", "a", "private", "sending", "")
        tracker.observe(state(emptyList()).copy(outbox = listOf(op)))
        assertEquals(1, tracker.pendingCount)
        tracker.observe(state(listOf(message())).copy(outbox = listOf(op.copy(status = "sent", messageId = "message"))))
        assertEquals(1, tracker.pendingCount)
        val done = tracker.observe(state(listOf(message(status = "complete")))
            .copy(outbox = listOf(op.copy(status = "sent", messageId = "message"))))
        assertEquals("request", done.single().requestId)
    }

    @Test fun peerChangeDropsOldRequestsWithoutAnnouncingSuccess() {
        val tracker = TaskCompletionTracker()
        tracker.observe(state(listOf(message())))
        assertTrue(tracker.observe(state(listOf(message(status = "complete")))
            .copy(device = DeviceInfo("Other", "different", "ticket"))).isEmpty())
        assertEquals(0, tracker.pendingCount)
    }

    @Test fun failureAndExplicitDiscardAreDifferentOutcomes() {
        for ((status, expected) in listOf("failed" to "failed", "needs_attention" to "failed", "discarded" to null)) {
            val tracker = TaskCompletionTracker()
            val op = OutboxItem("request", "agent.send", "a", "private", "sending", "")
            tracker.observe(state(emptyList()).copy(outbox = listOf(op)))
            val done = tracker.observe(state(emptyList()).copy(outbox = listOf(op.copy(status = status))))
            assertEquals(expected, done.singleOrNull()?.outcome)
            assertEquals(0, tracker.pendingCount)
        }
    }

    @Test fun delayedOperationReceiptDoesNotRepeatCanonicalCompletion() {
        val tracker = TaskCompletionTracker()
        val op = OutboxItem("request", "agent.send", "a", "private", "pending", "", messageId = "message")
        tracker.observe(state(listOf(message())).copy(outbox = listOf(op)))
        val done = state(listOf(message(status = "complete"))).copy(outbox = listOf(op))
        assertEquals(1, tracker.observe(done).size)
        repeat(5) { assertTrue(tracker.observe(done).isEmpty()) }
        assertEquals(0, tracker.pendingCount)
    }

    @Test fun completedCanonicalMessageTakesPriorityOverStalePendingOperation() {
        val tracker = TaskCompletionTracker()
        val op = OutboxItem("request", "agent.send", "a", "private", "pending", "", messageId = "message")
        assertTrue(tracker.observe(state(listOf(message(status = "complete"))).copy(outbox = listOf(op))).isEmpty())
        assertEquals(0, tracker.pendingCount)
    }

    @Test fun needsUserThenResumedSameRequestCanNotifyFinalCompletion() {
        val tracker = TaskCompletionTracker()
        val pending = message().copy(execution = ExecutionProcess(state = "running", startedAt = "", durationMs = 0, events = emptyList()))
        tracker.observe(state(listOf(pending)))
        val waiting = pending.copy(execution = pending.execution!!.copy(state = "needs_user"))
        assertEquals("needs_user", tracker.observe(state(listOf(waiting))).single().outcome)
        assertTrue(tracker.observe(state(listOf(waiting))).isEmpty())
        tracker.observe(state(listOf(pending)))
        assertEquals("completed", tracker.observe(state(listOf(pending.copy(requestStatus = "complete",
            execution = pending.execution!!.copy(state = "completed"))))).single().outcome)
    }

    @Test fun canonicalPullOvertakingQueueStateDoesNotAdoptTheSameTaskTwice() {
        val tracker = TaskCompletionTracker()
        val queued = OutboxItem("request", "agent.send", "a", "private", "queued", "")
        tracker.observe(state(listOf(message())).copy(outbox = listOf(queued)))
        assertEquals(1, tracker.pendingCount)
        val acknowledged = queued.copy(status = "pending", messageId = "message")
        tracker.observe(state(listOf(message())).copy(outbox = listOf(acknowledged)))
        assertEquals(1, tracker.pendingCount)
        val done = state(listOf(message(status = "complete"))).copy(outbox = listOf(acknowledged))
        assertEquals("request", tracker.observe(done).single().requestId)
        assertTrue(tracker.observe(done).isEmpty())
    }
}
