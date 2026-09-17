package com.feiyu.app.data.tts

/**
 * 语音播报引擎的统一接口。
 *
 * 两种实现：
 * - [SystemTtsEngine]：调用安卓系统 TTS，免费、离线、零延迟（默认）
 * - [CloudTtsEngine]：调用云端 TTS API，音质更好（需配置接口与 Key）
 */
interface TtsEngine {

    /** 引擎显示名（用于界面提示） */
    val name: String

    /** 当前是否可用（系统 TTS 需已安装引擎；云端需已配置） */
    suspend fun isAvailable(): Boolean

    /**
     * 朗读文本。
     *
     * @param text 要朗读的内容
     * @param onStart 开始播放回调（可选）
     * @param onDone 播放结束回调（可选）
     * @param onError 出错回调（可选）
     */
    fun speak(
        text: String,
        onStart: (() -> Unit)? = null,
        onDone: (() -> Unit)? = null,
        onError: ((String) -> Unit)? = null,
    )

    /** 停止播放 */
    fun stop()

    /** 释放资源 */
    fun release()
}

/**
 * 文本预处理：语音朗读前清理不适合朗读的内容。
 *
 * - 去掉 Markdown 标记（`**`、`##`、`` ` `` 等），否则会被逐字念出来
 * - 去掉代码块内容（读代码没有意义且冗长）
 * - 去掉颜文字/表情符号
 * - 收敛多余空白
 */
object SpeechText {
    fun clean(raw: String): String {
        var t = raw

        // 去掉 fenced code block
        t = t.replace(Regex("```[\\s\\S]*?```"), "（代码略）")

        // 去掉行内代码的反引号，保留内容
        t = t.replace(Regex("`([^`]*)`"), "$1")

        // 去掉图片/链接语法，保留链接文字
        t = t.replace(Regex("!\\[[^\\]]*\\]\\([^)]*\\)"), "")
        t = t.replace(Regex("\\[([^\\]]*)\\]\\([^)]*\\)"), "$1")

        // 去掉 markdown 强调/标题/引用/列表标记
        t = t.replace(Regex("[*_~]{1,3}"), "")
        t = t.replace(Regex("(?m)^\\s{0,3}#{1,6}\\s*"), "")
        t = t.replace(Regex("(?m)^\\s{0,3}>\\s*"), "")
        t = t.replace(Regex("(?m)^\\s{0,3}[-+*]\\s+"), "")
        t = t.replace(Regex("(?m)^\\s{0,3}\\d+\\.\\s+"), "")

        // 去掉常见颜文字/符号（如 (๑•̀ㅂ•́)و✧、~、→ 等）
        t = t.replace(Regex("[~～^_=]{2,}"), "")
        t = t.replace(Regex("[\uD800-\uDBFF][\uDC00-\uDFFF]"), "")   // 代理对（emoji）
        t = t.replace(Regex("[\\u2600-\\u27BF\\uFE0F]"), "")          // 杂项符号

        // 收敛空白
        t = t.replace(Regex("[ \\t]+"), " ")
        t = t.replace(Regex("\\n{3,}"), "\n\n")

        return t.trim()
    }
}
