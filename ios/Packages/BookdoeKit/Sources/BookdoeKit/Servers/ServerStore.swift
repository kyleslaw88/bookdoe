import Foundation

/// The saved servers, persisted as JSON in the app's Application Support directory
/// (no credentials in it; those are in the CredentialStore).
public final class ServerStore: @unchecked Sendable {
    public private(set) var servers: [ServerRecord]
    public private(set) var activeServerID: UUID?
    private let fileURL: URL?
    private let lock = NSLock()

    private struct Persisted: Codable {
        var servers: [ServerRecord]
        var activeServerID: UUID?
    }

    /// In-memory store (tests, previews).
    public init(servers: [ServerRecord] = [], activeServerID: UUID? = nil) {
        self.servers = servers
        self.activeServerID = activeServerID ?? servers.first?.id
        self.fileURL = nil
    }

    /// File-backed store.
    public init(fileURL: URL) {
        self.fileURL = fileURL
        if let data = try? Data(contentsOf: fileURL), let p = try? JSONDecoder().decode(Persisted.self, from: data) {
            servers = p.servers
            activeServerID = p.activeServerID ?? p.servers.first?.id
        } else {
            servers = []
            activeServerID = nil
        }
    }

    public static func defaultFileURL() -> URL {
        LegacyFiles.applicationSupport(named: "bookdoe-servers.json", legacy: "alexandria-servers.json")
    }

    public var active: ServerRecord? { servers.first { $0.id == activeServerID } }

    public func add(_ server: ServerRecord) {
        lock.lock(); defer { lock.unlock() }
        servers.removeAll { $0.id == server.id }
        servers.append(server)
        if activeServerID == nil { activeServerID = server.id }
        persist()
    }

    public func update(_ server: ServerRecord) {
        lock.lock(); defer { lock.unlock() }
        if let i = servers.firstIndex(where: { $0.id == server.id }) { servers[i] = server } else { servers.append(server) }
        persist()
    }

    public func remove(id: UUID) {
        lock.lock(); defer { lock.unlock() }
        servers.removeAll { $0.id == id }
        if activeServerID == id { activeServerID = servers.first?.id }
        persist()
    }

    public func setActive(id: UUID?) {
        lock.lock(); defer { lock.unlock() }
        activeServerID = id
        persist()
    }

    private func persist() {
        guard let fileURL else { return }
        let p = Persisted(servers: servers, activeServerID: activeServerID)
        guard let data = try? JSONEncoder().encode(p) else { return }
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        #if os(iOS)
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try? data.write(to: fileURL, options: [.atomic])
        #endif
    }
}
