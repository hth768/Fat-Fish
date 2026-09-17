package com.feiyu.app.ui

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppScaffold(vm: MainViewModel) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(vm.settings.displayName()) },
                navigationIcon = {
                    if (vm.screen != Screen.Chat) {
                        IconButton(onClick = { vm.navigate(Screen.Chat) }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                        }
                    }
                },
                actions = {
                    if (vm.screen == Screen.Chat) {
                        IconButton(onClick = { vm.newChat() }) {
                            Icon(Icons.Filled.Add, contentDescription = "新对话")
                        }
                    }
                    if (vm.screen != Screen.Memory) {
                        IconButton(onClick = { vm.navigate(Screen.Memory) }) {
                            Icon(Icons.Filled.Bookmark, contentDescription = "记忆")
                        }
                    }
                    if (vm.screen != Screen.Settings) {
                        IconButton(onClick = { vm.navigate(Screen.Settings) }) {
                            Icon(Icons.Filled.Settings, contentDescription = "设置")
                        }
                    }
                },
            )
        },
    ) { inner ->
        when (vm.screen) {
            Screen.Chat -> ChatScreen(vm, Modifier.padding(inner))
            Screen.Settings -> SettingsScreen(vm, Modifier.padding(inner))
            Screen.Memory -> MemoryScreen(vm, Modifier.padding(inner))
        }
    }
}
