package com.aura.companion.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Surface
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.unit.dp
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp-xhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class MarkdownRenderTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun multiParagraphQuoteUsesOneContinuousRailAndNoPrintedQuoteMarkers() {
        val source = "**故事梗概**（剧透预警）：\n\n> 老李去参加毕业二十年的同学会，偷偷租了豪车。\n>\n> 这时，助理推门进来，带来了账单。\n>\n> 而那家公司的老板，是当年班里的张小满。\n\n笑点落在真诚上。"
        compose.setContent {
            AuraTheme { Surface(Modifier.fillMaxSize()) {
                Column(Modifier.padding(22.dp).verticalScroll(rememberScrollState())) {
                    SelectionContainer { MessageText(source) }
                }
            } }
        }
        compose.onAllNodesWithTag("markdown-quote").assertCountEquals(1)
        compose.onNodeWithText("老李去参加毕业二十年的同学会，偷偷租了豪车。").assertIsDisplayed()
        compose.onNodeWithText("这时，助理推门进来，带来了账单。").assertIsDisplayed()
        compose.onNodeWithText(">", substring = true).assertDoesNotExist()
        val quote = compose.onNodeWithTag("markdown-quote").fetchSemanticsNode().boundsInRoot
        val after = compose.onNodeWithText("笑点落在真诚上。").fetchSemanticsNode().boundsInRoot
        assertTrue(after.top > quote.bottom)
        screenshot("quote-light.png")
    }

    @Test fun streamedMarkdownCanGrowIntoListsCodeAndHorizontallyScrollableTable() {
        val source = mutableStateOf("> 正在整理")
        compose.setContent {
            AuraTheme(ThemeMode.DARK) { Surface(Modifier.fillMaxSize()) {
                Column(Modifier.padding(20.dp).verticalScroll(rememberScrollState())) { MessageText(source.value) }
            } }
        }
        compose.onNodeWithText("正在整理").assertIsDisplayed()
        compose.runOnIdle { source.value = "## 结果\n\n1. 第一项\n   - 子项\n2. 第二项\n\n```text\ncode > keeps symbols\n```\n\n| A | B | C | D |\n|---|---|---|---|\n| 1 | 2 | 3 | 4 |" }
        compose.onAllNodesWithTag("markdown-list").assertCountEquals(2)
        compose.onNodeWithText("code > keeps symbols").assertIsDisplayed()
        compose.onNodeWithTag("markdown-table").assertIsDisplayed()
        compose.onNodeWithText("复制代码").assertIsDisplayed()
        screenshot("blocks-dark.png")
    }

    private fun screenshot(name: String) {
        // Robolectric has no display compositor for PixelCopy. Draw the actual
        // measured Android/Compose view into its native Skia canvas instead.
        lateinit var bitmap: Bitmap
        compose.runOnIdle {
            val view = compose.activity.window.decorView
            bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
        }
        assertTrue(bitmap.width > 100 && bitmap.height > 100)
        val colors = mutableSetOf<Int>()
        for (y in 0 until bitmap.height step 3) for (x in 0 until bitmap.width step 3) colors += bitmap.getPixel(x, y)
        assertTrue("Capture must contain painted content", colors.size > 20)
        val output = File("../../artifacts/android/markdown-render/$name").canonicalFile
        output.parentFile!!.mkdirs()
        output.outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) }
    }
}
