import SwiftUI
import BookdoeKit

/// Hands the episode to the app's player under the active server.
@MainActor
private final class EnginePlayer: EpisodePlayer {
    let model: AppModel
    let provider: MediaProvider
    let serverID: UUID
    init(model: AppModel, provider: MediaProvider, serverID: UUID) { self.model = model; self.provider = provider; self.serverID = serverID }
    func load(_ detail: MediaDetail) async { await model.player.load(detail, provider: provider, serverID: serverID) }
}

struct PodcastsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var shows: [PodcastShowRef] = []
    @State private var loading = false

    var body: some View {
        List {
            ForEach(shows) { show in NavigationLink(value: show) { Text(show.title) } }
            if !loading && shows.isEmpty { Text("No podcasts yet.").foregroundStyle(.secondary) }
        }
        .navigationTitle("Podcasts")
        .navigationDestination(for: PodcastShowRef.self) { PodcastShowView(ref: $0) }
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        guard let p = model.provider as? BookdoeProvider else { return }
        loading = true; defer { loading = false }
        do { shows = try await p.podcastShows() } catch { model.report(error) }
    }
}

struct PodcastShowView: View {
    @EnvironmentObject private var model: AppModel
    let ref: PodcastShowRef
    @State private var vm: PodcastShowViewModel?

    var body: some View {
        List {
            if let vm {
                Section {
                    HStack { Spacer(); CoverImage(url: vm.coverURL, size: 160); Spacer() }
                    Picker("Season", selection: Binding(get: { vm.selectedSeasonID ?? "" }, set: { self.vm?.select(seasonID: $0) })) {
                        ForEach(vm.seasonTitles, id: \.id) { Text($0.title).tag($0.id) }
                    }
                    .pickerStyle(.menu)
                }
                Section {
                    ForEach(vm.rows) { row in
                        Button { Task { await play(row.id) } } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(row.title).font(.headline).foregroundStyle(row.played ? .secondary : .primary)
                                    if row.played { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).accessibilityLabel("Played") }
                                }
                                if !row.dateText.isEmpty { Text(row.dateText).font(.caption).foregroundStyle(.secondary) }
                                if !row.description.isEmpty { Text(row.description).font(.subheadline).foregroundStyle(.secondary).lineLimit(3) }
                            }
                        }
                        .disabled(!row.playable)
                        if row.playable { EpisodeDownloadButton(downloads: model.downloads, serverID: model.activeServer?.id, episodeID: row.id) { await download(row.id) } }
                    }
                }
            }
        }
        .navigationTitle(ref.title)
        .task { await load() }
    }

    private func load() async {
        guard let p = model.provider as? BookdoeProvider else { return }
        do { vm = PodcastShowViewModel(show: try await p.podcastShow(id: ref.id)) } catch { model.report(error) }
    }

    private func download(_ episodeID: String) async {
        guard let p = model.provider as? BookdoeProvider, let sid = model.activeServer?.id, let vm, let ep = vm.episode(id: episodeID) else { return }
        do {
            let detail = try await p.podcastEpisodeDetail(ep, of: vm.show)
            model.downloads.download(detail, serverID: sid, headers: (try? p.mediaHeaders()) ?? [:])
        } catch { model.report(error) }
    }

    private func play(_ episodeID: String) async {
        guard let p = model.provider as? BookdoeProvider, let sid = model.activeServer?.id, let vm, let ep = vm.episode(id: episodeID) else { return }
        if var offline = model.downloads.offlineDetail(serverID: sid, itemID: episodeID) {
            offline.position = PlaybackPosition(trackID: episodeID, trackIndex: 0, positionInTrack: ep.positionSeconds, absolutePosition: ep.positionSeconds)
            await model.player.load(offline, provider: p, serverID: sid)
            return
        }
        do { try await PodcastPlayback.play(ep, of: vm.show, provider: p, player: EnginePlayer(model: model, provider: p, serverID: sid)) }
        catch { model.report(error) }
    }
}

/// Download, cancel or delete for one episode row. It observes the DownloadManager itself so progress redraws.
struct EpisodeDownloadButton: View {
    @ObservedObject var downloads: DownloadManager
    let serverID: UUID?
    let episodeID: String
    let start: () async -> Void

    var body: some View {
        if let sid = serverID {
            if downloads.isDownloaded(serverID: sid, itemID: episodeID) {
                Button(role: .destructive) { downloads.remove(serverID: sid, itemID: episodeID) } label: { Label("Delete download", systemImage: "trash") }
                    .buttonStyle(.borderless)
            } else if let p = downloads.active["\(sid.uuidString):\(episodeID)"], p.error == nil {
                Button(role: .destructive) { downloads.cancel(serverID: sid, itemID: episodeID) } label: { Label("Cancel download \(p.summary)", systemImage: "xmark.circle") }
                    .buttonStyle(.borderless)
            } else {
                Button { Task { await start() } } label: { Label("Download", systemImage: "arrow.down.circle") }
                    .buttonStyle(.borderless)
            }
        }
    }
}
