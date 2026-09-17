package com.feiyu.app.data.tts

import android.content.Context
import android.media.MediaPlayer
import com.feiyu.app.data.settings.Settings
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * 云端 TTS 引擎（OpenAI 兼容的 `/audio/speech` 接口）。
 *
 * 音质明显优于系统 TTS，且可选音色。需要联网并配置 API Key。
 *
 * 兼容各家实现，例如：
 * - OpenAI:        https://api.openai.com/v1        model=tts-1        voice=alloy
 * - 硅基流动:      https://api.siliconflow.cn/v1     model=...          voice=...
 * - 其他 OpenAI 兼容服务，自行填地址、模型与音色
 */
class CloudTtsEngine(
    private val context: Context,
    private var settings: Settings,
) : TtsEngine {

    override val name: String = "云端语音"

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    private var player: MediaPlayer? = null
    private var tempFile: File? = null

    fun updateSettings(s: Settings) {
        settings = s
    }

    override suspend fun isAvailable(): Boolean =
        settings.ttsBaseUrl.isNotBlank() && settings.ttsApiKey.isNotBlank()

    override fun speak(
        text: String,
        onStart: (() -> Unit)?,
        onDone: (() -> Unit)?,
        onError: ((String) -> Unit)?,
    ) {
        // 网络请求需异步，这里用后台线程承载；接口保持非挂起以便与系统 TTS 统一调用
        Thread {
            try {
                stop()
                val result = runBlocking { requestSpeech(text) }
                val audio = result.getOrElse { e ->
                    onError?.invoke(e.message ?: "云端语音请求失败")
                    return@Thread
                }
                if (audio.isEmpty()) {
                    onError?.invoke("云端返回了空音频")
                    return@Thread
                }
                val dir = File(context.cacheDir, "tts").apply { mkdirs() }
                val file = File(dir, "speech_${System.currentTimeMillis()}.mp3")
                file.writeBytes(audio)
                tempFile = file

                val mp = MediaPlayer()
                player = mp
                mp.setDataSource(file.absolutePath)
                mp.setOnPreparedListener {
                    onStart?.invoke()
                    it.start()
                }
                mp.setOnCompletionListener {
                    onDone?.invoke()
                    cleanup()
                }
                mp.setOnErrorListener { _, what, extra ->
                    onError?.invoke("播放失败 (what=$what extra=$extra)")
                    cleanup()
                    true
                }
                mp.prepareAsync()
            } catch (e: Exception) {
                onError?.invoke(e.message ?: "云端语音播放异常")
                cleanup()
            }
        }.start()
    }

    /** 调用 /audio/speech 获取音频字节。 */
    private suspend fun requestSpeech(text: String): Result<ByteArray> = withContext(Dispatchers.IO) {
        try {
            val client = OkHttpClient.Builder()
                .connectTimeout(20, TimeUnit.SECONDS)
                .readTimeout(90, TimeUnit.SECONDS)
                .writeTimeout(30, TimeUnit.SECONDS)
                .build()

            val payload = buildPayload(text)
            val body = json.encodeToString(JsonObject.serializer(), payload)
                .toRequestBody("application/json; charset=utf-8".toMediaType())

            val url = settings.ttsBaseUrl.trimEnd('/') + "/audio/speech"
            val request = Request.Builder()
                .url(url)
                .addHeader("Authorization", "Bearer ${settings.ttsApiKey}")
                .addHeader("Content-Type", "application/json")
                .post(body)
                .build()

            client.newCall(request).execute().use { resp ->
                val bytes = resp.body?.bytes() ?: ByteArray(0)
                if (!resp.isSuccessful) {
                    val msg = String(bytes).take(300)
                    Result.failure(Exception("HTTP ${resp.code}: $msg"))
                } else {
                    Result.success(bytes)
                }
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 组装请求体。
     *
     * 用 JsonObject 动态构建而非固定数据类，因为「情感参数」的名称与取值
     * 各家服务商不同（Azure 用 `voice_style`、MiniMax 用 `emotion`、
     * 火山用 `emotion`/`enable_emotion` 等），必须允许任意键名。
     */
    private fun buildPayload(text: String): JsonObject {
        // 情感参数（可选）：参数名与值都由用户在设置里指定
        val emoKey = settings.ttsEmotionKey.trim()
        val emoValue = settings.ttsEmotionValue.trim()

        return buildJsonObject {
            put("model", JsonPrimitive(settings.ttsModel.ifBlank { "tts-1" }))
            put("input", JsonPrimitive(text))
            put("voice", JsonPrimitive(settings.ttsVoice.ifBlank { "alloy" }))
            // OpenAI 的 speed 取值区间是 0.25 ~ 4.0
            put("speed", JsonPrimitive(settings.ttsSpeed.coerceIn(0.25f, 4.0f)))
            if (emoKey.isNotEmpty() && emoValue.isNotEmpty()) {
                put(emoKey, JsonPrimitive(emoValue))
            }
        }
    }

    override fun stop() {
        try {
            player?.stop()
        } catch (_: Exception) {
            // ignore
        }
        cleanup()
    }

    private fun cleanup() {
        try {
            player?.release()
        } catch (_: Exception) {
            // ignore
        }
        player = null
        try {
            tempFile?.delete()
        } catch (_: Exception) {
            // ignore
        }
        tempFile = null
    }

    override fun release() = stop()
}
