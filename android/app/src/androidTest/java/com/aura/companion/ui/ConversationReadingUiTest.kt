package com.aura.companion.ui

import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Synthetic streaming text only. No repository, real conversation or gateway is opened. */
@RunWith(AndroidJUnit4::class)
class ConversationReadingUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun streamingLongReplyKeepsTheReaderPositionAndCanReturnToItsActualEnd() {
        require(InstrumentationRegistry.getInstrumentation().targetContext.packageName == "com.aura.companion.debug")
        val body = (1..65).joinToString("\n\n") { "Owned paragraph $it. Read this synthetic message without interruption." }
        val messages = mutableStateOf(listOf(UiMessage("long-owned-reply", MessageRole.ASSISTANT, body)))
        compose.setContent {
            AuraTheme(ThemeMode.LIGHT) {
                ConversationMessages(messages.value, Modifier.fillMaxSize(), {}, {}, true)
            }
        }
        compose.onNodeWithTag("conversation-tail").assertIsDisplayed()
        compose.onNodeWithTag("conversation-messages").performTouchInput {
            swipe(Offset(center.x, height * .3f), Offset(center.x, height * .75f), 600)
        }
        compose.onNodeWithTag("return-to-latest").assertIsDisplayed()
        fun position() = compose.onNodeWithTag("conversation-messages").fetchSemanticsNode()
            .config[SemanticsProperties.VerticalScrollAxisRange].value()
        val before = position()
        repeat(5) { chunk ->
            compose.runOnIdle { messages.value = listOf(messages.value.single().copy(content = body + "\n\nNew tail $chunk")) }
            compose.waitForIdle()
            assertEquals("Streaming must not reset the reader", before, position(), .001f)
        }
        compose.onNodeWithContentDescription("回到最新消息").performClick()
        compose.onNodeWithTag("conversation-tail").assertIsDisplayed()
        compose.runOnIdle { messages.value = listOf(messages.value.single().copy(content = body + "\n\nFinal latest tail")) }
        compose.onNodeWithTag("conversation-tail").assertIsDisplayed()
        compose.onNodeWithTag("return-to-latest").assertDoesNotExist()
    }
}
