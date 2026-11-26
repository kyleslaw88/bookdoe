import Foundation

/// Pure mapper from podcast data to CarPlay section models (shows -> seasons -> episodes), no CarPlay import.
/// Lists are capped at `maxItems` rows so they stay within CarPlay's item limit.
public enum CarPlayPodcastMapper {

    public static let maxItems = 100
    public static let emptyShowsText = "No podcasts yet"
    public static let emptySeasonText = "No episodes"

    /// One row per show; `id` is the show id.
    public static func showSections(_ shows: [PodcastShowRef]) -> [CarPlaySectionModel] {
        guard !shows.isEmpty else { return [placeholder(emptyShowsText)] }
        return [CarPlaySectionModel(items: shows.prefix(maxItems).map { CarPlayRowModel(id: $0.id, text: $0.title) })]
    }

    /// One row per season in the server's order (numbered ascending, Extras last); `id` is the season id.
    public static func seasonSections(_ show: PodcastShow) -> [CarPlaySectionModel] {
        guard !show.seasons.isEmpty else { return [placeholder(emptySeasonText)] }
        let rows = show.seasons.prefix(maxItems).map { season in
            let count = season.episodes.filter { !$0.missing }.count
            return CarPlayRowModel(id: season.id, text: season.title, detailText: count == 1 ? "1 episode" : "\(count) episodes",
                                   coverURL: show.coverURL)
        }
        return [CarPlaySectionModel(items: rows)]
    }

    /// One row per playable episode of the season; `id` is the episode id. Missing files are left out.
    public static func episodeSections(_ season: PodcastSeason) -> [CarPlaySectionModel] {
        let episodes = season.episodes.filter { !$0.missing }
        guard !episodes.isEmpty else { return [placeholder(emptySeasonText)] }
        let rows = episodes.prefix(maxItems).map { episode -> CarPlayRowModel in
            var parts: [String] = []
            if let date = episode.releaseDate, !date.isEmpty { parts.append(date) }
            if episode.played { parts.append("Played") }
            return CarPlayRowModel(id: episode.id, text: episode.title, detailText: parts.isEmpty ? nil : parts.joined(separator: " · "))
        }
        return [CarPlaySectionModel(items: rows)]
    }

    private static func placeholder(_ text: String) -> CarPlaySectionModel {
        CarPlaySectionModel(items: [CarPlayRowModel(text: text, isPlaceholder: true)])
    }
}
