package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.window.SecureFlagPolicy

@Composable
internal fun ApiConfigDialog(state: UiState, actions: UiActions, onDismiss: () -> Unit) {
    val api = state.directApi
    var baseUrl by remember { mutableStateOf(api.baseUrl) }
    var model by remember { mutableStateOf(api.model) }
    // Passwords are never remembered across recreation or populated from storage.
    var apiKey by remember { mutableStateOf("") }
    var timeout by remember { mutableStateOf(api.timeoutSeconds.toString()) }
    var effort by remember { mutableStateOf(api.reasoningEffort) }
    var effortMenu by remember { mutableStateOf(false) }
    var validation by remember { mutableStateOf<String?>(null) }
    var submittedGeneration by remember { mutableStateOf<Long?>(null) }
    LaunchedEffect(state.apiConfigSaveGeneration) {
        if (submittedGeneration != null && state.apiConfigSaveGeneration != submittedGeneration) {
            apiKey = ""
            onDismiss()
        }
    }
    fun dismiss() { apiKey = ""; onDismiss() }
    AlertDialog(onDismissRequest = ::dismiss,
        title = { Text("手机模型配置") },
        text = {
            Column(Modifier.fillMaxWidth().heightIn(max = 480.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("手机直接连接模型，文字和图片都不需要等待电脑在线。",
                    style = MaterialTheme.typography.bodyMedium)
                OutlinedTextField(baseUrl, { baseUrl = it.take(2048) }, label = { Text("API 地址") },
                    placeholder = { Text("https://…") },
                    singleLine = true, enabled = !state.apiConfigSaving,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
                OutlinedTextField(model, { model = it.take(200) }, label = { Text("模型名称") },
                    singleLine = true, enabled = !state.apiConfigSaving, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(apiKey, { apiKey = it.take(16384) }, label = { Text("API Key") },
                    singleLine = true, enabled = !state.apiConfigSaving,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, keyboardType = KeyboardType.Password),
                    supportingText = { Text(if (api.keySaved) "已保存密钥；留空会保留原密钥。" else "密钥仅在保存时使用，不会回填或显示。") },
                    modifier = Modifier.fillMaxWidth().semantics { contentDescription = "API Key" })
                OutlinedTextField(timeout, { timeout = it.filter(Char::isDigit).take(3) }, label = { Text("请求超时（秒）") },
                    singleLine = true, enabled = !state.apiConfigSaving,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.fillMaxWidth())
                Box {
                    OutlinedButton({ effortMenu = true }, enabled = !state.apiConfigSaving,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        Text("思考强度 · ${effortLabels[effort] ?: "默认"}")
                    }
                    DropdownMenu(effortMenu, { effortMenu = false }, modifier = Modifier.heightIn(max = 320.dp)) {
                        effortLabels.forEach { (value, label) -> DropdownMenuItem(text = { Text(label) },
                            onClick = { effort = value; effortMenu = false }) }
                    }
                }
                (validation ?: state.apiConfigError)?.let { Text(it, color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
            }
        },
        confirmButton = {
            TextButton({
                val seconds = timeout.toIntOrNull()
                validation = when {
                    baseUrl.isBlank() || model.isBlank() -> "请填写 API 地址和模型名称。"
                    !api.keySaved && apiKey.isBlank() -> "请填写 API Key。"
                    seconds == null || seconds !in 1..120 -> "请求超时请输入 1～120 秒。"
                    else -> null
                }
                if (validation == null && seconds != null) {
                    submittedGeneration = state.apiConfigSaveGeneration
                    actions.saveDirectApiConfig(UiApiConfigInput(baseUrl.trim(), model.trim(), apiKey, seconds, effort))
                }
            }, enabled = !state.apiConfigSaving, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (state.apiConfigSaving) "正在保存…" else "保存配置")
            }
        },
        dismissButton = { TextButton(::dismiss, Modifier.heightIn(min = 48.dp)) { Text("取消") } },
        properties = DialogProperties(securePolicy = SecureFlagPolicy.SecureOn),
        shape = RoundedCornerShape(30.dp), tonalElevation = 0.dp,
        containerColor = glassSheetColor())
}

private val effortLabels = linkedMapOf("default" to "默认", "none" to "关闭", "minimal" to "最少",
    "low" to "低", "medium" to "中", "high" to "高", "xhigh" to "很高", "max" to "最高")
