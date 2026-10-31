package com.aura.companion.transport

import android.content.Context
import android.util.Base64
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.Dispatchers
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
     * Set instrumentation argument aura_ticket to a temporary desktop wire-test
     * endpoint ticket. No pairing secret is accepted in instrumentation args.
     * The endpoint must permit a status request for this controlled smoke.
     */
    @Test fun realDesktopRoundTripAndReconnectUseStableIdentity() = runBlocking(Dispatchers.IO) {
        val ticket = InstrumentationRegistry.getArguments().getString("aura_ticket")
        assumeTrue("Optional real desktop endpoint was not supplied", !ticket.isNullOrBlank())
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val client = IrohRpcClient(context)
        try {
            client.connect(ticket!!)
            val firstId = client.endpointId
            assertTrue("Endpoint identity was invalid", firstId.matches(Regex("[0-9a-f]{64}")))
            val requestId = UUID.randomUUID().toString()
            val response = client.request("status", JSONObject(), requestId)
            assertTrue("Desktop rejected the controlled wire test", response.optBoolean("ok"))
            assertEquals(requestId, response.getString("id"))
            client.close()
            client.connect(ticket)
            assertTrue("Identity changed across reconnect", firstId == client.endpointId)
            assertTrue("Reconnect failed", client.request("status").optBoolean("ok"))
            assertTrue("Connection diagnostics were invalid", client.connectionType in setOf("direct", "relay", "unknown"))
        } finally {
            client.close()
        }
    }
}
