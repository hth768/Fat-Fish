package com.feiyu.app.data.memory

import kotlinx.serialization.Serializable

/** 记忆类型：普通对话片段 / 对话摘要 / 用户画像 */
object MemoryKind {
    const val DIALOG = "dialog"     // 一轮对话片段
    const val SUMMARY = "summary"   // 旧对话压缩出的摘要
    const val PROFILE = "profile"   // 提炼出的用户事实
}

/** 一条被向量化的记忆片段。 */
@Serializable
data class StoredMemory(
    val id: String,
    val text: String,
    val source: String,        // 来源说明，如「用户说」「<角色名>回」「对话摘要」
    val createdAt: Long,
    val vector: List<Float>,   // 向量（本地 TF-IDF 或远程 embedding）
    val dim: Int,              // 向量维度（用于判断兼容）
    val kind: String = MemoryKind.DIALOG,
    val hits: Int = 0,         // 被检索命中次数（越多越重要）
    val updatedAt: Long = 0L,  // 去重更新时刷新
) {
    /** 重要性分数：命中次数 + 类型权重 + 新鲜度。用于淘汰与排序。 */
    fun importance(now: Long): Float {
        val kindWeight = when (kind) {
            MemoryKind.SUMMARY -> 2.0f
            MemoryKind.PROFILE -> 2.5f
            else -> 1.0f
        }
        val ageDays = ((now - createdAt).coerceAtLeast(0L)) / 86_400_000f
        val fresh = 1f / (1f + ageDays / 30f)   // 30 天衰减一半
        return kindWeight * (1f + hits * 0.5f) * (0.5f + fresh)
    }
}

/** 记忆库整体（持久化到 JSON 文件）。 */
@Serializable
data class MemoryStore(
    val items: List<StoredMemory> = emptyList(),
    val df: Map<String, Int> = emptyMap(),  // 词袋 doc-freq：bucketIndex -> 文档数
    val n: Int = 0,
    val summarizedUpTo: Long = 0L,          // 已摘要到的历史时间戳（水位线）
)
