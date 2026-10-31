package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class CompanionDatabaseTest {
    private lateinit var db: CompanionDatabase
    private val cid = UUID.randomUUID().toString()
    private val mid = UUID.randomUUID().toString()

    @Before fun setUp() {
        db = CompanionDatabase(ApplicationProvider.getApplicationContext<Context>(), null)
    }
    @After fun tearDown() { db.close() }

    private fun conversation(id: String = cid): JSONObject = JSONObject().put("entity", "conversation")
        .put("id", id).put("conversation_id", id).put("deleted", false).put("data", JSONObject()
            .put("id", id).put("title", "电脑会话").put("created_at", "2026-10-31T01:00:00Z")
            .put("updated_at", "2026-10-31T01:00:00Z").put("message_count", 1))

    private fun message(id: String = mid, content: String = "电脑保存的正文"): JSONObject = JSONObject().put("entity", "message")
        .put("id", id).put("conversation_id", cid).put("deleted", false).put("data", JSONObject()
            .put("id", id).put("conversation_id", cid).put("role", "user").put("source", "user")
            .put("content", content).put("created_at", "2026-10-31T01:00:00Z").put("request_status", "complete"))

    private fun snapshot(items: List<JSONObject>, watermark: Long, more: Boolean = false, cursor: String = ""): JSONObject =
        JSONObject().put("items", JSONArray(items)).put("watermark", watermark).put("has_more", more).put("next_cursor", cursor)

    private fun pull(items: List<JSONObject>, next: Long): JSONObject = JSONObject().put("changes", JSONArray(items))
        .put("next_since", next).put("has_more", false)

    @Test fun snapshotReplacesOnlyWhenAllPagesArriveAndPreservesOfflineQueue() {
        db.stageSnapshotPage(snapshot(listOf(conversation(), message()), 7), true)
        val local = db.createConversation()
        val queued = db.addOutbox("chat.send", JSONObject().put("text", "离线草稿").put("conversation_id", local), local, "离线草稿")
        db.stageSnapshotPage(snapshot(listOf(conversation()), 10, true, "next-page"), true)
        assertEquals("电脑保存的正文", db.messages(cid).single().content)
        assertEquals(7L, db.cursor())
        assertEquals("next-page", db.snapshotCursor())
        db.stageSnapshotPage(snapshot(listOf(message(content = "完整的新缓存")), 10), false)
        assertEquals("完整的新缓存", db.messages(cid).single().content)
        assertEquals(10L, db.cursor())
        assertEquals("queued", db.operation(queued)!!.item.status)
        assertTrue(db.conversations().any { it.id == local && it.localOnly })
    }

    @Test fun snapshotWatermarkMismatchRollsBackPageAndKeepsOldVisibleHistory() {
        db.stageSnapshotPage(snapshot(listOf(conversation(), message()), 3), true)
        db.stageSnapshotPage(snapshot(listOf(conversation()), 5, true, "next"), true)
        assertThrows(IllegalArgumentException::class.java) {
            db.stageSnapshotPage(snapshot(listOf(message(content = "不能提交")), 6), false)
        }
        assertEquals(3L, db.cursor())
        assertEquals("电脑保存的正文", db.messages(cid).single().content)
        assertEquals("next", db.snapshotCursor())
    }

    @Test fun repeatedPullDoesNotDuplicateMessagesOrRegressCursor() {
        db.stageSnapshotPage(snapshot(listOf(conversation()), 0), true)
        val first = pull(listOf(message().put("seq", 1)), 1)
        db.applyPull(first); db.applyPull(first)
        val reply = message(UUID.randomUUID().toString(), "AI 回复").put("seq", 2)
        reply.getJSONObject("data").put("role", "assistant").put("source", "assistant").put("reply_to", mid)
        db.applyPull(pull(listOf(reply), 2))
        db.applyPull(first)
        assertEquals(2, db.messages(cid).size)
        assertEquals(2L, db.cursor())
        assertEquals(mid, db.messages(cid).first { it.role == "assistant" }.replyTo)
    }

    @Test fun invalidPageDoesNotCommitEarlierItemsOrCursor() {
        db.stageSnapshotPage(snapshot(listOf(conversation()), 0), true)
        assertThrows(IllegalArgumentException::class.java) {
            db.applyPull(pull(listOf(message().put("seq", 2), message(UUID.randomUUID().toString()).put("seq", 1)), 2))
        }
        assertTrue(db.messages(cid).isEmpty())
        assertEquals(0L, db.cursor())
    }

    @Test fun deletingConversationBlocksItsQueueWithoutTouchingOtherConversation() {
        val other = db.createConversation()
        db.stageSnapshotPage(snapshot(listOf(conversation(), message()), 0), true)
        val first = db.addOutbox("chat.send", JSONObject().put("text", "old").put("conversation_id", cid), cid, "old")
        val second = db.addOutbox("chat.send", JSONObject().put("text", "new").put("conversation_id", other), other, "new")
        val deleted = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid)
            .put("deleted", true).put("data", JSONObject()).put("seq", 1)
        db.applyPull(pull(listOf(deleted), 1))
        assertEquals("needs_attention", db.operation(first)!!.item.status)
        assertEquals("queued", db.operation(second)!!.item.status)
        assertTrue(db.isConversationDeleted(cid))
        db.applyPull(pull(listOf(conversation().put("seq", 2), message().put("seq", 3)), 3))
        assertTrue(db.messages(cid).isEmpty())
        assertTrue(db.conversations().none { it.id == cid })
    }

    @Test fun fullSnapshotMissingConversationBlocksRatherThanResendsItsDraft() {
        db.stageSnapshotPage(snapshot(listOf(conversation(), message()), 0), true)
        val queued = db.addOutbox("chat.send", JSONObject().put("text", "离线未发"), cid, "离线未发")
        db.stageSnapshotPage(snapshot(emptyList(), 100), true)
        assertEquals("needs_attention", db.operation(queued)!!.item.status)
        assertEquals("离线未发", db.operation(queued)!!.item.text)
        assertEquals(100L, db.cursor())
        assertTrue(db.messages(cid).none { it.content == "电脑保存的正文" })
    }

    @Test fun authoritativeClearDoesNotRecreateSentMessageFromOutbox() {
        db.stageSnapshotPage(snapshot(listOf(conversation(), message()), 0), true)
        val queued = db.addOutbox("chat.send", JSONObject().put("text", "电脑保存的正文"), cid, "电脑保存的正文")
        db.updateOutbox(queued, "sent", messageId = mid, attempted = true)
        val deleted = JSONObject().put("entity", "message").put("id", mid).put("conversation_id", cid)
            .put("deleted", true).put("data", JSONObject()).put("seq", 1)
        db.applyPull(pull(listOf(deleted), 1))
        assertTrue(db.messages(cid).isEmpty())
        assertEquals("discarded", db.operation(queued)!!.item.status)
    }

    @Test fun replacingDesktopNeverSendsOldDesktopQueueToNewAuthority() {
        db.saveDevice(DeviceInfo("电脑 A", "desktop-a", "public-a"), "phone")
        val local = db.createConversation()
        val queued = db.addOutbox("chat.send", JSONObject().put("text", "属于A"), local, "属于A")
        db.saveDevice(DeviceInfo("电脑 B", "desktop-b", "public-b"), "phone")
        assertEquals("needs_attention", db.operation(queued)!!.item.status)
        assertEquals("desktop-a", db.operation(queued)!!.authorityId)
        assertEquals("desktop-b", db.device()!!.id)
        assertFalse(db.initialized())
    }

    @Test fun onlySelectedConversationMessagesAreReturnedAndLongHistoryStaysOnDisk() {
        db.stageSnapshotPage(snapshot(listOf(conversation()), 0), true)
        var seq = 0L
        repeat(6) {
            val items = (0 until 100).map { message(UUID.randomUUID().toString(), "text-${++seq}").put("seq", seq) }
            db.applyPull(pull(items, seq))
        }
        assertEquals(500, db.messages(cid).size)
        db.readableDatabase.rawQuery("SELECT count(*) FROM messages", null).use { it.moveToFirst(); assertEquals(600, it.getInt(0)) }
    }

    @Test fun stagingOnlyPersistsPublicProjectionWithoutPrivateMetadataOrSummary() {
        val item = message()
        item.getJSONObject("data").put("metadata", JSONObject().put("api_key", "SECRET"))
            .put("image_path", "private-screen.png").put("summary", "INTERNAL_SUMMARY")
        db.stageSnapshotPage(snapshot(listOf(item), 0, true, "next"), true)
        db.readableDatabase.rawQuery("SELECT payload FROM snapshot_items", null).use {
            assertTrue(it.moveToFirst())
            val persisted = it.getString(0)
            assertFalse(persisted.contains("SECRET")); assertFalse(persisted.contains("private-screen.png"))
            assertFalse(persisted.contains("INTERNAL_SUMMARY"))
        }
    }

    @Test fun imageInboxEventHasAttachmentButIsNeverAnAiChatRequest() {
        val local = db.createConversation()
        val imageId = "a".repeat(64)
        db.addOutbox("image.upload", JSONObject().put("sha256", imageId), local,
            "图片收件箱：photo.png", imageId)
        val message = db.messages(local).single()
        assertEquals("image_inbox", message.source)
        assertEquals(imageId, message.attachmentId)
        assertTrue(message.content.contains("不会自动识图或回复"))
        assertNull(message.requestStatus)
        assertEquals("image.upload", db.outbox().single().method)
    }
}
