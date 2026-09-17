package com.feiyu.app.data.memory

import com.feiyu.app.data.model.ChatMessage
import com.feiyu.app.data.model.MessageContent
import com.feiyu.app.data.remote.LlmClient
import com.feiyu.app.data.settings.Settings

/**
 * 对话摘要沉淀。
 *
 * 当会话历史超出「滚动窗口」时，窗口之外、且尚未被摘要过的对话会被压缩成一段摘要，
 * 作为 SUMMARY 类记忆写入向量库，保证长期连贯性又不会让 token 无限增长。
 */
object Summarizer {

    private const val SYSTEM = """你是对话记忆整理助手。请把下面这段对话压缩成简洁的要点，用于日后回忆。

要求：
- 保留：用户提到的事实（姓名、喜好、经历、计划、约定）、正在讨论的话题、未解决的问题
- 用第三人称陈述，不要寒暄，不要复述原话
- 3 句话以内，每句以「·」开头
- 只输出要点，不要任何额外解释"""

    /** 把 messages 压缩为摘要文本；失败返回 null。 */
    suspend fun summarize(settings: Settings, messages: List<ChatMessage>): String? {
        if (messages.isEmpty()) return null
        val charName = settings.displayName()
        val transcript = messages.joinToString("\n") { m ->
            val who = if (m.role == "user") "用户" else charName
            "$who：${m.content}"
        }
        val req = listOf(
            ChatMessage("system", MessageContent.of(SYSTEM)),
            ChatMessage("user", MessageContent.of(transcript.take(6000))),
        )
        return try {
            var text: String? = null
            LlmClient.chatOnceFlow(settings, req).collect { result ->
                text = result.getOrNull()
            }
            text?.trim()?.takeIf { it.isNotBlank() }
        } catch (_: Exception) {
            null
        }
    }
}
