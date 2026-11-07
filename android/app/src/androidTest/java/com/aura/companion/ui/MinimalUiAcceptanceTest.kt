package com.aura.companion.ui

import android.graphics.Bitmap
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import kotlin.math.abs

/** Owned ComponentActivity fixtures only: no MainActivity, repository, API or desktop session. */
@RunWith(AndroidJUnit4::class)
class MinimalUiAcceptanceTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Before fun ownedDebugActivityOnly() {
        require(InstrumentationRegistry.getInstrumentation().targetContext.packageName == "com.aura.companion.debug")
        compose.runOnUiThread {
            compose.activity.window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
            compose.activity.enableEdgeToEdge()
        }
    }

    private fun state(theme: ThemeMode = ThemeMode.DARK) = UiState(
        paired = true, desktopName = "我的电脑", connection = ConnectionState.CONNECTED, sync = SyncState.READY,
        theme = theme, selectedConversationId = "owned-current", desktopTaskAvailable = true,
        conversations = listOf(UiConversation("owned-current", "整理旅行计划", updatedAt = "今天"),
            UiConversation("owned-other", "另一段对话", updatedAt = "昨天")),
        directApi = UiDirectApi(available = true, keySaved = true, model = "synthetic-no-network",
            baseUrl = "https://fixture.invalid/v1"))

    @Test fun sidebarSettingsAreAtTheTopAndOpeningThemPreservesTheDraft() {
        val actions = MinimalActions()
        compose.setContent { AuraApp(state(), actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("这段草稿仍然属于当前对话")
        hideKeyboard()
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        val drawer = compose.onNodeWithTag("history-drawer").assertIsDisplayed().fetchSemanticsNode().boundsInRoot
        val settings = compose.onNodeWithTag("drawer-settings").assertIsDisplayed().fetchSemanticsNode().boundsInRoot
        assertTrue("Settings belongs in the drawer's top-right corner", settings.center.x > drawer.center.x)
        assertTrue("Settings must be reachable without scrolling history", settings.top < drawer.top + drawer.height * .2f)
        compose.onNodeWithText("桌宠控制").assertDoesNotExist()
        compose.onNodeWithText("图片收件箱").assertDoesNotExist()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        screenshot("sidebar-dark.png")
        compose.onNodeWithTag("drawer-settings").performClick()
        compose.onNodeWithTag("settings-sheet").assertIsDisplayed()
        back()
        compose.onNodeWithContentDescription("消息内容").assertTextEquals("这段草稿仍然属于当前对话")
        assertTrue(actions.sent.isEmpty())
        assertEquals(0, actions.desktopScreenshots)
    }

    @Test fun compactSettingsKeepOnlyUsefulStateAndApiEditingIsReachable() {
        val actions = MinimalActions()
        compose.setContent { AuraApp(state(ThemeMode.LIGHT).copy(
            connectionDetail = "通过加密中继连接", desktopTaskMessage = "REDUNDANT_AGENT_DETAIL",
            desktopTaskError = "REDUNDANT_AGENT_ERROR", directApi = state().directApi.copy(
                lastSyncedAt = "今天 11:30", message = "REDUNDANT_API_SYNC_DETAIL")), actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("打开设置不应清掉它")
        hideKeyboard()
        compose.onNodeWithTag("app-settings").performClick()
        compose.onNodeWithTag("settings-sheet").assertIsDisplayed()
        compose.onNodeWithText("我的电脑").assertIsDisplayed()
        compose.onNodeWithText("在线").assertIsDisplayed()
        compose.onNodeWithText("synthetic-no-network").assertIsDisplayed()
        compose.onNodeWithText("通过加密中继连接").assertDoesNotExist()
        compose.onAllNodesWithText("REDUNDANT_", substring = true).assertCountEquals(0)
        compose.onNodeWithText("配置详情").assertDoesNotExist()
        compose.onNodeWithText("密钥已安全保存").assertDoesNotExist()
        compose.onNodeWithText("配置更新时间", substring = true).assertDoesNotExist()
        compose.onNodeWithTag("pending-messages-setting").assertDoesNotExist()
        compose.onNodeWithTag("settings-sheet").assertIsDisplayed()
        screenshot("settings-light.png")
        compose.onNodeWithText("编辑").performClick()
        compose.onNode(hasSetTextAction() and hasText("API 地址")).assertIsDisplayed()
        val password = compose.onNode(hasSetTextAction() and hasContentDescription("API Key"))
            .assert(SemanticsMatcher.keyIsDefined(SemanticsProperties.Password))
        assertTrue(password.fetchSemanticsNode().config[SemanticsProperties.EditableText].text.isEmpty())
        back()
        compose.onNodeWithContentDescription("消息内容").assertTextEquals("打开设置不应清掉它")
        assertTrue(actions.sent.isEmpty())
    }

    @Test fun modeSwitchIsCompactCenteredAndRetainsAccessibleTouchTargets() {
        val value = mutableStateOf(state())
        val actions = object : MinimalActions() {
            override fun setDesktopAgentMode(enabled: Boolean) { value.value = value.value.copy(desktopAgentMode = enabled) }
        }
        compose.setContent { AuraApp(value.value, actions) }
        val modes = compose.onNodeWithTag("chat-mode-switch").assertIsDisplayed()
        val modeBounds = modes.fetchSemanticsNode().boundsInRoot
        assertTrue("Mode track should remain a compact control", modeBounds.width <=
            320f * compose.activity.resources.displayMetrics.density + 2f)
        val composerBounds = compose.onNodeWithTag("message-composer").fetchSemanticsNode().boundsInRoot
        assertTrue("Mode track must be centered over the composer", abs(modeBounds.center.x - composerBounds.center.x) <= 2f)
        compose.onNodeWithTag("phone-chat-mode").assertIsSelected().assertHeightIsAtLeast(48.dp)
        compose.onNodeWithTag("desktop-agent-mode").assertHeightIsAtLeast(48.dp).performClick()
        compose.onNodeWithTag("desktop-agent-mode").assertIsSelected()
        compose.onNodeWithTag("settings-sheet").assertDoesNotExist()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.onNodeWithContentDescription("更多选项").assertDoesNotExist()
        screenshot("conversation-dark.png")
        assertTrue(actions.sent.isEmpty())
        assertTrue(actions.desktopMessages.isEmpty())
        assertEquals(0, actions.desktopScreenshots)
    }

    @Test fun terminalStatesStayTruthfulNeutralAndTheirDetailsStartCollapsed() {
        val value = mutableStateOf(state().copy(messages = listOf(UiMessage("owned-terminal", MessageRole.ASSISTANT,
            "目标文件仍未创建。", MessageStatus.FAILED, detail = "连接中断；已完成的操作保留。",
            desktopAgent = true, execution = trace("failed")))))
        compose.setContent { AuraApp(value.value, MinimalActions()) }
        for ((index, stateName) in listOf("failed", "needs_user", "cancelled", "interrupted").withIndex()) {
            compose.runOnIdle {
                value.value = value.value.copy(messages = listOf(value.value.messages.single().copy(
                    execution = trace(stateName).copy(startedAt = "2026-11-07T10:00:0${index}Z"))))
            }
            compose.onNodeWithTag("execution-events").assertDoesNotExist()
            compose.onNodeWithTag("execution-detail").assertDoesNotExist()
            compose.onNodeWithText("目标文件仍未创建。").assertIsDisplayed()
            compose.onNodeWithText("等待确认", substring = true).assertDoesNotExist()
            assertNeutralInk(compose.onNodeWithTag("message-body-owned-terminal"))
            assertNeutralInk(compose.onNodeWithTag("execution-toggle"))
            compose.onNodeWithTag("execution-toggle").performClick()
            compose.onNodeWithTag("execution-detail").assertTextContains("连接中断", substring = true)
            assertNeutralInk(compose.onNodeWithTag("execution-detail"))
        }
        screenshot("terminal-detail-dark.png")
    }

    @Test fun realOperationHistoryRunsThenCollapsesAndCanBeReopened() {
        val current = trace("running")
        val value = mutableStateOf(state(ThemeMode.LIGHT).copy(desktopAgentMode = true,
            desktopTaskRunning = true, messages = listOf(UiMessage("owned-running", MessageRole.ASSISTANT,
                "", MessageStatus.PENDING, desktopAgent = true, execution = current))))
        compose.setContent { AuraApp(value.value, MinimalActions()) }
        compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertExists()
        compose.onNodeWithTag("execution-step-tool").assertIsDisplayed()
        compose.onNodeWithTag("execution-step-phase").assertDoesNotExist()
        compose.onNodeWithContentDescription("停止电脑任务").assertIsDisplayed()
        screenshot("execution-running-light.png")
        val completed = current.copy(state = "completed", durationMs = 4300,
            events = current.events.map { it.copy(status = "completed") })
        compose.runOnIdle { value.value = value.value.copy(desktopTaskRunning = false,
            messages = listOf(value.value.messages.single().copy(content = "已整理好文件。", execution = completed))) }
        compose.onNodeWithTag("execution-events").assertDoesNotExist()
        compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertDoesNotExist()
        compose.onNodeWithText("已整理好文件。").assertIsDisplayed()
        compose.onNodeWithTag("execution-toggle").performClick()
        compose.onNodeWithTag("execution-step-tool").assertIsDisplayed()
        compose.runOnIdle { value.value = value.value.copy(messages = listOf(value.value.messages.single().copy(
            execution = completed.copy(events = completed.events.toList())))) }
        compose.onNodeWithTag("execution-events").assertIsDisplayed()
        screenshot("execution-expanded-light.png")
    }

    @Test fun shortBubblesUseNaturalWidthAndImagesStayInlineWithoutRestaging() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val file = File(context.cacheDir, "owned-minimal-inline.png")
        val image = Bitmap.createBitmap(320, 180, Bitmap.Config.ARGB_8888)
        image.eraseColor(android.graphics.Color.rgb(100, 155, 180))
        file.outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) }
        image.recycle()
        val actions = object : MinimalActions() {
            override fun sendPhoneMessage(text: String, onAccepted: (Boolean) -> Unit) {
                sendMessage(text)
                onAccepted(true)
            }
        }
        compose.setContent { AuraApp(state(ThemeMode.LIGHT).copy(messages = listOf(
            UiMessage("owned-image", MessageRole.USER, "看看这张图", attachmentUri = file.absolutePath,
                attachmentId = "owned-image-id"), UiMessage("owned-short", MessageRole.USER, "继续"))), actions) }
        val bubble = compose.onNodeWithTag("message-bubble-owned-short").assertIsDisplayed().fetchSemanticsNode().boundsInRoot
        val row = compose.onNodeWithTag("message-owned-short").fetchSemanticsNode().boundsInRoot
        assertTrue("Short user messages should not stretch into a full row", bubble.width < row.width * .6f)
        // The conversation initially follows the last message. Bring the older
        // image into view and inspect the actual decoded Image below its merged
        // clickable preview semantics, rather than waiting for an off-screen row.
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("message-owned-image"))
        compose.waitUntil(5000) {
            compose.onAllNodesWithContentDescription("消息中的图片", useUnmergedTree = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        val inline = compose.onNodeWithContentDescription("消息中的图片", useUnmergedTree = true)
            .assertIsDisplayed().fetchSemanticsNode().boundsInRoot
        val imageBubble = compose.onNodeWithTag("message-bubble-owned-image").fetchSemanticsNode().boundsInRoot
        assertTrue("Inline image must fit inside its message bubble",
            inline.left >= imageBubble.left - 1f && inline.right <= imageBubble.right + 1f)
        compose.onNodeWithContentDescription("预览消息图片").performClick()
        assertEquals(listOf("owned-image-id"), actions.openedImages)
        compose.onNodeWithContentDescription("移除待发送图片").assertDoesNotExist()
        compose.onNodeWithContentDescription("消息内容").performTextInput("这次只有文字")
        compose.onNodeWithContentDescription("发送消息").performClick()
        assertEquals(listOf("这次只有文字"), actions.sent)
        hideKeyboard()
        screenshot("inline-image-and-short-bubble-light.png")
    }

    @Test fun pendingMessagesRemainReachableOnlyWhenThereIsSomethingToHandle() {
        val actions = MinimalActions()
        compose.setContent { AuraApp(state().copy(queue = listOf(UiQueueItem("owned-pending",
            "原会话里尚未发出的内容", QueueStatus.NEEDS_ATTENTION, detail = "原会话已删除"))), actions) }
        compose.onNodeWithTag("app-settings").performClick()
        compose.onNodeWithTag("pending-messages-setting").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("查看 (1)").performClick()
        compose.onNodeWithText("原会话里尚未发出的内容").assertIsDisplayed()
        assertTrue(actions.sent.isEmpty())
        assertTrue(actions.desktopMessages.isEmpty())
        screenshot("pending-message-recovery-dark.png")
    }

    private fun trace(state: String) = UiExecution(state, "2026-11-07T10:00:00Z", 2400,
        listOf(UiExecutionStep("phase", "progress", "正在规划任务…", "completed", 0),
            UiExecutionStep("tool", "tool", "查看目录信息", "completed", 1200),
            UiExecutionStep("computer", "computer", "查看电脑画面", if (state == "running") "running" else "completed", 2100)))

    private fun assertNeutralInk(node: SemanticsNodeInteraction) {
        val pixels = node.assertIsDisplayed().captureToImage().toPixelMap()
        var red = 0
        for (y in 0 until pixels.height) for (x in 0 until pixels.width) {
            val color = pixels[x, y]
            if (color.red > .55f && color.red > color.green * 1.25f && color.red > color.blue * 1.15f) red++
        }
        assertTrue("Task outcomes should not be rendered as a block of red ink", red < 20)
    }

    private fun hideKeyboard() {
        compose.runOnUiThread {
            androidx.core.view.WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView)
                .hide(androidx.core.view.WindowInsetsCompat.Type.ime())
        }
        compose.waitForIdle()
    }

    private fun back() {
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
        compose.waitForIdle()
    }

    private fun screenshot(name: String) {
        compose.runOnUiThread {
            val light = name.contains("-light")
            androidx.core.view.WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView).apply {
                isAppearanceLightStatusBars = light
                isAppearanceLightNavigationBars = light
            }
        }
        compose.waitForIdle()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.waitForIdleSync()
        // System-bar appearance and modal windows are committed by a separate
        // native frame, after Compose semantics have already become idle.
        android.os.SystemClock.sleep(250)
        instrumentation.waitForIdleSync()
        val directory = File(instrumentation.targetContext.filesDir, "qa/minimal-ui-1.17").apply { mkdirs() }
        val image = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { File(directory, name).outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) } }
        finally { image.recycle() }
    }
}

private open class MinimalActions : UiActions {
    val sent = mutableListOf<String>()
    val desktopMessages = mutableListOf<String>()
    val openedImages = mutableListOf<String>()
    var desktopScreenshots = 0
    override fun pair(payload: String) = Unit
    override fun consumePairInfo() = Unit
    override fun reconnect() = Unit
    override fun disconnect() = Unit
    override fun syncNow() = Unit
    override fun selectConversation(id: String) = Unit
    override fun newConversation() = Unit
    override fun sendMessage(text: String) { sent += text }
    override fun retryMessage(id: String) = Unit
    override fun discardOutbox(requestId: String) = Unit
    override fun pickImage() = Unit
    override fun removePendingImage() = Unit
    override fun petCommand(action: String, value: PetValue) = Unit
    override fun refreshImages() = Unit
    override fun openImage(id: String) { openedImages += id }
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
    override fun saveDirectApiConfig(input: UiApiConfigInput) = Unit
    override fun sendQueuedOnPhone(requestId: String) = Unit
    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { desktopMessages += text; onAccepted(false) }
    override fun requestDesktopScreenshot() { desktopScreenshots++ }
}
