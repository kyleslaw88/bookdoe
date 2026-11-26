import Foundation

/// Pure rules that keep a broken player from corrupting the saved position.
public enum PositionGuard {
    /// Largest backwards jump (seconds) a normal tick may report.
    public static let maxBackwardJump: Double = 30

    /// True when a periodic (non-seek) position would move far backwards, which is what
    /// a reset or dead player looks like (it reports ~0). Such ticks must not be reported.
    public static func isSuspiciousRegression(previous: Double, new: Double) -> Bool {
        guard previous.isFinite, new.isFinite else { return true }
        return previous - new > maxBackwardJump
    }
}

/// The last position the player held on this device, kept so a relaunch can resume from it
/// when the server copy is older (for example when reports were queued but never delivered).
public struct LocalPosition: Codable, Equatable, Sendable {
    public var serverID: UUID
    public var assetID: String
    public var position: PlaybackPosition
    public var savedAt: Date

    public init(serverID: UUID, assetID: String, position: PlaybackPosition, savedAt: Date = Date()) {
        self.serverID = serverID
        self.assetID = assetID
        self.position = position
        self.savedAt = savedAt
    }

    /// Pick the position to start from: the local copy when it is newer than the server's.
    public static func resolve(server: PlaybackPosition?, local: LocalPosition?) -> PlaybackPosition? {
        guard let local else { return server }
        guard let server else { return stamped(local) }
        guard let serverTime = server.updatedAt else { return stamped(local) }
        return local.savedAt > serverTime ? stamped(local) : server
    }

    private static func stamped(_ l: LocalPosition) -> PlaybackPosition {
        var p = l.position
        p.updatedAt = l.savedAt
        return p
    }
}

/// File-backed store of `LocalPosition`s, one per server and asset.
public final class LocalPositionStore: @unchecked Sendable {
    private let fileURL: URL?
    private let lock = NSLock()
    private var items: [LocalPosition]

    public init(fileURL: URL? = nil) {
        self.fileURL = fileURL
        if let fileURL, let data = try? Data(contentsOf: fileURL), let decoded = try? JSONDecoder().decode([LocalPosition].self, from: data) {
            items = decoded
        } else {
            items = []
        }
    }

    public static func defaultFileURL() -> URL {
        LegacyFiles.applicationSupport(named: "bookdoe-local-positions.json", legacy: "alexandria-local-positions.json")
    }

    public func save(_ position: LocalPosition) {
        lock.lock(); defer { lock.unlock() }
        items.removeAll { $0.serverID == position.serverID && $0.assetID == position.assetID }
        items.append(position)
        if items.count > 200 { items.removeFirst(items.count - 200) }
        guard let fileURL, let data = try? JSONEncoder().encode(items) else { return }
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: fileURL, options: [.atomic])
    }

    public func position(serverID: UUID, assetID: String) -> LocalPosition? {
        lock.lock(); defer { lock.unlock() }
        return items.first { $0.serverID == serverID && $0.assetID == assetID }
    }
}
