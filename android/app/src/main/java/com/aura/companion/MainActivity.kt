package com.aura.companion

import android.content.Intent
import android.os.Bundle
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
    private val picker = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        model.attachImage(uri)
    }
    private val actions by lazy { object : UiActions by model {
        override fun pickImage() { model.beginImageSelection { picker.launch("image/*") } }
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
            AuraApp(state, actions)
        }
    }

    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); acceptIntent(intent) }
    private fun acceptIntent(value: Intent?) {
        if (value?.data?.scheme == "aura" && value.data?.host == "pair") model.acceptPairInfo(value.dataString)
    }
    override fun onStart() { super.onStart(); model.setForeground(true) }
    override fun onStop() { model.setForeground(false); super.onStop() }
}
