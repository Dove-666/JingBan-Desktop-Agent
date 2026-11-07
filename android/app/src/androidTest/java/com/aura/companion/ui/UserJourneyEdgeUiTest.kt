package com.aura.companion.ui

import android.graphics.Bitmap
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.lifecycle.Lifecycle
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Pure owned UI states: no repository, account, clipboard, network or release data. */
@RunWith(AndroidJUnit4::class)
class UserJourneyEdgeUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Before fun isolatedDebugComponentOnly() {
        require(InstrumentationRegistry.getInstrumentation().targetContext.packageName == "com.aura.companion.debug")
        compose.runOnUiThread { configureActivity(compose.activity) }
    }

    private fun base() = UiState(paired = true, desktopName = "独立 UI 测试电脑",
        selectedConversationId = "owned-a", theme = ThemeMode.LIGHT,
        conversations = listOf(UiConversation("owned-a", "第一段合成对话"), UiConversation("owned-b", "第二段合成对话")),
        directApi = UiDirectApi(available = true, model = "synthetic-no-network"))

    @Test fun switchingConversationsRestoresEachUnsentDraft() {
        val state = mutableStateOf(base())
        val actions = object : InertActions() {
            override fun selectConversation(id: String) { state.value = state.value.copy(selectedConversationId = id) }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("A 对话尚未发送的草稿")
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("第二段合成对话").performClick()
        assertEquals("", draftText())
        compose.onNodeWithContentDescription("消息内容").performTextInput("B 对话自己的草稿")
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("第一段合成对话").performClick()
        compose.onNodeWithContentDescription("消息内容").assertTextEquals("A 对话尚未发送的草稿")
        screenshot("conversation-draft-return-a.png")
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("第二段合成对话").performClick()
        compose.onNodeWithContentDescription("消息内容").assertTextEquals("B 对话自己的草稿")
        assertEquals(0, actions.networkOperations)
    }

    @Test fun oversizedPasteIsPreservedUntilTheUserCanShortenIt() {
        val actions = InertActions()
        compose.setContent { AuraApp(base(), actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("旧草稿")
        assertEquals("旧草稿", draftText())
        val pasted = "素材内容".repeat(2100)
        compose.onNodeWithContentDescription("消息内容").performTextReplacement(pasted)
        assertTrue("Full pasted draft must remain editable: expected ${pasted.length}, actual ${draftText().length}", draftText() == pasted)
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.onNodeWithTag("draft-over-limit").assertIsDisplayed()
        screenshot("oversized-paste.png")
        compose.onNodeWithContentDescription("消息内容").performTextReplacement("缩短后可以发送")
        compose.onNodeWithTag("draft-over-limit").assertDoesNotExist()
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        assertEquals(0, actions.networkOperations)
    }

    @Test fun currentAndInactiveDraftsSurviveSavedStateRecreation() {
        val restoration = StateRestorationTester(compose)
        val state = mutableStateOf(base())
        val actions = object : InertActions() {
            override fun selectConversation(id: String) { state.value = state.value.copy(selectedConversationId = id) }
        }
        restoration.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("A 保留的草稿")
        selectConversation("第二段合成对话")
        compose.onNodeWithContentDescription("消息内容").performTextInput("B 当前草稿")
        restoration.emulateSavedInstanceStateRestore()
        assertEquals("B 当前草稿", draftText())
        selectConversation("第一段合成对话")
        assertEquals("A 保留的草稿", draftText())
        assertEquals(0, actions.networkOperations)
    }

    @Test fun actualActivityRecreationAndBackgroundKeepCurrentAndInactiveDrafts() {
        val state = mutableStateOf(base())
        val actions = object : InertActions() {
            override fun selectConversation(id: String) { state.value = state.value.copy(selectedConversationId = id) }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("A 旋转后仍保留")
        selectConversation("第二段合成对话")
        compose.onNodeWithContentDescription("消息内容").performTextInput("B 后台恢复后保留")
        compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
        compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
        assertEquals("B 后台恢复后保留", draftText())
        compose.activityRule.scenario.recreate()
        // ComponentActivity has no app repository or onCreate content; attach the same owned fixture.
        compose.activityRule.scenario.onActivity { activity ->
            configureActivity(activity)
            activity.setContent { AuraApp(state.value, actions) }
        }
        assertEquals("B 后台恢复后保留", draftText())
        selectConversation("第一段合成对话")
        assertEquals("A 旋转后仍保留", draftText())
        assertEquals(0, actions.networkOperations)
    }

    @Test fun lateDesktopAcceptanceClearsOnlyTheUnchangedSubmittedVersion() {
        val state = mutableStateOf(base().copy(desktopAgentMode = true, desktopTaskAvailable = true))
        var callback: ((Boolean) -> Unit)? = null
        val actions = object : InertActions() {
            override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { callback = onAccepted }
            override fun selectConversation(id: String) { state.value = state.value.copy(selectedConversationId = id) }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("A 已提交任务")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.onNodeWithContentDescription("消息内容").performTextReplacement("A 新编辑")
        compose.onNodeWithContentDescription("消息内容").performTextReplacement("A 已提交任务")
        selectConversation("第二段合成对话")
        compose.onNodeWithContentDescription("消息内容").performTextInput("B 不受影响")
        compose.runOnIdle { requireNotNull(callback).invoke(true) }
        assertEquals("B 不受影响", draftText())
        selectConversation("第一段合成对话")
        assertEquals("A 已提交任务", draftText())
        compose.onNodeWithContentDescription("发送消息").performClick()
        selectConversation("第二段合成对话")
        compose.runOnIdle { requireNotNull(callback).invoke(true) }
        selectConversation("第一段合成对话")
        assertEquals("", draftText())
        assertEquals(0, actions.networkOperations)
    }

    @Test fun pendingAcceptanceAfterSavedStateRestoreDoesNotLeaveASentDraftBehind() {
        val restoration = StateRestorationTester(compose)
        var callback: ((Boolean) -> Unit)? = null
        val actions = object : InertActions() {
            override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { callback = onAccepted }
        }
        restoration.setContent { AuraApp(base().copy(desktopAgentMode = true, desktopTaskAvailable = true), actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("等待回执的草稿")
        compose.onNodeWithContentDescription("发送消息").performClick()
        restoration.emulateSavedInstanceStateRestore()
        compose.runOnIdle { requireNotNull(callback).invoke(true) }
        assertEquals("", draftText())
        assertEquals(0, actions.networkOperations)
    }

    @Test fun firstDesktopRejectionBeforeIdIsObservedPreservesTheDraft() {
        val state = mutableStateOf(base().copy(selectedConversationId = null, conversations = emptyList(),
            desktopAgentMode = true, desktopTaskAvailable = true))
        var callback: ((Boolean) -> Unit)? = null
        val actions = object : InertActions() {
            override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { callback = onAccepted }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("首次任务拒绝后保留")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle {
            state.value = state.value.copy(selectedConversationId = "owned-created",
                conversations = listOf(UiConversation("owned-created", "创建的会话")))
            requireNotNull(callback).invoke(false)
        }
        assertEquals("首次任务拒绝后保留", draftText())
        assertEquals(0, actions.networkOperations)
    }

    @Test fun selectingAgentForAnUnassignedDraftMigratesItToTheNewConversation() {
        val state = mutableStateOf(base().copy(selectedConversationId = null, conversations = emptyList(),
            desktopTaskAvailable = true))
        val actions = object : InertActions() {
            override fun setDesktopAgentMode(enabled: Boolean) {
                state.value = state.value.copy(selectedConversationId = "owned-created", desktopAgentMode = enabled,
                    conversations = listOf(UiConversation("owned-created", "新 Agent 对话")))
            }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("切换前的草稿")
        compose.onNodeWithText("电脑 Agent").performClick()
        assertEquals("切换前的草稿", draftText())
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        assertEquals(0, actions.networkOperations)
    }

    @Test fun choosingAnImageForAnUnassignedDraftMigratesTextWithoutSending() {
        val state = mutableStateOf(base().copy(selectedConversationId = null, conversations = emptyList()))
        val actions = object : InertActions() {
            override fun pickImage() {
                state.value = state.value.copy(selectedConversationId = "owned-photo",
                    conversations = listOf(UiConversation("owned-photo", "新图文对话")))
            }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("选图前的问题")
        compose.onNodeWithContentDescription("添加图片").performClick()
        assertEquals("选图前的问题", draftText())
        assertEquals(0, actions.networkOperations)
    }

    @Test fun longArtifactAndFailureRemainUsableAtNarrowWidthAndLargeType() {
        val saved = mutableListOf<String>()
        val actions = object : InertActions() { override fun saveArtifact(reference: String) { saved += reference } }
        val reference = "artifact_" + "a".repeat(64)
        val error = "连接暂时中断，请检查电脑网络后再继续。已完成的操作会保留，不会自动重试。".repeat(4)
        val state = base().copy(connection = ConnectionState.DISCONNECTED,
            messages = listOf(UiMessage("owned-failure", MessageRole.USER, "整理这份合成资料", MessageStatus.FAILED,
                detail = error, desktopAgent = true,
                execution = UiExecution("failed", "2026-11-05T00:00:00Z", 8000,
                    (1..24).map { UiExecutionStep("owned-$it", "tool", "查看目录信息", "completed", it * 1000L) }))),
            agentArtifacts = listOf(UiAgentArtifact(reference, "很长的合成资料整理结果文件名".repeat(12) + ".docx", 4096)))
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, 1.5f)) {
                Box(Modifier.width(320.dp).fillMaxHeight()) { AuraApp(state, actions) }
            }
        }
        compose.onNodeWithContentDescription("消息内容").assertIsDisplayed()
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-artifact"))
        compose.onNodeWithText("保存").performClick()
        assertEquals(listOf(reference), saved)
        screenshot("narrow-large-type-artifact.png")
        assertEquals(0, actions.networkOperations)
    }

    @Test fun stopButtonRemainsReachableWithLongDraftAndExpandedRunningSteps() {
        var stopped = 0
        val actions = object : InertActions() { override fun cancelDesktopTask() { stopped++ } }
        val state = base().copy(connection = ConnectionState.CONNECTED,
            desktopTaskAvailable = true, desktopAgentMode = true, desktopTaskRunning = true, canSteerDesktop = true,
            messages = listOf(UiMessage("owned-running", MessageRole.USER, "整理独立测试资料", MessageStatus.PENDING,
                desktopAgent = true, execution = UiExecution("running", "2026-11-05T00:00:00Z", 2000,
                    (1..12).map { UiExecutionStep("owned-$it", "tool", "查看目录信息", if (it == 12) "running" else "completed", it * 1000L) }))))
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, 1.5f)) {
                Box(Modifier.width(320.dp).fillMaxHeight()) { AuraApp(state, actions) }
            }
        }
        compose.onNodeWithContentDescription("消息内容").performTextReplacement((1..12).joinToString("\n") { "第 $it 行待补充的内容" })
        compose.onNodeWithContentDescription("停止电脑任务").assertIsDisplayed().performClick()
        assertEquals(1, stopped)
        screenshot("narrow-large-type-running-stop.png")
        assertEquals(0, actions.networkOperations)
    }

    private fun screenshot(name: String) {
        compose.waitForIdle()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.waitForIdleSync()
        val run = InstrumentationRegistry.getArguments().getString("qa_output", "initial")!!
        require(run.matches(Regex("[a-zA-Z0-9_-]+")))
        val output = File(instrumentation.targetContext.filesDir, "qa/user-ux-20261105/$run/$name")
        output.parentFile!!.mkdirs()
        val image = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { output.outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) } }
        finally { image.recycle() }
    }

    private fun draftText() = compose.onNodeWithContentDescription("消息内容")
        .fetchSemanticsNode().config[SemanticsProperties.EditableText].text

    private fun selectConversation(title: String) {
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText(title).performClick()
    }

    private fun configureActivity(activity: ComponentActivity) {
        activity.window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
        activity.enableEdgeToEdge()
    }
}

private open class InertActions : UiActions {
    var networkOperations = 0
    override fun pair(payload: String) { networkOperations++ }
    override fun consumePairInfo() = Unit
    override fun reconnect() { networkOperations++ }
    override fun disconnect() = Unit
    override fun syncNow() { networkOperations++ }
    override fun selectConversation(id: String) = Unit
    override fun newConversation() = Unit
    override fun sendMessage(text: String) { networkOperations++ }
    override fun retryMessage(id: String) { networkOperations++ }
    override fun discardOutbox(requestId: String) = Unit
    override fun pickImage() = Unit
    override fun removePendingImage() = Unit
    override fun petCommand(action: String, value: PetValue) { networkOperations++ }
    override fun refreshImages() = Unit
    override fun openImage(id: String) = Unit
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
    override fun saveDirectApiConfig(input: UiApiConfigInput) = Unit
    override fun sendQueuedOnPhone(requestId: String) { networkOperations++ }
    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { networkOperations++;onAccepted(false) }
    override fun requestDesktopScreenshot() { networkOperations++ }
}
