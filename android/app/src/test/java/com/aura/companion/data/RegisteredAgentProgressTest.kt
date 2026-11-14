package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.UUID

/** Real canonicalization/SQLite roundtrip; synthetic labels, no host or model. */
@RunWith(RobolectricTestRunner::class)
class RegisteredAgentProgressTest {
    private val labels = listOf("分工阅读资料", "查看文档页面", "计算项目产物")
    private val at = "2026-11-03T10:00:00Z"
    private fun trace() = JSONObject().put("version", 1).put("state", "completed")
        .put("started_at", at).put("duration_ms", 3000).put("omitted_count", 0)
        .put("reasoning", "PRIVATE_REASONING").put("events", JSONArray().also { events ->
            (labels + "PRIVATE_UNKNOWN_SECRET_LABEL").forEachIndexed { index, label ->
                events.put(JSONObject().put("id", "step-$index").put("kind", "tool").put("label", label)
                    .put("status", "completed").put("elapsed_ms", index * 1000)
                    .put("arguments", "PRIVATE_TOOL_ARGUMENTS").put("detail", "PRIVATE_PROVIDER_SECRET"))
            }
        })

    @Test fun registeredProgressSurvivesSyncAndReopen() {
        val parsed = parseExecutionProcess(trace())!!
        assertEquals(labels, parsed.events.map { it.label })
        assertFalse(parsed.canonicalJson().contains("PRIVATE"))
        assertNull(validAgentActivity("计算项目产物 PRIVATE_KEY"))
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "progress-${UUID.randomUUID()}.db"
        val cid = UUID.randomUUID().toString(); val mid = UUID.randomUUID().toString()
        var db = CompanionDatabase(context, name)
        fun message(process: JSONObject, error: String) = JSONObject().put("entity", "message")
            .put("id", mid).put("conversation_id", cid).put("data", JSONObject().put("id", mid)
                .put("conversation_id", cid).put("role", "user").put("source", "user")
                .put("content", "Synthetic registered tool progress").put("created_at", at)
                .put("request_status", "failed").put("execution_mode", "agent")
                .put("agent_activity", labels.last()).put("agent_trace", process)
                .put("agent_error_code", error).put("agent_error", "PRIVATE_RAW_NATIVE_EXCEPTION"))
        try {
            val conversation = JSONObject().put("entity", "conversation").put("id", cid)
                .put("conversation_id", cid).put("data", JSONObject().put("id", cid)
                    .put("title", "工具进度兼容验证").put("created_at", at).put("updated_at", at))
            db.stageSnapshotPage(JSONObject().put("items", JSONArray().put(conversation)
                .put(message(trace(), "execution_runtime_invalid")))
                .put("watermark", 2).put("has_more", false), true)
            assertEquals(labels, db.messages(cid).single().execution!!.events.map { it.label })
            assertEquals("execution_runtime_invalid", db.messages(cid).single().agentErrorCode)
            val update = message(trace(), "execution_runtime_changed").put("seq", 3)
            db.applyPull(JSONObject().put("changes", JSONArray().put(update)).put("next_since", 3)
                .put("has_more", false))
            db.close(); db = CompanionDatabase(context, name)
            val saved = db.messages(cid).single()
            assertEquals(labels, saved.execution!!.events.map { it.label })
            assertEquals(labels.last(), saved.agentActivity)
            assertEquals("execution_runtime_changed", saved.agentErrorCode)
            assertEquals(AGENT_ERROR_MESSAGES["execution_runtime_changed"], saved.agentError)
            db.readableDatabase.rawQuery("SELECT agent_trace,agent_error_code,agent_activity FROM messages", null).use {
                assertTrue(it.moveToFirst())
                assertFalse((0..2).joinToString { column -> it.getString(column) ?: "" }.contains("PRIVATE"))
            }
        } finally { db.close(); context.deleteDatabase(name) }
    }

    @Test fun runtimeErrorsUseOnlyFixedHostCodesAndNeverRawExceptionText() {
        for (code in listOf("execution_runtime_invalid", "execution_runtime_changed", "stop_unconfirmed")) {
            assertEquals(code, validAgentErrorCode(code))
            val message = AGENT_ERROR_MESSAGES.getValue(code)
            assertTrue(message.contains("电脑") || message.contains("环境"))
            assertFalse(message.contains("PRIVATE"))
            assertNull(validAgentErrorCode("$code PRIVATE_NATIVE_EXCEPTION"))
        }
        assertNull(validAgentErrorCode("PRIVATE_UNKNOWN_ERROR"))
    }
}
