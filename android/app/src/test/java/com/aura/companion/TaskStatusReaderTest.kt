package com.aura.companion

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import com.aura.companion.data.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class TaskStatusReaderTest {
    @Test fun taskOlderThanFiveHundredMessagesStillCompletesWithoutLoadingConversationBodies() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        val database = CompanionDatabase(app, null)
        try {
            val cid = database.createConversation()
            val mid = UUID.randomUUID().toString()
            database.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source,request_status,execution_mode) VALUES(?,?,?,?,?,?,?,?)",
                arrayOf(mid, cid, "user", "PRIVATE_TASK_BODY", "2026-11-07T00:00:00Z", "user", "pending", "agent"))
            val tracker = TaskCompletionTracker()
            val state = AppState(device = DeviceInfo("电脑", "peer", "ticket"), conversations = listOf(Conversation(cid, "", "", "")))
            val running = readTaskStatuses(database, emptySet(), emptyList())
            assertEquals(mid, running.single().id)
            assertEquals("", running.single().content)
            tracker.observe(state, running)
            database.writableDatabase.beginTransaction()
            try {
                repeat(620) { index ->
                    database.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
                        arrayOf(UUID.randomUUID().toString(), cid, "assistant", "PRIVATE_CHAT_BODY_$index", "2026-11-07T01:00:00Z", "assistant"))
                }
                database.writableDatabase.setTransactionSuccessful()
            } finally { database.writableDatabase.endTransaction() }
            assertFalse("The ordinary 500-message UI window no longer contains the task", database.messages(cid).any { it.id == mid })
            database.writableDatabase.execSQL("UPDATE messages SET request_status='complete' WHERE id=?", arrayOf(mid))
            val finished = readTaskStatuses(database, tracker.messageIds, emptyList())
            assertEquals(1, finished.size)
            assertEquals("", finished.single().content)
            assertEquals("completed", tracker.observe(state, finished).single().outcome)
            assertTrue(tracker.observe(state, finished).isEmpty())
        } finally { database.close() }
    }

    @Test fun pendingTasksAcrossConversationsAndLargeIdSetsRemainBoundedToTaskMetadata() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        val database = CompanionDatabase(app, null)
        try {
            val first = database.createConversation()
            val second = database.createConversation()
            for ((id, cid) in listOf("one" to first, "two" to second))
                database.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source,request_status,execution_mode) VALUES(?,?,?,?,?,?,?,?)",
                    arrayOf(id, cid, "user", "PRIVATE", "2026-11-07T00:00:00Z", "user", "pending", "agent"))
            val statuses = readTaskStatuses(database, (1..1200).map { "absent-$it" }.toSet(), emptyList())
            assertEquals(setOf(first, second), statuses.map { it.conversationId }.toSet())
            assertTrue(statuses.all { it.content.isEmpty() })
        } finally { database.close() }
    }

    @Test fun fastCompletionIncludesNewCanonicalIdEvenWhenTheSendReceiptAlreadySaysSent() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        val database = CompanionDatabase(app, null)
        try {
            val cid = database.createConversation()
            val mid = UUID.randomUUID().toString()
            val queued = OutboxItem("request", "agent.send", cid, "PRIVATE", "queued", "")
            val tracker = TaskCompletionTracker()
            val initial = AppState(device = DeviceInfo("电脑", "peer", "ticket"), outbox = listOf(queued),
                conversations = listOf(Conversation(cid, "", "", "")))
            tracker.observe(initial, emptyList())
            database.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source,request_status,execution_mode) VALUES(?,?,?,?,?,?,?,?)",
                arrayOf(mid, cid, "user", "PRIVATE_TASK_BODY", "2026-11-07T00:00:00Z", "user", "complete", "agent"))
            val acknowledged = initial.copy(outbox = listOf(queued.copy(status = "sent", messageId = mid)))
            val statuses = readTaskStatuses(database, tracker.messageIds, acknowledged.outbox, tracker.requestIds)
            assertEquals(mid, statuses.single().id)
            assertEquals("request", tracker.observe(acknowledged, statuses).single().requestId)
        } finally { database.close() }
    }
}
