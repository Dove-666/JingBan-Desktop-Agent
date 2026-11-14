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
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.withContext
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference

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

/** Shared repository for the visible app and its optional task-notification service. */
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
    private class ConnectionOperation(val epoch: Any, val job: Job)
    private val connectionLock = Any()
    private var connectionEpoch = Any()
    private var lastEstablishedConnection = Any()
    private var activeConnection: ConnectionOperation? = null
    @Volatile private var directJob: Job? = null
    private data class OwnedDirectPreview(val requestId: String, val peerId: String?, val generation: String?,
        val userId: String, val config: DirectApiConfig, val message: Message)
    @Volatile private var directPreview: OwnedDirectPreview? = null
    private val configLock = Any()
    @Volatile private var directConfig: DirectApiConfig? = runCatching { apiVault.load() }.getOrNull()
    @Volatile private var syncedPersona: SyncedPersona? = runCatching { apiVault.loadPersona() }.getOrNull()
    @Volatile private var characterCatalog: CharacterCatalogCache? = runCatching { apiVault.loadCharacters() }.getOrNull()
    private val knownSecretValues = linkedSetOf<String>().apply { addAll(directConfig?.knownSecrets().orEmpty()) }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val localReady = CompletableDeferred<Unit>()
    private val imageCache = ImageCache(context.applicationContext)
    private val contextCache = ConversationContextCache(database)
    private val mutableState = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = mutableState.asStateFlow()
    private var lastImagesRefresh = 0L
    private var lastApiRefresh: Long? = null
    private var lastSyncStamp: Long? = null
    private var lastContextRefresh: Long? = null
    private var cachedCapabilities: Set<String>? = null
    private var cachedDesktopAllowed: Boolean? = null
    private var cachedAgentAvailable = false
    private var cachedAgentUnavailableReason: String? = null
    private var cachedAgentBusy = false
    private var cachedAgentConversationId: String? = null
    private var cachedAgentInteraction: AgentInteraction? = null
    private var cachedArtifactOwner: Pair<String?, String?>? = null
    private var cachedArtifacts: List<AgentArtifact> = emptyList()
    private val artifactCache = File(context.applicationContext.cacheDir, "agent-deliveries")
    @Volatile private var directError: String? = null

    init { scope.launch {
        try {
            // Only abandoned private delivery chunks; never touch chosen destinations.
            artifactCache.listFiles()?.filter { it.name.startsWith("delivery-") && it.name.endsWith(".part") &&
                it.lastModified() < System.currentTimeMillis() - 86_400_000L }?.forEach { it.delete() }
            database.recoverInterruptedDirectTurns()
            synchronized(configLock) {
                if (characterCatalog != null && currentCharacterCatalog() == null) clearCharacterCatalog()
                if (syncedPersona != null && currentPersona() == null) clearSyncedPersona()
                val persona = currentPersona()
                val active = directConfig
                if (persona != null && active != null && active.systemPrompt != active.redact(persona.systemPrompt)) {
                    val updated = active.withPersona(persona)
                    apiVault.save(updated); directConfig = updated
                }
            }
            val cached = directConfig
            if (cached != null && cached.source != ApiConfigSource.LOCAL && ((database.device()?.endpointId != null &&
                    cached.authorityId != database.device()?.endpointId) || database.getSetting("authorization_blocked") == "1")) clearDirectConfig()
            else if (cached != null) mutableState.update { it.copy(directApi = if (cached.source == ApiConfigSource.DESKTOP && database.device() == null)
                cached.publicInfo(boundarySecrets(cached)).copy(available = false, message = "此配置来自原电脑，请先保存为手机独立模型或重新配对") else cached.publicInfo(boundarySecrets(cached))) }
            refreshState()
        } catch (_: Exception) { mutableState.update { it.copy(error = "无法读取本地缓存，请检查手机存储空间") } }
        finally { localReady.complete(Unit) }
    } }

    private fun boundarySecrets(config: DirectApiConfig? = null, extra: String? = null): List<String> = synchronized(configLock) {
        knownSecretValues.addAll(directConfig?.knownSecrets().orEmpty())
        knownSecretValues.addAll(config?.knownSecrets().orEmpty())
        extra?.takeIf { it.isNotBlank() && it.length <= 16384 && it.none { c -> c.code < 32 || c.code == 127 } }
            ?.let(knownSecretValues::add)
        knownSecretValues.toList()
    }

    private fun ownsDirectPreview(owner: OwnedDirectPreview): Boolean {
        val active = directConfig ?: return false
        val original = owner.config
        return owner.peerId == database.device()?.endpointId && owner.generation == database.conversationGeneration(owner.message.conversationId) &&
            database.directTurnIsGenerating(owner.requestId, owner.message.conversationId, owner.userId, owner.message.id) &&
            original.authorityId == active.authorityId && original.source == active.source && original.revision == active.revision &&
            original.baseUrl == active.baseUrl && original.model == active.model && original.apiKey == active.apiKey &&
            directPreview?.let { it.requestId == owner.requestId && it.message.id == owner.message.id &&
                it.message.conversationId == owner.message.conversationId } == true
    }

    private fun currentDirectPreview(): Message? {
        val owner = directPreview ?: return null
        if (ownsDirectPreview(owner)) return owner.message
        directPreview = null
        directJob?.cancel()
        return null
    }

    private fun refreshState() {
        val conversations = database.conversations()
        var selected = database.getSetting("selected_conversation")
        if (selected == null || conversations.none { it.id == selected }) {
            selected = conversations.firstOrNull()?.id
            database.setSetting("selected_conversation", selected)
        }
        mutableState.update { it.copy(device = database.device(), conversations = conversations,
            selectedConversationId = selected, messages = database.messages(selected).let { messages ->
                currentDirectPreview()?.takeIf { preview -> preview.conversationId == selected }?.let { preview ->
                    messages.filter { message -> message.id != preview.id } + preview
                } ?: messages
            }, outbox = database.outbox(),
            images = database.images(), pendingImages = database.pendingImages(),
            desktopAgentConversations = database.desktopAgentConversations(),
            lastSyncedAt = database.getSetting("last_synced_at")?.toLongOrNull(), historyPendingCount = database.historyPendingCount(),
            error = directError ?: it.error,
            agentArtifacts = cachedArtifacts.takeIf { _ ->
                cachedArtifactOwner == (database.device()?.endpointId to selected) &&
                    it.connectionState == ConnectionState.CONNECTED && database.getSetting("authorization_blocked") != "1"
            }.orEmpty(),
            desktopTaskAvailable = it.connectionState == ConnectionState.CONNECTED &&
                database.getSetting("authorization_blocked") != "1" &&
                cachedAgentAvailable && "agent.send" in cachedCapabilities.orEmpty(),
            desktopAgentBusy = it.connectionState == ConnectionState.CONNECTED && cachedAgentBusy,
            desktopAgentConversationId = cachedAgentConversationId,
            agentInteraction = cachedAgentInteraction?.takeIf { interaction ->
                it.connectionState == ConnectionState.CONNECTED && cachedAgentBusy &&
                    interaction.authorityId == database.device()?.endpointId &&
                    database.getSetting("authorization_blocked") != "1" && "agent.answer" in cachedCapabilities.orEmpty()
            },
            desktopSteeringAvailable = it.connectionState == ConnectionState.CONNECTED &&
                "agent.steer" in cachedCapabilities.orEmpty() && database.getSetting("authorization_blocked") != "1",
            desktopTaskMessage = when {
                it.connectionState == ConnectionState.UNAUTHORIZED -> "电脑授权已撤销，请重新配对后使用电脑 Agent"
                it.connectionState != ConnectionState.CONNECTED -> "请先连接电脑；普通聊天仍使用手机模型"
                "agent.send" !in cachedCapabilities.orEmpty() -> "请更新并重启电脑端后使用电脑 Agent"
                !cachedAgentAvailable -> cachedAgentUnavailableReason ?: "电脑 Agent 尚未就绪，请检查电脑任务助手设置"
                cachedAgentBusy -> "电脑 Agent 正在执行任务，可查看进度或停止当前任务"
                else -> "本会话消息交给电脑 Agent，使用电脑端已启用的工具与任务上下文"
            }) }
        publishCharacterState()
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

    private suspend fun connectionOperation(silent: Boolean = false,
        block: suspend (ConnectionOperation) -> Unit) = supervisorScope {
        val epoch = synchronized(connectionLock) { connectionEpoch }
        // A user disconnect may cancel this child, never its foreground caller.
        val work = async(Dispatchers.IO) {
            val owner = ConnectionOperation(epoch, currentCoroutineContext()[Job]!!)
            perform(refreshAfter = false, silent = silent) {
                val admitted = synchronized(connectionLock) {
                    if (epoch !== connectionEpoch) false
                    else { activeConnection = owner; true }
                }
                if (!admitted) return@perform
                try {
                    currentCoroutineContext().ensureActive()
                    block(owner)
                    refreshState()
                } finally {
                    synchronized(connectionLock) { if (activeConnection === owner) activeConnection = null }
                }
            }
        }
        try { work.await() }
        catch (cancelled: CancellationException) {
            currentCoroutineContext().ensureActive()
            if (synchronized(connectionLock) { epoch === connectionEpoch }) throw cancelled
        }
    }

    private suspend fun ensureConnectionCurrent(owner: ConnectionOperation) {
        currentCoroutineContext().ensureActive()
        if (synchronized(connectionLock) { owner.epoch !== connectionEpoch || activeConnection !== owner }) {
            throw CancellationException("Connection request was superseded")
        }
    }

    private suspend fun connectOwned(owner: ConnectionOperation, ticket: String) {
        ensureConnectionCurrent(owner)
        rpc.connect(ticket)
        currentCoroutineContext().ensureActive()
        val established = synchronized(connectionLock) {
            if (owner.epoch !== connectionEpoch || activeConnection !== owner) false
            else { lastEstablishedConnection = Any(); true }
        }
        if (!established) throw CancellationException("Connection request was superseded")
    }

    suspend fun pair(pairInfo: String, deviceName: String) = connectionOperation { owner ->
        val info = parsePairing(pairInfo)
        require(deviceName.trim().length in 1..80 && deviceName.none { it.code < 32 }) { "设备名称须为 1～80 个字符" }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTING, error = null) }
        connectOwned(owner, info.getString("ticket"))
        call("pair", JSONObject().put("code", info.getString("code")).put("name", deviceName.trim()))
        ensureConnectionCurrent(owner)
        val desktopId = info.optString("endpoint_id").ifBlank { info.getString("ticket").take(80) }
        // Only the public ticket/IDs/name are persisted. Neither code nor the
        // original pairing URI/JSON is ever written to settings, logs or disk.
        database.saveDevice(DeviceInfo("我的电脑", desktopId, info.getString("ticket")), rpc.endpointId)
        cachedCapabilities = null
        synchronized(configLock) {
            if (syncedPersona?.authorityId != desktopId) clearSyncedPersona()
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

    suspend fun reconnect(silent: Boolean = false) = connectionOperation(silent) { owner ->
        val device = database.device() ?: throw IllegalArgumentException("请先在电脑生成配对信息")
        if (database.getSetting("authorization_blocked") == "1") {
            mutableState.update { it.copy(connectionState = ConnectionState.UNAUTHORIZED,
                error = "授权已撤销，请手动重新配对；应用不会自动配对") }
            return@connectionOperation
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTING, error = if (silent) directError else null) }
        connectOwned(owner, device.ticket)
        val status = status()
        ensureConnectionCurrent(owner)
        if (database.getSetting("selected_conversation") == null) {
            database.setSetting("selected_conversation", status.optionalString("current_conversation_id"))
        }
        mutableState.update { it.copy(connectionState = ConnectionState.CONNECTED, connectionType = rpc.connectionType,
            error = if (silent) directError else null) }
        syncInternal(forceConfig = true, initialStatus = status)
        refreshImagesInternal()
    }

    suspend fun disconnect() {
        currentCoroutineContext().ensureActive()
        val epoch = Any()
        val (previousConnection, previousJob) = synchronized(connectionLock) {
            connectionEpoch = epoch
            lastEstablishedConnection to activeConnection?.job
        }
        previousJob?.cancel()
        perform(refreshAfter = false) {
            if (synchronized(connectionLock) { epoch !== connectionEpoch ||
                    previousConnection !== lastEstablishedConnection }) return@perform
            rpc.close()
            cachedCapabilities = null
            mutableState.update { it.copy(connectionState = ConnectionState.DISCONNECTED, syncState = SyncState.IDLE,
                connectionType = "unknown", error = null) }
            refreshState()
        }
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

    private suspend fun status(): JSONObject {
      val expectedAuthority = database.device()?.endpointId
      return call("status").also { value ->
        require(expectedAuthority == database.device()?.endpointId) { "目标电脑已更换，请重新连接" }
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
        if ("context.get" !in cachedCapabilities.orEmpty()) contextCache.clear()
        cachedDesktopAllowed = value.optJSONObject("desktop_context")?.opt("available") as? Boolean
        cachedAgentAvailable = value.opt("agent_available") == true
        cachedAgentUnavailableReason = value.optionalString("agent_unavailable_reason")?.takeIf {
            it in AGENT_ERROR_MESSAGES.values || it in AGENT_UNAVAILABLE_MESSAGES
        }
        cachedAgentBusy = value.optBoolean("agent_busy")
        cachedAgentConversationId = value.optionalString("agent_conversation_id")?.takeIf(::canonicalUuid)
        cachedAgentInteraction = parseAgentInteraction(value.optJSONObject("agent_interaction"), expectedAuthority)
            ?.takeIf { cachedAgentBusy && it.conversationId == cachedAgentConversationId }
        refreshArtifacts(expectedAuthority)
        mutableState.update { it.copy(desktopAgentBusy = cachedAgentBusy, desktopAgentConversationId = cachedAgentConversationId,
            agentArtifacts = cachedArtifacts.takeIf { _ -> cachedArtifactOwner == (expectedAuthority to database.getSetting("selected_conversation")) &&
                it.connectionState == ConnectionState.CONNECTED && database.getSetting("authorization_blocked") != "1" }.orEmpty(),
            agentInteraction = cachedAgentInteraction?.takeIf { interaction ->
                it.connectionState == ConnectionState.CONNECTED && interaction.authorityId == database.device()?.endpointId &&
                    database.getSetting("authorization_blocked") != "1" && "agent.answer" in cachedCapabilities.orEmpty()
            },
            desktopSteeringAvailable = it.connectionState == ConnectionState.CONNECTED &&
                "agent.steer" in cachedCapabilities.orEmpty() && database.getSetting("authorization_blocked") != "1",
            desktopTaskAvailable = it.connectionState == ConnectionState.CONNECTED &&
                database.getSetting("authorization_blocked") != "1" && cachedAgentAvailable && "agent.send" in cachedCapabilities.orEmpty(),
            desktopTaskMessage = when {
                "agent.send" !in cachedCapabilities.orEmpty() -> "请更新并重启电脑端后使用电脑 Agent"
                !cachedAgentAvailable -> cachedAgentUnavailableReason ?: "电脑 Agent 尚未就绪，请检查电脑任务助手设置"
                cachedAgentBusy -> "电脑 Agent 正在执行任务，可查看进度或停止当前任务"
                else -> "本会话消息交给电脑 Agent，使用电脑端已启用的工具与任务上下文"
            }) }
      }
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
        database.getSetting("selected_conversation")?.let { cid ->
            val generation = database.conversationGeneration(cid)
            if (forceConfig || changed || lastContextRefresh?.let { monotonicMillis() - it >= REFRESH_INTERVAL_MS } != false ||
                contextCache.read(database.device()?.endpointId.orEmpty(), cid, generation) == null)
                refreshConversationContext(cid)
        }
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

    suspend fun selectConversation(id: String?) {
        localAction { database.setSetting("selected_conversation", id) }
        if (state.value.connectionState == ConnectionState.CONNECTED && "artifact.list" in cachedCapabilities.orEmpty())
            perform(silent = true) { refreshArtifacts(database.device()?.endpointId) }
    }

    private suspend fun refreshArtifacts(expectedAuthority: String?) {
        val cid = database.getSetting("selected_conversation")
        if (cid == null || !canonicalUuid(cid) || "artifact.list" !in cachedCapabilities.orEmpty()) {
            cachedArtifacts = emptyList(); cachedArtifactOwner = null; return
        }
        val response = call("artifact.list", JSONObject().put("conversation_id", cid))
        require(expectedAuthority == database.device()?.endpointId && cid == database.getSetting("selected_conversation")) {
            "会话或目标电脑已更换，请重新选择文件"
        }
        cachedArtifacts = parseAgentArtifacts(response.optJSONArray("artifacts"))
        cachedArtifactOwner = expectedAuthority to cid
    }

    /** Explicit click only. Chunks stay private until the complete file hash is verified. */
    suspend fun downloadArtifact(conversationId: String, reference: String): DownloadedArtifact? {
        localReady.await()
        if (!desktopReady("artifact.get")) return null
        val expectedAuthority = database.device()?.endpointId
        val original = state.value.agentArtifacts.firstOrNull { it.reference == reference } ?: return null
        var result: DownloadedArtifact? = null
        perform {
            status()
            requireDesktopReady("artifact.get", expectedAuthority)
            require(canonicalUuid(conversationId) && !database.isConversationDeleted(conversationId) &&
                cachedArtifactOwner == (expectedAuthority to conversationId) && original in cachedArtifacts) { "文件已更新，请重新选择" }
            artifactCache.mkdirs()
            val temporary = File.createTempFile("delivery-", ".part", artifactCache)
            try {
                val digest = MessageDigest.getInstance("SHA-256")
                var offset = 0L
                temporary.outputStream().use { output ->
                    do {
                        requireDesktopReady("artifact.get", expectedAuthority)
                        require(database.getSetting("selected_conversation") == conversationId) { "会话已切换，请重新选择文件" }
                        val chunk = call("artifact.get", JSONObject().put("conversation_id", conversationId)
                            .put("reference", reference).put("offset", offset).put("limit", 262144))
                        require(parseAgentArtifact(chunk) == original && (chunk.opt("offset") is Int || chunk.opt("offset") is Long) &&
                            chunk.getLong("offset") == offset && (chunk.opt("total_bytes") is Int || chunk.opt("total_bytes") is Long) &&
                            chunk.getLong("total_bytes") == original.bytes) { "文件元信息发生变化，下载已停止" }
                        val encoded = chunk.opt("data_base64") as? String ?: error("文件内容无效")
                        require(encoded.length <= 349528 && encoded.length % 4 == 0 && encoded.matches(Regex("[A-Za-z0-9+/]*={0,2}"))) { "文件分块无效" }
                        val bytes = Base64.decode(encoded, Base64.NO_WRAP)
                        require(bytes.size <= 262144 && offset + bytes.size <= original.bytes) { "文件分块超出范围" }
                        output.write(bytes); digest.update(bytes); offset += bytes.size
                        val next = chunk.opt("next_offset")
                        require(if (offset == original.bytes) next == null || next == JSONObject.NULL
                            else bytes.isNotEmpty() && (next is Int || next is Long) && (next as Number).toLong() == offset) { "文件分块顺序无效" }
                    } while (offset < original.bytes)
                }
                requireDesktopReady("artifact.get", expectedAuthority)
                require(database.getSetting("selected_conversation") == conversationId) { "会话已切换，请重新选择文件" }
                val actual = digest.digest().joinToString("") { "%02x".format(it) }
                require(actual == original.sha256 && temporary.length() == original.bytes) { "文件校验未通过，请重新下载" }
                result = DownloadedArtifact(original, temporary)
            } finally { if (result == null) temporary.delete() }
        }
        return result
    }

    suspend fun setDesktopAgentMode(conversationId: String, enabled: Boolean) = localAction {
        require(canonicalUuid(conversationId)) { "会话标识无效" }
        database.setDesktopAgentMode(conversationId, enabled)
    }

    suspend fun newConversation(): String = withContext(Dispatchers.IO) {
        localReady.await(); database.createConversation().also { refreshState() }
    }

    suspend fun sendMessage(conversationId: String, text: String): Boolean {
        localReady.await()
        // Do not acquire the PC mutex before choosing the cached direct route:
        // a desktop reconnect may be waiting on its 30-second network timeout.
        val config = directConfig
        if (config != null && (config.source == ApiConfigSource.LOCAL || (config.authorityId == database.device()?.endpointId &&
                database.getSetting("authorization_blocked") != "1"))) {
            return sendDirect(conversationId, text, config)
        }
        if (database.getSetting("direct_api_blocked") == "1") {
            mutableState.update { it.copy(error = "电脑 API 配置无法安全用于手机，请检查 API 地址和配置后重新同步") }
            return false
        }
        mutableState.update { it.copy(error = "请先配置手机模型，或连接电脑同步 API 配置；当前消息尚未发送") }
        return false
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
            require(!checkedStatus.optBoolean("agent_busy") || permitsQueuedAgent(checkedStatus)) {
                "电脑 Agent 正在处理其他请求，请完成后重新发送"
            }
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

    /** Explicit live answers are never queued, retried, or replayed after reconnect. */
    suspend fun answerAgentInteraction(conversationId: String, interactionId: String, answer: JSONObject): Boolean {
        localReady.await()
        if (!desktopReady("agent.answer")) return false
        val original = state.value.agentInteraction
        val expectedAuthority = database.device()?.endpointId
        if (original == null || original.id != interactionId || original.conversationId != conversationId) {
            mutableState.update { it.copy(error = "问题已更新，请查看当前请求后再回答") }; return false
        }
        val cleaned = validatedAgentAnswer(original, answer) ?: run {
            mutableState.update { it.copy(error = "答复格式无效") }; return false
        }
        var accepted = false
        perform {
            status()
            requireDesktopReady("agent.answer", expectedAuthority)
            require(canonicalUuid(conversationId) && !database.isConversationDeleted(conversationId)) { "会话已删除或无效" }
            require(cachedAgentInteraction == original && original.authorityId == expectedAuthority) {
                "问题或访问范围已更新，请重新确认当前请求"
            }
            val result = call("agent.answer", JSONObject().put("conversation_id", conversationId)
                .put("interaction_id", interactionId).put("answer", cleaned))
            accepted = result.opt("accepted") == true
            if (accepted) cachedAgentInteraction = null
            status()
            pullChanges()
        }
        return accepted
    }

    /** Steering changes the running task at a step boundary, never via outbox. */
    suspend fun steerDesktopTask(conversationId: String, text: String): Boolean {
        localReady.await()
        if (!desktopReady("agent.steer")) return false
        val expectedAuthority = database.device()?.endpointId
        val body = text.trim()
        var accepted = false
        perform {
            status()
            requireDesktopReady("agent.steer", expectedAuthority)
            require(canonicalUuid(conversationId) && !database.isConversationDeleted(conversationId)) { "会话已删除或无效" }
            require(body.length in 1..8000 && body.none { it.code < 32 && it != '\n' && it != '\t' }) { "请输入 1～8000 字的补充指令" }
            require(cachedAgentBusy && cachedAgentConversationId == conversationId) { "当前会话没有正在执行的电脑任务" }
            require(cachedAgentInteraction == null) { "请先回答任务中的待确认问题" }
            val result = call("agent.steer", JSONObject().put("conversation_id", conversationId).put("text", body))
            accepted = result.opt("accepted") == true
            status()
            pullChanges()
        }
        return accepted
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
        legacyRequestId: String? = null, expectedAuthority: String = if (config.source == ApiConfigSource.DESKTOP) config.authorityId else routeAuthority()): Boolean = withContext(Dispatchers.IO) {
        if (!directMutex.tryLock()) {
            mutableState.update { it.copy(error = "手机模型正在回复，请等待本次请求完成") }; return@withContext false
        }
        var admitted = false
        try {
            require(canonicalUuid(conversationId) && !database.isConversationDeleted(conversationId)) { "该会话已删除或无效，请新建会话" }
            val legacy = legacyRequestId?.let { database.operation(it) ?: throw IllegalArgumentException("原待发消息不存在") }
            val pending = if (legacy == null) database.pendingImage(conversationId) else null
            val requestSecrets = boundarySecrets(config)
            val requestConfig = config.withRedactionSecrets(requestSecrets)
            val body = text.trim().ifEmpty { if (pending != null) "请描述这张图片。" else "" }
                .let { redactKnownText(it, requestSecrets) }
            require(body.length in 1..8000) { "请输入 1～8000 字的消息，或先选择一张图片" }
            val history = database.messages(conversationId, limit = null).filter { legacy == null || it.id !in setOf(legacy.item.requestId, legacy.item.messageId) }
                .map { it.copy(content = redactKnownText(it.content, requestSecrets)) }
            val imageId = if (legacy != null) legacy.item.attachmentId else pending?.attachmentId
            val image = imageId?.let { loadDirectImage(it, required = true) }
            val sharedContext = database.device()?.endpointId?.let { contextCache.read(it, conversationId, database.conversationGeneration(conversationId)) }
            val messages = directMessages(requestConfig, history, body, image, sharedContext) { loadDirectImage(it, required = false) }
            val turn = database.beginDirectTurn(conversationId, body, imageId, expectedAuthority,
                allowUnpaired = config.source == ApiConfigSource.LOCAL, legacyRequestId = legacyRequestId,
                titleText = redactKnownText(text.trim(), requestSecrets))
            admitted = true
            directError = null
            val owner = OwnedDirectPreview(turn.id, database.device()?.endpointId, database.conversationGeneration(conversationId), turn.userId, config,
                Message(turn.assistantId, conversationId, "assistant", "", turn.userAt,
                    source = "mobile_direct", replyTo = turn.userId, localState = "generating"))
            directPreview = owner
            mutableState.update { it.copy(directChatBusy = true, directChatConversationId = conversationId, error = null) }
            refreshState()
            // Owned by the repository, not the foreground polling/picker job.
            val partial = AtomicReference("")
            val work = scope.launch(start = CoroutineStart.LAZY) {
                try {
                    if (!ownsDirectPreview(owner)) throw CancellationException("Phone request scope changed")
                    val reply = directGateway.stream(requestConfig, messages) { update ->
                        if (!ownsDirectPreview(owner)) { directPreview = null; directJob?.cancel(); return@stream }
                        val safe = redactStreamingText(update.text, boundarySecrets(config))
                        partial.set(safe)
                        directPreview = owner.copy(message = owner.message.copy(content = safe))
                        mutableState.update { current ->
                            if (current.directChatConversationId != conversationId || current.selectedConversationId != conversationId) current
                            else current.copy(messages = current.messages.filter { it.id != turn.assistantId } +
                                listOfNotNull(currentDirectPreview()?.takeIf { it.id == turn.assistantId }))
                        }
                    }
                    currentCoroutineContext().ensureActive()
                    if (!ownsDirectPreview(owner)) throw CancellationException("Phone request scope changed")
                    database.finishDirectTurn(turn.id, redactKnownText(reply, boundarySecrets(config)))
                    mutableState.update { it.copy(error = null) }
                } catch (cancelled: CancellationException) {
                    withContext(NonCancellable) { database.failDirectTurn(turn.id, "回复已停止，已保留收到的文字；可能已计费，不会自动重发",
                        partial = partial.get(), interrupted = true) }
                    throw cancelled
                } catch (failure: DirectApiFailure) {
                    database.failDirectTurn(turn.id, failure.safeMessage, partial = partial.get())
                    directError = failure.safeMessage
                    mutableState.update { it.copy(error = failure.safeMessage) }
                } catch (_: Exception) {
                    val message = "模型请求未完成，可能已计费；不会自动重试或转交电脑，请确认后重新发送"
                    database.failDirectTurn(turn.id, message, partial = partial.get())
                    directError = message
                    mutableState.update { it.copy(error = message) }
                } finally {
                    directPreview = null; directJob = null
                    mutableState.update { it.copy(directChatBusy = false, directChatConversationId = null) }
                    directMutex.unlock(); refreshState()
                }
                if (state.value.connectionState == ConnectionState.CONNECTED) poll()
            }
            directJob = work
            work.start()
            // Enrich future turns in the background; even a slow/offline PC
            // cannot hold up the phone's API request or first streamed token.
            scope.launch {
                if (!mutex.tryLock()) return@launch
                try { if (state.value.connectionState == ConnectionState.CONNECTED) refreshConversationContext(conversationId, body) }
                finally { mutex.unlock() }
            }
        } catch (invalid: IllegalArgumentException) {
            directMutex.unlock()
            mutableState.update { it.copy(error = invalid.message ?: "消息无法发送") }
        } catch (failure: Exception) {
            directMutex.unlock()
            if (failure is CancellationException) throw failure
            mutableState.update { it.copy(error = "无法保存手机聊天记录，请检查存储空间") }
        }
        admitted
    }

    /** Stops exactly the phone turn shown in this conversation; never replays it. */
    fun cancelDirectChat(conversationId: String) {
        if (state.value.directChatConversationId == conversationId) directJob?.cancel()
    }

    /** Called by the serialized sync path. Phone chat never waits for a PC RPC. */
    private suspend fun refreshConversationContext(conversationId: String, query: String = "") {
        val authority = database.device()?.endpointId ?: return
        if ("context.get" !in cachedCapabilities.orEmpty() || database.isConversationDeleted(conversationId) ||
            database.getSetting("authorization_blocked") == "1") { contextCache.clear(conversationId); return }
        val generation = database.conversationGeneration(conversationId)
        val epoch = database.contextEpoch(conversationId)
        lastContextRefresh = monotonicMillis()
        try {
            val params = JSONObject().put("conversation_id", conversationId).put("query", query.take(4000))
            generation?.let { params.put("generation", it) }
            val envelope = call("context.get", params)
            require(authority == database.device()?.endpointId &&
                generation == database.conversationGeneration(conversationId))
            contextCache.replace(authority, conversationId, generation, envelope,
                boundarySecrets(), expectedEpoch = epoch)
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { contextCache.clear(conversationId) }
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
        it.reasoningEffort, it.keySaved, it.source, it.reasoningProfile, it.reasoningBudgetTokens, it.reasoningMetadata) }

    suspend fun saveDirectApiConfig(baseUrl: String, model: String, apiKey: String, timeoutSeconds: Int,
        reasoningEffort: String, reasoningProfile: String? = null, reasoningBudgetTokens: Int = 0,
        reasoningMetadata: String? = null): Boolean = withContext(Dispatchers.IO) {
        localReady.await()
        try {
            val saved = synchronized(configLock) {
            val previous = directConfig
            val key = apiKey.trim().ifBlank { previous?.apiKey ?: throw IllegalArgumentException("请输入 API 密钥；首次配置不能留空") }
            val value = JSONObject().put("version", 1).put("revision", UUID.randomUUID().toString()).put("base_url", baseUrl.trim())
                .put("model", model.trim()).put("api_key", key).put("timeout_seconds", timeoutSeconds).put("reasoning_effort", reasoningEffort)
                .put("reasoning", ModelReasoning.selection(reasoningProfile ?: if (reasoningEffort == "default") "auto" else "legacy",
                    reasoningEffort, reasoningBudgetTokens, reasoningMetadata?.let {
                        runCatching { JSONObject(it) }.getOrElse { throw IllegalArgumentException("思考能力声明无效，请重新选择。") }
                    }))
                .put("system_prompt", redactKnownText(currentPersona()?.systemPrompt ?: previous?.systemPrompt ?: DEFAULT_PHONE_PROMPT,
                    boundarySecrets(previous) + key))
            val validated = try { DirectApiConfig.fromRpc(value, LOCAL_PHONE_AUTHORITY, allowCleartextApi) }
                catch (invalid: IllegalArgumentException) { throw IllegalArgumentException(invalid.message?.replace("电脑", "手机") ?: "手机模型配置无效") }
            validated.requireSafePublicSettings(boundarySecrets(previous) + key)
            val backup = (if (previous?.source == ApiConfigSource.DESKTOP) previous else previous?.desktopCache)?.takeIf {
                it.authorityId == database.device()?.endpointId && database.getSetting("authorization_blocked") != "1"
            }
            val saved = DirectApiConfig(validated.authorityId, validated.revision, validated.baseUrl, validated.model, validated.apiKey,
                validated.systemPrompt, validated.timeoutSeconds, validated.reasoningEffort, validated.syncedAt, ApiConfigSource.LOCAL,
                backup, reasoningProfile = validated.reasoningProfile, reasoningBudgetTokens = validated.reasoningBudgetTokens,
                reasoningMetadata = validated.reasoningMetadata)
            boundarySecrets(saved)
            apiVault.save(saved); directConfig = saved
            saved
            }
            database.setSetting("direct_api_blocked", null); directError = null
            mutableState.update { it.copy(directApi = saved.publicInfo(boundarySecrets(saved)), error = null) }; refreshState()
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
        contextCache.clear()
        synchronized(configLock) {
            clearSyncedPersona()
            val active = directConfig
            if (active?.source == ApiConfigSource.LOCAL) {
                if (active.desktopCache != null) { val clean = active.withDesktopCache(null); apiVault.save(clean); directConfig = clean }
                mutableState.update { it.copy(directApi = (directConfig ?: active).publicInfo(boundarySecrets()).copy(message = message)) }
            } else clearDirectConfig("电脑已撤销授权，已清除同步的 API 密钥；请配置手机模型或重新配对")
            }
    }

    private fun currentPersona(): SyncedPersona? = syncedPersona?.takeIf {
        it.authorityId == database.device()?.endpointId && database.getSetting("authorization_blocked") != "1"
    }

    private fun clearSyncedPersona() {
        clearCharacterCatalog()
        if (syncedPersona == null) return
        apiVault.clearPersona(); syncedPersona = null
        directConfig?.takeIf { it.source == ApiConfigSource.LOCAL }?.let { active ->
            val updated = active.withPersona(SyncedPersona(LOCAL_PHONE_AUTHORITY, "default", "鲸伴", DEFAULT_PHONE_PROMPT))
            apiVault.save(updated); directConfig = updated
        }
    }

    private fun syncPersona(result: JSONObject, authority: String) {
        // Older desktops expose only system_prompt in the complete API config.
        // Newer desktops also send persona when no usable desktop API exists.
        val envelope = result.optJSONObject("persona") ?: if (result.optBoolean("available")) JSONObject()
            .put("version", 1).put("revision", result.opt("revision")).put("name", "当前角色")
            .put("system_prompt", result.opt("system_prompt")) else return
        val secrets = boundarySecrets(extra = result.optString("api_key").takeIf { it.isNotBlank() })
        val incoming = SyncedPersona.fromRpc(envelope, authority, secrets)
        val previous = syncedPersona
        if (previous == null || previous.authorityId != incoming.authorityId || previous.revision != incoming.revision ||
            previous.systemPrompt != incoming.systemPrompt || previous.name != incoming.name) {
            apiVault.savePersona(incoming); syncedPersona = incoming
        }
        directConfig?.let { active ->
            if (active.systemPrompt != active.redact(incoming.systemPrompt)) {
                val updated = active.withPersona(incoming)
                apiVault.save(updated); directConfig = updated
            }
        }
        publishCharacterState()
    }

    private fun currentCharacterCatalog(): CharacterCatalogCache? = characterCatalog?.takeIf {
        it.authorityId == database.device()?.endpointId && database.getSetting("authorization_blocked") != "1"
    }

    private fun clearCharacterCatalog() {
        apiVault.clearCharacters()
        characterCatalog = null
        mutableState.update { it.copy(characterCards = emptyList(), activeCharacterId = "default",
            activeCharacterName = "鲸鱼娘", characterLibrarySupported = false, characterSyncBusy = false,
            characterSyncError = null) }
    }

    private fun publishCharacterState() {
        val catalog = currentCharacterCatalog()
        val persona = currentPersona()
        val secrets = boundarySecrets()
        val sameRole = catalogMatchesPersona(catalog, persona)
        val summaries = catalog?.items.orEmpty().map { card ->
            card.copy(label = redactKnownText(card.label, secrets), name = redactKnownText(card.name, secrets))
        }
        mutableState.update { it.copy(characterCards = summaries,
            activeCharacterId = if (sameRole) catalog!!.active.id else if (persona == null) "default" else null,
            activeCharacterName = persona?.name?.let { name -> redactKnownText(name, secrets) } ?: "鲸鱼娘",
            characterLibrarySupported = if (it.connectionState == ConnectionState.CONNECTED)
                CHARACTER_CAPABILITIES.all { capability -> capability in cachedCapabilities.orEmpty() }
                else catalog != null) }
    }

    private fun catalogMatchesPersona(catalog: CharacterCatalogCache?, persona: SyncedPersona?): Boolean =
        catalog != null && persona != null && catalog.active.persona.revision == persona.revision &&
            catalog.active.persona.systemPrompt == persona.systemPrompt && catalog.active.persona.name == persona.name

    /** Catalogs are read-only while offline; an unconfirmed switch never changes the local role. */
    suspend fun refreshCharacters() = perform {
        if (state.value.connectionState != ConnectionState.CONNECTED) {
            publishCharacterState()
            mutableState.update { it.copy(characterSyncError = "连接电脑后同步角色卡") }
            return@perform
        }
        if (cachedCapabilities == null) status()
        characterAction { refreshCharactersInternal(force = true) }
    }

    suspend fun selectCharacter(id: String) = perform {
        characterAction {
            require(state.value.connectionState == ConnectionState.CONNECTED) { "连接电脑后切换角色" }
            require(validCharacterId(id)) { "请选择有效的角色卡" }
            if (cachedCapabilities == null) status()
            require(CHARACTER_CAPABILITIES.all { it in cachedCapabilities.orEmpty() }) { "请更新并重启电脑端后同步角色卡" }
            // chat.config can update the persona even when the optional catalog refresh
            // fails. Never mistake the previously cached active card for the current role.
            if (!catalogMatchesPersona(currentCharacterCatalog(), currentPersona())) refreshCharactersInternal(force = true)
            val catalog = currentCharacterCatalog() ?: throw IllegalArgumentException("角色库尚未同步，请刷新后重试")
            val digest: String? = if (id == "default") null else (catalog.items.firstOrNull { it.id == id }?.digest
                ?: throw IllegalArgumentException("这张角色卡已不在角色库中，请刷新后重试"))
            if (catalog.active.id == id && (id == "default" || catalog.active.digest == digest)) return@characterAction
            val authority = catalog.authorityId
            val owner = synchronized(connectionLock) { connectionEpoch }
            val card = parseCharacterCard(call("character.get", JSONObject().put("id", id).apply {
                if (digest != null) put("digest", digest)
            }), authority, id, digest, boundarySecrets())
            ensureCharacterOwner(owner, authority)
            val result = call("character.select", JSONObject().put("id", id).put("digest", card.summary.digest)
                .put("expected_revision", catalog.revision))
            ensureCharacterOwner(owner, authority)
            val ack = if (result.optBoolean("duplicate")) {
                require(result.optString("state") == "complete") { "切换结果待确认，请刷新角色卡" }
                val envelope = result.optJSONObject("result")
                require(envelope?.optBoolean("ok") == true) { "切换结果待确认，请刷新角色卡" }
                envelope!!.getJSONObject("result")
            } else result
            require(ack.opt("version") == 1 && ack.opt("applied") == true && ack.optString("active_id") == id) {
                "切换结果待确认，请刷新角色卡"
            }
            characterDigest(ack, "revision", boundarySecrets())
            refreshCharactersInternal(force = true)
            if (currentCharacterCatalog()?.active?.id != id) {
                mutableState.update { it.copy(characterSyncError = "电脑角色已再次更新，已同步最新角色") }
            }
        }
    }

    private suspend fun characterAction(block: suspend () -> Unit) {
        mutableState.update { it.copy(characterSyncBusy = true, characterSyncError = null) }
        try { block() }
        catch (cancelled: CancellationException) { throw cancelled }
        catch (failure: RpcFailure) {
            if (failure.code == "unauthorized") throw failure
            if (failure.code == "character_conflict") {
                try { refreshCharactersInternal(force = true) }
                catch (cancelled: CancellationException) { throw cancelled }
                catch (unauthorized: RpcFailure) { if (unauthorized.code == "unauthorized") throw unauthorized }
                catch (_: Exception) { /* Preserve the previous valid role and shelf. */ }
                mutableState.update { it.copy(characterSyncError = "电脑角色或角色库已更新，请重新选择") }
            } else if (failure.code in setOf("character_missing", "character_unavailable", "invalid_request", "unknown_method")) {
                mutableState.update { it.copy(characterSyncError = if (failure.code == "character_missing")
                    "这张角色卡已不在电脑角色库中，请刷新后重试" else "角色卡暂时无法同步，请刷新后重试") }
            } else {
                mutableState.update { it.copy(characterSyncError = "切换结果待确认，重新连接后刷新角色卡") }
                throw failure
            }
        } catch (invalid: IllegalArgumentException) {
            // Locally generated validation strings only, never an arbitrary RPC/provider exception.
            mutableState.update { it.copy(characterSyncError = invalid.message?.takeIf { message ->
                message in CHARACTER_VALIDATION_MESSAGES
            } ?: "角色卡同步数据无效，请刷新后重试") }
        } catch (_: JSONException) {
            // A malformed optional role response must not disable a valid phone API.
            mutableState.update { it.copy(characterSyncError = "角色卡同步数据无效，请刷新后重试") }
        } catch (failure: Exception) {
            mutableState.update { it.copy(characterSyncError = "角色卡同步未完成，请重新连接后刷新") }
            throw failure
        } finally {
            publishCharacterState()
            mutableState.update { it.copy(characterSyncBusy = false) }
        }
    }

    private fun ensureCharacterOwner(owner: Any, authority: String) {
        if (synchronized(connectionLock) { owner !== connectionEpoch } || authority != database.device()?.endpointId ||
            database.getSetting("authorization_blocked") == "1") throw CancellationException("Stale character response")
    }

    private suspend fun refreshCharactersInternal(force: Boolean = false, advertised: JSONObject? = null) {
        if (!CHARACTER_CAPABILITIES.all { it in cachedCapabilities.orEmpty() }) {
            publishCharacterState()
            mutableState.update { it.copy(characterLibrarySupported = false,
                characterSyncError = "请更新并重启电脑端后同步角色卡") }
            return
        }
        val authority = database.device()?.endpointId ?: return
        val owner = synchronized(connectionLock) { connectionEpoch }
        val cached = currentCharacterCatalog()
        val revision = advertised?.let {
            require(it.opt("version") == 1) { "角色卡同步数据无效，请刷新后重试" }
            characterDigest(it, "revision", boundarySecrets())
        }
        if (!force && revision != null && cached?.revision == revision) {
            publishCharacterState()
            return
        }
        val entries = mutableListOf<CharacterCardSummary>()
        var offset = 0
        var first: CharacterPage? = null
        do {
            val params = JSONObject().put("offset", offset).put("limit", 50)
            first?.let { params.put("revision", it.revision) }
            val page = parseCharacterPage(call("character.list", params), authority, offset, boundarySecrets())
            currentCoroutineContext().ensureActive()
            ensureCharacterOwner(owner, authority)
            if (first == null) first = page
            else require(page.revision == first.revision && page.total == first.total &&
                page.active.id == first.active.id && page.active.digest == first.active.digest &&
                page.active.persona.revision == first.active.persona.revision &&
                page.active.persona.name == first.active.persona.name &&
                page.active.persona.systemPrompt == first.active.persona.systemPrompt) { "角色库已变化，请刷新后重试" }
            entries.addAll(page.items)
            require(entries.size <= MAX_CHARACTER_ENTRIES && entries.map { it.id }.distinct().size == entries.size) {
                "角色卡同步数据无效，请刷新后重试"
            }
            offset = page.nextOffset ?: break
        } while (true)
        val selected = requireNotNull(first)
        require(selected.active.id == null || selected.active.id == "default" || entries.any {
            it.id == selected.active.id && it.digest == selected.active.digest
        }) { "角色库已变化，请刷新后重试" }
        val incoming = CharacterCatalogCache(authority, selected.revision, entries.toList(), selected.active)
        synchronized(configLock) { synchronized(connectionLock) {
            ensureCharacterOwner(owner, authority)
            apiVault.saveCharacters(incoming)
            // The persona follows the shared role, while a phone's chosen API stays untouched.
            syncPersona(JSONObject().put("persona", JSONObject().put("version", 1)
                .put("name", selected.active.persona.name).put("revision", selected.active.persona.revision)
                .put("system_prompt", selected.active.persona.systemPrompt)), authority)
            characterCatalog = incoming
        } }
        publishCharacterState()
    }

    private fun keepConfigOrExplain(message: String, failure: Boolean = false) {
        synchronized(configLock) {
        val cached = directConfig
        mutableState.update { it.copy(directApi = if (cached != null) cached.publicInfo(boundarySecrets(cached)).let { summary -> summary.copy(
            sync = if (failure || !summary.available) ApiConfigSyncState.ERROR else ApiConfigSyncState.READY,
            message = if (summary.available) "$message；继续使用已保存的手机模型" else "已保存的模型参数需要重新配置。") }
            else ApiConnectionInfo(sync = if (failure) ApiConfigSyncState.ERROR else ApiConfigSyncState.WAITING, message = "$message；请先配置手机模型")) }
        }
    }

    private fun clearDirectConfig(message: String = "请配置手机模型，或连接电脑自动同步 API 配置") {
        synchronized(configLock) {
            boundarySecrets()
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
        val connectionOwner = synchronized(connectionLock) { connectionEpoch }
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
            currentCoroutineContext().ensureActive()
            if (synchronized(connectionLock) { connectionOwner !== connectionEpoch } ||
                device.endpointId != database.device()?.endpointId || database.getSetting("authorization_blocked") == "1") {
                throw CancellationException("Configuration response belongs to an old connection")
            }
            require(result.optInt("version") == 1) { "电脑 API 配置版本不兼容，请更新两端应用" }
            synchronized(configLock) { synchronized(connectionLock) {
                if (connectionOwner !== connectionEpoch || device.endpointId != database.device()?.endpointId ||
                    database.getSetting("authorization_blocked") == "1") throw CancellationException("Stale persona response")
                syncPersona(result, device.endpointId)
            } }
            if (CHARACTER_CAPABILITIES.all { it in cachedCapabilities.orEmpty() }) {
                characterAction { refreshCharactersInternal(advertised = result.optJSONObject("character_library")) }
            } else publishCharacterState()
            if (!result.optBoolean("available")) {
                keepConfigOrExplain("电脑暂未提供可用 API 配置")
                database.setSetting("direct_api_blocked", null)
                mutableState.update { it.copy(directApi = it.directApi.copy(sync = ApiConfigSyncState.READY, lastSyncedAt = System.currentTimeMillis())) }
                return checkedStatus
            }
            val config = DirectApiConfig.fromRpc(result, device.endpointId, allowCleartextApi)
            synchronized(configLock) { synchronized(connectionLock) {
                if (connectionOwner !== connectionEpoch || device.endpointId != database.device()?.endpointId ||
                    database.getSetting("authorization_blocked") == "1") throw CancellationException("Stale API response")
                val privacyKeys = boundarySecrets(config)
                config.requireSafePublicSettings(privacyKeys)
                val previous = directConfig
                val desktop = if (previous?.source == ApiConfigSource.LOCAL) previous.desktopCache else previous
                if (desktop == null || desktop.authorityId != config.authorityId || desktop.revision != config.revision) {
                    val active = (if (previous?.source == ApiConfigSource.LOCAL) previous.withDesktopCache(config) else config)
                        .let { candidate -> currentPersona()?.let(candidate::withPersona) ?: candidate }
                    active.requireSafePublicSettings(privacyKeys)
                    apiVault.save(active); directConfig = active
                }
            } }
            database.setSetting("direct_api_blocked", null)
            mutableState.update { it.copy(directApi = (directConfig ?: config).publicInfo(boundarySecrets(config))) }
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
                    if (item.method == "agent.send" && checkedStatus?.optBoolean("agent_busy") == true &&
                        !permitsQueuedAgent(checkedStatus)) {
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
                // Queue-capable hosts can return the durable ACK itself with
                // duplicate=true. That is distinct from operation.get's
                // {state,result} receipt and never authorizes a second send.
                if (result.optBoolean("duplicate") && !(item.method == "agent.send" &&
                    "agent.queue.v1" in cachedCapabilities.orEmpty() && result.opt("accepted") == true)) handleOperation(operation, result)
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

    /** Fresh host negotiation only; an offline phone outbox is not a PC queue. */
    private fun permitsQueuedAgent(status: JSONObject?): Boolean = status?.opt("agent_available") == true &&
        status.optJSONArray("capabilities")?.let { values ->
            (0 until values.length()).any { values.opt(it) == "agent.queue.v1" }
        } == true

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
                if (item.method == "agent.send" && "agent.queue.v1" in cachedCapabilities.orEmpty()) {
                    require(result.opt("accepted") == true && canonicalUuid(result.optString("job_id")) &&
                        result.optString("queue_state") in setOf("queued", "claimed", "running", "awaiting_user",
                            "cancel_requested", "completed", "failed", "cancelled", "timeout", "blocked", "interrupted", "unknown") &&
                        result.optString("execution_mode") == "agent" && result.optString("reply_delivery") == "sync.pull" &&
                        messageId == stableMessageId(rpc.endpointId, item.requestId)) { "电脑队列返回的任务确认无效；请检查原任务状态" }
                }
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
        private val CHARACTER_CAPABILITIES = setOf("character.list", "character.get", "character.select")
        private val CHARACTER_VALIDATION_MESSAGES = setOf("连接电脑后切换角色", "请选择有效的角色卡",
            "请更新并重启电脑端后同步角色卡", "角色库尚未同步，请刷新后重试", "这张角色卡已不在角色库中，请刷新后重试",
            "切换结果待确认，请刷新角色卡", "角色库已变化，请刷新后重试", "角色卡同步数据无效，请刷新后重试",
            "角色卡已变化，请刷新后重新选择")
        private const val REFRESH_INTERVAL_MS = 30_000L
        private const val DEFAULT_PHONE_PROMPT = "你是鲸伴，一位温柔自然的中文陪伴助手。请依据当前对话回答，不猜测不可见的图片或电脑状态。"
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
