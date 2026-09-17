package com.feiyu.app.data.settings

import com.feiyu.app.data.persona.Persona

/** 全部用户设置（持久化到 DataStore）。 */
data class Settings(
    val providerId: String = "deepseek",
    val baseUrl: String = "https://api.deepseek.com/v1",
    val apiKey: String = "",
    val model: String = "deepseek-flash",
    val temperature: Float = 0.9f,

    // 角色人设（名字 + 预设来源 + 五个字段，全部可自定义）
    val characterName: String = Persona.DEFAULT_PRESET.name,
    val personaPresetId: String = "custom",   // 用户改过字段后置为 custom
    val identity: String = Persona.DEFAULT_PRESET.fields["identity"].orEmpty(),
    val personality: String = Persona.DEFAULT_PRESET.fields["personality"].orEmpty(),
    val abilities: String = Persona.DEFAULT_PRESET.fields["abilities"].orEmpty(),
    val habits: String = Persona.DEFAULT_PRESET.fields["habits"].orEmpty(),
    val extra: String = Persona.DEFAULT_PRESET.fields["extra"].orEmpty(),

    // 向量记忆
    val memoryEnabled: Boolean = true,
    val embeddingBaseUrl: String = "",
    val embeddingApiKey: String = "",
    val embeddingModel: String = "text-embedding-3-small",
    val topK: Int = 5,
    val memoryLimit: Int = 800,       // 记忆条数上限（超出按重要性淘汰）
    val dedupThreshold: Float = 0.95f, // 相似度超过则合并去重

    // 会话窗口与摘要
    val historyWindow: Int = 12,      // 每次发给模型的最近对话轮数（1 轮 = 2 条消息）
    val summaryEnabled: Boolean = true, // 窗口外的旧对话自动摘要沉淀

    // 图片识别
    val visionEnabled: Boolean = true,  // 允许发送图片
    val visionModel: String = "deepseek-flash",  // 视觉模型名（默认与主模型一致）

    // 语音播报
    val ttsEnabled: Boolean = true,          // 显示朗读按钮
    val ttsAutoSpeak: Boolean = false,       // 回复完自动朗读
    val ttsUseCloud: Boolean = false,        // 用云端 TTS（否则用系统 TTS）
    val ttsBaseUrl: String = "",             // 云端 TTS 地址（OpenAI 兼容 /v1）
    val ttsApiKey: String = "",
    val ttsModel: String = "tts-1",          // 云端语音模型
    val ttsVoice: String = "alloy",          // 云端音色
    val ttsSpeed: Float = 1.0f,              // 语速 0.5 ~ 2.0（两端引擎都适用）
    val ttsPitch: Float = 1.0f,              // 音调 0.5 ~ 2.0（仅系统 TTS 支持）

    // 停顿节奏
    val ttsSentencePause: Int = 120,         // 句间停顿（毫秒）
    val ttsParagraphPause: Int = 320,        // 段间停顿（毫秒）

    // 情感参数（云端服务商大多支持，写法各异，故做成可配键值 + 服务商预设）
    val ttsEmotionKey: String = "",          // 参数名，如 emotion / voice_style
    val ttsEmotionValue: String = "",        // 参数值，如 happy / cheerful
    val ttsEmotionPresetId: String = "custom",

    // 朗读改写
    val ttsRewriteEnabled: Boolean = false,  // 朗读前用大模型改写得更口语化（耗额外 token）
) {
    /** 人设字段汇总成 Map（供 Persona 使用） */
    fun profileMap(): Map<String, String> = mapOf(
        "identity" to identity,
        "personality" to personality,
        "abilities" to abilities,
        "habits" to habits,
        "extra" to extra,
    )

    /** 角色显示名（空则用中性兜底）。 */
    fun displayName(): String = characterName.trim().ifBlank { Persona.DEFAULT_NAME }

    /** 实际用于请求的模型名（配了视觉模型且在发图时用视觉模型）。 */
    fun modelFor(withImage: Boolean): String {
        val vm = visionModel.trim()
        return if (withImage && vm.isNotEmpty()) vm else model
    }

    /** 视觉模型的展示名（用于提示用户该配哪个模型）。 */
    fun visionModelLabel(): String = visionModel.trim().ifBlank { model }

    /** 是否使用远程 embedding 端点 */
    fun useRemoteEmbedding(): Boolean = embeddingBaseUrl.isNotBlank() && embeddingApiKey.isNotBlank()
}
