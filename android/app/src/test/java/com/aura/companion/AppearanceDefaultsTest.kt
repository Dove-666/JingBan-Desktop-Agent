@file:OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)

package com.aura.companion

import android.app.Application
import androidx.lifecycle.viewModelScope
import androidx.test.core.app.ApplicationProvider
import com.aura.companion.data.ApiConfigVault
import com.aura.companion.data.AppRepository
import com.aura.companion.data.CompanionDatabase
import com.aura.companion.data.DirectApiConfig
import com.aura.companion.data.DirectChatGateway
import com.aura.companion.data.RpcGateway
import com.aura.companion.ui.ApiConfigSource
import com.aura.companion.ui.ThemeMode
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
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Actual ViewModel startup, isolated SQLite/preferences and forbidden network doubles. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AppearanceDefaultsTest {
    private class EmptyVault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = error("UNEXPECTED_CONFIG_SAVE")
        override fun clear() = Unit
    }

    private class NoNetwork : RpcGateway, DirectChatGateway {
        override val endpointId = "appearance-fixture-phone"
        override val connectionType = "direct"
        val attempts = AtomicInteger()
        override suspend fun connect(ticket: String) { attempts.incrementAndGet(); error("UNEXPECTED_CONNECT") }
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            attempts.incrementAndGet(); error("UNEXPECTED_RPC")
        }
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String {
            attempts.incrementAndGet(); error("UNEXPECTED_MODEL")
        }
    }

    private fun verifyStartup(savedTheme: String?, expected: ThemeMode) {
        val scheduler = TestCoroutineScheduler()
        Dispatchers.setMain(StandardTestDispatcher(scheduler))
        val app = ApplicationProvider.getApplicationContext<Application>()
        val preferences = app.getSharedPreferences("appearance", 0)
        preferences.edit().clear().apply {
            if (savedTheme != null) putString("theme", savedTheme)
        }.commit()
        val network = NoNetwork()
        val repository = AppRepository(app, network, CompanionDatabase(app, null), EmptyVault(), network)
        val model = AuraViewModel(app, repository)
        try {
            // Covers both stateIn's initial state and the real repository/UI combination.
            assertEquals(expected, model.state.value.theme)
            scheduler.runCurrent()
            model.setForeground(true)
            scheduler.runCurrent()
            assertEquals(expected, model.state.value.theme)
            assertFalse(model.state.value.directApi.available)
            assertEquals("", model.state.value.directApi.model)
            assertEquals("", model.state.value.directApi.baseUrl)
            assertEquals(ApiConfigSource.NONE, model.state.value.directApi.source)
            assertEquals(0, network.attempts.get())
            assertEquals(savedTheme, preferences.getString("theme", null))
        } finally {
            model.viewModelScope.cancel()
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
            while (model.viewModelScope.coroutineContext[Job]?.isCompleted == false && System.nanoTime() < deadline) {
                scheduler.runCurrent()
                Thread.sleep(2)
            }
            scheduler.runCurrent()
            try {
                assertTrue("Owned ViewModel work completed", model.viewModelScope.coroutineContext[Job]?.isCompleted != false)
                runBlocking { withTimeout(5_000) { repository.close() } }
            } finally {
                preferences.edit().clear().commit()
                Dispatchers.resetMain()
            }
        }
    }

    @Test fun aFreshInstallStartsLightAndDoesNotCreateAnApiOrNetworkRequest() = verifyStartup(null, ThemeMode.LIGHT)
    @Test fun anExplicitDarkChoiceSurvivesStartup() = verifyStartup("DARK", ThemeMode.DARK)
    @Test fun anExplicitSystemChoiceSurvivesStartup() = verifyStartup("SYSTEM", ThemeMode.SYSTEM)
    @Test fun anInvalidThemeFallsBackToLightWithoutRewritingTheSavedPreference() = verifyStartup("invalid-fixture", ThemeMode.LIGHT)
}
