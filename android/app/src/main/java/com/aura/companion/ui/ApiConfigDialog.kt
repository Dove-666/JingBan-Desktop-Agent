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
import com.aura.companion.data.ModelReasoning
import org.json.JSONObject

private data class ReasoningDraft(val profile: String, val value: String, val budget: String,
    val metadata: String?, val custom: Boolean)

@Composable
internal fun ApiConfigDialog(state: UiState, actions: UiActions, onDismiss: () -> Unit) {
    val api = state.directApi
    var baseUrl by remember { mutableStateOf(api.baseUrl) }
    var model by remember { mutableStateOf(api.model) }
    // Passwords are never remembered across recreation or populated from storage.
    var apiKey by remember { mutableStateOf("") }
    var timeout by remember { mutableStateOf(api.timeoutSeconds.toString()) }
    val editing = remember { ModelReasoning.forEditing(api.baseUrl, api.model, api.reasoningEffort, api.reasoningProfile,
        api.reasoningBudgetTokens, api.reasoningMetadata?.let { runCatching { JSONObject(it) }.getOrNull() },
        useHighestDefault = api.source == ApiConfigSource.NONE && !api.keySaved && !api.available) }
    var effort by remember { mutableStateOf(editing.getString("value")) }
    var profile by remember { mutableStateOf(editing.getString("profile")) }
    var budget by remember { mutableStateOf(editing.getInt("budget_tokens").takeIf { it > 0 }?.toString().orEmpty()) }
    var metadata by remember { mutableStateOf(editing.optJSONObject("metadata")?.toString()) }
    var customValue by remember { mutableStateOf(false) }
    var effortMenu by remember { mutableStateOf(false) }
    var profileMenu by remember { mutableStateOf(false) }
    var validation by remember { mutableStateOf<String?>(null) }
    var submittedGeneration by remember { mutableStateOf<Long?>(null) }
    val drafts = remember { linkedMapOf<String, ReasoningDraft>() }
    fun identity(base: String, name: String) = base.trim().trimEnd('/') + "\u0000" + name.trim()
    fun changeIdentity(base: String, name: String) {
        val previous = identity(baseUrl, model)
        val next = identity(base, name)
        if (previous != next) {
            drafts[previous] = ReasoningDraft(profile, effort, budget, metadata, customValue)
            while (drafts.size > 64) drafts.remove(drafts.keys.first())
            val restored = drafts[next]
            val default = if (restored == null) ModelReasoning.defaultSelection(base.trim(), name.trim()) else null
            profile = restored?.profile ?: "auto"
            effort = restored?.value ?: requireNotNull(default).getString("value")
            budget = restored?.budget ?: requireNotNull(default).getInt("budget_tokens").takeIf { it > 0 }?.toString().orEmpty()
            metadata = restored?.metadata
            customValue = restored?.custom ?: false
            validation = null
        }
        baseUrl = base
        model = name
    }
    val declared = remember(metadata) { metadata?.let { runCatching { JSONObject(it) }.getOrNull() } }
    val capability = remember(baseUrl, model, profile, metadata) {
        ModelReasoning.resolve(baseUrl.trim(), model.trim(), profile, declared)
    }
    val customEditing = customValue || profile == "custom" || capability.allowCustom && effort !in capability.values
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
                OutlinedTextField(baseUrl, { changeIdentity(it.take(2048), model) }, label = { Text("API 地址") },
                    placeholder = { Text("https://…") },
                    singleLine = true, enabled = !state.apiConfigSaving,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
                OutlinedTextField(model, { changeIdentity(baseUrl, it.take(200)) }, label = { Text("模型名称") },
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
                    OutlinedButton({ profileMenu = true }, enabled = !state.apiConfigSaving,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        Text("思考适配 · " + (ModelReasoning.profileChoices(profile == "legacy").firstOrNull { it.first == profile }?.second ?: profile))
                    }
                    DropdownMenu(profileMenu, { profileMenu = false }, modifier = Modifier.heightIn(max = 320.dp)) {
                        ModelReasoning.profileChoices(profile == "legacy").forEach { (value, label) ->
                            DropdownMenuItem(text = { Text(label) }, onClick = {
                                profile = value; effort = "default"; budget = ""; customValue = false
                                profileMenu = false; validation = null
                            })
                        }
                    }
                }
                Box {
                    OutlinedButton({ effortMenu = true }, enabled = !state.apiConfigSaving,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
                        Text("思考设置 · " + if (customEditing) "自定义值" else capability.labelFor(effort))
                    }
                    DropdownMenu(effortMenu, { effortMenu = false }, modifier = Modifier.heightIn(max = 320.dp)) {
                        capability.values.forEach { value -> DropdownMenuItem(text = { Text(capability.labelFor(value)) },
                            onClick = { effort = value; customValue = false; effortMenu = false; validation = null }) }
                        if (capability.allowCustom) DropdownMenuItem(text = { Text("自定义值…") }, onClick = {
                            if (effort in capability.values) effort = ""
                            customValue = true; effortMenu = false
                        })
                    }
                }
                if (customEditing) OutlinedTextField(if (effort == "default") "" else effort,
                    { effort = it.take(64); customValue = true }, label = { Text("接口规定的思考值") },
                    placeholder = { Text("例如 ultra") }, singleLine = true, enabled = !state.apiConfigSaving,
                    supportingText = { Text("按接口文档原值发送，不转换为其它档位。") }, modifier = Modifier.fillMaxWidth())
                if (capability.control == "budget" && effort == "enabled") OutlinedTextField(budget,
                    { budget = it.filter(Char::isDigit).take(6) }, label = { Text("思考预算（Token）") },
                    placeholder = { Text(capability.budgetDefault.toString()) }, singleLine = true, enabled = !state.apiConfigSaving,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    supportingText = { Text("${capability.budgetMin}～${capability.budgetMax}；留空使用 ${capability.budgetDefault}。") },
                    modifier = Modifier.fillMaxWidth())
                if (profile == "auto" && capability.source != "unknown") Text(
                    when (capability.source) { "metadata" -> "接口能力声明"; "provider" -> "接口适配"; else -> "模型预设" } + " · " + capability.label,
                    style = MaterialTheme.typography.bodySmall)
                if (capability.notice.isNotBlank()) Text(capability.notice, style = MaterialTheme.typography.bodySmall)
                if (api.reasoningProfile == "legacy") Text("旧配置将按当前模型适配，保存后生效。", style = MaterialTheme.typography.bodySmall)
                (validation ?: state.apiConfigError)?.let { Text(it, color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
            }
        },
        confirmButton = {
            TextButton({
                val seconds = timeout.toIntOrNull()
                val reasoningBudget = if (capability.control == "budget" && effort == "enabled") budget.toIntOrNull() ?: 0 else 0
                validation = when {
                    baseUrl.isBlank() || model.isBlank() -> "请填写 API 地址和模型名称。"
                    !api.keySaved && apiKey.isBlank() -> "请填写 API Key。"
                    seconds == null || seconds !in 1..120 -> "请求超时请输入 1～120 秒。"
                    else -> runCatching { ModelReasoning.validate(baseUrl.trim(), model.trim(), effort, profile, reasoningBudget, declared) }
                        .exceptionOrNull()?.let { it.message ?: "思考设置无效，请重新选择。" }
                }
                if (validation == null && seconds != null) {
                    submittedGeneration = state.apiConfigSaveGeneration
                    actions.saveDirectApiConfig(UiApiConfigInput(baseUrl.trim(), model.trim(), apiKey, seconds, effort,
                        profile, reasoningBudget, ModelReasoning.sanitizedMetadata(baseUrl.trim(), model.trim(), declared)?.toString()))
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

