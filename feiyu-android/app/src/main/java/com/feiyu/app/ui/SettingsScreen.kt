package com.feiyu.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.feiyu.app.data.persona.Persona
import com.feiyu.app.data.settings.PROVIDER_PRESETS
import com.feiyu.app.data.settings.Settings
import com.feiyu.app.data.settings.presetById

/** 云端 TTS 常用音色（OpenAI 规范命名，多数兼容服务通用）。 */
private val TTS_VOICE_PRESETS: List<Pair<String, String>> = listOf(
    "alloy" to "中性，均衡",
    "echo" to "男声，沉稳",
    "fable" to "叙事感，温和",
    "onyx" to "男声，低沉",
    "nova" to "女声，明亮",
    "shimmer" to "女声，轻柔",
)

/**
 * 常见 TTS 服务商的「情感参数」写法。
 * 各家命名不同，故做成预设 + 可手动覆盖。
 */
private data class EmotionPreset(
    val id: String,
    val label: String,
    val key: String,
    val value: String,
    val note: String,
)

private val TTS_EMOTION_PRESETS: List<EmotionPreset> = listOf(
    EmotionPreset("custom", "自定义 / 不设置", "", "", "参数名与值全部手动填写"),
    EmotionPreset("azure", "Azure TTS", "voice_style", "cheerful", "值可填 cheerful / sad / angry / friendly 等"),
    EmotionPreset("minimax", "MiniMax", "emotion", "happy", "值可填 happy / sad / angry / fearful / disgusted / surprised / neutral"),
    EmotionPreset("volc", "火山引擎", "emotion", "happy", "值可填 happy / sad / angry / surprised / fear / hate / excited 等"),
    EmotionPreset("openai", "OpenAI（不支持情感）", "", "", "OpenAI 的 tts-1 系列无情感参数，填了会被忽略或报错"),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    var draft by remember { mutableStateOf(vm.settings) }
    var expanded by remember { mutableStateOf(false) }

    Column(
        modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        // 供应商
        ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
            androidx.compose.material3.TextField(
                value = presetById(draft.providerId)?.label ?: draft.providerId,
                onValueChange = {},
                readOnly = true,
                label = { Text("供应商") },
                modifier = Modifier.menuAnchor().fillMaxWidth(),
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded) },
                colors = ExposedDropdownMenuDefaults.textFieldColors(),
            )
            ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                PROVIDER_PRESETS.forEach { p ->
                    DropdownMenuItem(
                        text = { Text(p.label) },
                        onClick = {
                            draft = draft.copy(
                                providerId = p.id,
                                baseUrl = if (p.id != "custom") p.baseUrl else draft.baseUrl,
                                model = if (p.id != "custom") p.defaultModel else draft.model,
                            )
                            expanded = false
                        },
                    )
                }
            }
        }

        TextField(draft.baseUrl, { draft = draft.copy(baseUrl = it) }, "API 地址（需含 /v1 等路径与结尾 /）", singleLine = true)
        TextField(draft.apiKey, { draft = draft.copy(apiKey = it) }, "API Key", password = true, singleLine = true)
        TextField(draft.model, { draft = draft.copy(model = it) }, "模型名", singleLine = true)

        HorizontalDivider()
        Text("图片识别", style = MaterialTheme.typography.titleMedium)
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("启用发送图片", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "开启后可在聊天页选择相册图片发给模型识别",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = draft.visionEnabled,
                onCheckedChange = { draft = draft.copy(visionEnabled = it) },
            )
        }
        TextField(
            draft.visionModel,
            { draft = draft.copy(visionModel = it) },
            "视觉模型名（留空则沿用上面的主模型）",
            singleLine = true,
        )
        Text(
            "说明：默认与主模型相同（deepseek-flash）。\n" +
                "若图片识别报错，可能是该模型暂不支持视觉输入，可在此单独填一个视觉模型。\n" +
                "注意该模型必须与上面的 API 地址 / Key 属于同一家服务商。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        HorizontalDivider()
        Text("语音播报", style = MaterialTheme.typography.titleMedium)
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("显示朗读按钮", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "每条回复下方出现小喇叭，点击朗读",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = draft.ttsEnabled,
                onCheckedChange = { draft = draft.copy(ttsEnabled = it) },
            )
        }
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("自动朗读回复", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "回复完成后自动朗读，无需手动点击",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = draft.ttsAutoSpeak,
                onCheckedChange = { draft = draft.copy(ttsAutoSpeak = it) },
            )
        }
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("使用云端语音", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "关闭时用手机自带 TTS（免费 / 离线）",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = draft.ttsUseCloud,
                onCheckedChange = { draft = draft.copy(ttsUseCloud = it) },
            )
        }
        if (draft.ttsUseCloud) {
            TextField(
                draft.ttsBaseUrl,
                { draft = draft.copy(ttsBaseUrl = it) },
                "TTS 地址（OpenAI 兼容，如 https://api.openai.com/v1）",
                singleLine = true,
            )
            TextField(
                draft.ttsApiKey,
                { draft = draft.copy(ttsApiKey = it) },
                "TTS Key",
                password = true,
                singleLine = true,
            )
            TextField(
                draft.ttsModel,
                { draft = draft.copy(ttsModel = it) },
                "TTS 模型名（如 tts-1）",
                singleLine = true,
            )
            // 音色：常用预设下拉 + 手动填写
            var voiceExpanded by remember { mutableStateOf(false) }
            ExposedDropdownMenuBox(expanded = voiceExpanded, onExpandedChange = { voiceExpanded = it }) {
                androidx.compose.material3.TextField(
                    value = draft.ttsVoice,
                    onValueChange = { draft = draft.copy(ttsVoice = it) },
                    label = { Text("音色（可直接输入或选预设）") },
                    modifier = Modifier.menuAnchor().fillMaxWidth(),
                    singleLine = true,
                    trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(voiceExpanded) },
                    colors = ExposedDropdownMenuDefaults.textFieldColors(),
                )
                ExposedDropdownMenu(expanded = voiceExpanded, onDismissRequest = { voiceExpanded = false }) {
                    TTS_VOICE_PRESETS.forEach { (id, desc) ->
                        DropdownMenuItem(
                            text = { Text("$id — $desc") },
                            onClick = {
                                draft = draft.copy(ttsVoice = id)
                                voiceExpanded = false
                            },
                        )
                    }
                }
            }
            Text(
                "说明：走 OpenAI 兼容的 /audio/speech 接口。\n" +
                    "支持 OpenAI、硅基流动等同类服务，填对应地址与 Key 即可。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            Text(
                "当前使用手机自带语音引擎。若中文发音不自然，\n" +
                    "可在系统设置 → 无障碍 → 文字转语音中更换引擎。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        // 语速（两种引擎都适用）
        Text("语速：%.2fx".format(draft.ttsSpeed), style = MaterialTheme.typography.labelMedium)
        Slider(
            value = draft.ttsSpeed,
            onValueChange = { draft = draft.copy(ttsSpeed = it) },
            valueRange = 0.5f..2.0f,
        )

        // 音调（仅系统 TTS 支持）
        Text(
            "音调：%.2f".format(draft.ttsPitch) +
                if (draft.ttsUseCloud) "（云端引擎暂不支持音调）" else "",
            style = MaterialTheme.typography.labelMedium,
        )
        Slider(
            value = draft.ttsPitch,
            onValueChange = { draft = draft.copy(ttsPitch = it) },
            valueRange = 0.5f..2.0f,
            enabled = !draft.ttsUseCloud,
        )

        // 停顿节奏
        HorizontalDivider()
        Text("停顿节奏", style = MaterialTheme.typography.titleMedium)
        Text(
            "长回复会被自动切句朗读，在句子/段落之间插入停顿，避免一口气念完。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text("句间停顿：${draft.ttsSentencePause} 毫秒", style = MaterialTheme.typography.labelMedium)
        Slider(
            value = draft.ttsSentencePause.toFloat(),
            onValueChange = { draft = draft.copy(ttsSentencePause = it.toInt()) },
            valueRange = 0f..800f,
            steps = 15,
        )
        Text("段间停顿：${draft.ttsParagraphPause} 毫秒", style = MaterialTheme.typography.labelMedium)
        Slider(
            value = draft.ttsParagraphPause.toFloat(),
            onValueChange = { draft = draft.copy(ttsParagraphPause = it.toInt()) },
            valueRange = 0f..1500f,
            steps = 14,
        )

        // 情感参数（仅云端引擎有效）
        HorizontalDivider()
        Text("情感语气", style = MaterialTheme.typography.titleMedium)
        if (!draft.ttsUseCloud) {
            Text(
                "情感参数只对云端语音生效。系统语音引擎不支持情感控制，\n" +
                    "只能通过语速/音调间接调节。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            Text(
                "不同服务商的参数名与取值不同，选一个预设或手动填写。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            var emoExpanded by remember { mutableStateOf(false) }
            val currentEmo = TTS_EMOTION_PRESETS.firstOrNull { it.id == draft.ttsEmotionPresetId }
                ?: TTS_EMOTION_PRESETS.first()
            ExposedDropdownMenuBox(expanded = emoExpanded, onExpandedChange = { emoExpanded = it }) {
                androidx.compose.material3.TextField(
                    value = currentEmo.label,
                    onValueChange = {},
                    readOnly = true,
                    label = { Text("服务商预设") },
                    modifier = Modifier.menuAnchor().fillMaxWidth(),
                    trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(emoExpanded) },
                    colors = ExposedDropdownMenuDefaults.textFieldColors(),
                )
                ExposedDropdownMenu(expanded = emoExpanded, onDismissRequest = { emoExpanded = false }) {
                    TTS_EMOTION_PRESETS.forEach { preset ->
                        DropdownMenuItem(
                            text = { Text(preset.label) },
                            onClick = {
                                draft = draft.copy(
                                    ttsEmotionPresetId = preset.id,
                                    ttsEmotionKey = preset.key,
                                    ttsEmotionValue = preset.value,
                                )
                                emoExpanded = false
                            },
                        )
                    }
                }
            }
            Text(
                currentEmo.note,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            TextField(
                draft.ttsEmotionKey,
                { draft = draft.copy(ttsEmotionKey = it, ttsEmotionPresetId = "custom") },
                "参数名（如 emotion / voice_style）",
                singleLine = true,
            )
            TextField(
                draft.ttsEmotionValue,
                { draft = draft.copy(ttsEmotionValue = it, ttsEmotionPresetId = "custom") },
                "参数值（如 happy / cheerful）",
                singleLine = true,
            )
        }

        // 试听
        HorizontalDivider()
        OutlinedButton(
            onClick = {
                // 先保存再试听，确保用最新参数
                vm.saveSettings(draft)
                vm.previewVoice()
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("试听当前效果")
        }

        // 朗读改写
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("朗读前改写稿件", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "用大模型把回复改写成口语化、带停顿的版本，听感更自然；" +
                        "每次朗读多消耗一次模型调用",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = draft.ttsRewriteEnabled,
                onCheckedChange = { draft = draft.copy(ttsRewriteEnabled = it) },
            )
        }

        // 温度
        Text("温度：%.2f".format(draft.temperature), style = MaterialTheme.typography.labelMedium)
        Slider(
            value = draft.temperature,
            onValueChange = { draft = draft.copy(temperature = it) },
            valueRange = 0f..2f,
        )

        HorizontalDivider()
        Text("角色人设", style = MaterialTheme.typography.titleMedium)
        Text(
            "名字和下面的字段都可以随意改，不限于预设角色。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        TextField(
            draft.characterName,
            { draft = draft.copy(characterName = it, personaPresetId = "custom") },
            "角色名（显示在标题栏与对话中）",
            singleLine = true,
        )

        // 预设模板（仅作起手，套用后仍可自由修改）
        var presetExpanded by remember { mutableStateOf(false) }
        ExposedDropdownMenuBox(expanded = presetExpanded, onExpandedChange = { presetExpanded = it }) {
            androidx.compose.material3.TextField(
                value = Persona.presetById(draft.personaPresetId)?.label ?: "自定义",
                onValueChange = {},
                readOnly = true,
                label = { Text("套用预设模板") },
                modifier = Modifier.menuAnchor().fillMaxWidth(),
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(presetExpanded) },
                colors = ExposedDropdownMenuDefaults.textFieldColors(),
            )
            ExposedDropdownMenu(expanded = presetExpanded, onDismissRequest = { presetExpanded = false }) {
                Persona.PRESETS.forEach { p ->
                    DropdownMenuItem(
                        text = { Text(p.label) },
                        onClick = {
                            draft = draft.copy(
                                characterName = p.name,
                                personaPresetId = p.id,
                                identity = p.fields["identity"].orEmpty(),
                                personality = p.fields["personality"].orEmpty(),
                                abilities = p.fields["abilities"].orEmpty(),
                                habits = p.fields["habits"].orEmpty(),
                                extra = p.fields["extra"].orEmpty(),
                            )
                            presetExpanded = false
                        },
                    )
                }
                DropdownMenuItem(
                    text = { Text("清空（从零自定义）") },
                    onClick = {
                        val empty = Persona.emptyFields()
                        draft = draft.copy(
                            personaPresetId = "custom",
                            identity = empty["identity"].orEmpty(),
                            personality = empty["personality"].orEmpty(),
                            abilities = empty["abilities"].orEmpty(),
                            habits = empty["habits"].orEmpty(),
                            extra = empty["extra"].orEmpty(),
                        )
                        presetExpanded = false
                    },
                )
            }
        }

        TextField(
            draft.identity,
            { draft = draft.copy(identity = it, personaPresetId = "custom") },
            "身份", multiline = true,
        )
        TextField(
            draft.personality,
            { draft = draft.copy(personality = it, personaPresetId = "custom") },
            "性格", multiline = true,
        )
        TextField(
            draft.abilities,
            { draft = draft.copy(abilities = it, personaPresetId = "custom") },
            "能力", multiline = true,
        )
        TextField(
            draft.habits,
            { draft = draft.copy(habits = it, personaPresetId = "custom") },
            "习惯", multiline = true,
        )
        TextField(
            draft.extra,
            { draft = draft.copy(extra = it, personaPresetId = "custom") },
            "其他", multiline = true,
        )

        HorizontalDivider()
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Text("启用向量记忆", style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
            Switch(checked = draft.memoryEnabled, onCheckedChange = { draft = draft.copy(memoryEnabled = it) })
        }
        TextField(
            draft.topK.toString(),
            { draft = draft.copy(topK = it.toIntOrNull()?.coerceAtLeast(1) ?: 5) },
            "记忆检索条数 (TopK)",
            singleLine = true,
            keyboardType = KeyboardType.Number,
        )
        TextField(
            draft.memoryLimit.toString(),
            { draft = draft.copy(memoryLimit = it.toIntOrNull()?.coerceAtLeast(20) ?: 800) },
            "记忆条数上限（超出按重要性淘汰）",
            singleLine = true,
            keyboardType = KeyboardType.Number,
        )
        Text("去重阈值：%.2f（越接近 1 越不易合并）".format(draft.dedupThreshold), style = MaterialTheme.typography.labelMedium)
        Slider(
            value = draft.dedupThreshold,
            onValueChange = { draft = draft.copy(dedupThreshold = it) },
            valueRange = 0.80f..0.99f,
        )

        HorizontalDivider()
        Text("会话窗口与摘要", style = MaterialTheme.typography.titleMedium)
        TextField(
            draft.historyWindow.toString(),
            { draft = draft.copy(historyWindow = it.toIntOrNull()?.coerceAtLeast(1) ?: 12) },
            "记忆窗口轮数（每次发给模型的最近对话）",
            singleLine = true,
            keyboardType = KeyboardType.Number,
        )
        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("旧对话自动摘要沉淀", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "超出窗口的对话会用大模型压缩成摘要记忆，保住长期连贯性",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(checked = draft.summaryEnabled, onCheckedChange = { draft = draft.copy(summaryEnabled = it) })
        }

        Text("远程 Embedding（可选，留空则用离线 TF-IDF）", style = MaterialTheme.typography.titleMedium)
        TextField(draft.embeddingBaseUrl, { draft = draft.copy(embeddingBaseUrl = it) }, "Embedding 地址（/v1）", singleLine = true)
        TextField(draft.embeddingApiKey, { draft = draft.copy(embeddingApiKey = it) }, "Embedding Key", password = true, singleLine = true)
        TextField(draft.embeddingModel, { draft = draft.copy(embeddingModel = it) }, "Embedding 模型名", singleLine = true)

        Spacer(Modifier.height(8.dp))
        Button(
            onClick = {
                vm.saveSettings(draft)
                vm.navigate(Screen.Chat)
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("保存")
        }
    }
}

@Composable
private fun TextField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    modifier: Modifier = Modifier.fillMaxWidth(),
    singleLine: Boolean = false,
    multiline: Boolean = false,
    password: Boolean = false,
    keyboardType: KeyboardType = KeyboardType.Text,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        modifier = modifier,
        singleLine = singleLine,
        maxLines = if (multiline) 4 else if (singleLine) 1 else Int.MAX_VALUE,
        visualTransformation = if (password) PasswordVisualTransformation() else androidx.compose.ui.text.input.VisualTransformation.None,
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
    )
}
