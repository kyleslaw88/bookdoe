package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.app.BuildConfig
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.ListItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(container: AppContainer, onServers: () -> Unit) {
    val model = container.model
    val active by model.activeServer.collectAsStateWithLifecycle()
    val servers by model.servers.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var pending by remember { mutableIntStateOf(model.progressQueue.pending.size) }
    var syncMessage by remember { mutableStateOf<String?>(null) }

    Scaffold(topBar = { TopAppBar(title = { Text("Settings") }) }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize().verticalScroll(rememberScrollState())) {
            SectionHeader("Servers")
            ListItem(
                headlineContent = { Text(active?.name ?: "No server") },
                supportingContent = { Text("${servers.size} saved. Tap to manage.") },
                modifier = Modifier.clickable(onClick = onServers),
            )
            HorizontalDivider()
            SectionHeader("Sync")
            ListItem(
                headlineContent = { Text("Sync now") },
                supportingContent = { Text(syncMessage ?: "$pending progress update(s) waiting") },
                modifier = Modifier.clickable {
                    scope.launch {
                        val r = model.flushProgress(null)
                        pending = model.progressQueue.pending.size
                        syncMessage = if (r == null) "No active server" else "Sent ${r.sent}, failed ${r.failed}, ${pending} waiting"
                    }
                },
            )
            HorizontalDivider()
            SectionHeader("About")
            ListItem(headlineContent = { Text("bookdoe for Android") }, supportingContent = { Text("Version ${BuildConfig.VERSION_NAME}") })
            ListItem(
                headlineContent = { Text("Privacy") },
                supportingContent = { Text("The app talks only to the servers you add. Credentials are stored encrypted on this device. Plain HTTP is allowed so servers on your own network work; use HTTPS when you can.") },
            )
        }
    }
}
