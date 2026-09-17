package com.feiyu.app.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.Color
import androidx.compose.runtime.Composable

private val PinkLight = lightColorScheme(
    primary = Color(0xFFD63384),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFFBD7E9),
    secondary = Color(0xFF7A4FBF),
    background = Color(0xFFFCF7FB),
    surface = Color.White,
    surfaceVariant = Color(0xFFF2E7F0),
)

private val PinkDark = darkColorScheme(
    primary = Color(0xFFF47FB5),
    onPrimary = Color(0xFF3A0020),
    primaryContainer = Color(0xFF5A1038),
    secondary = Color(0xFFB79CF2),
    background = Color(0xFF1B131A),
    surface = Color(0xFF241A22),
    surfaceVariant = Color(0xFF33252F),
)

@Composable
fun FeiyuTheme(
    useDark: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (useDark) PinkDark else PinkLight,
        content = content,
    )
}
