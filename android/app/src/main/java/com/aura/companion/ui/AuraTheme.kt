package com.aura.companion.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * AURA design: warm paper, open assistant prose, quiet user cards and one muted
 * blue-violet accent. System Chinese type at 16sp/26sp, generous 8dp spacing,
 * thin borders and native modal/navigation behavior keep the interface familiar
 * without copying another product's marks. Theme tokens cover both appearances;
 * the UI adds no decorative motion or downloadable typefaces.
 */
private val LightPalette = lightColorScheme(
    primary = Color(0xFF626695),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFEEEEF8),
    onPrimaryContainer = Color(0xFF41456C),
    secondary = Color(0xFF6F72A8),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFEEEEF8),
    onSecondaryContainer = Color(0xFF41456C),
    background = Color(0xFFFAF9F6),
    onBackground = Color(0xFF252525),
    surface = Color(0xFFFAF9F6),
    onSurface = Color(0xFF252525),
    surfaceVariant = Color(0xFFF0EFEC),
    onSurfaceVariant = Color(0xFF686862),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFF5F4F1),
    surfaceContainer = Color(0xFFF0EFEC),
    outline = Color(0xFF9B9B96),
    outlineVariant = Color(0xFFE2E1DC),
    error = Color(0xFFA0443C),
    errorContainer = Color(0xFFF8E9E6),
    onErrorContainer = Color(0xFF792F29),
)

private val DarkPalette = darkColorScheme(
    primary = Color(0xFFC2C4F0),
    onPrimary = Color(0xFF2B2E50),
    primaryContainer = Color(0xFF303149),
    onPrimaryContainer = Color(0xFFD4D6F8),
    secondary = Color(0xFFB8BBE4),
    onSecondary = Color(0xFF2B2E50),
    secondaryContainer = Color(0xFF303149),
    onSecondaryContainer = Color(0xFFD4D6F8),
    background = Color(0xFF191A1C),
    onBackground = Color(0xFFECECE7),
    surface = Color(0xFF191A1C),
    onSurface = Color(0xFFECECE7),
    surfaceVariant = Color(0xFF27282C),
    onSurfaceVariant = Color(0xFFB5B6B2),
    surfaceContainerLowest = Color(0xFF202124),
    surfaceContainerLow = Color(0xFF232427),
    surfaceContainer = Color(0xFF28292D),
    outline = Color(0xFF8A8C90),
    outlineVariant = Color(0xFF3A3B40),
    error = Color(0xFFFFB4A9),
    errorContainer = Color(0xFF4B2926),
    onErrorContainer = Color(0xFFFFDAD4),
)

private val AuraTypography = Typography(
    headlineLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium,
        fontSize = 32.sp, lineHeight = 44.sp),
    headlineMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium,
        fontSize = 26.sp, lineHeight = 36.sp),
    titleLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium,
        fontSize = 21.sp, lineHeight = 30.sp),
    titleMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium,
        fontSize = 16.sp, lineHeight = 24.sp),
    bodyLarge = TextStyle(fontFamily = FontFamily.Default, fontSize = 16.sp, lineHeight = 26.sp),
    bodyMedium = TextStyle(fontFamily = FontFamily.Default, fontSize = 14.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontFamily = FontFamily.Default, fontSize = 12.sp, lineHeight = 18.sp),
    labelLarge = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium,
        fontSize = 14.sp, lineHeight = 20.sp),
    labelMedium = TextStyle(fontFamily = FontFamily.Default, fontWeight = FontWeight.Medium,
        fontSize = 12.sp, lineHeight = 18.sp),
)

@Composable
fun AuraTheme(mode: ThemeMode = ThemeMode.SYSTEM, content: @Composable () -> Unit) {
    val dark = when (mode) {
        ThemeMode.SYSTEM -> isSystemInDarkTheme()
        ThemeMode.LIGHT -> false
        ThemeMode.DARK -> true
    }
    MaterialTheme(colorScheme = if (dark) DarkPalette else LightPalette,
        typography = AuraTypography, content = content)
}

/** Original orbital mark, drawn as a vector; no downloaded logos or fonts. */
@Composable
fun AuraMark(modifier: Modifier = Modifier) {
    val color = MaterialTheme.colorScheme.secondary
    Canvas(modifier) {
        val stroke = size.minDimension * .065f
        val diameter = size.minDimension * .62f
        val origin = Offset((size.width - diameter) / 2f, (size.height - diameter) / 2f)
        drawArc(color, 35f, 285f, false, origin, Size(diameter, diameter),
            style = Stroke(stroke, cap = StrokeCap.Round))
        drawCircle(color, size.minDimension * .065f,
            Offset(size.width * .79f, size.height * .63f))
        drawLine(color, Offset(size.width * .34f, size.height * .62f),
            Offset(size.width * .52f, size.height * .36f), stroke, StrokeCap.Round)
        drawLine(color, Offset(size.width * .52f, size.height * .36f),
            Offset(size.width * .64f, size.height * .57f), stroke, StrokeCap.Round)
    }
}
