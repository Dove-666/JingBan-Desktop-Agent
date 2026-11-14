package com.aura.companion.ui

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp-xhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class PhoneAttachmentUiTest {
    @get:Rule val compose = createComposeRule()

    @Test fun artifactRendersBetweenItsAnswerAndTheNextUserMessage() {
        val messages = mutableStateOf(listOf(
            UiMessage("answer-1", MessageRole.ASSISTANT, "Your document is ready."),
            UiMessage("question-2", MessageRole.USER, "Open the app"),
            UiMessage("answer-2", MessageRole.ASSISTANT, "The app is open.")))
        val files = listOf(UiAgentArtifact("artifact-owned", "report.txt", 4096, messageId = "answer-1"))
        compose.setContent {
            AuraTheme(ThemeMode.LIGHT) {
                ConversationMessages(messages.value, Modifier.fillMaxSize(), {}, {}, true, artifacts = files)
            }
        }
        fun verifyOrder() {
            val reply = compose.onNodeWithTag("message-answer-1").fetchSemanticsNode().boundsInRoot
            val file = compose.onNodeWithTag("agent-artifact").fetchSemanticsNode().boundsInRoot
            val next = compose.onNodeWithTag("message-question-2").fetchSemanticsNode().boundsInRoot
            assertTrue(reply.bottom <= file.top)
            assertTrue(file.bottom <= next.top)
        }
        verifyOrder()
        compose.runOnIdle { messages.value += UiMessage("question-3", MessageRole.USER, "Thanks") }
        verifyOrder()
    }

    @Test fun fileMenuPreservesDraftAndStagesAFileWithoutSending() {
        val state = mutableStateOf(UiState(paired = true,
            connection = ConnectionState.CONNECTED, selectedConversationId = "owned-chat",
            desktopAgentMode = true, desktopTaskAvailable = true, theme = ThemeMode.LIGHT))
        val actions = object : QuietActions() {
            override fun pickFile() {
                state.value = state.value.copy(pendingAttachment = UiPendingAttachment("file-owned", "notes.pdf", "",
                    "owned-chat", isFile = true, sizeBytes = 2048, mimeType = "application/pdf"))
            }
            override fun removePendingImage() { state.value = state.value.copy(pendingAttachment = null) }
        }
        compose.setContent { AuraApp(state.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("Read these notes")
        compose.onNodeWithContentDescription("添加附件").performClick()
        compose.onNodeWithText("文件").performClick()
        compose.onNodeWithText("notes.pdf").assertIsDisplayed()
        compose.onNodeWithText("2 KB").assertIsDisplayed()
        compose.onNodeWithContentDescription("消息内容").assertTextContains("Read these notes")
        compose.onNodeWithContentDescription("发送消息").assertIsEnabled()
        compose.onNodeWithContentDescription("移除待发送文件").performClick()
        compose.onNodeWithText("notes.pdf").assertDoesNotExist()
        compose.onNodeWithContentDescription("消息内容").assertTextContains("Read these notes")
        assertEquals(0, actions.sends)
    }

    private open class QuietActions : UiActions {
        var sends = 0
        override fun pair(payload: String) = Unit
        override fun consumePairInfo() = Unit
        override fun reconnect() = Unit
        override fun disconnect() = Unit
        override fun syncNow() = Unit
        override fun selectConversation(id: String) = Unit
        override fun newConversation() = Unit
        override fun sendMessage(text: String) { sends++ }
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
        override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { sends++; onAccepted(false) }
        override fun requestDesktopScreenshot() = Unit
    }
}
