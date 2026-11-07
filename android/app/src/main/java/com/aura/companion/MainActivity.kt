package com.aura.companion

import android.content.Intent
import android.os.Bundle
import android.os.Build
import android.Manifest
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.runtime.getValue
import androidx.compose.runtime.SideEffect
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.core.view.WindowCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aura.companion.ui.AuraApp
import com.aura.companion.ui.UiActions
import com.aura.companion.ui.ThemeMode

class MainActivity : ComponentActivity() {
    private val model by viewModels<AuraViewModel>()
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { allowed ->
        model.setTaskNotifications(allowed)
    }
    private val picker = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        model.attachImage(uri)
    }
    private val artifactDestination = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        model.finishArtifactSave(if (result.resultCode == RESULT_OK) result.data?.data else null)
    }
    private val actions by lazy { object : UiActions by model {
        override fun setTaskNotifications(enabled: Boolean) {
            if (enabled && Build.VERSION.SDK_INT >= 33 && !TaskNotifications.allowed(this@MainActivity))
                notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            else model.setTaskNotifications(enabled)
        }
        override fun pickImage() { model.beginImageSelection { picker.launch("image/*") } }
        override fun saveArtifact(reference: String) {
            model.prepareArtifact(reference) { name, mime ->
                try {
                    artifactDestination.launch(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                        .setType(mime).putExtra(Intent.EXTRA_TITLE, name))
                } catch (_: Exception) { model.finishArtifactSave(null) }
            }
        }
    } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        acceptIntent(intent)
        setContent {
            val state by model.state.collectAsStateWithLifecycle()
            val dark = when (state.theme) { ThemeMode.DARK -> true; ThemeMode.LIGHT -> false; else -> isSystemInDarkTheme() }
            SideEffect {
                WindowCompat.getInsetsController(window, window.decorView).apply {
                    isAppearanceLightStatusBars = !dark
                    isAppearanceLightNavigationBars = !dark
                }
            }
            AuraApp(state, actions, model.conversationDraftStore)
        }
    }

    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); acceptIntent(intent) }
    private fun acceptIntent(value: Intent?) {
        if (value?.data?.scheme == "aura" && value.data?.host == "pair") model.acceptPairInfo(value.dataString)
        value?.getStringExtra("conversation_id")?.let { id ->
            value.removeExtra("conversation_id")
            if (com.aura.companion.data.AppRepository.canonicalUuid(id)) model.selectConversation(id)
        }
    }
    override fun onStart() { super.onStart(); model.setForeground(true) }
    override fun onStop() { model.setForeground(false); super.onStop() }
}
