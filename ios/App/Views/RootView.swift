import SwiftUI
import BookdoeKit

struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @State private var section: AppSection? = .home
    @State private var columns: NavigationSplitViewVisibility = .all
    /// One navigation path per sidebar section, so switching sections keeps each stack.
    @State private var paths: [AppSection: NavigationPath] = [:]
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        Group {
            if model.activeServer == nil {
                NavigationStack { ServersView(forceAdd: true) }
            } else {
                if sizeClass == .regular {
                    NavigationSplitView(columnVisibility: $columns) {
                        List(sections, selection: $section) { s in
                            Label(s.title, systemImage: s.icon).tag(s).accessibilityIdentifier("sidebar.\(s.key)")
                        }
                        .navigationTitle("bookdoe")
                    } detail: {
                        NavigationStack(path: pathBinding(section ?? .home)) { content(for: section ?? .home) }
                            .safeAreaInset(edge: .bottom, spacing: 0) {
                                if !model.hidesMiniPlayer && !model.discoverShowsMiniPlayer { MiniPlayerBar(player: model.player, showPlayer: $model.showPlayer) }
                            }
                    }
                    .sheet(isPresented: $model.showPlayer) { PlayerView(player: model.player) }
                } else {
                    TabView {
                        ForEach(sections) { s in
                            tab(Label(s.title, systemImage: s.icon)) { content(for: s) }
                        }
                    }
                    .sheet(isPresented: $model.showPlayer) { PlayerView(player: model.player) }
                }
            }
        }
        .alert("Something went wrong", isPresented: Binding(get: { model.lastError != nil }, set: { if !$0 { model.lastError = nil } })) {
            Button("OK") { model.lastError = nil }
        } message: { Text(model.lastError ?? "") }
    }
}

/// The top-level destinations: tabs on iPhone, a sidebar at regular width (iPad).
enum AppSection: Hashable, Identifiable {
    case home, library, discover, podcasts, downloads, settings
    var id: Self { self }
    var title: LocalizedStringKey {
        switch self {
        case .home: "Home"
        case .library: "Library"
        case .discover: "Discover"
        case .podcasts: "Podcasts"
        case .downloads: "Downloads"
        case .settings: "Settings"
        }
    }
    /// Stable accessibility identifier suffix, independent of the localised title.
    var key: String {
        switch self {
        case .home: "home"
        case .library: "library"
        case .discover: "discover"
        case .podcasts: "podcasts"
        case .downloads: "downloads"
        case .settings: "settings"
        }
    }
    var icon: String {
        switch self {
        case .home: "house"
        case .library: "books.vertical"
        case .discover: "sparkles"
        case .podcasts: "mic"
        case .downloads: "arrow.down.circle"
        case .settings: "gear"
        }
    }
}

extension RootView {
    var sections: [AppSection] {
        [.home, .library] + (model.discoverChatAvailable ? [.discover] : []) + [.podcasts, .downloads, .settings]
    }

    func pathBinding(_ s: AppSection) -> Binding<NavigationPath> {
        Binding(get: { paths[s] ?? NavigationPath() }, set: { paths[s] = $0 })
    }

    @ViewBuilder
    func content(for section: AppSection) -> some View {
        switch section {
        case .home: HomeView()
        case .library: LibraryView()
        case .discover: DiscoverView()
        case .podcasts: PodcastsView()
        case .downloads: DownloadsView()
        case .settings: SettingsView()
        }
    }

    /// One tab. The mini player is inset inside each tab's stack so it sits above the
    /// tab bar; an inset on the TabView itself covered the tab bar while a book played.
    @ViewBuilder
    func tab<Content: View>(_ label: Label<Text, Image>, @ViewBuilder content: () -> Content) -> some View {
        NavigationStack { content() }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if !model.hidesMiniPlayer && !model.discoverShowsMiniPlayer { MiniPlayerBar(player: model.player, showPlayer: $model.showPlayer) }
            }
            .tabItem { label }
    }
}

struct MiniPlayerBar: View {
    @ObservedObject var player: PlayerEngine
    @Binding var showPlayer: Bool

    var body: some View {
        if let d = player.current {
            HStack(spacing: 12) {
                CoverImage(url: d.item.coverURL, size: 36)
                VStack(alignment: .leading) {
                    Text(d.item.title).font(.subheadline).lineLimit(1)
                    if let e = player.playbackError {
                        Text(e).font(.caption).foregroundStyle(.red).lineLimit(1)
                    } else {
                        Text(player.currentChapterTitle ?? d.item.authorLine).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer()
                Button { player.skip(-15) } label: { Image(systemName: "gobackward.15") }
                Button { player.togglePlayPause() } label: { Image(systemName: player.isPlaying ? "pause.fill" : "play.fill") }
                Button { player.skip(30) } label: { Image(systemName: "goforward.30") }
            }
            .font(.title3)
            .padding(.horizontal).padding(.vertical, 8)
            .background(.regularMaterial)
            .onTapGesture { showPlayer = true }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("miniplayer")
            .contextMenu {
                Button(role: .destructive) { Task { await player.close() } } label: { Label("Close player", systemImage: "xmark") }
                    .accessibilityIdentifier("miniplayer.close")
            }
            .gesture(DragGesture(minimumDistance: 20).onEnded { v in
                if v.translation.height > 40 && abs(v.translation.height) > abs(v.translation.width) { Task { await player.close() } }
            })
            .accessibilityAction(named: "Close player") { Task { await player.close() } }
        }
    }
}

struct HomeView: View {
    @EnvironmentObject private var model: AppModel
    @State private var continueItems: [MediaItem] = []
    @State private var recent: [MediaItem] = []
    @State private var loading = false

    var body: some View {
        List {
            if !continueItems.isEmpty {
                Section("Continue") {
                    ForEach(continueItems) { item in
                        continueRow(item)
                            .swipeActions(edge: .trailing) {
                                if let asset = item.assetID {
                                    Button { Task { await model.setFinished(true, assetID: asset, detail: nil) } } label: { Label(item.isEpisode ? "Played" : "Read", systemImage: "checkmark.circle") }.tint(.green)
                                    Button { Task { await model.setFinished(false, assetID: asset, detail: nil) } } label: { Label(item.isEpisode ? "Unplayed" : "Unread", systemImage: "arrow.uturn.backward.circle") }.tint(.orange)
                                }
                            }
                    }
                }
            }
            Section("Recently added") {
                if recent.isEmpty && !loading { Text("Nothing yet. Pull to refresh.").foregroundStyle(.secondary) }
                ForEach(recent) { item in NavigationLink(value: item.id) { ItemRow(item: item) } }
            }
        }
        .navigationTitle(model.activeServer?.name ?? "Home")
        .navigationDestination(for: String.self) { ItemDetailView(itemID: $0) }
        .refreshable { await load() }
        .task(id: model.progressRevision) { await load() }
        .overlay { if loading && recent.isEmpty { ProgressView() } }
        .readableWidth()
    }

    @ViewBuilder
    private func continueRow(_ item: MediaItem) -> some View {
        if item.isEpisode {
            Button { Task { await playEpisode(item) } } label: { ItemRow(item: item) }.buttonStyle(.plain)
        } else {
            NavigationLink(value: item.id) { ItemRow(item: item) }
        }
    }

    private func playEpisode(_ item: MediaItem) async {
        guard let p = model.provider as? BookdoeProvider, let sid = model.activeServer?.id else { return }
        do {
            await model.player.load(try await p.continueEpisodeDetail(item), provider: p, serverID: sid)
        } catch { model.report(error) }
    }

    private func load() async {
        guard let p = model.provider else { return }
        loading = true
        defer { loading = false }
        do {
            async let c = p.continueListening()
            async let r = p.browse(query: nil, kind: nil, sort: .added, offset: 0, limit: 20)
            continueItems = try await c
            recent = try await r.items
        } catch { model.report(error) }
    }
}

struct ItemRow: View {
    let item: MediaItem

    var body: some View {
        HStack(spacing: 12) {
            CoverImage(url: item.coverURL, size: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).lineLimit(2)
                if !item.authors.isEmpty { Text(item.authorLine).font(.subheadline).foregroundStyle(.secondary).lineLimit(1) }
                HStack(spacing: 6) {
                    Image(systemName: item.kind == .audiobook ? "headphones" : "book").font(.caption2)
                    if let d = item.durationSeconds { Text(Duration.seconds(d).formatted(.units(allowed: [.hours, .minutes], width: .narrow))).font(.caption2) }
                    if let s = item.series { Text(s + (item.seriesSequence.map { " #\($0)" } ?? "")).font(.caption2) }
                }.foregroundStyle(.secondary)
                if let p = item.progress, p > 0, p < 1 { ProgressView(value: p).tint(.accentColor) }
            }
        }
    }
}

extension View {
    /// Caps the content width on iPad so lists and text do not stretch edge to edge; a no-op on iPhone.
    func readableWidth(_ width: CGFloat = 720) -> some View {
        frame(maxWidth: width).frame(maxWidth: .infinity)
    }
}
