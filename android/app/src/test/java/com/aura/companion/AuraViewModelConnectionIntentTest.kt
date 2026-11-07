@file:OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)

package com.aura.companion

import android.app.Application
import androidx.lifecycle.viewModelScope
import androidx.test.core.app.ApplicationProvider
import com.aura.companion.data.ApiConfigVault
import com.aura.companion.data.AppRepository
import com.aura.companion.data.CompanionDatabase
import com.aura.companion.data.ConnectionState
import com.aura.companion.data.DirectApiConfig
import com.aura.companion.data.DirectChatGateway
import com.aura.companion.data.RpcGateway
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestCoroutineScheduler
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import kotlin.coroutines.CoroutineContext

/** Actual ViewModel and Repository, with isolated SQLite and finite inert RPC. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AuraViewModelConnectionIntentTest {
    private class Vault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }

    private class NoModel : DirectChatGateway {
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("NO_MODEL")
    }

    private class Rpc : RpcGateway {
        override val endpointId = "owned-phone"
        override val connectionType = "direct"
        val connects = AtomicInteger()
        val closes = AtomicInteger()
        val imageEntered = CountDownLatch(1)
        val imageRelease = CompletableDeferred<Unit>()
        var rejectPair = false

        override suspend fun connect(ticket: String) { connects.incrementAndGet() }
        override suspend fun close() { closes.incrementAndGet() }
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            if (method == "pair" && rejectPair) return JSONObject().put("id", requestId).put("ok", false)
                .put("error", JSONObject().put("code", "pairing_failed").put("message", "OWNED_PAIR_FAILED"))
            val result = when (method) {
                "pair" -> JSONObject().put("paired", true)
                "status" -> JSONObject().put("protocol_version", 1).put("capabilities", JSONArray())
                "sync.snapshot" -> JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("has_more", false).put("next_cursor", "")
                "sync.pull" -> JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since"))
                    .put("has_more", false)
                "image.list" -> {
                    imageEntered.countDown()
                    imageRelease.await()
                    JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0)
                }
                else -> error("UNEXPECTED_RPC_$method")
            }
            return JSONObject().put("id", requestId).put("ok", true).put("result", result)
        }
    }

    /** Observe an actual pair IO-to-Main resume without draining the Main queue. */
    private class MainQueue(private val scheduler: TestCoroutineScheduler) : CoroutineDispatcher() {
        private val delegate = StandardTestDispatcher(scheduler)
        private var captureThread: Thread? = null
        private var pairJob: Job? = null
        val pairResumeQueued = CountDownLatch(1)
        @Volatile var observePairResume = false

        fun capturePair(block: () -> Unit) {
            captureThread = Thread.currentThread()
            try { block() }
            finally { captureThread = null }
            check(pairJob != null)
        }

        private fun owns(root: Job, job: Job?): Boolean = root === job || root.children.any { owns(it, job) }

        override fun dispatch(context: CoroutineContext, block: Runnable) {
            val job = context[Job]
            if (captureThread === Thread.currentThread() && pairJob == null) pairJob = job
            delegate.dispatch(context, block)
            if (observePairResume && pairJob?.let { owns(it, job) } == true) pairResumeQueued.countDown()
        }
    }

    private fun withModel(block: (AuraViewModel, AppRepository, Rpc, TestCoroutineScheduler, MainQueue, Application) -> Unit) {
        val scheduler = TestCoroutineScheduler()
        val main = MainQueue(scheduler)
        Dispatchers.setMain(main)
        val app = ApplicationProvider.getApplicationContext<Application>()
        app.getSharedPreferences("appearance", 0).edit().clear().putBoolean("auto_connect", false).commit()
        val rpc = Rpc()
        val repository = AppRepository(app, rpc, CompanionDatabase(app, null), Vault(), NoModel())
        val model = AuraViewModel(app, repository)
        try {
            scheduler.runCurrent()
            block(model, repository, rpc, scheduler, main, app)
        } finally {
            rpc.imageRelease.complete(Unit)
            model.viewModelScope.cancel()
            try {
                drainUntil(scheduler) { model.viewModelScope.coroutineContext[Job]?.isCompleted != false }
            } finally {
                try { runBlocking { withTimeout(5_000) { repository.close() } } }
                finally { Dispatchers.resetMain() }
            }
        }
    }

    private fun pairInput() = JSONObject().put("ticket", "owned-public-ticket").put("endpoint_id", "owned-pc")
        .put("version", 1).put("code", "OWNED_EPHEMERAL_CODE").toString()

    private fun awaitPairReturn(model: AuraViewModel, rpc: Rpc, scheduler: TestCoroutineScheduler, main: MainQueue) {
        main.capturePair { model.pair(pairInput()) }
        scheduler.runCurrent()
        assertTrue("Pair reached its real final repository RPC", rpc.imageEntered.await(5, TimeUnit.SECONDS))
        main.observePairResume = true
        rpc.imageRelease.complete(Unit)
        assertTrue("Actual pair continuation queued on Main", main.pairResumeQueued.await(5, TimeUnit.SECONDS))
    }

    private fun drainUntil(scheduler: TestCoroutineScheduler, condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (!condition() && System.nanoTime() < deadline) {
            scheduler.runCurrent()
            Thread.sleep(2)
        }
        scheduler.runCurrent()
        assertTrue("Owned async IO completed", condition())
    }

    @Test fun manualDisconnectWinsOverAnOlderSuccessfulPairContinuation() = withModel { model, repository, rpc, scheduler, main, app ->
        awaitPairReturn(model, rpc, scheduler, main)
        assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
        model.disconnect()
        assertFalse(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", true))
        drainUntil(scheduler) { repository.state.value.connectionState == ConnectionState.DISCONNECTED }
        assertFalse("Old pair must not undo the user's disconnect", app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", true))
        val connections = rpc.connects.get()
        model.setForeground(true)
        scheduler.runCurrent()
        assertEquals(connections, rpc.connects.get())
    }

    @Test fun normalPairAllowsAutomaticReconnectAndKeepsThePublicApplicationConstructor() = withModel { model, _, rpc, scheduler, main, app ->
        assertNotNull(AuraViewModel::class.java.getConstructor(Application::class.java))
        awaitPairReturn(model, rpc, scheduler, main)
        drainUntil(scheduler) { !model.state.value.pairing }
        assertTrue(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", false))
    }

    @Test fun explicitReconnectAfterDisconnectStillWins() = withModel { model, repository, rpc, scheduler, main, app ->
        awaitPairReturn(model, rpc, scheduler, main)
        model.disconnect()
        model.reconnect()
        drainUntil(scheduler) { rpc.connects.get() == 2 && !model.state.value.pairing &&
            repository.state.value.connectionState == ConnectionState.CONNECTED }
        assertTrue(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", false))
    }

    @Test fun aFreshPairAfterDisconnectCanEnableReconnectAgain() = withModel { model, repository, rpc, scheduler, main, app ->
        awaitPairReturn(model, rpc, scheduler, main)
        model.disconnect()
        drainUntil(scheduler) { !model.state.value.pairing && repository.state.value.connectionState == ConnectionState.DISCONNECTED }
        model.pair(pairInput())
        drainUntil(scheduler) { rpc.connects.get() == 2 && !model.state.value.pairing &&
            repository.state.value.connectionState == ConnectionState.CONNECTED }
        assertTrue(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", false))
    }

    @Test fun aFailedPairPreservesTheExistingAutomaticReconnectChoice() = withModel { model, repository, rpc, scheduler, _, app ->
        rpc.rejectPair = true
        model.pair(pairInput())
        drainUntil(scheduler) { repository.state.value.connectionState == ConnectionState.ERROR && !model.state.value.pairing }
        assertFalse(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", true))
    }

    @Test fun aFailedPairDoesNotDisableAnAlreadyEnabledReconnectChoice() = withModel { model, repository, rpc, scheduler, main, app ->
        awaitPairReturn(model, rpc, scheduler, main)
        drainUntil(scheduler) { !model.state.value.pairing }
        assertTrue(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", false))
        rpc.rejectPair = true
        model.pair(pairInput())
        drainUntil(scheduler) { repository.state.value.connectionState == ConnectionState.ERROR && !model.state.value.pairing }
        assertTrue(app.getSharedPreferences("appearance", 0).getBoolean("auto_connect", false))
    }
}
