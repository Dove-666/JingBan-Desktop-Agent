package com.aura.companion.ui

import android.graphics.Bitmap
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Role UI fixtures only: no repository, gateway, real prompts or provider calls. */
@RunWith(AndroidJUnit4::class)
class CharacterCardsUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Before fun ownedActivityOnly() {
        require(InstrumentationRegistry.getInstrumentation().targetContext.packageName == "com.aura.companion.debug")
        compose.runOnUiThread {
            compose.activity.window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
            compose.activity.enableEdgeToEdge()
        }
    }

    private fun fixture() = UiState(paired = true, connection = ConnectionState.CONNECTED,
        theme = ThemeMode.LIGHT, selectedConversationId = "owned-role-conversation",
        conversations = listOf(UiConversation("owned-role-conversation", "我的角色对话")),
        directApi = UiDirectApi(available = true, model = "owned-model", source = ApiConfigSource.LOCAL),
        activeCharacterId = "default", activeCharacterName = "鲸鱼娘", characterLibrarySupported = true,
        characterCards = listOf(UiCharacterCard(ROLE_A, "海月 · 温柔", "海月"),
            UiCharacterCard(ROLE_B, "星澜 · 活泼", "星澜")))

    @Test fun settingsEntrySwitchAndReturnKeepTheCurrentConversationDraftAndApi() {
        val value = mutableStateOf(fixture())
        val actions = object : CharacterActions() {
            override fun selectCharacter(id: String) {
                super.selectCharacter(id)
                value.value = value.value.copy(activeCharacterId = id, activeCharacterName = "海月")
            }
        }
        compose.setContent { AuraApp(value.value, actions) }
        compose.onNodeWithContentDescription("消息内容").performTextInput("换角色也要留住这段草稿")
        hideKeyboard()
        compose.onNodeWithTag("app-settings").performClick()
        screenshot("settings-role-entry-light.png")
        compose.onNodeWithTag("character-setting").performScrollTo().performClick()
        compose.onNodeWithTag("character-cards-sheet").assertIsDisplayed()
        assertEquals(1, actions.refreshes)
        screenshot("character-library-light.png")
        compose.onNodeWithTag("character-card-$ROLE_A").performClick()
        assertEquals(listOf(ROLE_A), actions.selections)
        back()
        if (compose.onAllNodesWithTag("settings-sheet").fetchSemanticsNodes().isNotEmpty()) back()
        compose.onNodeWithContentDescription("消息内容").assertTextEquals("换角色也要留住这段草稿")
        compose.onNodeWithTag("conversation-title").assertTextEquals("我的角色对话")
        assertEquals("owned-model", value.value.directApi.model)
        assertEquals(ApiConfigSource.LOCAL, value.value.directApi.source)
        assertTrue(actions.messages.isEmpty())
    }

    @Test fun onlineCharacterAndDefaultChoicesReflectOnlyTheReturnedState() {
        val value = mutableStateOf(fixture())
        val actions = object : CharacterActions() {
            override fun selectCharacter(id: String) {
                super.selectCharacter(id)
                value.value = value.value.copy(activeCharacterId = id,
                    activeCharacterName = if (id == "default") "鲸鱼娘" else "海月")
            }
        }
        compose.setContent { AuraTheme(ThemeMode.LIGHT) {
            LiquidScene { CharacterCardsSheet(value.value, actions, onDismiss = {}, onConnect = {}) }
        } }
        compose.onNodeWithTag("character-card-default").assertIsSelected()
        compose.onNodeWithTag("character-card-$ROLE_A").performClick()
        compose.onNodeWithTag("character-card-$ROLE_A").assertIsSelected()
        compose.onNodeWithTag("character-card-default").assertIsNotSelected().performClick()
        compose.onNodeWithTag("character-card-default").assertIsSelected()
        assertEquals(listOf(ROLE_A, "default"), actions.selections)
    }

    @Test fun offlineCachedRolesRemainVisibleWithoutPretendingToChangeTheDesktop() {
        val actions = CharacterActions()
        var connections = 0
        val value = fixture().copy(connection = ConnectionState.DISCONNECTED,
            activeCharacterId = ROLE_A, activeCharacterName = "海月")
        compose.setContent { AuraTheme(ThemeMode.DARK) {
            LiquidScene { CharacterCardsSheet(value, actions, onDismiss = {}, onConnect = { connections++ }) }
        } }
        compose.onNodeWithTag("character-card-$ROLE_A").assertIsDisplayed().assertIsSelected().assertIsNotEnabled()
        compose.onNodeWithTag("character-card-$ROLE_B").assertIsDisplayed().assertIsNotEnabled()
        compose.onNodeWithTag("character-card-default").assertIsNotEnabled()
        screenshot("character-library-offline-dark.png")
        compose.onNodeWithTag("character-card-$ROLE_B").performTouchInput { click() }
        assertTrue(actions.selections.isEmpty())
        compose.onNodeWithTag("character-connect").performClick()
        assertEquals(1, connections)
    }

    @Test fun syncFailureKeepsTheActualRoleAndOffersExplicitRefresh() {
        val value = mutableStateOf(fixture().copy(activeCharacterId = ROLE_A, activeCharacterName = "海月",
            characterSyncError = "电脑角色已更新，请重新选择"))
        val actions = CharacterActions()
        compose.setContent { AuraTheme(ThemeMode.LIGHT) {
            LiquidScene { CharacterCardsSheet(value.value, actions, onDismiss = {}, onConnect = {}) }
        } }
        compose.onNodeWithText("电脑角色已更新，请重新选择").assertIsDisplayed()
        compose.onNodeWithTag("character-card-$ROLE_A").assertIsSelected()
        compose.onNodeWithTag("character-refresh").performClick()
        assertEquals(1, actions.refreshes)
        compose.onNodeWithTag("character-card-$ROLE_A").assertIsSelected()
        assertTrue(actions.selections.isEmpty())
        compose.runOnIdle { value.value = value.value.copy(characterSyncError = null,
            activeCharacterId = ROLE_B, activeCharacterName = "星澜") }
        compose.onNodeWithTag("character-card-$ROLE_B").assertIsSelected()
        compose.onNodeWithText("电脑角色已更新，请重新选择").assertDoesNotExist()
    }

    @Test fun busyRoleSynchronizationDisablesRepeatedChangesAndRefresh() {
        val value = mutableStateOf(fixture().copy(characterSyncBusy = true))
        val actions = object : CharacterActions() {
            override fun selectCharacter(id: String) {
                super.selectCharacter(id)
                value.value = value.value.copy(characterSyncBusy = true)
            }
        }
        compose.setContent { AuraTheme(ThemeMode.LIGHT) {
            LiquidScene { CharacterCardsSheet(value.value, actions, onDismiss = {}, onConnect = {}) }
        } }
        compose.onNodeWithTag("character-refresh").assertIsNotEnabled()
        compose.onNodeWithTag("character-card-$ROLE_A").assertIsNotEnabled().performTouchInput { click() }
        assertTrue(actions.selections.isEmpty())
        compose.runOnIdle { value.value = value.value.copy(characterSyncBusy = false) }
        compose.onNodeWithTag("character-card-$ROLE_A").performClick()
        compose.onNodeWithTag("character-card-$ROLE_A").assertIsNotEnabled().performTouchInput { click() }
        assertEquals(listOf(ROLE_A), actions.selections)
    }

    private fun hideKeyboard() {
        compose.runOnUiThread {
            compose.activity.currentFocus?.clearFocus()
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
            val dark = name.contains("-dark")
            androidx.core.view.WindowInsetsControllerCompat(compose.activity.window, compose.activity.window.decorView).apply {
                isAppearanceLightStatusBars = !dark
                isAppearanceLightNavigationBars = !dark
            }
        }
        compose.mainClock.advanceTimeBy(300)
        compose.waitForIdle()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.waitForIdleSync()
        android.os.SystemClock.sleep(250)
        instrumentation.waitForIdleSync()
        val directory = File(instrumentation.targetContext.filesDir, "qa/character-cards-ui").apply { mkdirs() }
        val image = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { File(directory, name).outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) } }
        finally { image.recycle() }
    }

    private companion object {
        const val ROLE_A = "11111111111111111111111111111111"
        const val ROLE_B = "22222222222222222222222222222222"
    }
}

private open class CharacterActions : UiActions {
    var refreshes = 0
    val selections = mutableListOf<String>()
    val messages = mutableListOf<String>()
    override fun refreshCharacters() { refreshes++ }
    override fun selectCharacter(id: String) { selections += id }
    override fun pair(payload: String) = Unit
    override fun consumePairInfo() = Unit
    override fun reconnect() = Unit
    override fun disconnect() = Unit
    override fun syncNow() = Unit
    override fun selectConversation(id: String) = Unit
    override fun newConversation() = Unit
    override fun sendMessage(text: String) { messages += text }
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
