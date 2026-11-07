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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AgentInteractionRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }
    private class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var cid: String? = null
        var interaction: JSONObject? = null
        var busy = true
        var revoked = false
        var loseAnswer = false
        var beforeStatus: (() -> Unit)? = null
        val answers = mutableListOf<JSONObject>()
        val steering = mutableListOf<JSONObject>()
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(value: JSONObject) = JSONObject().put("ok", true).put("result", value)
            return when (method) {
                "status" -> {
                    beforeStatus?.also { beforeStatus = null }?.invoke()
                    if (revoked) JSONObject().put("ok", false).put("error",
                        JSONObject().put("code", "unauthorized").put("message", "revoked"))
                    else ok(JSONObject().put("protocol_version", 1).put("agent_available", true)
                        .put("agent_busy", busy).put("agent_conversation_id", cid)
                        .put("agent_interaction", interaction).put("capabilities",
                            JSONArray(listOf("agent.send", "agent.answer", "agent.steer", "agent.cancel"))))
                }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                "agent.answer" -> {
                    answers += JSONObject(params.toString()); interaction = null
                    if (loseAnswer) { loseAnswer = false; throw IOException("synthetic lost acknowledgement") }
                    ok(JSONObject().put("accepted", true))
                }
                "agent.steer" -> { steering += JSONObject(params.toString()); ok(JSONObject().put("accepted", true)) }
                else -> error("Unexpected method $method")
            }
        }
    }
    private fun database() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }
    private fun question(cid: String, id: String = "question-1") = JSONObject().put("id", id)
        .put("kind", "question").put("question", "选择目标文件")
        .put("conversation_id", cid).put("options", JSONArray(listOf("A", "B")))
    private fun permission(cid: String, path: String = "C:\\Selected\\input.csv") = question(cid, "grant-1")
        .put("kind", "file_access").put("resource", JSONObject().put("path", path)
            .put("access", "read").put("directory", false))

    @Test fun parserRejectsMalformedGrantsAndAnswersInsteadOfCoercingBooleans() {
        val cid = UUID.randomUUID().toString()
        val value = permission(cid)
        val parsed = parseAgentInteraction(value, "desktop")!!
        assertEquals("read", parsed.resource!!.access)
        assertNull(validatedAgentAnswer(parsed, JSONObject().put("allowed", "true")))
        assertNull(validatedAgentAnswer(parsed, JSONObject().put("allowed", true).put("path", "C:\\Other")))
        value.getJSONObject("resource").put("directory", "false")
        assertNull(parseAgentInteraction(value, "desktop"))
        assertNull(parseAgentInteraction(question("not-a-uuid"), "desktop"))
        val longOption = "方案".repeat(120)
        assertEquals(listOf(longOption), parseAgentInteraction(question(cid).put("options", JSONArray(listOf(longOption))), "desktop")!!.options)
        assertNull(parseAgentInteraction(question(cid).put("options", JSONArray(listOf(longOption + "x"))), "desktop"))
    }

    @Test fun questionAnswerIsBoundToLiveIdAndNeverEntersOutbox() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            val cid = repo.newConversation(); rpc.cid = cid; rpc.interaction = question(cid)
            repo.reconnect()
            assertEquals("question-1", repo.state.value.agentInteraction?.id)
            assertFalse(repo.answerAgentInteraction(cid, "old-id", JSONObject().put("text", "A")))
            assertTrue(rpc.answers.isEmpty())
            assertTrue(repo.answerAgentInteraction(cid, "question-1", JSONObject().put("text", "B")))
            assertEquals("B", rpc.answers.single().getJSONObject("answer").getString("text"))
            assertNull(repo.state.value.agentInteraction)
            assertTrue(db.outbox().isEmpty())
            repo.disconnect(); repo.reconnect()
            assertEquals(1, rpc.answers.size)
        } finally { repo.close() }
    }

    @Test fun changedResourceWithSameIdNeedsNewUserReview() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            val cid = repo.newConversation(); rpc.cid = cid; rpc.interaction = permission(cid)
            repo.reconnect()
            rpc.interaction = permission(cid, "C:\\Different\\secret.csv")
            assertFalse(repo.answerAgentInteraction(cid, "grant-1", JSONObject().put("allowed", true)))
            assertTrue(rpc.answers.isEmpty()); assertTrue(db.outbox().isEmpty())
            assertTrue(repo.state.value.error.orEmpty().contains("访问范围"))
        } finally { repo.close() }
    }

    @Test fun revocationOrEndpointReplacementPreventsApprovalDispatch() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            val cid = repo.newConversation(); rpc.cid = cid; rpc.interaction = permission(cid)
            repo.reconnect()
            rpc.beforeStatus = { db.saveDevice(DeviceInfo("Other", "different-desktop", "other-ticket"), "phone") }
            assertFalse(repo.answerAgentInteraction(cid, "grant-1", JSONObject().put("allowed", true)))
            assertTrue(rpc.answers.isEmpty()); assertNull(repo.state.value.agentInteraction)
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            repo.reconnect(); rpc.revoked = true
            assertFalse(repo.answerAgentInteraction(cid, "grant-1", JSONObject().put("allowed", true)))
            assertTrue(rpc.answers.isEmpty()); assertEquals(ConnectionState.UNAUTHORIZED, repo.state.value.connectionState)
            assertNull(repo.state.value.agentInteraction); assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun lostAnswerAcknowledgementIsNeverReplayedByReconnect() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            val cid = repo.newConversation(); rpc.cid = cid; rpc.interaction = permission(cid)
            repo.reconnect(); rpc.loseAnswer = true
            assertFalse(repo.answerAgentInteraction(cid, "grant-1", JSONObject().put("allowed", true)))
            assertEquals(1, rpc.answers.size); assertTrue(db.outbox().isEmpty())
            repo.reconnect(); repo.poll()
            assertEquals(1, rpc.answers.size); assertNull(repo.state.value.agentInteraction)
        } finally { repo.close() }
    }

    @Test fun steeringIsLiveOnlyAndPendingQuestionTakesPriority() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            val cid = repo.newConversation(); rpc.cid = cid
            assertFalse(repo.steerDesktopTask(cid, "save a copy"))
            repo.reconnect()
            rpc.interaction = question(cid)
            assertFalse(repo.steerDesktopTask(cid, "save a copy")); assertTrue(rpc.steering.isEmpty())
            rpc.interaction = null
            assertFalse(repo.steerDesktopTask(UUID.randomUUID().toString(), "wrong conversation"))
            assertTrue(repo.steerDesktopTask(cid, "另存一份，先别发送"))
            assertEquals("另存一份，先别发送", rpc.steering.single().getString("text"))
            assertTrue(db.outbox().isEmpty())
            repo.disconnect(); repo.reconnect(); assertEquals(1, rpc.steering.size)
        } finally { repo.close() }
    }
}
