package bookdoe.app.ui

import bookdoe.app.AppContainer
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.LibraryBooks
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import kotlinx.coroutines.launch

object Routes {
    const val HOME = "home"
    const val LIBRARY = "library"
    const val PODCASTS = "podcasts"
    const val PODCAST_SHOW = "podcasts/{id}/{title}"
    const val DOWNLOADS = "downloads"
    const val SETTINGS = "settings"
    const val SERVERS = "servers"
    const val ADD_SERVER = "servers/add"
    const val ITEM = "item/{id}"
    const val OFFLINE_ITEM = "offline/{serverId}/{id}"
    fun item(id: String) = "item/${encode(id)}"
    fun offlineItem(serverId: String, id: String) = "offline/${encode(serverId)}/${encode(id)}"
    fun podcastShow(id: String, title: String) = "podcasts/${encode(id)}/${encode(title)}"
    fun encode(s: String): String = java.net.URLEncoder.encode(s, "UTF-8")
    fun decode(s: String): String = java.net.URLDecoder.decode(s, "UTF-8")
}

private data class Tab(val route: String, val label: String, val icon: ImageVector)

private val tabs = listOf(
    Tab(Routes.HOME, "Home", Icons.Default.Home),
    Tab(Routes.LIBRARY, "Library", Icons.Default.LibraryBooks),
    Tab(Routes.PODCASTS, "Podcasts", Icons.Default.Mic),
    Tab(Routes.DOWNLOADS, "Downloads", Icons.Default.Download),
    Tab(Routes.SETTINGS, "Settings", Icons.Default.Settings),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppRoot(container: AppContainer) {
    val model = container.model
    val activeServer by model.activeServer.collectAsStateWithLifecycle()
    val error by model.lastError.collectAsStateWithLifecycle()
    val nav = rememberNavController()
    var showPlayer by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val current by container.player.current.collectAsStateWithLifecycle()

    val backStack by nav.currentBackStackEntryAsState()
    val destination = backStack?.destination
    val onTab = tabs.any { t -> destination?.hierarchy?.any { it.route == t.route } == true }
    val noServer = activeServer == null

    Scaffold(
        bottomBar = {
            if (!noServer) {
                Column {
                    if (current != null) MiniPlayerBar(container.player) { showPlayer = true }
                    if (onTab) {
                        NavigationBar {
                            tabs.forEach { t ->
                                NavigationBarItem(
                                    selected = destination?.hierarchy?.any { it.route == t.route } == true,
                                    onClick = {
                                        nav.navigate(t.route) {
                                            popUpTo(nav.graph.findStartDestination().id) { saveState = true }
                                            launchSingleTop = true
                                            restoreState = true
                                        }
                                    },
                                    icon = { Icon(t.icon, t.label) },
                                    label = { Text(t.label) },
                                )
                            }
                        }
                    }
                }
            }
        },
    ) { padding ->
        NavHost(nav, startDestination = if (noServer) Routes.SERVERS else Routes.HOME, modifier = Modifier.padding(padding)) {
            composable(Routes.HOME) { HomeScreen(container, onOpen = { nav.navigate(Routes.item(it)) },
                onPlayEpisode = { ep ->
                    val server = activeServer
                    val provider = server?.let { model.providerFor(it) } as? bookdoe.core.BookdoeProvider
                    if (server == null || provider == null) {
                        model.report(IllegalStateException("Podcast episodes need an bookdoe server"))
                    } else {
                        scope.launch {
                            runCatching { provider.episodeDetail(ep.id) }
                                .onSuccess { container.player.load(it, provider, server.id); showPlayer = true }
                                .onFailure { model.report(it) }
                        }
                    }
                }) }
            composable(Routes.LIBRARY) { LibraryScreen(container, onOpen = { nav.navigate(Routes.item(it)) }) }
            composable(Routes.PODCASTS) { PodcastsScreen(container, onOpen = { id, title -> nav.navigate(Routes.podcastShow(id, title)) }) }
            composable(Routes.PODCAST_SHOW) { entry ->
                val id = Routes.decode(entry.arguments?.getString("id") ?: "")
                val title = Routes.decode(entry.arguments?.getString("title") ?: "")
                PodcastShowScreen(container, showId = id, title = title, onPlay = { showPlayer = true })
            }
            composable(Routes.DOWNLOADS) { DownloadsScreen(container, onOpen = { s, i -> nav.navigate(Routes.offlineItem(s, i)) }) }
            composable(Routes.SETTINGS) { SettingsScreen(container, onServers = { nav.navigate(Routes.SERVERS) }) }
            composable(Routes.SERVERS) {
                ServersScreen(model, onAdd = { nav.navigate(Routes.ADD_SERVER) }, onBack = if (noServer) null else ({ nav.popBackStack() }))
            }
            composable(Routes.ADD_SERVER) {
                AddServerScreen(
                    model,
                    onDone = { nav.navigate(Routes.HOME) { popUpTo(0) } },
                    onCancel = { nav.popBackStack() },
                )
            }
            composable(Routes.ITEM) { entry ->
                val id = Routes.decode(entry.arguments?.getString("id") ?: "")
                ItemDetailScreen(container, itemId = id, offlineServerId = null, onBack = { nav.popBackStack() }, onPlay = { showPlayer = true })
            }
            composable(Routes.OFFLINE_ITEM) { entry ->
                val id = Routes.decode(entry.arguments?.getString("id") ?: "")
                val serverId = Routes.decode(entry.arguments?.getString("serverId") ?: "")
                ItemDetailScreen(container, itemId = id, offlineServerId = serverId, onBack = { nav.popBackStack() }, onPlay = { showPlayer = true })
            }
        }
    }

    if (showPlayer) {
        val sheet = rememberModalBottomSheetState(skipPartiallyExpanded = true)
        ModalBottomSheet(onDismissRequest = { showPlayer = false }, sheetState = sheet) {
            PlayerScreen(container, onClose = { showPlayer = false })
        }
    }

    error?.let { msg ->
        AlertDialog(
            onDismissRequest = { model.lastError.value = null },
            title = { Text("Something went wrong") },
            text = { Text(msg) },
            confirmButton = { TextButton(onClick = { model.lastError.value = null }) { Text("OK") } },
        )
    }
}
