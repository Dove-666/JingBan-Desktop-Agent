package com.aura.companion.ui

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.Process
import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.test.core.app.ActivityScenario
import android.view.inputmethod.InputMethodManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.io.IOException
import java.util.UUID

/** Inert owned UI fixtures. Never starts MainActivity, a repository, or a gateway. */
@RunWith(AndroidJUnit4::class)
class DraftPersistenceUiTest {
    @get:Rule val compose = createEmptyComposeRule()
    private var activity: ActivityScenario<ComponentActivity>? = null
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val state = mutableStateOf(base())

    private fun base() = UiState(theme = ThemeMode.LIGHT, selectedConversationId = "qa-persist-a",
        conversations = listOf(UiConversation("qa-persist-a", "QA draft A"), UiConversation("qa-persist-b", "QA draft B")),
        directApi = UiDirectApi(available = true, model = "owned-inert-model"))

    private fun launch(store: DraftStore, actions: UiActions = NoNetworkActions()) {
        require(context.packageName == "com.aura.companion.debug")
        val intent = Intent(context, ComponentActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        activity = ActivityScenario.launch<ComponentActivity>(intent).also { scenario -> scenario.onActivity { host ->
            host.setContent { AuraApp(state.value, actions, store) }
        } }
        compose.onNodeWithContentDescription("消息内容").assertIsDisplayed()
    }

    private fun draft() = compose.onNodeWithContentDescription("消息内容").fetchSemanticsNode()
        .config[SemanticsProperties.EditableText].text

    private fun pressBack() {
        assertTrue(InstrumentationRegistry.getInstrumentation().uiAutomation
            .performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
        InstrumentationRegistry.getInstrumentation().waitForIdleSync()
    }

    @After fun closeActivity() { activity?.close() }

    @Test fun drawerBackOnlyClosesDrawerAndNormalBackFinishRecoversDraftFromDisk() {
        val file = File(context.filesDir, "qa/draft-back-${UUID.randomUUID()}/drafts.json")
        launch(PrivateDraftStore(file))
        val text = "owned back-exit draft"
        compose.onNodeWithContentDescription("消息内容").performTextInput(text)
        val before = draft()
        assertTrue("Before Back length=${before.length}", before == text)
        activity!!.onActivity { host -> host.getSystemService(InputMethodManager::class.java)
            .hideSoftInputFromWindow(host.window.decorView.windowToken, 0) }
        InstrumentationRegistry.getInstrumentation().waitForIdleSync()
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("QA draft B").assertIsDisplayed()
        pressBack()
        compose.onNodeWithContentDescription("消息内容").assertIsDisplayed()
        assertTrue(activity!!.state == Lifecycle.State.RESUMED)
        val after = draft()
        File(file.parentFile, "back-observation.json").writeText(JSONObject().put("before_characters", before.length)
            .put("after_characters", after.length).put("equal", after == before)
            .put("disk_characters", PrivateDraftStore(file).load().entries["qa-persist-a"]?.text?.length).toString())
        assertTrue("After drawer Back length=${after.length}, before=${before.length}", after == text)
        pressBack()
        compose.waitUntil(5000) { activity!!.state == Lifecycle.State.DESTROYED }
        activity!!.close()
        launch(PrivateDraftStore(file))
        assertTrue(draft() == text)
    }

    @Test fun rejectedPhoneSubmissionAndLaterAcceptanceDoNotEraseANewerDraft() {
        val file = File(context.filesDir, "qa/draft-phone-${UUID.randomUUID()}/drafts.json")
        var callback: ((Boolean) -> Unit)? = null
        val actions = object : NoNetworkActions() {
            override fun sendPhoneMessage(text: String, onAccepted: (Boolean) -> Unit) { callback = onAccepted }
        }
        launch(PrivateDraftStore(file), actions)
        compose.onNodeWithContentDescription("消息内容").performTextInput("first owned draft")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.waitUntil(5000) { callback != null }
        compose.runOnIdle { requireNotNull(callback).invoke(false) }
        assertTrue(draft() == "first owned draft")
        callback = null
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.waitUntil(5000) { callback != null }
        compose.onNodeWithContentDescription("消息内容").performTextReplacement("new owned draft")
        compose.runOnIdle { requireNotNull(callback).invoke(true) }
        assertTrue(draft() == "new owned draft")
    }

    @Test fun aLegacyVoidActionCannotAcknowledgeOrEraseTheDraft() {
        val file = File(context.filesDir, "qa/draft-unacknowledged-${UUID.randomUUID()}/drafts.json")
        var calls = 0
        val actions = object : NoNetworkActions() { override fun sendMessage(text: String) { calls++ } }
        launch(PrivateDraftStore(file), actions)
        compose.onNodeWithContentDescription("消息内容").performTextInput("owned unacknowledged draft")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.waitUntil(5000) { calls == 1 }
        compose.runOnIdle { assertEquals(1, calls) }
        assertTrue(draft() == "owned unacknowledged draft")
    }

    @Test fun storageFailureKeepsFullTextAndDisablesSubmission() {
        val broken = object : DraftStore {
            override val changes = MutableStateFlow(0L)
            override fun load() = PersistedDrafts()
            override fun update(change: (PersistedDrafts) -> PersistedDrafts): PersistedDrafts = throw IOException("owned test")
        }
        launch(broken)
        val complete = "owned unsaved material".repeat(450)
        compose.onNodeWithContentDescription("消息内容").performTextReplacement(complete)
        assertTrue(draft() == complete)
        compose.waitUntil(5000) { compose.onAllNodesWithTag("draft-storage-error").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("draft-storage-error").assertIsDisplayed()
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
    }

    @Test fun privateSnapshotWriteTimingUsesCompleteLongDrafts() {
        val file = File(context.filesDir, "qa/draft-timing-${UUID.randomUUID()}/drafts.json")
        val store = PrivateDraftStore(file)
        val profiles = org.json.JSONArray()
        val owners = ViewModelStore()
        lateinit var drafts: ConversationDrafts
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            val factory = object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T = ConversationDrafts(store) as T
            }
            drafts = ViewModelProvider(owners, factory)[ConversationDrafts::class.java]
            drafts.edit("qa-other", "other".repeat(100))
        }
        try {
            for ((label, complete) in listOf("normal_1000" to "资料".repeat(500), "oversized_10500" to "合成资料\n".repeat(2100))) {
              val elapsed = mutableListOf<Long>()
              InstrumentationRegistry.getInstrumentation().runOnMainSync {
                repeat(12) { index ->
                    val start = System.nanoTime()
                    drafts.edit("qa-large-a", complete + index)
                    elapsed += (System.nanoTime() - start) / 1_000_000
                }
              }
              compose.waitUntil(5000) { store.load().entries["qa-large-a"]?.text == complete + "11" }
              profiles.put(JSONObject().put("profile", label).put("ui_edits", elapsed.size)
                    .put("draft_characters", complete.length + 2).put("average_ms", elapsed.average()).put("maximum_ms", elapsed.maxOrNull()))
            }
        } finally { InstrumentationRegistry.getInstrumentation().runOnMainSync { owners.clear() } }
        val value = JSONObject().put("profiles", profiles).put("main_thread", true).put("other_cids", 1).put("real_api_calls", 0)
        File(context.filesDir, "qa/draft-write-timing.json").writeText(value.toString())
    }

    @Test fun largePersistedPasteKeepsControlsResponsiveAndCanReturnToANormalDraft() {
        val file = File(context.filesDir, "qa/draft-large-ui-${UUID.randomUUID()}/drafts.json")
        launch(PrivateDraftStore(file))
        val complete = "合成素材".repeat(2100)
        compose.onNodeWithContentDescription("消息内容").performTextReplacement(complete)
        assertTrue(draft() == complete)
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        pressBack()
        assertTrue(draft() == complete)
        compose.onNodeWithContentDescription("消息内容").performTextReplacement("normal owned draft")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        assertTrue(draft() == "normal owned draft")
        compose.waitUntil(5000) { PrivateDraftStore(file).load().entries["qa-persist-a"]?.text == "normal owned draft" }
        compose.waitForIdle()
        val screenshot = requireNotNull(InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot())
        try { File(context.filesDir, "qa/draft-ui-responsive.png").outputStream().use { screenshot.compress(Bitmap.CompressFormat.PNG, 100, it) } }
        finally { screenshot.recycle() }
    }

    private fun processCase(): Pair<File, String> {
        val run = InstrumentationRegistry.getArguments().getString("draft_case")
            ?: error("Explicit paired process-death phase requires draft_case")
        require(run.matches(Regex("[A-Za-z0-9_-]+")))
        return File(context.filesDir, "qa/draft-process/$run/drafts.json") to run
    }

    /** Host must kill the package after this phase and start a new instrumentation process. */
    @Test fun seedOwnedDraftsBeforeActualProcessTermination() {
        val (file, run) = processCase(); check(!file.exists())
        val actions = object : NoNetworkActions() {
            override fun selectConversation(id: String) { state.value = state.value.copy(selectedConversationId = id) }
        }
        launch(PrivateDraftStore(file), actions)
        compose.onNodeWithContentDescription("消息内容").performTextReplacement("owned-A-".repeat(1100))
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("QA draft B").performClick()
        compose.onNodeWithContentDescription("消息内容").performTextInput("owned-B-unsent")
        compose.runOnIdle { state.value = state.value.copy(selectedConversationId = null) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("owned-null-unsent")
        compose.waitUntil(5000) {
            val saved = PrivateDraftStore(file).load().entries
            saved["qa-persist-a"]?.text == "owned-A-".repeat(1100) &&
                saved["qa-persist-b"]?.text == "owned-B-unsent" && saved[null]?.text == "owned-null-unsent"
        }
        File(file.parentFile, "seed.json").writeText(JSONObject().put("process_id", Process.myPid())
            .put("run", run).put("saved_to_private_disk", file.isFile).put("real_api_calls", 0).toString())
    }

    @Test fun restoreOwnedDraftsAfterActualProcessTermination() {
        val (file, run) = processCase(); check(file.exists())
        val seed = JSONObject(File(file.parentFile, "seed.json").readText())
        check(seed.getInt("process_id") != Process.myPid()) { "A new Android process is required" }
        val actions = object : NoNetworkActions() {
            override fun selectConversation(id: String) { state.value = state.value.copy(selectedConversationId = id) }
        }
        launch(PrivateDraftStore(file), actions)
        compose.waitUntil(5000) { draft() == "owned-A-".repeat(1100) }
        assertTrue(draft() == "owned-A-".repeat(1100))
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("QA draft B").performClick()
        assertTrue(draft() == "owned-B-unsent")
        compose.runOnIdle { state.value = state.value.copy(selectedConversationId = null) }
        assertTrue(draft() == "owned-null-unsent")
        File(file.parentFile, "restore.json").writeText(JSONObject().put("process_id", Process.myPid())
            .put("seed_process_id", seed.getInt("process_id")).put("run", run).put("all_three_drafts_exact", true)
            .put("fresh_process_verified", true).put("saved_bundle_injected", false).put("real_api_calls", 0).toString())
    }
}

private open class NoNetworkActions : UiActions {
    private fun forbidden(): Unit = error("No gateway operation is allowed in this owned UI fixture")
    override fun pair(payload: String) = forbidden()
    override fun consumePairInfo() = Unit
    override fun reconnect() = forbidden()
    override fun disconnect() = Unit
    override fun syncNow() = forbidden()
    override fun selectConversation(id: String) = Unit
    override fun newConversation() = Unit
    override fun sendMessage(text: String) = forbidden()
    override fun retryMessage(id: String) = forbidden()
    override fun discardOutbox(requestId: String) = Unit
    override fun pickImage() = Unit
    override fun removePendingImage() = Unit
    override fun petCommand(action: String, value: PetValue) = forbidden()
    override fun refreshImages() = Unit
    override fun openImage(id: String) = Unit
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
    override fun saveDirectApiConfig(input: UiApiConfigInput) = forbidden()
    override fun sendQueuedOnPhone(requestId: String) = forbidden()
    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) = forbidden()
    override fun requestDesktopScreenshot() = forbidden()
}
