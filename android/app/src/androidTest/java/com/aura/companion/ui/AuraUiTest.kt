package com.aura.companion.ui

import android.graphics.Bitmap
import android.content.ClipData
import android.content.ClipboardManager
import android.os.Build
import android.os.SystemClock
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
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
        compose.onNodeWithText("连接电脑").assertIsDisplayed()
                compose.onNodeWithContentDescription("消息内容").assertDoesNotExist()
        capture("01-welcome-light.png")
        compose.onNodeWithText("连接电脑").performClick()
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
        compose.onNode(hasText("连接电脑") and hasAnyAncestor(isDialog())).performClick()
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

    @Test fun missing_api_keeps_history_and_draft_until_the_user_configures_a_model() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState(offline = true), actions) }
        compose.onAllNodesWithText("电脑暂时离线 · 消息会保留").assertCountEquals(0)
        compose.onNodeWithText("待发送").assertIsDisplayed()
        compose.onNodeWithContentDescription("消息内容").performTextInput("离线验收消息")
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.onNodeWithTag("app-settings").assertIsDisplayed()
        compose.runOnIdle { assertTrue(actions.sent.isEmpty()) }
        capture("03-offline-history.png", hideKeyboard = true)
    }

    @Test fun drawer_search_and_selection_use_real_actions() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState(), actions) }
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithContentDescription("搜索对话").performTextInput("第二段")
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
        compose.onNodeWithTag("app-settings").assertIsDisplayed()
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
        compose.onNodeWithText("待发送").performScrollTo().assertIsDisplayed()
        capture("05-type-scale-130.png")
    }

    @Test fun offline_pet_controls_do_not_queue_expired_actions() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState(offline = true), actions) }
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithTag("drawer-settings").assertIsDisplayed().performClick()
        compose.onNodeWithTag("settings-sheet").assertIsDisplayed()
        compose.onNodeWithText("桌宠控制").assertDoesNotExist()
        compose.onNodeWithText("图片收件箱").assertDoesNotExist()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.runOnIdle { assertTrue(actions.desktopRequests.isEmpty()); assertEquals(0, actions.screenshotRequests) }
        capture("06-minimal-settings-offline.png")
    }

    @Test fun local_attachment_is_decoded_as_a_thumbnail_and_visible_in_the_image_list() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val file = File(context.cacheDir, "ui-thumbnail-fixture.png")
        val source = Bitmap.createBitmap(2048, 1024, Bitmap.Config.ARGB_8888).apply {
            eraseColor(android.graphics.Color.rgb(133, 144, 166))
        }
        file.outputStream().use { source.compress(Bitmap.CompressFormat.PNG, 100, it) }
        source.recycle()
        val actions = RecordingActions()
        val state = fixtureState().copy(messages = listOf(UiMessage("qa-large-image", MessageRole.USER,
            "这张图片是什么？", attachmentId = "qa-image", attachmentUri = file.absolutePath)))
        compose.setContent { AuraApp(state, actions) }
        compose.waitUntil(5000) { compose.onAllNodesWithContentDescription("消息中的图片").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithContentDescription("消息中的图片").assertIsDisplayed()
        compose.onNodeWithContentDescription("预览消息图片").performClick()
        compose.runOnIdle { assertEquals(listOf("qa-image"), actions.openedImages) }
        capture("07-large-image-inline.png")
    }

    @Test fun assistant_markdown_is_readable_and_code_has_its_own_copy_action() {
        val message = "## 标题验收\n\n这是一段**重要内容**，包含 `inline code`。\n\n- 列表第一项\n- 列表第二项\n\n```kotlin\nval aura = \"hello\"\n```"
        val state = fixtureState().copy(messages = listOf(UiMessage("qa-markdown", MessageRole.ASSISTANT, message)))
        compose.setContent { AuraApp(state, RecordingActions()) }
        compose.onNodeWithText("标题验收").assertIsDisplayed()
        compose.onNodeWithText("复制代码").performClick()
        compose.onNodeWithText("已复制").assertIsDisplayed()
        compose.onNodeWithContentDescription("复制回复").assertIsDisplayed()
        capture("08-markdown-light.png")
    }

    @Test fun retained_outbox_content_can_be_copied_and_discard_requires_confirmation() {
        val actions = RecordingActions()
        val state = fixtureState(offline = true).copy(queue = listOf(UiQueueItem("qa-request",
            "丢弃前可以复制的验收内容", QueueStatus.NEEDS_ATTENTION, detail = "原会话已删除")))
        compose.setContent { AuraApp(state, actions) }
        openPendingQueue(1)
        compose.onNodeWithText("丢弃前可以复制的验收内容").assertIsDisplayed()
        compose.onNodeWithText("需要手动处理").assertIsDisplayed()
        compose.onNodeWithText("复制内容").performClick()
        capture("09-retained-outbox.png")
        compose.onNodeWithText("丢弃").performClick()
        assertTrue(actions.discarded.isEmpty())
        compose.onNodeWithText("确认丢弃").performClick()
        compose.runOnIdle { assertEquals(listOf("qa-request"), actions.discarded) }
    }

    @Test fun selecting_an_image_stages_it_until_the_user_sends_a_question() {
        val image = imageFixture()
        val state = mutableStateOf(fixtureState().copy(messages = emptyList(), directApi = directApiFixture()))
        val actions = RecordingActions(onPick = {
            state.value = state.value.copy(pendingAttachment = UiPendingAttachment(
                "qa-staged", image.name, image.absolutePath, "qa-first"))
        })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("添加图片").performClick()
        compose.onNodeWithContentDescription("移除待发送图片").assertIsDisplayed()
        compose.onNodeWithText("图片和提问由手机发送给模型，记录自动同步。").assertDoesNotExist()
        assertTrue(actions.sent.isEmpty())
        compose.waitUntil(5000) { compose.onAllNodesWithContentDescription("待发送图片").fetchSemanticsNodes().isNotEmpty() }
        capture("11-staged-image-before-send.png")
        compose.onNodeWithContentDescription("消息内容").performTextInput("这张图里有什么？")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { assertEquals(listOf("这张图里有什么？"), actions.sent) }
    }

    @Test fun image_only_send_is_enabled_and_removing_the_image_disables_empty_send() {
        val image = imageFixture()
        val attachment = UiPendingAttachment("qa-staged", image.name, image.absolutePath, "qa-first")
        val state = mutableStateOf(fixtureState(offline = true).copy(paired = false, messages = emptyList(), pendingAttachment = attachment,
            directApi = directApiFixture().copy(source = ApiConfigSource.LOCAL)))
        val actions = RecordingActions(onRemove = { state.value = state.value.copy(pendingAttachment = null) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithText("图片和提问由手机直接发送给模型。").assertDoesNotExist()
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        compose.onNodeWithContentDescription("移除待发送图片").performClick()
        compose.onNodeWithContentDescription("移除待发送图片").assertDoesNotExist()
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.runOnIdle { state.value = state.value.copy(pendingAttachment = attachment) }
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle {
            assertEquals(1, actions.removed)
            // The data layer owns the default image-only prompt.
            assertEquals(listOf(""), actions.sent)
        }
    }

    @Test fun pending_images_do_not_cross_conversations_and_preparing_disables_submission() {
        val image = imageFixture()
        val attachment = UiPendingAttachment("qa-staged", image.name, image.absolutePath, "qa-first")
        val state = mutableStateOf(fixtureState().copy(messages = emptyList(), pendingAttachment = attachment,
            selectedConversationId = "qa-second"))
        compose.setContent { AuraApp(state.value, RecordingActions()) }
        compose.onNodeWithContentDescription("移除待发送图片").assertDoesNotExist()
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.runOnIdle { state.value = state.value.copy(selectedConversationId = "qa-first", preparingImage = true) }
        compose.onNodeWithContentDescription("移除待发送图片").assertIsDisplayed()
        compose.onNodeWithText("正在准备，完成后可发送").assertDoesNotExist()
        compose.onNodeWithContentDescription("添加图片").assertIsNotEnabled()
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.onNodeWithContentDescription("移除待发送图片").assertIsNotEnabled()
    }

    @Test fun canonical_image_message_has_a_full_thumbnail_and_opens_its_attachment() {
        val image = imageFixture()
        val actions = RecordingActions()
        val state = fixtureState().copy(messages = listOf(UiMessage("qa-image-message", MessageRole.USER,
            "这张图里有什么？", attachmentUri = image.absolutePath, attachmentId = "qa-canonical-image")))
        compose.setContent { AuraApp(state, actions) }
        compose.waitUntil(5000) {
            compose.onAllNodesWithContentDescription("消息中的图片", useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithContentDescription("消息中的图片", useUnmergedTree = true).assertHeightIsAtLeast(180.dp)
        compose.onNodeWithText("这张图里有什么？").assertIsDisplayed()
        capture("12-canonical-image-message.png")
        compose.onNodeWithContentDescription("预览消息图片").performClick()
        compose.runOnIdle { assertEquals(listOf("qa-canonical-image"), actions.openedImages) }
    }

    @Test fun cached_direct_api_can_send_while_computer_is_offline() {
        val actions = RecordingActions()
        val state = fixtureState(offline = true).copy(paired = false, messages = emptyList(), queuedCount = 0,
            directApi = directApiFixture().copy(source = ApiConfigSource.LOCAL), historyPendingCount = 2)
        compose.setContent { AuraApp(state, actions) }
        assertConnectionStatus("离线")
        compose.onNodeWithTag("conversation-title").assertTextEquals("界面验收记录")
        compose.onAllNodesWithText("电脑离线 · 2 条记录待同步").assertCountEquals(0)
        compose.onNodeWithContentDescription("消息内容").performTextInput("电脑关机时也能聊吗？")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        capture("13-direct-api-computer-offline.png", hideKeyboard = true)
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { assertEquals(listOf("电脑关机时也能聊吗？"), actions.sent) }
    }

    @Test fun direct_api_busy_prevents_duplicate_submissions() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState(offline = true).copy(
            directApi = directApiFixture(), directChatBusy = true), actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("下一条先写在这里")
        compose.onNodeWithText("手机正在回复，记录会自动同步。").assertDoesNotExist()
        compose.onNodeWithContentDescription("正在回复，请稍候").assertIsNotEnabled()
        capture("14-direct-api-reply-in-progress.png", hideKeyboard = true)
        compose.runOnIdle { assertTrue(actions.sent.isEmpty()) }
    }

    @Test fun api_settings_show_only_safe_summary_and_automatic_sync() {
        val state = fixtureState(offline = true).copy(directApi = directApiFixture().copy(
            baseUrl = "https://unused:fixture-only@example.invalid/v1?token=fixture-only#fixture-only",
            sync = ApiConfigSyncState.ERROR, lastSyncedAt = "今天 11:30",
            message = "电脑暂时离线，已保存的配置仍可使用。"))
        compose.setContent { AuraApp(state, RecordingActions()) }
        openConnectionSettings()
        compose.onNodeWithTag("model-setting").assertIsDisplayed()
        compose.onNodeWithText("验收模型").assertIsDisplayed()
        compose.onNodeWithText("配置详情").assertDoesNotExist()
        compose.onNodeWithText("电脑同步 · 已配置").assertDoesNotExist()
        compose.onNodeWithText("密钥已安全保存").assertDoesNotExist()
        compose.onNodeWithText("配置更新时间", substring = true).assertDoesNotExist()
        compose.onAllNodesWithText("fixture-only", substring = true).assertCountEquals(0)
        compose.onAllNodesWithText("https://", substring = true).assertCountEquals(0)
        capture("15-api-settings-safe-summary.png")
        compose.onNodeWithText("编辑").performClick()
        compose.onNodeWithText("手机模型配置").assertIsDisplayed()
    }

    @Test fun history_sync_queue_describes_records_without_regenerating_a_reply() {
        val state = fixtureState(offline = true).copy(directApi = directApiFixture(), queuedCount = 0,
            historyPendingCount = 1, queue = listOf(UiQueueItem("qa-record-sync", "手机上已经完成的对话",
                QueueStatus.QUEUED, isHistorySync = true)))
        compose.setContent { AuraApp(state, RecordingActions()) }
        openPendingQueue(1)
        compose.onNodeWithText("记录等待同步").assertIsDisplayed()
        compose.onNodeWithText("手机上已经完成的对话").assertIsDisplayed()
        compose.onAllNodesWithText("等待电脑回复").assertCountEquals(0)
        capture("16-records-awaiting-sync.png")
    }

    @Test fun automatic_sync_does_not_show_chat_banners_progress_or_move_content() {
        val ready = fixtureState().copy(directApi = directApiFixture())
        val state = mutableStateOf(ready)
        val actions = RecordingActions()
        compose.setContent { AuraApp(state.value, actions) }
        compose.waitForIdle()
        val input = compose.onNodeWithContentDescription("消息内容")
        val message = compose.onNodeWithText("这是一条用于检验中文排版的消息。")
        val inputBounds = input.fetchSemanticsNode().boundsInRoot
        val messageBounds = message.fetchSemanticsNode().boundsInRoot
        fun assertQuietLayout() {
            compose.onAllNodesWithText("正在同步对话", substring = true).assertCountEquals(0)
            compose.onAllNodesWithText("正在同步记录", substring = true).assertCountEquals(0)
            compose.onAllNodesWithText("API 配置正在自动同步").assertCountEquals(0)
            compose.onAllNodes(SemanticsMatcher.keyIsDefined(SemanticsProperties.ProgressBarRangeInfo),
                useUnmergedTree = true).assertCountEquals(0)
            assertEquals(inputBounds, input.fetchSemanticsNode().boundsInRoot)
            assertEquals(messageBounds, message.fetchSemanticsNode().boundsInRoot)
            compose.onNodeWithTag("app-settings").assertIsEnabled()
            compose.onNodeWithTag("conversation-title").assertTextEquals("界面验收记录")
        }
        assertQuietLayout()
        compose.runOnIdle { state.value = ready.copy(sync = SyncState.SYNCING,
            directApi = ready.directApi.copy(sync = ApiConfigSyncState.SYNCING)) }
        assertQuietLayout()
        capture("17-quiet-chat-during-auto-sync.png")
        for (connection in listOf(ConnectionState.CONNECTING, ConnectionState.DISCONNECTED, ConnectionState.CONNECTED)) {
            compose.runOnIdle { state.value = ready.copy(connection = connection, historyPendingCount = 3) }
            assertQuietLayout()
        }
        openConnectionSettings()
        compose.onNodeWithText("同步记录").assertDoesNotExist()
        compose.runOnIdle { state.value = ready.copy(sync = SyncState.ERROR) }
        compose.onNodeWithText("重试同步").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, actions.syncRequests); state.value = ready.copy(sync = SyncState.SYNCING) }
        compose.onNodeWithText("重试同步").assertDoesNotExist()
        capture("18-sync-error-recovery-settings.png")
    }

    @Test fun connection_can_be_cancelled_before_pairing_and_during_phone_streaming() {
        val actions = RecordingActions()
        val state = mutableStateOf(UiState(connection = ConnectionState.CONNECTING, pairing = true,
            directApi = directApiFixture(), directChatBusy = true, theme = ThemeMode.LIGHT))
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithTag("app-settings").assertIsEnabled().performClick()
        compose.onNodeWithText("取消").performScrollTo().assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, actions.disconnectRequests) }
        compose.runOnIdle { state.value = state.value.copy(paired = true) }
        compose.onNodeWithText("取消").performScrollTo().assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(2, actions.disconnectRequests) }
        compose.runOnIdle { state.value = state.value.copy(connection = ConnectionState.CONNECTED, pairing = false) }
        compose.onNodeWithText("取消").assertDoesNotExist()
        compose.onNodeWithText("断开").performScrollTo().assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(3, actions.disconnectRequests) }
        compose.runOnIdle { state.value = state.value.copy(connection = ConnectionState.DISCONNECTED, paired = false) }
        compose.onNodeWithText("取消").assertDoesNotExist()
        compose.onNodeWithText("断开").assertDoesNotExist()
    }

    @Test fun unpaired_welcome_has_an_independent_model_setup_without_requiring_a_computer() {
        compose.setContent { AuraApp(UiState(theme = ThemeMode.LIGHT), RecordingActions()) }
        compose.onNodeWithText("配置模型").performScrollTo().assertIsDisplayed()
        capture("19-independent-model-welcome.png")
        compose.onNodeWithText("配置模型").performClick()
        compose.onNodeWithText("手机模型配置").assertIsDisplayed()
        val address = compose.onNode(hasSetTextAction() and hasText("API 地址"))
        assertTrue(address.fetchSemanticsNode().config[SemanticsProperties.EditableText].text.isEmpty())
        val password = compose.onNode(hasSetTextAction() and hasContentDescription("API Key"))
        password.performScrollTo().assert(SemanticsMatcher.keyIsDefined(SemanticsProperties.Password))
        assertTrue(password.fetchSemanticsNode().config[SemanticsProperties.EditableText].text.isEmpty())
    }

    @Test fun api_password_is_not_restored_and_blank_submission_keeps_the_saved_key_contract() {
        val actions = RecordingActions()
        val state = mutableStateOf(UiState(directApi = directApiFixture()))
        var dismissed = 0
        val restoration = StateRestorationTester(compose)
        restoration.setContent { AuraTheme(ThemeMode.LIGHT) {
            ApiConfigDialog(state.value, actions, onDismiss = { dismissed++ })
        } }
        val password = compose.onNode(hasSetTextAction() and hasContentDescription("API Key"))
        password.performScrollTo().performTextInput("private-ui-fixture-not-a-real-key")
        password.assert(SemanticsMatcher.keyIsDefined(SemanticsProperties.Password))
        restoration.emulateSavedInstanceStateRestore()
        assertTrue(password.fetchSemanticsNode().config[SemanticsProperties.EditableText].text.isEmpty())
        compose.onNodeWithText("保存配置").performClick()
        compose.runOnIdle {
            val input = requireNotNull(actions.savedConfig)
            assertTrue(input.apiKey.isEmpty())
            assertEquals("验收模型", input.model)
            assertTrue(!input.toString().contains("private-ui-fixture"))
            // A fast save can conflate saving=true; its receipt must still close the form.
            state.value = state.value.copy(apiConfigSaveGeneration = 1)
        }
        compose.waitForIdle()
        compose.runOnIdle { assertEquals(1, dismissed) }
    }

    @Test fun only_a_never_attempted_queue_item_offers_explicit_phone_sending() {
        val actions = RecordingActions()
        val state = fixtureState(offline = true).copy(directApi = directApiFixture(), queue = listOf(
            UiQueueItem("qa-safe-queued", "未发出的带图问题", QueueStatus.QUEUED,
                attachmentName = "已选择图片.png", canSendOnPhone = true),
            UiQueueItem("qa-unknown", "送达情况未知的旧问题", QueueStatus.UNKNOWN, canSendOnPhone = true)))
        compose.setContent { AuraApp(state, actions) }
        openPendingQueue(2)
        compose.onAllNodesWithText("手机直接发送").assertCountEquals(1)
        compose.onNodeWithText("图片 · 已选择图片.png").assertIsDisplayed()
        compose.onNodeWithText("手机直接发送").performScrollTo().performClick()
        compose.runOnIdle { assertEquals(listOf("qa-safe-queued"), actions.phoneQueueSends) }
        capture("20-explicit-phone-send-for-old-queue.png")
    }

    @Test fun eligible_queued_message_has_an_inline_phone_action_but_uncertain_message_does_not() {
        val actions = RecordingActions()
        val state = mutableStateOf(fixtureState(offline = true).copy(directApi = directApiFixture(), messages = listOf(
            UiMessage("qa-inline-safe", MessageRole.USER, "不用重新选择图片的旧问题", MessageStatus.QUEUED, retryOnPhone = true),
            UiMessage("qa-inline-unknown", MessageRole.USER, "已经尝试发送的旧问题", MessageStatus.UNCERTAIN, retryOnPhone = true))))
        compose.setContent { AuraApp(state.value, actions) }
        compose.onAllNodesWithText("手机直接发送").assertCountEquals(1)
        compose.onNodeWithText("手机直接发送").performScrollTo().performClick()
        compose.runOnIdle { assertEquals(listOf("qa-inline-safe"), actions.retried) }
        capture("21-inline-phone-send.png")
        compose.runOnIdle { state.value = state.value.copy(directApi = UiDirectApi()) }
        compose.onAllNodesWithText("手机直接发送").assertCountEquals(0)
    }

    @Test fun desktop_agent_is_explicit_and_stays_selected_after_acceptance() {
        val state = mutableStateOf(fixtureState().copy(directApi = directApiFixture(), desktopTaskAvailable = true))
        val actions = RecordingActions(onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithText("电脑 Agent").performClick()
        compose.runOnIdle { assertTrue(actions.desktopRequests.isEmpty()); assertEquals(0, actions.screenshotRequests) }
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.runOnIdle { assertEquals(0, actions.screenshotRequests) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("请打开电脑上的记事本")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { assertEquals(listOf("请打开电脑上的记事本"), actions.desktopRequests); assertTrue(actions.sent.isEmpty()) }
        capture("22-persistent-desktop-agent.png", hideKeyboard = true)
        compose.runOnIdle { actions.desktopResult?.invoke(false) }
        compose.onNodeWithContentDescription("消息内容").assertTextContains("请打开电脑上的记事本")
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { actions.desktopResult?.invoke(true) }
        assertTrue(compose.onNodeWithContentDescription("消息内容").fetchSemanticsNode()
            .config[SemanticsProperties.EditableText].text.isEmpty())
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
        compose.onNodeWithContentDescription("消息内容").performTextInput("继续")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle {
            assertEquals(listOf("请打开电脑上的记事本", "请打开电脑上的记事本", "继续"), actions.desktopRequests)
            assertTrue(actions.sent.isEmpty())
            actions.desktopResult?.invoke(true)
        }
        compose.onNodeWithText("手机聊天").performClick()
        compose.onNodeWithContentDescription("消息内容").performTextInput("谢谢")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { assertEquals(listOf("谢谢"), actions.sent) }
    }

    @Test fun disconnected_desktop_mode_keeps_the_draft_and_phone_chat_still_works() {
        val state = mutableStateOf(fixtureState(offline = true).copy(directApi = directApiFixture(),
            desktopTaskMessage = "电脑暂时离线，请连接后再交给它处理。"))
        val actions = RecordingActions(onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("请看一下我的电脑桌面")
        // This case checks offline routing; invoke the accessible action so an
        // IME inset animation cannot move the chip between pointer down and up.
        compose.onNodeWithText("电脑 Agent").assertIsEnabled()
            .performSemanticsAction(SemanticsActions.OnClick) { it() }
        connectionSheetHeading().assertDoesNotExist()
        openConnectionSettings()
        connectionSheetHeading().assertIsDisplayed()
        compose.onNodeWithText("连接").assertIsEnabled().performClick()
        compose.runOnIdle {
            assertEquals(1, actions.reconnectRequests)
            assertTrue(actions.desktopRequests.isEmpty())
        }
        capture("23-desktop-offline-recovery.png", hideKeyboard = true)
        dismissConnectionSheet()
        compose.waitForIdle()
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.onNodeWithText("连接电脑").assertDoesNotExist()
        assertConnectionStatus("离线")
        // Recomposition and a later failed reconnect must not reopen the sheet.
        compose.runOnIdle { state.value = state.value.copy(connection = ConnectionState.CONNECTING) }
        compose.runOnIdle { state.value = state.value.copy(connection = ConnectionState.DISCONNECTED) }
        connectionSheetHeading().assertDoesNotExist()
        compose.runOnIdle { assertEquals(1, actions.reconnectRequests) }
        capture("23-desktop-offline-preserves-draft.png", hideKeyboard = true)
        compose.onNodeWithText("手机聊天").performClick()
        compose.onNodeWithContentDescription("消息内容").assertTextContains("请看一下我的电脑桌面")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled().performClick()
        compose.runOnIdle { assertTrue(actions.desktopRequests.isEmpty()); assertEquals(listOf("请看一下我的电脑桌面"), actions.sent) }
    }

    @Test fun desktop_agent_accepts_an_explicit_photo_and_needs_no_phone_api() {
        val image = imageFixture()
        val state = mutableStateOf(fixtureState().copy(desktopTaskAvailable = true,
            pendingAttachment = UiPendingAttachment("qa-phone-photo", image.name, image.absolutePath, "qa-first")))
        val actions = RecordingActions(onRemove = { state.value = state.value.copy(pendingAttachment = null) },
            onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithText("电脑 Agent").performClick()
        compose.onNodeWithContentDescription("消息内容").performTextInput("帮我整理电脑桌面")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.onNodeWithText("将与消息一起交给电脑 Agent").assertDoesNotExist()
        compose.runOnIdle { assertTrue(actions.desktopRequests.isEmpty()); assertEquals(0, actions.removed) }
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(listOf("帮我整理电脑桌面"), actions.desktopRequests); assertTrue(actions.sent.isEmpty()) }
    }

    @Test fun accepting_an_old_desktop_turn_does_not_clear_a_new_conversation_draft() {
        val state = mutableStateOf(fixtureState().copy(directApi = directApiFixture(), desktopTaskAvailable = true))
        val actions = RecordingActions(onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithText("电脑 Agent").performClick()
        compose.onNodeWithContentDescription("消息内容").performTextInput("旧会话的电脑任务")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { state.value = state.value.copy(selectedConversationId = "qa-second", messages = emptyList(), desktopAgentMode = false) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("新会话保留这句话")
        compose.runOnIdle { requireNotNull(actions.desktopResult).invoke(true) }
        compose.onNodeWithContentDescription("消息内容").assertTextContains("新会话保留这句话")
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle { assertEquals(listOf("新会话保留这句话"), actions.sent) }
    }

    @Test fun first_desktop_task_rejection_preserves_the_draft_across_conversation_creation() {
        val state = mutableStateOf(fixtureState().copy(selectedConversationId = null, conversations = emptyList(),
            messages = emptyList(), directApi = directApiFixture(), desktopTaskAvailable = true))
        val actions = RecordingActions(onNew = {
            state.value = state.value.copy(selectedConversationId = "qa-manual-new",
                conversations = listOf(UiConversation("qa-manual-new", "手动新对话")), desktopAgentMode = false)
        }, onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithText("电脑 Agent").performClick()
        compose.onNodeWithContentDescription("消息内容").performTextInput("首次电脑任务，拒绝时保留这句话")
        compose.onNodeWithContentDescription("发送消息").performClick()
        compose.runOnIdle {
            // The result may arrive before Compose observes the newly created ID.
            state.value = state.value.copy(selectedConversationId = "qa-created-for-task",
                conversations = listOf(UiConversation("qa-created-for-task", "首次任务会话")))
            requireNotNull(actions.desktopResult).invoke(false)
        }
        compose.onNodeWithContentDescription("消息内容").assertTextContains("首次电脑任务，拒绝时保留这句话")
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
        capture("24-first-desktop-rejection-preserves-draft.png", hideKeyboard = true)
        newConversationFromMenu()
        assertTrue(compose.onNodeWithContentDescription("消息内容").fetchSemanticsNode()
            .config[SemanticsProperties.EditableText].text.isEmpty())
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.runOnIdle { assertTrue(actions.sent.isEmpty()) }
    }

    @Test fun choosing_agent_for_a_fresh_draft_preserves_text_when_mode_creates_the_conversation() {
        val state = mutableStateOf(fixtureState().copy(selectedConversationId = null, conversations = emptyList(),
            messages = emptyList(), directApi = directApiFixture(), desktopTaskAvailable = true))
        val actions = RecordingActions(onMode = { enabled ->
            state.value = state.value.copy(selectedConversationId = "qa-created-by-mode", desktopAgentMode = enabled,
                conversations = listOf(UiConversation("qa-created-by-mode", "新建 Agent 对话")))
        })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("选择电脑 Agent 前输入的任务")
        compose.onNodeWithText("电脑 Agent").performClick()
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
        compose.onNodeWithContentDescription("消息内容").assertTextContains("选择电脑 Agent 前输入的任务")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled().performClick()
        compose.runOnIdle {
            assertEquals(listOf("选择电脑 Agent 前输入的任务"), actions.desktopRequests)
            assertTrue(actions.sent.isEmpty())
            requireNotNull(actions.desktopResult).invoke(false)
        }
        compose.onNodeWithContentDescription("消息内容").assertTextContains("选择电脑 Agent 前输入的任务")
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
    }

    @Test fun attaching_a_photo_to_a_fresh_draft_preserves_text_when_picker_creates_the_conversation() {
        val image = imageFixture()
        val state = mutableStateOf(fixtureState().copy(selectedConversationId = null, conversations = emptyList(),
            messages = emptyList(), directApi = directApiFixture()))
        val actions = RecordingActions(onPick = {
            state.value = state.value.copy(selectedConversationId = "qa-created-by-photo",
                conversations = listOf(UiConversation("qa-created-by-photo", "新建图文对话")),
                pendingAttachment = UiPendingAttachment("qa-new-photo", image.name, image.absolutePath, "qa-created-by-photo"))
        })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("选择照片前输入的问题")
        compose.onNodeWithContentDescription("添加图片").performClick()
        compose.onNodeWithContentDescription("消息内容").assertTextContains("选择照片前输入的问题")
        compose.onNodeWithText("手机聊天").assertIsSelected()
        compose.onNodeWithContentDescription("移除待发送图片").assertIsDisplayed()
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        compose.runOnIdle { assertTrue(actions.sent.isEmpty()); assertTrue(actions.desktopRequests.isEmpty()) }
    }

    @Test fun desktop_agent_shows_task_progress_error_and_stop_separately_from_phone_chat() {
        val state = mutableStateOf(fixtureState().copy(desktopAgentMode = true, directApi = directApiFixture(),
            desktopTaskAvailable = true, desktopTaskRunning = true,
            desktopTaskActivity = "原始任务活动不显示在输入区", desktopTaskError = "原始错误不显示在输入区",
            messages = listOf(UiMessage("qa-agent-running", MessageRole.USER, "检查磁盘占用", MessageStatus.PENDING,
                detail = "正在执行电脑工具", desktopAgent = true))))
        val actions = RecordingActions(onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithText("正在执行电脑工具").assertDoesNotExist()
        compose.onNodeWithText("详情").performClick()
        compose.onNodeWithText("正在执行电脑工具").assertIsDisplayed()
        compose.onNodeWithText("原始任务活动不显示在输入区").assertDoesNotExist()
        compose.onNodeWithText("原始错误不显示在输入区").assertDoesNotExist()
        compose.onAllNodesWithText("正在执行电脑工具").assertCountEquals(1)
        compose.onNodeWithContentDescription("停止电脑任务").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, actions.stopRequests); assertTrue(actions.sent.isEmpty()) }
        capture("25-desktop-agent-progress-and-stop.png", hideKeyboard = true)
        compose.onNodeWithText("手机聊天").assertIsEnabled().performClick()
        compose.onNodeWithContentDescription("停止电脑任务").assertDoesNotExist()
        compose.onNodeWithContentDescription("消息内容").performTextInput("电脑执行时继续手机聊天")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(listOf("电脑执行时继续手机聊天"), actions.sent); assertEquals(1, actions.stopRequests) }
    }

    @Test fun desktop_agent_history_keeps_its_identity_in_phone_mode() {
        compose.setContent { AuraApp(fixtureState().copy(directApi = directApiFixture(), messages = listOf(
            UiMessage("qa-agent-request", MessageRole.USER, "整理文档", MessageStatus.PENDING,
                detail = "正在读取目录", desktopAgent = true),
            UiMessage("qa-agent-reply", MessageRole.ASSISTANT, "请确认保存位置。", desktopAgent = true))), RecordingActions()) }
        compose.onNodeWithText("正在处理…").assertIsDisplayed()
        compose.onNodeWithText("正在读取目录").assertDoesNotExist()
        compose.onNodeWithText("详情").performClick()
        compose.onNodeWithText("正在读取目录").assertIsDisplayed()
        compose.onNode(hasText("鲸伴") and hasAnyAncestor(hasTestTag("message-qa-agent-reply"))).assertIsDisplayed()
        compose.onNodeWithText("正在思考…").assertDoesNotExist()
    }

    @Test fun empty_conversation_preserves_freeform_input_without_extra_actions() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState().copy(messages = emptyList(),
            directApi = directApiFixture()), actions) }
        compose.onNodeWithText("今天想聊什么？").assertIsDisplayed()
        compose.onNodeWithText("陪我聊一会儿").assertDoesNotExist()
        compose.onNodeWithContentDescription("消息内容").performTextInput("由用户自由输入的内容")
        compose.onNodeWithContentDescription("消息内容").assertTextContains("由用户自由输入的内容")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        compose.runOnIdle { assertTrue(actions.sent.isEmpty()); assertTrue(actions.desktopRequests.isEmpty()) }
        capture("26-minimal-empty-light.png", hideKeyboard = true)
    }

    @Test fun liquid_mode_pills_keep_full_touch_targets_and_selection_with_large_type() {
        val state = mutableStateOf(fixtureState().copy(messages = emptyList(),
            directApi = directApiFixture(), desktopTaskAvailable = true, theme = ThemeMode.DARK))
        val actions = RecordingActions(onMode = { state.value = state.value.copy(desktopAgentMode = it) })
        compose.runOnUiThread {
            WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView).apply {
                isAppearanceLightStatusBars = false
                isAppearanceLightNavigationBars = false
            }
        }
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, 1.5f)) {
                AuraApp(state.value, actions)
            }
        }
        compose.onNodeWithText("手机聊天").assertIsSelected().assertHeightIsAtLeast(48.dp)
        compose.onNodeWithText("电脑 Agent").assertHeightIsAtLeast(48.dp).performClick()
        compose.onNodeWithText("电脑 Agent").assertIsSelected()
        compose.onNodeWithContentDescription("消息内容").assertIsDisplayed()
        compose.onNodeWithContentDescription("打开对话历史").assertIsDisplayed()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        compose.runOnIdle { assertTrue(actions.desktopRequests.isEmpty()); assertEquals(0, actions.screenshotRequests) }
        // Inspect rendered pixels, since semantics alone cannot detect inherited black text on dark glass.
        for (node in listOf(compose.onNodeWithText("今天想聊什么？"),
            compose.onNodeWithTag("conversation-title"),
            compose.onNodeWithContentDescription("打开对话历史"),
            compose.onNodeWithContentDescription("添加图片"))) {
            val pixels = node.assertIsDisplayed().captureToImage().toPixelMap()
            var visibleInk = 0
            for (y in 0 until pixels.height) for (x in 0 until pixels.width) {
                if (pixels[x, y].luminance() > .50f) visibleInk++
            }
            assertTrue("Dark glass foreground must render light, readable text and icons", visibleInk > 20)
        }
        capture("27-liquid-agent-dark-type150.png", hideKeyboard = true)
    }

    @Test fun minimal_header_keeps_the_conversation_title_and_an_independent_connection_action() {
        val state = mutableStateOf(fixtureState().copy(directApi = directApiFixture(), messages = emptyList(),
            desktopName = "我的电脑", desktopTaskAvailable = true, desktopAgentMode = true,
            desktopTaskMessage = "不要在模式下面显示连接说明", desktopTaskError = "不要在模式下面显示重复错误"))
        compose.setContent { AuraApp(state.value, RecordingActions()) }
        compose.onNodeWithTag("conversation-title").assertTextEquals("界面验收记录")
        compose.onNodeWithText("我的电脑（在线）").assertDoesNotExist()
        compose.onNodeWithTag("app-settings").assertHeightIsAtLeast(48.dp)
            .assertWidthIsAtLeast(48.dp)
        assertConnectionStatus("在线")
        compose.onNodeWithText("验收模型").assertDoesNotExist()
        compose.onNodeWithText("不要在模式下面显示连接说明").assertDoesNotExist()
        compose.onNodeWithText("不要在模式下面显示重复错误").assertDoesNotExist()
        compose.onNodeWithText("已保持电脑 Agent 模式；可切回手机聊天。").assertDoesNotExist()
        compose.onNodeWithText("在当前对话中持续使用电脑工具，下一条消息也会交给电脑 Agent。").assertDoesNotExist()
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
        capture("29-minimal-online-light.png", hideKeyboard = true)
        compose.runOnIdle { state.value = state.value.copy(connection = ConnectionState.DISCONNECTED,
            desktopTaskAvailable = false, directApi = directApiFixture()) }
        compose.onNodeWithTag("conversation-title").assertTextEquals("界面验收记录")
        assertConnectionStatus("离线")
        compose.onNodeWithText("我的电脑（在线）").assertDoesNotExist()
        compose.onNodeWithText("发送消息").assertDoesNotExist() // Icon-only action has an accessible description.
        compose.onNodeWithContentDescription("发送消息").assertIsNotEnabled()
        capture("30-minimal-offline-light.png", hideKeyboard = true)
        compose.onNodeWithText("查看电脑桌面").assertDoesNotExist()
    }

    @Test fun title_updates_with_renames_switching_and_new_chat_and_ellipsizes_at_large_type() {
        val longTitle = "讨论鲸鱼娘的桌面工具与接下来要完成的任务安排"
        val state = mutableStateOf(fixtureState().copy(messages = emptyList(), directApi = directApiFixture(),
            desktopTaskAvailable = true))
        val actions = RecordingActions(onSelect = { id -> state.value = state.value.copy(selectedConversationId = id) },
            onNew = { state.value = state.value.copy(selectedConversationId = null, messages = emptyList()) })
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, 1.5f)) {
                AuraApp(state.value, actions)
            }
        }
        compose.onNodeWithTag("conversation-title").assertTextEquals("界面验收记录")
        compose.runOnIdle { state.value = state.value.copy(conversations = state.value.conversations.map {
            if (it.id == "qa-first") it.copy(title = longTitle) else it
        }) }
        compose.onNodeWithTag("conversation-title").assertTextEquals(longTitle)
        val layouts = mutableListOf<TextLayoutResult>()
        compose.onNodeWithTag("conversation-title").performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
        assertTrue(layouts.single().lineCount == 1 && layouts.single().isLineEllipsized(0))
        capture("36-topic-header-light-type150.png", hideKeyboard = true)
        compose.onNodeWithTag("app-settings").assertIsDisplayed().assertHeightIsAtLeast(48.dp).performClick()
        connectionSheetHeading().assertIsDisplayed()
        compose.runOnIdle { assertEquals(0, actions.reconnectRequests); assertTrue(actions.desktopRequests.isEmpty()) }
        capture("38-connection-sheet-light-type150.png", hideKeyboard = true)
        dismissConnectionSheet()
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("第二段验收记录").performClick()
        compose.onNodeWithTag("conversation-title").assertTextEquals("第二段验收记录")
        newConversationFromMenu()
        compose.onNodeWithTag("conversation-title").assertTextEquals("新对话")
        compose.onNodeWithTag("app-settings").assertIsDisplayed()
        capture("37-topic-new-chat-light-type150.png", hideKeyboard = true)
    }

    @Test fun composer_contains_no_helper_or_status_text_in_any_chat_state() {
        val base = fixtureState().copy(messages = emptyList(), directApi = directApiFixture())
        val state = mutableStateOf(base)
        compose.setContent { AuraApp(state.value, RecordingActions()) }
        val states = listOf(base, base.copy(directChatBusy = true), base.copy(directApi = UiDirectApi()),
            base.copy(desktopAgentMode = true, desktopTaskAvailable = true),
            base.copy(desktopAgentMode = true, desktopTaskAvailable = false,
                desktopTaskMessage = "电脑离线说明", desktopTaskError = "重复任务错误"),
            base.copy(desktopAgentMode = true, desktopTaskAvailable = true, desktopTaskRunning = true,
                desktopTaskActivity = "活动进度说明"))
        for (sample in states) {
            compose.runOnIdle { state.value = sample }
            compose.waitForIdle()
            val labels = compose.onAllNodes(hasAnyAncestor(hasTestTag("message-composer")), useUnmergedTree = true)
                .fetchSemanticsNodes().flatMap {
                    if (it.config.contains(SemanticsProperties.Text)) it.config[SemanticsProperties.Text] else emptyList()
                }
                .map { it.text }.filter { it.isNotBlank() }.toSet()
            val placeholder = when {
                sample.desktopAgentMode && sample.desktopTaskRunning && sample.canSteerDesktop -> "补充当前任务…"
                sample.desktopAgentMode && !sample.desktopTaskAvailable ->
                    if (sample.connection == ConnectionState.CONNECTED) "电脑 Agent 暂不可用" else "电脑离线，连接后可执行任务"
                sample.desktopAgentMode -> "告诉电脑 Agent 下一步…"
                else -> "发消息给 鲸伴"
            }
            assertEquals(setOf("手机聊天", "电脑 Agent", placeholder), labels)
        }
    }

    @Test fun execution_process_runs_then_collapses_once_and_can_be_reopened() {
        val restoration = StateRestorationTester(compose)
        val process = executionFixture()
        val state = mutableStateOf(fixtureState().copy(messages = listOf(
            UiMessage("qa-process-user", MessageRole.USER, "检查一下 C 盘，只给建议"),
            UiMessage("qa-process", MessageRole.ASSISTANT, "", MessageStatus.PENDING,
                desktopAgent = true, execution = process)),
            desktopAgentMode = true, desktopTaskRunning = true, desktopTaskAvailable = true))
        restoration.setContent { AuraApp(state.value, RecordingActions()) }
        compose.onNodeWithTag("execution-toggle").assertIsDisplayed().assertHeightIsAtLeast(48.dp)
        compose.onNodeWithTag("execution-events").assertIsDisplayed()
        compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertExists()
        compose.onNodeWithText("检查磁盘占用").assertIsDisplayed()
        compose.onNodeWithContentDescription("停止电脑任务").assertIsEnabled()
        capture("31-execution-running-light.png", hideKeyboard = true)

        val completed = process.copy(state = "completed", durationMs = 12_400,
            events = process.events.map { it.copy(status = "completed") })
        compose.runOnIdle {
            state.value = state.value.copy(desktopTaskRunning = false, messages = listOf(
                state.value.messages.first(),
                state.value.messages.last().copy(content = "已经检查 C 盘占用，只整理了建议，没有清理文件。",
                    status = MessageStatus.SENT, execution = completed)))
        }
        compose.onNodeWithTag("execution-events").assertDoesNotExist()
        compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertDoesNotExist()
        assertExecutionState("已完成")
        compose.onNodeWithText("已经检查 C 盘占用，只整理了建议，没有清理文件。").assertIsDisplayed()
        capture("32-execution-completed-collapsed.png", hideKeyboard = true)
        compose.onNodeWithTag("execution-toggle").performClick()
        compose.onNodeWithTag("execution-events").assertIsDisplayed()
        compose.onNodeWithText("查看目录信息").assertIsDisplayed()
        compose.runOnIdle {
            // A later sync copy must not close the history after the user opened it.
            state.value = state.value.copy(messages = state.value.messages.map {
                if (it.execution != null) it.copy(execution = completed.copy(events = completed.events.toList())) else it
            })
        }
        compose.onNodeWithTag("execution-events").assertIsDisplayed()
        restoration.emulateSavedInstanceStateRestore()
        compose.onNodeWithTag("execution-events").assertIsDisplayed()
        capture("33-execution-completed-expanded.png", hideKeyboard = true)
    }

    @Test fun execution_terminal_states_keep_errors_visible_and_start_collapsed_at_large_type() {
        val process = mutableStateOf(executionFixture().copy(state = "failed", durationMs = 23_000))
        compose.runOnUiThread {
            WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView).apply {
                isAppearanceLightStatusBars = false
                isAppearanceLightNavigationBars = false
            }
        }
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, 1.5f)) {
                AuraApp(fixtureState().copy(theme = ThemeMode.DARK, messages = listOf(
                    UiMessage("qa-failed-user", MessageRole.USER, "检查磁盘"),
                    UiMessage("qa-failed-process", MessageRole.ASSISTANT, "", MessageStatus.FAILED,
                        detail = "本次任务已停止，已完成的操作会保留。", desktopAgent = true,
                        execution = process.value))), RecordingActions())
            }
        }
        assertExecutionState("未完成")
        compose.onNodeWithText("本次任务已停止，已完成的操作会保留。").assertDoesNotExist()
        compose.onNodeWithTag("execution-events").assertDoesNotExist()
        compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertDoesNotExist()
        capture("34-execution-failed-dark-type150.png", hideKeyboard = true)
        compose.onNodeWithTag("execution-toggle").performClick()
        compose.onNodeWithTag("execution-events").assertIsDisplayed()
        compose.onNodeWithTag("execution-detail").assertTextContains("已完成的操作会保留", substring = true)
        for ((index, status) in listOf("cancelled", "needs_user", "interrupted").withIndex()) {
            compose.runOnIdle { process.value = process.value.copy(state = status,
                startedAt = "2026-11-01T10:00:0${index + 1}+00:00") }
            compose.onNodeWithTag("execution-events").assertDoesNotExist()
            compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertDoesNotExist()
            compose.onNodeWithTag("execution-toggle").assertIsDisplayed().performClick()
            compose.onNodeWithTag("execution-events").assertIsDisplayed()
        }
    }

    @Test fun execution_running_history_is_bounded_until_user_asks_for_all_steps() {
        val process = executionFixture().copy(omittedCount = 13,
            events = List(8) { index -> UiExecutionStep("step-$index", "tool", "读取文件",
                if (index == 7) "running" else "completed", index * 1000L) })
        compose.setContent { AuraTheme { ExecutionProcess(process) } }
        compose.onNodeWithTag("execution-step-step-0").assertDoesNotExist()
        compose.onNodeWithTag("execution-step-step-7").assertExists()
        compose.onNodeWithText("查看全部 8 步").assertIsDisplayed().performClick()
        compose.onNodeWithTag("execution-step-step-0").assertExists()
        compose.onNodeWithText("另有 13 条较早记录").assertIsDisplayed()
    }

    @Test fun execution_glyph_animates_only_while_running_and_stops_at_completion() {
        val process = mutableStateOf(executionFixture())
        compose.mainClock.autoAdvance = false
        compose.setContent { AuraTheme { ExecutionProcess(process.value) } }
        compose.mainClock.advanceTimeBy(32)
        val before = compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).captureToImage().toPixelMap()
        compose.mainClock.advanceTimeBy(480)
        val after = compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).captureToImage().toPixelMap()
        if (android.animation.ValueAnimator.areAnimatorsEnabled()) {
            var changed = 0
            for (y in 0 until before.height) for (x in 0 until before.width) {
                if (before[x, y] != after[x, y]) changed++
            }
            assertTrue("The active glyph should visibly move", changed > 10)
        }
        compose.runOnIdle { process.value = process.value.copy(state = "completed", durationMs = 4300) }
        compose.mainClock.advanceTimeBy(32)
        compose.onNodeWithTag("execution-running-icon", useUnmergedTree = true).assertDoesNotExist()
        assertExecutionState("已完成")
        compose.mainClock.autoAdvance = true
    }

    @Test fun execution_events_expose_status_and_start_offsets_without_fake_step_durations() {
        val process = executionFixture().copy(durationMs = 65_000, events = listOf(
            UiExecutionStep("done", "tool", "已读取目录", "completed", 1500),
            UiExecutionStep("failed", "tool", "目录暂不可读", "failed", 63_500),
            UiExecutionStep("active", "tool", "正在整理已读取的信息", "running", 65_000)))
        compose.setContent { AuraTheme { ExecutionProcess(process) } }
        compose.onNodeWithTag("execution-step-done").assert(SemanticsMatcher.expectValue(
            SemanticsProperties.StateDescription, "已完成，任务开始后 00:01"))
        compose.onNodeWithTag("execution-step-failed").assert(SemanticsMatcher.expectValue(
            SemanticsProperties.StateDescription, "失败，任务开始后 01:03"))
        compose.onNodeWithTag("execution-step-active").assert(SemanticsMatcher.expectValue(
            SemanticsProperties.StateDescription, "执行中，任务开始后 01:05"))
        compose.onNodeWithText("+00:01").assertIsDisplayed()
        compose.onNodeWithText("+01:03").assertIsDisplayed()
        compose.onNodeWithText("+01:05").assertIsDisplayed()
        assertEquals("+00:00", executionOffset(-1000))
        assertEquals("+61:01", executionOffset(3_661_000))
    }

    private fun executionFixture() = UiExecution(state = "running", startedAt = "2026-11-01T10:00:00+00:00",
        durationMs = 2400, events = listOf(
            UiExecutionStep("step-0", "progress", "正在规划任务…", "completed", 0),
            UiExecutionStep("step-1", "tool", "检查磁盘占用", "completed", 1200),
            UiExecutionStep("step-2", "tool", "查看目录信息", "running", 2100)))

    private fun openConnectionSettings() {
        compose.onNodeWithTag("app-settings").performClick()
    }

    private fun newConversationFromMenu() {
        compose.onNodeWithContentDescription("打开对话历史").performClick()
        compose.onNodeWithText("新建对话").performClick()
    }

    private fun dismissConnectionSheet() {
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
        compose.waitForIdle()
    }

    private fun connectionSheetHeading() = compose.onNode(hasText("设置") and
        SemanticsMatcher.keyIsDefined(SemanticsProperties.Heading))

    private fun assertConnectionStatus(prefix: String) {
        // Header state changes must not add extra controls or move the topic.
        compose.onNodeWithTag("app-settings").assertIsDisplayed().assertIsEnabled()
        compose.onNodeWithTag("connection-status").assertDoesNotExist()
    }

    private fun openPendingQueue(count: Int) {
        openConnectionSettings()
        compose.onNodeWithTag("pending-messages-setting").performScrollTo()
        compose.onNodeWithText("查看 ($count)").performClick()
    }

    private fun assertExecutionState(expected: String) {
        compose.onNodeWithTag("execution-toggle").assert(SemanticsMatcher("Execution state contains $expected") {
            it.config[SemanticsProperties.StateDescription].contains(expected)
        })
    }

    @Test fun live_file_access_card_requires_explicit_review_and_never_uses_composer_text_as_approval() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState().copy(desktopAgentMode = true, desktopTaskRunning = true,
            desktopTaskAvailable = true, canSteerDesktop = true,
            agentInteraction = UiAgentInteraction("grant-1", "file_access", "读取这份文件吗？", "qa-first",
                resource = UiAgentFileResource("C:\\Selected\\input.csv", "read", false))), actions) }
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-interaction"))
        compose.onNodeWithTag("agent-resource-path").assertTextContains("C:\\Selected\\input.csv")
        compose.onNodeWithContentDescription("消息内容").performTextInput("普通草稿不能批准访问")
        assertTrue(actions.agentAnswers.isEmpty())
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
        compose.waitForIdle()
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-interaction"))
        compose.onNodeWithTag("agent-allow-access").performScrollTo().performClick()
        assertEquals(listOf("grant-1" to UiAgentAnswer.Permission(true)), actions.agentAnswers)
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-interaction"))
        compose.onNodeWithTag("agent-allow-access").assertIsNotEnabled()
        capture("46-agent-file-access.png", hideKeyboard = true)
    }

    @Test fun live_question_accepts_freeform_and_options_but_disappears_in_another_conversation() {
        val actions = RecordingActions()
        val state = mutableStateOf(fixtureState().copy(desktopAgentMode = true, desktopTaskRunning = true,
            desktopTaskAvailable = true, agentInteraction = UiAgentInteraction("question-1", "question",
                "选择哪个方案？", "qa-first", options = listOf("保留原件", "另存副本"))))
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-interaction"))
        compose.onNodeWithTag("agent-answer-input").performTextInput("先看一下文件内容")
        compose.onNodeWithTag("agent-answer-input").assert(SemanticsMatcher.expectValue(
            SemanticsProperties.EditableText, androidx.compose.ui.text.AnnotatedString("先看一下文件内容")))
        // Typing opens the IME. Wait for the native inset transition before a
        // physical click, so scrolling cannot move the button between down/up.
        compose.runOnUiThread {
            compose.activity.currentFocus?.clearFocus()
            WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView)
                .hide(WindowInsetsCompat.Type.ime())
        }
        SystemClock.sleep(350)
        compose.waitForIdle()
        compose.onNode(hasText("继续") and hasAnyAncestor(hasTestTag("agent-interaction")))
            .performScrollTo().assertIsEnabled().performClick()
        compose.waitUntil(5_000) { actions.agentAnswers.isNotEmpty() }
        assertEquals("question-1" to UiAgentAnswer.Text("先看一下文件内容"), actions.agentAnswers.single())
        compose.runOnIdle { state.value = state.value.copy(agentInteraction = state.value.agentInteraction!!.copy(id = "question-2")) }
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-interaction"))
        compose.onNodeWithText("另存副本").performClick()
        assertEquals("question-2" to UiAgentAnswer.Text("另存副本"), actions.agentAnswers.last())
        compose.runOnIdle { state.value = state.value.copy(selectedConversationId = "qa-second") }
        compose.onNodeWithTag("agent-interaction").assertDoesNotExist()
    }

    @Test fun running_task_can_receive_a_correction_without_starting_a_second_task() {
        val actions = RecordingActions()
        compose.setContent { AuraApp(fixtureState().copy(desktopAgentMode = true, desktopTaskRunning = true,
            desktopTaskAvailable = true, canSteerDesktop = true), actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("先别发送，另存一份")
        compose.onNodeWithContentDescription("停止电脑任务").assertIsDisplayed()
        compose.onNodeWithContentDescription("补充指令").performClick()
        assertEquals(listOf("先别发送，另存一份"), actions.steering)
        assertTrue(actions.desktopRequests.isEmpty())
        compose.onNodeWithContentDescription("消息内容").assert(SemanticsMatcher.expectValue(
            SemanticsProperties.EditableText, androidx.compose.ui.text.AnnotatedString("")))
    }

    @Test fun delivery_file_is_saved_only_after_explicit_click_and_shows_download_state() {
        val actions = RecordingActions()
        val artifact = UiAgentArtifact("artifact_" + "a".repeat(64), "验收报告.docx", 2048)
        val state = mutableStateOf(fixtureState().copy(agentArtifacts = listOf(artifact)))
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithTag("conversation-messages").performScrollToNode(hasTestTag("agent-artifact"))
        compose.onNodeWithText("验收报告.docx").assertIsDisplayed()
        assertTrue(actions.savedArtifacts.isEmpty())
        compose.onNodeWithText("保存").performClick()
        compose.runOnIdle { assertEquals(listOf(artifact.reference), actions.savedArtifacts) }
        compose.runOnIdle { state.value = state.value.copy(artifactDownloading = artifact.reference) }
        compose.onNodeWithText("保存").assertDoesNotExist()
        capture("47-agent-artifact.png", hideKeyboard = true)
    }

    private fun directApiFixture() = UiDirectApi(available = true,
        baseUrl = "https://example.invalid/v1", model = "验收模型", sync = ApiConfigSyncState.READY,
        source = ApiConfigSource.DESKTOP, keySaved = true)

    private fun imageFixture(): File {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        return File(context.cacheDir, "image-chat-fixture.png").also { file ->
            val bitmap = Bitmap.createBitmap(800, 1200, Bitmap.Config.ARGB_8888)
            val canvas = android.graphics.Canvas(bitmap)
            canvas.drawColor(android.graphics.Color.rgb(230, 225, 217))
            val paint = android.graphics.Paint().apply { color = android.graphics.Color.rgb(111, 114, 168) }
            canvas.drawCircle(400f, 480f, 240f, paint)
            file.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
            bitmap.recycle()
        }
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

private class RecordingActions(private val onConsume: () -> Unit = {},
                               private val onPick: () -> Unit = {}, private val onRemove: () -> Unit = {},
                               private val onNew: () -> Unit = {}, private val onMode: (Boolean) -> Unit = {},
                               private val onSelect: (String) -> Unit = {}) : UiActions {
    val pairings = mutableListOf<String>()
    val sent = mutableListOf<String>()
    val selected = mutableListOf<String>()
    val discarded = mutableListOf<String>()
    val openedImages = mutableListOf<String>()
    var consumed = 0
    var removed = 0
    var syncRequests = 0
    var reconnectRequests = 0
    var disconnectRequests = 0
    var savedConfig: UiApiConfigInput? = null
    val phoneQueueSends = mutableListOf<String>()
    val retried = mutableListOf<String>()
    val desktopRequests = mutableListOf<String>()
    var desktopResult: ((Boolean) -> Unit)? = null
    var screenshotRequests = 0
    var stopRequests = 0
    val agentAnswers = mutableListOf<Pair<String, UiAgentAnswer>>()
    val steering = mutableListOf<String>()
    val savedArtifacts = mutableListOf<String>()
    override fun saveArtifact(reference: String) { savedArtifacts += reference }
    override fun answerAgentInteraction(id: String, answer: UiAgentAnswer, onAccepted: (Boolean) -> Unit) {
        agentAnswers += id to answer; onAccepted(true)
    }
    override fun steerDesktopTask(text: String, onAccepted: (Boolean) -> Unit) {
        steering += text; onAccepted(true)
    }
    override fun setDesktopAgentMode(enabled: Boolean) { onMode(enabled) }
    override fun cancelDesktopTask() { stopRequests++ }
    override fun pair(payload: String) { pairings += payload }
    override fun consumePairInfo() { consumed++; onConsume() }
    override fun reconnect() { reconnectRequests++ }
    override fun disconnect() { disconnectRequests++ }
    override fun syncNow() { syncRequests++ }
    override fun selectConversation(id: String) { selected += id; onSelect(id) }
    override fun newConversation() { onNew() }
    override fun sendMessage(text: String) { sent += text }
    override fun retryMessage(id: String) { retried += id }
    override fun discardOutbox(requestId: String) { discarded += requestId }
    override fun pickImage() { onPick() }
    override fun removePendingImage() { removed++; onRemove() }
    override fun petCommand(action: String, value: PetValue) = Unit
    override fun refreshImages() = Unit
    override fun openImage(id: String) { openedImages += id }
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
    override fun saveDirectApiConfig(input: UiApiConfigInput) { savedConfig = input }
    override fun sendQueuedOnPhone(requestId: String) { phoneQueueSends += requestId }
    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) {
        desktopRequests += text
        desktopResult = onAccepted
    }
    override fun requestDesktopScreenshot() { screenshotRequests++ }
}
