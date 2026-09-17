package com.feiyu.app.data.tts

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.feiyu.app.data.settings.Settings

/**
 * 语音播报总控。
 *
 * 职责：
 * - 按设置挑选引擎（云端 / 系统）
 * - 把长文本切分为多个朗读单元，逐段朗读并在其间插入停顿，做出自然节奏
 * - 维护播放状态供界面显示
 */
class TtsController(context: Context) {

    private val systemEngine = SystemTtsEngine(context)
    private val cloudEngine = CloudTtsEngine(context, Settings())

    private var settings = Settings()
    private val mainHandler = Handler(Looper.getMainLooper())

    /** 播放代次：每次新的朗读/停止都自增，用于丢弃过期的回调 */
    private var generation = 0

    /** 正在朗读的消息 id（界面用于显示"停止"按钮与播放动画） */
    var speakingId: String? = null
        private set

    fun updateSettings(s: Settings) {
        settings = s
        cloudEngine.updateSettings(s)
        systemEngine.updateParams(s.ttsSpeed, s.ttsPitch)
    }

    /** 按设置挑选实际使用的引擎。 */
    private fun pickEngine(): TtsEngine {
        val useCloud = settings.ttsUseCloud &&
            settings.ttsBaseUrl.isNotBlank() &&
            settings.ttsApiKey.isNotBlank()
        return if (useCloud) cloudEngine else systemEngine
    }

    /**
     * 朗读文本（自动分句 + 停顿）。
     *
     * @param messageId 关联的消息 id（用于界面状态同步，可为 null）
     */
    fun speak(
        messageId: String?,
        rawText: String,
        onStateChange: (speakingId: String?, error: String?) -> Unit,
    ) {
        val text = SpeechText.clean(rawText)
        if (text.isBlank()) {
            onStateChange(null, "没有可朗读的内容")
            return
        }

        val chunks = SpeechChunker.split(
            text = text,
            sentencePauseMs = settings.ttsSentencePause,
            paragraphPauseMs = settings.ttsParagraphPause,
        )

        val gen = ++generation
        speakingId = messageId
        onStateChange(messageId, null)
        playChunks(chunks, 0, gen, onStateChange)
    }

    /** 递归播放第 index 个单元，读完后延时再读下一个。 */
    private fun playChunks(
        chunks: List<SpeechChunker.Chunk>,
        index: Int,
        gen: Int,
        onStateChange: (String?, String?) -> Unit,
    ) {
        if (gen != generation) return                  // 已被新的朗读/停止取代
        if (index >= chunks.size) {
            speakingId = null
            onStateChange(null, null)
            return
        }

        val chunk = chunks[index]
        val engine = pickEngine()

        engine.speak(
            text = chunk.text,
            onStart = { /* 已在 speak() 处置为播放中 */ },
            onDone = {
                if (gen == generation) {
                    if (chunk.pauseAfterMs > 0) {
                        mainHandler.postDelayed({
                            if (gen == generation) playChunks(chunks, index + 1, gen, onStateChange)
                        }, chunk.pauseAfterMs.toLong())
                    } else {
                        playChunks(chunks, index + 1, gen, onStateChange)
                    }
                }
            },
            onError = { msg ->
                if (gen == generation) {
                    speakingId = null
                    onStateChange(null, msg)
                }
            },
        )
    }

    /**
     * 试听：用当前设置朗读一句固定示例（同样走分句与停顿逻辑）。
     * 试听不关联消息，会占用播放通道但不改变外部状态。
     */
    fun preview(text: String, onResult: (String?) -> Unit) {
        val clean = SpeechText.clean(text)
        val chunks = SpeechChunker.split(
            text = clean,
            sentencePauseMs = settings.ttsSentencePause,
            paragraphPauseMs = settings.ttsParagraphPause,
        )
        val gen = ++generation
        speakingId = null
        previewChunks(chunks, 0, gen, onResult)
    }

    private fun previewChunks(
        chunks: List<SpeechChunker.Chunk>,
        index: Int,
        gen: Int,
        onResult: (String?) -> Unit,
    ) {
        if (gen != generation) return
        if (index >= chunks.size) {
            onResult(null)
            return
        }
        val chunk = chunks[index]
        pickEngine().speak(
            text = chunk.text,
            onDone = {
                if (gen == generation) {
                    if (chunk.pauseAfterMs > 0) {
                        mainHandler.postDelayed({
                            if (gen == generation) previewChunks(chunks, index + 1, gen, onResult)
                        }, chunk.pauseAfterMs.toLong())
                    } else {
                        previewChunks(chunks, index + 1, gen, onResult)
                    }
                }
            },
            onError = { msg -> if (gen == generation) onResult(msg) },
        )
    }

    /** 是否正在朗读指定消息。 */
    fun isSpeaking(messageId: String): Boolean = speakingId == messageId

    fun stop() {
        generation++                                  // 使所有待执行的回调失效
        try {
            systemEngine.stop()
            cloudEngine.stop()
        } catch (_: Exception) {
            // ignore
        }
        speakingId = null
    }

    fun release() {
        stop()
        systemEngine.release()
        cloudEngine.release()
    }

    /** 当前引擎名（用于设置页提示）。 */
    fun activeEngineName(): String = if (
        settings.ttsUseCloud &&
        settings.ttsBaseUrl.isNotBlank() &&
        settings.ttsApiKey.isNotBlank()
    ) {
        "云端语音"
    } else {
        "系统语音"
    }
}
