package com.aura.companion.data

import android.content.Context
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

/** Synthetic protocol/SQLite admission evidence, not actual PC task execution. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AgentQueueRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }
    private class NoModel : DirectChatGateway {
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("No model call for PC admission")
    }
    private class Rpc : RpcGateway {
        override val endpointId = "synthetic-phone"
        override val connectionType = "direct"
        var queueCapability = true
        var available = true
        var agentBusy = true
        var chatBusy = true
        var lose = false
        var reserve = true
        var duplicateAck = false
        var checks = 0
        var mutateAck: ((JSONObject) -> Unit)? = null
        val sends = mutableListOf<String>()
        val saved = mutableMapOf<String, JSONObject>()
        val jobIds = mutableMapOf<String, String>()
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("ok", true).put("result", result)
            return when (method) {
                "status" -> {
                    ok(JSONObject().put("protocol_version", 1).put("agent_available", available)
                        .put("agent_busy", agentBusy).put("chat_busy", chatBusy)
                        .put("capabilities", JSONArray(listOf("agent.send", "agent.cancel") +
                            if (queueCapability) listOf("agent.queue.v1") else emptyList<String>())))
                }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                "operation.get" -> { checks++; ok(saved[params.getString("request_id")] ?: JSONObject().put("state", "not_found")) }
                "agent.send" -> {
                    sends += requestId
                    val result = JSONObject().put("accepted", true).put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId))
                        .put("job_id", jobIds.getOrPut(requestId) { UUID.randomUUID().toString() })
                        .put("queue_state", "queued").put("execution_mode", "agent").put("reply_delivery", "sync.pull")
                    mutateAck?.invoke(result)
                    if (reserve) saved[requestId] = JSONObject().put("state", "complete").put("result", ok(result))
                    if (lose) { lose = false; throw IOException("Synthetic lost acknowledgement") }
                    if (duplicateAck) result.put("duplicate", true)
                    ok(result)
                }
                else -> error("Unexpected method: $method")
            }
        }
    }
    private fun database() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("Synthetic PC", "synthetic-desktop", "synthetic-ticket"), "synthetic-phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }

    @Test fun capableReadyHostAcceptsTwoExplicitRequestsWhileAgentAndChatAreBusy() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "First explicit task"))
            assertTrue(repo.sendDesktopMessage(cid, "Second explicit task"))
            assertEquals(2, rpc.sends.size)
            assertEquals(2, rpc.sends.toSet().size)
            assertEquals(2, rpc.jobIds.values.toSet().size)
            assertTrue(db.outbox().all { it.status == "sent" && !it.canSendOnPhone })
            // Delivery acknowledgement is not a completed assistant response.
            assertTrue(db.messages(cid).none { it.role == "assistant" })
            repo.disconnect(); repo.reconnect()
            assertEquals(2, rpc.sends.size)
        } finally { repo.close() }
    }

    @Test fun busyOlderHostWithoutQueueCapabilityKeepsOriginalWaitAndNeverQueuesTask() = runBlocking {
        val db = database(); val rpc = Rpc().apply { queueCapability = false }
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertFalse(repo.sendDesktopMessage(cid, "Old host must wait"))
            assertEquals(0, rpc.sends.size)
            assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun queueCapabilityDoesNotOverrideUnavailableAgentOrDisconnectedComputer() = runBlocking {
        val db = database(); val rpc = Rpc().apply { available = false }
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertFalse(repo.sendDesktopMessage(cid, "Unavailable host is not a queue"))
            rpc.available = true; repo.reconnect(); repo.disconnect()
            assertFalse(repo.sendDesktopMessage(cid, "Offline is not delivered"))
            assertEquals(0, rpc.sends.size)
            assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun freshPreflightDowngradeCannotUsePreviouslyCachedQueueCapability() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            rpc.queueCapability = false
            assertFalse(repo.sendDesktopMessage(cid, "Host capability changed"))
            assertEquals(0, rpc.sends.size)
            assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun lostQueuedAckQueriesSameOperationWithoutResendingTask() = runBlocking {
        val db = database(); val rpc = Rpc().apply { lose = true }
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "Only enqueue once"))
            val request = db.outbox().single()
            assertEquals("unknown", request.status)
            assertEquals(listOf(request.requestId), rpc.sends)
            repo.reconnect()
            assertEquals("sent", db.operation(request.requestId)!!.item.status)
            assertTrue(rpc.checks > 0)
            assertEquals(listOf(request.requestId), rpc.sends)
        } finally { repo.close() }
    }

    @Test fun unregisteredUnknownOrUnattemptedAgentTaskNeverAutomaticallyReplaysEvenOnQueueHost() = runBlocking {
        val db = database(); val rpc = Rpc().apply { lose = true; reserve = false }
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "Uncertain admission"))
            val uncertain = db.outbox().single().requestId
            repo.reconnect()
            assertEquals("needs_attention", db.operation(uncertain)!!.item.status)
            val neverAttempted = db.queueAgentMessage(cid, "Never explicitly dispatched")
            repo.syncNow()
            assertEquals("needs_attention", db.operation(neverAttempted)!!.item.status)
            assertEquals(listOf(uncertain), rpc.sends)
        } finally { repo.close() }
    }

    @Test fun directDuplicateQueueAcknowledgementIsDeliveryNotAnOperationEnvelope() = runBlocking {
        val db = database(); val rpc = Rpc().apply { duplicateAck = true; agentBusy = false; chatBusy = false }
        val repo = AppRepository(context, rpc, db, Vault(), NoModel())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "Existing durable task acknowledgement"))
            val item = db.outbox().single()
            assertEquals("sent", item.status)
            assertEquals(AppRepository.stableMessageId(rpc.endpointId, item.requestId), item.messageId)
            repo.syncNow()
            assertEquals(listOf(item.requestId), rpc.sends)
            assertEquals(1, rpc.jobIds.size)
        } finally { repo.close() }
    }

    @Test fun malformedQueueAcknowledgementNeverClaimsDeliveryOrResendsOriginalTask() = runBlocking {
        val mutations: List<(JSONObject) -> Unit> = listOf(
            { it.put("job_id", "not-a-job-uuid") },
            { it.put("queue_state", "unsupported-state") },
            { it.put("accepted", "true") },
            { it.put("execution_mode", "phone") },
            { it.put("reply_delivery", "raw") },
            { it.put("message_id", UUID.randomUUID().toString()) },
        )
        for (mutation in mutations) {
            val db = database(); val rpc = Rpc().apply { mutateAck = mutation }
            val repo = AppRepository(context, rpc, db, Vault(), NoModel())
            try {
                repo.reconnect(); val cid = repo.newConversation()
                assertFalse(repo.sendDesktopMessage(cid, "Check acknowledgement identity"))
                val item = db.outbox().single()
                assertEquals("needs_attention", item.status)
                assertTrue(db.operation(item.requestId)!!.attempted)
                repo.reconnect()
                assertEquals(listOf(item.requestId), rpc.sends)
            } finally { repo.close() }
        }
    }
}
