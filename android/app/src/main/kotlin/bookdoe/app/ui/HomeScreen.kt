package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.core.ContinueRow
import bookdoe.core.LibrarySort
import bookdoe.core.MediaItem
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(container: AppContainer, onOpen: (String) -> Unit, onPlayEpisode: (MediaItem) -> Unit) {
    val model = container.model
    val server by model.activeServer.collectAsStateWithLifecycle()
    var continueItems by remember { mutableStateOf<List<MediaItem>>(emptyList()) }
    var recent by remember { mutableStateOf<List<MediaItem>>(emptyList()) }
    var loading by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    suspend fun load() {
        val p = model.provider ?: return
        loading = true
        try {
            coroutineScope {
                val c = async { runCatching { p.continueListening() } }
                val r = async { runCatching { p.browse(null, null, LibrarySort.added, 0, 20).items } }
                c.await().onSuccess { continueItems = it }.onFailure { model.report(it) }
                r.await().onSuccess { recent = it }.onFailure { model.report(it) }
            }
        } finally { loading = false }
    }

    LaunchedEffect(server?.id, server?.libraryId) { load() }

    Scaffold(topBar = { TopAppBar(title = { Text(server?.name ?: "Home") }) }) { padding ->
        PullToRefreshBox(isRefreshing = loading, onRefresh = { scope.launch { load() } }, modifier = Modifier.padding(padding).fillMaxSize()) {
            LazyColumn(Modifier.fillMaxSize()) {
                val row = ContinueRow(continueItems)
                if (row.visible) {
                    item { SectionHeader("Continue listening") }
                    items(row.items, key = { "c:" + it.id }) { ItemRow(container, it) { row.tap(it, onPlayEpisode, onOpen) } }
                }
                item { SectionHeader("Recently added") }
                if (recent.isEmpty() && !loading) {
                    item { Text("Nothing yet. Pull to refresh.", Modifier.padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant) }
                }
                items(recent, key = { "r:" + it.id }) { ItemRow(container, it) { onOpen(it.id) } }
            }
        }
    }
}

@Composable
fun SectionHeader(text: String) {
    Text(text, Modifier.padding(start = 16.dp, top = 16.dp, bottom = 4.dp), style = MaterialTheme.typography.titleMedium)
}
