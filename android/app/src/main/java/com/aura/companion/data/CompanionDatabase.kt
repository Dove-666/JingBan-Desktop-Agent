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

internal const val LOCAL_PHONE_AUTHORITY = "LOCAL_PHONE"

/** An immediate local label, without another model request or a transcript copy. */
internal fun phoneConversationTitle(text: String, hasImage: Boolean): String {
    val plain = text.replace(Regex("[\\p{Cc}\\p{Z}\\s]+"), " ").trim()
    if (plain.isEmpty()) return if (hasImage) "图片对话" else "新对话"
    val length = plain.codePointCount(0, plain.length)
    return if (length <= 24) plain else plain.substring(0, plain.offsetByCodePoints(0, 24)) + "…"
}

internal val AGENT_ERROR_MESSAGES = mapOf(
    "disabled" to "任务助手尚未启用，请在设置中启用后再试。",
    "kind_disabled" to "这类任务尚未启用，请先检查任务助手设置。",
    "not_configured" to "请先在设置中配置模型与 API。",
    "vision_rejected" to "图文请求未被模型接口接受，请确认模型支持图片并检查接口配置。",
    "missing_backend" to "未找到任务助手后端，请先完成后端安装。",
    "missing_sdk" to "电脑任务组件不完整，请更新电脑端。",
    "sdk_unlimited_unsupported" to "当前任务 SDK 与不限步数模式不兼容，任务未执行；请检查本地后端版本。",
    "gui_not_configured" to "请在电脑端配置支持看图和工具调用的模型。",
    "cancelled" to "任务已停止；已经完成的操作会保留，未自动重试。",
    "stop_unconfirmed" to "正在等待电脑操作结束，暂时无法确认已停止。请稍后检查任务状态。",
    "timeout" to "任务超时，本次等待已结束。已完成的操作会保留，本次未自动重试。",
    "protocol" to "任务助手返回格式无效，已停止且未自动重试。",
    "backend_failed" to "任务助手未能完成这次操作；请检查当前结果，再发送新的任务消息。",
    "agent_failed" to "任务规划模型请求失败；请检查模型服务是否可用，再发送新的任务消息。",
    "empty_reply" to "任务助手没有返回结果说明；请检查已完成的操作，再发送新的任务消息。",
    "computer_not_used" to "任务助手没有执行电脑操作；请明确要操作的应用和目标后发送新消息。",
    "gui_runtime_missing" to "电脑操作需要 Node.js，当前任务环境找不到它；安装或修复后请重启桌宠。",
    "gui_start_failed" to "电脑操作组件未能启动，请更新或重启电脑端。",
    "gui_exited" to "电脑操作后台意外退出；请检查当前结果，再发送新的任务消息。",
    "gui_protocol" to "电脑操作后台返回格式无效，任务已停止；请检查当前结果。",
    "gui_failed" to "电脑操作未能完成；请检查当前画面，再发送更明确的任务消息。",
    "gui_sdk_unavailable" to "电脑操作组件尚未就绪，请更新电脑端。",
    "gui_unsupported_platform" to "当前电脑操作组件只支持 Windows。",
    "gui_step_limit" to "电脑操作已达到本次步数上限，仍未确认完成；请检查画面，再发送更具体的任务。",
    "gui_screenshot_failed" to "无法获取桌面画面，电脑操作已停止；请确认桌面会话可用。",
    "gui_desktop_locked" to "电脑已锁屏或正在显示安全桌面；请在电脑上解锁或完成安全提示后再发送任务。",
    "gui_desktop_session_unavailable" to "电脑桌面会话已断开或不可用；请恢复 Windows 桌面后再发送任务。",
    "gui_screenshot_display_unavailable" to "目标显示器不可用或已发生切换；电脑操作已停止，请检查显示器后再发送任务。",
    "gui_screenshot_black_frame" to "截图返回近乎全黑的不可用画面，电脑操作已停止；请恢复可见桌面后再发送任务。",
    "gui_model_failed" to "电脑操作模型请求失败；请检查视觉模型服务、密钥和动作格式是否可用。",
    "gui_context_length_exceeded" to "电脑操作上下文已达到模型服务的实际容量；请新建任务或选择容量更大的模型。",
    "model_context_exceeded" to "任务上下文已达到模型服务的实际容量；请新建对话或选择容量更大的模型。",
    "execution_runtime_invalid" to "项目计算环境尚未就绪，任务未继续；请在电脑上检查本地执行环境。",
    "execution_runtime_changed" to "项目计算配置已发生变化，任务已停止；请确认电脑端设置后发送新任务。",
    "gui_action_failed" to "鼠标或键盘操作执行失败，任务已停止；请检查当前画面。",
    "gui_unsupported_action" to "模型返回了尚不支持的电脑动作，任务已停止；请检查当前画面。",
    "gui_sdk_failed" to "电脑操作组件未能完成任务；请检查画面，再发送新的任务消息。",
    "gui_invalid_request" to "电脑操作参数无效；请检查任务助手中的模型类型和动作格式。",
    "gui_invalid_provider" to "电脑操作的动作格式不受支持；请检查任务助手设置。",
    "gui_busy" to "上一项电脑操作尚未结束；请稍后发送新任务。",
    "gui_worker_failed" to "电脑操作后台运行失败；请检查当前结果，再发送新的任务消息。",
    "gui_already_used" to "本次电脑操作已经执行，未再次执行；请检查结果后发送新任务。",
    "closed" to "任务助手已关闭。",
    "busy" to "上一项任务仍在运行或结束中，请稍后发送新任务；本次未启动任务助手。",
    "session_unavailable" to "任务会话暂时无法恢复，请检查电脑本地数据；已完成的操作不会重复执行。",
    "provider_auth_failed" to "模型服务未接受当前凭据，请检查 API 配置。",
    "provider_rate_limited" to "模型服务暂时限流，请稍后继续；已完成的操作已保留。",
    "provider_unavailable" to "模型服务暂时不可用；已完成的操作已保留。",
    "request_timeout" to "模型或网络请求未及时返回；已完成的操作已保留。",
)

internal fun validAgentErrorCode(value: String?): String? = value?.takeIf { it in AGENT_ERROR_MESSAGES }

private val AGENT_ACTIVITY_MESSAGES = setOf(
    "正在保存消息…", "正在检索相关记忆…", "正在生成回复…", "正在保存回复…",
    "正在启动任务助手…", "正在规划任务…", "正在使用任务工具…", "正在操作电脑…",
    "正在整理任务结果…", "正在核验任务结果…", "任务已完成。",
    "查看系统信息", "读取文件", "写入文件", "查找文件", "读取网页", "执行电脑操作", "打开应用", "查找已安装应用",
    "检查电脑状态", "检查磁盘占用", "查看目录信息",
    "查看当前画面", "查看电脑画面", "点击界面", "双击界面", "右键点击", "中键点击", "拖动界面元素", "选择界面元素",
    "移动指针", "输入文字", "使用快捷键", "滚动界面", "等待界面响应", "电脑操作返回结果",
    "需要手动接手", "检查操作环境", "停止电脑操作",
    "读取任务记录", "修改文件", "读取文档", "生成文档", "查看交付文件", "搜索网页", "查看历史图片",
    "等待你的答复", "等待文件授权", "查找电脑窗口", "切换电脑窗口", "原生浏览器", "渲染文档",
    "操作界面控件", "更新任务计划", "核对控件状态", "核算表格数据", "检查任务完成条件", "精确计算", "执行数据代码",
    "读取界面控件", "验收输出结果", "确认操作结果",
    "切换网页标签", "打开网页", "操作网页控件", "查看网页标签", "读取网页结构",
    "分工阅读资料", "查看文档页面", "计算项目产物", "检查工具请求",
    "编辑文件", "搜索文件内容", "执行命令", "查看图片", "查看工作目录", "交付文件", "分派任务",
    "协调子任务", "查看子任务", "等待子任务", "停止子任务", "操作电脑", "使用任务工具", "阅读网页",
)
internal fun validAgentActivity(value: String?): String? = value?.takeIf { it in AGENT_ACTIVITY_MESSAGES }


internal fun JSONObject.optionalString(key: String): String? =
    if (has(key) && !isNull(key)) optString(key).takeIf { it.isNotEmpty() } else null

internal data class QueuedOperation(
    val item: OutboxItem, val params: JSONObject, val attempted: Boolean,
    val authorityId: String, val checkedAt: Long, val dependencyId: String? = null,
)

internal data class DirectTurn(val id: String, val conversationId: String, val authorityId: String,
    val userId: String, val assistantId: String, val text: String, val userAt: String,
    val reply: String?, val assistantAt: String?, val imageId: String?, val state: String,
    val error: String?, val importRequestId: String?, val execution: ExecutionProcess? = null)

/** A record cache, never a synchronized copy of the desktop's SQLite file. */
class CompanionDatabase(context: Context, name: String? = "companion.db") :
    SQLiteOpenHelper(context.applicationContext, name, null, 5) {

    override fun onConfigure(db: SQLiteDatabase) { db.setForeignKeyConstraintsEnabled(true) }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execSQL("""CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,
            local_only INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,
            content TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,request_status TEXT,reply_to TEXT,image_id TEXT,image_error TEXT,
            agent_error_code TEXT,agent_activity TEXT,execution_mode TEXT,agent_trace TEXT)""")
        db.execSQL("CREATE INDEX messages_conversation ON messages(conversation_id,created_at,id)")
        db.execSQL("CREATE TABLE tombstones(entity TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(entity,id))")
        db.execSQL("""CREATE TABLE snapshot_items(entity TEXT NOT NULL,id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(entity,id))""")
        db.execSQL("""CREATE TABLE outbox(id TEXT PRIMARY KEY,method TEXT NOT NULL,params TEXT NOT NULL,
            conversation_id TEXT,text TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,
            message_id TEXT,attachment_id TEXT,error TEXT,attempted INTEGER NOT NULL DEFAULT 0,
            authority_id TEXT NOT NULL DEFAULT '',checked_at INTEGER NOT NULL DEFAULT 0,
            dependency_id TEXT,hidden INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE images(id TEXT PRIMARY KEY,filename TEXT NOT NULL,mime_type TEXT NOT NULL,
            size INTEGER NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,created_at TEXT NOT NULL,
            local_path TEXT,upload_state TEXT NOT NULL DEFAULT 'received')""")
        createPendingImages(db)
        createDirectTurns(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            db.execSQL("ALTER TABLE messages ADD COLUMN image_id TEXT")
            db.execSQL("ALTER TABLE messages ADD COLUMN image_error TEXT")
            db.execSQL("ALTER TABLE outbox ADD COLUMN dependency_id TEXT")
            db.execSQL("ALTER TABLE outbox ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            createPendingImages(db)
        }
        if (oldVersion < 3) createDirectTurns(db)
        if (oldVersion < 4) {
            db.execSQL("ALTER TABLE messages ADD COLUMN agent_error_code TEXT")
            db.execSQL("ALTER TABLE messages ADD COLUMN agent_activity TEXT")
            db.execSQL("ALTER TABLE messages ADD COLUMN execution_mode TEXT")
        }
        if (oldVersion < 5) {
            db.execSQL("ALTER TABLE messages ADD COLUMN agent_trace TEXT")
            if (oldVersion >= 3) db.execSQL("ALTER TABLE direct_turns ADD COLUMN process_json TEXT")
            // Existing visible rows remain available offline. A fresh snapshot
            // backfills traces for terminal desktop messages after upgrading.
            putSetting(db, "sync_ready", null)
            resetSnapshot(db)
        }
    }

    private fun createDirectTurns(db: SQLiteDatabase) {
        // Transcripts are normal chat data. Credentials/configuration never enter SQLite.
        db.execSQL("""CREATE TABLE direct_turns(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
            authority_id TEXT NOT NULL,user_id TEXT NOT NULL UNIQUE,assistant_id TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,user_at TEXT NOT NULL,reply TEXT,assistant_at TEXT,image_id TEXT,
            state TEXT NOT NULL,error TEXT,import_request_id TEXT,process_json TEXT)""")
        db.execSQL("CREATE INDEX direct_turns_conversation ON direct_turns(conversation_id,user_at)")
    }

    private fun createPendingImages(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE pending_images(conversation_id TEXT PRIMARY KEY,image_id TEXT NOT NULL,filename TEXT NOT NULL,
            authority_id TEXT NOT NULL DEFAULT '')""")
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

    internal fun clearContextEnvelopes(conversationId: String? = null) = transaction { db ->
        if (conversationId == null) db.delete("settings", "key LIKE 'context_envelope:%'", null)
        else db.delete("settings", "key LIKE ?", arrayOf("context_envelope:%:$conversationId"))
        Unit
    }
    private fun invalidateContext(db: SQLiteDatabase, conversationId: String) {
        db.delete("settings", "key LIKE ?", arrayOf("context_envelope:%:$conversationId"))
        putSetting(db, "context_epoch:$conversationId", UUID.randomUUID().toString())
    }
    internal fun contextEpoch(conversationId: String): String = getSetting("context_epoch:$conversationId").orEmpty()
    internal fun cacheContextEnvelope(authority: String, cid: String, generation: String?, epoch: String,
        encoded: String) = transaction { db ->
        require(setting(db, "device_id") == authority && !isDeleted(db, "conversation", cid) &&
            setting(db, "authorization_blocked") != "1" && conversationGeneration(cid) == generation &&
            setting(db, "context_epoch:$cid").orEmpty() == epoch) { "上下文已变化，请重新同步" }
        putSetting(db, "context_envelope:$authority:$cid", encoded)
    }
    fun setDesktopAgentMode(conversationId: String, enabled: Boolean) = transaction { db ->
        require(!isDeleted(db, "conversation", conversationId)) { "该会话已删除，请新建会话" }
        putSetting(db, "agent_mode:$conversationId", if (enabled) "1" else null)
    }
    fun desktopAgentConversations(): Set<String> = readableDatabase.rawQuery(
        "SELECT key FROM settings WHERE key LIKE 'agent_mode:%' AND value='1'", null).use {
        buildSet { while (it.moveToNext()) add(it.getString(0).removePrefix("agent_mode:")) }
    }
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
            db.delete("settings", "key LIKE 'context_envelope:%'", null)
            db.delete("settings", "key LIKE 'agent_mode:%'", null)
            db.delete("settings", "key LIKE 'host_title_manual:%' OR key LIKE 'phone_title:%'", null)
            db.execSQL("""UPDATE outbox SET state='needs_attention',error='已更换电脑，请处理原设备的待发内容'
                WHERE state NOT IN ('sent','discarded')""")
            db.delete("messages", null, null)
            db.execSQL("DELETE FROM conversations WHERE id NOT IN (SELECT conversation_id FROM direct_turns WHERE authority_id=?)", arrayOf(LOCAL_PHONE_AUTHORITY))
            db.execSQL("UPDATE conversations SET local_only=1 WHERE id IN (SELECT conversation_id FROM direct_turns WHERE authority_id=?)", arrayOf(LOCAL_PHONE_AUTHORITY))
            db.delete("tombstones", null, null); resetSnapshot(db)
            db.execSQL("UPDATE direct_turns SET state='needs_attention',error='已更换电脑，原记录不会自动发送到新电脑' WHERE state!='imported' AND authority_id!=?", arrayOf(LOCAL_PHONE_AUTHORITY))
            putSetting(db, "sync_ready", null); putSetting(db, "sync_cursor", null)
            putSetting(db, "selected_conversation", null)
        }
        putSetting(db, "device_ticket", device.ticket)
        putSetting(db, "device_id", device.endpointId)
        putSetting(db, "device_name", device.name)
        putSetting(db, "local_peer_id", localPeer)
        putSetting(db, "authorization_blocked", null)
        db.execSQL("UPDATE outbox SET authority_id=? WHERE authority_id=''", arrayOf(device.endpointId))
        db.execSQL("UPDATE pending_images SET authority_id=? WHERE authority_id=''", arrayOf(device.endpointId))
        // Explicit pairing authorizes only this phone's still-unbound history.
        db.execSQL("UPDATE direct_turns SET authority_id=? WHERE authority_id=?", arrayOf(device.endpointId, LOCAL_PHONE_AUTHORITY))
    }

    fun createConversation(id: String = UUID.randomUUID().toString(), select: Boolean = true): String = transaction { db ->
        require(!isDeleted(db, "conversation", id)) { "该会话已在电脑删除，请新建会话" }
        val now = Instant.now().toString()
        val inserted = db.insertWithOnConflict("conversations", null, ContentValues().apply {
            put("id", id); put("title", "新对话"); put("created_at", now); put("updated_at", now)
            put("message_count", 0); put("local_only", 1)
        }, SQLiteDatabase.CONFLICT_IGNORE)
        if (inserted != -1L) invalidateContext(db, id)
        if (select) putSetting(db, "selected_conversation", id)
        id
    }

    private fun hostTitleManual(db: SQLiteDatabase, cid: String): Boolean? =
        setting(db, "host_title_manual:$cid")?.let { value -> runCatching {
            JSONObject(value).takeIf { it.optString("peer") == setting(db, "device_id") }
                ?.opt("manual") as? Boolean
        }.getOrNull() }

    private fun restorePhoneTitle(db: SQLiteDatabase, cid: String) {
        val source = setting(db, "phone_title:$cid")?.let { runCatching { JSONObject(it) }.getOrNull() } ?: return
        val label = source.optString("title")
        val generation = source.optString("generation")
        if (generation.isEmpty() || generation != conversationGeneration(cid) || isDeleted(db, "message", generation)) {
            putSetting(db, "phone_title:$cid", null)
            db.execSQL("UPDATE conversations SET title='新对话' WHERE id=? AND title=?", arrayOf(cid, label))
            return
        }
        if (hostTitleManual(db, cid) == false) db.execSQL("""UPDATE conversations SET title=? WHERE id=?
            AND trim(title) IN ('','新对话')""", arrayOf(label, cid))
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
        db.delete("settings", "key LIKE ?", arrayOf("context_envelope:%:$id"))
        putSetting(db, "agent_mode:$id", null)
        putSetting(db, "phone_title:$id", null)
        putSetting(db, "host_title_manual:$id", null)
        db.execSQL("""UPDATE outbox SET state='needs_attention',error='会话已在电脑删除，请复制内容后新建会话'
            WHERE conversation_id=? AND state NOT IN ('sent','discarded')""", arrayOf(id))
        db.execSQL("UPDATE direct_turns SET state='needs_attention',error='会话已在电脑删除，请复制内容后新建会话' WHERE conversation_id=? AND state!='imported'", arrayOf(id))
    }

    private fun applyEntity(db: SQLiteDatabase, item: JSONObject, resolveTitle: Boolean = true) {
        val entity = item.getString("entity")
        val id = item.getString("id")
        val cid = item.getString("conversation_id")
        require(id.length in 1..200 && cid.length in 1..200) { "同步标识无效" }
        if (entity in setOf("conversation", "message")) invalidateContext(db, if (entity == "conversation") id else cid)
        if (item.optBoolean("deleted")) {
            tombstone(db, entity, id)
            when (entity) {
                "conversation" -> {
                    putSetting(db, "agent_mode:$id", null)
                    db.delete("conversations", "id=?", arrayOf(id))
                    db.delete("messages", "conversation_id=?", arrayOf(id))
                    blockDeletedQueue(db, id)
                }
                "message" -> {
                    db.delete("messages", "id=?", arrayOf(id))
                    db.execSQL("""UPDATE outbox SET state=CASE WHEN state='sent' THEN 'discarded' ELSE 'needs_attention' END,
                        error='该消息已在电脑清空，不能自动重新发送' WHERE message_id=? AND state!='discarded'""", arrayOf(id))
                    restorePhoneTitle(db, cid)
                }
            }
            return
        }
        if (isDeleted(db, entity, id) || isDeleted(db, "conversation", cid)) return
        val data = item.getJSONObject("data")
        when (entity) {
            "conversation" -> {
                val title = data.optString("title", "新对话")
                val manual = data.opt("title_manual") as? Boolean
                putSetting(db, "host_title_manual:$id", manual?.let { JSONObject()
                    .put("peer", setting(db, "device_id")).put("manual", it).toString() })
                if (manual != false || title.trim() !in setOf("", "新对话")) putSetting(db, "phone_title:$id", null)
                db.insertWithOnConflict("conversations", null, ContentValues().apply {
                    put("id", id); put("title", title)
                    put("created_at", data.optString("created_at")); put("updated_at", data.optString("updated_at"))
                    put("message_count", data.optInt("message_count")); put("local_only", 0)
                }, SQLiteDatabase.CONFLICT_REPLACE)
                if (resolveTitle) restorePhoneTitle(db, id)
            }
            "message" -> {
                db.insertWithOnConflict("messages", null, ContentValues().apply {
                    put("id", id); put("conversation_id", cid); put("role", data.getString("role"))
                    put("content", data.getString("content")); put("created_at", data.getString("created_at"))
                    put("source", data.optString("source", "assistant"))
                    put("request_status", data.optionalString("request_status")); put("reply_to", data.optionalString("reply_to"))
                    put("image_id", validImageId(data.optionalString("image_id")))
                    put("image_error", validImageError(data.optionalString("image_error")))
                    put("agent_error_code", validAgentErrorCode(data.optionalString("agent_error_code")))
                    put("agent_activity", validAgentActivity(data.optionalString("agent_activity")))
                    put("execution_mode", data.optionalString("execution_mode")?.takeIf { it == "agent" })
                    put("agent_trace", if (data.optString("role") == "user" && data.optString("execution_mode") == "agent")
                        parseExecutionProcess(data.opt("agent_trace"))?.canonicalJson() else null)
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
                    .put("data", JSONObject(it.getString(3))), resolveTitle = false)
            }
            // Conversation rows precede message rows. Re-check generation only
            // after the complete replacement snapshot has loaded its messages.
            val titleConversations = db.rawQuery("SELECT key FROM settings WHERE key LIKE 'phone_title:%'", null).use {
                buildList { while (it.moveToNext()) add(it.getString(0).removePrefix("phone_title:")) }
            }
            for (cid in titleConversations) restorePhoneTitle(db, cid)
            putSetting(db, "sync_cursor", watermark.toString())
            putSetting(db, "sync_ready", "1")
            resetSnapshot(db)
        }
        Unit
    }

    private fun sanitizeSnapshotItem(item: JSONObject): JSONObject {
        val entity = item.getString("entity")
        val fields = when (entity) {
            "conversation" -> listOf("id", "title", "title_manual", "created_at", "updated_at", "message_count")
            "message" -> listOf("id", "conversation_id", "role", "content", "created_at", "source", "request_status", "reply_to", "image_id", "image_error", "agent_error_code", "agent_activity", "execution_mode")
            "screen_event" -> listOf("id", "conversation_id", "observed_at", "source_kind")
            else -> throw IllegalArgumentException("不支持的同步实体")
        }
        val source = item.getJSONObject("data")
        val data = JSONObject()
        for (field in fields) if (source.has(field)) data.put(field, source.get(field))
        if (entity == "message") {
            data.put("image_id", validImageId(source.optionalString("image_id")))
            data.put("image_error", validImageError(source.optionalString("image_error")))
            data.put("agent_error_code", validAgentErrorCode(source.optionalString("agent_error_code")))
            data.put("agent_activity", validAgentActivity(source.optionalString("agent_activity")))
            data.put("execution_mode", source.optionalString("execution_mode")?.takeIf { it == "agent" })
            val trace = if (source.optString("role") == "user" && source.optString("execution_mode") == "agent")
                parseExecutionProcess(source.opt("agent_trace")) else null
            data.put("agent_trace", trace?.let { JSONObject(it.canonicalJson()) })
        }
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
                  text: String = "", attachmentId: String? = null, dependencyId: String? = null): String = transaction { db ->
        addOutbox(db, method, params, conversationId, text, attachmentId, dependencyId)
    }

    private fun addOutbox(db: SQLiteDatabase, method: String, params: JSONObject, conversationId: String?,
                          text: String, attachmentId: String?, dependencyId: String? = null, hidden: Boolean = false): String {
        val count = db.rawQuery("SELECT count(*) FROM outbox WHERE state NOT IN ('sent','discarded')", null).use { it.moveToFirst(); it.getInt(0) }
        require(count < 1000) { "待发队列已满，请先处理旧消息" }
        val id = UUID.randomUUID().toString()
        val deleted = conversationId != null && isDeleted(db, "conversation", conversationId)
        db.insertOrThrow("outbox", null, ContentValues().apply {
            put("id", id); put("method", method); put("params", params.toString()); put("conversation_id", conversationId)
            put("text", text); put("attachment_id", attachmentId); put("state", if (deleted) "needs_attention" else "queued")
            put("dependency_id", dependencyId); put("hidden", if (hidden) 1 else 0)
            put("created_at", Instant.now().toString()); put("authority_id", setting(db, "device_id") ?: "")
            if (deleted) put("error", "会话已在电脑删除，请新建会话")
        })
        db.execSQL("""DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE state IN ('sent','discarded')
            AND id NOT IN (SELECT dependency_id FROM outbox WHERE dependency_id IS NOT NULL)
            ORDER BY created_at DESC LIMIT -1 OFFSET 200)""")
        return id
    }

    /** The composer is consumed only after both durable operations are committed. */
    fun queueMessage(conversationId: String, text: String, desktopContext: Boolean = false): String =
        queueChatMessage(conversationId, text, "chat.send", desktopContext)

    fun queueAgentMessage(conversationId: String, text: String): String =
        queueChatMessage(conversationId, text, "agent.send")

    private fun queueChatMessage(conversationId: String, text: String, method: String, desktopContext: Boolean = false): String = transaction { db ->
        require(!isDeleted(db, "conversation", conversationId)) { "该会话已在电脑删除，请新建会话" }
        val pending = pendingImage(conversationId)
        val params = JSONObject().put("conversation_id", conversationId).put("text", text)
        require(!desktopContext || pending == null) { "电脑协助会读取电脑当前画面，请先移除输入框图片后再发送" }
        if (desktopContext) params.put("desktop_context", true)
        var dependency: String? = null
        if (pending != null) {
            val owner = db.rawQuery("SELECT authority_id FROM pending_images WHERE conversation_id=?", arrayOf(conversationId)).use {
                it.moveToFirst(); it.getString(0)
            }
            require(owner.isBlank() || owner == setting(db, "device_id")) { "待发图片属于原电脑，请移除后重新选择" }
            dependency = addOutbox(db, "image.upload", JSONObject().put("filename", pending.filename)
                .put("mime_type", pending.mimeType).put("sha256", pending.attachmentId), conversationId,
                "上传图片：${pending.filename}", pending.attachmentId, hidden = true)
            params.put("image_id", pending.attachmentId)
        }
        val requestId = addOutbox(db, method, params, conversationId, text, pending?.attachmentId, dependency)
        invalidateContext(db, conversationId)
        db.delete("pending_images", "conversation_id=?", arrayOf(conversationId))
        requestId
    }

    fun stageImage(conversationId: String, image: ImageItem) = transaction { db ->
        require(!isDeleted(db, "conversation", conversationId)) { "该会话已在电脑删除，请新建会话" }
        require(validImageId(image.id) != null) { "图片标识无效" }
        putImage(image)
        db.insertWithOnConflict("pending_images", null, ContentValues().apply {
            put("conversation_id", conversationId); put("image_id", image.id)
            put("filename", image.filename)
            put("authority_id", setting(db, "device_id") ?: "")
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun removePendingImage(conversationId: String) { writableDatabase.delete("pending_images", "conversation_id=?", arrayOf(conversationId)) }

    fun pendingImages(): Map<String, PendingImage> = readableDatabase.rawQuery("""SELECT p.conversation_id,
        i.id,p.filename,i.mime_type,i.size,i.local_path FROM pending_images p JOIN images i ON p.image_id=i.id""", null).use {
        buildMap { while (it.moveToNext()) put(it.getString(0), PendingImage(it.getString(0), it.getString(1),
            it.getString(2), it.getString(3), it.getLong(4), it.nullable("local_path"))) }
    }

    fun pendingImage(conversationId: String): PendingImage? = pendingImages()[conversationId]

    fun protectedImageIds(): Set<String> = outbox().filter { it.status !in setOf("sent", "discarded") }
        .mapNotNull { it.attachmentId }.toSet() + pendingImages().values.map { it.attachmentId } + directTurns()
        .filter { it.state != "imported" }.mapNotNull { it.imageId }

    private fun validImageId(value: String?): String? = value?.takeIf { it.matches(Regex("[0-9a-f]{64}")) }
    private fun validImageError(value: String?): String? = value?.takeIf { it in setOf("image_missing", "vision_rejected", "image_unavailable") }

    private fun Cursor.outboxItem(): OutboxItem = OutboxItem(
        getString(getColumnIndexOrThrow("id")), getString(getColumnIndexOrThrow("method")),
        nullable("conversation_id"), getString(getColumnIndexOrThrow("text")), getString(getColumnIndexOrThrow("state")),
        getString(getColumnIndexOrThrow("created_at")), nullable("error"), nullable("message_id"), nullable("attachment_id"),
        hidden = getInt(getColumnIndexOrThrow("hidden")) != 0,
        canSendOnPhone = getString(getColumnIndexOrThrow("method")) == "chat.send" && getString(getColumnIndexOrThrow("state")) == "queued" &&
            !JSONObject(getString(getColumnIndexOrThrow("params"))).optBoolean("desktop_context") &&
            getInt(getColumnIndexOrThrow("attempted")) == 0 && nullable("conversation_id")?.let { cid ->
                !isConversationDeleted(cid) && (getString(getColumnIndexOrThrow("authority_id")).isBlank() ||
                    getString(getColumnIndexOrThrow("authority_id")) == device()?.endpointId) && nullable("message_id")?.let(::hasMessage) != true
            } == true)

    private fun Cursor.nullable(column: String): String? = getColumnIndexOrThrow(column).let { if (isNull(it)) null else getString(it) }

    internal fun operation(id: String): QueuedOperation? = readableDatabase.rawQuery("SELECT * FROM outbox WHERE id=?", arrayOf(id)).use {
        if (!it.moveToFirst()) null else QueuedOperation(it.outboxItem(), JSONObject(it.getString(it.getColumnIndexOrThrow("params"))),
            it.getInt(it.getColumnIndexOrThrow("attempted")) != 0, it.getString(it.getColumnIndexOrThrow("authority_id")),
            it.getLong(it.getColumnIndexOrThrow("checked_at")), it.nullable("dependency_id"))
    }

    internal fun outgoing(): List<QueuedOperation> = readableDatabase.rawQuery("""SELECT id FROM outbox
        WHERE hidden=0 AND state IN ('queued','sending','unknown','pending') ORDER BY created_at,id LIMIT 20""", null).use {
        buildList { while (it.moveToNext()) operation(it.getString(0))?.let(::add) }
    }

    internal fun hasOutgoingOperations(): Boolean = readableDatabase.rawQuery("""SELECT 1 FROM outbox
        WHERE hidden=0 AND state IN ('queued','sending','unknown','pending') LIMIT 1""", null).use { it.moveToFirst() }

    internal fun hasUnqueuedDirectImports(): Boolean = readableDatabase.rawQuery("""SELECT 1 FROM direct_turns
        WHERE state='ready' AND import_request_id IS NULL AND authority_id!=? LIMIT 1""", arrayOf(LOCAL_PHONE_AUTHORITY)).use { it.moveToFirst() }

    fun updateOutbox(id: String, state: String, error: String? = null, messageId: String? = null,
                     attempted: Boolean? = null, checkedAt: Long? = null, attachmentId: String? = null) {
        writableDatabase.update("outbox", ContentValues().apply {
            put("state", state); put("error", error)
            if (messageId != null) put("message_id", messageId)
            if (attempted != null) put("attempted", if (attempted) 1 else 0)
            if (checkedAt != null) put("checked_at", checkedAt)
            if (attachmentId != null) put("attachment_id", attachmentId)
        }, "id=? AND (state NOT IN ('discarded','sent','needs_attention') OR state=? OR ?='discarded')", arrayOf(id, state, state))
    }

    fun outbox(): List<OutboxItem> = readableDatabase.rawQuery("SELECT * FROM outbox ORDER BY created_at DESC LIMIT 1200", null).use {
        buildList { while (it.moveToNext()) add(it.outboxItem()) }
    }

    /** Admission races with an explicit move-to-phone action; only one may win. */
    internal fun claimOutboxForSend(id: String, messageId: String?): Boolean = writableDatabase.update("outbox", ContentValues().apply {
        put("state", "sending"); put("attempted", 1); putNull("error"); if (messageId != null) put("message_id", messageId)
    }, "id=? AND state IN ('queued','sending','unknown','pending')", arrayOf(id)) == 1

    fun hasMessage(id: String): Boolean = readableDatabase.rawQuery("SELECT 1 FROM messages WHERE id=?", arrayOf(id)).use { it.moveToFirst() }

    /** Indexed identity lookup for sync polling; never materialize the archive. */
    internal fun conversationGeneration(conversationId: String): String? {
        if (isConversationDeleted(conversationId)) return null
        readableDatabase.rawQuery("SELECT id FROM messages WHERE conversation_id=? ORDER BY created_at,id LIMIT 1",
            arrayOf(conversationId)).use { if (it.moveToFirst()) return it.getString(0) }
        // An unacknowledged outbox item cannot define the desktop generation.
        return readableDatabase.rawQuery("""SELECT user_id FROM direct_turns WHERE conversation_id=?
            AND state NOT IN ('imported','needs_attention') AND authority_id IN (?,?) ORDER BY user_at,user_id LIMIT 1""",
            arrayOf(conversationId, device()?.endpointId.orEmpty(), LOCAL_PHONE_AUTHORITY)).use {
            if (it.moveToFirst()) it.getString(0) else null
        }
    }

    fun conversations(): List<Conversation> = readableDatabase.rawQuery("SELECT * FROM conversations ORDER BY updated_at DESC,id LIMIT 500", null).use {
        buildList { while (it.moveToNext()) add(Conversation(it.getString(0), it.getString(1), it.getString(2), it.getString(3), it.getInt(4), it.getInt(5) == 1)) }
    }

    fun messages(conversationId: String?, limit: Int? = 500): List<Message> {
        // A purged conversation has no visible thread, including optimistic
        // drafts. Its unsent text remains exclusively in the attention queue.
        if (conversationId == null || isConversationDeleted(conversationId)) return emptyList()
        require(limit == null || limit > 0)
        val mirrored = readableDatabase.rawQuery("""SELECT * FROM messages WHERE conversation_id=?
            ORDER BY created_at DESC,id DESC LIMIT ${limit ?: -1}""", arrayOf(conversationId)).use {
            buildList { while (it.moveToNext()) add(Message(it.getString(0), it.getString(1), it.getString(2),
                it.getString(3), it.getString(4), it.getString(5), it.nullable("request_status"), it.nullable("reply_to"),
                attachmentId = it.nullable("image_id"), imageError = it.nullable("image_error"),
                agentErrorCode = it.nullable("agent_error_code"), agentError = it.nullable("agent_error_code")?.let(AGENT_ERROR_MESSAGES::get),
                agentActivity = it.nullable("agent_activity"), executionMode = it.nullable("execution_mode"),
                execution = parseExecutionProcess(it.nullable("agent_trace")))) }
        }
        val ids = mirrored.map { it.id }.toSet()
        val pending = outbox().filter { it.conversationId == conversationId && it.method in setOf("chat.send", "agent.send", "image.upload") && it.status != "discarded" }
            .filter { it.method != "image.upload" || !isHidden(it.requestId) }
            .filter { it.messageId !in ids && (it.messageId == null || !hasMessage(it.messageId)) }
            .map { Message(it.messageId ?: it.requestId, conversationId, "user",
                if (it.method == "image.upload") it.text + "\n仅传入电脑图片收件箱，不会自动识图或回复" else it.text,
                it.createdAt, source = if (it.method == "image.upload") "image_inbox" else "user",
                localState = it.status, attachmentId = it.attachmentId, localError = it.error,
                executionMode = if (it.method == "agent.send") "agent" else null) }
        val localTurns = directTurns().filter { it.conversationId == conversationId &&
            (it.authorityId == device()?.endpointId || it.authorityId == LOCAL_PHONE_AUTHORITY) }
        val direct = localTurns.filter { it.state != "imported" }
            .flatMap { turn -> buildList {
                if (turn.userId !in ids && !isDeleted(readableDatabase, "message", turn.userId)) add(Message(turn.userId, conversationId, "user", turn.text,
                    turn.userAt, source = "mobile_direct", localState = if (turn.state == "ready") "history_pending" else turn.state,
                    attachmentId = turn.imageId, localError = turn.error, execution = turn.execution))
                if (turn.reply != null && turn.assistantId !in ids && !isDeleted(readableDatabase, "message", turn.assistantId)) add(Message(turn.assistantId,
                    conversationId, "assistant", turn.reply, turn.assistantAt ?: turn.userAt, source = "mobile_direct", replyTo = turn.userId,
                    localState = if (turn.state == "ready") "history_pending" else turn.state, localError = turn.error))
            } }
        val localTraces = localTurns.filter { it.execution != null }.associateBy { it.userId }
        // Imported IDs are stable; keep locally recorded phone process data
        // even when an older desktop later snapshots a null agent_trace.
        val visibleMirror = mirrored.map { message ->
            localTraces[message.id]?.let { message.copy(execution = it.execution) } ?: message
        }
        val ordered = (visibleMirror + pending + direct).sortedWith(compareBy<Message> { it.createdAt }.thenBy { it.id })
        return if (limit == null) ordered else ordered.takeLast(limit)
    }

    internal fun directTurns(): List<DirectTurn> = readableDatabase.rawQuery("SELECT * FROM direct_turns ORDER BY user_at,id", null).use {
        buildList { while (it.moveToNext()) add(DirectTurn(it.getString(0), it.getString(1), it.getString(2), it.getString(3), it.getString(4),
            it.getString(5), it.getString(6), it.nullable("reply"), it.nullable("assistant_at"), it.nullable("image_id"), it.getString(10),
            it.nullable("error"), it.nullable("import_request_id"), parseExecutionProcess(it.nullable("process_json")))) }
    }

    internal fun directTurnIsGenerating(id: String, cid: String, userId: String, assistantId: String): Boolean =
        readableDatabase.rawQuery("""SELECT 1 FROM direct_turns WHERE id=? AND conversation_id=?
            AND user_id=? AND assistant_id=? AND state='generating'
            AND NOT EXISTS (SELECT 1 FROM tombstones WHERE (entity='conversation' AND id=?)
                OR (entity='message' AND id IN (?,?)))""", arrayOf(id, cid, userId, assistantId, cid, userId, assistantId))
            .use { it.moveToFirst() }

    internal fun recoverInterruptedDirectTurns() = transaction { db ->
        for (turn in directTurns().filter { it.state == "generating" }) db.update("direct_turns", ContentValues().apply {
            put("state", "unknown"); put("error", "上次模型请求被中断，可能已计费；不会自动重发，请确认后重新发送")
            put("process_json", finishDirectExecution(turn.execution, "interrupted")?.canonicalJson())
        }, "id=? AND state='generating'", arrayOf(turn.id))
        Unit
    }

    internal fun beginDirectTurn(conversationId: String, text: String, imageId: String?, expectedAuthority: String? = null,
        allowUnpaired: Boolean = false, legacyRequestId: String? = null, titleText: String = text): DirectTurn = transaction { db ->
        require(!isDeleted(db, "conversation", conversationId)) { "该会话已在电脑删除，请新建会话" }
        val authority = if (allowUnpaired && (setting(db, "device_id") == null || setting(db, "authorization_blocked") == "1")) LOCAL_PHONE_AUTHORITY
            else setting(db, "device_id") ?: throw IllegalArgumentException("请先配置手机模型或配对电脑")
        require(expectedAuthority == null || expectedAuthority == authority) { "电脑已更换，请重新同步 API 配置后发送" }
        require(allowUnpaired || setting(db, "authorization_blocked") != "1") { "电脑已撤销授权，请手动重新配对" }
        val pendingOwner = db.rawQuery("SELECT authority_id FROM pending_images WHERE conversation_id=?", arrayOf(conversationId)).use {
            if (it.moveToFirst()) it.getString(0) else null
        }
        require(legacyRequestId != null || pendingOwner.isNullOrBlank() || pendingOwner == authority ||
            (allowUnpaired && pendingOwner == setting(db, "device_id"))) { "待发图片属于原电脑，请移除后重新选择" }
        val legacy = legacyRequestId?.let { request ->
            val queued = operation(request) ?: throw IllegalArgumentException("原待发消息不存在")
            require(queued.item.canSendOnPhone) { "该请求可能已经送达电脑，不能再次调用模型；请先确认原请求状态" }
            require(queued.item.conversationId == conversationId && queued.item.attachmentId == imageId) { "原待发消息或图片已变化，请刷新后重试" }
            val changed = db.update("outbox", ContentValues().apply { put("state", "discarded"); put("error", "已由用户明确改为手机直接发送") },
                "id=? AND state='queued' AND attempted=0", arrayOf(request))
            require(changed == 1) { "原请求正在发送，不能重复调用模型" }
            queued.dependencyId?.let { db.update("outbox", ContentValues().apply { put("state", "discarded") }, "id=? AND attempted=0", arrayOf(it)) }
            queued
        }
        createConversation(conversationId)
        val id = UUID.randomUUID().toString(); val user = legacy?.item?.messageId ?: legacy?.item?.requestId ?: UUID.randomUUID().toString()
        val assistant = UUID.randomUUID().toString()
        val now = legacy?.item?.createdAt ?: Instant.now().toString()
        // Old PCs do not expose title provenance: only their phone-created
        // conversations are eligible. A new PC explicitly marks automatic
        // titles, so an intentional rename to “新对话” is never replaced.
        val priorPhoneTurn = db.rawQuery("""SELECT 1 FROM direct_turns d WHERE conversation_id=?
            AND authority_id IN (?,?) AND state!='needs_attention'
            AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.entity='message' AND t.id=d.user_id)
            AND (state!='imported' OR EXISTS (SELECT 1 FROM messages m WHERE m.id=d.user_id)) LIMIT 1""",
            arrayOf(conversationId, authority, LOCAL_PHONE_AUTHORITY)).use { it.moveToFirst() }
        if (!priorPhoneTurn) {
            val title = phoneConversationTitle(titleText, imageId != null)
            val changed = db.update("conversations", ContentValues().apply { put("title", title) },
                "id=? AND (local_only=1 OR ?='1') AND trim(title) IN ('','新对话')",
                arrayOf(conversationId, if (hostTitleManual(db, conversationId) == false) "1" else "0"))
            if (changed == 1) putSetting(db, "phone_title:$conversationId", JSONObject().put("title", title)
                .put("generation", conversationGeneration(conversationId) ?: user).toString())
        }
        val execution = newDirectExecution()
        db.insertOrThrow("direct_turns", null, ContentValues().apply {
            put("id", id); put("conversation_id", conversationId); put("authority_id", authority); put("user_id", user); put("assistant_id", assistant)
            put("text", text); put("user_at", now); put("image_id", imageId); put("state", "generating")
            put("process_json", execution.canonicalJson())
        })
        invalidateContext(db, conversationId)
        if (legacyRequestId == null) db.delete("pending_images", "conversation_id=?", arrayOf(conversationId))
        db.execSQL("UPDATE conversations SET updated_at=? WHERE id=?", arrayOf(now, conversationId))
        DirectTurn(id, conversationId, authority, user, assistant, text, now, null, null, imageId, "generating", null, null, execution)
    }

    internal fun finishDirectTurn(id: String, reply: String) = transaction { db ->
        require(reply.isNotBlank() && reply.length <= 24000) { "模型回复为空或过长，未自动重试" }
        val turn = directTurns().first { it.id == id }
        if (turn.state != "generating") return@transaction
        val blocked = (turn.authorityId != LOCAL_PHONE_AUTHORITY && (turn.authorityId != setting(db, "device_id") || setting(db, "authorization_blocked") == "1")) ||
            isDeleted(db, "conversation", turn.conversationId) || turn.state == "needs_attention"
        db.update("direct_turns", ContentValues().apply {
            put("reply", reply); put("assistant_at", Instant.now().toString()); put("state", if (blocked) "needs_attention" else "ready")
            put("process_json", finishDirectExecution(turn.execution, "completed")?.canonicalJson())
            if (!blocked) putNull("error")
        }, "id=?", arrayOf(id))
        invalidateContext(db, turn.conversationId)
    }

    internal fun failDirectTurn(id: String, error: String, unknown: Boolean = true, partial: String? = null,
        interrupted: Boolean = false) = transaction { db ->
        val turn = directTurns().firstOrNull { it.id == id && it.state == "generating" } ?: return@transaction
        db.update("direct_turns", ContentValues().apply {
            put("state", if (unknown) "unknown" else "failed"); put("error", error)
            partial?.takeIf { it.isNotBlank() && it.length <= 24000 }?.let {
                put("reply", it); put("assistant_at", Instant.now().toString())
            }
            put("process_json", finishDirectExecution(turn.execution, if (interrupted) "interrupted" else "failed")?.canonicalJson())
        }, "id=? AND state='generating'", arrayOf(id))
        invalidateContext(db, turn.conversationId)
        Unit
    }

    internal fun historyPendingCount(): Int = directTurns().count { it.state == "ready" }

    /** Retrying this only imports saved text. It can never call a model again. */
    internal fun prepareDirectImports() {
        for (turn in directTurns().filter { it.state == "ready" && it.importRequestId == null }.take(20)) transaction { db ->
            if (turn.authorityId != setting(db, "device_id") || isDeleted(db, "conversation", turn.conversationId)) return@transaction
            val queued = db.rawQuery("SELECT count(*) FROM outbox WHERE state NOT IN ('sent','discarded')", null).use { it.moveToFirst(); it.getInt(0) }
            if (queued + (if (turn.imageId == null) 1 else 2) > 1000) return@transaction
            val users = JSONObject().put("id", turn.userId).put("role", "user").put("content", turn.text).put("created_at", turn.userAt)
            if (turn.imageId != null) users.put("image_id", turn.imageId)
            val assistant = JSONObject().put("id", turn.assistantId).put("role", "assistant").put("content", turn.reply)
                .put("created_at", turn.assistantAt).put("reply_to", turn.userId)
            val params = JSONObject().put("conversation_id", turn.conversationId).put("messages", JSONArray().put(users).put(assistant))
            var dependency: String? = null
            if (turn.imageId != null) {
                val image = image(turn.imageId) ?: throw IllegalArgumentException("待同步图片信息缺失，聊天记录仍保存在手机")
                dependency = addOutbox(db, "image.upload", JSONObject().put("filename", image.filename).put("mime_type", image.mimeType)
                    .put("sha256", image.id), turn.conversationId, "同步聊天图片", image.id, hidden = true)
            }
            val request = addOutbox(db, "chat.import", params, turn.conversationId, turn.text, turn.imageId, dependency)
            db.update("direct_turns", ContentValues().apply { put("import_request_id", request) }, "id=?", arrayOf(turn.id))
        }
    }

    internal fun acceptDirectImport(requestId: String, result: JSONObject) = transaction { db ->
        val turn = directTurns().firstOrNull { it.importRequestId == requestId } ?: error("同步记录缺失")
        val ids = result.getJSONArray("message_ids")
        require(result.optBoolean("imported") && result.getString("conversation_id") == turn.conversationId &&
            ids.length() == 2 && ids.getString(0) == turn.userId && ids.getString(1) == turn.assistantId) { "电脑返回的记录归属无效" }
        // Import acknowledgment confirms these exact stable IDs/content. Commit
        // the confirmed mirror now so a lost subsequent pull cannot hide a reply.
        if (!isDeleted(db, "conversation", turn.conversationId) && turn.authorityId == setting(db, "device_id")) {
            val user = JSONObject().put("role", "user").put("content", turn.text).put("created_at", turn.userAt)
                .put("source", "mobile_direct").put("request_status", "complete").put("image_id", turn.imageId)
            val assistant = JSONObject().put("role", "assistant").put("content", turn.reply).put("created_at", turn.assistantAt)
                .put("source", "mobile_direct").put("request_status", "complete").put("reply_to", turn.userId)
            for ((id, data) in listOf(turn.userId to user, turn.assistantId to assistant)) applyEntity(db, JSONObject()
                .put("entity", "message").put("id", id).put("conversation_id", turn.conversationId).put("data", data))
        }
        db.update("direct_turns", ContentValues().apply { put("state", "imported"); putNull("error") }, "id=?", arrayOf(turn.id))
        updateOutbox(requestId, "sent")
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

    fun reconcileImages(remoteIds: Set<String>): Boolean {
        var changed = false
        val protected = protectedImageIds()
        for (image in images()) if (image.id !in remoteIds && image.id !in protected && image.uploadState in setOf("received", "uploaded")) {
            deleteImage(image.id)
            changed = true
        }
        return changed
    }

    private fun isHidden(requestId: String): Boolean = readableDatabase.rawQuery("SELECT hidden FROM outbox WHERE id=?", arrayOf(requestId)).use {
        it.moveToFirst() && it.getInt(0) != 0
    }
}
