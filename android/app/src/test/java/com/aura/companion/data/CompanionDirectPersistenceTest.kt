package com.aura.companion.data

import android.content.Context
import android.content.ContextWrapper
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.Rule
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID
import java.io.File

/** Storage-only regressions: no HTTP request, credential or model fixture is used. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class CompanionDirectPersistenceTest {
    @get:Rule val databaseFiles = TemporaryFolder()
    private val context by lazy {
        // Keep SQLite's database/WAL/SHM paths short and owned by this test.
        // Robolectric's per-method sandbox paths can exceed Windows' native
        // SQLite path limit before journal_mode=WAL creates its sidecars.
        object : ContextWrapper(ApplicationProvider.getApplicationContext<Context>()) {
            override fun getApplicationContext(): Context = this
            override fun getDatabasePath(name: String): File = File(databaseFiles.root, name).also {
                require(it.parentFile!!.isDirectory || it.parentFile!!.mkdirs())
            }
            override fun deleteDatabase(name: String): Boolean = SQLiteDatabase.deleteDatabase(getDatabasePath(name))
        }
    }

    private fun paired(database: CompanionDatabase) {
        database.saveDevice(DeviceInfo("验收电脑", "desktop", "public-ticket"), "phone")
    }

    private fun completeTurn(database: CompanionDatabase): DirectTurn {
        val turn = database.beginDirectTurn(database.createConversation(), "手机上的问题", null)
        database.finishDirectTurn(turn.id, "手机已经生成的回复")
        return database.directTurns().single { it.id == turn.id }
    }

    private fun acknowledge(database: CompanionDatabase, turn: DirectTurn) {
        database.prepareDirectImports()
        val request = requireNotNull(database.directTurns().single { it.id == turn.id }.importRequestId)
        database.acceptDirectImport(request, JSONObject().put("imported", true)
            .put("conversation_id", turn.conversationId)
            .put("message_ids", JSONArray().put(turn.userId).put(turn.assistantId)))
    }

    private fun assertExchange(database: CompanionDatabase, turn: DirectTurn) {
        val messages = database.messages(turn.conversationId)
        assertEquals(2, messages.size)
        assertEquals(setOf(turn.userId, turn.assistantId), messages.map { it.id }.toSet())
        assertEquals(turn.text, messages.single { it.id == turn.userId }.content)
        assertEquals(turn.reply, messages.single { it.id == turn.assistantId }.content)
        assertEquals(turn.userId, messages.single { it.id == turn.assistantId }.replyTo)
    }

    private fun conversation(turn: DirectTurn) = JSONObject().put("entity", "conversation")
        .put("id", turn.conversationId).put("conversation_id", turn.conversationId).put("deleted", false)
        .put("data", JSONObject().put("id", turn.conversationId).put("title", "已同步的会话")
            .put("created_at", turn.userAt).put("updated_at", turn.assistantAt).put("message_count", 2))

    private fun message(turn: DirectTurn, assistant: Boolean): JSONObject {
        val id = if (assistant) turn.assistantId else turn.userId
        val role = if (assistant) "assistant" else "user"
        val data = JSONObject().put("id", id).put("conversation_id", turn.conversationId)
            .put("role", role).put("source", role).put("content", if (assistant) turn.reply else turn.text)
            .put("created_at", if (assistant) turn.assistantAt else turn.userAt)
        if (assistant) data.put("reply_to", turn.userId) else data.put("request_status", "complete")
        return JSONObject().put("entity", "message").put("id", id).put("conversation_id", turn.conversationId)
            .put("deleted", false).put("data", data)
    }

    private fun snapshot(items: List<JSONObject>, watermark: Long) = JSONObject()
        .put("items", JSONArray(items)).put("watermark", watermark).put("has_more", false).put("next_cursor", "")

    private fun pull(items: List<JSONObject>, next: Long) = JSONObject()
        .put("changes", JSONArray(items)).put("next_since", next).put("has_more", false)

    @Test fun importReceiptBeforePullKeepsBothMessagesAcrossAnOfflineRestart() {
        val name = "direct-ack-${UUID.randomUUID()}.db"
        try {
            val database = CompanionDatabase(context, name)
            val turn: DirectTurn
            try {
                paired(database)
                turn = completeTurn(database)
                acknowledge(database, turn)
                // No sync.pull has happened; a lost pull must not hide the reply.
                assertExchange(database, turn)
                assertEquals(0, database.historyPendingCount())
            } finally { database.close() }
            val reopened = CompanionDatabase(context, name)
            try { assertExchange(reopened, turn) } finally { reopened.close() }
        } finally { context.deleteDatabase(name) }
    }

    @Test fun unsyncedExchangeSurvivesSnapshotResetAndCanonicalIdsNeverDuplicateIt() {
        val database = CompanionDatabase(context, null)
        try {
            paired(database)
            val turn = completeTurn(database)
            database.stageSnapshotPage(snapshot(emptyList(), 10), true)
            assertExchange(database, turn)
            assertEquals(1, database.historyPendingCount())

            database.applyPull(pull(listOf(conversation(turn).put("seq", 11), message(turn, false).put("seq", 12)), 12))
            // One mirrored user plus the still-local assistant remains two rows.
            assertExchange(database, turn)
            database.applyPull(pull(listOf(message(turn, true).put("seq", 13)), 13))
            assertExchange(database, turn)
            acknowledge(database, turn)
            assertExchange(database, turn)
        } finally { database.close() }
    }

    @Test fun acknowledgedExchangeSurvivesCanonicalSnapshotWithoutRevivingLaterDesktopDeletion() {
        val database = CompanionDatabase(context, null)
        try {
            paired(database)
            val turn = completeTurn(database)
            acknowledge(database, turn)
            database.stageSnapshotPage(snapshot(listOf(conversation(turn), message(turn, false), message(turn, true)), 20), true)
            assertExchange(database, turn)
            database.stageSnapshotPage(snapshot(emptyList(), 21), true)
            assertTrue(database.messages(turn.conversationId).isEmpty())
            assertTrue(database.isConversationDeleted(turn.conversationId))
        } finally { database.close() }
    }

    @Test fun versionTwoUpgradePreservesPairingHistoryAndQueueAndCanSaveDirectTurns() {
        val name = "direct-v2-${UUID.randomUUID()}.db"
        val file = context.getDatabasePath(name)
        file.parentFile!!.mkdirs()
        val cid = UUID.randomUUID().toString()
        val mid = UUID.randomUUID().toString()
        val queued = UUID.randomUUID().toString()
        SQLiteDatabase.openOrCreateDatabase(file, null).use { old ->
            old.execSQL("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            old.execSQL("""CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,local_only INTEGER NOT NULL DEFAULT 0)""")
            old.execSQL("""CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,
                content TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,request_status TEXT,reply_to TEXT,image_id TEXT,image_error TEXT)""")
            old.execSQL("CREATE INDEX messages_conversation ON messages(conversation_id,created_at,id)")
            old.execSQL("CREATE TABLE tombstones(entity TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(entity,id))")
            old.execSQL("CREATE TABLE snapshot_items(entity TEXT NOT NULL,id TEXT NOT NULL,conversation_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(entity,id))")
            old.execSQL("""CREATE TABLE outbox(id TEXT PRIMARY KEY,method TEXT NOT NULL,params TEXT NOT NULL,
                conversation_id TEXT,text TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,message_id TEXT,
                attachment_id TEXT,error TEXT,attempted INTEGER NOT NULL DEFAULT 0,authority_id TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL DEFAULT 0,dependency_id TEXT,hidden INTEGER NOT NULL DEFAULT 0)""")
            old.execSQL("""CREATE TABLE images(id TEXT PRIMARY KEY,filename TEXT NOT NULL,mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,created_at TEXT NOT NULL,
                local_path TEXT,upload_state TEXT NOT NULL DEFAULT 'received')""")
            old.execSQL("CREATE TABLE pending_images(conversation_id TEXT PRIMARY KEY,image_id TEXT NOT NULL,filename TEXT NOT NULL,authority_id TEXT NOT NULL DEFAULT '')")
            old.execSQL("INSERT INTO settings VALUES('device_ticket','public-ticket')")
            old.execSQL("INSERT INTO settings VALUES('device_id','desktop')")
            old.execSQL("INSERT INTO settings VALUES('device_name','验收电脑')")
            old.execSQL("INSERT INTO settings VALUES('local_peer_id','phone')")
            old.execSQL("INSERT INTO settings VALUES('sync_cursor','42')")
            old.execSQL("INSERT INTO settings VALUES('sync_ready','1')")
            old.execSQL("INSERT INTO conversations VALUES(?,'旧版会话','2026-10-31T00:00:00Z','2026-10-31T00:00:00Z',1,0)", arrayOf(cid))
            old.execSQL("INSERT INTO messages VALUES(?,?,'user','升级前已保存的正文','2026-10-31T00:00:00Z','user',NULL,NULL,NULL,NULL)", arrayOf(mid, cid))
            old.execSQL("""INSERT INTO outbox(id,method,params,conversation_id,text,state,created_at,authority_id)
                VALUES(?,'chat.send',?,?,'升级前排队内容','queued','2026-10-31T00:00:00Z','desktop')""",
                arrayOf(queued, JSONObject().put("conversation_id", cid).put("text", "升级前排队内容").toString(), cid))
            old.version = 2
        }
        try {
            val upgraded = CompanionDatabase(context, name)
            try {
            assertEquals(5, upgraded.readableDatabase.version)
                assertEquals("public-ticket", upgraded.device()!!.ticket)
                assertEquals("desktop", upgraded.device()!!.endpointId)
                assertEquals(42L, upgraded.cursor())
            assertFalse(upgraded.initialized()) // Refresh old records once to fetch public process history.
                assertEquals("升级前已保存的正文", upgraded.messages(cid).single { it.id == mid }.content)
                assertEquals("queued", upgraded.operation(queued)!!.item.status)
                assertFalse(upgraded.operation(queued)!!.item.hidden)
                assertTrue(upgraded.directTurns().isEmpty())
                val turn = upgraded.beginDirectTurn(cid, "升级后手机提问", null)
                upgraded.finishDirectTurn(turn.id, "升级后手机回复")
                assertEquals(setOf(mid, queued, turn.userId, turn.assistantId), upgraded.messages(cid).map { it.id }.toSet())
                assertEquals("升级后手机回复", upgraded.messages(cid).single { it.id == turn.assistantId }.content)
            } finally { upgraded.close() }
        } finally { context.deleteDatabase(name) }
    }
}
