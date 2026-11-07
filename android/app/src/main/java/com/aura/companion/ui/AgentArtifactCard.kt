package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@Composable
internal fun AgentArtifactCard(artifact: UiAgentArtifact, downloading: String?, onSave: (String) -> Unit) {
    LiquidSurface(Modifier.fillMaxWidth().testTag("agent-artifact"), shape = RoundedCornerShape(16.dp), elevation = 1.dp) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Icon(Icons.AutoMirrored.Filled.List, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
            Column(Modifier.weight(1f)) {
                Text(artifact.name, maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyMedium)
                Text(if (artifact.bytes < 1024) "${artifact.bytes} B" else "${(artifact.bytes + 1023) / 1024} KB",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (downloading == artifact.reference) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
            else TextButton(onClick = { onSave(artifact.reference) }, enabled = downloading == null) { Text("保存") }
        }
    }
}
