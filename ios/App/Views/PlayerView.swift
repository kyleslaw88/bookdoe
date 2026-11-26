import SwiftUI
import BookdoeKit

struct PlayerView: View {
    @ObservedObject var player: PlayerEngine
    @EnvironmentObject private var appModel: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var marks: BookmarksModel?
    @State private var showMarks = false
    @State private var showChapters = false
    @State private var flash: String?
    @State private var scrubbing = false
    @State private var scrubValue: Double = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: hasChapters ? 10 : 24) {
                if let d = player.current {
                    CoverImage(url: d.item.coverURL, size: hasChapters ? 120 : 200)
                    VStack(spacing: 4) {
                        Text(d.item.title).font(.title3).bold().multilineTextAlignment(.center)
                        Text(d.item.authorLine).foregroundStyle(.secondary)
                        if let title = player.currentChapterTitle { Text(title).font(.subheadline).foregroundStyle(.secondary) }
                    }
                    VStack {
                        Slider(value: Binding(get: { scrubbing ? scrubValue : player.absolutePosition }, set: { scrubValue = $0 }),
                               in: 0...max(1, player.duration)) { editing in
                            scrubbing = editing
                            if !editing { player.seek(toAbsolute: scrubValue) }
                        }
                        HStack {
                            Text(time(scrubbing ? scrubValue : player.absolutePosition))
                            Spacer()
                            Text("-" + time(max(0, player.duration - (scrubbing ? scrubValue : player.absolutePosition))))
                        }.font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    if let err = player.playbackError {
                        VStack(spacing: 6) {
                            Text(err).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
                            Button("Retry") { player.retry() }.buttonStyle(.borderedProminent)
                        }
                    }
                    HStack(spacing: 36) {
                        Button { player.skip(-15) } label: { Image(systemName: "gobackward.15").font(.title) }
                        Button { player.togglePlayPause() } label: { Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 64)) }
                        Button { player.skip(30) } label: { Image(systemName: "goforward.30").font(.title) }
                    }
                    HStack {
                        Menu {
                            ForEach(PlaybackSpeed.options, id: \.self) { r in
                                Button(PlaybackSpeed.label(for: r)) { player.rate = r }
                            }
                        } label: { Label(PlaybackSpeed.label(for: player.rate), systemImage: "speedometer") }
                        Spacer()
                        Button { Task { await addBookmark() } } label: { Label("Bookmark", systemImage: "bookmark") }
                            .accessibilityIdentifier("player.bookmark")
                        Button { showMarks = true } label: { Label("\(marks?.items.count ?? 0)", systemImage: "list.bullet") }
                            .accessibilityLabel("Bookmarks")
                            .accessibilityIdentifier("player.bookmarks")
                        Spacer()
                        Menu {
                            Button("Off") { player.setSleepTimer(minutes: nil) }
                            ForEach([5, 15, 30, 45, 60], id: \.self) { m in Button("\(m) minutes") { player.setSleepTimer(minutes: m) } }
                        } label: {
                            Label(player.sleepTimerEnds.map { "Sleep " + time($0.timeIntervalSinceNow) } ?? "Sleep timer", systemImage: "moon.zzz")
                        }
                    }
                    .font(.subheadline)
                    if d.effectiveChapters.count > 1 {
                        // Tap a chapter to jump to it; the time is where it starts in the book.
                        Button { showChapters = true } label: {
                            HStack {
                                Text("Chapters").font(.headline)
                                Spacer()
                                Text("\(d.effectiveChapters.count)").font(.subheadline).foregroundStyle(.secondary)
                                Image(systemName: "chevron.up").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("player.chaptersButton")
                        chapterList(d, onPick: {})
                    }
                } else {
                    Text("Nothing is playing.").foregroundStyle(.secondary)
                }
                if player.current?.effectiveChapters.count ?? 0 <= 1 { Spacer(minLength: 0) }
            }
            .padding()
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
            .overlay(alignment: .bottom) {
                if let flash {
                    Text(flash).font(.subheadline).padding(.horizontal, 14).padding(.vertical, 8)
                        .background(.regularMaterial, in: Capsule()).padding(.bottom, 24)
                        .transition(.opacity)
                }
            }
            .task(id: player.current?.assetID) { await loadMarks() }
            .sheet(isPresented: $showMarks) { marksSheet }
            .safeAreaInset(edge: .bottom) {
                if appModel.needsReauth { Text("Sign in again to sync your progress. It is saved on this device.").font(.footnote).foregroundStyle(.orange).padding(.horizontal) }
            }
            .sheet(isPresented: $showChapters) { chaptersSheet }
            .navigationTitle("Now playing")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .topBarTrailing) { Button("Stop") { Task { await player.close(); dismiss() } } }
            }
        }
    }

    private var hasChapters: Bool { (player.current?.effectiveChapters.count ?? 0) > 1 }

    /// Tap a chapter to jump to it; the time is where it starts in the book.
    private func chapterList(_ d: MediaDetail, onPick: @escaping () -> Void) -> some View {
        ScrollViewReader { proxy in
            List(Array(d.effectiveChapters.enumerated()), id: \.offset) { i, ch in
                let isCurrent = player.currentChapterIndex == i
                Button { player.seek(toChapter: ch); onPick() } label: {
                    HStack {
                        if isCurrent { Image(systemName: "speaker.wave.2.fill").font(.caption) }
                        Text(ch.displayTitle(index: i)).lineLimit(1).fontWeight(isCurrent ? .semibold : .regular)
                        Spacer()
                        Text(time(ch.start)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
                .tint(isCurrent ? .accentColor : .primary)
                .accessibilityIdentifier("player.chapterRow.\(i)")
                .id(i)
            }
            .listStyle(.plain)
            .frame(maxHeight: .infinity)
            .onAppear { if let i = player.currentChapterIndex { proxy.scrollTo(i, anchor: .center) } }
        }
    }

    @ViewBuilder private var chaptersSheet: some View {
        if let d = player.current {
            NavigationStack {
                chapterList(d, onPick: { showChapters = false })
                    .navigationTitle("Chapters")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showChapters = false } } }
            }
            .presentationDetents([.large])
        }
    }

    private func loadMarks() async {
        guard let d = player.current, let m = appModel.bookmarksModel(for: BookRef(itemID: d.item.id, assetID: d.assetID)) else { marks = nil; return }
        marks = m
        await m.load()
    }

    private func addBookmark() async {
        guard let marks, player.current != nil else { return }
        let position = player.absolutePosition
        let title = player.currentChapterTitle ?? "Bookmark"
        let percent = player.duration > 0 ? position / player.duration : nil
        if await marks.add(NewBookmark(position: position, percent: percent, title: title)) != nil {
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            show("Bookmarked at \(timeText(position))")
        } else {
            UINotificationFeedbackGenerator().notificationOccurred(.error)
            show(marks.error ?? "Could not save the bookmark")
        }
    }

    private func show(_ text: String) {
        withAnimation { flash = text }
        Task {
            try? await Task.sleep(for: .seconds(2.5))
            withAnimation { flash = nil }
        }
    }

    @ViewBuilder private var marksSheet: some View {
        if let marks {
            NavigationStack {
                BookmarksListView(model: marks, showHighlights: false, emptyText: "No bookmarks yet. Tap Bookmark while you listen.") { b in
                    if let p = b.position { player.seek(toAbsolute: p); showMarks = false }
                }
                .navigationTitle("Bookmarks")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showMarks = false } } }
            }
        }
    }

    private func time(_ s: Double) -> String {
        Duration.seconds(max(0, s)).formatted(.time(pattern: s >= 3600 ? .hourMinuteSecond : .minuteSecond))
    }
}
