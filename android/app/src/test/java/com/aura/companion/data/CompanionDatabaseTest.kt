package com.aura.companion.data

import android.content.Context
import android.database.sqlite.SQLiteDatabase
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

    private fun image(id: String = "a".repeat(64)) = ImageItem(id, "photo.png", "image/png", 100, 1, 1,
        "2026-10-31T01:00:00Z", "/local/cache/$id.img", "staged")

    @Test fun canonicalImageAttachmentSurvivesSnapshotAndPullWithoutPrivateMetadata() {
        val id = "a".repeat(64)
        val item = message()
        item.getJSONObject("data").put("image_id", id).put("image_error", "vision_rejected")
            .put("image_path", "/private/capture.png").put("metadata", JSONObject().put("secret", "SECRET"))
        db.stageSnapshotPage(snapshot(listOf(conversation(), item), 3), true)
        assertEquals(id, db.messages(cid).single().attachmentId)
        assertEquals("vision_rejected", db.messages(cid).single().imageError)
        val updated = message().put("seq", 4)
        updated.getJSONObject("data").put("image_id", id).put("image_error", "image_missing")
        db.applyPull(pull(listOf(updated), 4))
        assertEquals(id, db.messages(cid).single().attachmentId)
        assertEquals("image_missing", db.messages(cid).single().imageError)
        val unsafe = message(UUID.randomUUID().toString()).put("seq", 5)
        unsafe.getJSONObject("data").put("image_id", "../../private.png").put("image_error", "SECRET_INTERNAL_ERROR")
        db.applyPull(pull(listOf(unsafe), 5))
        assertNull(db.messages(cid).last { it.id != mid }.attachmentId)
        assertNull(db.messages(cid).last { it.id != mid }.imageError)
    }

    @Test fun agentProgressAndErrorsPersistOnlyAsPublicWhitelistedMetadata() {
        val item = message()
        item.getJSONObject("data").put("execution_mode", "agent").put("request_status", "pending")
            .put("agent_error_code", "cancelled").put("agent_error", "PRIVATE_PROVIDER_KEY")
            .put("agent_activity", "正在规划任务…").put("agent_context", "PRIVATE_TOOL_OUTPUT")
        db.stageSnapshotPage(snapshot(listOf(conversation(), item), 3), true)
        val saved = db.messages(cid).single()
        assertEquals("agent", saved.executionMode); assertEquals("cancelled", saved.agentErrorCode)
        assertEquals(AGENT_ERROR_MESSAGES.getValue("cancelled"), saved.agentError)
        assertEquals("正在规划任务…", saved.agentActivity)
        val unsafe = message().put("seq", 4)
        unsafe.getJSONObject("data").put("agent_error_code", "PRIVATE_KEY").put("agent_error", "PRIVATE_KEY")
            .put("agent_activity", "PRIVATE_TOOL_ARGUMENT").put("execution_mode", "PRIVATE_EXECUTION")
        db.applyPull(pull(listOf(unsafe), 4))
        val cleared = db.messages(cid).single()
        assertNull(cleared.agentErrorCode); assertNull(cleared.agentError)
        assertNull(cleared.agentActivity); assertNull(cleared.executionMode)
        listOf("gui_desktop_locked", "gui_desktop_session_unavailable",
            "gui_screenshot_display_unavailable", "gui_screenshot_black_frame").forEachIndexed { index, code ->
            val seq = 5L + index
            val update = message().put("seq", seq)
            update.getJSONObject("data").put("request_status", "failed").put("agent_error_code", code)
                .put("agent_error", "PRIVATE_NATIVE_EXCEPTION").put("agent_activity", "查找已安装应用")
            db.applyPull(pull(listOf(update), seq))
            assertEquals(code, db.messages(cid).single().agentErrorCode)
            assertEquals(AGENT_ERROR_MESSAGES.getValue(code), db.messages(cid).single().agentError)
            assertEquals("查找已安装应用", db.messages(cid).single().agentActivity)
        }
        listOf("检查电脑状态", "检查磁盘占用", "查看目录信息").forEachIndexed { index, label ->
            val seq = 9L + index
            val update = message().put("seq", seq)
            update.getJSONObject("data").put("request_status", "pending").put("agent_activity", label)
            db.applyPull(pull(listOf(update), seq))
            assertEquals(label, db.messages(cid).single().agentActivity)
        }
    }

    @Test fun stagedImageIsConversationSpecificAndQueueConsumptionIsAtomic() {
        val first = db.createConversation(); val second = db.createConversation()
        db.stageImage(first, image()); db.stageImage(second, image("b".repeat(64)))
        val request = db.queueMessage(first, "看图")
        assertEquals(setOf(second), db.pendingImages().keys)
        val chat = db.operation(request)!!
        assertEquals("a".repeat(64), chat.item.attachmentId)
        assertEquals("a".repeat(64), chat.params.getString("image_id"))
        assertNotNull(chat.dependencyId)
        assertEquals("image.upload", db.operation(chat.dependencyId!!)!!.item.method)
        assertTrue(db.operation(chat.dependencyId!!)!!.item.hidden)
        assertFalse(chat.item.hidden)
        // Keep both durable operations available, while the UI can show one chat.
        assertEquals(2, db.outbox().size)
        assertEquals(listOf(request), db.outbox().filterNot { it.hidden }.map { it.requestId })
        assertEquals(listOf(request), db.outgoing().map { it.item.requestId })
        assertEquals(1, db.messages(first).size)
        assertEquals("b".repeat(64), db.pendingImages()[second]!!.attachmentId)
        assertEquals(setOf("a".repeat(64), "b".repeat(64)), db.protectedImageIds())
    }

    @Test fun fullQueueRollsBackUploadAndLeavesComposerAttachmentIntact() {
        val local = db.createConversation()
        db.stageImage(local, image())
        val sql = db.writableDatabase
        sql.beginTransaction()
        try {
            repeat(999) { index -> sql.execSQL("""INSERT INTO outbox(id,method,params,text,state,created_at)
                VALUES(?,'chat.send','{}','old','queued',?)""", arrayOf("old-$index", "2026-10-31T01:00:00Z")) }
            sql.setTransactionSuccessful()
        } finally { sql.endTransaction() }
        assertThrows(IllegalArgumentException::class.java) { db.queueMessage(local, "不能部分提交") }
        assertEquals("a".repeat(64), db.pendingImage(local)!!.attachmentId)
        assertEquals(999, db.outbox().size)
        assertTrue(db.outbox().none { it.method == "image.upload" })
    }

    @Test fun replacingComputerCannotAttachAnOldComputersComposerImage() {
        db.saveDevice(DeviceInfo("A", "desktop-a", "ticket-a"), "phone")
        val local = db.createConversation()
        db.stageImage(local, image())
        db.saveDevice(DeviceInfo("B", "desktop-b", "ticket-b"), "phone")
        assertThrows(IllegalArgumentException::class.java) { db.queueMessage(local, "不要发送给 B") }
        assertNotNull(db.pendingImage(local))
        assertTrue(db.outbox().isEmpty())
    }

    @Test fun versionOneUpgradePreservesHistoryLegacyInboxQueueAndPairing() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "version-one-${UUID.randomUUID()}.db"
        val file = context.getDatabasePath(name)
        file.parentFile!!.mkdirs()
        SQLiteDatabase.openOrCreateDatabase(file, null).use { old ->
            old.execSQL("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            old.execSQL("""CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,local_only INTEGER NOT NULL DEFAULT 0)""")
            old.execSQL("""CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,
                content TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,request_status TEXT,reply_to TEXT)""")
            old.execSQL("CREATE INDEX messages_conversation ON messages(conversation_id,created_at,id)")
            old.execSQL("CREATE TABLE tombstones(entity TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(entity,id))")
            old.execSQL("CREATE TABLE snapshot_items(entity TEXT NOT NULL,id TEXT NOT NULL,conversation_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(entity,id))")
            old.execSQL("""CREATE TABLE outbox(id TEXT PRIMARY KEY,method TEXT NOT NULL,params TEXT NOT NULL,
                conversation_id TEXT,text TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,message_id TEXT,
                attachment_id TEXT,error TEXT,attempted INTEGER NOT NULL DEFAULT 0,authority_id TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL DEFAULT 0)""")
            old.execSQL("""CREATE TABLE images(id TEXT PRIMARY KEY,filename TEXT NOT NULL,mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,created_at TEXT NOT NULL,
                local_path TEXT,upload_state TEXT NOT NULL DEFAULT 'received')""")
            old.execSQL("INSERT INTO settings VALUES('device_ticket','public-ticket')")
            old.execSQL("INSERT INTO settings VALUES('device_id','old-desktop')")
            old.execSQL("INSERT INTO settings VALUES('sync_cursor','42')")
            old.execSQL("INSERT INTO settings VALUES('sync_ready','1')")
            old.execSQL("INSERT INTO conversations VALUES(?,'历史会话','old','old',1,0)", arrayOf(cid))
            old.execSQL("INSERT INTO messages VALUES(?,?,'user','已有消息','old','user',NULL,NULL)", arrayOf(mid, cid))
            old.execSQL("""INSERT INTO outbox(id,method,params,conversation_id,text,state,created_at,attachment_id,authority_id)
                VALUES('legacy-upload','image.upload',?,?,'旧版图片收件箱','queued','old',?,'old-desktop')""",
                arrayOf(JSONObject().put("sha256", "a".repeat(64)).toString(), cid, "a".repeat(64)))
            old.version = 1
        }
        try {
            val upgraded = CompanionDatabase(context, name)
            try {
            assertEquals(5, upgraded.readableDatabase.version)
                assertEquals("public-ticket", upgraded.device()!!.ticket)
                assertEquals(42L, upgraded.cursor())
                assertEquals("已有消息", upgraded.messages(cid).first { it.id == mid }.content)
                assertNull(upgraded.messages(cid).first { it.id == mid }.attachmentId)
                assertEquals("image.upload", upgraded.operation("legacy-upload")!!.item.method)
                assertNull(upgraded.operation("legacy-upload")!!.dependencyId)
                assertEquals(listOf("legacy-upload"), upgraded.outgoing().map { it.item.requestId })
                upgraded.stageImage(cid, image())
                assertNotNull(upgraded.pendingImage(cid))
                assertEquals("chat.send", upgraded.operation(upgraded.queueMessage(cid, "新图文"))!!.item.method)
            } finally { upgraded.close() }
        } finally { context.deleteDatabase(name) }
    }
}
