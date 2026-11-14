package com.aura.companion.data

import android.content.Context
import android.content.ContextWrapper
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import com.aura.companion.ui.DraftSnapshot
import com.aura.companion.ui.PrivateDraftStore
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.Rule
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class FileAttachmentUpgradeTest {
    @get:Rule val ownedFiles = TemporaryFolder()
    @Test fun v5UpgradeKeepsDraftImageHistoryDirectTurnAndPairingWhileBackfillingFiles() {
        // Windows sqlite4java cannot create WAL sidecars under Robolectric's long method paths.
        val context = object : ContextWrapper(ApplicationProvider.getApplicationContext<Context>()) {
            override fun getApplicationContext(): Context = this
            override fun getFilesDir(): File = ownedFiles.root
            override fun getDatabasePath(name: String): File = File(ownedFiles.root, name)
            override fun deleteDatabase(name: String): Boolean = SQLiteDatabase.deleteDatabase(getDatabasePath(name))
        }
        val name = "v5.db"
        val draftFile = File(context.filesDir, "draft.json")
        val cid = UUID.randomUUID().toString(); val mid = UUID.randomUUID().toString()
        var queued = ""; var turnId = ""; var generation: String? = null
        val draftStore = PrivateDraftStore(draftFile)
        try {
            val old = CompanionDatabase(context, name)
            try {
                old.saveDevice(DeviceInfo("Paired PC", "old-desktop", "synthetic-ticket"), "phone")
                old.createConversation(cid)
                val conversation = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid)
                    .put("data", JSONObject().put("title", "Previous conversation").put("created_at", "2026-11-01T00:00:00Z")
                        .put("updated_at", "2026-11-01T00:00:00Z"))
                val message = JSONObject().put("entity", "message").put("id", mid).put("conversation_id", cid)
                    .put("data", JSONObject().put("role", "assistant").put("content", "Existing answer")
                        .put("created_at", "2026-11-01T00:00:00Z"))
                old.stageSnapshotPage(JSONObject().put("items", JSONArray().put(conversation).put(message))
                    .put("watermark", 42).put("has_more", false), true)
                queued = old.queueAgentMessage(cid, "Unsent task")
                val turn = old.beginDirectTurn(cid, "Local question", null)
                old.finishDirectTurn(turn.id, "Local answer"); turnId = turn.id
                old.stageImage(cid, ImageItem("a".repeat(64), "draft.png", "image/png", 128, 4, 4,
                    "2026-11-01T00:00:00Z", uploadState = "staged"))
                old.setDesktopAgentMode(cid, true)
                generation = old.conversationGeneration(cid)
                old.setSetting("snapshot_cursor", "partial-snapshot")
                draftStore.update { it.copy(revision = 3, entries = mapOf(cid to DraftSnapshot("Still editing", 3, "draft-identity"))) }
                old.writableDatabase.apply {
                    execSQL("""CREATE TABLE messages_v5(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,
                        content TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,request_status TEXT,reply_to TEXT,
                        image_id TEXT,image_error TEXT,agent_error_code TEXT,agent_activity TEXT,execution_mode TEXT,agent_trace TEXT)""")
                    execSQL("""INSERT INTO messages_v5 SELECT id,conversation_id,role,content,created_at,source,
                        request_status,reply_to,image_id,image_error,agent_error_code,agent_activity,execution_mode,agent_trace FROM messages""")
                    execSQL("DROP TABLE messages")
                    execSQL("ALTER TABLE messages_v5 RENAME TO messages")
                    execSQL("CREATE INDEX messages_conversation ON messages(conversation_id,created_at,id)")
                    execSQL("DROP TABLE pending_files")
                    version = 5
                }
            } finally { old.close() }
            val upgraded = CompanionDatabase(context, name)
            try {
                assertEquals(6, upgraded.readableDatabase.version)
                assertEquals("old-desktop", upgraded.device()!!.endpointId)
                assertEquals("synthetic-ticket", upgraded.device()!!.ticket)
                assertEquals("Existing answer", upgraded.messages(cid).first { it.id == mid }.content)
                assertNull(upgraded.messages(cid).first { it.id == mid }.fileName)
                assertEquals("queued", upgraded.operation(queued)!!.item.status)
                assertEquals("Unsent task", upgraded.operation(queued)!!.item.text)
                assertFalse(upgraded.operation(queued)!!.attempted)
                assertEquals("Local answer", upgraded.directTurns().single { it.id == turnId }.reply)
                assertEquals("a".repeat(64), upgraded.pendingImage(cid)!!.attachmentId)
                assertTrue(cid in upgraded.desktopAgentConversations())
                assertEquals(generation, upgraded.conversationGeneration(cid))
                assertEquals(42L, upgraded.cursor()); assertFalse(upgraded.initialized())
                assertEquals("", upgraded.snapshotCursor()); assertTrue(upgraded.pendingFiles().isEmpty())
                val draft = PrivateDraftStore(draftFile).load().entries[cid]!!
                assertEquals("Still editing", draft.text); assertEquals("draft-identity", draft.identity)
                val file = PendingFile(cid, "b".repeat(64), "new.pdf", "application/pdf", 2, "synthetic-path", "old-desktop")
                upgraded.stageFile(file)
                assertEquals(file, upgraded.pendingFile(cid)); assertNull(upgraded.pendingImage(cid))
                val request = upgraded.queueAgentMessage(cid, "New file task", file)
                assertEquals("new.pdf", upgraded.messages(cid).first { it.id == request }.fileName)
                assertNull(upgraded.pendingFile(cid))
            } finally { upgraded.close() }
        } finally { context.deleteDatabase(name); draftFile.delete() }
    }
}
