package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.aura.companion.transport.RpcTransportException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID

/** Synthetic host journal and in-memory RPC only; no actual Iroh or PC task. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AgentReconnectBoundaryReviewTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = error("No API configuration in this fixture")
        override fun clear() = Unit
    }
    private class NoModel : DirectChatGateway {
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("No model permitted")
    }
    private class Rpc : RpcGateway {
        override val endpointId = "owned-phone"
        override val connectionType = "direct"
        var loseAckAfterCompletion = false
        var blockPull = false
        val pullEntered = CompletableDeferred<Unit>()
        val sends = mutableListOf<String>()
        var operationQueries = 0
        var closes = 0
        private var completed = false
        private var cid = ""
        private var mid = ""
        private val replyId = UUID.randomUUID().toString()
        private val journal = mutableListOf<JSONObject>()
        private val date = "2026-11-06T02:00:00Z"
        override suspend fun connect(ticket: String) { require(ticket == "owned-public-ticket") }
        override suspend fun close() { closes++ }
        private fun publish(entity: String, id: String, data: JSONObject) {
            journal += JSONObject().put("seq", journal.size + 1).put("entity", entity).put("id", id)
                .put("conversation_id", cid).put("data", data)
        }
        private fun user(status: String) = JSONObject().put("id", mid).put("conversation_id", cid)
            .put("role", "user").put("content", "Owned computer request").put("source", "user")
            .put("created_at", date).put("request_status", status).put("execution_mode", "agent")
        fun completeTask() {
            check(!completed)
            completed = true
            publish("message", mid, user("complete"))
            publish("message", replyId, JSONObject().put("id", replyId).put("conversation_id", cid)
                .put("role", "assistant").put("source", "assistant").put("content", "OWNED_COMPLETED_REPLY")
                .put("reply_to", mid).put("created_at", "2026-11-06T02:00:01Z"))
        }
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("id", requestId).put("ok", true).put("result", result)
            return when (method) {
                "status" -> ok(JSONObject().put("protocol_version", 1).put("agent_available", true)
                    .put("agent_busy", sends.isNotEmpty() && !completed).put("chat_busy", false)
                    .put("capabilities", JSONArray(listOf("agent.send", "agent.cancel", "agent.queue.v1"))))
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> {
                    if (blockPull) { pullEntered.complete(Unit); awaitCancellation() }
                    val rows = journal.filter { it.getLong("seq") > params.getLong("since") }
                    ok(JSONObject().put("changes", JSONArray(rows)).put("next_since", rows.lastOrNull()?.getLong("seq")
                        ?: params.getLong("since")).put("has_more", false))
                }
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                "operation.get" -> { operationQueries++; error("Canonical pulled message should resolve this item") }
                "agent.send" -> {
                    sends += requestId
                    check(sends.size == 1) { "Original Agent submission was repeated" }
                    cid = params.getString("conversation_id")
                    mid = AppRepository.stableMessageId(endpointId, requestId)
                    publish("conversation", cid, JSONObject().put("id", cid).put("title", "Owned review")
                        .put("created_at", date).put("updated_at", date).put("message_count", 1))
                    publish("message", mid, user("pending"))
                    if (loseAckAfterCompletion) {
                        completeTask()
                        throw RpcTransportException("timeout", "Owned transport response timed out")
                    }
                    ok(JSONObject().put("accepted", true).put("conversation_id", cid).put("message_id", mid)
                        .put("job_id", UUID.randomUUID().toString()).put("queue_state", "queued")
                        .put("execution_mode", "agent").put("reply_delivery", "sync.pull"))
                }
                else -> error("Unexpected method $method")
            }
        }
    }
    private fun database() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("Owned PC", "owned-pc", "owned-public-ticket"), "owned-phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }
    private fun assertCompleted(db: CompanionDatabase, cid: String, rpc: Rpc, requestId: String) {
        val messages = db.messages(cid)
        assertEquals(2, messages.size)
        val user = messages.single { it.role == "user" }
        assertEquals("complete", user.requestStatus)
        assertNull(user.agentErrorCode)
        assertNull(user.agentError)
        assertEquals("OWNED_COMPLETED_REPLY", messages.single { it.role == "assistant" }.content)
        assertEquals(user.id, messages.single { it.role == "assistant" }.replyTo)
        assertEquals("sent", db.operation(requestId)!!.item.status)
        assertEquals(listOf(requestId), rpc.sends)
        assertEquals(0, rpc.operationQueries)
    }

    @Test fun transportTimeoutAfterHostCompletionRecoversPulledReplyWithoutTaskTimeoutOrResend() = runBlocking {
        val db = database(); val rpc = Rpc().apply { loseAckAfterCompletion = true }
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "Owned computer request"))
            val request = db.outbox().single().requestId
            assertEquals(ConnectionState.ERROR, repo.state.value.connectionState)
            assertEquals("unknown", db.operation(request)!!.item.status)
            assertNull(db.messages(cid).single().agentErrorCode)
            repo.reconnect()
            assertCompleted(db, cid, rpc, request)
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertNull(repo.state.value.error)
            repo.poll()
            assertCompleted(db, cid, rpc, request)
        } finally { repo.close() }
    }

    @Test fun cancelledPendingPollRecoversOneCompletedReplyOnForegroundReconnect() = runBlocking {
        val db = database(); val rpc = Rpc()
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "Owned computer request"))
            val request = db.outbox().single().requestId
            assertEquals("pending", db.messages(cid).single().requestStatus)
            rpc.blockPull = true
            val poll = launch { repo.poll() }
            try {
                withTimeout(5000) { rpc.pullEntered.await() }
                rpc.completeTask()
            } finally { withTimeout(5000) { poll.cancelAndJoin() } }
            assertEquals(ConnectionState.DISCONNECTED, repo.state.value.connectionState)
            assertEquals("pending", db.messages(cid).single().requestStatus)
            assertNull(db.messages(cid).single().agentErrorCode)
            rpc.blockPull = false
            repo.reconnect()
            assertCompleted(db, cid, rpc, request)
            repo.disconnect(); repo.reconnect(); repo.poll()
            assertCompleted(db, cid, rpc, request)
        } finally { repo.close() }
    }
}
