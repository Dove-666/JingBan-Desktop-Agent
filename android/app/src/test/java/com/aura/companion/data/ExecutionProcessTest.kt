package com.aura.companion.data

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class ExecutionProcessTest {
    private val start = "2026-11-01T10:00:00.123456+00:00"
    private fun event(index: Int = 0, label: String = "检查磁盘占用", elapsed: Long = 100) = JSONObject()
        .put("id", "step-$index").put("kind", "tool").put("label", label)
        .put("status", "completed").put("elapsed_ms", elapsed)
    private fun trace(state: String = "completed") = JSONObject().put("version", 1).put("state", state)
        .put("started_at", start).put("duration_ms", 1200L).put("omitted_count", 0)
        .put("events", JSONArray().put(event()))

    @Test fun canonicalTraceRetainsPublicEventsAndRemovesReasoningArgumentsAndUnknownLabels() {
        val source = trace().put("reasoning", "PRIVATE_MODEL_THOUGHTS").put("context", "PRIVATE_SYSTEM_PROMPT")
        source.getJSONArray("events").getJSONObject(0).put("detail", "PRIVATE_API_KEY").put("arguments", "PRIVATE_PATH")
        source.getJSONArray("events").put(event(1, "PRIVATE_MODEL_THOUGHTS"))
            .put(event(2).put("id", "PRIVATE_PROVIDER_KEY"))
        val parsed = parseExecutionProcess(source)!!
        assertEquals(1, parsed.events.size)
        assertEquals("检查磁盘占用", parsed.events.single().label)
        val saved = parsed.canonicalJson()
        assertFalse(saved.contains("PRIVATE")); assertFalse(saved.contains("reasoning"))
        assertEquals(parsed, parseExecutionProcess(saved))
    }

    @Test fun delegationSourcesRetainDirectAndModelLabelsAndFilterUnknownPrivateFields() {
        val labels = listOf("任务分派说明（模型公开回复）", "任务分派说明（直接请求）")
        val input = trace().put("events", JSONArray((labels + "PRIVATE_UNKNOWN_LABEL").mapIndexed { index, label ->
            event(index, label).put("kind", "summary").put("detail", "PRIVATE_DETAIL")
                .put("arguments", "PRIVATE_ARGUMENTS").put("reasoning", "PRIVATE_THOUGHTS")
        }))
        val parsed = parseExecutionProcess(input)!!
        assertEquals(labels, parsed.events.map { it.label })
        assertEquals(listOf("step-0", "step-1"), parsed.events.map { it.id })
        assertTrue(parsed.events.all { it.kind == "summary" && it.status == "completed" })
        assertFalse(parsed.canonicalJson().contains("PRIVATE"))
        assertEquals(parsed, parseExecutionProcess(parsed.canonicalJson()))
    }

    @Test fun harnessToolEventsSurviveSyncProjectionAndStoredRoundTrip() {
        val labels = listOf("编辑文件", "搜索文件内容", "执行命令", "查看图片", "查看工作目录", "交付文件",
            "分派任务", "协调子任务", "查看子任务", "等待子任务", "停止子任务", "操作电脑", "使用任务工具", "阅读网页")
        val input = trace().put("events", JSONArray(labels.mapIndexed { index, label ->
            event(index, label).put("arguments", "PRIVATE_ARGUMENTS").put("reasoning", "PRIVATE_THOUGHTS")
        }))
        val parsed = parseExecutionProcess(input)!!
        assertEquals(labels, parsed.events.map { it.label })
        assertEquals(parsed, parseExecutionProcess(parsed.canonicalJson()))
        assertFalse(parsed.canonicalJson().contains("PRIVATE"))
        assertEquals("provider_unavailable", validAgentErrorCode("provider_unavailable"))
        assertEquals("session_unavailable", validAgentErrorCode("session_unavailable"))
    }

    @Test fun malformedTimesStatesAndOversizedTracesAreRejectedWithoutCoercion() {
        for (value in listOf(-1L, MAX_EXECUTION_MS + 1, "1200", true, 1.5))
            assertNull(parseExecutionProcess(trace().put("duration_ms", value)))
        for (value in listOf("running_private", "", true)) assertNull(parseExecutionProcess(trace().put("state", value)))
        for (value in listOf("2026-11-01T10:00:00", "2026-02-30T10:00:00Z", "2026-11-01T10:00:00.123456789Z"))
            assertNull(parseExecutionProcess(trace().put("started_at", value)))
        assertNull(parseExecutionProcess(trace().put("version", 2)))
        assertNull(parseExecutionProcess(trace().put("omitted_count", "3")))
        assertNull(parseExecutionProcess(trace().put("events", JSONArray((0..80).map { event(it) }))))
        assertNull(parseExecutionProcess(trace().put("unexpected", "X".repeat(64000))))
    }

    @Test fun eventBoundsAndDuplicatesCannotInventOperationHistory() {
        val input = trace().put("events", JSONArray().put(event()).put(event())
            .put(event(1, elapsed = 1201)).put(event(2).put("kind", "reasoning"))
            .put(event(3).put("elapsed_ms", "50")).put(event(4, "回复已完成", 1200).put("kind", "summary")))
        val parsed = parseExecutionProcess(input)!!
        assertEquals(listOf("step-0", "step-4"), parsed.events.map { it.id })
        assertEquals(0, parsed.omittedCount)
    }

    @Test fun snapshotPullAndReopenPersistOnlyCanonicalProcessData() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "process-${UUID.randomUUID()}.db"
        var db = CompanionDatabase(context, name)
        val cid = UUID.randomUUID().toString(); val mid = UUID.randomUUID().toString()
        fun item(process: JSONObject) = JSONObject().put("entity", "message").put("id", mid).put("conversation_id", cid)
            .put("data", JSONObject().put("id", mid).put("conversation_id", cid).put("role", "user").put("source", "user")
                .put("content", "Inspect only").put("created_at", start).put("request_status", "pending")
                .put("execution_mode", "agent").put("agent_trace", process))
        val conversation = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid)
            .put("data", JSONObject().put("id", cid).put("title", "QA").put("created_at", start).put("updated_at", start))
        try {
            val running = trace("running").put("reasoning", "PRIVATE_RAW_THOUGHTS")
            db.stageSnapshotPage(JSONObject().put("items", JSONArray().put(conversation).put(item(running)))
                .put("watermark", 3).put("has_more", false), true)
            assertEquals("running", db.messages(cid).single().execution!!.state)
            val complete = item(trace()).put("seq", 4)
            complete.getJSONObject("data").put("request_status", "complete")
            db.applyPull(JSONObject().put("changes", JSONArray().put(complete)).put("next_since", 4).put("has_more", false))
            val expected = db.messages(cid).single().execution!!
            assertEquals("completed", expected.state)
            db.close(); db = CompanionDatabase(context, name)
            assertEquals(expected, db.messages(cid).single().execution)
            val stored = db.readableDatabase.rawQuery("SELECT agent_trace FROM messages", null).use { it.moveToFirst(); it.getString(0) }
            assertFalse(stored.contains("PRIVATE"))
            val assistant = item(trace()).put("seq", 5)
            assistant.getJSONObject("data").put("role", "assistant")
            db.applyPull(JSONObject().put("changes", JSONArray().put(assistant)).put("next_since", 5).put("has_more", false))
            assertNull(db.messages(cid).single().execution)
        } finally { db.close(); context.deleteDatabase(name) }
    }

    @Test fun localPhoneProcessSurvivesImportAndOlderDesktopSnapshotWithoutTrace() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        CompanionDatabase(context, null).use { db ->
            db.saveDevice(DeviceInfo("QA", "desktop", "ticket"), "phone")
            val cid = db.createConversation()
            val turn = db.beginDirectTurn(cid, "Hello", null)
            assertEquals("running", db.messages(cid).single().execution!!.state)
            db.finishDirectTurn(turn.id, "Actual reply")
            val before = db.messages(cid).first { it.role == "user" }.execution!!
            assertEquals("completed", before.state)
            assertEquals(listOf("正在请求模型…", "回复已完成"), before.events.map { it.label })
            db.prepareDirectImports()
            val imported = db.directTurns().single()
            db.acceptDirectImport(imported.importRequestId!!, JSONObject().put("imported", true)
                .put("conversation_id", cid).put("message_ids", JSONArray().put(turn.userId).put(turn.assistantId)))
            assertEquals(before, db.messages(cid).first { it.role == "user" }.execution)
            val conversation = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid)
                .put("data", JSONObject().put("id", cid).put("title", "QA").put("created_at", turn.userAt).put("updated_at", turn.userAt))
            fun mirror(id: String, role: String, content: String) = JSONObject().put("entity", "message").put("id", id)
                .put("conversation_id", cid).put("data", JSONObject().put("role", role).put("content", content)
                    .put("created_at", turn.userAt).put("request_status", "complete").put("reply_to", if (role == "assistant") turn.userId else null))
            db.stageSnapshotPage(JSONObject().put("items", JSONArray().put(conversation)
                .put(mirror(turn.userId, "user", turn.text)).put(mirror(turn.assistantId, "assistant", "Actual reply")))
                .put("watermark", 20).put("has_more", false), true)
            assertEquals(before, db.messages(cid).first { it.role == "user" }.execution)
            assertEquals(2, db.messages(cid).size)
        }
    }

    @Test fun interruptedAndFailedPhoneRequestsPersistTerminalProcessWithoutReplaying() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        CompanionDatabase(context, null).use { db ->
            val cid = db.createConversation()
            val first = db.beginDirectTurn(cid, "Interrupted", null, allowUnpaired = true)
            db.recoverInterruptedDirectTurns()
            assertEquals("unknown", db.directTurns().single().state)
            assertEquals("interrupted", db.messages(cid).single().execution!!.state)
            assertEquals("上次请求意外中断", db.messages(cid).single().execution!!.events.last().label)
            val second = db.beginDirectTurn(cid, "Failed", null, allowUnpaired = true)
            db.failDirectTurn(second.id, "Safe failure", unknown = false)
            assertEquals("failed", db.messages(cid).first { it.id == second.userId }.execution!!.state)
            assertEquals(2, db.directTurns().size)
            assertTrue(db.messages(cid).none { it.role == "assistant" })
            assertNotEquals(first.userId, second.userId)
        }
    }
}
