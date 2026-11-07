package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
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
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class PhoneAdmissionRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        var value: DirectApiConfig? = null
        override fun load() = value
        override fun save(config: DirectApiConfig) { value = config }
        override fun clear() { value = null }
    }
    private class NoRpc : RpcGateway {
        override val endpointId = "owned-phone"
        override val connectionType = "direct"
        override suspend fun connect(ticket: String): Unit = error("No PC connection allowed")
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject = error("No RPC allowed")
    }
    private class BlockedModel : DirectChatGateway {
        val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
        val calls = AtomicInteger()
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String {
            calls.incrementAndGet(); entered.complete(Unit); finish.await(); return "owned reply"
        }
    }

    /** Exact former ViewModel heuristic under a legal delayed-continuation interleaving. */
    @Test fun legacyCollectionDifferenceMisattributesTheRejectedConcurrentCall() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = BlockedModel()
        val repo = AppRepository(context, NoRpc(), db, Vault(), gateway)
        try {
            assertTrue(repo.saveDirectApiConfig("https://owned.example/v1", "owned-model", "SYNTHETIC_OWNED", 30, "default"))
            val cid = repo.newConversation(); val ready = AtomicInteger(); val start = CompletableDeferred<Unit>()
            val bothReady = CompletableDeferred<Unit>()
            val calls = (1..2).map { index -> async(Dispatchers.IO) {
                val turns = db.directTurns().map { it.id }.toSet()
                val queued = db.outbox().map { it.requestId }.toSet()
                if (ready.incrementAndGet() == 2) bothReady.complete(Unit)
                start.await(); repo.sendMessage(cid, "owned concurrent $index")
                // A continuation may resume after the other call committed its turn.
                gateway.entered.await()
                db.directTurns().any { it.conversationId == cid && it.id !in turns } ||
                    db.outbox().any { it.conversationId == cid && it.requestId !in queued }
            } }
            withTimeout(5000) { bothReady.await() }; start.complete(Unit)
            val incorrectlyAccepted = withTimeout(5000) { calls.awaitAll() }
            assertEquals(1, gateway.calls.get()); assertEquals(1, db.directTurns().size)
            assertEquals(2, incorrectlyAccepted.count { it })
        } finally { gateway.finish.complete(Unit); repo.close() }
    }

    @Test fun exactAdmissionReceiptDistinguishesConcurrentOwnerFromRejectedCall() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = BlockedModel()
        val repo = AppRepository(context, NoRpc(), db, Vault(), gateway)
        try {
            assertTrue(repo.saveDirectApiConfig("https://owned.example/v1", "owned-model", "SYNTHETIC_OWNED", 30, "default"))
            val cid = repo.newConversation(); val start = CompletableDeferred<Unit>()
            val calls = (1..2).map { index -> async(Dispatchers.IO) { start.await(); repo.sendMessage(cid, "owned receipt $index") } }
            start.complete(Unit)
            val admissions = withTimeout(5000) { calls.awaitAll() }
            withTimeout(5000) { gateway.entered.await() }
            assertEquals(1, admissions.count { it }); assertEquals(1, admissions.count { !it })
            assertEquals(1, gateway.calls.get()); assertEquals(1, db.directTurns().size)
            assertFalse("No reply has completed; receipt must not wait for it", gateway.finish.isCompleted)
        } finally { gateway.finish.complete(Unit); repo.close() }
    }

    @Test fun missingConfigurationAndInvalidPreflightNeverAcknowledgeSubmission() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = BlockedModel()
        val repo = AppRepository(context, NoRpc(), db, Vault(), gateway)
        try {
            val cid = repo.newConversation()
            assertFalse(repo.sendMessage(cid, "no configured route"))
            assertTrue(repo.saveDirectApiConfig("https://owned.example/v1", "owned-model", "SYNTHETIC_OWNED", 30, "default"))
            assertFalse(repo.sendMessage(cid, "x".repeat(8001)))
            assertFalse(repo.sendMessage("not-a-canonical-cid", "invalid scope"))
            assertTrue(db.directTurns().isEmpty()); assertTrue(db.outbox().isEmpty()); assertEquals(0, gateway.calls.get())
        } finally { gateway.finish.complete(Unit); repo.close() }
    }
}
