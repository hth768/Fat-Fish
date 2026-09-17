package com.feiyu.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.feiyu.app.data.memory.MemoryKind
import com.feiyu.app.data.memory.StoredMemory
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun MemoryScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val items = vm.memoryItems
    val stats = vm.memoryStats

    Column(modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("本地记忆（${stats.total} 条）", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            Button(onClick = { vm.clearMemory() }, enabled = items.isNotEmpty()) {
                Text("清空")
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "摘要 ${stats.summaries} · 对话 ${stats.dialogs} · 画像 ${stats.profiles}",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(10.dp))

        if (items.isEmpty()) {
            Text(
                "还没有记忆。聊天时对话会自动被向量化存储；\n超过窗口轮数的旧对话会被压缩成摘要长期保留。",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(items, key = { it.id }) { mem ->
                    MemoryCard(mem, onDelete = { vm.deleteMemory(mem.id) })
                }
            }
        }
    }
}

@Composable
private fun MemoryCard(mem: StoredMemory, onDelete: () -> Unit) {
    val fmt = remember { SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()) }
    Card(Modifier.fillMaxWidth()) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.Top) {
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = kindLabel(mem.kind),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary,
                    )
                    if (mem.hits > 0) {
                        Text(
                            "  · 命中 ${mem.hits}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                Spacer(Modifier.height(4.dp))
                Text(mem.text, style = MaterialTheme.typography.bodyMedium)
                Spacer(Modifier.height(4.dp))
                Text(
                    "${mem.source} · ${fmt.format(Date(mem.createdAt))}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            IconButton(onClick = onDelete) {
                Icon(Icons.Filled.Delete, contentDescription = "删除")
            }
        }
    }
}

private fun kindLabel(kind: String): String = when (kind) {
    MemoryKind.SUMMARY -> "对话摘要"
    MemoryKind.PROFILE -> "用户画像"
    else -> "对话片段"
}
