package com.aura.companion.data

import android.content.Context
import android.os.SystemClock
import android.util.Base64
import com.aura.companion.BuildConfig
import com.aura.companion.transport.IrohRpcClient
import com.aura.companion.transport.RpcTransportException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
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

private val AGENT_UNAVAILABLE_MESSAGES = setOf(
    "电脑 Agent 尚未就绪，请检查电脑任务助手设置",
    "任务助手尚未启用，请在电脑设置中启用。",
    "任务助手尚未配置完成，请检查电脑模型与后端设置。",
    "电脑已暂停，请恢复后使用 Agent。",
)

/** Foreground-driven repository. No service, hidden reconnect loop or second AI. */
class AppRepository internal constructor(
    context: Context,
    private val rpc: RpcGateway,
    internal val database: CompanionDatabase = CompanionDatabase(context),
    private val apiVault: ApiConfigVault = EncryptedApiConfigVault(context.applicationContext),
    private val directGateway: DirectChatGateway = HttpDirectChatGateway(),
    private val allowCleartextApi: Boolean = BuildConfig.DEBUG,
    private val monotonicMillis: () -> Long = { SystemClock.elapsedRealtime() },
) {
    constructor(context: Context, client: IrohRpcClient) : this(context, ClientGateway(client))
    constructor(context: Context, client: IrohRpcClient, database: CompanionDatabase) : this(context, ClientGateway(client), database)
    internal constructor(context: Context, client: IrohRpcClient, database: CompanionDatabase, vault: ApiConfigVault) :
        this(context, ClientGateway(client), database, vault)

    private val mutex = Mutex()
    private val directMutex = Mutex()
    private val configLock = Any()
    @Volatile private var directConfig: DirectApiConfig? = runCatching { apiVault.load() }.getOrNull()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val localReady = CompletableDeferred<Unit>()
    private val imageCache = ImageCache(context.applicationContext)
    private val mutableState = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = mutableState.asStateFlow()
    private var lastImagesRefresh = 0L
    private var lastApiRefresh: Long? = null
    private var lastSyncStamp: Long? = null
    private var cachedCapabilities: Set<String>? = null
    private var cachedDesktopAllowed: Boolean? = null
    private var cachedAgentAvailable = false
    private var cachedAgentUnavailableReason: String? = null
    private var cachedAgentBusy = false
    private var cachedAgentConversationId: String? = null
    @Volatile private var directError: String? = null

    init { scope.launch {
        try {
            database.recoverInterruptedDirectTurns()
            val cached = directConfig
            if (cached != null && cached.source != ApiConfigSource.LOCAL && ((database.device()?.endpointId != null &&
                    cached.authorityId != database.device()?.endpointId) || database.getSetting("authorization_blocked") == "1")) clearDirectConfig()
            else if (cached != null) mutableState.update { it.copy(directApi = if (cached.source == ApiConfigSource.DESKTOP && database.device() == null)
                cached.publicInfo().copy(available = false, message = "此配置来自原电脑，请先保存为手机独立模型或重新配对") else cached.publicInfo()) }
            refreshState()
        } catch (_: Exception) { mutableState.update { it.copy(error = "无法读取本地缓存，请检查手机存储空间") } }
        finally { localReady.complete(Unit) }
    } }

    private fun refreshState() {
        val conversations = database.conversations()
        var selected = database.getSetting("selected_conversation")
        if (selected == null || conversations.none { it.id == selected }) {
            selected = conversations.firstOrNull()?.id
            database.setSetting("selected_conversation", selected)
        }
        mutableState.update { it.copy(device = database.device(), conversations = conversations,
            selectedConversationId = selected, messages = database.messages(selected), outbox = database.outbox(),
            images = database.images(), pendingImages = database.pendingImages(),
            desktopAgentConversations = database.desktopAgentConversations(),
            lastSyncedAt = database.getSetting("last_synced_at")?.toLongOrNull(), historyPendingCount = database.historyPendingCount(),
            error = directError ?: it.error,
            desktopTaskAvailable = it.connectionState == ConnectionState.CONNECTED &&
                database.getSetting("authorization_blocked") != "1" &&
                cachedAgentAvailable && "agent.send" in cachedCapabilities.orEmpty(),
            desktopAgentBusy = it.connectionState == ConnectionState.CONNECTED && cachedAgentBusy,
            desktopAgentConversationId = cachedAgentConversationId,
            desktopTaskMessage = when {
                it.connectionState == ConnectionState.UNAUTHORIZED -> "电脑授权已撤销，请重新配对后使用电脑 Agent"
                it.connectionState != ConnectionState.CONNECTED -> "请先连接电脑；普通聊天仍使用手机模型"
                "agent.send" !in cachedCapabilities.orEmpty() -> "请更新并重启电脑端后使用电脑 Agent"
                !cachedAgentAvailable -> cachedAgentUnavailableReason ?: "电脑 Agent 尚未就绪，请检查电脑任务助手设置"
                cachedAgentBusy -> "电脑 Agent 正在执行任务，可查看进度或停止当前任务"
                else -> "本会话消息交给电脑 Agent，使用电脑端已启用的工具与任务上下文"
            }) }
    }

    private suspend fun perform(refreshAfter: Boolean = true, silent: Boolean = false, block: suspend () -> Unit) = withContext(Dispatchers.IO) {
        mutex.withLock {
            var refresh = refreshAfter
            try { block() }
            catch (cancelled: CancellationException) {
                refresh = true
                cachedCapabilities = null
                // Foreground work is cancelled when the system picker opens.
                // Do not leave CONNECTING/SYNCING behind: the next foreground
                // loop must be able to reconnect, using the existing outbox IDs.
                withContext(NonCancellable) {
                    runCatching { rpc.close() }
                    mutableState.update { it.copy(connectionState = ConnectionState.DISCONNECTED,
                        syncState = SyncState.IDLE, connectionType = "unknown", error = directError) }
                }
                throw cancelled
            }
            catch (failure: RpcFailure) {
                refresh = true
                if (failure.code == "unauthorized") {
                    database.setSetting("authorization_blocked", "1")
                    clearDesktopConfig("电脑已撤销授权；手机独立模型仍可使用")
                    runCatching { rpc.close() }
                    mutableState.update { it.copy(connectionState = ConnectionState.UNAUTHORIZED, syncState = SyncState.ERROR,
                        error = "电脑已撤销授权，请在电脑重新生成配对信息后手动配对") }
                } else if (failure.code in setOf("connection_lost", "disabled", "timeout", "connection_failed") ||
                    state.value.connectionState == ConnectionState.CONNECTING) {
                    runCatching { rpc.close() }
                    mutableState.update { it.copy(connectionState = ConnectionState.ERROR, syncState = SyncState.ERROR,
                        error = if (silent) directError else failure.message) }
                } else mutableState.update { it.copy(syncState = SyncState.ERROR, error = if (silent) directError else failure.message) }
            } catch (transport: RpcTransportException) {
                refresh = true
                cachedCapabilities = null
                runCatching { rpc.close() }
                mutableState.update { it.copy(connectionState = ConnectionState.ERROR, syncState = SyncState.ERROR,
                    error = if (silent) directError else transport.message ?: "加密连接中断，请重新连接") }
            } catch (invalid: IllegalArgumentException) {
                refresh = true
                mutableState.update { it.copy(error = if (silent) directError else invalid.message ?: "输入内容无效") }
            } catch (_: Exception) {
                refresh = true
                cachedCapabilities = null
                runCatching { rpc.close() }
                mutableState.update { it.copy(connectionState = ConnectionState.ERROR, syncState = SyncState.ERROR,
                    error = if (silent) directError else "连接中断，离线记录和待发内容已保留") }
            } finally { if (refresh) refreshState() }
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
        cachedCapabilities = null
        synchronized(configLock) {
            val active = directConfig
            if (active?.source == ApiConfigSource.LOCAL) {
                if (active.desktopCache?.authorityId != desktopId) clearDesktopConfig()
            } else if (active?.authorityId != desktopId) clearDirectConfig()
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTED, connectionType = rpc.connectionType,
            error = null, syncState = SyncState.IDLE) }
        syncInternal(forceConfig = true)
        refreshImagesInternal()
    }

    suspend fun reconnect(silent: Boolean = false) = perform(silent = silent) {
        val device = database.device() ?: throw IllegalArgumentException("请先在电脑生成配对信息")
        if (database.getSetting("authorization_blocked") == "1") {
            mutableState.update { it.copy(connectionState = ConnectionState.UNAUTHORIZED,
                error = "授权已撤销，请手动重新配对；应用不会自动配对") }
            return@perform
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTING, error = if (silent) directError else null) }
        rpc.connect(device.ticket)
        val status = status()
        if (database.getSetting("selected_conversation") == null) {
            database.setSetting("selected_conversation", status.optionalString("current_conversation_id"))
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTED, connectionType = rpc.connectionType,
            error = if (silent) directError else null) }
        syncInternal(forceConfig = true, initialStatus = status)
        refreshImagesInternal()
    }

    suspend fun disconnect() = perform {
        rpc.close()
        cachedCapabilities = null
        mutableState.update { it.copy(connectionState = ConnectionState.DISCONNECTED, syncState = SyncState.IDLE,
            connectionType = "unknown", error = null) }
    }

    suspend fun syncNow(forceConfig: Boolean = true) = synchronize(forceConfig, silent = false)

    private suspend fun synchronize(forceConfig: Boolean, silent: Boolean) = perform(refreshAfter = forceConfig, silent = silent) {
        if (state.value.connectionState != ConnectionState.CONNECTED) return@perform
        var changed = syncInternal(forceConfig = forceConfig)
        if (monotonicMillis() - lastImagesRefresh >= REFRESH_INTERVAL_MS) changed = refreshImagesInternal() || changed
        if (!forceConfig && changed) refreshState()
    }

    /** Foreground automatic sync: idle calls use one pull without rereading all local history. */
    suspend fun poll() = synchronize(forceConfig = false, silent = true)

    private suspend fun status(): JSONObject = call("status").also { value ->
        require(value.optInt("protocol_version", 1) == 1) { "电脑通信协议不兼容" }
        // A phone may update before its desktop. Refresh historical projections
        // when this particular paired desktop first supports process records,
        // even if the phone's schema-upgrade snapshot came from an older host.
        database.device()?.endpointId?.takeIf { value.optInt("agent_trace_version", 0) >= 1 }?.let { desktop ->
            val key = "agent_trace_projection:$desktop"
            if (database.getSetting(key) != "1") {
                database.resetSnapshot()
                database.setSetting("sync_ready", null)
                database.setSetting(key, "1")
            }
        }
        val capabilities = value.optJSONArray("capabilities")
        cachedCapabilities = if (capabilities == null) emptySet() else (0 until capabilities.length())
            .map { capabilities.optString(it) }.toSet()
        cachedDesktopAllowed = value.optJSONObject("desktop_context")?.opt("available") as? Boolean
        cachedAgentAvailable = value.opt("agent_available") == true
        cachedAgentUnavailableReason = value.optionalString("agent_unavailable_reason")?.takeIf {
            it in AGENT_ERROR_MESSAGES.values || it in AGENT_UNAVAILABLE_MESSAGES
        }
        cachedAgentBusy = value.optBoolean("agent_busy")
        cachedAgentConversationId = value.optionalString("agent_conversation_id")?.takeIf(::canonicalUuid)
        mutableState.update { it.copy(desktopAgentBusy = cachedAgentBusy, desktopAgentConversationId = cachedAgentConversationId,
            desktopTaskAvailable = it.connectionState == ConnectionState.CONNECTED &&
                database.getSetting("authorization_blocked") != "1" && cachedAgentAvailable && "agent.send" in cachedCapabilities.orEmpty(),
            desktopTaskMessage = when {
                "agent.send" !in cachedCapabilities.orEmpty() -> "请更新并重启电脑端后使用电脑 Agent"
                !cachedAgentAvailable -> cachedAgentUnavailableReason ?: "电脑 Agent 尚未就绪，请检查电脑任务助手设置"
                cachedAgentBusy -> "电脑 Agent 正在执行任务，可查看进度或停止当前任务"
                else -> "本会话消息交给电脑 Agent，使用电脑端已启用的工具与任务上下文"
            }) }
    }

    private suspend fun syncInternal(forceConfig: Boolean = false, initialStatus: JSONObject? = null): Boolean {
        val active = database.hasOutgoingOperations() || database.hasUnqueuedDirectImports()
        if (forceConfig || active || !database.initialized()) mutableState.update { it.copy(syncState = SyncState.SYNCING) }
        val agentStatus = if (initialStatus == null && !forceConfig && (cachedAgentBusy ||
            database.getSetting("selected_conversation")?.let { database.getSetting("agent_mode:$it") } == "1")) status() else initialStatus
        val freshStatus = refreshApiConfigInternal(force = forceConfig, initialStatus = agentStatus, quiet = !forceConfig)
        var changed = false
        var resets = 0
        while (true) {
            try {
                if (!database.initialized() || database.snapshotCursor().isNotEmpty()) { fetchSnapshot(); changed = true }
                changed = pullChanges() || changed
                break
            } catch (failure: RpcFailure) {
                if (failure.code != "sync_reset_required" || ++resets > 2) throw failure
                database.resetSnapshot()
                database.setSetting("sync_ready", null)
            }
        }
        if (database.hasUnqueuedDirectImports()) { database.prepareDirectImports(); changed = true }
        if (flushOutbox(freshStatus)) { changed = true; pullChanges() }
        val now = monotonicMillis()
        if (forceConfig || changed || lastSyncStamp == null || now - lastSyncStamp!! >= REFRESH_INTERVAL_MS) {
            val stamp = System.currentTimeMillis()
            database.setSetting("last_synced_at", stamp.toString()); lastSyncStamp = now
            mutableState.update { it.copy(lastSyncedAt = stamp) }
        }
        mutableState.update { it.copy(syncState = SyncState.READY, error = directError, connectionType = rpc.connectionType) }
        return changed
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

    private suspend fun pullChanges(): Boolean {
        var changed = false
        var pages = 0
        do {
            require(++pages <= 2000) { "同步分页超过限制" }
            val page = call("sync.pull", JSONObject().put("since", database.cursor()).put("limit", 100))
            if (page.getJSONArray("changes").length() > 0 || page.getLong("next_since") != database.cursor()) {
                database.applyPull(page); changed = true
            }
        } while (page.getBoolean("has_more"))
        return changed
    }

    private suspend fun localAction(block: () -> Unit) = withContext(Dispatchers.IO) {
        localReady.await()
        try { block() } catch (invalid: IllegalArgumentException) { mutableState.update { it.copy(error = invalid.message ?: "本地内容无效") } }
        finally { refreshState() }
    }

    suspend fun selectConversation(id: String?) = localAction { database.setSetting("selected_conversation", id) }

    suspend fun setDesktopAgentMode(conversationId: String, enabled: Boolean) = localAction {
        require(canonicalUuid(conversationId)) { "会话标识无效" }
        database.setDesktopAgentMode(conversationId, enabled)
    }

    suspend fun newConversation(): String = withContext(Dispatchers.IO) {
        localReady.await(); database.createConversation().also { refreshState() }
    }

    suspend fun sendMessage(conversationId: String, text: String) {
        localReady.await()
        // Do not acquire the PC mutex before choosing the cached direct route:
        // a desktop reconnect may be waiting on its 30-second network timeout.
        val config = directConfig
        if (config != null && (config.source == ApiConfigSource.LOCAL || (config.authorityId == database.device()?.endpointId &&
                database.getSetting("authorization_blocked") != "1"))) {
            sendDirect(conversationId, text, config)
            return
        }
        if (database.getSetting("direct_api_blocked") == "1") {
            mutableState.update { it.copy(error = "电脑 API 配置无法安全用于手机，请检查 API 地址和配置后重新同步") }
            return
        }
        mutableState.update { it.copy(error = "请先配置手机模型，或连接电脑同步 API 配置；当前消息尚未发送") }
    }

    /** Explicit legacy compatibility entry, never selected as a silent send fallback. */
    internal suspend fun sendLegacyMessage(conversationId: String, text: String) = perform {
        val body = text.trim().ifEmpty { if (database.pendingImage(conversationId) != null) "请描述这张图片。" else "" }
        require(canonicalUuid(conversationId) && body.length in 1..8000) { "请输入 1～8000 字的消息，或先选择一张图片" }
        require(!database.isConversationDeleted(conversationId)) { "该会话已在电脑删除，请新建会话" }
        database.createConversation(conversationId)
        database.queueMessage(conversationId, body)
        refreshState()
        if (state.value.connectionState == ConnectionState.CONNECTED) syncInternal()
    }

    /** Explicit connected-only action. Never selected by ordinary phone chat. */
    suspend fun sendDesktopMessage(conversationId: String, text: String): Boolean {
        localReady.await()
        if (!desktopReady("agent.send")) return false
        val expectedAuthority = database.device()?.endpointId
        var accepted = false
        perform {
            val checkedStatus = status()
            requireDesktopReady("agent.send", expectedAuthority)
            require(!checkedStatus.optBoolean("agent_busy")) { "电脑 Agent 正在处理其他请求，请完成后重新发送" }
            val body = text.trim().ifEmpty { if (database.pendingImage(conversationId) != null) "请结合这张图片完成任务。" else "" }
            require(canonicalUuid(conversationId) && body.length in 1..8000) { "请输入 1～8000 字的电脑 Agent 请求，或先选择一张图片" }
            require(!database.isConversationDeleted(conversationId)) { "该会话已删除，请新建会话" }
            database.createConversation(conversationId)
            val requestId = database.queueAgentMessage(conversationId, body)
            refreshState()
            try {
                flushOutbox(initialStatus = checkedStatus, onlyRequestId = requestId)
                pullChanges()
                status()
            } finally {
                val operation = database.operation(requestId)
                if (operation?.attempted == false) database.updateOutbox(requestId, "needs_attention",
                    "电脑 Agent 请求尚未投递；不会在重连后自动执行，请确认后重新发送")
                accepted = database.operation(requestId)?.item?.status in setOf("sending", "unknown", "pending", "sent")
            }
        }
        return accepted
    }

    /** Cancellation is explicit, connected-only, and never replayed on reconnect. */
    suspend fun cancelDesktopTask(conversationId: String): Boolean {
        localReady.await()
        if (state.value.connectionState != ConnectionState.CONNECTED) {
            mutableState.update { it.copy(error = "请先连接电脑后停止任务") }; return false
        }
        val expectedAuthority = database.device()?.endpointId
        var cancelled = false
        perform {
            require(expectedAuthority != null && expectedAuthority == database.device()?.endpointId) { "目标电脑已更换，本次停止请求未发送" }
            require(canonicalUuid(conversationId)) { "会话标识无效" }
            require("agent.cancel" in cachedCapabilities.orEmpty()) { "电脑版本暂不支持停止 Agent，请更新并重启电脑端" }
            require(database.getSetting("authorization_blocked") != "1") { "电脑授权已撤销，请重新配对" }
            val result = call("agent.cancel", JSONObject().put("conversation_id", conversationId))
            cancelled = result.optBoolean("cancelled") || result.optBoolean("accepted")
            status()
            pullChanges()
        }
        return cancelled
    }

    /** A screenshot goes to the image library; it never fabricates a chat turn. */
    suspend fun requestDesktopScreenshot(): String? {
        localReady.await()
        if (!desktopReady("desktop.snapshot")) return null
        val expectedAuthority = database.device()?.endpointId
        var imageId: String? = null
        perform {
            requireDesktopReady("desktop.snapshot", expectedAuthority)
            val requestId = database.addOutbox("desktop.snapshot", JSONObject(), text = "查看电脑桌面")
            try {
                flushOutbox(onlyRequestId = requestId)
            } finally {
                val operation = database.operation(requestId)
                if (operation?.attempted == false) database.updateOutbox(requestId, "needs_attention",
                    "截图请求尚未投递；不会在重连后自动截图，请重新操作")
                imageId = database.operation(requestId)?.item?.takeIf { it.status == "sent" }?.attachmentId
            }
        }
        return imageId
    }

    private fun desktopReady(capability: String): Boolean {
        val message = when {
            database.getSetting("authorization_blocked") == "1" || state.value.connectionState == ConnectionState.UNAUTHORIZED -> "电脑授权已撤销，请重新配对后使用电脑协助"
            state.value.connectionState != ConnectionState.CONNECTED -> "电脑尚未连接，请连接后重新发送；本次电脑请求未排队"
            capability !in cachedCapabilities.orEmpty() -> "电脑版本暂不支持此操作，请更新并重启电脑端"
            capability == "agent.send" && !cachedAgentAvailable -> cachedAgentUnavailableReason ?: "电脑 Agent 尚未就绪，请检查电脑任务助手设置"
            capability == "desktop.snapshot" && cachedDesktopAllowed == false -> "电脑尚未允许远程查看屏幕，请在电脑设置中检查权限后重试"
            else -> null
        }
        if (message != null) mutableState.update { it.copy(error = message) }
        return message == null
    }

    private fun requireDesktopReady(capability: String, expectedAuthority: String?) {
        require(expectedAuthority != null && expectedAuthority == database.device()?.endpointId) {
            "目标电脑已更换，本次请求未发送，请确认当前电脑后重试"
        }
        require(desktopReady(capability)) { state.value.error ?: "电脑当前不可用，请重新连接" }
    }

    private suspend fun sendDirect(conversationId: String, text: String, config: DirectApiConfig,
        legacyRequestId: String? = null, expectedAuthority: String = if (config.source == ApiConfigSource.DESKTOP) config.authorityId else routeAuthority()) = withContext(Dispatchers.IO) {
        if (!directMutex.tryLock()) {
            mutableState.update { it.copy(error = "手机模型正在回复，请等待本次请求完成") }; return@withContext
        }
        try {
            require(canonicalUuid(conversationId) && !database.isConversationDeleted(conversationId)) { "该会话已删除或无效，请新建会话" }
            val legacy = legacyRequestId?.let { database.operation(it) ?: throw IllegalArgumentException("原待发消息不存在") }
            val pending = if (legacy == null) database.pendingImage(conversationId) else null
            val body = text.trim().ifEmpty { if (pending != null) "请描述这张图片。" else "" }
                .replace(config.apiKey, "[已隐藏密钥]")
            require(body.length in 1..8000) { "请输入 1～8000 字的消息，或先选择一张图片" }
            val history = database.messages(conversationId).filter { legacy == null || it.id !in setOf(legacy.item.requestId, legacy.item.messageId) }
            val imageId = if (legacy != null) legacy.item.attachmentId else pending?.attachmentId
            val image = imageId?.let { loadDirectImage(it, required = true) }
            val messages = directMessages(config, history, body, image) { loadDirectImage(it, required = false) }
            val turn = database.beginDirectTurn(conversationId, body, imageId, expectedAuthority,
                allowUnpaired = config.source == ApiConfigSource.LOCAL, legacyRequestId = legacyRequestId)
            directError = null
            mutableState.update { it.copy(directChatBusy = true, error = null) }
            refreshState()
            // Owned by the repository, not the foreground polling/picker job.
            scope.launch {
                try {
                    val reply = directGateway.complete(config, messages)
                    database.finishDirectTurn(turn.id, reply)
                    mutableState.update { it.copy(error = null) }
                } catch (cancelled: CancellationException) {
                    withContext(NonCancellable) { database.failDirectTurn(turn.id, "模型请求被中断，可能已计费；不会自动重发，请确认后重新发送") }
                    throw cancelled
                } catch (failure: DirectApiFailure) {
                    database.failDirectTurn(turn.id, failure.safeMessage)
                    directError = failure.safeMessage
                    mutableState.update { it.copy(error = failure.safeMessage) }
                } catch (_: Exception) {
                    val message = "模型请求未完成，可能已计费；不会自动重试或转交电脑，请确认后重新发送"
                    database.failDirectTurn(turn.id, message)
                    directError = message
                    mutableState.update { it.copy(error = message) }
                } finally {
                    mutableState.update { it.copy(directChatBusy = false) }
                    directMutex.unlock(); refreshState()
                }
                if (state.value.connectionState == ConnectionState.CONNECTED) poll()
            }
        } catch (invalid: IllegalArgumentException) {
            directMutex.unlock()
            mutableState.update { it.copy(error = invalid.message ?: "消息无法发送") }
        } catch (failure: Exception) {
            directMutex.unlock()
            if (failure is CancellationException) throw failure
            mutableState.update { it.copy(error = "无法保存手机聊天记录，请检查存储空间") }
        }
    }

    /** At most one current or explicitly referenced historical image is loaded. */
    private fun loadDirectImage(id: String, required: Boolean): Pair<String, ByteArray>? {
        val file = imageCache.file(id)
        val metadata = database.image(id)
        if (file == null || metadata == null) {
            require(!required) { "待发图片尚未缓存在手机，请重新选择图片" }
            return null
        }
        val bytes = runCatching { file.readBytes() }.getOrNull()
        if (bytes == null || bytes.isEmpty() || bytes.size > ImageCache.MAX_IMAGE_BYTES || ImageCache.digest(bytes) != id ||
            metadata.mimeType !in setOf("image/png", "image/jpeg", "image/webp")) {
            require(!required) { "待发图片无法读取或校验，请重新选择图片" }
            return null
        }
        return metadata.mimeType to bytes
    }

    private fun routeAuthority(): String = if (database.getSetting("authorization_blocked") == "1") LOCAL_PHONE_AUTHORITY
        else database.device()?.endpointId ?: LOCAL_PHONE_AUTHORITY

    fun apiConfigDraft(): ApiConfigDraft = state.value.directApi.let { ApiConfigDraft(it.baseUrl, it.model, it.timeoutSeconds,
        it.reasoningEffort, it.keySaved, it.source) }

    suspend fun saveDirectApiConfig(baseUrl: String, model: String, apiKey: String, timeoutSeconds: Int,
        reasoningEffort: String): Boolean = withContext(Dispatchers.IO) {
        localReady.await()
        try {
            val saved = synchronized(configLock) {
            val previous = directConfig
            val key = apiKey.trim().ifBlank { previous?.apiKey ?: throw IllegalArgumentException("请输入 API 密钥；首次配置不能留空") }
            val value = JSONObject().put("version", 1).put("revision", UUID.randomUUID().toString()).put("base_url", baseUrl.trim())
                .put("model", model.trim()).put("api_key", key).put("timeout_seconds", timeoutSeconds).put("reasoning_effort", reasoningEffort)
                .put("system_prompt", previous?.systemPrompt ?: DEFAULT_PHONE_PROMPT)
            val validated = try { DirectApiConfig.fromRpc(value, LOCAL_PHONE_AUTHORITY, allowCleartextApi) }
                catch (invalid: IllegalArgumentException) { throw IllegalArgumentException(invalid.message?.replace("电脑", "手机") ?: "手机模型配置无效") }
            val backup = (if (previous?.source == ApiConfigSource.DESKTOP) previous else previous?.desktopCache)?.takeIf {
                it.authorityId == database.device()?.endpointId && database.getSetting("authorization_blocked") != "1"
            }
            val saved = DirectApiConfig(validated.authorityId, validated.revision, validated.baseUrl, validated.model, validated.apiKey,
                validated.systemPrompt, validated.timeoutSeconds, validated.reasoningEffort, validated.syncedAt, ApiConfigSource.LOCAL,
                backup)
            apiVault.save(saved); directConfig = saved
            saved
            }
            database.setSetting("direct_api_blocked", null); directError = null
            mutableState.update { it.copy(directApi = saved.publicInfo(), error = null) }; refreshState()
            true
        } catch (invalid: IllegalArgumentException) { mutableState.update { it.copy(error = invalid.message ?: "手机模型配置无效") }; false }
        catch (_: Exception) { mutableState.update { it.copy(error = "无法加密保存手机模型配置，请检查存储空间后重试") }; false }
    }

    suspend fun sendQueuedOnPhone(requestId: String) {
        localReady.await()
        val config = directConfig
        if (config == null || (config.source != ApiConfigSource.LOCAL &&
                (database.getSetting("authorization_blocked") == "1" || config.authorityId != database.device()?.endpointId))) {
            mutableState.update { it.copy(error = "请先配置手机模型，或连接电脑同步 API 配置") }; return
        }
        val operation = database.operation(requestId)
        if (operation?.item?.canSendOnPhone != true) {
            mutableState.update { it.copy(error = "该请求可能已经送达电脑，不能再次调用模型；请先确认原请求状态") }; return
        }
        sendDirect(requireNotNull(operation.item.conversationId), operation.item.text, config, requestId)
    }

    private fun clearDesktopConfig(message: String = "手机独立模型，无需连接电脑即可对话") {
        synchronized(configLock) {
            val active = directConfig
            if (active?.source == ApiConfigSource.LOCAL) {
                if (active.desktopCache != null) { val clean = active.withDesktopCache(null); apiVault.save(clean); directConfig = clean }
                mutableState.update { it.copy(directApi = (directConfig ?: active).publicInfo().copy(message = message)) }
            } else clearDirectConfig("电脑已撤销授权，已清除同步的 API 密钥；请配置手机模型或重新配对")
            }
    }

    private fun keepConfigOrExplain(message: String, failure: Boolean = false) {
        synchronized(configLock) {
        val cached = directConfig
        mutableState.update { it.copy(directApi = if (cached != null) cached.publicInfo().copy(
            sync = if (failure) ApiConfigSyncState.ERROR else ApiConfigSyncState.READY, message = "$message；继续使用已保存的手机模型")
            else ApiConnectionInfo(sync = if (failure) ApiConfigSyncState.ERROR else ApiConfigSyncState.WAITING, message = "$message；请先配置手机模型")) }
        }
    }

    private fun clearDirectConfig(message: String = "请配置手机模型，或连接电脑自动同步 API 配置") {
        synchronized(configLock) {
            apiVault.clear(); directConfig = null
            mutableState.update { it.copy(directApi = ApiConnectionInfo(message = message)) }
        }
    }

    suspend fun refreshApiConfig() = perform {
        require(state.value.connectionState == ConnectionState.CONNECTED) { "请先连接电脑同步 API 配置" }
        refreshApiConfigInternal()
    }

    private suspend fun refreshApiConfigInternal(force: Boolean = true, initialStatus: JSONObject? = null,
        quiet: Boolean = false): JSONObject? {
        val device = database.device() ?: return initialStatus
        val now = monotonicMillis()
        if (!force && lastApiRefresh?.let { now - it < REFRESH_INTERVAL_MS } == true) return initialStatus
        lastApiRefresh = now
        if (!quiet) mutableState.update { it.copy(directApi = it.directApi.copy(sync = ApiConfigSyncState.SYNCING)) }
        try {
            val checkedStatus = initialStatus ?: status()
            val supported = "chat.config" in cachedCapabilities.orEmpty()
            if (!supported) {
                keepConfigOrExplain("电脑版本未提供手机 API 配置")
                database.setSetting("direct_api_blocked", null)
                return checkedStatus
            }
            val result = call("chat.config")
            require(result.optInt("version") == 1) { "电脑 API 配置版本不兼容，请更新两端应用" }
            if (!result.optBoolean("available")) {
                keepConfigOrExplain("电脑暂未提供可用 API 配置")
                database.setSetting("direct_api_blocked", null)
                mutableState.update { it.copy(directApi = it.directApi.copy(sync = ApiConfigSyncState.READY, lastSyncedAt = System.currentTimeMillis())) }
                return checkedStatus
            }
            val config = DirectApiConfig.fromRpc(result, device.endpointId, allowCleartextApi)
            synchronized(configLock) {
                val previous = directConfig
                val desktop = if (previous?.source == ApiConfigSource.LOCAL) previous.desktopCache else previous
                if (desktop == null || desktop.authorityId != config.authorityId || desktop.revision != config.revision) {
                    val active = if (previous?.source == ApiConfigSource.LOCAL) previous.withDesktopCache(config) else config
                    apiVault.save(active); directConfig = active
                }
            }
            database.setSetting("direct_api_blocked", null)
            mutableState.update { it.copy(directApi = (directConfig ?: config).publicInfo()) }
            return checkedStatus
        } catch (invalid: IllegalArgumentException) {
            database.setSetting("direct_api_blocked", if (directConfig == null) "1" else null)
            keepConfigOrExplain(invalid.message ?: "电脑 API 配置无效", failure = true)
            return initialStatus
        } catch (cancelled: CancellationException) {
            mutableState.update { it.copy(directApi = it.directApi.copy(sync = ApiConfigSyncState.WAITING)) }
            throw cancelled
        } catch (failure: Exception) {
            mutableState.update { it.copy(directApi = it.directApi.copy(sync = ApiConfigSyncState.ERROR,
                message = if (it.directApi.available) "API 配置刷新失败，仍可使用手机已缓存的配置" else "API 配置同步失败，请连接电脑后重试")) }
            throw failure
        }
    }

    private fun protectedImages(): Set<String> = database.protectedImageIds()

    suspend fun stageImage(conversationId: String, bytes: ByteArray, filename: String, mimeType: String) = localAction {
        require(canonicalUuid(conversationId)) { "会话标识无效" }
        require(!database.isConversationDeleted(conversationId)) { "该会话已在电脑删除，请新建会话" }
        val label = safeImageFilename(filename)
        val stored = imageCache.store(bytes, mimeType, protectedImages())
        database.createConversation(conversationId, select = false)
        database.stageImage(conversationId, ImageItem(stored.id, label, stored.mimeType, bytes.size.toLong(),
            stored.width, stored.height, Instant.now().toString(), stored.file.absolutePath, "staged"))
    }

    suspend fun removePendingImage(conversationId: String) = localAction { database.removePendingImage(conversationId) }

    private fun safeImageFilename(filename: String): String {
        val label = filename.replace('\\', '/').substringAfterLast('/').take(160).ifBlank { "image" }
        require(label.none { it.code < 32 }) { "图片文件名无效" }
        return label
    }

    suspend fun sendImage(bytes: ByteArray, filename: String, mimeType: String) = perform {
        val label = safeImageFilename(filename)
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

    private suspend fun flushOutbox(initialStatus: JSONObject? = null, onlyRequestId: String? = null): Boolean {
        var touched = false
        var freshStatus = initialStatus
        val authority = database.device()?.endpointId ?: return false
        val outgoing = if (onlyRequestId == null) database.outgoing() else listOfNotNull(database.operation(onlyRequestId))
        for (operation in outgoing) {
            val item = operation.item
            var attempted = operation.attempted
            val desktopAction = operation.isDesktopAction()
            if (operation.authorityId != authority || item.conversationId?.let(database::isConversationDeleted) == true) {
                database.updateOutbox(item.requestId, "needs_attention", "原会话或电脑已不可用，请手动处理")
                touched = true
                continue
            }
            if (desktopAction && !attempted && item.requestId != onlyRequestId) {
                database.updateOutbox(item.requestId, "needs_attention", "电脑请求尚未投递；不会在重连后自动执行，请确认后重新发送")
                touched = true
                continue
            }
            if (item.messageId != null && database.hasMessage(item.messageId)) {
                database.updateOutbox(item.requestId, "sent"); touched = true; continue
            }
            if (item.status == "pending" && System.currentTimeMillis() - operation.checkedAt < 15_000) continue
            try {
                if (operation.attempted) {
                    val saved = call("operation.get", JSONObject().put("request_id", item.requestId))
                    touched = true
                    database.updateOutbox(item.requestId, item.status, checkedAt = System.currentTimeMillis())
                    if (saved.optString("state") != "not_found") { handleOperation(operation, saved); continue }
                    if (desktopAction) {
                        database.updateOutbox(item.requestId, "needs_attention", "电脑未登记原请求；不会自动重新截图或执行，请确认后重新发送")
                        continue
                    }
                }
                if (desktopAction) require((when (item.method) {
                    "desktop.snapshot" -> "desktop.snapshot"
                    "agent.send" -> "agent.send"
                    else -> "chat.desktop_context"
                }) in cachedCapabilities.orEmpty()) {
                    "电脑版本暂不支持此操作，请更新并重启电脑端"
                }
                if (item.method in setOf("chat.send", "agent.send", "chat.import")) {
                    val checkedStatus = if (item.method in setOf("chat.send", "agent.send")) (freshStatus ?: status()).also { freshStatus = null }
                        else if (cachedCapabilities == null) status() else null
                    if (item.method == "agent.send") require(cachedAgentAvailable) {
                        cachedAgentUnavailableReason ?: "电脑 Agent 尚未就绪，请检查电脑任务助手设置"
                    }
                    if (item.method == "chat.import") {
                        require("chat.import" in cachedCapabilities.orEmpty()) {
                            "电脑版本不支持同步手机聊天记录，请更新电脑；手机回复已保留，不会重新调用模型"
                        }
                    }
                    if (item.attachmentId != null && item.method in setOf("chat.send", "agent.send")) {
                        val supported = "chat.image" in cachedCapabilities.orEmpty()
                        if (!supported) {
                            database.updateOutbox(item.requestId, "queued", IMAGE_CHAT_UPDATE_REQUIRED)
                            throw RpcFailure("image_chat_unavailable", IMAGE_CHAT_UPDATE_REQUIRED)
                        }
                    }
                    if (item.method == "agent.send" && checkedStatus?.optBoolean("agent_busy") == true) {
                        throw IllegalArgumentException("电脑 Agent 正在处理其他请求，请完成后重新发送")
                    }
                    if (item.method == "chat.send" && checkedStatus?.optBoolean("chat_busy") == true) {
                        require(!desktopAction) { "电脑正在处理其他请求，请完成后重新发送电脑协助" }
                        break
                    }
                    if (operation.dependencyId != null && !uploadChatAttachment(operation.dependencyId)) break
                }
                val params = JSONObject(operation.params.toString())
                if (item.method == "image.upload") {
                    val file = item.attachmentId?.let(imageCache::file)
                    require(file != null) { "待发图片已缺失，请重新选择" }
                    params.put("data_base64", Base64.encodeToString(file.readBytes(), Base64.NO_WRAP))
                }
                val messageId = if (item.method in setOf("chat.send", "agent.send")) stableMessageId(rpc.endpointId, item.requestId) else null
                if (!database.claimOutboxForSend(item.requestId, messageId)) continue
                attempted = true
                refreshState()
                val result = call(item.method, params, item.requestId)
                touched = true
                if (result.optBoolean("duplicate")) handleOperation(operation, result)
                else acceptOperation(operation, result)
                if (item.method in setOf("chat.send", "agent.send")) break // The desktop owns one AI generation at a time.
            } catch (cancelled: CancellationException) {
                database.updateOutbox(item.requestId, if (attempted) "unknown" else "queued", "等待恢复连接后确认投递状态", attempted = attempted)
                throw cancelled
            } catch (failure: RpcFailure) {
                if (failure.code == "image_chat_unavailable") throw failure
                if (desktopAction && failure.code == "desktop_permission_denied") cachedDesktopAllowed = false
                if (desktopAction) mutableState.update { it.copy(error = when (failure.code) {
                    "desktop_permission_denied" -> "电脑尚未允许远程查看屏幕，请在电脑设置中检查权限后重试"
                    "desktop_capture_unavailable" -> "暂时无法读取电脑画面，请确认电脑未暂停且当前窗口可以捕获"
                    else -> failure.message
                }) }
                if (failure.code == "unauthorized") {
                    database.updateOutbox(item.requestId, "unknown", "授权已撤销，待发内容已保留")
                    throw failure
                }
                val retryable = failure.code in setOf("rate_limited", "busy", "disabled", "storage_error", "operation_failed")
                database.updateOutbox(item.requestId, if (retryable) "unknown" else "failed", failure.message)
                if (failure.code == "disabled") throw failure
                if (retryable) break
            } catch (transport: RpcTransportException) {
                database.updateOutbox(item.requestId, if (attempted) "unknown" else "queued", "响应未确认；重连后查询原操作", attempted = attempted)
                throw transport
            } catch (invalid: IllegalArgumentException) {
                database.updateOutbox(item.requestId, "needs_attention", invalid.message ?: "内容需手动处理")
            } catch (_: Exception) {
                database.updateOutbox(item.requestId, if (attempted) "unknown" else "queued", "响应未确认；重连后会查询原操作，不会重新生成 AI 请求", attempted = attempted)
                throw RpcFailure("connection_lost", "连接中断，消息状态尚待确认")
            }
        }
        return touched
    }

    private fun QueuedOperation.isDesktopAction(): Boolean = item.method in setOf("desktop.snapshot", "agent.send") ||
        (item.method == "chat.send" && params.optBoolean("desktop_context"))

    /** Upload has its own durable request ID, and is confirmed before chat is admitted. */
    private suspend fun uploadChatAttachment(requestId: String): Boolean {
        val operation = database.operation(requestId) ?: throw IllegalArgumentException("待发图片任务缺失，请手动处理此消息")
        val item = operation.item
        if (item.status == "sent") return true
        require(item.status !in setOf("discarded", "failed", "needs_attention")) { item.error ?: "图片上传需要手动处理" }
        try {
            if (operation.attempted) {
                val saved = call("operation.get", JSONObject().put("request_id", requestId))
                if (saved.optString("state") != "not_found") {
                    handleOperation(operation, saved)
                    return database.operation(requestId)?.item?.status == "sent"
                }
            }
            val file = item.attachmentId?.let(imageCache::file)
            require(file != null) { "待发图片已从本地丢失，请重新选择" }
            val params = JSONObject(operation.params.toString()).put("data_base64", Base64.encodeToString(file.readBytes(), Base64.NO_WRAP))
            if (!database.claimOutboxForSend(requestId, null)) return false
            val result = call("image.upload", params, requestId)
            if (result.optBoolean("duplicate")) handleOperation(operation, result) else acceptOperation(operation, result)
            return database.operation(requestId)?.item?.status == "sent"
        } catch (cancelled: CancellationException) {
            database.updateOutbox(requestId, "unknown", "图片响应待确认，重连后查询同一上传请求")
            throw cancelled
        } catch (failure: Exception) {
            database.updateOutbox(requestId, "unknown", "图片响应待确认，重连后查询同一上传请求")
            throw failure
        }
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
            "chat.send", "agent.send" -> {
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
            "chat.import" -> database.acceptDirectImport(item.requestId, result)
            "desktop.snapshot" -> {
                val image = imageFromJson(result, "received")
                database.putImage(image)
                database.updateOutbox(item.requestId, "sent", attachmentId = image.id)
            }
        }
    }

    suspend fun retryOutbox(requestId: String) = perform {
        val operation = database.operation(requestId) ?: return@perform
        val item = operation.item
        require(item.conversationId?.let(database::isConversationDeleted) != true) { "会话已删除，请复制内容后新建会话" }
        require(operation.authorityId == database.device()?.endpointId) { "待发内容属于另一台电脑，请手动复制处理" }
        if (operation.isDesktopAction()) {
            require(state.value.connectionState == ConnectionState.CONNECTED) { "请先连接电脑确认原请求状态" }
            require(operation.attempted) { "电脑请求尚未投递，请确认后重新发送" }
            val saved = call("operation.get", JSONObject().put("request_id", requestId))
            if (saved.optString("state") == "not_found") database.updateOutbox(requestId, "needs_attention",
                "电脑未登记原请求；不会自动重新截图或执行，请确认后重新发送")
            else handleOperation(operation, saved)
            return@perform
        }
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
            database.addOutbox(item.method, operation.params, item.conversationId, item.text, item.attachmentId, operation.dependencyId)
            database.updateOutbox(requestId, "discarded", "已由用户明确重新发送")
        } else if (item.status == "needs_attention") {
            throw IllegalArgumentException("请检查内容和原会话，复制到新会话后再发送")
        }
        if (state.value.connectionState == ConnectionState.CONNECTED) syncInternal()
    }

    suspend fun discardOutbox(requestId: String) = perform {
        database.operation(requestId)?.dependencyId?.let {
            database.updateOutbox(it, "discarded", "关联图文消息已取消，不再继续上传")
        }
        database.updateOutbox(requestId, "discarded", "已取消本地排队；已到达电脑的请求不能撤回")
    }

    suspend fun refreshImages() = perform {
        require(state.value.connectionState == ConnectionState.CONNECTED) { "请先连接电脑" }
        refreshImagesInternal()
    }

    private suspend fun refreshImagesInternal(): Boolean {
        var offset = 0
        var pages = 0
        var changed = false
        val previous = database.images().associateBy { it.id }
        val remoteIds = mutableSetOf<String>()
        do {
            require(++pages <= 1000) { "图片分页超过限制" }
            val page = call("image.list", JSONObject().put("offset", offset).put("limit", 100))
            val items = page.getJSONArray("items")
            for (i in 0 until items.length()) {
                val image = imageFromJson(items.getJSONObject(i), "received")
                remoteIds.add(image.id)
                if (image.copy(localPath = previous[image.id]?.localPath) != previous[image.id]) {
                    database.putImage(image); changed = true
                }
            }
            val next = page.getInt("next_offset")
            require(!page.getBoolean("has_more") || next > offset) { "图片分页游标无效" }
            offset = next
        } while (page.getBoolean("has_more"))
        changed = database.reconcileImages(remoteIds) || changed
        lastImagesRefresh = monotonicMillis()
        return changed
    }

    suspend fun downloadImage(id: String): File? {
        imageCache.file(id)?.let { return it }
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
        require(database.pendingImages().values.none { it.attachmentId == id } && database.outbox().none {
            it.method in setOf("chat.send", "agent.send") && it.attachmentId == id && it.status !in setOf("sent", "discarded")
        }) { "图片仍在待发消息或输入框中，请先移除附件或取消待发消息" }
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

    fun clearError() { directError = null; mutableState.update { it.copy(error = null) } }

    suspend fun close() {
        scope.coroutineContext[kotlinx.coroutines.Job]?.cancelAndJoin()
        withContext(Dispatchers.IO) { mutex.withLock { rpc.close(); database.close() } }
    }

    private fun imageFromJson(value: JSONObject, uploadState: String): ImageItem = ImageItem(value.getString("id"),
        value.optString("filename", "image"), value.optString("mime_type"), value.getLong("size"), value.getInt("width"),
        value.getInt("height"), value.optString("created_at"), uploadState = uploadState)

    companion object {
        private const val REFRESH_INTERVAL_MS = 30_000L
        private const val DEFAULT_PHONE_PROMPT = "你是 AURA，一位温柔自然的中文陪伴助手。请依据当前对话回答，不猜测不可见的图片或电脑状态。"
        internal const val IMAGE_CHAT_UPDATE_REQUIRED = "电脑端尚不支持图文识别，请更新并重启电脑端后重试；图片和文字已保留，未发送。"
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
