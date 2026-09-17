package com.feiyu.app.data.tts

/**
 * 把长文本切成适合朗读的句子/段落，支持在句子与段落之间插入停顿。
 *
 * 停顿的实现方式：
 * - 系统 TTS：按段落依次调用 `speak`（QUEUE_ADD 排队），段间用延时控制间隔
 * - 云端 TTS：每段单独请求并播放，段间沿用同一套延时
 *
 * 这样能做出自然的抑扬顿挫，而不是一口气念完。
 */
object SpeechChunker {

    /** 一个朗读单元（一段文本 + 读完后应停顿的毫秒数）。 */
    data class Chunk(
        val text: String,
        val pauseAfterMs: Int,
    )

    // 句末标点（中英文）
    private const val SENTENCE_END = "。！？!?；;…"

    /**
     * 切分为朗读单元。
     *
     * @param text            已清洗的文本
     * @param sentencePauseMs 句间停顿
     * @param paragraphPauseMs 段间停顿
     */
    fun split(
        text: String,
        sentencePauseMs: Int,
        paragraphPauseMs: Int,
    ): List<Chunk> {
        val chunks = mutableListOf<Chunk>()
        val paragraphs = text.split(Regex("\\n\\s*\\n")).map { it.trim() }.filter { it.isNotEmpty() }

        paragraphs.forEachIndexed { pIndex, para ->
            val sentences = splitSentences(para)
            sentences.forEachIndexed { sIndex, sentence ->
                val isLastSentence = sIndex == sentences.lastIndex
                val isLastParagraph = pIndex == paragraphs.lastIndex
                val pause = when {
                    isLastSentence && isLastParagraph -> 0          // 最后一段最后一句不再停顿
                    isLastSentence -> paragraphPauseMs              // 段末
                    else -> sentencePauseMs                         // 句间
                }
                if (sentence.isNotBlank()) {
                    chunks += Chunk(sentence, pause)
                }
            }
        }

        // 极端情况：没有切出任何单元
        if (chunks.isEmpty() && text.isNotBlank()) {
            chunks += Chunk(text.trim(), 0)
        }
        return chunks
    }

    /**
     * 按句末标点切句。
     * 会保留标点（让 TTS 的语调更自然），并合并过短的片段（避免频繁中断）。
     */
    private fun splitSentences(para: String): List<String> {
        val result = mutableListOf<String>()
        val sb = StringBuilder()

        for (ch in para) {
            sb.append(ch)
            if (SENTENCE_END.contains(ch)) {
                // 连续标点（如"？！"）一并并入当前句
                result += sb.toString().trim()
                sb.setLength(0)
            }
        }
        if (sb.isNotBlank()) result += sb.toString().trim()

        // 合并过短的句子（阈值 10 字），让朗读更连贯
        val merged = mutableListOf<String>()
        var buffer = StringBuilder()
        for (s in result.filter { it.isNotBlank() }) {
            if (buffer.isNotEmpty()) buffer.append(s) else buffer.append(s)
            if (buffer.length >= 10) {
                merged += buffer.toString()
                buffer = StringBuilder()
            }
        }
        if (buffer.isNotEmpty()) {
            if (merged.isNotEmpty() && buffer.length < 10) {
                merged[merged.lastIndex] = merged.last() + buffer.toString()
            } else {
                merged += buffer.toString()
            }
        }
        return merged.filter { it.isNotBlank() }
    }
}
