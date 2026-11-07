package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.util.UUID

/** Synthetic privacy/race fixtures only: no model, real account, peer or screenshot. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ChatBoundaryAuditTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val oldKey = "SYNTHETIC_DESKTOP_KEY_DO_NOT_FORWARD"
    private val phoneKey = "SYNTHETIC_PHONE_KEY_DO_NOT_FORWARD"
    private fun config() = DirectApiConfig(LOCAL_PHONE_AUTHORITY, "phone-revision", "https://phone.invalid/v1", "synthetic",
        phoneKey, "System with $oldKey", 30, "default", 0, ApiConfigSource.LOCAL,
        DirectApiConfig("peer-a", "desktop-revision", "https://desktop.invalid/v1", "desktop", oldKey, "system", 30, "default", 0))
    private class Vault(var value: DirectApiConfig) : ApiConfigVault {
        override fun load() = value
        override fun save(config: DirectApiConfig) { value = config }
        override fun clear() = Unit
    }
    private class OfflineRpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject = error("Offline fixture")
    }
    private class LateStream : DirectChatGateway {
        val first = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<Unit>()
        var calls = 0
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray) = error("Expected stream")
        override suspend fun stream(config: DirectApiConfig, messages: JSONArray, onUpdate: (DirectChatUpdate) -> Unit): String {
            calls++; onUpdate(DirectChatUpdate("PRIVATE_PEER_A_PARTIAL")); first.complete(Unit)
            // Deliberately emulate a non-cooperative callback after cancellation.
            return withContext(NonCancellable) {
                finish.await(); onUpdate(DirectChatUpdate("PRIVATE_PEER_A_LATE")); "PRIVATE_PEER_A_LATE"
            }
        }
    }
    private suspend fun await(check: () -> Boolean) = withTimeout(5000) { while (!check()) delay(10) }

    @Test fun nonGenerationMessageDeletionInvalidatesTheWholeCachedSummaryInSameCommit() {
        val db = CompanionDatabase(context, null)
        try {
            db.saveDevice(DeviceInfo("PC A", "peer-a", "public-ticket"), "phone")
            val cid = db.createConversation(); val first = UUID.randomUUID().toString(); val deleted = UUID.randomUUID().toString()
            val rows = listOf(Message(first, cid, "user", "Retained first", "2026-11-03T00:00:00Z"),
                Message(deleted, cid, "assistant", "PRIVATE_REVOKED_MIDDLE_TEXT", "2026-11-03T00:00:01Z"))
            for (row in rows) db.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
                arrayOf(row.id, cid, row.role, row.content, row.createdAt, row.role))
            val envelope = ContextEnvelope.local(cid, rows, "").json.put("memory_status", "available")
            envelope.getJSONObject("summary").put("text", "PRIVATE_REVOKED_MIDDLE_TEXT")
            val cache = ConversationContextCache(db)
            cache.replace("peer-a", cid, first, envelope)
            assertNotNull(cache.read("peer-a", cid, first))
            db.applyPull(JSONObject().put("changes", JSONArray().put(JSONObject().put("seq", 1).put("entity", "message")
                .put("id", deleted).put("conversation_id", cid).put("deleted", true))).put("next_since", 1))
            assertEquals(first, db.conversationGeneration(cid))
            assertNull("A same-generation deletion must not resend the deleted extract", cache.read("peer-a", cid, first))
        } finally { db.close() }
    }

    @Test fun allKnownCredentialValuesAreRedactedBeforeSystemAndHistoryReachNewProvider() {
        val cfg = config(); val cid = UUID.randomUUID().toString()
        val history = listOf(Message(UUID.randomUUID().toString(), cid, "user", "pasted $oldKey and $phoneKey", "2026-11-03T00:00:00Z"))
        val body = directMessages(cfg, history, "new request $oldKey", null).toString()
        assertFalse("A cached desktop key must not be sent to an independent phone provider", body.contains(oldKey))
        assertFalse(body.contains(phoneKey))
    }

    @Test fun staleSameGenerationContextRpcCannotRestoreCacheAfterConcurrentMutation() {
        val db = CompanionDatabase(context, null)
        try {
            db.saveDevice(DeviceInfo("PC A", "peer-a", "public-ticket"), "phone")
            val cid = db.createConversation(); val first = UUID.randomUUID().toString()
            val row = Message(first, cid, "user", "Before edit", "2026-11-03T00:00:00Z")
            db.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
                arrayOf(first, cid, row.role, row.content, row.createdAt, row.role))
            val epochBeforeRpc = db.contextEpoch(cid)
            val oldReply = ContextEnvelope.local(cid, listOf(row), "").json.put("memory_status", "available")
            val edited = JSONObject().put("entity", "message").put("id", first).put("conversation_id", cid).put("seq", 1)
                .put("data", JSONObject().put("id", first).put("conversation_id", cid).put("role", "user")
                    .put("source", "user").put("content", "After edit").put("created_at", row.createdAt))
            db.applyPull(JSONObject().put("changes", JSONArray().put(edited)).put("next_since", 1))
            assertEquals(first, db.conversationGeneration(cid))
            val cache = ConversationContextCache(db)
            try {
                cache.replace("peer-a", cid, first, oldReply, expectedEpoch = epochBeforeRpc)
                fail("An older in-flight response must not resurrect a stale summary")
            } catch (_: IllegalArgumentException) { }
            assertNull(cache.read("peer-a", cid, first))
        } finally { db.close() }
    }

    @Test fun multipleKnownSecretsStayHiddenAcrossEveryCharacterChunk() {
        val updates = mutableListOf<String>()
        val visible = DirectVisibleText(listOf(phoneKey, oldKey)) { updates += it.text }
        "公开 $oldKey 与 $phoneKey 完成".forEach { visible.append(it.toString()) }
        assertEquals("公开 [已隐藏密钥] 与 [已隐藏密钥] 完成", visible.finish())
        assertTrue(updates.all { !it.contains(phoneKey) && !it.contains(oldKey) })
        assertTrue(updates.none { it.endsWith("SYNTHETIC_") })
    }

    @Test fun stoppedPartialCannotBeFinishedOrImportedByAnyLateOrRepeatedCompletion() {
        val db = CompanionDatabase(context, null)
        try {
            db.saveDevice(DeviceInfo("PC A", "peer-a", "public-ticket"), "phone")
            val turn = db.beginDirectTurn(db.createConversation(), "request", null)
            db.failDirectTurn(turn.id, "Stopped fixture", partial = "accepted partial", interrupted = true)
            repeat(3) { db.finishDirectTurn(turn.id, "late complete response"); db.prepareDirectImports() }
            assertEquals("unknown", db.directTurns().single().state)
            assertEquals("accepted partial", db.directTurns().single().reply)
            assertTrue(db.outbox().isEmpty())
        } finally { db.close() }
    }

    @Test fun activePreviewAndLateChunkCannotTransplantToAnotherPeerWithSameConversationId() = runBlocking {
        val db = CompanionDatabase(context, null)
        db.saveDevice(DeviceInfo("PC A", "peer-a", "public-ticket-a"), "phone")
        val gateway = LateStream(); val repo = AppRepository(context, OfflineRpc(), db, Vault(config()), gateway)
        try {
            val cid = repo.newConversation(); repo.sendMessage(cid, "request")
            withTimeout(3000) { gateway.first.await() }
            await { repo.state.value.messages.any { it.content == "PRIVATE_PEER_A_PARTIAL" } }
            db.saveDevice(DeviceInfo("PC B", "peer-b", "public-ticket-b"), "phone")
            db.createConversation(cid); repo.selectConversation(cid)
            assertFalse("A private preview belongs to its full peer/request identity",
                repo.state.value.messages.any { it.content.startsWith("PRIVATE_PEER_A") })
            gateway.finish.complete(Unit); await { !repo.state.value.directChatBusy }
            assertFalse(repo.state.value.messages.any { it.content.startsWith("PRIVATE_PEER_A") })
            db.prepareDirectImports(); assertTrue(db.outbox().isEmpty()); assertEquals(1, gateway.calls)
        } finally { gateway.finish.complete(Unit); repo.close() }
    }

    @Test fun apiReplacementStopsOldPreviewAndLateUncooperativeCompletionCannotBecomeImportReady() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = LateStream(); val vault = Vault(config())
        val repo = AppRepository(context, OfflineRpc(), db, vault, gateway)
        try {
            val cid = repo.newConversation(); repo.sendMessage(cid, "request")
            withTimeout(3000) { gateway.first.await() }
            assertTrue(repo.saveDirectApiConfig("https://replacement.invalid/v1", "replacement", "SYNTHETIC_REPLACEMENT_KEY", 30, "default"))
            gateway.finish.complete(Unit); await { !repo.state.value.directChatBusy }
            assertEquals("unknown", db.directTurns().single().state)
            assertEquals("PRIVATE_PEER_A_PARTIAL", db.directTurns().single().reply)
            db.prepareDirectImports(); assertTrue(db.outbox().isEmpty()); assertEquals(1, gateway.calls)
            assertFalse("Dropped cache secrets must not survive in the replacement prompt", vault.value.systemPrompt.contains(oldKey))
        } finally { gateway.finish.complete(Unit); repo.close() }
    }

    @Test fun clearedDesktopGenerationStopsActiveOldContextAndNeverImportsItsLateReply() = runBlocking {
        val db = CompanionDatabase(context, null)
        db.saveDevice(DeviceInfo("PC A", "peer-a", "public-ticket-a"), "phone")
        val cid = db.createConversation(); val oldGeneration = UUID.randomUUID().toString()
        db.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
            arrayOf(oldGeneration, cid, "user", "PRIVATE_OLD_GENERATION", "2026-11-03T00:00:00Z", "user"))
        val gateway = LateStream(); val repo = AppRepository(context, OfflineRpc(), db, Vault(config()), gateway)
        try {
            repo.selectConversation(cid); repo.sendMessage(cid, "Follow old context")
            withTimeout(3000) { gateway.first.await() }
            db.applyPull(JSONObject().put("changes", JSONArray().put(JSONObject().put("seq", 1).put("entity", "message")
                .put("id", oldGeneration).put("conversation_id", cid).put("deleted", true))).put("next_since", 1))
            repo.selectConversation(cid)
            gateway.finish.complete(Unit); await { !repo.state.value.directChatBusy }
            assertEquals("unknown", db.directTurns().single().state)
            assertEquals("PRIVATE_PEER_A_PARTIAL", db.directTurns().single().reply)
            db.prepareDirectImports(); assertTrue(db.outbox().isEmpty()); assertEquals(1, gateway.calls)
        } finally { gateway.finish.complete(Unit); repo.close() }
    }
}
