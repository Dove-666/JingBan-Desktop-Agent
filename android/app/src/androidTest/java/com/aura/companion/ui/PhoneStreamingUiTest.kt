package com.aura.companion.ui

import android.graphics.Bitmap
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.core.view.WindowInsetsControllerCompat
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Controlled rendered fixture; transport and persistence are tested separately. */
@RunWith(AndroidJUnit4::class)
class PhoneStreamingUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun phoneReplyUpdatesInlineAndStopUsesPhoneActionWhileComputerOffline() {
        compose.runOnUiThread {
            compose.activity.enableEdgeToEdge()
            WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView).apply {
                isAppearanceLightStatusBars = true; isAppearanceLightNavigationBars = true
            }
        }
        var stops = 0
        val actions = object : EmptyStreamingActions() { override fun cancelDirectChat() { stops++ } }
        val state = mutableStateOf(UiState(theme = ThemeMode.LIGHT, connection = ConnectionState.DISCONNECTED,
            directApi = UiDirectApi(available = true, model = "合成界面验收"),
            conversations = listOf(UiConversation("streaming-fixture", "鲸鱼的旅行计划")),
            selectedConversationId = "streaming-fixture", directChatBusy = true, directChatConversationId = "streaming-fixture",
            messages = listOf(UiMessage("user", MessageRole.USER, "帮我计划一次海边旅行。"),
                UiMessage("assistant", MessageRole.ASSISTANT, "我们先挑一个安静的海边", MessageStatus.SENDING,
                    execution = UiExecution("running", "2026-11-02T00:00:00Z", 1700,
                        listOf(UiExecutionStep("model-request", "model", "手机模型正在生成回复", "running", 0)))))))
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithTag("conversation-title").assertTextEquals("鲸鱼的旅行计划").assertIsDisplayed()
        compose.onNodeWithText("我们先挑一个安静的海边").assertIsDisplayed()
        compose.onNodeWithContentDescription("停止回复").assertIsDisplayed().assertIsEnabled()
        compose.runOnIdle {
            state.value = state.value.copy(messages = state.value.messages.dropLast(1) +
                state.value.messages.last().copy(content = "我们先挑一个安静的海边，再留半天看日落。"))
        }
        compose.onNodeWithText("我们先挑一个安静的海边，再留半天看日落。").assertIsDisplayed()
        compose.onNodeWithContentDescription("停止回复").performClick()
        compose.runOnIdle { assertEquals(1, stops) }
        compose.waitForIdle()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val target = File(instrumentation.targetContext.filesDir, "qa/phone-streaming-fixture.png")
        target.parentFile!!.mkdirs()
        val screenshot = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        target.outputStream().use { screenshot.compress(Bitmap.CompressFormat.PNG, 100, it) }; screenshot.recycle()
    }
}

private open class EmptyStreamingActions : UiActions {
    override fun pair(payload: String) = Unit
    override fun consumePairInfo() = Unit
    override fun reconnect() = Unit
    override fun disconnect() = Unit
    override fun syncNow() = Unit
    override fun selectConversation(id: String) = Unit
    override fun newConversation() = Unit
    override fun sendMessage(text: String) = Unit
    override fun retryMessage(id: String) = Unit
    override fun discardOutbox(requestId: String) = Unit
    override fun pickImage() = Unit
    override fun removePendingImage() = Unit
    override fun petCommand(action: String, value: PetValue) = Unit
    override fun refreshImages() = Unit
    override fun openImage(id: String) = Unit
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
    override fun saveDirectApiConfig(input: UiApiConfigInput) = Unit
    override fun sendQueuedOnPhone(requestId: String) = Unit
    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { onAccepted(false) }
    override fun requestDesktopScreenshot() = Unit
}
