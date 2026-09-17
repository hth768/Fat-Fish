package com.feiyu.app.data.remote

import com.feiyu.app.data.model.*
import com.feiyu.app.data.settings.Settings
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.buffer
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flow
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import retrofit2.Call
import retrofit2.Response
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import retrofit2.http.Body
import retrofit2.http.POST
import retrofit2.http.Streaming
import okhttp3.ResponseBody

import java.util.concurrent.TimeUnit

val json = Json {
    ignoreUnknownKeys = true
    isLenient = true
    encodeDefaults = true
}

private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

/** 手动序列化为 RequestBody，绕开 Retrofit 反射找序列化器的问题。 */
private fun Any.toJsonBody(): RequestBody = when (this) {
    is ChatRequest -> json.encodeToString(this).toRequestBody(JSON_MEDIA)
    is EmbeddingRequest -> json.encodeToString(this).toRequestBody(JSON_MEDIA)
    else -> throw IllegalArgumentException("未支持的请求体类型: ${this::class}")
}

private interface LlmApi {
    @POST("chat/completions")
    suspend fun chat(@Body body: RequestBody): ChatCompletion

    @POST("chat/completions")
    @Streaming
    fun chatStreaming(@Body body: RequestBody): Call<ResponseBody>

    @POST("embeddings")
    suspend fun embed(@Body body: RequestBody): EmbeddingResponse
}

private fun normalizeBaseUrl(url: String): String {
    val u = url.trim()
    return if (u.endsWith("/")) u else "$u/"
}

private fun authClient(apiKey: String): OkHttpClient =
    OkHttpClient.Builder()
        .addInterceptor { chain ->
            val req = chain.request().newBuilder()
                .addHeader("Authorization", "Bearer $apiKey")
                .addHeader("Accept", "text/event-stream")
                .build()
            chain.proceed(req)
        }
        // 流式响应时模型可能长时间不吐字（思考/排队），
        // 默认 readTimeout 仅 10 秒会把连接掐断导致回复被截断。
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.MINUTES)
        .writeTimeout(30, TimeUnit.SECONDS)
        .callTimeout(10, TimeUnit.MINUTES)
        .retryOnConnectionFailure(true)
        .build()

private fun buildRetrofit(baseUrl: String, apiKey: String): Retrofit =
    Retrofit.Builder()
        .baseUrl(normalizeBaseUrl(baseUrl))
        .client(authClient(apiKey))
        .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
        .build()

object LlmClient {

    /** 非流式一次性返回 */
    fun chatOnceFlow(
        settings: Settings,
        messages: List<ChatMessage>,
        withImage: Boolean = false,
    ): Flow<Result<String>> = flow {
        try {
            val api = buildRetrofit(settings.baseUrl, settings.apiKey).create(LlmApi::class.java)
            val resp = api.chat(
                ChatRequest(
                    settings.modelFor(withImage),
                    messages,
                    stream = false,
                    temperature = settings.temperature,
                ).toJsonBody(),
            )
            val text = resp.choices.firstOrNull()?.message?.content
            if (text != null) emit(Result.success(text))
            else emit(Result.failure(IllegalStateException("空响应")))
        } catch (e: Exception) {
            emit(Result.failure(e))
        }
    }

    /**
     * 流式返回（SSE）。每收到一段 delta 就发射一次。完成时正常结束。
     *
     * 两个关键点（都曾导致「回复被截断」）：
     * 1. **不要用 `while (!source.exhausted())` 作为循环条件。**
     *    `exhausted()` 是非阻塞探测：当 socket 缓冲区暂时为空但流尚未结束时，
     *    它可能返回 true，导致循环提前退出、后续内容全部丢失。
     *    正确做法是读行直到 `readUtf8Line()` 返回 null（真正的 EOF）。
     * 2. **发射端要有足够缓冲。** 回调运行在 OkHttp 网络线程（非挂起），
     *    callbackFlow 默认缓冲仅 64 个元素，消费稍慢时 trySend 会静默失败 → 丢字。
     */
    fun streamChatFlow(
        settings: Settings,
        messages: List<ChatMessage>,
        withImage: Boolean = false,
    ): Flow<String> =
        callbackFlow {
            val api = buildRetrofit(settings.baseUrl, settings.apiKey).create(LlmApi::class.java)
            val call = api.chatStreaming(
                ChatRequest(
                    settings.modelFor(withImage),
                    messages,
                    stream = true,
                    temperature = settings.temperature,
                ).toJsonBody(),
            )
            call.enqueue(object : retrofit2.Callback<ResponseBody> {
                override fun onResponse(c: Call<ResponseBody>, r: Response<ResponseBody>) {
                    if (!r.isSuccessful) {
                        val err = try {
                            r.errorBody()?.string()
                        } catch (_: Exception) {
                            null
                        }
                        close(Exception("请求失败 (${r.code()}): ${err ?: "无详情"}"))
                        return
                    }
                    val body = r.body()
                    if (body == null) {
                        close(Exception("空响应体"))
                        return
                    }
                    try {
                        val source: okio.BufferedSource = body.source()
                        // 读行直到真正的 EOF（返回 null）。
                        // 不能用 while (!source.exhausted()) —— 该探测在 socket 缓冲暂时为空
                        // 但流未结束时可能返回 true，导致提前退出、内容被截断。
                        while (true) {
                            val line = source.readUtf8Line() ?: break
                            // SSE 规范：注释行以 ':' 开头，忽略
                            if (line.isEmpty() || line.startsWith(":")) continue
                            if (!line.startsWith("data:")) continue
                            val payload = line.removePrefix("data:").trim()
                            if (payload == "[DONE]") break
                            if (payload.isEmpty()) continue
                            try {
                                val chunk = json.decodeFromString<StreamChunk>(payload)
                                val choice = chunk.choices.firstOrNull()
                                val delta = choice?.delta?.content
                                if (!delta.isNullOrEmpty()) {
                                    // 若发送失败说明下游已关闭，此时才应停止读取
                                    if (trySend(delta).isFailure) break
                                }
                                // 记录结束原因：length 表示达到输出长度上限（属正常截断，非 bug）
                                val reason = choice?.finish_reason
                                if (reason != null) {
                                    android.util.Log.i("LlmClient", "流结束原因: $reason")
                                }
                            } catch (e: Exception) {
                                // 单个分片解析失败不影响整体（如心跳/未知字段），
                                // 但记录到日志便于排查「截断」问题
                                android.util.Log.w("LlmClient", "解析分片失败: ${payload.take(120)}", e)
                            }
                        }
                        close()
                    } catch (e: Exception) {
                        close(e)
                    }
                }

                override fun onFailure(c: Call<ResponseBody>, t: Throwable) {
                    close(t)
                }
            })
            awaitClose { call.cancel() }
        }.buffer(Channel.UNLIMITED)


    /** 调用远程 embedding 端点，返回每条文本的向量。input 为空时返回 null。 */
    suspend fun embedTexts(settings: Settings, texts: List<String>): List<List<Float>>? {
        if (texts.isEmpty()) return null
        return try {
            val api = buildRetrofit(settings.embeddingBaseUrl, settings.embeddingApiKey)
                .create(LlmApi::class.java)
            val resp = api.embed(
                EmbeddingRequest(settings.embeddingModel, texts).toJsonBody(),
            )
            resp.data.sortedBy { it.index }.map { it.embedding }
        } catch (e: Exception) {
            null
        }
    }
}
