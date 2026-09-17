package com.feiyu.app.ui

import android.app.Application
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.feiyu.app.data.chat.ChatHistory
import com.feiyu.app.data.chat.StoredMsg
import com.feiyu.app.data.image.ImageUtils
import com.feiyu.app.data.memory.MemoryKind
import com.feiyu.app.data.memory.MemoryStats
import com.feiyu.app.data.memory.StoredMemory
import com.feiyu.app.data.memory.Summarizer
import com.feiyu.app.data.memory.VectorMemory
import com.feiyu.app.data.model.ChatMessage
import com.feiyu.app.data.model.ContentPart
import com.feiyu.app.data.model.MessageContent
import com.feiyu.app.data.persona.Persona
import com.feiyu.app.data.remote.LlmClient
import com.feiyu.app.data.settings.Settings
import com.feiyu.app.data.settings.SettingsRepository
import com.feiyu.app.data.tts.SpeechRewriter
import com.feiyu.app.data.tts.TtsController
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.util.UUID

data class UiMessage(
    val id: String = UUID.randomUUID().toString(),
    val role: String,        // "user" | "assistant"
    val text: String,
    val isStreaming: Boolean = false,
    val imagePath: String? = null,   // 本地图片路径（用于气泡中显示）
)

enum class Screen { Chat, Settings, Memory }

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val repo = SettingsRepository(app)
    private val vectorMemory = VectorMemory(app)
    private val tts = TtsController(app)
    private val application = app

    /** 正在朗读的消息 id（null 表示未在朗读） */
    var speakingId by mutableStateOf<String?>(null)
        private set

    var messages by mutableStateOf<List<UiMessage>>(emptyList())
        private set
    var input by mutableStateOf("")
        private set

    /** 待发送的图片（在输入框中预览用） */
    var pendingImage by mutableStateOf<File?>(null)
        private set
    var isImportingImage by mutableStateOf(false)
        private set

    var isSending by mutableStateOf(false)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    var settings by mutableStateOf(Settings())
        private set
    var memoryItems by mutableStateOf<List<StoredMemory>>(emptyList())
        private set
    var memoryStats by mutableStateOf(MemoryStats())
        private set
    var statusHint by mutableStateOf<String?>(null)
        private set
    var screen by mutableStateOf(Screen.Chat)
        private set

    init {
        // 先读一次已保存的设置（确保欢迎语用对角色名），再持续订阅变更
        viewModelScope.launch {
            val first = repo.settingsFlow.first()
            settings = first
            tts.updateSettings(first)
            if (messages.isEmpty()) {
                messages = listOf(UiMessage(role = "assistant", text = welcome(first.displayName())))
            }
            repo.settingsFlow.collect {
                settings = it
                tts.updateSettings(it)
            }
        }
        val hist = ChatHistory.load(application)
        messages = hist.map { UiMessage(role = it.role, text = it.content, imagePath = it.imagePath) }
        refreshMemory()
    }

    fun updateInput(s: String) { input = s }

    fun navigate(s: Screen) { screen = s }

    fun clearHint() { statusHint = null }

    fun newChat() {
        ChatHistory.clear(application)
        vectorMemory.setSummarizedUpTo(0L)
        discardPendingImage()
        stopSpeak()
        messages = listOf(UiMessage(role = "assistant", text = "开始新对话啦，说吧～"))
    }

    // ------------------------------------------------------------------
    // 图片
    // ------------------------------------------------------------------

    /** 从相册选择图片 → 压缩入缓存 → 作为待发送图片。 */
    fun onImagePicked(uri: Uri) {
        viewModelScope.launch {
            isImportingImage = true
            error = null
            val file = ImageUtils.importToCache(application, uri)
            isImportingImage = false
            if (file == null) {
                error = "图片读取失败，请换一张试试"
                return@launch
            }
            // 替换掉之前的待发送图片，避免堆积
            pendingImage?.let { ImageUtils.delete(it) }
            pendingImage = file
        }
    }

    /** 取消待发送图片。 */
    fun discardPendingImage() {
        pendingImage?.let { ImageUtils.delete(it) }
        pendingImage = null
    }

    // ------------------------------------------------------------------
    // 记忆
    // ------------------------------------------------------------------

    fun refreshMemory() {
        memoryItems = vectorMemory.getAll()
        memoryStats = vectorMemory.stats()
    }

    fun deleteMemory(id: String) {
        vectorMemory.delete(id)
        refreshMemory()
    }

    fun clearMemory() {
        vectorMemory.clear()
        refreshMemory()
    }

    fun saveSettings(next: Settings) {
        tts.updateSettings(next)
        viewModelScope.launch { repo.update { next } }
    }

    // ------------------------------------------------------------------
    // 语音播报
    // ------------------------------------------------------------------

    /** 点击朗读按钮：正在读同一条则停止，否则开始朗读。 */
    fun toggleSpeak(messageId: String, text: String) {
        if (speakingId == messageId) {
            stopSpeak()
            return
        }
        startSpeak(messageId, text)
    }

    /** 启动朗读（必要时先做「朗读改写」）。 */
    private fun startSpeak(messageId: String, text: String) {
        val s = settings
        if (!s.ttsRewriteEnabled) {
            tts.speak(messageId, text) { id, err ->
                speakingId = id
                if (err != null) error = err
            }
            return
        }
        // 改写模式下：先标记为播放中（避免按钮状态闪烁），再异步改写
        viewModelScope.launch {
            speakingId = messageId
            val rewritten = SpeechRewriter.rewrite(s, text)
            if (speakingId != messageId) return@launch   // 期间被停止/切换了
            tts.speak(messageId, rewritten) { id, err ->
                speakingId = id
                if (err != null) error = err
            }
        }
    }

    fun stopSpeak() {
        tts.stop()
        speakingId = null
    }

    /**
     * 试听当前语音设置。
     * 用于设置页调完语速/音调/音色后立即听效果。
     */
    fun previewVoice(text: String = PREVIEW_TEXT) {
        stopSpeak()
        tts.preview(text) { err ->
            if (err != null) error = err
        }
    }

    override fun onCleared() {
        tts.release()
        super.onCleared()
    }

    // ------------------------------------------------------------------
    // 发送
    // ------------------------------------------------------------------

    fun send() {
        val text = input.trim()
        val image = pendingImage
        if ((text.isBlank() && image == null) || isSending) return
        val s = settings

        viewModelScope.launch {
            isSending = true
            error = null
            input = ""
            pendingImage = null

            val userMsg = UiMessage(
                role = "user",
                text = text,
                imagePath = image?.absolutePath,
            )
            messages = messages + userMsg + UiMessage(role = "assistant", text = "", isStreaming = true)
            val assistantIndex = messages.lastIndex

            val sb = StringBuilder()
            var failed = false
            try {
                // 1) 检索相关长期记忆（用文字检索；纯图片消息跳过检索）
                val memoryCtx = if (s.memoryEnabled && text.isNotBlank()) {
                    buildMemoryContext(text, s)
                } else {
                    null
                }
                // 2) 只取最近 N 轮的滚动窗口作为短期记忆
                val window = buildWindow(s)
                val sys = Persona.systemMessage(
                    s.displayName(), s.profileMap(), memoryCtx,
                    visionEnabled = s.visionEnabled && image != null,
                )
                // 3) 组装当前这条用户消息（可能含图片）
                val userContent = buildUserContent(text, image)
                val reqMessages = listOf(sys) + window + listOf(ChatMessage("user", userContent))

                // 4) 流式请求
                LlmClient.streamChatFlow(s, reqMessages, withImage = image != null).collect { delta ->
                    sb.append(delta)
                    updateAssistant(assistantIndex, sb.toString(), true)
                }
            } catch (e: Exception) {
                failed = true
                error = e.message
                // 关键：保留已经收到的内容，只在末尾追加中断提示，绝不整体覆盖
                val partial = sb.toString()
                val notice = if (partial.isBlank()) {
                    "（出错了：${e.message}）"
                } else {
                    "$partial\n\n（回复中断了：${e.message}）"
                }
                updateAssistant(assistantIndex, notice, false)
            } finally {
                isSending = false
            }

            val reply = sb.toString()
            if (!failed) {
                updateAssistant(assistantIndex, reply, false)
                // 自动朗读（若已开启）
                if (s.ttsEnabled && s.ttsAutoSpeak && reply.isNotBlank()) {
                    val msgId = messages.getOrNull(assistantIndex)?.id
                    if (msgId != null) {
                        startSpeak(msgId, reply)
                    }
                }
            }

            // 5) 落盘历史 + 向量化（只保存真正收到的内容，避免把错误提示混进记忆）
            if (reply.isNotBlank()) {
                withContext(Dispatchers.IO) {
                    val hist = ChatHistory.load(application)
                    val now = System.currentTimeMillis()
                    hist.add(StoredMsg("user", text, now, image?.absolutePath))
                    hist.add(StoredMsg("assistant", reply, now))
                    ChatHistory.save(application, hist)
                    if (s.memoryEnabled) {
                        // 图片消息的文本部分仍然参与记忆（图片本身不向量化）
                        if (text.isNotBlank()) {
                            vectorMemory.addMemory(text, "用户说", s, MemoryKind.DIALOG)
                        }
                        vectorMemory.addMemory(reply, "${s.displayName()}回", s, MemoryKind.DIALOG)
                    }
                }
                refreshMemory()
            }

            // 6) 超出窗口的旧对话 → 摘要沉淀（异步，不阻塞界面）
            if (!failed && s.memoryEnabled && s.summaryEnabled) {
                launch { runSummarize(s) }
            }
        }
    }

    /**
     * 组装用户消息内容。
     * 无图片 → 纯文本；有图片 → 多模态分段（图片在前，文本在后）。
     */
    private suspend fun buildUserContent(text: String, image: File?): MessageContent {
        if (image == null) return MessageContent.of(text)

        val dataUrl = ImageUtils.toDataUrl(image)
        if (dataUrl == null) {
            // 图片编码失败，降级为纯文本并给出提示
            error = "图片编码失败，本次以纯文本发送"
            return MessageContent.of(text)
        }
        val parts = mutableListOf(ContentPart.image(dataUrl))
        if (text.isNotBlank()) parts += ContentPart.text(text)
        return MessageContent.ofParts(parts)
    }

    /** 滚动窗口：只取最近 historyWindow 轮（1 轮 = user+assistant 两条）。 */
    private fun buildWindow(s: Settings): List<ChatMessage> {
        val maxMsgs = (s.historyWindow.coerceAtLeast(1)) * 2
        val hist = ChatHistory.load(application)
        val recent = if (hist.size <= maxMsgs) hist else hist.subList(hist.size - maxMsgs, hist.size)
        // 历史中的图片不再重复上传（体积大且模型已有上下文），只保留文字，
        // 若该条只有图片没有文字，用占位说明代替。
        return recent.map { m ->
            val t = m.content.ifBlank { if (m.imagePath != null) "（用户发来了一张图片）" else "" }
            ChatMessage(m.role, MessageContent.of(t))
        }
    }

    /** 把窗口之外的旧对话摘要成一条 SUMMARY 记忆。 */
    private suspend fun runSummarize(s: Settings) {
        try {
            val hist = ChatHistory.load(application)
            val maxMsgs = (s.historyWindow.coerceAtLeast(1)) * 2
            if (hist.size <= maxMsgs) return

            val watermark = vectorMemory.summarizedUpTo()
            // 窗口之外的旧消息（尚未摘要过的）
            val older = hist.subList(0, hist.size - maxMsgs)
                .filter { it.ts > watermark }
            if (older.size < 4) return

            val msgs = older.map { m ->
                val t = m.content.ifBlank { if (m.imagePath != null) "（发了一张图片）" else "" }
                ChatMessage(m.role, MessageContent.of(t))
            }
            val summary = Summarizer.summarize(s, msgs) ?: return
            if (summary.isBlank()) return

            vectorMemory.addMemory(summary, "对话摘要", s, MemoryKind.SUMMARY)
            val newWatermark = older.maxOf { it.ts }
            vectorMemory.setSummarizedUpTo(newWatermark)
            withContext(Dispatchers.Main) {
                refreshMemory()
                statusHint = "已沉淀 ${older.size} 条旧对话为摘要记忆"
            }
        } catch (_: Exception) {
            // 摘要失败不影响主流程
        }
    }

    private fun updateAssistant(index: Int, text: String, streaming: Boolean) {
        if (index < 0 || index >= messages.size) return
        messages = messages.toMutableList().also {
            it[index] = it[index].copy(text = text, isStreaming = streaming)
        }
    }

    private suspend fun buildMemoryContext(query: String, s: Settings): String {
        val hits = vectorMemory.retrieve(query, s, s.topK.coerceAtLeast(1))
        if (hits.isEmpty()) return ""
        val lines = hits.joinToString("\n") { "- ${it.memory.text}" }
        return "【相关记忆】（以下是从以往对话中检索到的相关内容，可作为参考，但不要生硬复述）\n$lines"
    }

    companion object {
        /** 欢迎语模板（用角色名填充） */
        fun welcome(name: String): String = "你好呀，我是 $name。想聊点什么？"

        /** 试听示例文本 */
        const val PREVIEW_TEXT = "你好呀，我是你的语音助手，这是当前的朗读效果。"
    }
}
