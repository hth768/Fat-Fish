package com.feiyu.app.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import android.graphics.BitmapFactory
import java.io.File

@Composable
fun ChatScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val messages = vm.messages
    val listState = rememberLazyListState()

    // 系统相册选择器（Photo Picker，Android 13+ 原生，低版本自动回落）
    val pickImage = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.PickVisualMedia(),
    ) { uri: Uri? ->
        if (uri != null) vm.onImagePicked(uri)
    }

    // 跟随内容增长：仅在最后一条文本长度或条数变化时滚动，
    // 保证流式吐字过程中视图一直贴底
    val tailLength = messages.lastOrNull()?.text?.length ?: 0
    LaunchedEffect(messages.size, tailLength) {
        if (messages.isNotEmpty()) {
            listState.scrollToItem(messages.lastIndex)
            listState.animateScrollToItem(messages.lastIndex)
        }
    }

    Column(modifier.fillMaxSize()) {
        // 摘要沉淀等后台动作的轻提示
        vm.statusHint?.let { hint ->
            Surface(
                color = MaterialTheme.colorScheme.secondaryContainer,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp),
                shape = RoundedCornerShape(8.dp),
            ) {
                Row(
                    Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(hint, style = MaterialTheme.typography.labelSmall, modifier = Modifier.weight(1f))
                    TextButton(onClick = { vm.clearHint() }) { Text("知道了") }
                }
            }
        }

        LazyColumn(
            state = listState,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            contentPadding = PaddingValues(vertical = 12.dp),
        ) {
            items(messages, key = { it.id }) { msg ->
                if (msg.text.isBlank() && msg.imagePath == null && msg.isStreaming) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
                        Text(
                            "${vm.settings.displayName()}正在输入…",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            fontSize = 13.sp,
                        )
                    }
                } else {
                    MessageBubble(
                        msg = msg,
                        ttsVisible = vm.settings.ttsEnabled && !msg.isStreaming && msg.text.isNotBlank(),
                        isSpeaking = vm.speakingId != null && vm.speakingId == msg.id,
                        onSpeakClick = { vm.toggleSpeak(msg.id, msg.text) },
                    )
                }
            }
        }

        // 待发送图片预览
        vm.pendingImage?.let { file ->
            Surface(
                color = MaterialTheme.colorScheme.surfaceVariant,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp),
                shape = RoundedCornerShape(8.dp),
            ) {
                Row(
                    Modifier.padding(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    LocalImage(
                        file = file,
                        modifier = Modifier.size(64.dp).clip(RoundedCornerShape(6.dp)),
                    )
                    Spacer(Modifier.width(10.dp))
                    Column(Modifier.weight(1f)) {
                        Text("已选择 1 张图片", style = MaterialTheme.typography.labelLarge)
                        Text(
                            "将连同文字一起发送给模型识别",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    IconButton(onClick = { vm.discardPendingImage() }) {
                        Icon(Icons.Filled.Close, contentDescription = "移除图片")
                    }
                }
            }
        }

        if (vm.isImportingImage) {
            LinearProgressIndicator(Modifier.fillMaxWidth().padding(horizontal = 8.dp))
        }

        // 输入区
        Row(
            Modifier
                .fillMaxWidth()
                .padding(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(
                onClick = {
                    pickImage.launch(
                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
                    )
                },
                enabled = !vm.isSending && vm.settings.visionEnabled,
            ) {
                Icon(
                    Icons.Filled.Image,
                    contentDescription = "发送图片",
                    tint = if (vm.settings.visionEnabled) {
                        MaterialTheme.colorScheme.primary
                    } else {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    },
                )
            }
            OutlinedTextField(
                value = vm.input,
                onValueChange = { vm.updateInput(it) },
                modifier = Modifier.weight(1f),
                placeholder = { Text("说点什么吧～") },
                enabled = !vm.isSending,
                maxLines = 4,
            )
            Spacer(Modifier.width(8.dp))
            IconButton(
                onClick = { vm.send() },
                enabled = !vm.isSending && (vm.input.isNotBlank() || vm.pendingImage != null),
            ) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送")
            }
        }
    }
}

@Composable
private fun MessageBubble(
    msg: UiMessage,
    ttsVisible: Boolean,
    isSpeaking: Boolean,
    onSpeakClick: () -> Unit,
) {
    val isUser = msg.role == "user"
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start,
    ) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            color = if (isUser) {
                MaterialTheme.colorScheme.primaryContainer
            } else {
                MaterialTheme.colorScheme.surfaceVariant
            },
            modifier = Modifier.widthIn(max = 320.dp),
        ) {
            Column(Modifier.padding(10.dp)) {
                // 图片
                msg.imagePath?.let { path ->
                    val file = File(path)
                    if (file.exists()) {
                        LocalImage(
                            file = file,
                            modifier = Modifier
                                .fillMaxWidth()
                                .heightIn(max = 260.dp)
                                .clip(RoundedCornerShape(10.dp)),
                        )
                        if (msg.text.isNotBlank()) Spacer(Modifier.height(8.dp))
                    }
                }
                // 文字
                if (msg.text.isNotBlank()) {
                    SelectionContainer {
                        Text(
                            text = msg.text,
                            fontSize = 15.sp,
                            lineHeight = 21.sp,
                            color = MaterialTheme.colorScheme.onSurface,
                        )
                    }
                }
                // 朗读按钮（仅助手消息、非流式中显示）
                if (ttsVisible) {
                    Spacer(Modifier.height(2.dp))
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.End,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        IconButton(
                            onClick = onSpeakClick,
                            modifier = Modifier.size(32.dp),
                        ) {
                            Icon(
                                imageVector = if (isSpeaking) {
                                    Icons.Filled.Stop
                                } else {
                                    Icons.AutoMirrored.Filled.VolumeUp
                                },
                                contentDescription = if (isSpeaking) "停止朗读" else "朗读",
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.size(18.dp),
                            )
                        }
                    }
                }
            }
        }
    }
}

/** 从本地文件加载并显示图片（Compose 无内置文件加载器，手动解码）。 */
@Composable
private fun LocalImage(file: File, modifier: Modifier = Modifier) {
    val bitmap = remember(file.absolutePath) {
        try {
            BitmapFactory.decodeFile(file.absolutePath)
        } catch (_: Exception) {
            null
        }
    }
    if (bitmap != null) {
        Image(
            bitmap = bitmap.asImageBitmap(),
            contentDescription = "图片",
            modifier = modifier,
            contentScale = ContentScale.Fit,
        )
    } else {
        Box(
            modifier,
            contentAlignment = Alignment.Center,
        ) {
            Text("图片无法显示", style = MaterialTheme.typography.labelSmall)
        }
    }
}
