package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.core.BookdoeProvider
import bookdoe.core.EpisodePlayer
import bookdoe.core.MediaKind
import bookdoe.core.PodcastPlayback
import bookdoe.core.PodcastShowRef
import bookdoe.core.PodcastShowViewModel
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PodcastsScreen(container: AppContainer, onOpen: (String, String) -> Unit) {
    val model = container.model
    val server by model.activeServer.collectAsStateWithLifecycle()
    var shows by remember { mutableStateOf<List<PodcastShowRef>>(emptyList()) }
    LaunchedEffect(server?.id) {
        val p = model.provider as? BookdoeProvider ?: return@LaunchedEffect
        runCatching { p.podcastShows() }.onSuccess { shows = it }.onFailure { model.report(it) }
    }
    Scaffold(topBar = { TopAppBar(title = { Text("Podcasts") }) }) { padding ->
        LazyColumn(Modifier.padding(padding)) {
            items(shows, key = { it.id }) { s ->
                Text(s.title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.fillMaxWidth().clickable { onOpen(s.id, s.title) }.padding(16.dp))
            }
            if (shows.isEmpty()) item { Text("No podcasts yet.", Modifier.padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PodcastShowScreen(container: AppContainer, showId: String, title: String, onPlay: () -> Unit) {
    val model = container.model
    val scope = rememberCoroutineScope()
    var vm by remember { mutableStateOf<PodcastShowViewModel?>(null) }
    val downloads = container.downloads
    LaunchedEffect(showId) {
        val p = model.provider as? BookdoeProvider ?: return@LaunchedEffect
        runCatching { p.podcastShow(showId) }.onSuccess { vm = PodcastShowViewModel(it) }.onFailure { model.report(it) }
    }

    fun play(episodeId: String) {
        val current = vm ?: return
        val p = model.provider as? BookdoeProvider ?: return
        val sid = model.activeServer.value?.id ?: return
        val episode = current.episode(episodeId) ?: return
        val offline = downloads.offlineDetail(sid, episodeId)
        if (offline != null) {
            scope.launch {
                runCatching { container.player.load(offline.copy(position = bookdoe.core.PlaybackPosition(trackId = episodeId, trackIndex = 0, positionInTrack = episode.positionSeconds, absolutePosition = episode.positionSeconds)), p, sid) }
                    .onSuccess { onPlay() }.onFailure { model.report(it) }
            }
            return
        }
        scope.launch {
            runCatching { PodcastPlayback.play(episode, current.show, p, EpisodePlayer { container.player.load(it, p, sid) }) }
                .onSuccess { onPlay() }.onFailure { model.report(it) }
        }
    }

    val active by downloads.active.collectAsStateWithLifecycle()
    val completed by downloads.completed.collectAsStateWithLifecycle()

    fun download(episodeId: String) {
        val current = vm ?: return
        val p = model.provider as? BookdoeProvider ?: return
        val server = model.activeServer.value ?: return
        val episode = current.episode(episodeId) ?: return
        scope.launch {
            runCatching { downloads.download(p.podcastEpisodeDetail(episode, current.show), server.id, p.mediaHeaders()) }
                .onFailure { model.report(it) }
        }
    }

    Scaffold(topBar = { TopAppBar(title = { Text(title) }) }) { padding ->
        val current = vm
        LazyColumn(Modifier.padding(padding), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            if (current != null) {
                item {
                    Column(Modifier.padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                        CoverImage(container, current.coverUrl, MediaKind.audiobook, 160.dp)
                    }
                }
                item {
                    Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        current.seasonTitles.forEach { (id, label) ->
                            FilterChip(selected = id == current.selectedSeasonId, onClick = { vm = current.select(id) }, label = { Text(label) })
                        }
                    }
                }
                items(current.rows, key = { it.id }) { row ->
                    Column(Modifier.fillMaxWidth().clickable(enabled = row.playable) { play(row.id) }.padding(16.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(row.title, style = MaterialTheme.typography.titleMedium,
                                color = if (row.played) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface)
                            if (row.played) Icon(Icons.Default.CheckCircle, "Played", tint = MaterialTheme.colorScheme.primary)
                        }
                        if (row.dateText.isNotEmpty()) Text(row.dateText, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        if (row.description.isNotEmpty()) Text(row.description, maxLines = 3, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        val sid = model.activeServer.value?.id
                        if (sid != null && row.playable) {
                            val downloaded = completed.any { it.serverId == sid && it.itemId == row.id }
                            val progress = active[bookdoe.app.downloads.DownloadJob.key(sid, row.id)]
                            if (downloaded) TextButton(onClick = { downloads.remove(sid, row.id) }) { Text("Delete download") }
                            else if (progress != null && progress.error == null) TextButton(onClick = { downloads.cancel(sid, row.id) }) { Text("Cancel download") }
                            else TextButton(onClick = { download(row.id) }) { Text(if (progress != null) "Retry download" else "Download") }
                        }
                    }
                }
            }
        }
    }
}
