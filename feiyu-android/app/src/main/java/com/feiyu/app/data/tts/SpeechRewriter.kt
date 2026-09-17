package com.feiyu.app.data.tts

import com.feiyu.app.data.model.ChatMessage
import com.feiyu.app.data.model.MessageContent
import com.feiyu.app.data.remote.LlmClient
import com.feiyu.app.data.settings.Settings

/**
 * 朗读改写：把书面化的回复改写成更适合「听」的口语版本。
 *
 * 大模型输出的文字往往适合阅读（分点、标题、括号补充），直接朗读会很生硬。
 * 改写后加入口语连接词与停顿标点，听感更自然。
 *
 * 代价：每次朗读多消耗一次 LLM 调用（token）。
 */
object SpeechRewriter {

    private const val SYSTEM = """你是语音播报稿改写助手。把用户给你的文字改写成适合【朗读】的口语稿。

要求：
- 保持原意与信息完整，不要增删事实
- 去掉不适合朗读的内容：markdown 标记、列表符号、括号补充、代码、链接
- 改成口语表达：把书面语换成人话，适当加「那么」「也就是说」这类连接词
- 用标点控制停顿：短句用逗号，话题转换用句号，需要明显停顿处用换行分段
- 不要输出任何解释、标题或前后缀，只输出改写后的正文
- 保持原语言"""

    /**
     * 改写文本。失败时返回原文（保证朗读功能不受影响）。
     */
    suspend fun rewrite(settings: Settings, text: String): String {
        if (text.isBlank()) return text
        val req = listOf(
            ChatMessage("system", MessageContent.of(SYSTEM)),
            ChatMessage("user", MessageContent.of(text.take(4000))),
        )
        return try {
            var result: String? = null
            LlmClient.chatOnceFlow(settings, req).collect { r ->
                result = r.getOrNull()
            }
            val rewritten = result?.trim()
            if (rewritten.isNullOrBlank()) text else rewritten
        } catch (_: Exception) {
            text
        }
    }
}
