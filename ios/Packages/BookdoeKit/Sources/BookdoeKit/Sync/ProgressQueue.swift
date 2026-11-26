import Foundation

/// Offline-safe progress reporting. Updates are queued with their original client
/// timestamp, coalesced per item (the newest wins locally) and flushed in order. A
/// server answer of `applied: false` carries the server's newer position, which the
/// caller adopts (bookdoe's conflict rule; Jellyfin and Audiobookshelf always
/// apply, so for them the queue is only a retry buffer).
public actor ProgressQueue {
    public private(set) var pending: [ProgressUpdate]
    private let fileURL: URL?

    public init(fileURL: URL? = nil) {
        self.fileURL = fileURL
        if let fileURL, let data = try? Data(contentsOf: fileURL), let items = try? JSONDecoder().decode([ProgressUpdate].self, from: data) {
            pending = items
        } else {
            pending = []
        }
    }

    public static func defaultFileURL() -> URL {
        LegacyFiles.applicationSupport(named: "bookdoe-progress-queue.json", legacy: "alexandria-progress-queue.json")
    }

    /// Queue an update. An older queued update for the same server and asset is
    /// replaced, with its `listened` seconds folded into the new one so
    /// Audiobookshelf's time-listened stays accurate.
    public func enqueue(_ update: ProgressUpdate) {
        var merged = update
        if let i = pending.firstIndex(where: { $0.serverID == update.serverID && $0.assetID == update.assetID && $0.sessionID == update.sessionID }) {
            merged.listened += pending[i].listened
            pending.remove(at: i)
        }
        pending.append(merged)
        persist()
    }

    public struct FlushResult: Equatable, Sendable {
        public var sent: Int
        public var failed: Int
        /// The server refused our credentials (401/403). Updates stay queued; the UI should
        /// ask the person to sign in again and flush afterwards.
        public var authRequired = false
        /// Updates the server rejected as stale, with the server position to adopt.
        public var conflicts: [(ProgressUpdate, PlaybackPosition)]

        public init(sent: Int, failed: Int, authRequired: Bool = false, conflicts: [(ProgressUpdate, PlaybackPosition)]) {
            self.sent = sent
            self.failed = failed
            self.authRequired = authRequired
            self.conflicts = conflicts
        }

        public static func == (a: FlushResult, b: FlushResult) -> Bool {
            a.sent == b.sent && a.failed == b.failed && a.authRequired == b.authRequired && a.conflicts.map { $0.0.id } == b.conflicts.map { $0.0.id }
        }
    }

    /// Send every pending update for `serverID` through `provider`. Network failures
    /// and authorization errors (401/403) keep items queued (a later sign-in makes them
    /// deliverable; `authRequired` is set); only not-found drops one (retrying would never succeed). Returns what happened so the UI can adopt server positions.
    public func flush(serverID: UUID, provider: MediaProvider, session: PlaybackSession? = nil) async -> FlushResult {
        var result = FlushResult(sent: 0, failed: 0, conflicts: [])
        let mine = pending.filter { $0.serverID == serverID }.sorted { $0.clientUpdatedAt < $1.clientUpdatedAt }
        for update in mine {
            do {
                let report = try await provider.reportProgress(update, session: session ?? update.sessionID.map { PlaybackSession(sessionID: $0) })
                pending.removeAll { $0.id == update.id }
                result.sent += 1
                if !report.applied, let pos = report.serverPosition { result.conflicts.append((update, pos)) }
            } catch APIError.unauthorized, APIError.forbidden {
                result.failed += 1
                result.authRequired = true
                break  // every remaining update would fail the same way
            } catch APIError.notFound {
                pending.removeAll { $0.id == update.id }
                result.failed += 1
            } catch {
                result.failed += 1
                break  // keep order; try again next flush
            }
        }
        persist()
        return result
    }

    /// Drop anything queued for one book, so a stale position cannot come back after
    /// the book was marked read or unread.
    public func remove(serverID: UUID, assetID: String) {
        pending.removeAll { $0.serverID == serverID && $0.assetID == assetID }
        persist()
    }

    public func clear(serverID: UUID) {
        pending.removeAll { $0.serverID == serverID }
        persist()
    }

    private func persist() {
        guard let fileURL, let data = try? JSONEncoder().encode(pending) else { return }
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        #if os(iOS)
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try? data.write(to: fileURL, options: [.atomic])
        #endif
    }
}
