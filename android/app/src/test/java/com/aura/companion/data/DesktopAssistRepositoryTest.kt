package com.aura.companion.data

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
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
import org.robolectric.annotation.GraphicsMode
import java.io.IOException

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class DesktopAssistRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        var config: DirectApiConfig? = DirectApiConfig(LOCAL_PHONE_AUTHORITY, "local", "https://example.test/v1", "model",
            "synthetic-key", "system", 30, "default", 0, ApiConfigSource.LOCAL)
        override fun load() = config
        override fun save(config: DirectApiConfig) { this.config = config }
        override fun clear() { config = null }
    }
    private class Model : DirectChatGateway {
        var calls = 0
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String { calls++; return "phone reply" }
    }
    private class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var supported = true
        var allowed: Boolean? = null
        var agentAvailable = true
        var agentBusy = false
        var lose = false
        var reserve = true
        var taskCalls = 0
        var captures = 0
        var checks = 0
        var cancellations = 0
        var uploads = 0
        var statusGate: CompletableDeferred<Unit>? = null
        val statusEntered = CompletableDeferred<Unit>()
        val tasks = mutableListOf<JSONObject>()
        val saved = mutableMapOf<String, JSONObject>()
        val image = JSONObject().put("id", "a".repeat(64)).put("sha256", "a".repeat(64)).put("filename", "desktop.png")
            .put("mime_type", "image/png").put("size", 100).put("width", 24).put("height", 16).put("created_at", "2026-11-01T00:00:00Z")
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("ok", true).put("result", result)
            return when (method) {
                "status" -> {
                    statusGate?.let { gate -> statusGate = null; statusEntered.complete(Unit); gate.await() }
                    ok(JSONObject().put("protocol_version", 1).put("chat_busy", false)
                        .put("agent_available", agentAvailable).put("agent_busy", agentBusy)
                        .put("agent_unavailable_reason", "PRIVATE_REASON").put("capabilities",
                    JSONArray(if (supported) listOf("agent.send", "agent.cancel", "chat.desktop_context", "desktop.snapshot", "chat.image") else emptyList<String>())).apply {
                    allowed?.let { put("desktop_context", JSONObject().put("available", it).put("reason", "PRIVATE_REASON")) }
                }) }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray().apply { if (captures > 0) put(image) }).put("has_more", false).put("next_offset", 1))
                "operation.get" -> { checks++; ok(saved[params.getString("request_id")] ?: JSONObject().put("state", "not_found")) }
                "agent.cancel" -> { cancellations++; agentBusy = false; ok(JSONObject().put("cancelled", true)) }
                "image.upload" -> {
                    uploads++
                    val result = JSONObject(image.toString()).put("id", params.getString("sha256")).put("sha256", params.getString("sha256"))
                    saved[requestId] = JSONObject().put("state", "complete").put("result", ok(result))
                    ok(result)
                }
                "agent.send", "chat.send", "desktop.snapshot" -> {
                    val result = if (method in setOf("agent.send", "chat.send")) {
                        taskCalls++; tasks += JSONObject(params.toString())
                        JSONObject().put("conversation_id", params.getString("conversation_id"))
                            .put("message_id", AppRepository.stableMessageId(endpointId, requestId))
                    } else { captures++; image }
                    if (reserve) saved[requestId] = JSONObject().put("state", "complete").put("result", ok(result))
                    if (lose) { lose = false; throw IOException("Deliberately lost response") }
                    ok(result)
                }
                else -> error("Unexpected method: $method")
            }
        }
    }
    private fun database() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""), true)
    }
    private suspend fun await(condition: () -> Boolean) = withTimeout(5000) { while (!condition()) delay(10) }
    private fun png() = Base64.decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==", Base64.NO_WRAP)

    @Test fun offlineTaskDoesNotQueueOrConsumeDraftAndOrdinaryChatStillRunsOnPhone() = runBlocking {
        val db = database(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            val cid = repo.newConversation(); repo.stageImage(cid, png(), "draft.png", "image/png")
            assertFalse(repo.sendDesktopMessage(cid, "请看电脑屏幕")); assertNull(repo.requestDesktopScreenshot())
            assertTrue(db.outbox().isEmpty()); assertNotNull(db.pendingImage(cid)); assertEquals(0, rpc.taskCalls)
            repo.sendMessage(cid, "phone-only image"); await { !repo.state.value.directChatBusy }
            assertEquals(1, model.calls); assertEquals(0, rpc.taskCalls)
        } finally { repo.close() }
    }

    @Test fun explicitConnectedTaskUsesAgentWithoutScreenshotGateAndCannotBecomePhoneGeneration() = runBlocking {
        val db = database(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            rpc.allowed = false
            repo.reconnect(); assertTrue(repo.state.value.desktopTaskAvailable)
            val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "请查看电脑并处理任务"))
            assertEquals(1, rpc.taskCalls); assertEquals(0, model.calls)
            assertFalse(rpc.tasks.single().has("desktop_context")); assertFalse(rpc.tasks.single().has("image_id"))
            assertEquals("agent.send", db.outbox().single().method); assertEquals(0, rpc.captures)
            assertFalse(db.outbox().single().canSendOnPhone)
            repo.disconnect(); repo.sendMessage(cid, "普通聊天")
            await { !repo.state.value.directChatBusy }; assertEquals(1, model.calls); assertEquals(1, rpc.taskCalls)
        } finally { repo.close() }
    }

    @Test fun explicitImageUploadsOnceBeforeAgentAndUnavailableDesktopKeepsComposer() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), Model())
        try {
            repo.reconnect(); val cid = repo.newConversation(); repo.stageImage(cid, png(), "keep.png", "image/png")
            val imageId = db.pendingImage(cid)!!.attachmentId
            assertTrue(repo.sendDesktopMessage(cid, "use desktop")); assertNull(db.pendingImage(cid))
            assertEquals(1, rpc.uploads); assertEquals(imageId, rpc.tasks.single().getString("image_id"))
            assertEquals(0, rpc.captures)
            repo.stageImage(cid, png(), "keep.png", "image/png"); rpc.agentAvailable = false; repo.syncNow()
            assertFalse(repo.state.value.desktopTaskAvailable); assertFalse(repo.sendDesktopMessage(cid, "permission denied"))
            assertNotNull(db.pendingImage(cid))
            assertFalse(repo.state.value.desktopTaskMessage.contains("PRIVATE_REASON"))
            rpc.agentAvailable = true; rpc.supported = false; repo.reconnect()
            assertFalse(repo.state.value.desktopTaskAvailable); assertFalse(repo.sendDesktopMessage(cid, "not supported"))
            assertEquals(1, rpc.taskCalls)
        } finally { repo.close() }
    }

    @Test fun lostTaskResponseQueriesOriginalOperationWithoutResendingOrPhoneConversion() = runBlocking {
        val db = database(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); val cid = repo.newConversation(); rpc.lose = true
            assertTrue(repo.sendDesktopMessage(cid, "once only"))
            val task = db.outbox().single(); assertEquals("unknown", task.status); assertFalse(task.canSendOnPhone)
            repo.sendQueuedOnPhone(task.requestId); assertEquals(0, model.calls)
            repo.reconnect(); assertEquals("sent", db.operation(task.requestId)!!.item.status)
            assertEquals(1, rpc.taskCalls); assertTrue(rpc.checks > 0); assertEquals(0, model.calls)
        } finally { repo.close() }
    }

    @Test fun unregisteredOrNeverAttemptedDesktopTaskIsNotExecutedOnReconnect() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), Model())
        try {
            repo.reconnect(); val cid = repo.newConversation(); rpc.lose = true; rpc.reserve = false
            assertTrue(repo.sendDesktopMessage(cid, "uncertain")); val unknown = db.outbox().single().requestId
            repo.reconnect(); assertEquals("needs_attention", db.operation(unknown)!!.item.status); assertEquals(1, rpc.taskCalls)
            val queued = db.queueAgentMessage(cid, "stale queued task")
            assertFalse(db.operation(queued)!!.item.canSendOnPhone)
            repo.syncNow(); assertEquals("needs_attention", db.operation(queued)!!.item.status); assertEquals(1, rpc.taskCalls)
        } finally { repo.close() }
    }

    @Test fun screenshotIsAnImageNotAChatTurnAndLostResponseNeverCapturesTwice() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), Model())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertEquals("a".repeat(64), repo.requestDesktopScreenshot()); assertTrue(db.messages(cid).isEmpty())
            rpc.lose = true; assertNull(repo.requestDesktopScreenshot()); assertEquals(2, rpc.captures)
            repo.reconnect(); assertEquals(2, rpc.captures); assertTrue(db.outbox().all { it.status == "sent" })
            assertTrue(db.images().any { it.id == "a".repeat(64) }); assertTrue(db.messages(cid).isEmpty())
        } finally { repo.close() }
    }

    @Test fun agentModePersistsPerConversationAcrossRepositoryRestartAndDoesNotFollowAnotherComputer() = runBlocking {
        val folder = java.nio.file.Files.createTempDirectory("agent-mode").toFile()
        val persistentContext = object : android.content.ContextWrapper(context) {
            override fun getApplicationContext(): Context = this
            override fun getDatabasePath(name: String) = java.io.File(folder, name)
            override fun deleteDatabase(name: String) = android.database.sqlite.SQLiteDatabase.deleteDatabase(getDatabasePath(name))
        }
        val name = "mode.db"
        val first = CompanionDatabase(persistentContext, name).apply { saveDevice(DeviceInfo("PC", "desktop", "ticket"), "phone") }
        val repo = AppRepository(persistentContext, Rpc(), first, Vault(), Model())
        val cid = repo.newConversation(); val other = repo.newConversation()
        repo.setDesktopAgentMode(cid, true)
        repo.selectConversation(cid)
        assertEquals(setOf(cid), repo.state.value.desktopAgentConversations)
        repo.close()
        val reopened = CompanionDatabase(persistentContext, name)
        val after = AppRepository(persistentContext, Rpc(), reopened, Vault(), Model())
        try {
            after.selectConversation(cid)
            assertEquals(setOf(cid), after.state.value.desktopAgentConversations)
            after.selectConversation(other)
            assertFalse(other in after.state.value.desktopAgentConversations)
            reopened.saveDevice(DeviceInfo("B", "desktop-b", "ticket-b"), "phone")
            assertTrue(reopened.desktopAgentConversations().isEmpty())
        } finally { after.close(); persistentContext.deleteDatabase(name); folder.delete() }
    }

    @Test fun busyAgentKeepsDraftAndExplicitCancelDoesNotCallModelOrCaptureScreen() = runBlocking {
        val db = database(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); val cid = repo.newConversation()
            repo.stageImage(cid, png(), "keep.png", "image/png"); rpc.agentBusy = true
            assertFalse(repo.sendDesktopMessage(cid, "continue task"))
            assertTrue(db.outbox().isEmpty()); assertNotNull(db.pendingImage(cid))
            assertTrue(repo.state.value.desktopAgentBusy)
            assertTrue(repo.cancelDesktopTask(cid)); assertFalse(repo.state.value.desktopAgentBusy)
            assertEquals(1, rpc.cancellations); assertEquals(0, rpc.taskCalls); assertEquals(0, rpc.captures); assertEquals(0, model.calls)
            repo.disconnect(); assertFalse(repo.cancelDesktopTask(cid)); assertEquals(1, rpc.cancellations)
        } finally { repo.close() }
    }

    @Test fun neutralFollowUpUsesSameAgentConversationWithoutKeywordRoutingOrInitialCapture() = runBlocking {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), Model())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            assertTrue(repo.sendDesktopMessage(cid, "make a file"))
            assertTrue(repo.sendDesktopMessage(cid, "continue"))
            assertEquals(2, rpc.taskCalls); assertEquals(0, rpc.captures)
            assertEquals(listOf(cid, cid), rpc.tasks.map { it.getString("conversation_id") })
            assertTrue(db.outbox().all { it.method == "agent.send" && !it.canSendOnPhone })
        } finally { repo.close() }
    }

    private suspend fun rejectTargetChangeWhileWaitingForNetworkMutex(snapshot: Boolean) = coroutineScope {
        val db = database(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, Vault(), Model())
        val gate = CompletableDeferred<Unit>()
        try {
            repo.reconnect(); val cid = repo.newConversation()
            rpc.statusGate = gate
            val poll = launch { repo.syncNow() }
            rpc.statusEntered.await()
            // UNDISTPATCHED reaches perform's IO dispatch only after the public
            // entry has captured A. Its mutex is held by the controlled poll.
            val request = async(start = CoroutineStart.UNDISPATCHED) {
                if (snapshot) repo.requestDesktopScreenshot() != null else repo.sendDesktopMessage(cid, "only for computer A")
            }
            db.saveDevice(DeviceInfo("B", "desktop-b", "public-ticket-b"), "phone")
            gate.complete(Unit); poll.join()
            assertFalse(request.await())
            assertEquals(0, rpc.taskCalls); assertEquals(0, rpc.captures); assertTrue(db.outbox().isEmpty())
            assertTrue(repo.state.value.error!!.contains("目标电脑已更换"))
        } finally { gate.complete(Unit); repo.close() }
    }

    @Test fun queuedDesktopTaskCannotFollowAPairingChangeToAnotherComputer() = runBlocking {
        rejectTargetChangeWhileWaitingForNetworkMutex(snapshot = false)
    }

    @Test fun queuedScreenshotCannotFollowAPairingChangeToAnotherComputer() = runBlocking {
        rejectTargetChangeWhileWaitingForNetworkMutex(snapshot = true)
    }
}
