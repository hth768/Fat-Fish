package com.feiyu.app.data.model

import kotlinx.serialization.Serializable

/**
 * OpenAI 兼容对话消息。
 *
 * content 支持两种形态（OpenAI 多模态规范）：
 * - 纯文本：`"content": "你好"`
 * - 多模态：`"content": [{"type":"text","text":"..."},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]`
 */
@Serializable
data class ChatMessage(
    val role: String,
    val content: MessageContent,
)

/** 消息内容：可以是纯文本，也可以是分段数组。 */
@Serializable(with = MessageContentSerializer::class)
data class MessageContent private constructor(
    val text: String?,                 // 纯文本形态
    val parts: List<ContentPart>?,     // 多模态形态
) {
    companion object {
        fun of(text: String): MessageContent = MessageContent(text, null)
        fun ofParts(parts: List<ContentPart>): MessageContent = MessageContent(null, parts)
    }
}

/** 多模态内容的一个分段。 */
@Serializable
data class ContentPart(
    val type: String,                 // "text" | "image_url"
    val text: String? = null,
    val image_url: ImageUrl? = null,
) {
    companion object {
        fun text(value: String) = ContentPart(type = "text", text = value)
        fun image(dataUrl: String) = ContentPart(type = "image_url", image_url = ImageUrl(url = dataUrl))
    }
}

@Serializable
data class ImageUrl(val url: String)

/** /chat/completions 请求体 */
@Serializable
data class ChatRequest(
    val model: String,
    val messages: List<ChatMessage>,
    val stream: Boolean = true,
    val temperature: Float = 0.9f,
)

/** 非流式返回的 choice.message（响应里 content 恒为字符串） */
@Serializable
data class RespMessage(
    val role: String? = null,
    val content: String? = null,
)

/** 非流式返回的 choice */
@Serializable
data class NonStreamChoice(
    val message: RespMessage? = null,
    val finish_reason: String? = null,
)

@Serializable
data class ChatCompletion(
    val id: String? = null,
    val choices: List<NonStreamChoice> = emptyList(),
)

/** 流式返回的 delta */
@Serializable
data class ChoiceDelta(
    val role: String? = null,
    val content: String? = null,
)

@Serializable
data class StreamChoice(
    val delta: ChoiceDelta? = null,
    val finish_reason: String? = null,
)

@Serializable
data class StreamChunk(
    val choices: List<StreamChoice> = emptyList(),
)

/** /embeddings 请求/响应 */
@Serializable
data class EmbeddingRequest(
    val model: String,
    val input: List<String>,
)

@Serializable
data class EmbeddingObject(
    val embedding: List<Float> = emptyList(),
    val index: Int = 0,
)

@Serializable
data class EmbeddingResponse(
    val data: List<EmbeddingObject> = emptyList(),
)
