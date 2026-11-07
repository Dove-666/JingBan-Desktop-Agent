package com.aura.companion.ui

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.lang.reflect.Proxy

/** Pure form fixtures; never constructs the app repository, network client or a real key. */
@RunWith(AndroidJUnit4::class)
class ReasoningConfigUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()
    private var saved: UiApiConfigInput? = null
    private val actions: UiActions by lazy {
        Proxy.newProxyInstance(UiActions::class.java.classLoader, arrayOf(UiActions::class.java)) { proxy, method, args ->
            when (method.name) {
                "equals" -> proxy === args?.getOrNull(0)
                "hashCode" -> System.identityHashCode(proxy)
                "toString" -> "ReasoningFormActions"
                "saveDirectApiConfig" -> { saved = args?.get(0) as UiApiConfigInput; null }
                else -> null
            }
        } as UiActions
    }
    private fun display(model: String = "deepseek-flash", value: String = "default", profile: String = "auto") {
        val state = UiState(directApi = UiDirectApi(baseUrl = "https://fixture.example/v1", model = model, keySaved = true,
            reasoningEffort = value, reasoningProfile = profile))
        compose.setContent { AuraTheme(ThemeMode.LIGHT) { ApiConfigDialog(state, actions) {} } }
    }

    @Test fun aNewApiUsesTheHighestRecognizedTierAndSubmitsTheDisplayedChoice() {
        val state = UiState(directApi = UiDirectApi(baseUrl = "https://fixture.example/v1", model = "deepseek-flash"))
        compose.setContent { AuraTheme(ThemeMode.LIGHT) { ApiConfigDialog(state, actions) {} } }
        compose.onNodeWithText("思考设置 · 最高").performScrollTo().assertIsDisplayed()
        compose.onNode(hasSetTextAction() and hasText("API Key")).performScrollTo().performTextInput("SYNTHETIC_FORM_ONLY_KEY")
        compose.onNodeWithText("保存配置").performClick()
        compose.runOnIdle {
            assertEquals("max", saved!!.reasoningEffort)
            assertEquals("auto", saved!!.reasoningProfile)
        }
    }

    @Test fun aNewModelUsesItsOwnMaximumAndAnExplicitOffDraftRestores() {
        display(value = "none")
        compose.onNodeWithText("思考设置 · 关闭思考").performScrollTo().assertIsDisplayed()
        val model = compose.onNode(hasSetTextAction() and hasText("模型名称"))
        model.performScrollTo().performTextReplacement("gpt-5.4")
        compose.onNodeWithText("思考设置 · 很高").performScrollTo().assertIsDisplayed()
        model.performScrollTo().performTextReplacement("deepseek-flash")
        compose.onNodeWithText("思考设置 · 关闭思考").performScrollTo().assertIsDisplayed()
    }

    @Test fun modelChangeRebuildsOptionsAndRestoresItsOwnDraft() {
        display()
        compose.onNodeWithText("思考设置 · 默认（由模型决定）").performScrollTo().performClick()
        compose.onNodeWithText("中", useUnmergedTree = true).assertDoesNotExist()
        compose.onNodeWithText("高").performClick()
        val model = compose.onNode(hasSetTextAction() and hasText("模型名称"))
        model.performScrollTo().performTextReplacement("ordinary-alias")
        compose.onNodeWithText("思考设置 · 默认（由模型决定）").performScrollTo().performClick()
        compose.onNodeWithText("高").assertDoesNotExist()
        compose.onNodeWithText("最高").assertDoesNotExist()
        compose.onNodeWithText("默认（由模型决定）").performClick()
        model.performScrollTo().performTextReplacement("deepseek-flash")
        compose.onNodeWithText("思考设置 · 高").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("保存配置").performClick()
        compose.runOnIdle {
            assertEquals("auto", saved!!.reasoningProfile)
            assertEquals("high", saved!!.reasoningEffort)
            assertTrue(saved!!.apiKey.isEmpty())
        }
    }

    @Test fun budgetControlHasItsOwnValueAndSavesItsActualTokenCount() {
        display("private-alias")
        compose.onNodeWithText("思考适配 · 自动适配").performScrollTo().performClick()
        compose.onNodeWithText("兼容思考预算").performScrollTo().performClick()
        compose.onNodeWithText("思考设置 · 默认（由模型决定）").performScrollTo().performClick()
        compose.onNodeWithText("开启思考").performClick()
        compose.onNode(hasSetTextAction() and hasText("思考预算（Token）")).performScrollTo().performTextInput("16384")
        compose.onNodeWithText("保存配置").performClick()
        compose.runOnIdle {
            assertEquals("qwen_budget", saved!!.reasoningProfile)
            assertEquals("enabled", saved!!.reasoningEffort)
            assertEquals(16384, saved!!.reasoningBudgetTokens)
        }
    }

    @Test fun unknownCustomEffortIsAnExplicitSelectionAndIsNeverRenamed() {
        display("private-alias")
        compose.onNodeWithText("思考适配 · 自动适配").performScrollTo().performClick()
        compose.onNodeWithText("自定义 effort").performScrollTo().performClick()
        compose.onNode(hasSetTextAction() and hasText("接口规定的思考值")).performScrollTo().performTextInput("ultra")
        compose.onNodeWithText("保存配置").performClick()
        compose.runOnIdle {
            assertEquals("custom", saved!!.reasoningProfile)
            assertEquals("ultra", saved!!.reasoningEffort)
            assertEquals(0, saved!!.reasoningBudgetTokens)
        }
    }

    @Test fun aLegacySelectionOpensAsAutomaticOptionsAndOnlyChangesWhenSaved() {
        display("ordinary-alias", "max", "legacy")
        compose.onNodeWithText("思考适配 · 自动适配").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("思考设置 · 默认（由模型决定）").performScrollTo().performClick()
        compose.onNodeWithText("最高").assertDoesNotExist()
        compose.onNodeWithText("中").assertDoesNotExist()
        compose.onNodeWithText("默认（由模型决定）").performClick()
        compose.runOnIdle { assertNull(saved) }
        compose.onNodeWithText("保存配置").performClick()
        compose.runOnIdle {
            assertEquals("auto", saved!!.reasoningProfile)
            assertEquals("default", saved!!.reasoningEffort)
        }
    }
}
