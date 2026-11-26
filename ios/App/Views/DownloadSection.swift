import SwiftUI
import BookdoeKit

/// The download button, progress bar and Cancel/Retry on a book's page.
///
/// It observes the DownloadManager itself. The book page only observes the app model, and the
/// manager is a separate object, so when this lived inside the page its progress never redrew
/// until the page was left and opened again.
struct DownloadSection: View {
    @ObservedObject var downloads: DownloadManager
    let detail: MediaDetail
    let serverID: UUID?
    /// The headers a download must send (the server's credential), or nil when there is none.
    let headers: () -> [String: String]?

    private var itemID: String { detail.item.id }
    private var progress: DownloadManager.Progress? { serverID.flatMap { downloads.active["\($0.uuidString):\(itemID)"] } }
    private var downloaded: Bool { serverID.map { downloads.isDownloaded(serverID: $0, itemID: itemID) } ?? false }

    var body: some View {
        if let p = progress {
            VStack(alignment: .leading, spacing: 6) {
                ProgressView(value: p.fraction) { Text(p.error == nil ? "Downloading" : "Download failed") }
                Text(p.error ?? p.summary).font(.caption).foregroundStyle(p.error == nil ? Color.secondary : Color.red)
                    .accessibilityIdentifier("download.summary")
                HStack {
                    if p.error != nil, let sid = serverID, let h = headers() {
                        Button("Retry") { downloads.download(detail, serverID: sid, headers: h) }
                    }
                    Button("Cancel", role: .destructive) { if let sid = serverID { downloads.cancel(serverID: sid, itemID: itemID) } }
                        .accessibilityIdentifier("download.cancel")
                }.font(.subheadline)
            }
        } else if downloaded {
            Button(role: .destructive) {
                if let sid = serverID { downloads.remove(serverID: sid, itemID: itemID) }
            } label: { Label("Remove download", systemImage: "trash") }
            .accessibilityIdentifier("download.remove")
        } else if !detail.tracks.isEmpty || detail.ebookDownloadURL != nil {
            Button {
                if let sid = serverID, let h = headers() { downloads.download(detail, serverID: sid, headers: h) }
            } label: { Label("Download for offline", systemImage: "arrow.down.circle") }
            .accessibilityIdentifier("download.start")
        }
    }
}
