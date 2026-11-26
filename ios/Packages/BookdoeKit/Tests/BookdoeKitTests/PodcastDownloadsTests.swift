import XCTest
@testable import BookdoeKit

private final class FakeStorage: EpisodeStorage {
    var files: [String: Data] = [:]
    func exists(_ relativePath: String) -> Bool { files[relativePath] != nil }
    func delete(_ relativePath: String) -> Bool { files.removeValue(forKey: relativePath) != nil }
    func url(for relativePath: String) -> URL { URL(string: "fake://downloads/")!.appendingPathComponent(relativePath) }
    func read(_ url: URL) -> Data? { files[url.absoluteString.replacingOccurrences(of: "fake://downloads/", with: "")] }
}

private struct Offline: LocalizedError { var errorDescription: String? { "offline" } }

/// Serves `body` into the storage while `online`; otherwise throws. `gate` holds a fetch open until resumed.
private final class FakeNetwork: EpisodeFetcher {
    let storage: FakeStorage
    var online = true
    var body = Data(repeating: 7, count: 1234)
    var fetched: [URL] = []
    var leavePartialOnFailure = false
    var gate: CheckedContinuation<Void, Never>?
    var holdOpen = false
    var opened: (() -> Void)?

    init(storage: FakeStorage) { self.storage = storage }

    func fetch(_ url: URL, headers: [String: String], to relativePath: String) async throws -> Int64 {
        fetched.append(url)
        if holdOpen {
            await withCheckedContinuation { gate = $0; opened?() }
        }
        guard online else {
            if leavePartialOnFailure { storage.files[relativePath] = Data(count: 10) }
            throw Offline()
        }
        storage.files[relativePath] = body
        return Int64(body.count)
    }
}

private func episodeDetail(_ id: String = "ep1") -> MediaDetail {
    MediaDetail(item: MediaItem(id: id, title: "Pilot", authors: ["The Show"], kind: .audiobook, durationSeconds: 1000),
                tracks: [Track(id: id, index: 0, title: "Pilot", url: URL(string: "https://books.example.com/api/player/v1/podcasts/episodes/\(id)/audio")!, durationSeconds: 1000)],
                assetID: "podcast:" + id)
}

@MainActor
final class PodcastDownloadStateMachineTests: XCTestCase {
    private let serverID = UUID()
    private let storage = FakeStorage()
    private lazy var network = FakeNetwork(storage: storage)
    private lazy var downloads = PodcastDownloads(serverID: serverID, storage: storage, fetcher: network, index: DownloadIndex())

    func testStartsNotDownloadedAndEndsDownloaded() async {
        XCTAssertEqual(downloads.state("ep1"), .notDownloaded)
        let end = await downloads.download(episodeDetail(), headers: ["Authorization": "Bearer t"])
        XCTAssertEqual(end, .downloaded(bytes: 1234))
        XCTAssertEqual(downloads.state("ep1"), .downloaded(bytes: 1234))
        XCTAssertEqual(storage.files.count, 1)
        XCTAssertEqual(downloads.usedBytes, 1234)
    }

    func testIsDownloadingWhileTheFetchIsOpenAndASecondTapDoesNotRefetch() async {
        network.holdOpen = true
        let opened = expectation(description: "fetch open")
        network.opened = { opened.fulfill() }
        let task = Task { await downloads.download(episodeDetail(), headers: [:]) }
        await fulfillment(of: [opened], timeout: 5)
        XCTAssertEqual(downloads.state("ep1"), .downloading)
        _ = await downloads.download(episodeDetail(), headers: [:])
        XCTAssertEqual(network.fetched.count, 1)
        network.gate?.resume()
        _ = await task.value
        XCTAssertEqual(downloads.state("ep1"), .downloaded(bytes: 1234))
    }

    func testFailureRemovesThePartialFileAndCanBeRetried() async {
        network.online = false
        network.leavePartialOnFailure = true
        let failed = await downloads.download(episodeDetail(), headers: [:])
        XCTAssertEqual(failed, .failed("offline"))
        XCTAssertTrue(storage.files.isEmpty)
        XCTAssertEqual(downloads.state("ep1"), .failed("offline"))

        network.online = true
        let retried = await downloads.download(episodeDetail(), headers: [:])
        XCTAssertEqual(retried, .downloaded(bytes: 1234))
    }

    func testAlreadyDownloadedIsNotFetchedAgain() async {
        _ = await downloads.download(episodeDetail(), headers: [:])
        _ = await downloads.download(episodeDetail(), headers: [:])
        XCTAssertEqual(network.fetched.count, 1)
    }

    func testDeleteRemovesTheFileAndFreesItsStorage() async {
        _ = await downloads.download(episodeDetail("ep1"), headers: [:])
        _ = await downloads.download(episodeDetail("ep2"), headers: [:])
        XCTAssertEqual(downloads.usedBytes, 2468)
        XCTAssertEqual(downloads.delete("ep1"), 1234)
        XCTAssertEqual(downloads.state("ep1"), .notDownloaded)
        XCTAssertEqual(storage.files.count, 1)
        XCTAssertFalse(storage.files.keys.contains { $0.contains("ep1") })
        XCTAssertEqual(downloads.usedBytes, 1234)
        XCTAssertEqual(downloads.delete("ep1"), 0, "deleting again frees nothing")
    }
}

@MainActor
final class PodcastOfflinePlaybackTests: XCTestCase {
    private let storage = FakeStorage()
    private lazy var network = FakeNetwork(storage: storage)
    private lazy var downloads = PodcastDownloads(serverID: UUID(), storage: storage, fetcher: network, index: DownloadIndex())

    /// A player that reads the track from the URL it is given, as the real one does.
    private final class ReadingPlayer: EpisodePlayer {
        let storage: FakeStorage
        var loaded: MediaDetail?
        var bytesRead: Data?
        init(storage: FakeStorage) { self.storage = storage }
        func load(_ detail: MediaDetail) async {
            loaded = detail
            bytesRead = storage.read(detail.tracks[0].url)
        }
    }

    func testDownloadedEpisodePlaysFromTheLocalFileWithTheNetworkOff() async {
        _ = await downloads.download(episodeDetail(), headers: [:])
        network.online = false
        let fetchesBefore = network.fetched.count
        let player = ReadingPlayer(storage: storage)
        let played = await downloads.playOffline("ep1", position: 250, player: player)
        XCTAssertTrue(played)
        XCTAssertEqual(player.loaded?.tracks[0].url.scheme, "fake")
        XCTAssertEqual(player.bytesRead, network.body)
        XCTAssertEqual(player.loaded?.position?.positionInTrack, 250)
        XCTAssertEqual(player.loaded?.assetID, "podcast:ep1")
        XCTAssertEqual(network.fetched.count, fetchesBefore, "no request was made")
    }

    func testNotDownloadedOrDeletedEpisodeDoesNotPlayOffline() async {
        let player = ReadingPlayer(storage: storage)
        var played = await downloads.playOffline("ep1", position: 0, player: player)
        XCTAssertFalse(played)
        _ = await downloads.download(episodeDetail(), headers: [:])
        downloads.delete("ep1")
        played = await downloads.playOffline("ep1", position: 0, player: player)
        XCTAssertFalse(played)
        XCTAssertNil(player.loaded)
    }
}

final class PodcastResumeAfterReconnectTests: XCTestCase {
    func testOfflinePositionIsWrittenToTheProgressEndpointOnceBackOnline() async throws {
        let server = makeServer(.bookdoe)
        let creds = InMemoryCredentialStore()
        try creds.save(Credential(accessToken: "alxd_tok"), for: server.id)
        var online = false
        let client = MockHTTPClient { _ in
            online ? .json(200, ["applied": true, "progress": ["position_seconds": 480.0]]) : nil
        }
        let provider = BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1.0")
        let queue = ProgressQueue()
        await PodcastOfflineProgress.record(queue: queue, serverID: server.id, episodeID: "ep1", position: 300, duration: 1000, at: Date(timeIntervalSince1970: 1))
        await PodcastOfflineProgress.record(queue: queue, serverID: server.id, episodeID: "ep1", position: 480, duration: 1000, at: Date(timeIntervalSince1970: 2))

        let offline = await queue.flush(serverID: server.id, provider: provider)
        XCTAssertEqual(offline.sent, 0)
        let kept = await queue.pending
        XCTAssertEqual(kept.count, 1, "kept for later")

        online = true
        let back = await queue.flush(serverID: server.id, provider: provider)
        XCTAssertEqual(back.sent, 1)
        let left = await queue.pending
        XCTAssertTrue(left.isEmpty)
        let put = try XCTUnwrap(client.last)
        XCTAssertEqual(put.method, .put)
        XCTAssertEqual(put.url.path, "/api/player/v1/podcasts/episodes/ep1/progress")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(put.body)) as? [String: Any])
        XCTAssertEqual(body["position_seconds"] as? Double, 480)
    }
}
