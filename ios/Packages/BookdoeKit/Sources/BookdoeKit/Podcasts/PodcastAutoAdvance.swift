import Foundation

/// What the player needs from the server to move on to the next episode (the bookdoe provider; a fake in tests).
public protocol PodcastNextSource: Sendable {
    /// The unplayed episode after `episodeID` in its show, across seasons; nil at the end of the show.
    func nextPodcastEpisode(after episodeID: String) async throws -> PodcastEpisode?
    /// The playable detail for an episode, resuming at the position the server stores.
    func podcastEpisodeDetail(_ episode: PodcastEpisode, of show: PodcastShow) async throws -> MediaDetail
}

/// Setting "Continue to next episode" (Settings > Playback), default on.
public enum PodcastAutoAdvance {
    public static let settingKey = "player.continueToNextEpisode"

    public static func isEnabled(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: settingKey) as? Bool ?? true
    }

    /// The episode id when `assetID` is a podcast episode asset.
    public static func episodeID(fromAssetID id: String) -> String? { BookdoeProvider.podcastEpisodeID(fromAssetID: id) }

    /// The detail to play after `finished` ends, or nil when playback should stop (not a podcast, setting off, end of show).
    /// A downloaded next episode plays from its local file (`offline`), at the server's stored position.
    public static func nextDetail(after finished: MediaDetail, source: PodcastNextSource, enabled: Bool = isEnabled(),
                                  offline: (String) -> MediaDetail? = { _ in nil }) async throws -> MediaDetail? {
        guard enabled, let id = episodeID(fromAssetID: finished.assetID),
              let next = try await source.nextPodcastEpisode(after: id) else { return nil }
        let show = PodcastShow(id: finished.item.showID ?? "", title: finished.item.authorLine, coverURL: finished.item.coverURL)
        if var local = offline(next.id) {
            local.position = PlaybackPosition(trackID: next.id, trackIndex: 0, positionInTrack: next.positionSeconds, absolutePosition: next.positionSeconds)
            return local
        }
        return try await source.podcastEpisodeDetail(next, of: show)
    }
}

/// The engine's end-of-episode decision, behind closures so tests can drive it without AVPlayer.
@MainActor
public struct PodcastEpisodeAdvancer {
    public var source: PodcastNextSource?
    public var isEnabled: () -> Bool = { PodcastAutoAdvance.isEnabled() }
    public var offline: (String) -> MediaDetail? = { _ in nil }
    public var currentItemID: () -> String?
    /// Stops the player; `report` says whether to send the position first.
    public var stop: (_ report: Bool) async -> Void
    public var load: (MediaDetail) async -> Void

    public init(source: PodcastNextSource?, isEnabled: @escaping () -> Bool = { PodcastAutoAdvance.isEnabled() },
                offline: @escaping (String) -> MediaDetail? = { _ in nil }, currentItemID: @escaping () -> String?,
                stop: @escaping (Bool) async -> Void, load: @escaping (MediaDetail) async -> Void) {
        self.source = source
        self.isEnabled = isEnabled
        self.offline = offline
        self.currentItemID = currentItemID
        self.stop = stop
        self.load = load
    }

    /// A podcast episode played to its end (already reported as finished): continue when the setting is on.
    public func episodeEnded(_ finished: MediaDetail) async { await advance(after: finished, enabled: isEnabled(), alreadyReported: true) }

    /// Now-playing "next" (lock screen, CarPlay): the next episode whatever the setting says. Mid-episode, so load() reports the position.
    public func skip(_ current: MediaDetail) async {
        guard PodcastAutoAdvance.episodeID(fromAssetID: current.assetID) != nil else { return }
        await advance(after: current, enabled: true, alreadyReported: false)
    }

    private func advance(after finished: MediaDetail, enabled: Bool, alreadyReported: Bool) async {
        guard let source else { return }
        let next = try? await PodcastAutoAdvance.nextDetail(after: finished, source: source, enabled: enabled, offline: offline)
        guard let next, !Task.isCancelled, currentItemID() == finished.item.id else { return }
        if alreadyReported { await stop(false) }
        await load(next)
    }
}

extension BookdoeProvider: PodcastNextSource {}
