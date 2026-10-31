package com.aura.companion.data

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.IOException
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AppRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    private class FakeRpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var chatCalls = 0
        var pairCalls = 0
        var connectCalls = 0
        var closeCalls = 0
        var statusUnauthorized = false
        var rejectPairing = false
        var failSync = false
        var loseChatResponse = false
        var desktopBusy = false
        var resets = 0
        var deletedConversation: String? = null
        val operations = mutableMapOf<String, JSONObject>()
        val sentChatIds = mutableListOf<String>()
        override suspend fun connect(ticket: String) { connectCalls++; require(ticket == "public-ticket") }
        override suspend fun close() { closeCalls++ }
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("id", requestId).put("ok", true).put("result", result)
            fun failure(code: String) = JSONObject().put("id", requestId).put("ok", false)
                .put("error", JSONObject().put("code", code).put("message", code))
            if (statusUnauthorized && method != "pair") return failure("unauthorized")
            return when (method) {
                "pair" -> { pairCalls++; if (rejectPairing) failure("pairing_failed") else ok(JSONObject().put("paired", true)) }
                "status" -> ok(JSONObject().put("protocol_version", 1).put("chat_busy", desktopBusy))
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("next_cursor", "").put("has_more", false))
                "sync.pull" -> {
                    if (failSync) return failure("sync_error")
                    if (resets-- > 0) return failure("sync_reset_required")
                    val deleted = deletedConversation
                    val items = JSONArray()
                    if (deleted != null && params.getLong("since") == 0L) items.put(JSONObject().put("entity", "conversation")
                        .put("id", deleted).put("conversation_id", deleted).put("seq", 1).put("deleted", true).put("data", JSONObject()))
                    ok(JSONObject().put("changes", items).put("next_since", if (items.length() > 0) 1 else params.getLong("since"))
                        .put("has_more", false))
                }
                "operation.get" -> ok(operations[params.getString("request_id")] ?: JSONObject().put("state", "not_found"))
                "chat.send" -> {
                    chatCalls++; sentChatIds.add(requestId)
                    operations[requestId] = JSONObject().put("state", "pending")
                    if (loseChatResponse) { loseChatResponse = false; throw IOException("response deliberately lost") }
                    ok(JSONObject().put("accepted", true).put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId)))
                }
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                else -> error("Unexpected method $method")
            }
        }
    }

    private fun seededDb(): CompanionDatabase = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("电脑", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }

    @Test fun offlineChatSurvivesReconnectAndIsSentWithOneStableRequestId() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.sendMessage(cid, "离线内容")
            val queuedId = db.outbox().single().requestId
            assertEquals(0, rpc.chatCalls)
            assertEquals("queued", db.outbox().single().status)
            repo.reconnect()
            assertEquals(listOf(queuedId), rpc.sentChatIds)
            assertEquals("sent", db.outbox().single().status)
            assertEquals(AppRepository.stableMessageId("phone", queuedId), db.outbox().single().messageId)
            repo.syncNow(); repo.reconnect()
            assertEquals(1, rpc.chatCalls)
        } finally { repo.close() }
    }

    @Test fun lostResponseQueriesPendingOperationAndNeverAutomaticallyRepeatsAi() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { loseChatResponse = true }
        val repo = AppRepository(context, rpc, db)
        try {
            repo.sendMessage(repo.newConversation(), "只生成一次")
            repo.reconnect()
            assertEquals("unknown", db.outbox().single().status)
            assertEquals(ConnectionState.ERROR, repo.state.value.connectionState)
            repo.reconnect()
            assertEquals(1, rpc.chatCalls)
            assertEquals("pending", db.outbox().single().status)
            repo.syncNow(); repo.retryOutbox(db.outbox().single().requestId)
            assertEquals(1, rpc.chatCalls)
            assertEquals(1, db.outbox().size)
        } finally { repo.close() }
    }

    @Test fun deletedConversationQueueIsRetainedForUserInsteadOfRevived() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.sendMessage(cid, "电脑删除后不能自动复活")
            rpc.deletedConversation = cid
            repo.reconnect()
            assertEquals(0, rpc.chatCalls)
            assertEquals("needs_attention", db.outbox().single().status)
            assertEquals("电脑删除后不能自动复活", db.outbox().single().text)
        } finally { repo.close() }
    }

    @Test fun authorizationRevocationBlocksAutomaticReconnectAndPairing() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { statusUnauthorized = true }
        val repo = AppRepository(context, rpc, db)
        try {
            repo.sendMessage(repo.newConversation(), "授权撤销时保留")
            repo.reconnect(); repo.reconnect(); repo.syncNow()
            assertEquals(ConnectionState.UNAUTHORIZED, repo.state.value.connectionState)
            assertEquals(0, rpc.pairCalls)
            assertEquals(0, rpc.chatCalls)
            assertEquals("queued", db.outbox().single().status)
        } finally { repo.close() }
    }

    @Test fun busyDesktopKeepsQueuedChatWithoutCreatingFailedDuplicateOperations() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { desktopBusy = true }
        val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.sendMessage(cid, "first"); repo.sendMessage(cid, "second")
            repo.reconnect(); repo.syncNow()
            assertEquals(0, rpc.chatCalls)
            assertTrue(db.outbox().all { it.status == "queued" })
            rpc.desktopBusy = false
            repo.syncNow()
            assertEquals(1, rpc.chatCalls)
            assertEquals(1, db.outbox().count { it.status == "queued" })
        } finally { repo.close() }
    }

    @Test fun syncResetObtainsFreshSnapshotAndPreservesUnsentDraft() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { resets = 1; desktopBusy = true }
        val repo = AppRepository(context, rpc, db)
        try {
            repo.sendMessage(repo.newConversation(), "重建缓存后仍保留")
            repo.reconnect()
            assertEquals(SyncState.READY, repo.state.value.syncState)
            assertEquals("queued", db.outbox().single().status)
            assertEquals("重建缓存后仍保留", repo.state.value.messages.single().content)
        } finally { repo.close() }
    }

    @Test fun nestedPairingUriWorksWithoutPersistingTemporaryCode() = runBlocking {
        val db = CompanionDatabase(context, null); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val temporary = "TEMPORARY_PAIR_SECRET"
            val payload = JSONObject().put("ticket", "public-ticket").put("endpoint_id", "desktop")
                .put("version", 1).put("code", temporary)
            val uri = "aura://pair/" + Base64.encodeToString(payload.toString().toByteArray(), Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
            repo.pair(JSONObject().put("pairing", JSONObject().put("uri", uri)).toString(), "测试手机")
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals("public-ticket", db.device()!!.ticket)
            val persisted = db.readableDatabase.rawQuery("SELECT value FROM settings", null).use {
                buildString { while (it.moveToNext()) append(it.getString(0)) }
            }
            assertFalse(persisted.contains(temporary)); assertFalse(persisted.contains("aura://pair/"))
        } finally { repo.close() }
    }

    @Test fun stableUuidMatchesDesktopPythonUuid5() {
        assertEquals("a73947cc-c9dc-5d9a-9a1f-2e31586fd55b",
            AppRepository.stableMessageId("phone", "91e374fc-5c45-44e0-b4c3-bf03b7c05470"))
    }

    @Test fun failedPairLeavesConnectingStateButKeepsPreviouslySavedComputer() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { rejectPairing = true }; val repo = AppRepository(context, rpc, db)
        try {
            repo.pair(JSONObject().put("ticket", "public-ticket").put("code", "expired-code")
                .put("endpoint_id", "another-desktop").toString(), "测试手机")
            assertEquals(ConnectionState.ERROR, repo.state.value.connectionState)
            assertEquals("desktop", db.device()!!.endpointId)
            assertEquals("pairing_failed", repo.state.value.error)
        } finally { repo.close() }
    }

    @Test fun businessSyncErrorDoesNotDisconnectValidEncryptedConnection() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { failSync = true }; val repo = AppRepository(context, rpc, db)
        try {
            repo.reconnect()
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.ERROR, repo.state.value.syncState)
        } finally { repo.close() }
    }

    @Test fun invalidPairingNeverConnectsOrClosesAnExistingSessionAndPreservesCacheAndQueue() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { desktopBusy = true }; val repo = AppRepository(context, rpc, db)
        try {
            repo.reconnect()
            val cid = repo.newConversation()
            repo.sendMessage(cid, "原连接的待发内容")
            val device = repo.state.value.device
            val queued = db.outbox().single()
            val cursor = db.cursor()
            val connections = rpc.connectCalls
            val closes = rpc.closeCalls
            val invalid = listOf("Q".repeat(24), "{broken-json", "aura://pair/%%%",
                JSONObject().put("code", "temporary-code").toString(),
                JSONObject().put("ticket", "public-ticket").put("code", "temporary-code").put("version", 2).toString(),
                JSONObject().put("ticket", "public-ticket").put("code", "temporary-code").put("expires_at", 1).toString(),
                "Q".repeat(32769))
            for (input in invalid) {
                repo.pair(input, "测试手机")
                assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
                assertEquals(SyncState.READY, repo.state.value.syncState)
                assertEquals(device, repo.state.value.device)
                assertEquals(cid, repo.state.value.selectedConversationId)
                assertEquals(cursor, db.cursor())
                assertEquals(queued, db.outbox().single())
                assertEquals(connections, rpc.connectCalls)
                assertEquals(closes, rpc.closeCalls)
                assertEquals(0, rpc.pairCalls)
                assertNotNull(repo.state.value.error)
                assertFalse(repo.state.value.error.orEmpty().contains("temporary-code"))
            }
        } finally { repo.close() }
    }

    @Test fun malformedPairingKeepsDisconnectedStateAndExplainsFullCopy() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            repo.sendMessage(repo.newConversation(), "离线草稿")
            repo.pair("Q".repeat(24), "测试手机")
            assertEquals(ConnectionState.DISCONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.IDLE, repo.state.value.syncState)
            assertTrue(repo.state.value.error.orEmpty().contains("复制配对信息"))
            assertEquals(0, rpc.connectCalls); assertEquals(0, rpc.closeCalls)
            repo.pair("{\"ticket\":\"NEVER_ECHO_THIS\"", "测试手机")
            assertEquals(ConnectionState.DISCONNECTED, repo.state.value.connectionState)
            assertFalse(repo.state.value.error.orEmpty().contains("NEVER_ECHO_THIS"))
            assertEquals("queued", db.outbox().single().status)
            assertEquals(0, rpc.connectCalls); assertEquals(0, rpc.closeCalls)
        } finally { repo.close() }
    }

    @Test fun completeNestedJsonReachesTransportAndSynchronizesNormally() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val info = JSONObject().put("ticket", "public-ticket").put("code", "temporary-code")
                .put("version", 1).put("endpoint_id", "desktop").put("expires_at", System.currentTimeMillis() / 1000.0 + 300)
            repo.pair(JSONObject().put("pairing", info).toString(), "测试手机")
            assertEquals(1, rpc.connectCalls); assertEquals(1, rpc.pairCalls)
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.READY, repo.state.value.syncState)
            assertNull(repo.state.value.error)
        } finally { repo.close() }
    }
}
