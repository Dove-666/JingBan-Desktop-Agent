package com.aura.companion.ui

import android.graphics.Bitmap
import android.content.ClipData
import android.content.ClipboardManager
import android.os.Build
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.unit.Density
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.WindowInsetsCompat
import com.aura.companion.MainActivity
import com.aura.companion.BuildConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** These states exist only in the instrumentation APK, never in production. */
@RunWith(AndroidJUnit4::class)
class AuraUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Before fun edge_to_edge_matches_the_real_activity() {
        compose.runOnUiThread {
            compose.activity.window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
            compose.activity.enableEdgeToEdge()
            compose.activity.getSystemService(ClipboardManager::class.java)?.apply {
                if (Build.VERSION.SDK_INT >= 28) clearPrimaryClip()
                else setPrimaryClip(ClipData.newPlainText("", ""))
            }
        }
    }

    @Test fun welcome_has_clear_pairing_action_and_no_demo_conversation() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(UiState(theme = ThemeMode.LIGHT), actions) }
        compose.onNodeWithText("连接我的电脑").assertIsDisplayed()
        compose.onNodeWithText("把陪伴，\n带在身边。").assertIsDisplayed()
        compose.onNodeWithContentDescription("消息内容").assertDoesNotExist()
        capture("01-welcome-light.png")
        compose.onNodeWithText("连接我的电脑").performClick()
        compose.onNodeWithText("完整配对链接").assertIsDisplayed()
        assertTrue(actions.pairings.isEmpty())
    }

    @Test fun deep_link_prefills_but_never_submits_without_confirmation() {
        val payload = "aura://pair/instrumentation-only"
        val state = mutableStateOf(UiState(pendingPairInfo = payload, theme = ThemeMode.LIGHT))
        val actions = RecordingActions(onConsume = { state.value = state.value.copy(pendingPairInfo = null) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNode(hasSetTextAction() and hasText("完整配对链接")).assertTextContains(payload)
        assertTrue(actions.pairings.isEmpty())
        assertEquals(1, actions.consumed)
        capture("02-pairing-prefill.png")
        compose.onNodeWithText("连接电脑").performClick()
        compose.runOnIdle { assertEquals(listOf(payload), actions.pairings) }
    }

    @Test fun short_code_error_explains_the_full_link_and_allows_correction_at_large_type() {
        val message = "不要只填短码。请在电脑点击「复制配对信息」，粘贴完整链接。"
        val actions = RecordingActions()
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, 1.3f)) {
                AuraTheme(ThemeMode.LIGHT) {
                    PairingDialog(UiState(pairingError = message), actions, initialPayload = "123456", onDismiss = {})
                }
            }
        }
        compose.onNode(hasSetTextAction() and hasText("完整配对链接")).assertTextContains("123456")
        compose.onNodeWithText("以 aura://pair/ 开头，也支持完整 JSON；不要只填短码。")
            .performScrollTo().assertIsDisplayed()
        compose.onNodeWithText(message).performScrollTo().assertIsDisplayed()
        assertTrue(actions.pairings.isEmpty())
        capture("10-pairing-short-code-error.png")
        compose.onNode(hasSetTextAction() and hasText("完整配对链接"))
            .performTextReplacement("aura://pair/corrected-instrumentation-only")
        compose.onNodeWithText("连接电脑").performClick()
        compose.runOnIdle { assertEquals(listOf("aura://pair/corrected-instrumentation-only"), actions.pairings) }
    }

    @Test fun offline_history_remains_readable_and_send_is_queued_by_the_app_action() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState(offline = true), actions) }
        compose.onNodeWithText("电脑暂时离线 · 消息会保留").assertIsDisplayed()
        compose.onNodeWithText("等待连接后发送").assertIsDisplayed()
        compose.onNodeWithContentDescription("消息内容").performTextInput("离线验收消息")
        compose.onNodeWithContentDescription("保存消息，连接后发送").performClick()
        compose.runOnIdle { assertEquals(listOf("离线验收消息"), actions.sent) }
        capture("03-offline-history.png", hideKeyboard = true)
    }

    @Test fun drawer_search_and_selection_use_real_actions() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState(), actions) }
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("搜索对话").performTextInput("第二段")
        compose.onNodeWithText("第二段验收记录").performClick()
        compose.runOnIdle { assertEquals(listOf("qa-second"), actions.selected) }
    }

    @Test fun dark_conversation_has_readable_native_controls() {
        compose.runOnUiThread {
            WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView).apply {
                isAppearanceLightStatusBars = false
                isAppearanceLightNavigationBars = false
            }
        }
        compose.setContent { AuraApp(fixtureState().copy(theme = ThemeMode.DARK), RecordingActions()) }
        compose.onNodeWithContentDescription("消息内容").assertIsDisplayed()
        compose.onNodeWithText("已连接 · 对话已同步").assertIsDisplayed()
        capture("04-conversation-dark.png")
    }

    @Test fun scaled_type_keeps_input_and_history_accessible() {
        compose.setContent {
            val current = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(current.density, 1.3f)) {
                AuraApp(fixtureState(offline = true), RecordingActions())
            }
        }
        compose.onNodeWithContentDescription("消息内容").assertIsDisplayed()
        compose.onNodeWithContentDescription("打开对话历史").assertIsDisplayed()
        compose.onNodeWithText("等待连接后发送").performScrollTo().assertIsDisplayed()
        capture("05-type-scale-130.png")
    }

    @Test fun offline_pet_controls_do_not_queue_expired_actions() {
        compose.setContent { AuraApp(fixtureState(offline = true), RecordingActions()) }
        compose.onNodeWithContentDescription("更多选项").performClick()
        compose.onNode(hasText("桌宠控制") and hasAnyAncestor(isPopup())).performClick()
        compose.onNodeWithText("开心").assertIsNotEnabled()
        capture("06-pet-controls-offline.png")
    }

    @Test fun local_attachment_is_decoded_as_a_thumbnail_and_visible_in_the_image_list() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val file = File(context.cacheDir, "ui-thumbnail-fixture.png")
        val source = Bitmap.createBitmap(2048, 1024, Bitmap.Config.ARGB_8888).apply {
            eraseColor(android.graphics.Color.rgb(133, 144, 166))
        }
        file.outputStream().use { source.compress(Bitmap.CompressFormat.PNG, 100, it) }
        source.recycle()
        val state = fixtureState().copy(images = listOf(UiImage("qa-image", "缩略图验收.png",
            "2048 × 1024", "已发送到电脑", file.absolutePath)))
        compose.setContent { AuraApp(state, RecordingActions()) }
        compose.onNodeWithContentDescription("更多选项").performClick()
        compose.onNode(hasText("图片收件箱") and hasAnyAncestor(isPopup())).performClick()
        compose.waitUntil(5000) {
            compose.onAllNodesWithContentDescription("缩略图验收.png的缩略图").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("已发送到电脑").assertIsDisplayed()
        capture("07-image-inbox.png")
    }

    @Test fun assistant_markdown_is_readable_and_code_has_its_own_copy_action() {
        val message = "## 标题验收\n\n这是一段**重要内容**，包含 `inline code`。\n\n- 列表第一项\n- 列表第二项\n\n```kotlin\nval aura = \"hello\"\n```"
        val state = fixtureState().copy(messages = listOf(UiMessage("qa-markdown", MessageRole.ASSISTANT, message)))
        compose.setContent { AuraApp(state, RecordingActions()) }
        compose.onNodeWithText("标题验收").assertIsDisplayed()
        compose.onNodeWithText("复制代码").performClick()
        compose.onNodeWithText("已复制").assertIsDisplayed()
        compose.onNodeWithText("复制回复").assertIsDisplayed()
        capture("08-markdown-light.png")
    }

    @Test fun retained_outbox_content_can_be_copied_and_discard_requires_confirmation() {
        val actions = RecordingActions()
        val state = fixtureState(offline = true).copy(queue = listOf(UiQueueItem("qa-request",
            "丢弃前可以复制的验收内容", QueueStatus.NEEDS_ATTENTION, detail = "原会话已删除")))
        compose.setContent { AuraApp(state, actions) }
        compose.onNodeWithContentDescription("更多选项").performClick()
        compose.onNode(hasText("待处理消息 (1)") and hasAnyAncestor(isPopup())).performClick()
        compose.onNodeWithText("丢弃前可以复制的验收内容").assertIsDisplayed()
        compose.onNodeWithText("需要手动处理").assertIsDisplayed()
        compose.onNodeWithText("复制内容").performClick()
        capture("09-retained-outbox.png")
        compose.onNodeWithText("丢弃").performClick()
        assertTrue(actions.discarded.isEmpty())
        compose.onNodeWithText("确认丢弃").performClick()
        compose.runOnIdle { assertEquals(listOf("qa-request"), actions.discarded) }
    }

    private fun capture(filename: String, hideKeyboard: Boolean = false) {
        if (hideKeyboard) {
            compose.runOnUiThread {
                compose.activity.currentFocus?.clearFocus()
                WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView)
                    .hide(WindowInsetsCompat.Type.ime())
            }
            SystemClock.sleep(350)
        }
        compose.waitForIdle()
        // Compose idling does not wait for the Android dialog window fade.
        SystemClock.sleep(350)
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val directory = File(instrumentation.targetContext.filesDir,
            "qa/ui-${BuildConfig.VERSION_NAME.removeSuffix("-debug")}").apply { mkdirs() }
        val image = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        File(directory, filename).outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) }
        image.recycle()
    }
}

@RunWith(AndroidJUnit4::class)
class MainActivityStartupTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    @Test fun production_activity_does_not_insert_preview_messages() {
        compose.onAllNodesWithText("周末的一点计划", substring = true).assertCountEquals(0)
        compose.onAllNodesWithText("今天终于忙完了", substring = true).assertCountEquals(0)
        compose.onAllNodesWithText("界面验收记录", substring = true).assertCountEquals(0)
    }
}

private fun fixtureState(offline: Boolean = false) = UiState(
    paired = true, desktopName = "验收电脑", theme = ThemeMode.LIGHT,
    connection = if (offline) ConnectionState.DISCONNECTED else ConnectionState.CONNECTED,
    sync = if (offline) SyncState.IDLE else SyncState.READY,
    selectedConversationId = "qa-first", queuedCount = if (offline) 1 else 0,
    conversations = listOf(UiConversation("qa-first", "界面验收记录", "测试进程中的记录"),
        UiConversation("qa-second", "第二段验收记录", "用于验证本地搜索")),
    messages = listOf(
        UiMessage("qa-system", MessageRole.SYSTEM, "界面验收数据 · 仅存在于测试进程"),
        UiMessage("qa-user", MessageRole.USER, "这是一条用于检验中文排版的消息。"),
        UiMessage("qa-assistant", MessageRole.ASSISTANT,
            "正文保留清晰的行距与留白。\n\n即使暂时离线，手机上已有的对话仍然可以阅读。"),
        UiMessage("qa-last", MessageRole.USER, "离线时记录的内容。",
            if (offline) MessageStatus.QUEUED else MessageStatus.SENT)),
)

private class RecordingActions(private val onConsume: () -> Unit = {}) : UiActions {
    val pairings = mutableListOf<String>()
    val sent = mutableListOf<String>()
    val selected = mutableListOf<String>()
    val discarded = mutableListOf<String>()
    var consumed = 0
    override fun pair(payload: String) { pairings += payload }
    override fun consumePairInfo() { consumed++; onConsume() }
    override fun reconnect() = Unit
    override fun disconnect() = Unit
    override fun syncNow() = Unit
    override fun selectConversation(id: String) { selected += id }
    override fun newConversation() = Unit
    override fun sendMessage(text: String) { sent += text }
    override fun retryMessage(id: String) = Unit
    override fun discardOutbox(requestId: String) { discarded += requestId }
    override fun pickImage() = Unit
    override fun petCommand(action: String, value: PetValue) = Unit
    override fun refreshImages() = Unit
    override fun openImage(id: String) = Unit
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
}
