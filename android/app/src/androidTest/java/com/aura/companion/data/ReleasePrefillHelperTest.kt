package com.aura.companion.data

import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Explicit QA helper only; -e releasePrefill true is public, not a secret. */
@RunWith(AndroidJUnit4::class)
class ReleasePrefillHelperTest {
    @Test fun prefillReleaseFromPrivateFixtureConfiguration() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        assumeTrue("Run this helper explicitly with releasePrefill=true",
            InstrumentationRegistry.getArguments().getString("releasePrefill") == "true")
        val context = instrumentation.targetContext
        val configuration = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Renew the isolated fixture pairing first", configuration.isFile)
        val input = JSONObject(configuration.readText())
        require(input.optBoolean("isolated_fixture"))
        val intent = Intent(Intent.ACTION_VIEW).apply {
            component = ComponentName("com.aura.companion", "com.aura.companion.MainActivity")
            data = Uri.parse(input.getString("pairing_uri"))
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        }
        context.startActivity(intent)
        // MainActivity accepts this URI only as a prefill. The user/test
        // operator still has to press the release app's confirmation button.
        File(context.filesDir, "aura-release-prefill-result.json").writeText(JSONObject()
            .put("prefilled", true).put("target_package", "com.aura.companion")
            .put("auto_submit", false).put("fixture_generation", input.optInt("fixture_generation")).toString())
    }
}
