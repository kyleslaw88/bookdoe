import Foundation
import BookdoeKit

/// Executes DownloadPlans with a background URLSession and keeps the DownloadIndex.
/// Files land under `root/<serverID>/<item>/...`; the index maps items to them.
@MainActor
final class DownloadManager: NSObject, ObservableObject, URLSessionDownloadDelegate {
    struct Progress: Equatable {
        var itemID: String
        var serverID: UUID
        var title: String = ""
        var completedFiles: Int
        var totalFiles: Int
        var bytes: Int64
        /// Bytes received so far and bytes expected, across all files (expected is 0 until known).
        var received: Int64 = 0
        var expected: Int64 = 0
        var error: String?
        var fraction: Double {
            if expected > 0 { return min(1, Double(received) / Double(expected)) }
            return totalFiles == 0 ? 0 : Double(completedFiles) / Double(totalFiles)
        }
        var summary: String {
            let pct = Int((fraction * 100).rounded())
            guard expected > 0 else { return "Starting download..." }
            let f = ByteCountFormatter()
            f.countStyle = .file
            return "\(pct)%  \(f.string(fromByteCount: received)) of \(f.string(fromByteCount: expected))"
        }
    }

    @Published private(set) var active: [String: Progress] = [:]
    @Published private(set) var completed: [DownloadedItem] = []

    /// Background session id. Deliberately kept at the value shipped in TestFlight builds so
    /// downloads already in flight stay attached after an update. Changing it orphans them;
    /// do so only with a migration or explicit acceptance.
    static let sessionIdentifier = "com.example.alexandria.downloads"

    let root: URL
    let index: DownloadIndex
    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        cfg.isDiscretionary = false
        cfg.sessionSendsLaunchEvents = true
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()
    private var tasks: [Int: (plan: DownloadPlan, file: DownloadPlan.File, detail: MediaDetail)] = [:]
    private var finished: [String: [DownloadedTrack]] = [:]
    private var bytes: [String: Int64] = [:]
    private var inflight: [Int: (written: Int64, expected: Int64)] = [:]

    init(root: URL, index: DownloadIndex) {
        self.root = root
        self.index = index
        super.init()
        completed = index.items
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    private func key(_ serverID: UUID, _ itemID: String) -> String { "\(serverID.uuidString):\(itemID)" }

    func isDownloaded(serverID: UUID, itemID: String) -> Bool {
        guard let item = index.item(serverID: serverID, itemID: itemID) else { return false }
        return filesIntact(item)
    }

    /// A download counts only while every file is on disk and not empty; an entry whose
    /// files went missing or were cut short must never be played from.
    private func filesIntact(_ item: DownloadedItem) -> Bool {
        item.tracks.allSatisfy { t in
            let size = (try? FileManager.default.attributesOfItem(atPath: root.appendingPathComponent(t.relativePath).path)[.size] as? Int64) ?? 0
            // A `.bin` file is from an older build that could not name downloads; it will not play.
            return size > 0 && URL(fileURLWithPath: t.relativePath).pathExtension.lowercased() != "bin"
        }
    }

    /// The downloaded ebook file (`book.epub` or `book.pdf`), if this item has one.
    func ebookFileURL(serverID: UUID, itemID: String) -> URL? {
        let dir = root.appendingPathComponent(serverID.uuidString).appendingPathComponent(DownloadPlanNaming.safe(itemID))
        guard let name = (try? FileManager.default.contentsOfDirectory(atPath: dir.path))?.first(where: { $0.hasPrefix("book.") }) else { return nil }
        return dir.appendingPathComponent(name)
    }

    func offlineDetail(serverID: UUID, itemID: String) -> MediaDetail? {
        guard let item = index.item(serverID: serverID, itemID: itemID) else { return nil }
        guard filesIntact(item) else {
            remove(serverID: serverID, itemID: itemID)   // stale entry: drop it so the book streams instead
            return nil
        }
        return item.detail(root: root)
    }

    func download(_ detail: MediaDetail, serverID: UUID, headers: [String: String]) {
        let plan = DownloadPlan(serverID: serverID, detail: detail, headers: headers)
        let k = key(serverID, detail.item.id)
        if active[k]?.error != nil { cancel(serverID: serverID, itemID: detail.item.id) }   // retry after a failure
        guard active[k] == nil, !plan.files.isEmpty else { return }
        active[k] = Progress(itemID: detail.item.id, serverID: serverID, title: detail.item.title, completedFiles: 0, totalFiles: plan.files.count, bytes: 0,
                             expected: plan.files.reduce(0) { $0 + ($1.expectedBytes ?? 0) })
        finished[k] = []
        bytes[k] = 0
        for file in plan.files {
            var req = URLRequest(url: file.remote)
            for (h, v) in file.headers { req.setValue(v, forHTTPHeaderField: h) }
            let task = session.downloadTask(with: req)
            tasks[task.taskIdentifier] = (plan, file, detail)
            task.resume()
        }
    }

    /// Stop an in-progress (or failed) download and forget it.
    func cancel(serverID: UUID, itemID: String) {
        let k = key(serverID, itemID)
        let ids = tasks.filter { key($0.value.plan.serverID, $0.value.plan.itemID) == k }.map(\.key)
        for id in ids { tasks[id] = nil; inflight[id] = nil }
        session.getAllTasks { all in all.filter { ids.contains($0.taskIdentifier) }.forEach { $0.cancel() } }
        active[k] = nil
        finished[k] = nil
        bytes[k] = nil
        try? FileManager.default.removeItem(at: root.appendingPathComponent("\(serverID.uuidString)/\(DownloadPlanNaming.safe(itemID))"))
    }

    func remove(serverID: UUID, itemID: String) {
        if let item = index.remove(serverID: serverID, itemID: itemID) {
            for t in item.tracks { try? FileManager.default.removeItem(at: root.appendingPathComponent(t.relativePath)) }
            try? FileManager.default.removeItem(at: root.appendingPathComponent("\(serverID.uuidString)/\(DownloadPlanNaming.safe(itemID))"))
        }
        completed = index.items
    }

    func removeAll(serverID: UUID) {
        for item in index.items where item.serverID == serverID { remove(serverID: serverID, itemID: item.itemID) }
        try? FileManager.default.removeItem(at: root.appendingPathComponent(serverID.uuidString))
    }

    var totalBytes: Int64 { index.items.reduce(0) { $0 + $1.bytes } }

    // MARK: URLSessionDownloadDelegate (called off the main actor; hop back)

    nonisolated func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        // The temp file is deleted when this returns, so move it synchronously first.
        let staging = FileManager.default.temporaryDirectory.appendingPathComponent("bookdoe-\(UUID().uuidString)")
        try? FileManager.default.moveItem(at: location, to: staging)
        let status = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 200
        let suggested = downloadTask.response?.suggestedFilename
        let mime = downloadTask.response?.mimeType
        let id = downloadTask.taskIdentifier
        Task { @MainActor in self.finishedFile(taskID: id, staging: staging, status: status, suggestedName: suggested, mimeType: mime) }
    }

    nonisolated func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        let id = downloadTask.taskIdentifier
        Task { @MainActor in self.wrote(taskID: id, written: totalBytesWritten, expected: totalBytesExpectedToWrite) }
    }

    private func wrote(taskID: Int, written: Int64, expected: Int64) {
        guard let entry = tasks[taskID] else { return }
        inflight[taskID] = (written, expected)
        let k = key(entry.plan.serverID, entry.plan.itemID)
        updateTotals(k)
    }

    /// Bytes of finished files plus what is in flight now; the expected total prefers the
    /// server's own Content-Length for each file in flight, then the planned size.
    private func updateTotals(_ k: String) {
        guard var p = active[k] else { return }
        let running = tasks.filter { key($0.value.plan.serverID, $0.value.plan.itemID) == k }
        var received = bytes[k] ?? 0
        var expected = bytes[k] ?? 0
        for (id, entry) in running {
            let w = inflight[id]
            received += w?.written ?? 0
            let known = (w?.expected ?? 0) > 0 ? w!.expected : (entry.file.expectedBytes ?? 0)
            expected += max(known, w?.written ?? 0)
        }
        // Skip tiny changes so a fast download does not redraw the screen hundreds of times a second.
        if abs(p.fraction - (expected > 0 ? min(1, Double(received) / Double(expected)) : p.fraction)) < 0.002, p.expected == expected, received < expected { return }
        p.received = received
        p.expected = expected
        active[k] = p
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error else { return }
        let id = task.taskIdentifier
        Task { @MainActor in self.failed(taskID: id, message: error.localizedDescription) }
    }

    private func finishedFile(taskID: Int, staging: URL, status: Int, suggestedName: String?, mimeType: String?) {
        guard let entry = tasks.removeValue(forKey: taskID) else { try? FileManager.default.removeItem(at: staging); return }
        inflight[taskID] = nil
        let k = key(entry.plan.serverID, entry.plan.itemID)
        guard (200..<300).contains(status) else {
            try? FileManager.default.removeItem(at: staging)
            active[k]?.error = "Server answered HTTP \(status)"
            return
        }
        // The planned name comes from the URL, which has no extension; name the file by what the
        // server says it is, because AVPlayer will not play a local `.bin`.
        let planned = (entry.file.localRelativePath as NSString).pathExtension
        let ext = DownloadPlan.resolvedExtension(suggestedFilename: suggestedName, mimeType: mimeType, planned: planned)
        let relativePath = ((entry.file.localRelativePath as NSString).deletingPathExtension as String) + "." + ext
        let dest = root.appendingPathComponent(relativePath)
        do {
            try FileManager.default.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.moveItem(at: staging, to: dest)
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            var d = dest; try? d.setResourceValues(values)
        } catch {
            active[k]?.error = error.localizedDescription
            return
        }
        let size = (try? FileManager.default.attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        bytes[k, default: 0] += size
        if entry.file.trackID != "ebook", let track = entry.detail.tracks.first(where: { $0.id == entry.file.trackID }) {
            finished[k, default: []].append(DownloadedTrack(trackID: track.id, title: track.title, relativePath: relativePath, durationSeconds: track.durationSeconds))
        }
        active[k]?.completedFiles += 1
        active[k]?.bytes = bytes[k] ?? 0
        updateTotals(k)
        if let p = active[k], p.completedFiles >= p.totalFiles {
            let ordered = (finished[k] ?? []).sorted { a, b in
                (entry.detail.tracks.first { $0.id == a.trackID }?.index ?? 0) < (entry.detail.tracks.first { $0.id == b.trackID }?.index ?? 0)
            }
            index.upsert(DownloadedItem(serverID: entry.plan.serverID, detail: entry.detail, tracks: ordered, bytes: bytes[k] ?? 0))
            completed = index.items
            active[k] = nil
            finished[k] = nil
            bytes[k] = nil
        }
    }

    private func failed(taskID: Int, message: String) {
        guard let entry = tasks.removeValue(forKey: taskID) else { return }
        inflight[taskID] = nil
        active[key(entry.plan.serverID, entry.plan.itemID)]?.error = message
    }
}

enum DownloadPlanNaming {
    static func safe(_ s: String) -> String {
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        let out = String(s.map { allowed.contains($0) ? $0 : "_" })
        return out.isEmpty ? "item" : String(out.prefix(80))
    }
}
