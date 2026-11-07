package com.aura.companion.ui

import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aura.companion.data.CompanionDatabase
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/** Actual SQLite→public process→Compose rendering, entirely synthetic/debug. */
@RunWith(AndroidJUnit4::class)
class RegisteredAgentProgressUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun registeredToolLabelsRenderFromSyncedCanonicalProcess() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        require(context.packageName.endsWith(".debug"))
        val labels = listOf("分工阅读资料", "查看文档页面", "计算项目产物")
        val cid = UUID.randomUUID().toString(); val mid = UUID.randomUUID().toString()
        val at = "2026-11-03T10:00:00Z"
        val name = "qa-registered-progress-${UUID.randomUUID()}.db"
        val trace = JSONObject().put("version", 1).put("state", "completed").put("started_at", at)
            .put("duration_ms", 3000).put("events", JSONArray().also { events ->
                (labels + "PRIVATE_UNKNOWN_SECRET").forEachIndexed { index, label ->
                    events.put(JSONObject().put("id", "step-$index").put("kind", "tool")
                        .put("label", label).put("status", "completed").put("elapsed_ms", index * 1000)
                        .put("arguments", "PRIVATE_ARGUMENTS"))
                }
            }).put("reasoning", "PRIVATE_REASONING")
        val process = try {
            CompanionDatabase(context, name).use { db ->
                val conversation = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid)
                    .put("data", JSONObject().put("id", cid).put("title", "新工具进度").put("created_at", at).put("updated_at", at))
                val message = JSONObject().put("entity", "message").put("id", mid).put("conversation_id", cid)
                    .put("data", JSONObject().put("id", mid).put("conversation_id", cid).put("role", "user")
                        .put("source", "user").put("content", "合成进度验证").put("created_at", at)
                        .put("request_status", "complete").put("execution_mode", "agent").put("agent_trace", trace))
                db.stageSnapshotPage(JSONObject().put("items", JSONArray().put(conversation).put(message))
                    .put("watermark", 2).put("has_more", false), true)
                db.messages(cid).single().execution!!
            }
        } finally { context.deleteDatabase(name) }
        assertEquals(labels, process.events.map { it.label })
        val ui = UiExecution(process.state, process.startedAt, process.durationMs,
            process.events.map { UiExecutionStep(it.id, it.kind, it.label, it.status, it.elapsedMs) })
        compose.setContent { AuraTheme(ThemeMode.LIGHT) { ExecutionProcess(ui) } }
        compose.onNodeWithTag("execution-events").assertDoesNotExist()
        compose.onNodeWithTag("execution-toggle").performClick()
        labels.forEach { compose.onNodeWithText(it).assertIsDisplayed() }
        compose.onNodeWithText("PRIVATE_UNKNOWN_SECRET").assertDoesNotExist()
        compose.onNodeWithText("PRIVATE_ARGUMENTS").assertDoesNotExist()
        val output = File(context.filesDir, "qa/registered-tool-progress-1.13.png")
        output.parentFile!!.mkdirs()
        val screenshot = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        output.outputStream().use { screenshot.compress(Bitmap.CompressFormat.PNG, 100, it) }
        screenshot.recycle()
    }
}
