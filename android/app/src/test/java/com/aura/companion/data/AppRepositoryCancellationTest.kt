package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Reproduces the cancellation caused by leaving for, then returning from a picker. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AppRepositoryCancellationTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    private class SuspendedRpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        @Volatile var blockConnect = false
        @Volatile var blockChat = false
        @Volatile var desktopBusy = false
        var connectCalls = 0
        var closeCalls = 0
        val connectEntered = CompletableDeferred<Unit>()
        val chatEntered = CompletableDeferred<Unit>()
        val chatIds = mutableListOf<String>()
        val operationQueries = mutableListOf<String>()
        private val accepted = mutableSetOf<String>()

        override suspend fun connect(ticket: String) {
            require(ticket == "public-ticket")
            connectCalls++
            if (blockConnect) {
                connectEntered.complete(Unit)
                awaitCancellation()
            }
        }

        override suspend fun close() {
            // Cleanup must still complete after its original caller is cancelled.
            yield()
            closeCalls++
        }

        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("id", requestId).put("ok", true).put("result", result)
            return when (method) {
                "status" -> ok(JSONObject().put("protocol_version", 1).put("chat_busy", desktopBusy))
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("next_cursor", "").put("has_more", false))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray())
                    .put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                "operation.get" -> {
                    val original = params.getString("request_id")
                    operationQueries.add(original)
                    ok(JSONObject().put("state", if (original in accepted) "pending" else "not_found"))
                }
                "chat.send" -> {
                    chatIds.add(requestId)
                    if (blockChat) {
                        // The desktop can accept a request just before its reply
                        // is cancelled. Reconnecting must query, not clone it.
                        accepted.add(requestId)
                        chatEntered.complete(Unit)
                        awaitCancellation()
                    }
                    ok(JSONObject().put("accepted", true).put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId)))
                }
                else -> error("Unexpected method $method")
            }
        }
    }

    private fun seededDb() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("电脑", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }

    @Test fun cancelledReconnectBecomesDisconnectedAndKeepsTheOriginalQueuedRequest() = runBlocking {
        val db = seededDb()
        val rpc = SuspendedRpc().apply { blockConnect = true }
        val repository = AppRepository(context, rpc, db)
        var attempt: Job? = null
        try {
            repository.sendLegacyMessage(repository.newConversation(), "取消重连时仍保留的消息")
            val requestId = db.outbox().single().requestId
            val reconnect = launch { repository.reconnect() }
            attempt = reconnect
            withTimeout(5_000) { rpc.connectEntered.await() }
            assertEquals(ConnectionState.CONNECTING, repository.state.value.connectionState)

            withTimeout(5_000) { reconnect.cancelAndJoin() }

            assertTrue(reconnect.isCancelled)
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(SyncState.IDLE, repository.state.value.syncState)
            assertEquals("unknown", repository.state.value.connectionType)
            assertEquals(1, rpc.closeCalls)
            assertEquals(requestId, db.outbox().single().requestId)
            assertEquals("queued", db.outbox().single().status)

            rpc.blockConnect = false
            withTimeout(5_000) { repository.reconnect() }
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertEquals(SyncState.READY, repository.state.value.syncState)
            assertEquals(listOf(requestId), rpc.chatIds)
            assertEquals("sent", db.outbox().single().status)
        } finally {
            attempt?.cancelAndJoin()
            repository.close()
        }
    }

    @Test fun cancelledSyncReconnectsAndQueriesTheSamePossiblyAcceptedRequest() = runBlocking {
        val db = seededDb()
        val rpc = SuspendedRpc().apply { desktopBusy = true }
        val repository = AppRepository(context, rpc, db)
        var syncing: Job? = null
        try {
            repository.reconnect()
            repository.sendLegacyMessage(repository.newConversation(), "回复被取消时不能重复触发 AI")
            val requestId = db.outbox().single().requestId
            rpc.desktopBusy = false
            rpc.blockChat = true
            val sync = launch { repository.syncNow() }
            syncing = sync
            withTimeout(5_000) { rpc.chatEntered.await() }
            assertEquals(SyncState.SYNCING, repository.state.value.syncState)

            withTimeout(5_000) { sync.cancelAndJoin() }

            assertTrue(sync.isCancelled)
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(SyncState.IDLE, repository.state.value.syncState)
            assertEquals("unknown", db.outbox().single().status)
            assertEquals(requestId, db.outbox().single().requestId)
            assertEquals(1, rpc.closeCalls)

            rpc.blockChat = false
            withTimeout(5_000) { repository.reconnect() }
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertEquals(SyncState.READY, repository.state.value.syncState)
            assertEquals(listOf(requestId), rpc.operationQueries)
            assertEquals(listOf(requestId), rpc.chatIds)
            assertEquals(1, db.outbox().size)
            assertEquals("pending", db.outbox().single().status)
        } finally {
            syncing?.cancelAndJoin()
            repository.close()
        }
    }
}
