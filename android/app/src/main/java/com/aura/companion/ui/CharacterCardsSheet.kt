@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/** Only safe library summaries enter this view; role prompts remain in the encrypted repository. */
@Composable
internal fun CharacterCardsSheet(state: UiState, actions: UiActions, onDismiss: () -> Unit, onConnect: () -> Unit) {
    val online = state.connection == ConnectionState.CONNECTED
    val canSelect = online && state.characterLibrarySupported && !state.characterSyncBusy
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = glassSheetColor(),
        contentColor = MaterialTheme.colorScheme.onSurface, tonalElevation = 0.dp,
        shape = RoundedCornerShape(topStart = 32.dp, topEnd = 32.dp),
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(.78f).padding(horizontal = 20.dp)
            .padding(bottom = 24.dp).testTag("character-cards-sheet")) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("角色卡", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.weight(1f))
                IconButton(actions::refreshCharacters, enabled = online && !state.characterSyncBusy,
                    modifier = Modifier.size(48.dp).testTag("character-refresh")) {
                    if (state.characterSyncBusy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    else Icon(Icons.Default.Refresh, contentDescription = "同步角色卡", modifier = Modifier.size(22.dp))
                }
            }
            Text("当前角色 · ${state.activeCharacterName.ifBlank { "鲸鱼娘" }}",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp, bottom = 12.dp).testTag("character-current"))
            when {
                !online -> Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(if (state.paired) "离线时沿用已同步的角色" else "连接电脑后同步角色卡",
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.weight(1f))
                    TextButton(onConnect, Modifier.heightIn(min = 48.dp).testTag("character-connect")) { Text("连接电脑") }
                }
                !state.characterLibrarySupported && !state.characterSyncBusy ->
                    Text("更新并重启电脑端后，可同步角色库", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(bottom = 12.dp))
            }
            state.characterSyncError?.takeIf { it.isNotBlank() }?.let { message ->
                Text(message, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(bottom = 12.dp).testTag("character-sync-error"))
            }
            LazyColumn(Modifier.fillMaxWidth().weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                item(key = "default") {
                    CharacterCardChoice("default", "鲸鱼娘", "默认角色", state.activeCharacterId == "default",
                        canSelect, onClick = { actions.selectCharacter("default") })
                }
                items(state.characterCards, key = { it.id }) { card ->
                    CharacterCardChoice(card.id, card.label.ifBlank { card.name },
                        card.name.takeIf { it != card.label }.orEmpty(), state.activeCharacterId == card.id,
                        canSelect, onClick = { actions.selectCharacter(card.id) })
                }
                if (online && state.characterLibrarySupported && !state.characterSyncBusy && state.characterCards.isEmpty()) {
                    item(key = "empty") {
                        Text("在电脑保存角色卡后，会同步到这里", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(vertical = 20.dp))
                    }
                }
            }
        }
    }
}

@Composable
private fun CharacterCardChoice(id: String, name: String, subtitle: String, isSelected: Boolean,
                                enabled: Boolean, onClick: () -> Unit) {
    Surface(onClick = onClick, enabled = enabled, shape = RoundedCornerShape(14.dp),
        color = if (isSelected) MaterialTheme.colorScheme.primaryContainer.copy(alpha = .45f) else Color.Transparent,
        modifier = Modifier.fillMaxWidth().testTag("character-card-$id").semantics { selected = isSelected }) {
        Row(Modifier.fillMaxWidth().heightIn(min = 64.dp).padding(horizontal = 12.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(name, style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (subtitle.isNotBlank()) Text(subtitle, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (isSelected) Icon(Icons.Default.Check, contentDescription = "当前角色", modifier = Modifier.size(20.dp),
                tint = MaterialTheme.colorScheme.primary)
        }
    }
}
