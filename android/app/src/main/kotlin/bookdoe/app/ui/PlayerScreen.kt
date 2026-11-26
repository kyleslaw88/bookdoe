package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.app.playback.PlayerController
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bedtime
import androidx.compose.material.icons.filled.Forward30
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Replay
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch
import java.util.Locale

@Composable
fun MiniPlayerBar(player: PlayerController, onOpen: () -> Unit) {
    val d by player.current.collectAsStateWithLifecycle()
    val playing by player.isPlaying.collectAsStateWithLifecycle()
    val detail = d ?: return
    Surface(tonalElevation = 3.dp) {
        Row(Modifier.fillMaxWidth().clickable(onClick = onOpen).padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(detail.item.title, style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(player.currentChapter?.title ?: detail.item.authorLine, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            IconButton(onClick = { player.skip(-15.0) }) { Icon(Icons.Default.Replay, "Back 15 seconds") }
            IconButton(onClick = { player.togglePlayPause() }) { Icon(if (playing) Icons.Default.Pause else Icons.Default.PlayArrow, if (playing) "Pause" else "Play") }
            IconButton(onClick = { player.skip(30.0) }) { Icon(Icons.Default.Forward30, "Forward 30 seconds") }
        }
    }
}

@Composable
fun PlayerScreen(container: AppContainer, onClose: () -> Unit) {
    val player = container.player
    val d by player.current.collectAsStateWithLifecycle()
    val playing by player.isPlaying.collectAsStateWithLifecycle()
    val trackIndex by player.trackIndex.collectAsStateWithLifecycle()
    val positionInTrack by player.positionInTrack.collectAsStateWithLifecycle()
    val rate by player.rate.collectAsStateWithLifecycle()
    val sleepEnds by player.sleepTimerEnds.collectAsStateWithLifecycle()
    val buffering by player.buffering.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var scrubbing by remember { mutableStateOf(false) }
    var scrubValue by remember { mutableFloatStateOf(0f) }
    var speedMenu by remember { mutableStateOf(false) }
    var sleepMenu by remember { mutableStateOf(false) }

    val detail = d
    if (detail == null) {
        Column(Modifier.fillMaxWidth().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Text("Nothing is playing.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            TextButton(onClick = onClose) { Text("Close") }
        }
        return
    }
    val absolute = (detail.track(trackIndex)?.startOffset ?: 0.0) + positionInTrack
    val duration = detail.totalDuration
    val shown = if (scrubbing) scrubValue.toDouble() else absolute

    LazyColumn(Modifier.fillMaxWidth(), contentPadding = androidx.compose.foundation.layout.PaddingValues(start = 16.dp, end = 16.dp, bottom = 32.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                TextButton(onClick = onClose) { Text("Close") }
                TextButton(onClick = { scope.launch { player.stop(report = true); onClose() } }) { Text("Stop") }
            }
            Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                CoverImage(container, detail.item, 200.dp)
                Spacer(Modifier.height(12.dp))
                Text(detail.item.title, style = MaterialTheme.typography.titleLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Text(player.currentChapter?.title ?: detail.item.authorLine, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
                if (buffering) Text("Buffering...", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(8.dp))
            Slider(
                value = shown.toFloat().coerceIn(0f, maxOf(1f, duration.toFloat())),
                onValueChange = { scrubbing = true; scrubValue = it },
                onValueChangeFinished = { scrubbing = false; player.seekToAbsolute(scrubValue.toDouble()) },
                valueRange = 0f..maxOf(1f, duration.toFloat()),
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(formatTime(shown), style = MaterialTheme.typography.labelMedium)
                Text("-" + formatTime(maxOf(0.0, duration - shown)), style = MaterialTheme.typography.labelMedium)
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = { player.skip(-15.0) }, modifier = Modifier.size(56.dp)) { Icon(Icons.Default.Replay, "Back 15 seconds", Modifier.size(32.dp)) }
                Spacer(Modifier.padding(8.dp))
                FilledIconButton(onClick = { player.togglePlayPause() }, modifier = Modifier.size(72.dp)) {
                    Icon(if (playing) Icons.Default.Pause else Icons.Default.PlayArrow, if (playing) "Pause" else "Play", Modifier.size(40.dp))
                }
                Spacer(Modifier.padding(8.dp))
                IconButton(onClick = { player.skip(30.0) }, modifier = Modifier.size(56.dp)) { Icon(Icons.Default.Forward30, "Forward 30 seconds", Modifier.size(32.dp)) }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                Column {
                    TextButton(onClick = { speedMenu = true }) { Icon(Icons.Default.Speed, null); Spacer(Modifier.padding(2.dp)); Text(String.format(Locale.ROOT, "%.2fx", rate)) }
                    DropdownMenu(expanded = speedMenu, onDismissRequest = { speedMenu = false }) {
                        listOf(0.75f, 1.0f, 1.25f, 1.5f, 1.75f, 2.0f).forEach { r ->
                            DropdownMenuItem(text = { Text(String.format(Locale.ROOT, "%.2fx", r)) }, onClick = { player.setRate(r); speedMenu = false })
                        }
                    }
                }
                Column {
                    TextButton(onClick = { sleepMenu = true }) {
                        Icon(Icons.Default.Bedtime, null); Spacer(Modifier.padding(2.dp))
                        Text(sleepEnds?.let { "Sleep " + formatTime(((it - System.currentTimeMillis()) / 1000.0).coerceAtLeast(0.0)) } ?: "Sleep timer")
                    }
                    DropdownMenu(expanded = sleepMenu, onDismissRequest = { sleepMenu = false }) {
                        DropdownMenuItem(text = { Text("Off") }, onClick = { player.setSleepTimer(null); sleepMenu = false })
                        listOf(5, 15, 30, 45, 60).forEach { m -> DropdownMenuItem(text = { Text("$m minutes") }, onClick = { player.setSleepTimer(m); sleepMenu = false }) }
                    }
                }
            }
            val chapters = detail.effectiveChapters
            if (chapters.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text("Chapters", style = MaterialTheme.typography.titleMedium)
            }
        }
        itemsIndexed(detail.effectiveChapters) { i, ch ->
            val isCurrent = player.currentChapter == ch
            Row(Modifier.fillMaxWidth().clickable { player.seekToChapter(ch) }.padding(vertical = 8.dp), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("${i + 1}. ${ch.title.ifEmpty { "Chapter ${i + 1}" }}", maxLines = 1, modifier = Modifier.weight(1f), color = if (isCurrent) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface)
                Text(formatTime(ch.start), color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}
