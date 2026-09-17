package com.feiyu.app.data.settings

/** 大模型供应商预设（OpenAI 兼容 /chat/completions）。 */
data class ProviderPreset(
    val id: String,
    val label: String,
    val baseUrl: String,
    val defaultModel: String,
)

val PROVIDER_PRESETS: List<ProviderPreset> = listOf(
    ProviderPreset(
        id = "deepseek",
        label = "DeepSeek",
        baseUrl = "https://api.deepseek.com/v1",
        defaultModel = "deepseek-flash",
    ),
    ProviderPreset(
        id = "gemini",
        label = "Gemini（OpenAI 兼容）",
        baseUrl = "https://generativelanguage.googleapis.com/v1beta/openai",
        defaultModel = "gemini-1.5-flash",
    ),
    ProviderPreset(
        id = "glm",
        label = "智谱 GLM",
        baseUrl = "https://open.bigmodel.cn/api/paas/v4",
        defaultModel = "glm-4-flash",
    ),
    ProviderPreset(
        id = "openai",
        label = "OpenAI",
        baseUrl = "https://api.openai.com/v1",
        defaultModel = "gpt-4o-mini",
    ),
    ProviderPreset(
        id = "custom",
        label = "自定义",
        baseUrl = "",
        defaultModel = "",
    ),
)

fun presetById(id: String): ProviderPreset? = PROVIDER_PRESETS.firstOrNull { it.id == id }
