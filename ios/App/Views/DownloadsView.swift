import SwiftUI
import BookdoeKit

struct DownloadsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        // The manager is an ObservableObject owned by the model; observing it from a
        // child view is what makes progress updates redraw this screen.
        DownloadsList(downloads: model.downloads)
    }
}

private struct DownloadsList: View {
    @EnvironmentObject private var model: AppModel
    @ObservedObject var downloads: DownloadManager

    var body: some View {
        List {
            if !downloads.active.isEmpty {
                Section("In progress") {
                    ForEach(Array(downloads.active.values), id: \.itemID) { p in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(p.title.isEmpty ? "Downloading" : p.title).lineLimit(2)
                            ProgressView(value: p.fraction)
                            HStack {
                                Text(p.error ?? p.summary).font(.caption).foregroundStyle(p.error == nil ? Color.secondary : Color.red)
                                Spacer()
                                Button("Cancel", role: .destructive) { downloads.cancel(serverID: p.serverID, itemID: p.itemID) }
                                    .font(.caption).buttonStyle(.borderless)
                            }
                        }
                    }
                }
            }
            Section("Available offline") {
                if downloads.completed.isEmpty { Text("Nothing downloaded yet. Open a book and choose Download for offline.").foregroundStyle(.secondary) }
                ForEach(downloads.completed) { item in
                    NavigationLink(value: item) {
                        VStack(alignment: .leading) {
                            Text(item.title)
                            Text("\(item.authors.joined(separator: ", "))  \(ByteCountFormatter.string(fromByteCount: item.bytes, countStyle: .file))").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { idx in
                    for i in idx { let it = downloads.completed[i]; downloads.remove(serverID: it.serverID, itemID: it.itemID) }
                }
            }
            Section { Text("Total: " + ByteCountFormatter.string(fromByteCount: downloads.totalBytes, countStyle: .file)).font(.caption).foregroundStyle(.secondary) }
        }
        .navigationTitle("Downloads")
        .navigationDestination(for: DownloadedItem.self) { item in
            OfflineItemView(item: item)
        }
    }
}

extension DownloadedItem: Hashable {
    public func hash(into hasher: inout Hasher) { hasher.combine(id) }
}

struct OfflineItemView: View {
    @EnvironmentObject private var model: AppModel
    let item: DownloadedItem

    var body: some View {
        VStack(spacing: 16) {
            Text(item.title).font(.title2).bold()
            Text(item.authors.joined(separator: ", ")).foregroundStyle(.secondary)
            Button {
                Task {
                    let detail = item.detail(root: model.downloads.root)
                    // Offline playback still reports progress through the queue; the
                    // provider for that server flushes it when the network is back.
                    let server = model.servers.first { $0.id == item.serverID } ?? model.activeServer
                    guard let server else { return }
                    await model.player.load(detail, provider: model.factory.make(server), serverID: server.id)
                }
            } label: { Label("Play offline", systemImage: "play.fill").frame(maxWidth: .infinity) }
            .buttonStyle(.borderedProminent)
            Spacer()
        }
        .padding()
        .navigationTitle("Offline")
    }
}
