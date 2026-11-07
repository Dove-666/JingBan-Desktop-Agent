package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
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
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ContextSyncRepositoryTest {
    @Test fun advertisedContextIsCachedAndUsedByOfflinePhoneWithoutComputerOrModelReplay() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val db = CompanionDatabase(context, null)
        val cid = UUID.randomUUID().toString(); val firstId = UUID.randomUUID().toString()
        db.saveDevice(DeviceInfo("synthetic pc", "test-peer", "public-ticket"), "phone")
        db.stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""), true)
        db.createConversation(cid)
        db.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
            arrayOf(firstId, cid, "user", "Original archive", "2026-11-02T00:00:00Z", "user"))
        val unsent = db.queueMessage(cid, "older unsent draft")
        db.writableDatabase.execSQL("UPDATE outbox SET created_at=? WHERE id=?", arrayOf("2000-01-01T00:00:00Z", unsent))
        assertEquals(firstId, db.conversationGeneration(cid))
        db.updateOutbox(unsent, "discarded")
        val envelope = ContextEnvelope.local(cid, db.messages(cid, limit = null), "").json.put("memory_status", "available")
            .put("memories", JSONArray().put(JSONObject().put("id", "shared-preference").put("text", "Shared user preference: quiet seaside")
                .put("source_ids", JSONArray().put(firstId)).put("updated_at", "2026-11-02")))
        var contextCalls = 0; var allRpcCalls = 0
        val rpc = object : RpcGateway {
            override val endpointId = "phone"
            override val connectionType = "direct"
            override suspend fun connect(ticket: String) = Unit
            override suspend fun close() = Unit
            override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
                allRpcCalls++
                val result = when (method) {
                    "status" -> JSONObject().put("protocol_version", 1).put("capabilities", JSONArray().put("context.get"))
                    "sync.pull" -> JSONObject().put("changes", JSONArray()).put("next_since", 0).put("has_more", false)
                    "image.list" -> JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0)
                    "context.get" -> { contextCalls++; assertEquals(cid, params.getString("conversation_id"))
                        assertEquals(firstId, params.getString("generation")); envelope }
                    else -> error("Unexpected isolated RPC $method")
                }
                return JSONObject().put("ok", true).put("result", result)
            }
        }
        val config = DirectApiConfig(LOCAL_PHONE_AUTHORITY, "local", "https://test.invalid/v1", "synthetic-model",
            "SYNTHETIC_SECRET", "test prompt", 30, "default", 0, ApiConfigSource.LOCAL)
        val vault = object : ApiConfigVault {
            override fun load() = config
            override fun save(config: DirectApiConfig) = Unit
            override fun clear() = Unit
        }
        var modelCalls = 0; var sentMessages = JSONArray()
        val model = object : DirectChatGateway {
            override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String {
                modelCalls++; sentMessages = messages; return "synthetic complete response"
            }
        }
        val repo = AppRepository(context, rpc, db, vault, model)
        try {
            repo.reconnect(); assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals(1, contextCalls)
            repo.disconnect(); val callsBefore = allRpcCalls
            repo.sendMessage(cid, "Plan a trip")
            withTimeout(3000) { while (repo.state.value.directChatBusy) delay(10) }
            assertEquals(1, modelCalls); assertEquals(callsBefore, allRpcCalls)
            assertTrue(sentMessages.toString().contains("Shared user preference"))
            assertTrue(sentMessages.toString().contains("reference_data_only"))
            assertFalse(sentMessages.toString().contains("SYNTHETIC_SECRET"))
            assertEquals("ready", db.directTurns().single().state)
        } finally { repo.close() }
    }
}
