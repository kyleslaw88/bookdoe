import SwiftUI
import BookdoeKit

struct ItemDetailView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.horizontalSizeClass) private var sizeClass
    let itemID: String
    @State private var detail: MediaDetail?
    @State private var loading = true
    @State private var offline = false
    @State private var showPlayer = false
    @State private var shareURL: URL?
    @State private var reader: ReaderFile?
    @State private var marks: BookmarksModel?
    @State private var ebookMarks: BookmarksModel?   // only for a work that has audio and an ebook
    @State private var preparingBook = false

    private var regular: Bool { sizeClass == .regular }

    private var serverID: UUID? { model.activeServer?.id }

    var body: some View {
        ScrollView {
            if let d = detail {
                VStack(alignment: .leading, spacing: 16) {
                    if regular {
                        // Regular width: cover on the left; metadata, actions and description in a readable column beside it.
                        HStack(alignment: .top, spacing: 32) {
                            CoverImage(url: d.item.coverURL, size: 260)
                            VStack(alignment: .leading, spacing: 16) {
                                header(d)
                                actions(d)
                                description(d)
                            }
                            .frame(maxWidth: 560, alignment: .leading)
                            .accessibilityIdentifier("detail.column")
                        }
                    } else {
                        HStack(alignment: .top, spacing: 16) {
                            CoverImage(url: d.item.coverURL, size: 120)
                            header(d)
                        }
                        actions(d)
                        description(d)
                    }
                    chapters(d)
                }
                .padding(regular ? 32 : 16)
            } else if loading {
                ProgressView().padding()
            }
        }
        .readableWidth(regular ? 960 : 720)
        .navigationTitle(detail?.item.title ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if model.activeServer?.kind == .bookdoe, let d = detail {
                ToolbarItem(placement: .topBarTrailing) { readStateMenu(d) }
            }
        }
        .task(id: itemID) { await load() }
        .sheet(isPresented: $showPlayer) { PlayerView(player: model.player) }
        .fullScreenCover(item: $reader) { file in
            if let readMarks = ebookMarks ?? marks {
                EbookReaderView(file: file, bookmarks: readMarks) { location, percent in saveReading(file, location: location, percent: percent) }
            }
        }
        .sheet(item: $shareURL) { url in ShareLinkSheet(url: url, headers: (try? model.provider?.mediaHeaders()) ?? [:]) }
    }

    @ViewBuilder
    private func header(_ d: MediaDetail) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(d.item.title).font(regular ? .largeTitle : .title2).bold()
            if let s = d.item.subtitle { Text(s).foregroundStyle(.secondary) }
            if !d.item.authors.isEmpty { Text(d.item.authorLine) }
            if !d.item.narrators.isEmpty { Text("Read by " + d.item.narrators.joined(separator: ", ")).font(.subheadline).foregroundStyle(.secondary) }
            if let s = d.item.series { Text(s + (d.item.seriesSequence.map { " #\($0)" } ?? "")).font(.subheadline).foregroundStyle(.secondary) }
            if d.totalDuration > 0 { Text(Duration.seconds(d.totalDuration).formatted(.units(allowed: [.hours, .minutes]))).font(.subheadline).foregroundStyle(.secondary) }
            if offline { Label("Playing from download", systemImage: "arrow.down.circle.fill").font(.caption).foregroundStyle(.green) }
        }
    }

    @ViewBuilder
    private func actions(_ d: MediaDetail) -> some View {
        if d.hasAudio {
            Button {
                Task { await play(d) }
            } label: {
                Label(d.position.map { $0.absolutePosition > 0 } == true ? "Resume" : "Play", systemImage: "play.fill").frame(maxWidth: regular ? 320 : .infinity)
            }
            .buttonStyle(.borderedProminent)
            if let pos = d.position, pos.absolutePosition > 0 {
                Text("Position: " + Duration.seconds(pos.absolutePosition).formatted(.time(pattern: .hourMinuteSecond))).font(.caption).foregroundStyle(.secondary)
            }
        }
        if d.hasEbook {
            Button {
                Task { await openReader(d) }
            } label: {
                HStack {
                    if preparingBook { ProgressView() }
                    Text(readLabel(d))
                }.frame(maxWidth: regular ? 320 : .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(preparingBook)
            if let u = d.ebookDownloadURL {
                Button { shareURL = u } label: { Label("Open in another app", systemImage: "square.and.arrow.up") }
                    .font(.footnote)
            }
        }
        DownloadSection(downloads: model.downloads, detail: d, serverID: serverID) { (try? model.provider?.mediaHeaders()) ?? nil }
        if let marks, d.hasAudio {
            NavigationLink {
                BookmarksScreen(model: marks, kind: .audiobook) { b in
                    if let p = b.position { Task { await play(d, at: p) } }
                }
            } label: {
                Label(marksLabel(marks), systemImage: "bookmark").frame(maxWidth: .infinity, alignment: .leading)
            }
            .accessibilityIdentifier("detail.bookmarks")
        }
        if let readMarks = ebookMarks ?? (d.hasAudio ? nil : marks), d.hasEbook {
            NavigationLink {
                BookmarksScreen(model: readMarks, kind: .ebook) { b in
                    if let loc = b.location { Task { await openReader(d, start: loc) } }
                }
            } label: {
                Label(d.hasAudio ? "Reading bookmarks & notes" : marksLabel(readMarks), systemImage: "bookmark").frame(maxWidth: .infinity, alignment: .leading)
            }
            .accessibilityIdentifier(d.hasAudio ? "detail.ebookBookmarks" : "detail.bookmarks")
        }
    }

    @ViewBuilder
    private func description(_ d: MediaDetail) -> some View {
        if let desc = d.description, !desc.isEmpty {
            Text(HTMLText.plain(desc)).font(.body)
        }
    }

    @ViewBuilder
    private func chapters(_ d: MediaDetail) -> some View {
        if d.effectiveChapters.count > 1 {
            Text("Chapters").font(.headline)
            ForEach(Array(d.effectiveChapters.enumerated()), id: \.offset) { i, ch in
                Button {
                    Task { await play(d, chapter: ch) }
                } label: {
                    HStack {
                        Text("\(i + 1). \(ch.title.isEmpty ? "Chapter \(i + 1)" : ch.title)").lineLimit(1)
                        Spacer()
                        Text(Duration.seconds(ch.start).formatted(.time(pattern: .hourMinuteSecond))).foregroundStyle(.secondary).font(.caption)
                    }
                }.tint(.primary)
                Divider()
            }
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        guard let p = model.provider, let sid = serverID else { return }
        do {
            var d = try await p.detail(id: itemID)
            if let off = model.downloads.offlineDetail(serverID: sid, itemID: itemID) {
                d.tracks = off.tracks   // play local files; keep server metadata and position
                offline = true
            }
            detail = d
            makeMarks(for: d)
        } catch {
            if let off = model.downloads.offlineDetail(serverID: sid, itemID: itemID) {
                detail = off
                offline = true
                makeMarks(for: off)
            } else {
                model.report(error)
            }
        }
    }

    private func makeMarks(for d: MediaDetail) {
        if marks == nil || marks?.book.assetID != d.assetID {
            marks = model.bookmarksModel(for: BookRef(itemID: d.item.id, assetID: d.assetID))
            Task { await marks?.load() }
        }
        if d.hasAudio, d.hasEbook, ebookMarks == nil || ebookMarks?.book.assetID != d.readingAssetID {
            ebookMarks = model.bookmarksModel(for: BookRef(itemID: d.item.id, assetID: d.readingAssetID))
            Task { await ebookMarks?.load() }
        }
    }

    private func marksLabel(_ m: BookmarksModel) -> String {
        guard m.loaded, !m.items.isEmpty else { return "Bookmarks & notes" }
        return "Bookmarks & notes (\(m.items.count))"
    }

    private func isFinished(_ d: MediaDetail) -> Bool { (d.position?.percent ?? 0) >= 0.995 }
    private func hasProgress(_ d: MediaDetail) -> Bool { (d.position?.percent ?? 0) > 0 || (d.position?.absolutePosition ?? 0) > 0 }

    @ViewBuilder
    private func readStateMenu(_ d: MediaDetail) -> some View {
        Menu {
            if !isFinished(d) {
                Button { Task { await setFinished(true, d) } } label: { Label("Mark as read", systemImage: "checkmark.circle") }
            }
            if hasProgress(d) {
                Button { Task { await setFinished(false, d) } } label: { Label("Mark as unread", systemImage: "arrow.uturn.backward.circle") }
            }
        } label: { Image(systemName: "ellipsis.circle") }
        .accessibilityLabel("Book actions")
    }

    private func setFinished(_ finished: Bool, _ d: MediaDetail) async {
        await model.setFinished(finished, assetID: d.assetID, detail: d)
        await load()
    }

    private func readLabel(_ d: MediaDetail) -> String {
        if let p = d.readingPosition?.percent, p > 0.005, p < 0.995 { return "Continue reading (\(Int((p * 100).rounded()))%)" }
        return d.readingPosition?.percent ?? 0 >= 0.995 ? "Read again" : "Read"
    }

    private func openReader(_ d: MediaDetail, start: String? = nil) async {
        guard let sid = serverID else { return }
        preparingBook = true
        defer { preparingBook = false }
        do {
            let headers = (try? model.provider?.mediaHeaders()) ?? [:]
            let url = try await EbookFetcher.localFile(downloads: model.downloads, serverID: sid, detail: d, headers: headers)
            reader = ReaderFile(url: url, format: url.pathExtension.lowercased() == "pdf" ? "pdf" : "epub", title: d.item.title,
                                itemID: d.item.id, assetID: d.readingAssetID, start: start ?? d.readingPosition?.location)
        } catch { model.report(error) }
    }

    /// Queue the position for the server (offline-safe) and remember it here so the next
    /// open in this session starts from it.
    private func saveReading(_ file: ReaderFile, location: String, percent: Double?) {
        guard let sid = serverID else { return }
        let saved = PlaybackPosition(percent: percent, updatedAt: Date(), location: location)
        if detail?.ebookAssetID != nil { detail?.ebookPosition = saved } else { detail?.position = saved }
        let update = ProgressUpdate(serverID: sid, itemID: file.itemID, assetID: file.assetID, percent: percent, location: location)
        Task {
            await model.progressQueue.enqueue(update)
            await model.flushProgress(session: nil)
        }
    }

    private func play(_ d: MediaDetail, chapter: Chapter? = nil, at seconds: Double? = nil) async {
        guard let p = model.provider, let sid = serverID else { return }
        var start = d
        if let at = seconds ?? chapter?.start {
            let loc = d.locate(absolute: at)
            start.position = PlaybackPosition(trackID: d.tracks.first { $0.index == loc.index }?.id, trackIndex: loc.index, positionInTrack: loc.offset, absolutePosition: at, updatedAt: Date())
        }
        await model.player.load(start, provider: p, serverID: sid)
        showPlayer = true
    }
}

extension URL: Identifiable {
    public var id: String { absoluteString }
}

/// Downloads the ebook to a temp file with auth headers and offers the share sheet.
struct ShareLinkSheet: View {
    let url: URL
    let headers: [String: String]
    @State private var local: URL?
    @State private var error: String?

    var body: some View {
        VStack(spacing: 16) {
            if let local {
                ShareLink(item: local) { Label("Share or open in another app", systemImage: "square.and.arrow.up") }.buttonStyle(.borderedProminent)
            } else if let error {
                Text(error).foregroundStyle(.red)
            } else {
                ProgressView("Fetching the file...")
            }
        }
        .padding()
        .presentationDetents([.medium])
        .task {
            var req = URLRequest(url: url)
            for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
            do {
                let (tmp, resp) = try await URLSession.shared.download(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else { error = "The server refused the download."; return }
                let name = resp.suggestedFilename ?? url.lastPathComponent
                let dest = FileManager.default.temporaryDirectory.appendingPathComponent(name)
                try? FileManager.default.removeItem(at: dest)
                try FileManager.default.moveItem(at: tmp, to: dest)
                local = dest
            } catch { self.error = error.localizedDescription }
        }
    }
}

/// A book's bookmarks and notes as their own screen, reached from the book's page.
struct BookmarksScreen: View {
    @ObservedObject var model: BookmarksModel
    let kind: MediaKind
    let onOpen: (Bookmark) -> Void

    var body: some View {
        BookmarksListView(model: model, showHighlights: kind == .ebook, emptyText: kind == .audiobook
                          ? "No bookmarks yet. Tap Bookmark in the player while you listen."
                          : "Nothing saved yet. Tap the ribbon on a page, or highlight text with the highlighter.", onOpen: onOpen)
            .navigationTitle("Bookmarks & notes")
            .navigationBarTitleDisplayMode(.inline)
    }
}
