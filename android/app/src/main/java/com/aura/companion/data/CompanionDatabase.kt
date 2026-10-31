package com.aura.companion.data

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.UUID

internal fun JSONObject.optionalString(key: String): String? =
    if (has(key) && !isNull(key)) optString(key).takeIf { it.isNotEmpty() } else null

internal data class QueuedOperation(
    val item: OutboxItem, val params: JSONObject, val attempted: Boolean,
    val authorityId: String, val checkedAt: Long,
)

/** A record cache, never a synchronized copy of the desktop's SQLite file. */
class CompanionDatabase(context: Context, name: String? = "companion.db") :
    SQLiteOpenHelper(context.applicationContext, name, null, 1) {

    override fun onConfigure(db: SQLiteDatabase) { db.setForeignKeyConstraintsEnabled(true) }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,
            local_only INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,
            content TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,request_status TEXT,reply_to TEXT)""")
        db.execSQL("CREATE INDEX messages_conversation ON messages(conversation_id,created_at,id)")
        db.execSQL("CREATE TABLE tombstones(entity TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(entity,id))")
        db.execSQL("""CREATE TABLE snapshot_items(entity TEXT NOT NULL,id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(entity,id))""")
        db.execSQL("""CREATE TABLE outbox(id TEXT PRIMARY KEY,method TEXT NOT NULL,params TEXT NOT NULL,
            conversation_id TEXT,text TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,
            message_id TEXT,attachment_id TEXT,error TEXT,attempted INTEGER NOT NULL DEFAULT 0,
            authority_id TEXT NOT NULL DEFAULT '',checked_at INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE images(id TEXT PRIMARY KEY,filename TEXT NOT NULL,mime_type TEXT NOT NULL,
            size INTEGER NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,created_at TEXT NOT NULL,
            local_path TEXT,upload_state TEXT NOT NULL DEFAULT 'received')""")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        error("Unsupported companion database upgrade")
    }

    private fun <T> transaction(block: (SQLiteDatabase) -> T): T {
        val db = writableDatabase
        db.beginTransaction()
        try { val result = block(db); db.setTransactionSuccessful(); return result }
        finally { db.endTransaction() }
    }

    private fun setting(db: SQLiteDatabase, key: String): String? =
        db.rawQuery("SELECT value FROM settings WHERE key=?", arrayOf(key)).use { if (it.moveToFirst()) it.getString(0) else null }

    private fun putSetting(db: SQLiteDatabase, key: String, value: String?) {
        if (value == null) db.delete("settings", "key=?", arrayOf(key))
        else db.insertWithOnConflict("settings", null, ContentValues().apply {
            put("key", key); put("value", value)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun getSetting(key: String): String? = setting(readableDatabase, key)
    fun setSetting(key: String, value: String?) = transaction { putSetting(it, key, value) }
    fun cursor(): Long = getSetting("sync_cursor")?.toLongOrNull() ?: 0L
    fun initialized(): Boolean = getSetting("sync_ready") == "1"
    fun snapshotCursor(): String = getSetting("snapshot_cursor") ?: ""

    fun device(): DeviceInfo? {
        val ticket = getSetting("device_ticket") ?: return null
        return DeviceInfo(getSetting("device_name") ?: "我的电脑", getSetting("device_id") ?: "", ticket)
    }

    fun saveDevice(device: DeviceInfo, localPeer: String) = transaction { db ->
        val old = setting(db, "device_id")
        if (!old.isNullOrBlank() && old != device.endpointId) {
            db.execSQL("""UPDATE outbox SET state='needs_attention',error='已更换电脑，请处理原设备的待发内容'
                WHERE state NOT IN ('sent','discarded')""")
            db.delete("messages", null, null); db.delete("conversations", null, null)
            db.delete("tombstones", null, null); resetSnapshot(db)
            putSetting(db, "sync_ready", null); putSetting(db, "sync_cursor", null)
            putSetting(db, "selected_conversation", null)
        }
        putSetting(db, "device_ticket", device.ticket)
        putSetting(db, "device_id", device.endpointId)
        putSetting(db, "device_name", device.name)
        putSetting(db, "local_peer_id", localPeer)
        putSetting(db, "authorization_blocked", null)
        db.execSQL("UPDATE outbox SET authority_id=? WHERE authority_id=''", arrayOf(device.endpointId))
    }

    fun createConversation(id: String = UUID.randomUUID().toString()): String = transaction { db ->
        require(!isDeleted(db, "conversation", id)) { "该会话已在电脑删除，请新建会话" }
        val now = Instant.now().toString()
        db.insertWithOnConflict("conversations", null, ContentValues().apply {
            put("id", id); put("title", "新对话"); put("created_at", now); put("updated_at", now)
            put("message_count", 0); put("local_only", 1)
        }, SQLiteDatabase.CONFLICT_IGNORE)
        putSetting(db, "selected_conversation", id)
        id
    }

    private fun isDeleted(db: SQLiteDatabase, entity: String, id: String): Boolean =
        db.rawQuery("SELECT 1 FROM tombstones WHERE entity=? AND id=?", arrayOf(entity, id)).use { it.moveToFirst() }

    fun isConversationDeleted(id: String): Boolean = isDeleted(readableDatabase, "conversation", id)

    private fun tombstone(db: SQLiteDatabase, entity: String, id: String) {
        db.insertWithOnConflict("tombstones", null, ContentValues().apply {
            put("entity", entity); put("id", id)
        }, SQLiteDatabase.CONFLICT_IGNORE)
    }

    private fun blockDeletedQueue(db: SQLiteDatabase, id: String) {
        db.execSQL("""UPDATE outbox SET state='needs_attention',error='会话已在电脑删除，请复制内容后新建会话'
            WHERE conversation_id=? AND state NOT IN ('sent','discarded')""", arrayOf(id))
    }

    private fun applyEntity(db: SQLiteDatabase, item: JSONObject) {
        val entity = item.getString("entity")
        val id = item.getString("id")
        val cid = item.getString("conversation_id")
        require(id.length in 1..200 && cid.length in 1..200) { "同步标识无效" }
        if (item.optBoolean("deleted")) {
            tombstone(db, entity, id)
            when (entity) {
                "conversation" -> {
                    db.delete("conversations", "id=?", arrayOf(id))
                    db.delete("messages", "conversation_id=?", arrayOf(id))
                    blockDeletedQueue(db, id)
                }
                "message" -> {
                    db.delete("messages", "id=?", arrayOf(id))
                    db.execSQL("""UPDATE outbox SET state=CASE WHEN state='sent' THEN 'discarded' ELSE 'needs_attention' END,
                        error='该消息已在电脑清空，不能自动重新发送' WHERE message_id=? AND state!='discarded'""", arrayOf(id))
                }
            }
            return
        }
        if (isDeleted(db, entity, id) || isDeleted(db, "conversation", cid)) return
        val data = item.getJSONObject("data")
        when (entity) {
            "conversation" -> db.insertWithOnConflict("conversations", null, ContentValues().apply {
                put("id", id); put("title", data.optString("title", "新对话"))
                put("created_at", data.optString("created_at")); put("updated_at", data.optString("updated_at"))
                put("message_count", data.optInt("message_count")); put("local_only", 0)
            }, SQLiteDatabase.CONFLICT_REPLACE)
            "message" -> {
                db.insertWithOnConflict("messages", null, ContentValues().apply {
                    put("id", id); put("conversation_id", cid); put("role", data.getString("role"))
                    put("content", data.getString("content")); put("created_at", data.getString("created_at"))
                    put("source", data.optString("source", "assistant"))
                    put("request_status", data.optionalString("request_status")); put("reply_to", data.optionalString("reply_to"))
                }, SQLiteDatabase.CONFLICT_REPLACE)
                db.execSQL("""UPDATE outbox SET state='sent',error=NULL WHERE message_id=?
                    AND state NOT IN ('discarded','needs_attention')""", arrayOf(id))
            }
            "screen_event" -> Unit // Comments are ordinary messages; no capture metadata is cached.
            else -> error("不支持的同步实体")
        }
    }

    private fun resetSnapshot(db: SQLiteDatabase) {
        db.delete("snapshot_items", null, null)
        for (key in listOf("snapshot_cursor", "snapshot_watermark", "snapshot_size", "snapshot_count")) putSetting(db, key, null)
    }

    fun resetSnapshot() = transaction { resetSnapshot(it) }

    /** A failed/interrupted first sync leaves the visible old cache intact. */
    fun stageSnapshotPage(page: JSONObject, first: Boolean) = transaction { db ->
        if (first) resetSnapshot(db)
        val watermark = page.getLong("watermark")
        require(watermark >= 0) { "同步水位无效" }
        val previous = setting(db, "snapshot_watermark")?.toLongOrNull()
        require(previous == null || previous == watermark) { "同步快照不一致" }
        putSetting(db, "snapshot_watermark", watermark.toString())
        var size = setting(db, "snapshot_size")?.toLongOrNull() ?: 0
        var count = setting(db, "snapshot_count")?.toLongOrNull() ?: 0
        val items = page.getJSONArray("items")
        require(items.length() <= 100) { "同步页过大" }
        for (index in 0 until items.length()) {
            val item = sanitizeSnapshotItem(items.getJSONObject(index))
            require(!item.optBoolean("deleted")) { "完整快照不能包含删除记录" }
            // Store the public data once, rather than duplicating the entire
            // envelope and its IDs. This matches the desktop's 16 MiB budget.
            val payload = item.getJSONObject("data").toString()
            size += payload.toByteArray(Charsets.UTF_8).size
            count++
            require(size <= 16L * 1024 * 1024 && count <= 100000) { "同步缓存超过容量" }
            db.insertWithOnConflict("snapshot_items", null, ContentValues().apply {
                put("entity", item.getString("entity")); put("id", item.getString("id"))
                put("conversation_id", item.getString("conversation_id")); put("payload", payload)
            }, SQLiteDatabase.CONFLICT_REPLACE)
        }
        putSetting(db, "snapshot_size", size.toString())
        putSetting(db, "snapshot_count", count.toString())
        val next = page.optString("next_cursor")
        require(!page.getBoolean("has_more") || next.isNotBlank()) { "同步分页游标缺失" }
        putSetting(db, "snapshot_cursor", next)
        if (!page.getBoolean("has_more")) {
            db.rawQuery("""SELECT id FROM conversations WHERE local_only=0 AND id NOT IN
                (SELECT id FROM snapshot_items WHERE entity='conversation')""", null).use { old ->
                while (old.moveToNext()) { tombstone(db, "conversation", old.getString(0)); blockDeletedQueue(db, old.getString(0)) }
            }
            db.execSQL("""UPDATE outbox SET state=CASE WHEN state='sent' THEN 'discarded' ELSE 'needs_attention' END,
                error='该消息已在电脑清空，不能自动重新发送' WHERE message_id IN
                (SELECT id FROM messages WHERE id NOT IN (SELECT id FROM snapshot_items WHERE entity='message'))
                AND state!='discarded'""")
            db.delete("messages", null, null)
            db.delete("conversations", "local_only=0", null)
            db.rawQuery("SELECT * FROM snapshot_items ORDER BY CASE entity WHEN 'conversation' THEN 0 ELSE 1 END,id", null).use {
                while (it.moveToNext()) applyEntity(db, JSONObject().put("entity", it.getString(0))
                    .put("id", it.getString(1)).put("conversation_id", it.getString(2)).put("deleted", false)
                    .put("data", JSONObject(it.getString(3))))
            }
            putSetting(db, "sync_cursor", watermark.toString())
            putSetting(db, "sync_ready", "1")
            resetSnapshot(db)
        }
        Unit
    }

    private fun sanitizeSnapshotItem(item: JSONObject): JSONObject {
        val entity = item.getString("entity")
        val fields = when (entity) {
            "conversation" -> listOf("id", "title", "created_at", "updated_at", "message_count")
            "message" -> listOf("id", "conversation_id", "role", "content", "created_at", "source", "request_status", "reply_to")
            "screen_event" -> listOf("id", "conversation_id", "observed_at", "source_kind")
            else -> throw IllegalArgumentException("不支持的同步实体")
        }
        val source = item.getJSONObject("data")
        val data = JSONObject()
        for (field in fields) if (source.has(field)) data.put(field, source.get(field))
        return JSONObject().put("entity", entity).put("id", item.getString("id"))
            .put("conversation_id", item.getString("conversation_id")).put("deleted", item.optBoolean("deleted"))
            .put("data", data)
    }

    /** Public records and cursor commit together. Duplicate pages are harmless. */
    fun applyPull(page: JSONObject) = transaction { db ->
        val old = setting(db, "sync_cursor")?.toLongOrNull() ?: 0L
        val changes = page.getJSONArray("changes")
        require(changes.length() <= 100) { "同步页过大" }
        var latest = old
        var previous = -1L
        for (index in 0 until changes.length()) {
            val item = changes.getJSONObject(index)
            val seq = item.getLong("seq")
            require(seq > previous && seq > 0) { "同步序列无效" }
            previous = seq
            if (seq <= old) continue
            applyEntity(db, item); latest = seq
        }
        val next = page.getLong("next_since")
        if (latest == old && next <= old) return@transaction // Retry of an already committed page.
        require(next >= old && next == latest) { "同步游标不一致" }
        putSetting(db, "sync_cursor", next.toString())
    }

    fun addOutbox(method: String, params: JSONObject, conversationId: String? = null,
                  text: String = "", attachmentId: String? = null): String = transaction { db ->
        val count = db.rawQuery("SELECT count(*) FROM outbox WHERE state NOT IN ('sent','discarded')", null).use { it.moveToFirst(); it.getInt(0) }
        require(count < 1000) { "待发队列已满，请先处理旧消息" }
        val id = UUID.randomUUID().toString()
        val deleted = conversationId != null && isDeleted(db, "conversation", conversationId)
        db.insertOrThrow("outbox", null, ContentValues().apply {
            put("id", id); put("method", method); put("params", params.toString()); put("conversation_id", conversationId)
            put("text", text); put("attachment_id", attachmentId); put("state", if (deleted) "needs_attention" else "queued")
            put("created_at", Instant.now().toString()); put("authority_id", setting(db, "device_id") ?: "")
            if (deleted) put("error", "会话已在电脑删除，请新建会话")
        })
        db.execSQL("""DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE state IN ('sent','discarded')
            ORDER BY created_at DESC LIMIT -1 OFFSET 200)""")
        id
    }

    private fun Cursor.outboxItem(): OutboxItem = OutboxItem(
        getString(getColumnIndexOrThrow("id")), getString(getColumnIndexOrThrow("method")),
        nullable("conversation_id"), getString(getColumnIndexOrThrow("text")), getString(getColumnIndexOrThrow("state")),
        getString(getColumnIndexOrThrow("created_at")), nullable("error"), nullable("message_id"), nullable("attachment_id"))

    private fun Cursor.nullable(column: String): String? = getColumnIndexOrThrow(column).let { if (isNull(it)) null else getString(it) }

    internal fun operation(id: String): QueuedOperation? = readableDatabase.rawQuery("SELECT * FROM outbox WHERE id=?", arrayOf(id)).use {
        if (!it.moveToFirst()) null else QueuedOperation(it.outboxItem(), JSONObject(it.getString(it.getColumnIndexOrThrow("params"))),
            it.getInt(it.getColumnIndexOrThrow("attempted")) != 0, it.getString(it.getColumnIndexOrThrow("authority_id")),
            it.getLong(it.getColumnIndexOrThrow("checked_at")))
    }

    internal fun outgoing(): List<QueuedOperation> = outbox().filter { it.status in setOf("queued", "sending", "unknown", "pending") }
        .sortedBy { it.createdAt }.take(20).mapNotNull { operation(it.requestId) }

    fun updateOutbox(id: String, state: String, error: String? = null, messageId: String? = null,
                     attempted: Boolean? = null, checkedAt: Long? = null) {
        writableDatabase.update("outbox", ContentValues().apply {
            put("state", state); put("error", error)
            if (messageId != null) put("message_id", messageId)
            if (attempted != null) put("attempted", if (attempted) 1 else 0)
            if (checkedAt != null) put("checked_at", checkedAt)
        }, "id=?", arrayOf(id))
    }

    fun outbox(): List<OutboxItem> = readableDatabase.rawQuery("SELECT * FROM outbox ORDER BY created_at DESC LIMIT 1200", null).use {
        buildList { while (it.moveToNext()) add(it.outboxItem()) }
    }

    fun hasMessage(id: String): Boolean = readableDatabase.rawQuery("SELECT 1 FROM messages WHERE id=?", arrayOf(id)).use { it.moveToFirst() }

    fun conversations(): List<Conversation> = readableDatabase.rawQuery("SELECT * FROM conversations ORDER BY updated_at DESC,id LIMIT 500", null).use {
        buildList { while (it.moveToNext()) add(Conversation(it.getString(0), it.getString(1), it.getString(2), it.getString(3), it.getInt(4), it.getInt(5) == 1)) }
    }

    fun messages(conversationId: String?): List<Message> {
        // A purged conversation has no visible thread, including optimistic
        // drafts. Its unsent text remains exclusively in the attention queue.
        if (conversationId == null || isConversationDeleted(conversationId)) return emptyList()
        val mirrored = readableDatabase.rawQuery("""SELECT * FROM messages WHERE conversation_id=?
            ORDER BY created_at DESC,id DESC LIMIT 500""", arrayOf(conversationId)).use {
            buildList { while (it.moveToNext()) add(Message(it.getString(0), it.getString(1), it.getString(2),
                it.getString(3), it.getString(4), it.getString(5), it.nullable("request_status"), it.nullable("reply_to"))) }
        }
        val ids = mirrored.map { it.id }.toSet()
        val pending = outbox().filter { it.conversationId == conversationId && it.method in setOf("chat.send", "image.upload") && it.status != "discarded" }
            .filter { it.messageId !in ids && (it.messageId == null || !hasMessage(it.messageId)) }
            .map { Message(it.messageId ?: it.requestId, conversationId, "user",
                if (it.method == "image.upload") it.text + "\n仅传入电脑图片收件箱，不会自动识图或回复" else it.text,
                it.createdAt, source = if (it.method == "image.upload") "image_inbox" else "user",
                localState = it.status, attachmentId = it.attachmentId) }
        return (mirrored + pending).sortedWith(compareBy<Message> { it.createdAt }.thenBy { it.id }).takeLast(500)
    }

    fun putImage(image: ImageItem) {
        val previousPath = imagePath(image.id)
        writableDatabase.insertWithOnConflict("images", null, ContentValues().apply {
            put("id", image.id); put("filename", image.filename); put("mime_type", image.mimeType); put("size", image.size)
            put("width", image.width); put("height", image.height); put("created_at", image.createdAt)
            put("local_path", image.localPath ?: previousPath); put("upload_state", image.uploadState)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun imagePath(id: String): String? = readableDatabase.rawQuery("SELECT local_path FROM images WHERE id=?", arrayOf(id)).use {
        if (it.moveToFirst() && !it.isNull(0)) it.getString(0) else null
    }

    fun clearImagePath(id: String) { writableDatabase.execSQL("UPDATE images SET local_path=NULL WHERE id=?", arrayOf(id)) }

    fun image(id: String): ImageItem? = images().firstOrNull { it.id == id }

    fun images(): List<ImageItem> = readableDatabase.rawQuery("SELECT * FROM images ORDER BY created_at DESC LIMIT 1000", null).use {
        buildList { while (it.moveToNext()) add(ImageItem(it.getString(0), it.getString(1), it.getString(2), it.getLong(3),
            it.getInt(4), it.getInt(5), it.getString(6), it.nullable("local_path"), it.getString(8))) }
    }

    fun deleteImage(id: String) { writableDatabase.delete("images", "id=?", arrayOf(id)) }

    fun reconcileImages(remoteIds: Set<String>) {
        val protected = outbox().filter { it.status !in setOf("sent", "discarded") }.mapNotNull { it.attachmentId }.toSet()
        for (image in images()) if (image.id !in remoteIds && image.id !in protected && image.uploadState in setOf("received", "uploaded")) {
            deleteImage(image.id)
        }
    }
}
