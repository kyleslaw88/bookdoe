import Foundation

/// Podcast shows as the bookdoe Player API serves them (`GET /api/player/v1/podcasts/shows/{id}`, issue #514).
public struct PodcastEpisode: Identifiable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var releaseDate: String?
    public var durationSeconds: Double?
    public var description: String
    public var played: Bool
    public var positionSeconds: Double
    public var missing: Bool

    public init(id: String, title: String, releaseDate: String? = nil, durationSeconds: Double? = nil, description: String = "",
                played: Bool = false, positionSeconds: Double = 0, missing: Bool = false) {
        self.id = id
        self.title = title
        self.releaseDate = releaseDate
        self.durationSeconds = durationSeconds
        self.description = description
        self.played = played
        self.positionSeconds = positionSeconds
        self.missing = missing
    }
}

public struct PodcastSeason: Identifiable, Equatable, Sendable {
    public var id: String
    /// Nil for Extras.
    public var number: Int?
    public var isExtras: Bool
    public var episodes: [PodcastEpisode]

    public init(id: String, number: Int?, isExtras: Bool, episodes: [PodcastEpisode] = []) {
        self.id = id
        self.number = number
        self.isExtras = isExtras
        self.episodes = episodes
    }

    public var title: String { isExtras ? "Extras" : "Season \(number ?? 0)" }
}

public struct PodcastShow: Identifiable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var coverURL: URL?
    /// Numbered seasons ascending, Extras last (the server's order).
    public var seasons: [PodcastSeason]

    public init(id: String, title: String, coverURL: URL? = nil, seasons: [PodcastSeason] = []) {
        self.id = id
        self.title = title
        self.coverURL = coverURL
        self.seasons = seasons
    }

    static func parse(_ obj: [String: Any], baseURL: URL) -> PodcastShow? {
        guard let id = JSON.string(obj, "id") else { return nil }
        let seasons: [PodcastSeason] = JSON.array(obj, "seasons").compactMap { s in
            guard let sid = JSON.string(s, "id") else { return nil }
            let episodes: [PodcastEpisode] = JSON.array(s, "episodes").compactMap { e in
                guard let eid = JSON.string(e, "id") else { return nil }
                return PodcastEpisode(id: eid, title: JSON.string(e, "title") ?? "", releaseDate: JSON.string(e, "release_date"),
                                      durationSeconds: JSON.double(e, "duration_seconds"), description: JSON.string(e, "description") ?? "",
                                      played: JSON.bool(e, "played") ?? false, positionSeconds: JSON.double(e, "position_seconds") ?? 0,
                                      missing: JSON.bool(e, "missing") ?? false)
            }
            return PodcastSeason(id: sid, number: JSON.int(s, "number"), isExtras: JSON.bool(s, "is_extras") ?? false, episodes: episodes)
        }
        // The #514 cover_url is on the cookie-only /api/v1 route; a device token reaches the Player API copy.
        return PodcastShow(id: id, title: JSON.string(obj, "title") ?? "", coverURL: baseURL.appending(path: "/api/player/v1/podcasts/shows/\(id)/cover"),
                           seasons: seasons)
    }
}

/// One row of the show list.
public struct PodcastShowRef: Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public init(id: String, title: String) { self.id = id; self.title = title }
}

/// What the show page draws for one episode.
public struct PodcastEpisodeRow: Identifiable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var dateText: String
    public var description: String
    public var played: Bool
    public var playable: Bool
}

/// State behind the show page: the season selector (numbered seasons in order, Extras last) and the rows of the
/// selected season.
public struct PodcastShowViewModel: Equatable, Sendable {
    public let show: PodcastShow
    public private(set) var selectedSeasonID: String?

    public init(show: PodcastShow) {
        self.show = show
        self.selectedSeasonID = show.seasons.first?.id
    }

    public var title: String { show.title }
    public var coverURL: URL? { show.coverURL }
    public var seasonTitles: [(id: String, title: String)] { show.seasons.map { ($0.id, $0.title) } }
    public var selectedSeason: PodcastSeason? { show.seasons.first { $0.id == selectedSeasonID } }

    public mutating func select(seasonID: String) {
        if show.seasons.contains(where: { $0.id == seasonID }) { selectedSeasonID = seasonID }
    }

    public var rows: [PodcastEpisodeRow] {
        (selectedSeason?.episodes ?? []).map {
            PodcastEpisodeRow(id: $0.id, title: $0.title, dateText: $0.releaseDate ?? "", description: HTMLText.plain($0.description),
                              played: $0.played, playable: !$0.missing)
        }
    }

    public func episode(id: String) -> PodcastEpisode? {
        show.seasons.lazy.flatMap { $0.episodes }.first { $0.id == id }
    }
}

/// The app's player, as the podcast code sees it (`PlayerEngine` in the App target).
@MainActor
public protocol EpisodePlayer: AnyObject {
    func load(_ detail: MediaDetail) async
}

/// Tapping an episode: build the playable detail (audio URL, stored position, show name for the lock screen) and
/// hand it to the existing player, which then reports progress through the ProgressQueue.
public enum PodcastPlayback {
    @MainActor
    public static func play(_ episode: PodcastEpisode, of show: PodcastShow, provider: BookdoeProvider, player: EpisodePlayer) async throws {
        let detail = try await provider.podcastEpisodeDetail(episode, of: show)
        await player.load(detail)
    }
}
