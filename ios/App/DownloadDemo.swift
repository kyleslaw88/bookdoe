#if DEBUG
import SwiftUI
import BookdoeKit

/// Debug builds only (ALX_DOWNLOAD_DEMO=<url of a large file>): downloads that file the way a book is
/// downloaded and shows the progress twice, so the UI tests can check it moves while the screen stays put.
/// "Old way" is a parent that does not observe the manager, the shape of the bug this once had.
struct DownloadDemo: View {
    let url: URL
    /// A plain holder, like AppModel: the manager hangs off it, but the holder is not observable, so this view
    /// (like the book page used to) is never told that the manager changed.
    private final class Holder { let downloads: DownloadManager; init(_ d: DownloadManager) { downloads = d } }
    @State private var holder: Holder
    private let serverID = UUID()

    init(url: URL) {
        self.url = url
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("download-demo-\(UUID().uuidString)")
        _holder = State(initialValue: Holder(DownloadManager(root: dir, index: DownloadIndex(fileURL: dir.appendingPathComponent("index.json")))))
    }
    private var downloads: DownloadManager { holder.downloads }

    private var detail: MediaDetail {
        let track = Track(id: "t1", index: 0, title: "Demo", url: url, durationSeconds: 60, startOffset: 0)
        return MediaDetail(item: MediaItem(id: "demo-book", title: "Demo book", kind: .audiobook), tracks: [track], assetID: "demo")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text("Download demo").font(.title2).bold()
            VStack(alignment: .leading) {
                Text("A book's download section").font(.caption).foregroundStyle(.secondary)
                DownloadSection(downloads: downloads, detail: detail, serverID: serverID) { [:] }
            }
            VStack(alignment: .leading) {
                Text("Old way: a parent that does not observe").font(.caption).foregroundStyle(.secondary)
                OldWay(downloads: downloads)
            }
            Spacer()
        }
        .padding()
    }
}

/// Reads the manager's progress straight from a parent that does not observe it: this is what the book page did.
private struct OldWay: View {
    let downloads: DownloadManager
    var body: some View { Text(downloads.active.values.first?.summary ?? "idle").accessibilityIdentifier("download.old.summary") }
}
#endif
