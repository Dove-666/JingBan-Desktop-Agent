package com.aura.companion.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.aura.companion.R

/**
 * AURA liquid glass: blue water-light, soft lilac and translucent floating lenses.
 * Opaque ink and system Chinese type at 16sp/26sp keep long conversations clear.
 * Both appearances use semantic colors; no remote fonts or decorative animation.
 */
private val LightPalette = lightColorScheme(
    primary = Color(0xFF385D9D),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDDE9FF),
    onPrimaryContainer = Color(0xFF253E6A),
    secondary = Color(0xFF62568A),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFEAE4FF),
    onSecondaryContainer = Color(0xFF443960),
    tertiary = Color(0xFF276C70),
    background = Color(0xFFF2F6FC),
    onBackground = Color(0xFF202C40),
    surface = Color(0xFFF7FAFF),
    onSurface = Color(0xFF202C40),
    surfaceVariant = Color(0xFFE5ECF7),
    onSurfaceVariant = Color(0xFF526178),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFEDF3FC),
    surfaceContainer = Color(0xFFE7EFFB),
    outline = Color(0xFF7B8AA3),
    outlineVariant = Color(0xFFCEDBED),
    error = Color(0xFFA0443C),
    errorContainer = Color(0xFFF8E9E6),
    onErrorContainer = Color(0xFF792F29),
)

private val DarkPalette = darkColorScheme(
    primary = Color(0xFFB3CEFF),
    onPrimary = Color(0xFF17365F),
    primaryContainer = Color(0xFF293F60),
    onPrimaryContainer = Color(0xFFD8E6FF),
    secondary = Color(0xFFD0C3F4),
    onSecondary = Color(0xFF352B50),
    secondaryContainer = Color(0xFF403656),
    onSecondaryContainer = Color(0xFFEAE1FF),
    tertiary = Color(0xFF9CDDD9),
    background = Color(0xFF101923),
    onBackground = Color(0xFFEAF1FF),
    surface = Color(0xFF172331),
    onSurface = Color(0xFFEAF1FF),
    surfaceVariant = Color(0xFF273547),
    onSurfaceVariant = Color(0xFFB5C4D9),
    surfaceContainerLowest = Color(0xFF192637),
    surfaceContainerLow = Color(0xFF1D2C3F),
    surfaceContainer = Color(0xFF283A51),
    outline = Color(0xFF8B9CB4),
    outlineVariant = Color(0xFF3D506B),
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
    val colors = if (dark) DarkPalette else LightPalette
    MaterialTheme(colorScheme = colors, typography = AuraTypography) {
        // Transparent containers cannot infer contentColorFor; establish readable ink at the root.
        CompositionLocalProvider(LocalContentColor provides colors.onBackground, content = content)
    }
}

/** Original whale-maid artwork, kept sharp and centered with its transparent edges intact. */
@Composable
fun AuraMark(modifier: Modifier = Modifier) {
    // Adjacent AURA labels already identify this decorative avatar to accessibility services.
    Image(painterResource(R.drawable.whale_icon_art), contentDescription = null,
        modifier = modifier, contentScale = ContentScale.Fit)
}
