package bookdoe.app.ui

import bookdoe.app.AppContainer
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DownloadsScreen(container: AppContainer, onOpen: (serverId: String, itemId: String) -> Unit) {
    val downloads = container.downloads
    val active by downloads.active.collectAsStateWithLifecycle()
    val completed by downloads.completed.collectAsStateWithLifecycle()
    val servers by container.model.servers.collectAsStateWithLifecycle()

    Scaffold(topBar = { TopAppBar(title = { Text("Downloads") }) }) { padding ->
        LazyColumn(Modifier.padding(padding).fillMaxSize()) {
            if (active.isNotEmpty()) {
                item { SectionHeader("In progress") }
                items(active.values.toList(), key = { "a:" + it.key }) { p ->
                    ListItem(
                        headlineContent = { Text(p.title ?: p.itemId) },
                        supportingContent = {
                            Column {
                                if (p.error != null) Text(p.error, color = MaterialTheme.colorScheme.error) else {
                                    Text("${p.completedFiles}/${p.totalFiles} files  ${formatBytes(p.bytes)}", style = MaterialTheme.typography.bodySmall)
                                    LinearProgressIndicator(progress = { p.fraction.toFloat() }, modifier = Modifier.fillMaxWidth().padding(top = 4.dp))
                                }
                            }
                        },
                        trailingContent = { IconButton(onClick = { downloads.cancel(p.serverId, p.itemId) }) { Icon(Icons.Default.Delete, "Cancel") } },
                    )
                }
            }
            item { SectionHeader("Available offline") }
            if (completed.isEmpty()) {
                item { Text("Nothing downloaded yet. Open a book and choose Download for offline.", Modifier.padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
            items(completed, key = { it.id }) { item ->
                val serverName = servers.firstOrNull { it.id == item.serverId }?.name
                ListItem(
                    headlineContent = { Text(item.title) },
                    supportingContent = { Text(listOfNotNull(item.authors.joinToString(", ").ifEmpty { null }, formatBytes(item.bytes), serverName).joinToString("  "), style = MaterialTheme.typography.bodySmall) },
                    trailingContent = { IconButton(onClick = { downloads.remove(item.serverId, item.itemId) }) { Icon(Icons.Default.Delete, "Remove") } },
                    modifier = Modifier.clickable { onOpen(item.serverId, item.itemId) },
                )
            }
            item { Text("Total: " + formatBytes(downloads.totalBytes), Modifier.padding(16.dp), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
}
