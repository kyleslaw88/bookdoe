package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.core.MediaDetail
import bookdoe.core.MediaKind
import android.content.Intent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.OpenInNew
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch
import java.io.File

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ItemDetailScreen(container: AppContainer, itemId: String, offlineServerId: String?, onBack: () -> Unit, onPlay: () -> Unit) {
    val model = container.model
    val downloads = container.downloads
    val player = container.player
    val activeServer by model.activeServer.collectAsStateWithLifecycle()
    val servers by model.servers.collectAsStateWithLifecycle()
    val serverId = offlineServerId ?: activeServer?.id
    val server = servers.firstOrNull { it.id == serverId }
    var detail by remember { mutableStateOf<MediaDetail?>(null) }
    var loading by remember { mutableStateOf(true) }
    var offline by remember { mutableStateOf(false) }
    val active by downloads.active.collectAsStateWithLifecycle()
    val completed by downloads.completed.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    LaunchedEffect(itemId, serverId, completed.size) {
        loading = true
        val sid = serverId
        val p = if (offlineServerId == null) model.provider else null
        val online = if (p != null) runCatching { p.detail(itemId) }.onFailure { if (sid == null || !downloads.isDownloaded(sid, itemId)) model.report(it) }.getOrNull() else null
        if (online != null) {
            detail = online; offline = false
        } else if (sid != null) {
            downloads.offlineDetail(sid, itemId)?.let { detail = it; offline = true }
        }
        loading = false
    }

    val d = detail
    val isDownloaded = serverId != null && downloads.isDownloaded(serverId, itemId)
    val progress = serverId?.let { active[bookdoe.app.downloads.DownloadJob.key(it, itemId)] }

    fun play(fromChapter: bookdoe.core.Chapter? = null) {
        val sid = serverId ?: return
        val base = d ?: return
        val server = model.servers.value.firstOrNull { it.id == sid } ?: return
        val provider = model.providerFor(server)
        // Prefer the local files when the book is downloaded; keep the server position.
        val target = downloads.offlineDetail(sid, itemId)?.copy(position = base.position, description = base.description) ?: base
        val withStart = if (fromChapter != null) target.copy(position = target.locate(fromChapter.start).let { l -> bookdoe.core.PlaybackPosition(trackId = target.track(l.index)?.id, trackIndex = l.index, positionInTrack = l.offset, absolutePosition = fromChapter.start) }) else target
        scope.launch {
            player.load(withStart, provider, sid)
            onPlay()
        }
    }

    fun openEbook() {
        val sid = serverId ?: return
        val file: File = downloads.localEbook(sid, itemId) ?: return
        val uri = FileProvider.getUriForFile(context, context.packageName + ".files", file)
        val mime = when (d?.ebookFormat?.lowercase() ?: file.extension.lowercase()) {
            "epub" -> "application/epub+zip"
            "pdf" -> "application/pdf"
            "mobi" -> "application/x-mobipocket-ebook"
            "azw3" -> "application/vnd.amazon.mobi8-ebook"
            "cbz" -> "application/vnd.comicbook+zip"
            else -> "*/*"
        }
        val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        runCatching { context.startActivity(Intent.createChooser(view, "Open ebook with")) }.onFailure {
            val send = Intent(Intent.ACTION_SEND).setType(mime).putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            runCatching { context.startActivity(Intent.createChooser(send, "Share ebook")) }.onFailure { e -> model.report(e) }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(d?.item?.title ?: "", maxLines = 1) },
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } },
            )
        },
    ) { padding ->
        if (d == null) {
            Column(Modifier.padding(padding).fillMaxSize(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
                if (loading) CircularProgressIndicator() else Text("Not available.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            return@Scaffold
        }
        LazyColumn(Modifier.padding(padding).fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp)) {
            item {
                Row(verticalAlignment = Alignment.Top) {
                    CoverImage(container, d.item, 120.dp)
                    Column(Modifier.padding(start = 16.dp)) {
                        Text(d.item.title, style = MaterialTheme.typography.titleLarge)
                        if (d.item.authors.isNotEmpty()) Text(d.item.authorLine, style = MaterialTheme.typography.bodyMedium)
                        if (d.item.narrators.isNotEmpty()) Text("Read by " + d.item.narrators.joinToString(", "), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        d.item.series?.let { Text(if (d.item.seriesSequence != null) "$it #${d.item.seriesSequence}" else it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                        formatDuration(d.totalDuration)?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                        if (offline) Text("Offline copy", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.tertiary)
                    }
                }
                Spacer(Modifier.height(16.dp))
                if (d.item.kind == MediaKind.audiobook && d.tracks.isNotEmpty()) {
                    Button(onClick = { play() }, modifier = Modifier.fillMaxWidth()) {
                        Icon(Icons.Default.PlayArrow, null); Spacer(Modifier.padding(4.dp))
                        Text(if ((d.position?.absolutePosition ?: 0.0) > 0) "Resume" else "Play")
                    }
                    d.position?.let { pos ->
                        if (pos.absolutePosition > 0) Text("Position: " + formatTime(pos.absolutePosition) + (pos.percent?.let { "  (${(it * 100).toInt()}%)" } ?: ""), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 4.dp))
                    }
                }
                if (d.item.kind == MediaKind.ebook) {
                    if (serverId != null && downloads.localEbook(serverId, itemId) != null) {
                        Button(onClick = { openEbook() }, modifier = Modifier.fillMaxWidth()) {
                            Icon(Icons.Default.OpenInNew, null); Spacer(Modifier.padding(4.dp)); Text("Open ebook (${d.ebookFormat ?: "file"}) in another app")
                        }
                    } else if (d.ebookDownloadUrl != null) {
                        Text("Download the ebook, then open it in a reader app.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text("Reading happens in another reader; this app syncs audiobook progress only.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 4.dp))
                }
                Spacer(Modifier.height(12.dp))
                DownloadSection(d, server, isDownloaded, progress, downloads, model)
                d.description?.takeIf { it.isNotBlank() }?.let {
                    Spacer(Modifier.height(16.dp))
                    Text(it, style = MaterialTheme.typography.bodyMedium)
                }
                val chapters = d.effectiveChapters
                if (chapters.isNotEmpty()) {
                    Spacer(Modifier.height(16.dp))
                    Text("Chapters", style = MaterialTheme.typography.titleMedium)
                }
            }
            itemsIndexed(d.effectiveChapters) { i, ch ->
                Row(
                    Modifier.fillMaxWidth().clickable(enabled = d.tracks.isNotEmpty()) { play(ch) }.padding(vertical = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text("${i + 1}. ${ch.title.ifEmpty { "Chapter ${i + 1}" }}", maxLines = 1, modifier = Modifier.weight(1f))
                    Text(formatTime(ch.start), color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
private fun DownloadSection(
    d: MediaDetail, server: bookdoe.core.ServerRecord?, isDownloaded: Boolean, progress: bookdoe.app.downloads.DownloadManager.Progress?,
    downloads: bookdoe.app.downloads.DownloadManager, model: bookdoe.app.AppModel,
) {
    val sid = server?.id ?: return
    if (progress != null && progress.error == null) {
        Column {
            Text("Downloading ${progress.completedFiles}/${progress.totalFiles}", style = MaterialTheme.typography.bodySmall)
            LinearProgressIndicator(progress = { progress.fraction.toFloat() }, modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp))
            OutlinedButton(onClick = { downloads.cancel(sid, d.item.id) }) { Text("Cancel") }
        }
        return
    }
    progress?.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
    if (isDownloaded) {
        OutlinedButton(onClick = { downloads.remove(sid, d.item.id) }, modifier = Modifier.fillMaxWidth()) { Text("Remove download") }
    } else if (d.tracks.isNotEmpty() || d.ebookDownloadUrl != null) {
        OutlinedButton(
            onClick = {
                val headers = runCatching { model.providerFor(server).mediaHeaders() }.getOrNull() ?: emptyMap()
                downloads.download(d, sid, headers)
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Icon(Icons.Default.Download, null); Spacer(Modifier.padding(4.dp)); Text(if (d.item.kind == MediaKind.ebook) "Download ebook" else "Download for offline") }
    }
}
