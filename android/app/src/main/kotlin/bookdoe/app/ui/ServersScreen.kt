package bookdoe.app.ui

import bookdoe.app.AppModel
import bookdoe.core.ApiError
import bookdoe.core.LibraryRef
import bookdoe.core.PairingPoll
import bookdoe.core.ServerInfo
import bookdoe.core.ServerKind
import bookdoe.core.ServerRecord
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
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ServersScreen(model: AppModel, onAdd: () -> Unit, onBack: (() -> Unit)?) {
    val servers by model.servers.collectAsStateWithLifecycle()
    val active by model.activeServer.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var toRemove by remember { mutableStateOf<ServerRecord?>(null) }
    var toEdit by remember { mutableStateOf<ServerRecord?>(null) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Servers") },
                navigationIcon = { if (onBack != null) IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } },
                actions = { IconButton(onClick = onAdd) { Icon(Icons.Default.Add, "Add server") } },
            )
        },
    ) { padding ->
        if (servers.isEmpty()) {
            Column(Modifier.padding(padding).fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
                Text("Add an bookdoe, Jellyfin or Audiobookshelf server to begin.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(16.dp))
                Button(onClick = onAdd) { Text("Add server") }
            }
        } else {
            LazyColumn(Modifier.padding(padding)) {
                items(servers, key = { it.id }) { s ->
                    ListItem(
                        headlineContent = { Text(s.name) },
                        supportingContent = { Text("${s.kind.displayName}  ${s.hostLabel}  ${s.username}", style = MaterialTheme.typography.bodySmall) },
                        leadingContent = { if (s.id == active?.id) Icon(Icons.Default.Check, "Active") else Spacer(Modifier.height(24.dp)) },
                        trailingContent = {
                            Row {
                                TextButton(onClick = { toEdit = s }) { Text("Edit") }
                                IconButton(onClick = { toRemove = s }) { Icon(Icons.Default.Delete, "Remove") }
                            }
                        },
                        modifier = Modifier.clickable { model.setActive(s); onBack?.invoke() },
                    )
                    HorizontalDivider()
                }
            }
        }
    }

    toRemove?.let { s ->
        AlertDialog(
            onDismissRequest = { toRemove = null },
            title = { Text("Remove ${s.name}?") },
            text = { Text("Signs out, forgets the credential and deletes this server's downloads.") },
            confirmButton = { TextButton(onClick = { toRemove = null; scope.launch { model.removeServer(s) } }) { Text("Remove") } },
            dismissButton = { TextButton(onClick = { toRemove = null }) { Text("Cancel") } },
        )
    }

    toEdit?.let { s -> EditServerDialog(model, s) { toEdit = null } }
}

@Composable
private fun EditServerDialog(model: AppModel, server: ServerRecord, onDismiss: () -> Unit) {
    var name by remember { mutableStateOf(server.name) }
    var libraries by remember { mutableStateOf<List<LibraryRef>>(emptyList()) }
    var libraryId by remember { mutableStateOf(server.libraryId) }
    LaunchedEffect(server.id) {
        if (server.kind != ServerKind.bookdoe) libraries = runCatching { model.providerFor(server).libraries() }.getOrDefault(emptyList())
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Edit server") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(value = name, onValueChange = { name = it }, label = { Text("Name") }, singleLine = true)
                if (libraries.isNotEmpty()) {
                    Text("Library", style = MaterialTheme.typography.labelLarge)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        libraries.forEach { l -> FilterChip(selected = l.id == libraryId, onClick = { libraryId = l.id }, label = { Text(l.name) }) }
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = { model.updateServer(server.copy(name = name.ifBlank { server.name }, libraryId = libraryId)); onDismiss() }) { Text("Save") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AddServerScreen(model: AppModel, onDone: () -> Unit, onCancel: () -> Unit) {
    val scope = rememberCoroutineScope()
    var kind by remember { mutableStateOf(ServerKind.bookdoe) }
    var name by remember { mutableStateOf("") }
    var urlText by remember { mutableStateOf("") }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var info by remember { mutableStateOf<ServerInfo?>(null) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var pairingCode by remember { mutableStateOf<String?>(null) }
    var pairingJob by remember { mutableStateOf<Job?>(null) }
    val url = ServerRecord.normalizeBaseUrl(urlText)

    fun fail(e: Throwable) {
        error = (e as? ApiError)?.message ?: e.message ?: "Something went wrong"
    }

    suspend fun probe() {
        val u = url ?: return
        busy = true; error = null
        try {
            info = model.factory.make(ServerRecord(name = "probe", kind = kind, baseUrl = u, username = "")).probe()
        } catch (e: Exception) { fail(e) } finally { busy = false }
    }

    suspend fun signIn() {
        val u = url ?: return
        busy = true; error = null
        try {
            model.addServer(kind, name, u, username.trim(), password)
            onDone()
        } catch (e: Exception) { fail(e) } finally { busy = false }
    }

    fun startPairing() {
        val u = url ?: return
        pairingJob = scope.launch {
            busy = true; error = null
            try {
                val (record, provider, pairing) = model.startPairing(name, u)
                pairingCode = pairing.userCode
                var interval = pairing.interval.coerceAtLeast(2).toLong()
                val deadline = System.currentTimeMillis() + pairing.expiresIn * 1000L
                while (isActive && System.currentTimeMillis() < deadline) {
                    delay(interval * 1000)
                    when (val poll = provider.pollPairing(pairing)) {
                        PairingPoll.Pending -> {}
                        PairingPoll.SlowDown -> interval += 5
                        is PairingPoll.Approved -> { model.finishPairing(record, poll.result); onDone(); return@launch }
                        PairingPoll.Denied -> { error = "Pairing was denied on the server."; break }
                        PairingPoll.Expired -> { error = "The pairing code expired. Try again."; break }
                    }
                }
                if (isActive && error == null) error = "The pairing code expired. Try again."
            } catch (e: Exception) { fail(e) } finally { busy = false; pairingCode = null }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Add server") },
                navigationIcon = { IconButton(onClick = { pairingJob?.cancel(); onCancel() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Cancel") } },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).padding(horizontal = 16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Spacer(Modifier.height(4.dp))
            Text("Server", style = MaterialTheme.typography.titleMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ServerKind.entries.forEach { k -> FilterChip(selected = kind == k, onClick = { kind = k; info = null }, label = { Text(k.displayName) }) }
            }
            OutlinedTextField(value = name, onValueChange = { name = it }, label = { Text("Name (optional)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(
                value = urlText, onValueChange = { urlText = it; info = null }, label = { Text("Address") },
                placeholder = { Text("http://lan-host:8795 or https://books.example.com") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth(),
            )
            OutlinedButton(onClick = { scope.launch { probe() } }, enabled = url != null && !busy) { Text("Check connection") }
            info?.let { i ->
                Text("${i.name} ${i.version ?: ""}".trim(), color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (i.setupRequired) Text("This server has not finished its first-run setup; open it in a browser first.", color = MaterialTheme.colorScheme.error)
            }

            if (pairingCode == null) {
                Text("Sign in", style = MaterialTheme.typography.titleMedium)
                OutlinedTextField(value = username, onValueChange = { username = it }, label = { Text("Username") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(
                    value = password, onValueChange = { password = it }, label = { Text("Password") }, singleLine = true,
                    visualTransformation = PasswordVisualTransformation(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), modifier = Modifier.fillMaxWidth(),
                )
                Button(onClick = { scope.launch { signIn() } }, enabled = url != null && username.isNotBlank() && !busy, modifier = Modifier.fillMaxWidth()) {
                    Text(if (busy) "Signing in..." else "Sign in")
                }
                if (kind == ServerKind.bookdoe) {
                    OutlinedButton(onClick = { startPairing() }, enabled = url != null && !busy, modifier = Modifier.fillMaxWidth()) { Text("Pair with a code instead") }
                }
            } else {
                Text("Pairing", style = MaterialTheme.typography.titleMedium)
                Text("On any signed-in browser open the Player, Settings, Pair, and enter:")
                Text(pairingCode ?: "", style = MaterialTheme.typography.displaySmall)
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    CircularProgressIndicator(Modifier.height(20.dp))
                    Text("Waiting for approval...")
                }
                TextButton(onClick = { pairingJob?.cancel(); pairingCode = null; busy = false }) { Text("Cancel pairing") }
            }
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            Spacer(Modifier.height(24.dp))
        }
    }
}
