package com.aura.companion.data

import android.content.Context
import android.util.Base64
import com.aura.companion.transport.IrohRpcClient
import com.aura.companion.transport.RpcTransportException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

internal interface RpcGateway {
    val endpointId: String
    val connectionType: String
    suspend fun connect(ticket: String)
    suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject
    suspend fun close()
}

private class ClientGateway(private val client: IrohRpcClient) : RpcGateway {
    override val endpointId get() = client.endpointId
    override val connectionType get() = client.connectionType
    override suspend fun connect(ticket: String) = client.connect(ticket)
    override suspend fun request(method: String, params: JSONObject, requestId: String) = client.request(method, params, requestId)
    override suspend fun close() = client.close()
}

internal class RpcFailure(val code: String, override val message: String) : Exception(message)

/** Foreground-driven repository. No service, hidden reconnect loop or second AI. */
class AppRepository internal constructor(
    context: Context,
    private val rpc: RpcGateway,
    internal val database: CompanionDatabase = CompanionDatabase(context),
) {
    constructor(context: Context, client: IrohRpcClient) : this(context, ClientGateway(client))
    constructor(context: Context, client: IrohRpcClient, database: CompanionDatabase) : this(context, ClientGateway(client), database)

    private val mutex = Mutex()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val imageCache = ImageCache(context.applicationContext)
    private val mutableState = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = mutableState.asStateFlow()
    private var lastImagesRefresh = 0L

    init { scope.launch { mutex.withLock {
        try { refreshState() } catch (_: Exception) { mutableState.update { it.copy(error = "无法读取本地缓存，请检查手机存储空间") } }
    } } }

    private fun refreshState() {
        val conversations = database.conversations()
        var selected = database.getSetting("selected_conversation")
        if (selected == null || conversations.none { it.id == selected }) {
            selected = conversations.firstOrNull()?.id
            database.setSetting("selected_conversation", selected)
        }
        mutableState.update { it.copy(device = database.device(), conversations = conversations,
            selectedConversationId = selected, messages = database.messages(selected), outbox = database.outbox(),
            images = database.images(), lastSyncedAt = database.getSetting("last_synced_at")?.toLongOrNull()) }
    }

    private suspend fun perform(block: suspend () -> Unit) = withContext(Dispatchers.IO) {
        mutex.withLock {
            try { block() }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: RpcFailure) {
                if (failure.code == "unauthorized") {
                    database.setSetting("authorization_blocked", "1")
                    runCatching { rpc.close() }
                    mutableState.update { it.copy(connectionState = ConnectionState.UNAUTHORIZED, syncState = SyncState.ERROR,
                        error = "电脑已撤销授权，请在电脑重新生成配对信息后手动配对") }
                } else if (failure.code in setOf("connection_lost", "disabled", "timeout", "connection_failed") ||
                    state.value.connectionState == ConnectionState.CONNECTING) {
                    runCatching { rpc.close() }
                    mutableState.update { it.copy(connectionState = ConnectionState.ERROR, syncState = SyncState.ERROR, error = failure.message) }
                } else mutableState.update { it.copy(syncState = SyncState.ERROR, error = failure.message) }
            } catch (transport: RpcTransportException) {
                runCatching { rpc.close() }
                mutableState.update { it.copy(connectionState = ConnectionState.ERROR, syncState = SyncState.ERROR,
                    error = transport.message ?: "加密连接中断，请重新连接") }
            } catch (invalid: IllegalArgumentException) {
                mutableState.update { it.copy(error = invalid.message ?: "输入内容无效") }
            } catch (_: Exception) {
                runCatching { rpc.close() }
                mutableState.update { it.copy(connectionState = ConnectionState.ERROR, syncState = SyncState.ERROR,
                    error = "连接中断，离线记录和待发内容已保留") }
            } finally { refreshState() }
        }
    }

    private suspend fun call(method: String, params: JSONObject = JSONObject(), requestId: String = UUID.randomUUID().toString()): JSONObject {
        val response = rpc.request(method, params, requestId)
        if (!response.optBoolean("ok")) {
            val error = response.optJSONObject("error") ?: JSONObject()
            throw RpcFailure(error.optString("code", "operation_failed"), error.optString("message", "电脑未能完成请求"))
        }
        return response.optJSONObject("result") ?: throw RpcFailure("protocol_error", "电脑响应格式无效")
    }

    suspend fun pair(pairInfo: String, deviceName: String) = perform {
        val info = parsePairing(pairInfo)
        require(deviceName.trim().length in 1..80 && deviceName.none { it.code < 32 }) { "设备名称须为 1～80 个字符" }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTING, error = null) }
        rpc.connect(info.getString("ticket"))
        call("pair", JSONObject().put("code", info.getString("code")).put("name", deviceName.trim()))
        val desktopId = info.optString("endpoint_id").ifBlank { info.getString("ticket").take(80) }
        // Only the public ticket/IDs/name are persisted. Neither code nor the
        // original pairing URI/JSON is ever written to settings, logs or disk.
        database.saveDevice(DeviceInfo("我的电脑", desktopId, info.getString("ticket")), rpc.endpointId)
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTED, connectionType = rpc.connectionType,
            error = null, syncState = SyncState.IDLE) }
        syncInternal()
        refreshImagesInternal()
    }

    suspend fun reconnect() = perform {
        val device = database.device() ?: throw IllegalArgumentException("请先在电脑生成配对信息")
        if (database.getSetting("authorization_blocked") == "1") {
            mutableState.update { it.copy(connectionState = ConnectionState.UNAUTHORIZED,
                error = "授权已撤销，请手动重新配对；应用不会自动配对") }
            return@perform
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTING, error = null) }
        rpc.connect(device.ticket)
        val status = call("status")
        require(status.optInt("protocol_version", 1) == 1) { "电脑通信协议不兼容" }
        if (database.getSetting("selected_conversation") == null) {
            database.setSetting("selected_conversation", status.optionalString("current_conversation_id"))
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTED, connectionType = rpc.connectionType, error = null) }
        syncInternal()
        refreshImagesInternal()
    }

    suspend fun disconnect() = perform {
        rpc.close()
        mutableState.update { it.copy(connectionState = ConnectionState.DISCONNECTED, syncState = SyncState.IDLE,
            connectionType = "unknown", error = null) }
    }

    suspend fun syncNow() = perform {
        if (state.value.connectionState != ConnectionState.CONNECTED) return@perform
        syncInternal()
        if (System.currentTimeMillis() - lastImagesRefresh > 30_000) refreshImagesInternal()
    }

    private suspend fun syncInternal() {
        mutableState.update { it.copy(syncState = SyncState.SYNCING) }
        var resets = 0
        while (true) {
            try {
                if (!database.initialized() || database.snapshotCursor().isNotEmpty()) fetchSnapshot()
                pullChanges()
                break
            } catch (failure: RpcFailure) {
                if (failure.code != "sync_reset_required" || ++resets > 2) throw failure
                database.resetSnapshot()
                database.setSetting("sync_ready", null)
            }
        }
        if (flushOutbox()) pullChanges()
        database.setSetting("last_synced_at", System.currentTimeMillis().toString())
        mutableState.update { it.copy(syncState = SyncState.READY, error = null, connectionType = rpc.connectionType) }
    }

    private suspend fun fetchSnapshot() {
        var cursor = database.snapshotCursor()
        var pages = 0
        do {
            require(++pages <= 2000) { "同步分页超过限制" }
            val page = call("sync.snapshot", JSONObject().put("limit", 100).put("cursor", cursor))
            database.stageSnapshotPage(page, first = cursor.isEmpty())
            cursor = page.optString("next_cursor")
        } while (page.getBoolean("has_more"))
    }

    private suspend fun pullChanges() {
        var pages = 0
        do {
            require(++pages <= 2000) { "同步分页超过限制" }
            val page = call("sync.pull", JSONObject().put("since", database.cursor()).put("limit", 100))
            database.applyPull(page)
        } while (page.getBoolean("has_more"))
    }

    suspend fun selectConversation(id: String?) = perform { database.setSetting("selected_conversation", id) }

    suspend fun newConversation(): String = withContext(Dispatchers.IO) {
        mutex.withLock { database.createConversation().also { refreshState() } }
    }

    suspend fun sendMessage(conversationId: String, text: String) = perform {
        require(canonicalUuid(conversationId) && text.trim().length in 1..8000) { "请输入 1～8000 字的消息" }
        require(!database.isConversationDeleted(conversationId)) { "该会话已在电脑删除，请新建会话" }
        database.createConversation(conversationId)
        database.addOutbox("chat.send", JSONObject().put("conversation_id", conversationId).put("text", text.trim()),
            conversationId = conversationId, text = text.trim())
        refreshState()
        if (state.value.connectionState == ConnectionState.CONNECTED) syncInternal()
    }

    private fun protectedImages(): Set<String> = database.outbox()
        .filter { it.status !in setOf("sent", "discarded") }.mapNotNull { it.attachmentId }.toSet()

    suspend fun sendImage(bytes: ByteArray, filename: String, mimeType: String) = perform {
        val label = filename.replace('\\', '/').substringAfterLast('/').take(160).ifBlank { "image" }
        require(label.none { it.code < 32 }) { "图片文件名无效" }
        val stored = imageCache.store(bytes, mimeType, protectedImages())
        val existing = database.outbox().any { it.method == "image.upload" && it.attachmentId == stored.id && it.status !in setOf("discarded", "failed") }
        if (!existing) {
            database.putImage(ImageItem(stored.id, label, stored.mimeType, bytes.size.toLong(), stored.width, stored.height,
                Instant.now().toString(), stored.file.absolutePath, "queued"))
            database.addOutbox("image.upload", JSONObject().put("filename", label).put("mime_type", stored.mimeType).put("sha256", stored.id),
                conversationId = state.value.selectedConversationId, text = "图片收件箱：$label", attachmentId = stored.id)
        }
        refreshState()
        if (state.value.connectionState == ConnectionState.CONNECTED) syncInternal()
    }

    /** Transient pet actions are never put into the durable offline queue. */
    suspend fun petCommand(action: String, value: Any) = perform {
        require(state.value.connectionState == ConnectionState.CONNECTED) { "请先连接电脑，桌宠动作不会离线重放" }
        call("pet.command", JSONObject().put("action", action).put("value", value)
            .put("expires_at", System.currentTimeMillis() / 1000.0 + 30.0))
    }

    private suspend fun flushOutbox(): Boolean {
        var touched = false
        val authority = database.device()?.endpointId ?: return false
        for (operation in database.outgoing()) {
            val item = operation.item
            if (operation.authorityId != authority || item.conversationId?.let(database::isConversationDeleted) == true) {
                database.updateOutbox(item.requestId, "needs_attention", "原会话或电脑已不可用，请手动处理")
                continue
            }
            if (item.messageId != null && database.hasMessage(item.messageId)) {
                database.updateOutbox(item.requestId, "sent"); continue
            }
            if (item.status == "pending" && System.currentTimeMillis() - operation.checkedAt < 15_000) continue
            try {
                if (operation.attempted) {
                    val saved = call("operation.get", JSONObject().put("request_id", item.requestId))
                    touched = true
                    database.updateOutbox(item.requestId, item.status, checkedAt = System.currentTimeMillis())
                    if (saved.optString("state") != "not_found") { handleOperation(operation, saved); continue }
                }
                if (item.method == "chat.send" && call("status").optBoolean("chat_busy")) break
                val params = JSONObject(operation.params.toString())
                if (item.method == "image.upload") {
                    val file = item.attachmentId?.let(imageCache::file)
                    require(file != null) { "待发图片已缺失，请重新选择" }
                    params.put("data_base64", Base64.encodeToString(file.readBytes(), Base64.NO_WRAP))
                }
                val messageId = if (item.method == "chat.send") stableMessageId(rpc.endpointId, item.requestId) else null
                database.updateOutbox(item.requestId, "sending", messageId = messageId, attempted = true)
                refreshState()
                val result = call(item.method, params, item.requestId)
                touched = true
                if (result.optBoolean("duplicate")) handleOperation(operation, result)
                else acceptOperation(operation, result)
                if (item.method == "chat.send") break // The desktop owns one AI generation at a time.
            } catch (cancelled: CancellationException) {
                database.updateOutbox(item.requestId, "unknown", "等待恢复连接后确认投递状态", attempted = true)
                throw cancelled
            } catch (failure: RpcFailure) {
                if (failure.code == "unauthorized") {
                    database.updateOutbox(item.requestId, "unknown", "授权已撤销，待发内容已保留")
                    throw failure
                }
                val retryable = failure.code in setOf("rate_limited", "busy", "disabled", "storage_error", "operation_failed")
                database.updateOutbox(item.requestId, if (retryable) "unknown" else "failed", failure.message)
                if (failure.code == "disabled") throw failure
                if (retryable) break
            } catch (transport: RpcTransportException) {
                database.updateOutbox(item.requestId, "unknown", "响应未确认；重连后查询原操作", attempted = true)
                throw transport
            } catch (invalid: IllegalArgumentException) {
                database.updateOutbox(item.requestId, "needs_attention", invalid.message ?: "内容需手动处理")
            } catch (_: Exception) {
                database.updateOutbox(item.requestId, "unknown", "响应未确认；重连后会查询原操作，不会重新生成 AI 请求", attempted = true)
                throw RpcFailure("connection_lost", "连接中断，消息状态尚待确认")
            }
        }
        return touched
    }

    private fun handleOperation(operation: QueuedOperation, result: JSONObject) {
        val id = operation.item.requestId
        when (result.optString("state")) {
            "complete" -> {
                val completion = result.optJSONObject("result") ?: JSONObject()
                if (completion.optBoolean("ok")) acceptOperation(operation, completion.optJSONObject("result") ?: JSONObject())
                else database.updateOutbox(id, "failed", completion.optJSONObject("error")?.optString("message") ?: "电脑未完成该操作，请手动处理")
            }
            "pending" -> database.updateOutbox(id, "pending", "电脑已登记；等待确认，不会自动重复触发 AI", checkedAt = System.currentTimeMillis())
            "expired" -> database.updateOutbox(id, "failed", "操作已过期")
            else -> database.updateOutbox(id, "unknown", "操作状态尚待确认")
        }
    }

    private fun acceptOperation(operation: QueuedOperation, result: JSONObject) {
        val item = operation.item
        when (item.method) {
            "chat.send" -> {
                val messageId = result.getString("message_id")
                require(canonicalUuid(messageId) && result.getString("conversation_id") == item.conversationId) { "电脑返回的消息归属无效" }
                database.updateOutbox(item.requestId, "sent", messageId = messageId)
            }
            "image.upload" -> {
                require(result.getString("id") == item.attachmentId) { "图片校验不一致" }
                database.putImage(imageFromJson(result, "uploaded"))
                database.updateOutbox(item.requestId, "sent")
            }
            "image.delete" -> {
                item.attachmentId?.let { imageCache.delete(it); database.deleteImage(it) }
                database.updateOutbox(item.requestId, "sent")
            }
        }
    }

    suspend fun retryOutbox(requestId: String) = perform {
        val operation = database.operation(requestId) ?: return@perform
        val item = operation.item
        require(item.conversationId?.let(database::isConversationDeleted) != true) { "会话已删除，请复制内容后新建会话" }
        require(operation.authorityId == database.device()?.endpointId) { "待发内容属于另一台电脑，请手动复制处理" }
        if (item.status == "failed") {
            if (operation.attempted) {
                require(state.value.connectionState == ConnectionState.CONNECTED) { "请先连接电脑确认原请求，再决定重新发送" }
                pullChanges()
                if (item.messageId != null && database.hasMessage(item.messageId)) {
                    database.updateOutbox(requestId, "sent")
                    return@perform
                }
                val saved = call("operation.get", JSONObject().put("request_id", requestId))
                if (saved.optString("state") == "pending") { handleOperation(operation, saved); return@perform }
                if (saved.optString("state") == "complete" && saved.optJSONObject("result")?.optBoolean("ok") == true) {
                    handleOperation(operation, saved); return@perform
                }
            }
            // This is an explicit user action after a definite failure. The
            // old ID stays recorded; uncertain/pending operations never clone.
            database.addOutbox(item.method, operation.params, item.conversationId, item.text, item.attachmentId)
            database.updateOutbox(requestId, "discarded", "已由用户明确重新发送")
        } else if (item.status == "needs_attention") {
            throw IllegalArgumentException("请检查内容和原会话，复制到新会话后再发送")
        }
        if (state.value.connectionState == ConnectionState.CONNECTED) syncInternal()
    }

    suspend fun discardOutbox(requestId: String) = perform {
        database.updateOutbox(requestId, "discarded", "已取消本地排队；已到达电脑的请求不能撤回")
    }

    suspend fun refreshImages() = perform {
        require(state.value.connectionState == ConnectionState.CONNECTED) { "请先连接电脑" }
        refreshImagesInternal()
    }

    private suspend fun refreshImagesInternal() {
        var offset = 0
        var pages = 0
        val remoteIds = mutableSetOf<String>()
        do {
            require(++pages <= 1000) { "图片分页超过限制" }
            val page = call("image.list", JSONObject().put("offset", offset).put("limit", 100))
            val items = page.getJSONArray("items")
            for (i in 0 until items.length()) {
                val image = imageFromJson(items.getJSONObject(i), "received")
                remoteIds.add(image.id)
                database.putImage(image)
            }
            val next = page.getInt("next_offset")
            require(!page.getBoolean("has_more") || next > offset) { "图片分页游标无效" }
            offset = next
        } while (page.getBoolean("has_more"))
        database.reconcileImages(remoteIds)
        lastImagesRefresh = System.currentTimeMillis()
    }

    suspend fun downloadImage(id: String): File? {
        var result: File? = null
        perform {
            val cached = imageCache.file(id)
            if (cached != null) { result = cached; return@perform }
            require(state.value.connectionState == ConnectionState.CONNECTED) { "图片尚未缓存，请先连接电脑" }
            val image = call("image.get", JSONObject().put("id", id))
            val encoded = image.getString("data_base64")
            require(encoded.length <= ((ImageCache.MAX_IMAGE_BYTES + 2) / 3) * 4) { "图片超过 4 MiB" }
            val bytes = Base64.decode(encoded, Base64.DEFAULT)
            require(ImageCache.digest(bytes) == id && image.getString("id") == id) { "图片校验失败" }
            val stored = imageCache.store(bytes, image.optString("mime_type"), protectedImages())
            database.putImage(imageFromJson(image, "received").copy(localPath = stored.file.absolutePath))
            result = stored.file
        }
        return result
    }

    suspend fun deleteImage(id: String) = perform {
        val image = database.image(id) ?: return@perform
        val pending = database.outbox().filter { it.attachmentId == id && it.method == "image.upload" && it.status !in setOf("sent", "discarded") }
        if (pending.isNotEmpty() && pending.all { database.operation(it.requestId)?.attempted == false }) {
            pending.forEach { database.updateOutbox(it.requestId, "discarded") }
            imageCache.delete(id); database.deleteImage(id)
        } else {
            database.addOutbox("image.delete", JSONObject().put("id", id), text = "删除图片：${image.filename}", attachmentId = id)
            database.putImage(image.copy(uploadState = "deleting"))
            if (state.value.connectionState == ConnectionState.CONNECTED) syncInternal()
        }
    }

    fun clearError() { mutableState.update { it.copy(error = null) } }

    suspend fun close() {
        scope.cancel()
        withContext(Dispatchers.IO) { mutex.withLock { rpc.close(); database.close() } }
    }

    private fun imageFromJson(value: JSONObject, uploadState: String): ImageItem = ImageItem(value.getString("id"),
        value.optString("filename", "image"), value.optString("mime_type"), value.getLong("size"), value.getInt("width"),
        value.getInt("height"), value.optString("created_at"), uploadState = uploadState)

    companion object {
        internal fun canonicalUuid(value: String): Boolean = try { UUID.fromString(value).toString() == value } catch (_: IllegalArgumentException) { false }

        internal fun stableMessageId(peerId: String, requestId: String): String {
            val namespace = UUID.fromString("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
            val prefix = ByteBuffer.allocate(16).putLong(namespace.mostSignificantBits).putLong(namespace.leastSignificantBits).array()
            val digest = MessageDigest.getInstance("SHA-1").digest(prefix + "aura-remote:$peerId:$requestId".toByteArray(Charsets.UTF_8))
            digest[6] = ((digest[6].toInt() and 15) or 0x50).toByte()
            digest[8] = ((digest[8].toInt() and 63) or 0x80).toByte()
            val buffer = ByteBuffer.wrap(digest)
            return UUID(buffer.long, buffer.long).toString()
        }

        internal fun parsePairing(value: String): JSONObject {
            return PairingInput.parse(value)
        }
    }
}
