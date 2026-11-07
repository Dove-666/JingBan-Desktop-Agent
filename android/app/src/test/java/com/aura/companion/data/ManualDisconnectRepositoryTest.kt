package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.aura.companion.transport.RpcTransportException
import com.aura.companion.ui.DraftSnapshot
import com.aura.companion.ui.PersistedDrafts
import com.aura.companion.ui.PrivateDraftStore
import java.io.File
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.ThreadContextElement
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.AbstractCoroutineContextElement
import kotlin.coroutines.ContinuationInterceptor
import kotlin.coroutines.CoroutineContext
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Repository entry points with finite in-memory RPC and model bodies only. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class ManualDisconnectRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    private class Vault(var config: DirectApiConfig? = null) : ApiConfigVault {
        override fun load() = config
        override fun save(config: DirectApiConfig) { this.config = config }
        override fun clear() { config = null }
    }

    private class NoModel : DirectChatGateway {
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("NO_MODEL")
    }

    /** Hold one owned IO dispatch, without replacing a global dispatcher. */
    private class IoHandoff : ThreadContextElement<Unit>, AbstractCoroutineContextElement(Key) {
        companion object Key : CoroutineContext.Key<IoHandoff>
        val entered = CompletableDeferred<Unit>()
        val release = CountDownLatch(1)
        val timedOut = AtomicBoolean()
        private val held = AtomicBoolean()

        override fun updateThreadContext(context: CoroutineContext) {
            if (context[ContinuationInterceptor] === Dispatchers.IO && held.compareAndSet(false, true)) {
                entered.complete(Unit)
                timedOut.set(!release.await(5, TimeUnit.SECONDS))
            }
        }

        override fun restoreThreadContext(context: CoroutineContext, oldState: Unit) = Unit
    }

    private class Rpc : RpcGateway {
        override val endpointId = "owned-phone"
        override val connectionType = "direct"
        @Volatile var blockedConnect: Int? = 1
        @Volatile var ignoreConnectCancellation = false
        @Volatile var holdFirstClose = false
        @Volatile var loseChatAck = false
        @Volatile var holdAgentAck = false
        val connects = AtomicInteger()
        val closes = AtomicInteger()
        val physicalConnection = AtomicInteger()
        val connectEntered = CompletableDeferred<Unit>()
        val connectRelease = CompletableDeferred<Unit>()
        val closeEntered = CompletableDeferred<Unit>()
        val closeRelease = CompletableDeferred<Unit>()
        val agentEntered = CompletableDeferred<Unit>()
        val agentRelease = CompletableDeferred<Unit>()
        val chatIds = CopyOnWriteArrayList<String>()
        val queriedIds = CopyOnWriteArrayList<String>()
        val methods = CopyOnWriteArrayList<String>()
        val jobId = UUID.randomUUID().toString()

        override suspend fun connect(ticket: String) {
            require(ticket == "owned-ticket")
            val number = connects.incrementAndGet()
            if (number == blockedConnect) {
                connectEntered.complete(Unit)
                if (ignoreConnectCancellation) withContext(NonCancellable) { connectRelease.await() }
                else connectRelease.await()
            }
            physicalConnection.set(number)
        }

        override suspend fun close() {
            val number = closes.incrementAndGet()
            if (number == 1 && holdFirstClose) {
                closeEntered.complete(Unit)
                closeRelease.await()
            }
            physicalConnection.set(0)
        }

        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            methods += method
            val result = when (method) {
                "status" -> JSONObject().put("protocol_version", 1).put("agent_available", true)
                    .put("capabilities", JSONArray(listOf("agent.send", "agent.queue.v1")))
                    .put("agent_busy", false).put("chat_busy", false)
                "pair" -> JSONObject()
                "sync.snapshot" -> JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("has_more", false).put("next_cursor", "")
                "sync.pull" -> JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since"))
                    .put("has_more", false)
                "image.list" -> JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0)
                "chat.send" -> {
                    chatIds += requestId
                    if (loseChatAck) throw RpcTransportException("timeout", "OWNED_LOST_ACK")
                    JSONObject().put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId))
                }
                "operation.get" -> {
                    queriedIds += params.getString("request_id")
                    JSONObject().put("state", "pending")
                }
                "agent.send" -> {
                    agentEntered.complete(Unit)
                    if (holdAgentAck) agentRelease.await()
                    JSONObject().put("accepted", true).put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId))
                        .put("job_id", jobId).put("queue_state", "queued")
                        .put("execution_mode", "agent").put("reply_delivery", "sync.pull")
                }
                else -> error("UNEXPECTED_RPC_$method")
            }
            return JSONObject().put("id", requestId).put("ok", true).put("result", result)
        }

        fun releaseAll() {
            connectRelease.complete(Unit)
            closeRelease.complete(Unit)
            agentRelease.complete(Unit)
        }
    }

    private fun database() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("Owned PC", "owned-pc", "owned-ticket"), "owned-phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }

    private fun repository(rpc: Rpc, database: CompanionDatabase = database(),
        vault: Vault = Vault(), model: DirectChatGateway = NoModel()) = AppRepository(context, rpc, database, vault, model)

    private suspend fun waitFor(condition: () -> Boolean) = withTimeout(5_000) {
        while (!condition()) delay(5)
    }

    private suspend fun cleanup(repository: AppRepository, rpc: Rpc, vararg jobs: Job?) {
        rpc.releaseAll()
        withTimeout(5_000) {
            jobs.forEach { it?.cancelAndJoin() }
            repository.close()
        }
    }

    @Test fun disconnectInterruptsPendingReconnectAndKeepsItsForegroundCallerAlive() = runBlocking {
        val rpc = Rpc()
        val database = database()
        val repository = repository(rpc, database)
        val returned = CompletableDeferred<Unit>()
        val callerRelease = CompletableDeferred<Unit>()
        val cid = repository.newConversation()
        repository.sendLegacyMessage(cid, "Owned queued input")
        val original = database.outbox().single()
        val reconnect = launch {
            repository.reconnect(silent = true)
            returned.complete(Unit)
            callerRelease.await()
        }
        try {
            withTimeout(5_000) { rpc.connectEntered.await() }
            withTimeout(1_000) { repository.disconnect() }
            withTimeout(1_000) { returned.await() }
            assertTrue(reconnect.isActive)
            assertFalse(reconnect.isCancelled)
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(0, rpc.physicalConnection.get())
            assertTrue(rpc.closes.get() > 0)
            assertEquals(original, database.outbox().single())
            assertEquals("Owned queued input", database.messages(cid).single().content)
            assertTrue(rpc.chatIds.isEmpty())
        } finally {
            callerRelease.complete(Unit)
            cleanup(repository, rpc, reconnect)
        }
    }

    @Test fun establishedConnectionAndRepeatedDisconnectRemainUsable() = runBlocking {
        val rpc = Rpc().apply { blockedConnect = null }
        val repository = repository(rpc)
        try {
            repository.reconnect()
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            withTimeout(1_000) { repository.disconnect(); repository.disconnect() }
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(0, rpc.physicalConnection.get())
            assertEquals(2, rpc.closes.get())
            repository.reconnect()
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertEquals(2, rpc.connects.get())
        } finally { cleanup(repository, rpc) }
    }

    @Test fun reconnectQueuedBeforeDisconnectNeverStartsAnotherConnection() = runBlocking {
        val rpc = Rpc().apply { holdFirstClose = true }
        val repository = repository(rpc)
        val first = launch { repository.reconnect(silent = true) }
        var queued: Job? = null
        var disconnect: Job? = null
        try {
            withTimeout(5_000) { rpc.connectEntered.await() }
            queued = launch(start = CoroutineStart.UNDISPATCHED) { repository.reconnect() }
            disconnect = launch(start = CoroutineStart.UNDISPATCHED) { repository.disconnect() }
            withTimeout(5_000) { rpc.closeEntered.await() }
            assertEquals(1, rpc.connects.get())
            rpc.closeRelease.complete(Unit)
            withTimeout(5_000) { first.join(); queued.join(); disconnect.join() }
            assertEquals(1, rpc.connects.get())
            assertTrue(rpc.methods.isEmpty())
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(0, rpc.physicalConnection.get())
        } finally { cleanup(repository, rpc, first, queued, disconnect) }
    }

    @Test fun establishedNewReconnectSurvivesOlderQueuedConnectAndDelayedDisconnect() = runBlocking {
        val rpc = Rpc()
        val repository = repository(rpc)
        val first = launch { repository.reconnect(silent = true) }
        val queuedHandoff = IoHandoff()
        val disconnectHandoff = IoHandoff()
        var queued: Job? = null
        var disconnect: Job? = null
        var newer: Job? = null
        try {
            withTimeout(5_000) { rpc.connectEntered.await() }
            // B has captured the old epoch, but has not acquired the PC mutex.
            queued = launch(queuedHandoff, start = CoroutineStart.UNDISPATCHED) { repository.reconnect() }
            withTimeout(5_000) { queuedHandoff.entered.await() }
            // D has replaced the epoch and cancelled A before its IO handoff.
            disconnect = launch(disconnectHandoff, start = CoroutineStart.UNDISPATCHED) { repository.disconnect() }
            withTimeout(5_000) { disconnectHandoff.entered.await(); first.join() }
            newer = launch(start = CoroutineStart.UNDISPATCHED) { repository.reconnect() }
            withTimeout(5_000) { newer.join() }
            assertEquals(2, rpc.connects.get())
            assertEquals(1, rpc.methods.count { it == "status" })
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertEquals(2, rpc.physicalConnection.get())
            val closeCount = rpc.closes.get()
            queuedHandoff.release.countDown()
            disconnectHandoff.release.countDown()
            withTimeout(5_000) { queued.join(); disconnect.join() }
            assertFalse(queuedHandoff.timedOut.get())
            assertFalse(disconnectHandoff.timedOut.get())
            assertEquals(closeCount, rpc.closes.get())
            assertEquals(2, rpc.connects.get())
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertEquals(2, rpc.physicalConnection.get())
        } finally {
            queuedHandoff.release.countDown()
            disconnectHandoff.release.countDown()
            cleanup(repository, rpc, first, queued, disconnect, newer)
        }
    }

    @Test fun externalCancellationStillCancelsTheCallerAndClosesItsConnection() = runBlocking {
        val rpc = Rpc()
        val repository = repository(rpc)
        val reconnect = launch { repository.reconnect() }
        try {
            withTimeout(5_000) { rpc.connectEntered.await() }
            withTimeout(5_000) { reconnect.cancelAndJoin() }
            assertTrue(reconnect.isCancelled)
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(0, rpc.physicalConnection.get())
            assertTrue(rpc.closes.get() > 0)
        } finally { cleanup(repository, rpc, reconnect) }
    }

    @Test fun lateUncancellableConnectCannotPublishConnectedAfterDisconnect() = runBlocking {
        val rpc = Rpc().apply { ignoreConnectCancellation = true }
        val repository = repository(rpc)
        val reconnect = launch { repository.reconnect() }
        var disconnect: Job? = null
        try {
            withTimeout(5_000) { rpc.connectEntered.await() }
            disconnect = launch(start = CoroutineStart.UNDISPATCHED) { repository.disconnect() }
            delay(50)
            assertFalse(disconnect.isCompleted)
            rpc.connectRelease.complete(Unit)
            withTimeout(5_000) { reconnect.join(); disconnect.join() }
            assertTrue(rpc.methods.isEmpty())
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(0, rpc.physicalConnection.get())
        } finally { cleanup(repository, rpc, reconnect, disconnect) }
    }

    @Test fun pendingPairCanBeDisconnectedBeforeSendingItsPairingRequest() = runBlocking {
        val rpc = Rpc()
        val database = database()
        val repository = repository(rpc, database)
        val input = JSONObject().put("ticket", "owned-ticket").put("endpoint_id", "owned-new-pc")
            .put("code", "OWNED_TEMPORARY_PAIRING_CODE").put("version", 1).toString()
        val pairing = launch { repository.pair(input, "Owned fixture phone") }
        try {
            withTimeout(5_000) { rpc.connectEntered.await() }
            withTimeout(1_000) { repository.disconnect(); pairing.join() }
            assertEquals("owned-pc", database.device()!!.endpointId)
            assertFalse("pair" in rpc.methods)
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(0, rpc.physicalConnection.get())
        } finally { cleanup(repository, rpc, pairing) }
    }

    @Test fun disconnectKeepsPhoneStreamingAndItsPersistedDraft() = runBlocking {
        val rpc = Rpc()
        val config = DirectApiConfig("local-phone", "owned", "https://example.test/v1", "owned-model",
            "OWNED_SYNTHETIC_KEY", "Owned prompt", 30, "default", 0, ApiConfigSource.LOCAL)
        val started = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<Unit>()
        val calls = AtomicInteger()
        val model = object : DirectChatGateway {
            override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("STREAM_ONLY")
            override suspend fun stream(config: DirectApiConfig, messages: JSONArray,
                onUpdate: (DirectChatUpdate) -> Unit): String {
                calls.incrementAndGet()
                onUpdate(DirectChatUpdate("Owned partial"))
                started.complete(Unit)
                finish.await()
                return "Owned final"
            }
        }
        val database = database()
        val repository = repository(rpc, database, Vault(config), model)
        val cid = repository.newConversation()
        val file = File(context.filesDir, "qa-manual-disconnect-${UUID.randomUUID()}.json")
        val drafts = PrivateDraftStore(file)
        val original = PersistedDrafts(1, mapOf(cid to DraftSnapshot("Owned unsent draft", 1)))
        drafts.update { original }
        val originalBytes = file.readBytes()
        var reconnect: Job? = null
        try {
            assertTrue(repository.sendMessage(cid, "Owned phone request"))
            withTimeout(5_000) { started.await() }
            reconnect = launch { repository.reconnect(silent = true) }
            withTimeout(5_000) { rpc.connectEntered.await() }
            withTimeout(1_000) { repository.disconnect(); reconnect.join() }
            assertTrue(repository.state.value.directChatBusy)
            assertEquals(1, calls.get())
            assertEquals(original, drafts.load())
            assertArrayEquals(originalBytes, file.readBytes())
            finish.complete(Unit)
            waitFor { !repository.state.value.directChatBusy }
            assertEquals("Owned final", database.messages(cid).last().content)
            assertEquals(1, calls.get())
        } finally {
            finish.complete(Unit)
            cleanup(repository, rpc, reconnect)
            file.delete()
        }
    }

    @Test fun previouslyAttemptedOutboxKeepsItsIdAndIsQueriedAfterDisconnect() = runBlocking {
        val rpc = Rpc().apply { blockedConnect = null; loseChatAck = true }
        val database = database()
        val repository = repository(rpc, database)
        var reconnect: Job? = null
        try {
            repository.reconnect()
            val cid = repository.newConversation()
            repository.sendLegacyMessage(cid, "Owned possibly accepted request")
            val original = database.outbox().single()
            assertEquals("unknown", original.status)
            assertEquals(listOf(original.requestId), rpc.chatIds)
            rpc.blockedConnect = 2
            reconnect = launch { repository.reconnect(silent = true) }
            withTimeout(5_000) { rpc.connectEntered.await() }
            withTimeout(1_000) { repository.disconnect(); reconnect.join() }
            assertEquals(original, database.outbox().single())
            rpc.blockedConnect = null
            repository.reconnect()
            assertEquals(listOf(original.requestId), rpc.chatIds)
            assertEquals(listOf(original.requestId), rpc.queriedIds)
            assertEquals(original.requestId, database.outbox().single().requestId)
            assertEquals("pending", database.outbox().single().status)
        } finally { cleanup(repository, rpc, reconnect) }
    }

    @Test fun disconnectDoesNotCancelAGenericAgentSendOrChangeItsAcceptance() = runBlocking {
        val rpc = Rpc().apply { blockedConnect = null; holdAgentAck = true }
        val database = database()
        val repository = repository(rpc, database)
        var send: Job? = null
        var disconnect: Job? = null
        val accepted = CompletableDeferred<Boolean>()
        try {
            repository.reconnect()
            val cid = repository.newConversation()
            send = launch { accepted.complete(repository.sendDesktopMessage(cid, "Owned agent task")) }
            withTimeout(5_000) { rpc.agentEntered.await() }
            val requestId = database.outbox().single().requestId
            disconnect = launch { repository.disconnect() }
            assertNull(withTimeoutOrNull(100) { accepted.await() })
            assertFalse(send.isCancelled)
            assertFalse(disconnect.isCompleted)
            rpc.agentRelease.complete(Unit)
            assertTrue(withTimeout(5_000) { accepted.await() })
            withTimeout(5_000) { send.join(); disconnect.join() }
            assertEquals(requestId, database.outbox().single().requestId)
            assertEquals("sent", database.outbox().single().status)
            assertEquals(1, rpc.methods.count { it == "agent.send" })
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
        } finally { cleanup(repository, rpc, send, disconnect) }
    }
}
