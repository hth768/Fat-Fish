package com.feiyu.app.data.model

import kotlinx.serialization.KSerializer
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.buildClassSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.*

/**
 * MessageContent 的序列化器：
 * - 纯文本 → 序列化为 JSON 字符串
 * - 含图片 → 序列化为 OpenAI 多模态分段数组
 *
 * 反序列化时两种形态都能识别。
 */
object MessageContentSerializer : KSerializer<MessageContent> {

    override val descriptor: SerialDescriptor = buildClassSerialDescriptor("MessageContent")

    override fun serialize(encoder: Encoder, value: MessageContent) {
        val jsonEncoder = encoder as? JsonEncoder
            ?: throw IllegalStateException("MessageContent 仅支持 JSON 序列化")
        val element: JsonElement = when {
            value.parts != null -> Json.encodeToJsonElement(
                ListSerializer(ContentPart.serializer()), value.parts,
            )
            else -> JsonPrimitive(value.text ?: "")
        }
        jsonEncoder.encodeJsonElement(element)
    }

    override fun deserialize(decoder: Decoder): MessageContent {
        val jsonDecoder = decoder as? JsonDecoder
            ?: throw IllegalStateException("MessageContent 仅支持 JSON 反序列化")
        return when (val element = jsonDecoder.decodeJsonElement()) {
            is JsonPrimitive -> MessageContent.of(element.contentOrNull ?: "")
            is JsonArray -> MessageContent.ofParts(
                Json.decodeFromJsonElement(ListSerializer(ContentPart.serializer()), element),
            )
            else -> MessageContent.of("")
        }
    }
}
