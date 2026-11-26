import Foundation

/// Pure mapping of Continue Listening items to CarPlay rows. Episode rows carry a `podcast:` row id so a tap
/// can tell them from books without any state kept from the load.
public enum CarPlayContinueMapper {

    public static let episodeRowPrefix = "podcast:"

    /// Audiobooks and episodes only (ebooks cannot play in the car), server order kept, capped at `limit`.
    public static func rows(_ items: [MediaItem], limit: Int) -> [CarPlayBookRow] {
        items.filter { $0.kind == .audiobook }.prefix(limit).map { media in
            var parts: [String] = []
            if !media.authorLine.isEmpty { parts.append(media.authorLine) }
            if let progress = media.progress { parts.append("\(Int((progress * 100).rounded()))%") }
            return CarPlayBookRow(id: media.isEpisode ? episodeRowPrefix + media.id : media.id, title: media.title,
                                  subtitle: parts.isEmpty ? nil : parts.joined(separator: " · "), coverURL: media.coverURL)
        }
    }

    /// The episode id behind a row id made by `rows`, or nil for a book row.
    public static func episodeID(fromRowID id: String) -> String? {
        id.hasPrefix(episodeRowPrefix) ? String(id.dropFirst(episodeRowPrefix.count)) : nil
    }
}
