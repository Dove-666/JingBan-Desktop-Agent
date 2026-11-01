package com.aura.companion.transport

import android.content.Context
import android.util.Base64
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aura.companion.data.AppRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.security.MessageDigest
import java.util.UUID

/** Real Android Keystore and optional real QUIC; no mocked native endpoints. */
@RunWith(AndroidJUnit4::class)
class IrohClientInstrumentedTest {
    @Test fun encryptedIdentitySurvivesNewStoreInstance() = runBlocking(Dispatchers.IO) {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val first = DeviceIdentityStore(context).loadOrCreate()
        val second = DeviceIdentityStore(context).loadOrCreate()
        try {
            assertTrue("Device identity length was invalid", first.size == 32)
            // Assert booleans so a failed test never dumps private key bytes.
            assertTrue("Device identity changed", MessageDigest.isEqual(first, second))
            val preferences = context.getSharedPreferences(DeviceIdentityStore.PREFERENCES, Context.MODE_PRIVATE)
            val stored = preferences.getString("encrypted_secret_v1", null)
            assertTrue("Encrypted identity is missing", !stored.isNullOrBlank())
            assertTrue("A plaintext key was persisted",
                stored != Base64.encodeToString(first, Base64.NO_WRAP))
        } finally {
            first.fill(0)
            second.fill(0)
        }
    }

    /**
     * Prefer the fixture's app-private aura-e2e-config.json: only an isolated
     * MOCK fixture may supply its temporary pairing URI. No pairing secret is
     * accepted in instrumentation args. The optional legacy aura_ticket argument
     * still supports a controlled wire host that already permits status.
     */
    @Test fun realDesktopRoundTripAndReconnectUseStableIdentity() = runBlocking(Dispatchers.IO) {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val argumentTicket = InstrumentationRegistry.getArguments().getString("aura_ticket")?.takeIf { it.isNotBlank() }
        val pairing = if (argumentTicket == null) {
            val file = context.filesDir.resolve("aura-e2e-config.json")
            val configuration = runCatching {
                if (!file.isFile || file.length() > 65536) null else JSONObject(file.readText())
            }.getOrNull()
            assumeTrue("An isolated mock fixture was not supplied",
                configuration != null && configuration.optBoolean("isolated_fixture") && configuration.optBoolean("mock_chat_model"))
            val parsed = runCatching { AppRepository.parsePairing(configuration!!.getString("pairing_uri")) }.getOrNull()
            assumeTrue("An isolated mock pairing configuration was not supplied", parsed != null)
            parsed
        } else null
        val ticket = argumentTicket ?: pairing!!.getString("ticket")
        val client = IrohRpcClient(context)
        try {
            var started = System.nanoTime()
            client.connect(ticket)
            val coldConnectMillis = (System.nanoTime() - started) / 1_000_000
            val firstMetrics = client.connectionMetrics()
            val firstId = client.endpointId
            assertTrue("Endpoint identity was invalid", firstId.matches(Regex("[0-9a-f]{64}")))
            if (pairing != null) {
                val authorized = client.request("pair", JSONObject()
                    .put("code", pairing.getString("code")).put("name", "Transport performance QA"))
                assertTrue("Isolated mock fixture pairing failed", authorized.optBoolean("ok"))
            }
            started = System.nanoTime()
            // Repeated callers must share the authenticated live connection,
            // not cancel each other's streams or build new native endpoints.
            coroutineScope {
                List(3) { async { client.connect("  $ticket  ") } }.awaitAll()
            }
            val warmConnectMillis = (System.nanoTime() - started) / 1_000_000
            val warmMetrics = client.connectionMetrics()
            assertEquals(firstMetrics.endpointBinds, warmMetrics.endpointBinds)
            assertEquals(firstMetrics.reusedConnections + 3, warmMetrics.reusedConnections)
            val requestId = UUID.randomUUID().toString()
            val response = client.request("status", JSONObject(), requestId)
            assertTrue("Desktop rejected the controlled wire test", response.optBoolean("ok"))
            assertEquals(requestId, response.getString("id"))
            client.close()
            assertTrue("Closed endpoint identity was retained", client.endpointId.isEmpty())
            assertEquals("unknown", client.connectionType)
            started = System.nanoTime()
            client.connect(ticket)
            val reconnectMillis = (System.nanoTime() - started) / 1_000_000
            val reopenedMetrics = client.connectionMetrics()
            assertEquals(warmMetrics.endpointBinds + 1, reopenedMetrics.endpointBinds)
            assertTrue("Identity changed across reconnect", firstId == client.endpointId)
            assertTrue("Reconnect failed", client.request("status").optBoolean("ok"))
            assertTrue("Connection diagnostics were invalid", client.connectionType in setOf("direct", "relay", "unknown"))
            // Counters and elapsed time only. No ticket, peer, request UUID,
            // response or private identity is written into this QA artifact.
            context.filesDir.resolve("iroh-connect-performance.json").writeText(JSONObject()
                .put("cold_connect_ms", coldConnectMillis)
                .put("three_reused_connects_ms", warmConnectMillis)
                .put("closed_then_connect_ms", reconnectMillis)
                .put("endpoint_binds", reopenedMetrics.endpointBinds)
                .put("reused_connections", reopenedMetrics.reusedConnections).toString())
        } finally {
            client.close()
        }
    }
}
