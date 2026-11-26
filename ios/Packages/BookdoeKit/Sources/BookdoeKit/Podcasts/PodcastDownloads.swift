import Foundation

/// Where downloaded episode files live. The app backs it with the downloads root; tests with a dictionary.
public protocol EpisodeStorage: AnyObject {
    func exists(_ relativePath: String) -> Bool
    /// Removes the file; true when something was removed.
    @discardableResult func delete(_ relativePath: String) -> Bool
    /// The URL the player reads the file from.
    func url(for relativePath: String) -> URL
}

/// Fetches one URL into `relativePath` of the storage and returns the bytes written. Throws on any network or HTTP failure.
public protocol EpisodeFetcher: AnyObject {
    func fetch(_ url: URL, headers: [String: String], to relativePath: String) async throws -> Int64
}

public enum EpisodeDownloadState: Equatable, Sendable {
    case notDownloaded
    case downloading
    case downloaded(bytes: Int64)
    case failed(String)
}

/// Offline download of podcast episodes (issue #521). One state per episode: not downloaded, downloading, downloaded or
/// failed (a failed download can be started again). Completed downloads are recorded in the same `DownloadIndex` the
/// audiobook downloads use, so `offlineDetail` is the shape the player already loads.
@MainActor
public final class PodcastDownloads {
    private let serverID: UUID
    private let storage: EpisodeStorage
    private let fetcher: EpisodeFetcher
    private let index: DownloadIndex
    private var failures: [String: String] = [:]
    private var running: Set<String> = []

    public init(serverID: UUID, storage: EpisodeStorage, fetcher: EpisodeFetcher, index: DownloadIndex) {
        self.serverID = serverID
        self.storage = storage
        self.fetcher = fetcher
        self.index = index
    }

    public func state(_ episodeID: String) -> EpisodeDownloadState {
        if running.contains(episodeID) { return .downloading }
        if let item = index.item(serverID: serverID, itemID: episodeID) { return .downloaded(bytes: item.bytes) }
        if let message = failures[episodeID] { return .failed(message) }
        return .notDownloaded
    }

    /// Total bytes held by downloaded episodes.
    public var usedBytes: Int64 { index.items.filter { $0.serverID == serverID }.reduce(0) { $0 + $1.bytes } }

    /// Download the episode described by `detail` (the one-track detail from the show page). Does nothing when the episode
    /// is already downloaded or downloading. On failure the partial file is removed and the state is `.failed`.
    @discardableResult
    public func download(_ detail: MediaDetail, headers: [String: String]) async -> EpisodeDownloadState {
        let id = detail.item.id
        if running.contains(id) || index.item(serverID: serverID, itemID: id) != nil { return state(id) }
        running.insert(id)
        failures[id] = nil
        defer { running.remove(id) }
        let plan = DownloadPlan(serverID: serverID, detail: detail, headers: headers)
        guard let file = plan.files.first(where: { $0.trackID != "ebook" }), let track = detail.tracks.first(where: { $0.id == file.trackID }) else {
            failures[id] = "The episode has no audio file."
            return .failed("The episode has no audio file.")
        }
        do {
            let bytes = try await fetcher.fetch(file.remote, headers: file.headers, to: file.localRelativePath)
            guard storage.exists(file.localRelativePath) else { throw EpisodeDownloadError.noFile }
            index.upsert(DownloadedItem(serverID: serverID, detail: detail,
                                        tracks: [DownloadedTrack(trackID: track.id, title: track.title, relativePath: file.localRelativePath, durationSeconds: track.durationSeconds)],
                                        bytes: bytes))
        } catch {
            storage.delete(file.localRelativePath)
            let message = error.localizedDescription
            failures[id] = message
            return .failed(message)
        }
        return .downloaded(bytes: index.item(serverID: serverID, itemID: id)?.bytes ?? 0)
    }

    /// Delete the downloaded file and its index entry. Returns the bytes freed (0 when nothing was downloaded).
    @discardableResult
    public func delete(_ episodeID: String) -> Int64 {
        failures[episodeID] = nil
        guard let item = index.remove(serverID: serverID, itemID: episodeID) else { return 0 }
        for t in item.tracks { storage.delete(t.relativePath) }
        return item.bytes
    }

    /// The detail of a downloaded episode with its track pointing at the local file, or nil when it is not downloaded (or the
    /// file has gone). `position` is where to resume, in seconds.
    public func offlineDetail(_ episodeID: String, position: Double = 0) -> MediaDetail? {
        guard let item = index.item(serverID: serverID, itemID: episodeID),
              item.tracks.allSatisfy({ storage.exists($0.relativePath) }) else { return nil }
        var detail = item.detail(root: URL(fileURLWithPath: "/"))
        detail.tracks = zip(detail.tracks, item.tracks).map { track, saved in
            var t = track
            t.url = storage.url(for: saved.relativePath)
            return t
        }
        detail.position = PlaybackPosition(trackID: detail.tracks.first?.id, trackIndex: 0, positionInTrack: position, absolutePosition: position)
        return detail
    }

    /// Tapping a downloaded episode: hand the local-file detail to the player. False when the episode is not available offline.
    public func playOffline(_ episodeID: String, position: Double, player: EpisodePlayer) async -> Bool {
        guard let detail = offlineDetail(episodeID, position: position) else { return false }
        await player.load(detail)
        return true
    }
}

enum EpisodeDownloadError: LocalizedError {
    case noFile
    var errorDescription: String? { "The download did not produce a file." }
}

/// Resume positions listened to offline: queued with their own timestamp, written to the podcast progress route (#516) on flush.
public enum PodcastOfflineProgress {
    public static func record(queue: ProgressQueue, serverID: UUID, episodeID: String, position: Double, duration: Double?, at: Date = Date()) async {
        await queue.enqueue(ProgressUpdate(serverID: serverID, itemID: episodeID, assetID: BookdoeProvider.podcastAssetPrefix + episodeID, trackID: episodeID,
                                           positionInTrack: position, absolutePosition: position, duration: duration, clientUpdatedAt: at))
    }
}
