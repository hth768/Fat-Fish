package com.feiyu.app.data.memory

import android.content.Context
import com.feiyu.app.data.remote.LlmClient
import com.feiyu.app.data.settings.Settings
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File
import java.util.UUID
import kotlin.math.ln
import kotlin.math.sqrt

/**
 * 本地向量记忆。
 *
 * - 默认（离线）：对文本做特征哈希（feature hashing）得到固定维度（DIM）的 TF-IDF 向量，
 *   用余弦相似度检索相关记忆，完全本地、免费、无需任何额外接口。
 * - 增强：在设置里配置远程 embedding 端点后，改用真实语义向量（维度随模型而定）。
 *
 * 特性：
 * - 去重：新记忆与已有记忆相似度 > dedupThreshold 时【更新】而非新增（合并文本、刷新时间）。
 * - 淘汰：条数超过上限时，按 importance（类型权重 × 命中次数 × 新鲜度）淘汰最弱的。
 * - 命中统计：检索命中的记忆 hits+1，越常用的记忆越不容易被淘汰。
 */
class VectorMemory(private val context: Context) {

    private val DIM = 512
    private val json = Json { ignoreUnknownKeys = true }
    private val file = File(context.filesDir, "vector_memory.json")

    private fun load(): MemoryStore {
        if (!file.exists()) return MemoryStore()
        return try {
            json.decodeFromString<MemoryStore>(file.readText())
        } catch (_: Exception) {
            MemoryStore()
        }
    }

    private fun save(store: MemoryStore) {
        file.writeText(json.encodeToString(store))
    }

    /** 分词：CJK 取字符二元组，拉丁数字按词。 */
    private fun tokenize(text: String): List<String> {
        val tokens = mutableListOf<String>()
        val cjk = StringBuilder()
        val latin = StringBuilder()
        fun flushCjk() {
            val s = cjk.toString()
            if (s.length >= 2) {
                for (i in 0 until s.length - 1) tokens.add("c:" + s[i] + s[i + 1])
            } else if (s.length == 1) {
                tokens.add("c:" + s[0])
            }
            cjk.setLength(0)
        }
        fun flushLatin() {
            if (latin.isNotEmpty()) {
                tokens.add("w:" + latin.toString().lowercase())
                latin.setLength(0)
            }
        }
        for (ch in text) {
            when {
                ch.isWhitespace() || "，。！？、；：,.!?;:\"'\n()（）【】[]《》<>~`@#\$%^&*-=+\\|/_…—".contains(ch) -> {
                    flushCjk(); flushLatin()
                }
                ch.code in 0x4E00..0x9FFF -> {
                    flushLatin(); cjk.append(ch)
                }
                ch.isLetterOrDigit() -> {
                    flushCjk(); latin.append(ch)
                }
                else -> {
                    flushCjk(); flushLatin()
                }
            }
        }
        flushCjk(); flushLatin()
        return tokens
    }

    private fun hashToken(t: String): Int {
        val h = t.hashCode()
        return (if (h < 0) -h else h) % DIM
    }

    /** 本地 TF-IDF 向量（依赖 store 的词频信息）。 */
    private fun localVector(text: String, store: MemoryStore): List<Float> {
        val toks = tokenize(text)
        if (toks.isEmpty()) return FloatArray(DIM).toList()
        val counts = mutableMapOf<Int, Int>()
        for (t in toks) counts[hashToken(t)] = (counts[hashToken(t)] ?: 0) + 1
        val total = toks.size.toFloat()
        val n = store.n.coerceAtLeast(1)
        val vec = FloatArray(DIM)
        for ((b, c) in counts) {
            val tf = c / total
            val dfv = store.df[b.toString()]?.toFloat() ?: 0f
            val idf = ln((n + 1) / (dfv + 1)) + 1f
            vec[b] += tf * idf
        }
        // L2 归一化
        var norm = 0f
        for (v in vec) norm += v * v
        norm = sqrt(norm)
        if (norm > 0f) for (i in vec.indices) vec[i] /= norm
        return vec.toList()
    }

    private fun cosine(a: List<Float>, b: List<Float>): Float {
        if (a.size != b.size || a.isEmpty()) return 0f
        var dot = 0f
        for (i in a.indices) dot += a[i] * b[i]
        return dot // 两者均 L2 归一化，点积即为余弦
    }

    /** 计算文本向量（远程优先，失败回落本地）。 */
    private suspend fun vectorOf(text: String, store: MemoryStore, settings: Settings): Pair<List<Float>, Int> {
        if (settings.useRemoteEmbedding()) {
            val emb = LlmClient.embedTexts(settings, listOf(text))?.firstOrNull()
            if (emb != null && emb.isNotEmpty()) return emb to emb.size
        }
        return localVector(text, store) to DIM
    }

    /** 新增/更新一条记忆。返回 true 表示新增，false 表示被去重合并。 */
    suspend fun addMemory(
        text: String,
        source: String,
        settings: Settings,
        kind: String = MemoryKind.DIALOG,
    ): Boolean = withContext(Dispatchers.IO) {
        val clean = text.trim()
        if (clean.isBlank()) return@withContext false
        var store = load()
        val now = System.currentTimeMillis()

        // 本地模式下先更新 df（远程 embedding 不依赖 df）
        if (!settings.useRemoteEmbedding()) {
            val buckets = tokenize(clean).map { hashToken(it) }.toSet()
            val df = store.df.toMutableMap()
            for (b in buckets) df[b.toString()] = (df[b.toString()] ?: 0) + 1
            store = store.copy(df = df, n = store.n + 1)
        }

        val (vector, dim) = vectorOf(clean, store, settings)

        // ---- 去重：与本类型内最相似的记忆比较 ----
        val threshold = settings.dedupThreshold
        var best: StoredMemory? = null
        var bestScore = 0f
        for (m in store.items) {
            if (m.kind != kind) continue
            val sc = cosine(vector, m.vector)
            if (sc > bestScore) { bestScore = sc; best = m }
        }

        val items = store.items.toMutableList()
        val merged = best
        if (merged != null && bestScore >= threshold) {
            // 合并：保留更长的文本，累加命中，刷新向量与时间
            val idx = items.indexOfFirst { it.id == merged.id }
            items[idx] = merged.copy(
                text = if (clean.length > merged.text.length) clean else merged.text,
                updatedAt = now,
                hits = merged.hits + 1,
                vector = vector,
                dim = dim,
            )
            store = store.copy(items = items)
            save(prune(store, settings))
            return@withContext false
        }

        val item = StoredMemory(
            id = UUID.randomUUID().toString(),
            text = clean,
            source = source,
            createdAt = now,
            updatedAt = now,
            vector = vector,
            dim = dim,
            kind = kind,
        )
        items.add(item)
        store = store.copy(items = items)
        save(prune(store, settings))
        true
    }

    /** 超限淘汰：按 importance 从低到高删。 */
    private fun prune(store: MemoryStore, settings: Settings): MemoryStore {
        val limit = settings.memoryLimit.coerceAtLeast(20)
        if (store.items.size <= limit) return store
        val now = System.currentTimeMillis()
        // 摘要类永不淘汰（保留最重要的对话脉络）
        val keepSummaries = store.items.filter { it.kind != MemoryKind.DIALOG }
        val dialogs = store.items.filter { it.kind == MemoryKind.DIALOG }
        val room = (limit - keepSummaries.size).coerceAtLeast(1)
        val survivors = dialogs.sortedByDescending { it.importance(now) }.take(room)
        return store.copy(items = (survivors + keepSummaries).sortedBy { it.createdAt })
    }

    /** 检索相关记忆，返回 topK（含相似度分数），并累加命中次数。 */
    suspend fun retrieve(query: String, settings: Settings, topK: Int): List<MemoryHit> =
        withContext(Dispatchers.IO) {
            var store = load()
            if (store.items.isEmpty() || query.isBlank()) return@withContext emptyList()
            val (qvec, _) = vectorOf(query, store, settings)

            val scored = store.items
                .filter { it.vector.size == qvec.size }
                .map { MemoryHit(it, cosine(qvec, it.vector)) }
                .filter { it.score > 0.04f }
                .sortedByDescending { it.score }
                .take(topK.coerceAtLeast(1))

            // 命中计数（提升常用记忆的重要性，避免被淘汰）
            if (scored.isNotEmpty()) {
                val hitIds = scored.map { it.memory.id }.toSet()
                store = store.copy(items = store.items.map {
                    if (it.id in hitIds) it.copy(hits = it.hits + 1) else it
                })
                save(store)
            }
            scored
        }

    /** 取出所有历史消息（按时间升序），供摘要沉淀与新消息向量化使用。 */
    fun getAll(): List<StoredMemory> = load().items.sortedByDescending { it.createdAt }

    fun getAllAsc(): List<StoredMemory> = load().items.sortedBy { it.createdAt }

    fun summarizedUpTo(): Long = load().summarizedUpTo

    fun setSummarizedUpTo(ts: Long) {
        val store = load()
        save(store.copy(summarizedUpTo = ts))
    }

    fun delete(id: String) {
        val store = load()
        save(store.copy(items = store.items.filter { it.id != id }))
    }

    fun clear() = save(MemoryStore())

    fun count(): Int = load().items.size

    fun stats(): MemoryStats {
        val items = load().items
        return MemoryStats(
            total = items.size,
            summaries = items.count { it.kind == MemoryKind.SUMMARY },
            dialogs = items.count { it.kind == MemoryKind.DIALOG },
            profiles = items.count { it.kind == MemoryKind.PROFILE },
        )
    }
}

data class MemoryStats(
    val total: Int = 0,
    val summaries: Int = 0,
    val dialogs: Int = 0,
    val profiles: Int = 0,
)

data class MemoryHit(val memory: StoredMemory, val score: Float)
