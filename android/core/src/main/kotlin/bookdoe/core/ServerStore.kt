package bookdoe.core

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File

/**
 * The saved servers, persisted as JSON in app-private storage (no credentials in
 * it; those are in the [CredentialStore]).
 */
class ServerStore private constructor(private val file: File?, initial: Persisted) {
    @Serializable
    data class Persisted(val servers: List<ServerRecord> = emptyList(), val activeServerId: String? = null)

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val _state = MutableStateFlow(initial.copy(activeServerId = initial.activeServerId ?: initial.servers.firstOrNull()?.id))

    /** Observable snapshot for the UI. */
    val state: StateFlow<Persisted> = _state.asStateFlow()

    val servers: List<ServerRecord> get() = _state.value.servers
    val activeServerId: String? get() = _state.value.activeServerId
    val active: ServerRecord? get() = servers.firstOrNull { it.id == activeServerId }

    /** In-memory store (tests, previews). */
    constructor(servers: List<ServerRecord> = emptyList(), activeServerId: String? = null) : this(null, Persisted(servers, activeServerId))

    /** File-backed store. */
    constructor(file: File) : this(file, load(file))

    @Synchronized
    fun add(server: ServerRecord) {
        val s = _state.value
        val list = s.servers.filter { it.id != server.id } + server
        _state.value = s.copy(servers = list, activeServerId = s.activeServerId ?: server.id)
        persist()
    }

    @Synchronized
    fun update(server: ServerRecord) {
        val s = _state.value
        val list = if (s.servers.any { it.id == server.id }) s.servers.map { if (it.id == server.id) server else it } else s.servers + server
        _state.value = s.copy(servers = list)
        persist()
    }

    @Synchronized
    fun remove(id: String) {
        val s = _state.value
        val list = s.servers.filter { it.id != id }
        _state.value = Persisted(list, if (s.activeServerId == id) list.firstOrNull()?.id else s.activeServerId)
        persist()
    }

    @Synchronized
    fun setActive(id: String?) {
        _state.value = _state.value.copy(activeServerId = id)
        persist()
    }

    private fun persist() {
        val f = file ?: return
        runCatching {
            f.parentFile?.mkdirs()
            f.writeText(json.encodeToString(Persisted.serializer(), _state.value))
        }
    }

    companion object {
        private val loader = Json { ignoreUnknownKeys = true }

        private fun load(file: File): Persisted {
            if (!file.exists()) return Persisted()
            return runCatching { loader.decodeFromString(Persisted.serializer(), file.readText()) }.getOrDefault(Persisted())
        }
    }
}
