package com.aura.companion.ui

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.*
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.graphics.layer.GraphicsLayer
import androidx.compose.ui.graphics.layer.drawLayer
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.positionInRoot
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/** A single, static GPU backdrop shared by the floating lenses. No screenshots or bitmaps. */
private class GlassBackdrop(val layer: GraphicsLayer, val hardwareBlur: Boolean)
private val LocalGlassBackdrop = staticCompositionLocalOf<GlassBackdrop?> { null }

@Composable
internal fun LiquidScene(content: @Composable BoxScope.() -> Unit) {
    val layer = rememberGraphicsLayer()
    val context = LocalContext.current
    val canBlur = remember(context) {
        val lowRam = (context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager)?.isLowRamDevice == true
        Build.VERSION.SDK_INT >= 31 && !lowRam
    }
    val backdrop = remember(layer, canBlur) { GlassBackdrop(layer, canBlur) }
    CompositionLocalProvider(LocalGlassBackdrop provides backdrop) {
        Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
            LiquidBackdrop(Modifier.fillMaxSize().drawWithContent {
                layer.record { this@drawWithContent.drawContent() }
                drawLayer(layer)
            })
            content()
        }
    }
}

/** The atmosphere is deliberately still; only real progress indicators animate. */
@Composable
private fun LiquidBackdrop(modifier: Modifier) {
    val colors = MaterialTheme.colorScheme
    val dark = colors.background.luminance() < .5f
    val blue = if (dark) Color(0xFF244A75) else Color(0xFFB8DAFF)
    val lilac = if (dark) Color(0xFF3A315D) else Color(0xFFDBD1FF)
    val aqua = if (dark) Color(0xFF154C50) else Color(0xFFBAF0EB)
    Canvas(modifier) {
        drawRect(colors.background)
        drawRect(Brush.radialGradient(listOf(blue.copy(alpha = if (dark) .43f else .38f), Color.Transparent),
            center = Offset(size.width * .07f, size.height * .07f), radius = size.width * .96f))
        drawRect(Brush.radialGradient(listOf(lilac.copy(alpha = if (dark) .40f else .33f), Color.Transparent),
            center = Offset(size.width * 1.05f, size.height * .42f), radius = size.width * 1.02f))
        drawRect(Brush.radialGradient(listOf(aqua.copy(alpha = if (dark) .36f else .28f), Color.Transparent),
            center = Offset(size.width * .04f, size.height * .94f), radius = size.width * .94f))
        // Broad, very faint water-light contours make transparency visible without competing with prose.
        drawCircle(Color.White.copy(alpha = if (dark) .018f else .14f), size.width * .72f,
            Offset(size.width * .88f, -size.width * .20f), style = Stroke(1.5.dp.toPx()))
        drawCircle(Color.White.copy(alpha = if (dark) .012f else .08f), size.width * .84f,
            Offset(size.width * .88f, -size.width * .20f), style = Stroke(1.dp.toPx()))
    }
}

/**
 * A glass lens: sample the shared scene at this exact position, blur only that
 * sample on API 31+, then draw tint, inner reflections and a crisp foreground.
 * Text and controls are siblings of the blurred layer, never its children.
 * Older Android and low-RAM devices use the same translucent lens without GPU blur.
 */
@Composable
internal fun LiquidSurface(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(24.dp),
    tint: Color = MaterialTheme.colorScheme.surface,
    prominent: Boolean = false,
    elevation: Dp = 0.dp,
    content: @Composable BoxScope.() -> Unit,
) {
    val backdrop = LocalGlassBackdrop.current
    val dark = MaterialTheme.colorScheme.background.luminance() < .5f
    val density = LocalDensity.current
    val blur = remember(density) { with(density) { 18.dp.toPx() } }
    var position by remember { mutableStateOf(Offset.Zero) }
    val tintAlpha = if (prominent) { if (dark) .80f else .74f } else { if (dark) .75f else .67f }
    val rim = Brush.linearGradient(listOf(
        Color.White.copy(alpha = if (dark) .24f else .72f),
        Color.White.copy(alpha = if (dark) .04f else .20f),
        Color(0xFFA5C9FF).copy(alpha = if (dark) .18f else .34f),
    ))
    Surface(modifier = modifier.onGloballyPositioned { position = it.positionInRoot() },
        shape = shape, color = Color.Transparent, contentColor = MaterialTheme.colorScheme.onSurface,
        shadowElevation = elevation,
        border = BorderStroke(1.dp, rim)) {
        Box(Modifier.clip(shape)) {
            if (backdrop != null) {
                Box(Modifier.matchParentSize().graphicsLayer {
                    // RenderEffect is confined to this backdrop sample, below all foreground content.
                    renderEffect = if (backdrop.hardwareBlur) BlurEffect(blur, blur, TileMode.Clamp) else null
                }.drawWithContent {
                    // A small local magnification bends the atmosphere at the lens boundary.
                    scale(1.025f) {
                        translate(-position.x, -position.y) { drawLayer(backdrop.layer) }
                    }
                })
            }
            Box(Modifier.matchParentSize().background(tint.copy(alpha = tintAlpha))
                .drawWithCache {
                    val reflection = Brush.linearGradient(listOf(
                        Color.White.copy(alpha = if (dark) .045f else .26f),
                        Color.White.copy(alpha = 0f),
                        Color(0xFFB5CFFF).copy(alpha = if (dark) .035f else .09f),
                    ), end = Offset(size.width * .72f, size.height))
                    onDrawBehind {
                        drawRect(reflection)
                        // Curved top reflection, clipped by the lens rather than painted over ink.
                        drawOval(Color.White.copy(alpha = if (dark) .018f else .08f),
                            topLeft = Offset(-size.width * .20f, -size.height * 1.03f),
                            size = Size(size.width * 1.30f, size.height * 1.22f))
                    }
                })
            content()
        }
    }
}

/** High-opacity sheet material keeps forms readable while preserving the same lens rim. */
@Composable
internal fun glassSheetColor(): Color = MaterialTheme.colorScheme.surface.copy(alpha = .97f)

