import BookdoeKit
import Foundation

/// Loads the data behind the CarPlay Library and Continue Listening tabs as
/// `CarPlayLoadState`s, and starts playback when a book row is selected.
/// Lists are capped at `listLimit` rows so they stay short on the car display.
/// Audiobooks only: ebooks cannot play in the car.
@MainActor
enum CarPlayLibraryLoader {

    /// Maximum rows per list.
    static let listLimit = 20

    static let signInMessage = "Sign in on iPhone to add a server"

    static func libraryState(model: AppModel) async -> CarPlayLoadState {
        guard let provider = model.provider else { return .error(message: signInMessage) }
        do {
            let page = try await provider.browse(query: nil, kind: .audiobook, sort: .title, offset: 0, limit: listLimit)
            return state(page.items)
        } catch {
            return CarPlayStateMapper.state(for: error)
        }
    }

    static func continueListeningState(model: AppModel) async -> CarPlayLoadState {
        guard let provider = model.provider else { return .error(message: signInMessage) }
        do {
            let rows = CarPlayContinueMapper.rows(try await provider.continueListening(), limit: listLimit)
            return rows.isEmpty ? .empty : .ready(items: rows)
        } catch {
            return CarPlayStateMapper.state(for: error)
        }
    }

    /// Start playback of one audiobook through the shared PlayerEngine.
    static func play(id: String, model: AppModel) async {
        guard let provider = model.provider, let serverID = model.activeServer?.id else { return }
        do {
            if let episodeID = CarPlayContinueMapper.episodeID(fromRowID: id), let bookdoe = provider as? BookdoeProvider {
                guard let ep = try await bookdoe.continueListening().first(where: { $0.isEpisode && $0.id == episodeID }) else { return }
                await model.player.load(try await bookdoe.continueEpisodeDetail(ep), provider: provider, serverID: serverID)
                return
            }
            let detail = try await provider.detail(id: id)
            await model.player.load(detail, provider: provider, serverID: serverID)
        } catch {
            model.report(error)
        }
    }

    /// Start playback of one podcast episode through the same player path as the iOS show page, which resumes
    /// at the stored position and reports progress to the podcast endpoint.
    static func playEpisode(_ episode: PodcastEpisode, of show: PodcastShow, provider: BookdoeProvider, model: AppModel) async {
        guard let serverID = model.activeServer?.id else { return }
        do {
            try await PodcastPlayback.play(episode, of: show, provider: provider,
                                           player: CarPlayEpisodePlayer(model: model, provider: provider, serverID: serverID))
        } catch {
            model.report(error)
        }
    }

    private static func state(_ items: [MediaItem]) -> CarPlayLoadState {
        items.isEmpty ? .empty : .ready(items: items.map(row))
    }

    private static func row(_ media: MediaItem) -> CarPlayBookRow {
        var parts: [String] = []
        if !media.authorLine.isEmpty { parts.append(media.authorLine) }
        if let progress = media.progress { parts.append("\(Int((progress * 100).rounded()))%") }
        return CarPlayBookRow(id: media.id, title: media.title, subtitle: parts.isEmpty ? nil : parts.joined(separator: " · "),
                            coverURL: media.coverURL)
    }
}


@MainActor
private final class CarPlayEpisodePlayer: EpisodePlayer {
    let model: AppModel
    let provider: MediaProvider
    let serverID: UUID
    init(model: AppModel, provider: MediaProvider, serverID: UUID) { self.model = model; self.provider = provider; self.serverID = serverID }
    func load(_ detail: MediaDetail) async { await model.player.load(detail, provider: provider, serverID: serverID) }
}
