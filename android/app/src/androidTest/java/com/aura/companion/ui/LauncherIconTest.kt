package com.aura.companion.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.drawable.AdaptiveIconDrawable
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aura.companion.BuildConfig
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Render the actual resource through Android's adaptive mask for visual QA. */
@RunWith(AndroidJUnit4::class)
class LauncherIconTest {
    @Test fun whale_icon_is_a_real_adaptive_icon_with_centered_artwork() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val icon = context.applicationInfo.loadIcon(context.packageManager)
        assertTrue("Launcher icon must retain Android adaptive masking", icon is AdaptiveIconDrawable)
        val bitmap = Bitmap.createBitmap(432, 432, Bitmap.Config.ARGB_8888)
        icon.setBounds(0, 0, bitmap.width, bitmap.height)
        icon.draw(Canvas(bitmap))
        assertTrue("Icon center must not be transparent", Color.alpha(bitmap.getPixel(216, 216)) > 240)
        val directory = File(context.filesDir, "qa/ui-${BuildConfig.VERSION_NAME.removeSuffix("-debug")}").apply { mkdirs() }
        File(directory, "28-whale-launcher-adaptive.png").outputStream().use {
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
        }
        bitmap.recycle()
    }
}
