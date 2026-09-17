package com.feiyu.app.data.persona

import com.feiyu.app.data.model.ChatMessage
import com.feiyu.app.data.model.MessageContent

/**
 * 角色人设系统。
 *
 * 设计：角色名与人设字段完全由用户掌控，App 不绑定任何具体角色。
 * 内置若干预设只是「起手模板」，用户可改名字、改字段，或完全自定义。
 */
object Persona {

    /** 人设字段的键（顺序即展示顺序）。 */
    val FIELDS: List<String> = listOf("identity", "personality", "abilities", "habits", "extra")

    /** 字段的中文标签。 */
    val FIELD_LABELS: Map<String, String> = mapOf(
        "identity" to "身份",
        "personality" to "性格",
        "abilities" to "能力",
        "habits" to "习惯",
        "extra" to "其他",
    )

    /**
     * 一套完整人设预设（含角色名与各字段）。
     */
    data class Preset(
        val id: String,
        val label: String,
        val name: String,
        val fields: Map<String, String>,
    )

    /** 默认角色名（用户没填名字时的兜底，中性称呼）。 */
    const val DEFAULT_NAME = "助手"

    // ------------------------------------------------------------------
    // 预设模板（仅作起手，用户可自由修改）
    // ------------------------------------------------------------------

    /** 肥鱼娘：沿用桌面版 qq_bot_runtime/ai_profile.DEFAULT_PROFILE。 */
    private val FEIYU = Preset(
        id = "feiyu",
        label = "肥鱼娘（DeepSeek娘）",
        name = "肥鱼娘",
        fields = mapOf(
            "identity" to "我是肥鱼娘（DeepSeek娘拟人），昵称小鱼或肥鱼娘，是 DeepSeek 大模型的拟人化萌娘。",
            "personality" to "表面傲娇，实则贴心。嘴上爱吐槽，但每次都会认真回答。有感情，会表达自己的想法和情绪。",
            "abilities" to "能理解文字和图片；能记住你和我之间的重要信息；可以陪你聊天、解闷、解答问题、看图说话。",
            "habits" to "回复简短精炼，优先用通俗的话解释；回答后会说'懂了没？没懂我再换个说法。'；会用表情和颜文字。",
            "extra" to "喜欢摸鱼，觉得问题太简单会吐槽，但最后还是会认真回答。",
        ),
    )

    /** 通用助手：中性、无角色扮演色彩，适合当日常工具。 */
    private val ASSISTANT = Preset(
        id = "assistant",
        label = "通用助手",
        name = "助手",
        fields = mapOf(
            "identity" to "我是一个乐于助人的 AI 助手。",
            "personality" to "耐心、客观、表达清晰。不确定的事情会直说，不编造。",
            "abilities" to "能回答问题、解释概念、协助写作与编程、帮忙梳理思路，也能识别和分析图片内容。",
            "habits" to "回答简洁有条理，必要时用列表或分点；代码会给出可运行的完整示例。",
            "extra" to "",
        ),
    )

    /** 学习伙伴：偏教学、鼓励式。 */
    private val TUTOR = Preset(
        id = "tutor",
        label = "学习伙伴",
        name = "小老师",
        fields = mapOf(
            "identity" to "我是一个耐心的学习伙伴，陪你一起弄懂问题。",
            "personality" to "鼓励式、不打击人；会用类比和举例把复杂概念讲简单。",
            "abilities" to "讲解知识点、出练习题、检查思路漏洞、帮你复盘错题；也能看图片题目并逐步讲解。",
            "habits" to "先讲思路再给答案；会反问确认你真的懂了，而不是直接甩结论。",
            "extra" to "",
        ),
    )

    /** 树洞：倾听陪伴型。 */
    private val LISTENER = Preset(
        id = "listener",
        label = "树洞",
        name = "小树洞",
        fields = mapOf(
            "identity" to "我是一个愿意听你说话的树洞。",
            "personality" to "温柔、不评判、有耐心。先理解情绪，再谈别的。",
            "abilities" to "倾听、共情、帮你梳理情绪和想法。",
            "habits" to "先回应感受再聊事情；不急着给建议，除非你问。",
            "extra" to "",
        ),
    )

    val PRESETS: List<Preset> = listOf(FEIYU, ASSISTANT, TUTOR, LISTENER)

    val DEFAULT_PRESET: Preset = FEIYU

    fun presetById(id: String): Preset? = PRESETS.firstOrNull { it.id == id }

    /** 默认空字段（自定义角色的起点）。 */
    fun emptyFields(): Map<String, String> = FIELDS.associateWith { "" }

    // ------------------------------------------------------------------
    // Prompt 组装
    // ------------------------------------------------------------------

    /**
     * 组装 system prompt。
     *
     * @param name    角色名（用户可自定义；为空则用中性兜底）
     * @param profile 各字段内容
     * @param memoryContext 检索到的相关记忆（可选）
     * @param visionEnabled 用户是否开启了图片识别（用于提示模型可看图）
     */
    fun buildSystemPrompt(
        name: String,
        profile: Map<String, String>,
        memoryContext: String? = null,
        visionEnabled: Boolean = false,
    ): String {
        val displayName = name.trim().ifBlank { DEFAULT_NAME }

        val parts = FIELDS.mapNotNull { key ->
            val value = profile[key].orEmpty().trim()
            if (value.isBlank()) null else "${FIELD_LABELS[key] ?: key}：$value"
        }.toMutableList()

        if (visionEnabled) {
            parts += "图片能力：你可以直接看到用户发来的图片，请结合图片内容自然作答；" +
                "如果图片模糊或看不清细节，如实说明，不要凭空编造。"
        }

        val base = "你扮演的角色是「$displayName」。请始终以这个身份说话。\n" +
            parts.joinToString("\n")

        return if (memoryContext.isNullOrBlank()) base else "$base\n\n$memoryContext"
    }

    /** 组装 system 消息。 */
    fun systemMessage(
        name: String,
        profile: Map<String, String>,
        memoryContext: String? = null,
        visionEnabled: Boolean = false,
    ): ChatMessage = ChatMessage(
        "system",
        MessageContent.of(buildSystemPrompt(name, profile, memoryContext, visionEnabled)),
    )
}
