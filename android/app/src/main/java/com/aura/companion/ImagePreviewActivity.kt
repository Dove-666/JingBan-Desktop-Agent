package com.aura.companion

import android.graphics.BitmapFactory
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.unit.dp
import com.aura.companion.ui.AuraTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

class ImagePreviewActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val requested = intent.getStringExtra("image_path")?.let(::File)?.canonicalFile
        val allowed = File(filesDir, "companion-images").canonicalFile
        if (requested == null || !requested.toPath().startsWith(allowed.toPath()) || !requested.isFile) { finish(); return }
        setContent {
            AuraTheme {
                val bitmap by produceState<android.graphics.Bitmap?>(null, requested.path) {
                    value = withContext(Dispatchers.IO) {
                        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                        BitmapFactory.decodeFile(requested.path, bounds)
                        var sample = 1
                        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > 2048) sample *= 2
                        BitmapFactory.decodeFile(requested.path, BitmapFactory.Options().apply { inSampleSize = sample })
                    }
                }
                var zoom by remember { mutableFloatStateOf(1f) }
                val transform = rememberTransformableState { factor, _, _ -> zoom = (zoom * factor).coerceIn(1f, 4f) }
                Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).safeDrawingPadding()) {
                    Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { finish() }, modifier = Modifier.heightIn(min = 48.dp)) { Text("返回") }
                        Text("图片预览", style = MaterialTheme.typography.titleMedium)
                    }
                    Box(Modifier.fillMaxWidth().weight(1f).transformable(transform), contentAlignment = Alignment.Center) {
                        bitmap?.let { Image(it.asImageBitmap(), "接收的图片", Modifier.fillMaxSize().graphicsLayer {
                            scaleX = zoom; scaleY = zoom
                        }) } ?: CircularProgressIndicator()
                    }
                }
            }
        }
    }
}
