package com.feiyu.app.data.chat

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * 持久化的一条消息。
 *
 * @param imagePath 图片消息的本地文件路径（可空）；只存路径不存 Base64，
 *                  避免历史文件膨胀（一条图片消息 Base64 可达数百 KB）。
 */
@Serializable
data class StoredMsg(
    val role: String,
    val content: String,
    val ts: Long = 0L,
    val imagePath: String? = null,
)

object ChatHistory {
    private val json = Json { prettyPrint = false; ignoreUnknownKeys = true }

    private fun file(context: Context) = File(context.filesDir, "chat_history.json")

    fun load(context: Context): MutableList<StoredMsg> {
        val f = file(context)
        if (!f.exists()) return mutableListOf()
        return try {
            json.decodeFromString<List<StoredMsg>>(f.readText()).toMutableList()
        } catch (_: Exception) {
            mutableListOf()
        }
    }

    fun save(context: Context, list: List<StoredMsg>) {
        file(context).writeText(json.encodeToString(list))
    }

    fun clear(context: Context) {
        file(context).writeText("[]")
    }
}
