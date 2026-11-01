package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.collect
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.IOException
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList

/** Counts wire operations and encrypted writes, rather than timing a fast fake network. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class RepositoryPollingTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        var value: DirectApiConfig? = null
        var writes = 0
        override fun load() = value
        override fun save(config: DirectApiConfig) { value = config; writes++ }
        override fun clear() { value = null }
    }
    private class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var connects = 0
        var supported = true
        var authorized = true
        var failPull = false
        var revision = "a".repeat(64)
        var loseChatReply = false
        var chatCalls = 0
        var originalChatId: String? = null
        val requests = mutableListOf<String>()
        val changes = mutableListOf<JSONObject>()
        override suspend fun connect(ticket: String) { connects++ }
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            requests += method
            fun ok(result: JSONObject) = JSONObject().put("ok", true).put("result", result)
            if (!authorized) return JSONObject().put("ok", false).put("error", JSONObject().put("code", "unauthorized").put("message", "Authorization removed"))
            return when (method) {
                "status" -> ok(JSONObject().put("protocol_version", 1).put("chat_busy", false).put("capabilities",
                    JSONArray(if (supported) listOf("chat.config", "chat.import", "chat.image") else listOf("chat.image"))))
                "chat.config" -> ok(JSONObject().put("version", 1).put("available", true).put("revision", revision)
                    .put("base_url", "https://example.test/v1").put("api_key", "TEST_ONLY_CREDENTIAL")
                    .put("model", "model-$revision").put("system_prompt", "Desktop prompt").put("timeout_seconds", 30).put("reasoning_effort", "default"))
                "sync.pull" -> {
                    if (failPull) throw IOException("Deliberate network failure")
                    val since = params.getLong("since")
                    val rows = changes.filter { it.getLong("seq") > since }
                    ok(JSONObject().put("changes", JSONArray(rows)).put("next_since", rows.lastOrNull()?.getLong("seq") ?: since).put("has_more", false))
                }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0).put("next_cursor", "").put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("next_offset", 0).put("has_more", false))
                "chat.send" -> {
                    chatCalls++; originalChatId = requestId
                    if (loseChatReply) { loseChatReply = false; throw IOException("deliberately lost accepted response") }
                    ok(JSONObject().put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId)))
                }
                "operation.get" -> {
                    assertEquals(originalChatId, params.getString("request_id"))
                    ok(JSONObject().put("state", "pending"))
                }
                else -> error("Unexpected RPC")
            }
        }
    }
    private fun db() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0).put("next_cursor", "").put("has_more", false), true)
    }

    @Test fun reconnectUsesOneStatusAndIdlePollOnlyPullsWithoutWritingEncryptedConfig() = runBlocking {
        var now = 0L; val rpc = Rpc(); val vault = Vault(); val database = db()
        val repository = AppRepository(context, rpc, database, vault, monotonicMillis = { now })
        try {
            repository.reconnect()
            assertEquals(listOf("status", "chat.config", "sync.pull", "image.list"), rpc.requests)
            assertEquals(1, vault.writes)
            val firstStamp = repository.state.value.lastSyncedAt
            rpc.requests.clear()
            repeat(10) { now += 2000; repository.poll() }
            assertEquals(List(10) { "sync.pull" }, rpc.requests)
            assertEquals(1, rpc.connects); assertEquals(1, vault.writes)
            assertEquals(firstStamp, repository.state.value.lastSyncedAt)
            assertEquals(SyncState.READY, repository.state.value.syncState)
            assertEquals(ApiConfigSyncState.READY, repository.state.value.directApi.sync)
        } finally { repository.close() }
    }

    @Test fun periodicConfigAndImagesRefreshAfterThirtySecondsButSameRevisionNeverReencrypts() = runBlocking {
        var now = 0L; val rpc = Rpc(); val vault = Vault(); val repository = AppRepository(context, rpc, db(), vault, monotonicMillis = { now })
        try {
            repository.reconnect(); rpc.requests.clear()
            now = 29999; repository.poll()
            assertEquals(listOf("sync.pull"), rpc.requests)
            rpc.requests.clear(); now = 30000; repository.poll()
            assertEquals(listOf("status", "chat.config", "sync.pull", "image.list"), rpc.requests)
            assertEquals(1, vault.writes)
            rpc.requests.clear(); now = 60000; rpc.revision = "b".repeat(64); repository.poll()
            assertEquals(listOf("status", "chat.config", "sync.pull", "image.list"), rpc.requests)
            assertEquals(2, vault.writes); assertEquals(rpc.revision, vault.value!!.revision)
            assertEquals("model-${rpc.revision}", repository.state.value.directApi.model)
        } finally { repository.close() }
    }

    @Test fun manualSyncAndExplicitConfigRefreshAreForcedWithinPollingCooldown() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val repository = AppRepository(context, rpc, db(), vault, monotonicMillis = { 0 })
        try {
            repository.reconnect(); rpc.requests.clear()
            repository.syncNow()
            assertEquals(listOf("status", "chat.config", "sync.pull"), rpc.requests)
            rpc.requests.clear(); repository.refreshApiConfig()
            assertEquals(listOf("status", "chat.config"), rpc.requests)
            assertEquals(1, vault.writes)
        } finally { repository.close() }
    }

    @Test fun lightweightPullStillObservesRevocationImmediatelyAndClearsKey() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val repository = AppRepository(context, rpc, db(), vault, monotonicMillis = { 0 })
        try {
            repository.reconnect(); rpc.requests.clear(); rpc.authorized = false
            repository.poll()
            assertEquals(listOf("sync.pull"), rpc.requests)
            assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            assertFalse(repository.state.value.directApi.available); assertNull(vault.value)
            repository.reconnect()
            assertEquals(listOf("sync.pull"), rpc.requests)
        } finally { repository.close() }
    }

    @Test fun nonemptyPullRefreshesCachedThreadWithoutRecheckingStatusOrConfig() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val repository = AppRepository(context, rpc, db(), vault, monotonicMillis = { 0 })
        try {
            repository.reconnect(); rpc.requests.clear()
            val cid = UUID.randomUUID().toString(); val mid = UUID.randomUUID().toString()
            rpc.changes += JSONObject().put("seq", 1).put("entity", "conversation").put("id", cid).put("conversation_id", cid)
                .put("data", JSONObject().put("title", "New desktop conversation").put("created_at", "2026-10-31T00:00:00Z")
                    .put("updated_at", "2026-10-31T00:00:00Z").put("message_count", 1))
            rpc.changes += JSONObject().put("seq", 2).put("entity", "message").put("id", mid).put("conversation_id", cid)
                .put("data", JSONObject().put("role", "assistant").put("content", "New desktop message").put("created_at", "2026-10-31T00:00:00Z"))
            repository.poll()
            assertEquals(listOf("sync.pull"), rpc.requests)
            assertEquals(cid, repository.state.value.selectedConversationId)
            assertEquals("New desktop message", repository.state.value.messages.single().content)
            assertEquals(1, vault.writes)
        } finally { repository.close() }
    }

    @Test fun legacyComputerUsesSingleInitialStatusAndPendingUnknownRequestNeverRegenerates() = runBlocking {
        val rpc = Rpc().apply { supported = false; loseChatReply = true }; val database = db()
        val repository = AppRepository(context, rpc, database, Vault(), monotonicMillis = { 0 })
        try {
            val cid = repository.newConversation(); repository.sendLegacyMessage(cid, "legacy unknown response")
            val originalId = database.outbox().single().requestId
            repository.reconnect()
            assertEquals(1, rpc.requests.count { it == "status" })
            assertEquals(0, rpc.requests.count { it == "chat.config" })
            assertEquals("unknown", database.operation(originalId)!!.item.status)
            repository.reconnect()
            assertEquals("pending", database.operation(originalId)!!.item.status)
            rpc.requests.clear(); repository.poll()
            assertEquals(listOf("sync.pull"), rpc.requests)
            assertEquals(1, rpc.chatCalls); assertEquals(originalId, rpc.originalChatId)
            assertFalse(repository.state.value.directApi.available)
        } finally { repository.close() }
    }

    @Test fun automaticFailureIsSilentButManualFailureAndRevocationStillExplainTheProblem() = runBlocking {
        val rpc = Rpc(); val vault = Vault()
        val direct = object : DirectChatGateway {
            override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = throw DirectApiFailure("手机模型请求失败，可能已计费；不会自动重试")
        }
        val repository = AppRepository(context, rpc, db(), vault, direct, monotonicMillis = { 0 })
        try {
            repository.reconnect(); rpc.failPull = true
            repository.poll()
            assertEquals(ConnectionState.ERROR, repository.state.value.connectionState)
            assertEquals(SyncState.ERROR, repository.state.value.syncState)
            assertNull(repository.state.value.error)
            assertTrue(repository.state.value.directApi.available)
            repository.reconnect(silent = true)
            assertNull(repository.state.value.error)
            repository.reconnect()
            assertNotNull(repository.state.value.error)
            rpc.failPull = false; repository.reconnect()
            rpc.failPull = true
            repository.sendMessage(repository.newConversation(), "API error must survive silent sync")
            withTimeout(5000) { while (repository.state.value.directChatBusy || repository.state.value.error?.contains("模型请求失败") != true) delay(10) }
            repository.reconnect(silent = true)
            assertTrue(repository.state.value.error!!.contains("模型请求失败"))
            rpc.failPull = false
            val apiError = repository.state.value.error
            val observedErrors = CopyOnWriteArrayList<String?>()
            // Unconfined observation runs with each emitter, catching a
            // CONNECTED/null emission even if the final state restores it.
            val observer = launch(Dispatchers.Unconfined, start = CoroutineStart.UNDISPATCHED) {
                repository.state.collect { observedErrors += it.error }
            }
            try {
                repository.reconnect(silent = true)
                assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
                assertTrue(observedErrors.isNotEmpty())
                assertTrue("Silent success must never transiently clear the API error", observedErrors.all { it == apiError })
            } finally { observer.cancelAndJoin() }
            repository.clearError()
            rpc.failPull = false; rpc.authorized = false
            repository.reconnect(silent = true)
            assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            assertTrue(repository.state.value.error!!.contains("撤销"))
            assertNull(vault.value)
        } finally { repository.close() }
    }
}
