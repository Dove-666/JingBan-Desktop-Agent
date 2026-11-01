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

/** A later desktop update must enrich cached history without hiding it offline. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class ExecutionProjectionRefreshTest {
    @Test fun latePcUpgradeRefreshesOnceAndKeepsCacheIfOffline() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val cid = UUID.randomUUID().toString()
        val mid = UUID.randomUUID().toString()
        val date = "2026-11-01T00:00:00Z"
        fun snapshot(trace: Boolean): JSONObject {
            val conversation = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid).put("data", JSONObject()
                .put("id", cid).put("title", "Retained history").put("created_at", date).put("updated_at", date).put("message_count", 1))
            val message = JSONObject().put("id", mid).put("conversation_id", cid).put("role", "user")
                .put("content", "Existing task").put("source", "user").put("created_at", date)
                .put("request_status", "complete").put("execution_mode", "agent")
            if (trace) message.put("agent_trace", JSONObject().put("version", 1).put("state", "completed")
                .put("started_at", date).put("duration_ms", 50).put("omitted_count", 0).put("events", JSONArray().put(
                    JSONObject().put("id", "step-0").put("kind", "tool").put("label", "检查磁盘占用")
                        .put("status", "completed").put("elapsed_ms", 50))))
            return JSONObject().put("items", JSONArray().put(conversation).put(JSONObject().put("entity", "message")
                .put("id", mid).put("conversation_id", cid).put("data", message)))
                .put("watermark", 42).put("next_cursor", "").put("has_more", false)
        }
        val database = CompanionDatabase(context, null)
        database.saveDevice(DeviceInfo("PC", "desktop", "ticket"), "phone")
        database.stageSnapshotPage(snapshot(false), true)
        database.setSetting("selected_conversation", cid)
        var version = 0
        var failSnapshot = false
        var snapshots = 0
        val rpc = object : RpcGateway {
            override val endpointId = "phone"
            override val connectionType = "direct"
            override suspend fun connect(ticket: String) = Unit
            override suspend fun close() = Unit
            override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
                val result = when (method) {
                    "status" -> JSONObject().put("protocol_version", 1).put("agent_trace_version", version)
                        .put("capabilities", JSONArray().put("agent.send")).put("agent_available", true)
                    "sync.snapshot" -> { snapshots++; if (failSnapshot) throw IOException("Controlled offline snapshot"); snapshot(true) }
                    "sync.pull" -> JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false)
                    "image.list" -> JSONObject().put("items", JSONArray()).put("next_offset", 0).put("has_more", false)
                    else -> error("Unexpected RPC: $method")
                }
                return JSONObject().put("id", requestId).put("ok", true).put("result", result)
            }
        }
        val vault = object : ApiConfigVault {
            override fun load(): DirectApiConfig? = null
            override fun save(config: DirectApiConfig) = Unit
            override fun clear() = Unit
        }
        val repository = AppRepository(context, rpc, database, vault)
        try {
            repository.reconnect()
            assertEquals(0, snapshots)
            assertNull(database.messages(cid).single().execution)
            repository.disconnect()
            version = 1; failSnapshot = true
            repository.reconnect()
            assertEquals(1, snapshots)
            assertFalse(database.initialized())
            assertEquals("Existing task", database.messages(cid).single().content)
            failSnapshot = false
            repository.reconnect()
            assertEquals(2, snapshots)
            assertTrue(database.initialized())
            assertEquals("completed", database.messages(cid).single().execution?.state)
            repository.disconnect(); repository.reconnect(); repository.syncNow()
            assertEquals("Projection refresh is once per desktop version", 2, snapshots)
        } finally { repository.close() }
    }
}
