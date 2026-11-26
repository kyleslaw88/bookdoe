package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.core.LibrarySort
import bookdoe.core.MediaItem
import bookdoe.core.MediaKind
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Sort
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.delay

/** Paged library state: resets when the query, filter, sort or server changes. */
class LibraryPager(private val pageSize: Int = 50) {
    var items by mutableStateOf<List<MediaItem>>(emptyList())
        private set
    var loading by mutableStateOf(false)
        private set
    var total by mutableIntStateOf(0)
        private set
    private var nextOffset: Int? = 0
    val hasMore: Boolean get() = nextOffset != null

    fun reset() {
        items = emptyList(); total = 0; nextOffset = 0
    }

    suspend fun loadMore(fetch: suspend (offset: Int, limit: Int) -> bookdoe.core.LibraryPage) {
        val offset = nextOffset ?: return
        if (loading) return
        loading = true
        try {
            val page = fetch(offset, pageSize)
            items = if (offset == 0) page.items else items + page.items.filter { n -> items.none { it.id == n.id } }
            total = page.total
            nextOffset = page.nextOffset
        } finally { loading = false }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen(container: AppContainer, onOpen: (String) -> Unit) {
    val model = container.model
    val server by model.activeServer.collectAsStateWithLifecycle()
    var query by remember { mutableStateOf("") }
    var kind by remember { mutableStateOf<MediaKind?>(null) }
    var sort by remember { mutableStateOf(LibrarySort.title) }
    var sortMenu by remember { mutableStateOf(false) }
    val pager = remember { LibraryPager() }
    val listState = rememberLazyListState()

    LaunchedEffect(server?.id, server?.libraryId, query, kind, sort) {
        if (query.isNotEmpty()) delay(300)
        pager.reset()
        val p = model.provider ?: return@LaunchedEffect
        runCatching { pager.loadMore { o, l -> p.browse(query.ifBlank { null }, kind, sort, o, l) } }.onFailure { model.report(it) }
    }

    val nearEnd by remember { derivedStateOf { val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0; last >= pager.items.size - 10 } }
    LaunchedEffect(nearEnd, pager.items.size) {
        if (nearEnd && pager.hasMore && pager.items.isNotEmpty()) {
            val p = model.provider ?: return@LaunchedEffect
            runCatching { pager.loadMore { o, l -> p.browse(query.ifBlank { null }, kind, sort, o, l) } }.onFailure { model.report(it) }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Library") },
                actions = {
                    IconButton(onClick = { sortMenu = true }) { Icon(Icons.Default.Sort, "Sort") }
                    DropdownMenu(expanded = sortMenu, onDismissRequest = { sortMenu = false }) {
                        DropdownMenuItem(text = { Text("Title") }, onClick = { sort = LibrarySort.title; sortMenu = false })
                        DropdownMenuItem(text = { Text("Recently added") }, onClick = { sort = LibrarySort.added; sortMenu = false })
                        DropdownMenuItem(text = { Text("Recently updated") }, onClick = { sort = LibrarySort.updated; sortMenu = false })
                    }
                },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            OutlinedTextField(
                value = query, onValueChange = { query = it }, singleLine = true, placeholder = { Text("Search") },
                leadingIcon = { Icon(Icons.Default.Search, null) },
                trailingIcon = { if (query.isNotEmpty()) IconButton(onClick = { query = "" }) { Icon(Icons.Default.Clear, "Clear") } },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
            )
            Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(selected = kind == null, onClick = { kind = null }, label = { Text("All") })
                FilterChip(selected = kind == MediaKind.audiobook, onClick = { kind = MediaKind.audiobook }, label = { Text("Audiobooks") })
                FilterChip(selected = kind == MediaKind.ebook, onClick = { kind = MediaKind.ebook }, label = { Text("Ebooks") })
            }
            if (pager.total > 0) Text("${pager.total} items", Modifier.padding(horizontal = 16.dp), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
                itemsIndexed(pager.items, key = { _, it -> it.id }) { _, item -> ItemRow(container, item) { onOpen(item.id) } }
                if (pager.loading) item { Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() } }
                if (!pager.loading && pager.items.isEmpty()) item { Text("Nothing here.", Modifier.padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
        }
    }
}
