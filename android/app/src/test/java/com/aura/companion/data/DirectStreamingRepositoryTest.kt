package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class DirectStreamingRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val config get() = DirectApiConfig(LOCAL_PHONE_AUTHORITY, "local", "https://test.invalid/v1", "synthetic-model",
        "SYNTHETIC_SECRET", "test prompt", 30, "default", 0, ApiConfigSource.LOCAL)
    private class Vault(val config: DirectApiConfig) : ApiConfigVault {
        override fun load() = config
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }
    private class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var calls = 0
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            calls++; error("Offline phone chat must not wait for a computer")
        }
    }
    private class Stream : DirectChatGateway {
        var calls = 0
        val first = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<Unit>()
        var fail = false
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("Must use stream")
        override suspend fun stream(config: DirectApiConfig, messages: JSONArray, onUpdate: (DirectChatUpdate) -> Unit): String {
            calls++; onUpdate(DirectChatUpdate("已经收到的内容")); first.complete(Unit); finish.await()
            if (fail) throw DirectApiFailure("合成断流；未重试")
            onUpdate(DirectChatUpdate("已经收到的内容，完整回复")); return "已经收到的内容，完整回复"
        }
    }
    private suspend fun await(check: () -> Boolean) = withTimeout(5000) { while (!check()) delay(10) }

    @Test fun incrementalPreviewIsVisibleBeforeCompletionAndOnlyFinishedTurnCanImport() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = Stream(); val rpc = Rpc()
        val repo = AppRepository(context, rpc, db, Vault(config), gateway)
        try {
            val cid = repo.newConversation(); repo.sendMessage(cid, "offline request")
            withTimeout(3000) { gateway.first.await() }
            await { repo.state.value.messages.any { it.role == "assistant" && it.content == "已经收到的内容" } }
            assertTrue(repo.state.value.directChatBusy)
            assertEquals(cid, repo.state.value.directChatConversationId)
            assertEquals("generating", db.directTurns().single().state)
            assertEquals(0, db.historyPendingCount()); assertTrue(db.outbox().isEmpty())
            gateway.finish.complete(Unit); await { !repo.state.value.directChatBusy }
            assertEquals("ready", db.directTurns().single().state)
            assertEquals("已经收到的内容，完整回复", db.messages(cid).last().content)
            assertEquals(1, gateway.calls); assertEquals(0, rpc.calls)
        } finally { repo.close() }
    }

    @Test fun cancelAndBrokenStreamRetainPartialWithoutCompletionImportOrAutomaticRetry() = runBlocking {
        for (cancel in listOf(true, false)) {
            val db = CompanionDatabase(context, null); val gateway = Stream().apply { fail = !cancel }; val rpc = Rpc()
            val repo = AppRepository(context, rpc, db, Vault(config), gateway)
            try {
                val cid = repo.newConversation(); repo.sendMessage(cid, "request")
                withTimeout(3000) { gateway.first.await() }
                // A stop from another conversation cannot stop this request.
                repo.cancelDirectChat("different-conversation"); assertTrue(repo.state.value.directChatBusy)
                if (cancel) repo.cancelDirectChat(cid) else gateway.finish.complete(Unit)
                await { !repo.state.value.directChatBusy }
                assertEquals("unknown", db.directTurns().single().state)
                assertEquals("已经收到的内容", db.directTurns().single().reply)
                assertEquals("unknown", db.messages(cid).last().localState)
                assertEquals(0, db.historyPendingCount()); db.prepareDirectImports(); assertTrue(db.outbox().isEmpty())
                delay(50); assertEquals(1, gateway.calls); assertEquals(0, rpc.calls)
            } finally { repo.close() }
        }
    }
}
