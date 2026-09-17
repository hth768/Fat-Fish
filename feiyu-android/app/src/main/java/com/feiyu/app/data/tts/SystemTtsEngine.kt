package com.feiyu.app.data.tts

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.Locale
import java.util.UUID
import kotlin.coroutines.resume

/**
 * 安卓系统 TTS 引擎。
 *
 * 优点：免费、离线、零延迟。
 * 音质取决于手机自带的 TTS 引擎（多数国产 ROM 自带引擎中文尚可，
 * 也可在系统设置里安装/切换第三方引擎）。
 */
class SystemTtsEngine(private val context: Context) : TtsEngine {

    override val name: String = "系统语音"

    private var tts: TextToSpeech? = null
    private var ready = false

    /** 语速与音调（由设置注入，播放时实时应用） */
    private var speed = 1.0f
    private var pitch = 1.0f

    /** 更新语音参数；若引擎已就绪则立即生效。 */
    fun updateParams(speed: Float, pitch: Float) {
        this.speed = speed.coerceIn(0.5f, 2.0f)
        this.pitch = pitch.coerceIn(0.5f, 2.0f)
        if (ready) applyParams()
    }

    private fun applyParams() {
        try {
            tts?.setSpeechRate(this.speed)
            tts?.setPitch(this.pitch)
        } catch (_: Exception) {
            // ignore
        }
    }

    /** 当前播放的 utteranceId，用于区分回调归属 */
    private var currentId: String? = null

    override suspend fun isAvailable(): Boolean = ensureReady()

    /** 懒初始化：首次使用时才创建，避免拖慢启动。 */
    private suspend fun ensureReady(): Boolean {
        if (ready) return true
        return suspendCancellableCoroutine { cont ->
            var resumed = false
            fun finish(ok: Boolean) {
                if (!resumed) {
                    resumed = true
                    if (cont.isActive) cont.resume(ok)
                }
            }
            val engine = TextToSpeech(context) { status ->
                ready = status == TextToSpeech.SUCCESS
                if (ready) {
                    try {
                        engineSetChinese()
                    } catch (_: Exception) {
                        // ignore
                    }
                }
                finish(ready)
            }
            tts = engine
            // 兜底：极少数设备初始化回调不触发，避免永久挂起
            cont.invokeOnCancellation { finish(false) }
        }
    }

    private fun engineSetChinese() {
        val engine = tts ?: return
        val result = engine.setLanguage(Locale.CHINA)
        if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) {
            // 中文不可用时回落到系统默认语言
            engine.setLanguage(Locale.getDefault())
        }
        applyParams()
    }

    override fun speak(
        text: String,
        onStart: (() -> Unit)?,
        onDone: (() -> Unit)?,
        onError: ((String) -> Unit)?,
    ) {
        val engine = tts
        if (engine == null || !ready) {
            onError?.invoke("系统语音尚未就绪，请稍后重试")
            return
        }
        val id = UUID.randomUUID().toString()
        currentId = id

        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {
                if (utteranceId == currentId) onStart?.invoke()
            }

            override fun onDone(utteranceId: String?) {
                if (utteranceId == currentId) onDone?.invoke()
            }

            @Deprecated("旧版本回调")
            override fun onError(utteranceId: String?) {
                if (utteranceId == currentId) onError?.invoke("播放失败")
            }

            override fun onError(utteranceId: String?, errorCode: Int) {
                if (utteranceId == currentId) onError?.invoke("播放失败 (code=$errorCode)")
            }
        })

        val result = engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, id)
        if (result != TextToSpeech.SUCCESS) {
            onError?.invoke("发起播放失败")
        }
    }

    override fun stop() {
        try {
            tts?.stop()
        } catch (_: Exception) {
            // ignore
        }
    }

    override fun release() {
        try {
            tts?.stop()
            tts?.shutdown()
        } catch (_: Exception) {
            // ignore
        }
        tts = null
        ready = false
    }
}
