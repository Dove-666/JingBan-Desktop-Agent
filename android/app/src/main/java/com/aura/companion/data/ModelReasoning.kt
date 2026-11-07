package com.aura.companion.data

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

/** Shared declarative catalogue; no network inference, probing or retry is performed. */
object ModelReasoning {
    data class Capability(val id: String, val label: String, val source: String, val control: String,
        val values: List<String>, val valueLabels: List<String>, val allowCustom: Boolean = false,
        val budgetMin: Int = 0, val budgetMax: Int = 0, val budgetDefault: Int = 0,
        val budgetRequired: Boolean = false, val notice: String = "", val temperaturePolicy: String = "preserve") {
        fun labelFor(value: String): String = valueLabels.getOrNull(values.indexOf(value)) ?: value
    }

    private val token = Regex("[A-Za-z][A-Za-z0-9_-]{0,63}")
    private val legacyValues = setOf("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")
    private const val VALUE_ERROR = "当前模型或接口不支持所选思考设置，请选择可用选项或显式自定义协议。"
    private const val BUDGET_ERROR = "思考预算无效，请按当前接口的范围设置。"
    private val catalogue: JSONObject by lazy {
        val stream = ModelReasoning::class.java.classLoader?.getResourceAsStream("reasoning/profiles.json")
            ?: throw IllegalArgumentException("思考参数目录不可用，请更新或修复应用。")
        stream.bufferedReader(Charsets.UTF_8).use { JSONObject(it.readText()) }.also {
            require(it.optInt("version") == 1 && it.optJSONObject("profiles") != null) { "思考参数目录版本无效，请更新应用。" }
        }
    }

    private fun JSONObject.fieldNames(): Set<String> = keys().asSequence().toSet()
    private fun JSONArray.texts(): List<String> = (0 until length()).map { getString(it) }
    private fun profiles() = catalogue.getJSONObject("profiles")
    private fun row(profile: String): JSONObject = profiles().optJSONObject(profile)
        ?: throw IllegalArgumentException("思考参数协议无效，请重新选择。")

    fun profileChoices(includeLegacy: Boolean = false): List<Pair<String, String>> {
        val order = listOf("auto", "openai", "deepseek", "thinking", "enable_thinking", "qwen_budget", "openrouter",
            "gemini", "claude_adaptive", "claude_budget", "custom", "omit", "legacy")
        return profiles().fieldNames().filter { includeLegacy || it != "legacy" }.sortedBy {
            order.indexOf(it).takeIf { i -> i >= 0 } ?: order.size
        }.map { it to row(it).getString("label") }
    }

    private data class UrlIdentity(val scheme: String, val host: String, val port: Int, val path: String)
    private fun identity(baseUrl: String): UrlIdentity? = runCatching {
        if (baseUrl.length > 2048 || baseUrl.any { it.code < 32 }) return null
        val parsed = URI(baseUrl)
        val scheme = parsed.scheme?.lowercase()
        if (scheme !in setOf("http", "https") || parsed.host.isNullOrEmpty() || parsed.rawUserInfo != null ||
            parsed.rawQuery != null || parsed.rawFragment != null) return null
        if (parsed.port !in -1..65535) return null
        UrlIdentity(requireNotNull(scheme), parsed.host.lowercase(), if (parsed.port == -1) (if (scheme == "https") 443 else 80) else parsed.port,
            parsed.rawPath.orEmpty().trimEnd('/'))
    }.getOrNull()

    private fun capability(profile: String, source: String, values: List<String>? = null): Capability {
        val declared = row(profile)
        val choices = values ?: declared.getJSONArray("values").texts()
        val labels = catalogue.getJSONObject("labels")
        return Capability(profile, declared.getString("label"), source, declared.getString("control"), choices,
            choices.map { labels.optString(it, it) }, declared.optBoolean("allow_custom", false),
            declared.optInt("budget_min", 0), declared.optInt("budget_max", 0), declared.optInt("budget_default", 0),
            declared.optBoolean("budget_required", false), declared.optString("notice", ""), declared.optString("temperature_policy", "preserve"))
    }

    /** Only exact host/model-bound declarations can restrict an already-known wire protocol. */
    fun sanitizedMetadata(baseUrl: String, model: String, raw: Any?): JSONObject? = runCatching {
        val data = raw as? JSONObject ?: return null
        if (data.fieldNames() != setOf("version", "base_url", "model", "profile", "values") || data.opt("version") != 1) return null
        val declaredBase = data.opt("base_url") as? String ?: return null
        val declaredModel = data.opt("model") as? String ?: return null
        if (identity(baseUrl) == null || identity(baseUrl) != identity(declaredBase) || model != declaredModel) return null
        val profile = data.opt("profile") as? String ?: return null
        if (!profiles().has(profile) || profile in setOf("auto", "legacy", "custom")) return null
        val rawValues = data.optJSONArray("values") ?: return null
        if (rawValues.length() !in 1..16) return null
        val values = (0 until rawValues.length()).map { rawValues.opt(it) as? String ?: return null }
        if (values.any { !token.matches(it) }) return null
        val declared = row(profile)
        val actual = (listOf("default") + values).distinct()
        if (declared.getString("control") in setOf("none", "toggle", "budget") &&
            actual.any { it !in declared.getJSONArray("values").texts() }) return null
        JSONObject().put("version", 1).put("base_url", declaredBase).put("model", declaredModel)
            .put("profile", profile).put("values", JSONArray(values))
    }.getOrNull()

    fun resolve(baseUrl: String, model: String, profile: String = "auto", metadata: JSONObject? = null): Capability {
        row(profile) // Unknown profile is a configuration error, never an arbitrary request field.
        if (profile != "auto") return capability(profile, "explicit")
        sanitizedMetadata(baseUrl, model, metadata)?.let {
            val inferred = resolve(baseUrl, model, "auto")
            val cap = capability(it.getString("profile"), "metadata", (listOf("default") + it.getJSONArray("values").texts()).distinct())
            return cap.copy(allowCustom = false,
                temperaturePolicy = if (cap.id == inferred.id && inferred.temperaturePolicy == "omit") "omit" else cap.temperaturePolicy)
        }
        val url = identity(baseUrl)
        val name = model.trim().lowercase()
        val rules = catalogue.getJSONArray("auto_rules")
        for (i in 0 until rules.length()) {
            val rule = rules.getJSONObject(i)
            val match = rule.getJSONObject("match")
            if (match.has("hosts") && (url == null || url.host !in match.getJSONArray("hosts").texts())) continue
            if (match.has("paths") && (url == null || match.getJSONArray("paths").texts().none { url.path == it || url.path.startsWith("$it/") })) continue
            val modelNames = match.optJSONArray("models")?.texts().orEmpty()
            val knownDate = match.optBoolean("allow_date_suffix", false) && modelNames.any {
                Regex(Regex.escape(it) + "-[0-9]{4}-[0-9]{2}-[0-9]{2}").matches(name)
            }
            if ((match.has("models") || match.has("model_prefixes")) &&
                !(name in modelNames || knownDate ||
                    match.optJSONArray("model_prefixes")?.texts().orEmpty().any { name.startsWith(it) })) continue
            return capability(rule.getString("profile"), rule.getString("source"), rule.optJSONArray("values")?.texts())
                .copy(allowCustom = false, temperaturePolicy = rule.optString("temperature_policy", row(rule.getString("profile")).optString("temperature_policy", "preserve")))
        }
        return capability("auto", "unknown").copy(notice = "尚无此模型和接口的能力声明，默认不发送思考参数；可显式选择兼容协议。")
    }

    fun selection(profile: String, value: String, budgetTokens: Int = 0, metadata: JSONObject? = null): JSONObject {
        validateShape(profile, value, budgetTokens)
        return JSONObject().put("version", 1).put("profile", profile).put("value", value).put("budget_tokens", budgetTokens)
            .apply { metadata?.let { put("metadata", JSONObject(it.toString())) } }
    }

    /** Only unset choices use this default; declared value arrays need not be ordered. */
    fun defaultSelection(baseUrl: String, model: String, profile: String = "auto", metadata: JSONObject? = null): JSONObject {
        val declared = sanitizedMetadata(baseUrl, model, metadata)
        val cap = resolve(baseUrl, model, profile, declared)
        val effortOrder = listOf("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
        val value = when {
            cap.control == "effort" -> effortOrder.lastOrNull { it in cap.values } ?: "default"
            cap.control in setOf("toggle", "budget") && "enabled" in cap.values -> "enabled"
            else -> "default"
        }
        // A token budget also has to fit the request's eventual output/context allowance.
        val budget = if (cap.control == "budget" && value == "enabled") cap.budgetDefault else 0
        return selection(profile, value, budget, declared)
    }

    /** An editor preview never rewrites a saved legacy configuration before Save. */
    fun forEditing(baseUrl: String, model: String, value: String, profile: String, budgetTokens: Int = 0,
        metadata: JSONObject? = null, useHighestDefault: Boolean = false): JSONObject {
        val nextProfile = if (profile == "legacy") "auto" else profile
        val declared = sanitizedMetadata(baseUrl, model, metadata)
        if (useHighestDefault) return defaultSelection(baseUrl, model, nextProfile, declared)
        val cap = resolve(baseUrl, model, nextProfile, declared)
        val nextValue = if (value in cap.values || cap.allowCustom) value else "default"
        val nextBudget = if (cap.control == "budget" && nextValue == "enabled") budgetTokens else 0
        return selection(nextProfile, nextValue, nextBudget, declared)
    }

    fun parseSelection(raw: Any?, legacyValue: String, baseUrl: String, model: String): JSONObject {
        if (raw == null || raw === JSONObject.NULL) {
            require(legacyValue in legacyValues) { "电脑 API 推理配置无效" }
            return selection(if (legacyValue == "default") "auto" else "legacy", legacyValue)
        }
        val data = raw as? JSONObject ?: throw IllegalArgumentException("思考参数格式无效，请更新模型配置。")
        require(data.fieldNames().all { it in setOf("version", "profile", "value", "budget_tokens", "metadata") } && data.opt("version") == 1) {
            "思考参数版本或字段无效，请更新模型配置。"
        }
        val profile = data.opt("profile") as? String ?: throw IllegalArgumentException("思考参数协议无效，请重新选择。")
        val value = data.opt("value") as? String ?: throw IllegalArgumentException(VALUE_ERROR)
        val rawBudget = data.opt("budget_tokens") ?: 0
        require(rawBudget is Int && rawBudget in 0..262144) { BUDGET_ERROR }
        val metadata = sanitizedMetadata(baseUrl, model, data.opt("metadata"))
        validate(baseUrl, model, value, profile, rawBudget, metadata)
        return selection(profile, value, rawBudget, metadata)
    }

    private fun validateShape(profile: String, value: String, budgetTokens: Int) {
        val cap = capability(profile, "explicit")
        require(token.matches(value)) { VALUE_ERROR }
        require(budgetTokens in 0..262144) { BUDGET_ERROR }
        if (profile != "auto") {
            require(value in cap.values || cap.allowCustom) { VALUE_ERROR }
            require(if (cap.control == "budget") budgetTokens == 0 || budgetTokens in cap.budgetMin..cap.budgetMax else budgetTokens == 0) { BUDGET_ERROR }
        }
    }

    fun validate(baseUrl: String, model: String, value: String, profile: String = "auto", budgetTokens: Int = 0,
        metadata: JSONObject? = null): Capability {
        validateShape(profile, value, budgetTokens)
        val cap = resolve(baseUrl, model, profile, metadata)
        require(value in cap.values || cap.allowCustom) { VALUE_ERROR }
        require(if (cap.control == "budget") budgetTokens == 0 || budgetTokens in cap.budgetMin..cap.budgetMax else budgetTokens == 0) { BUDGET_ERROR }
        return cap
    }

    fun body(baseUrl: String, model: String, value: String = "default", profile: String = "auto", budgetTokens: Int = 0,
        metadata: JSONObject? = null): JSONObject {
        val cap = validate(baseUrl, model, value, profile, budgetTokens, metadata)
        if (value == "default") return JSONObject()
        return when (row(cap.id).getString("wire")) {
            "effort" -> JSONObject().put("reasoning_effort", value)
            "deepseek" -> JSONObject().put("thinking", JSONObject().put("type", if (value == "none") "disabled" else "enabled"))
                .apply { if (value != "none") put("reasoning_effort", value) }
            "thinking" -> JSONObject().put("thinking", JSONObject().put("type", if (value == "none") "disabled" else "enabled"))
            "enable_thinking" -> JSONObject().put("enable_thinking", value != "none")
            "qwen_budget" -> JSONObject().put("enable_thinking", value != "none")
                .apply { if (value != "none") put("thinking_budget", budgetTokens.takeIf { it != 0 } ?: cap.budgetDefault) }
            "openrouter" -> JSONObject().put("reasoning", if (value == "none") JSONObject().put("enabled", false) else JSONObject().put("effort", value))
            "claude_adaptive" -> JSONObject().put("thinking", JSONObject().put("type", "adaptive"))
                .put("output_config", JSONObject().put("effort", value))
            "claude_budget" -> JSONObject().put("thinking", JSONObject().put("type", if (value == "none") "disabled" else "enabled")
                .apply { if (value != "none") put("budget_tokens", budgetTokens.takeIf { it != 0 } ?: cap.budgetDefault) })
            else -> throw IllegalArgumentException(VALUE_ERROR)
        }
    }
}
