import XCTest
@testable import BookdoeKit

final class MarkFinishedTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: BookdoeProvider!
    let server = makeServer(.bookdoe)

    override func setUp() {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        try? creds.save(Credential(accessToken: "alxd_t"), for: server.id)
        provider = BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1")
    }

    private func audiobook() -> MediaDetail {
        let item = MediaItem(id: "w1", title: "Book", kind: .audiobook)
        let tracks = [Track(id: "f1", index: 0, title: "1", url: URL(string: "https://x/1")!, durationSeconds: 100, startOffset: 0),
                      Track(id: "f2", index: 1, title: "2", url: URL(string: "https://x/2")!, durationSeconds: 50, startOffset: 100)]
        return MediaDetail(item: item, tracks: tracks, assetID: "a1")
    }

    func testMarkUnreadDeletesTheProgressRow() async throws {
        client.enqueueJSON(200, ["asset_id": "a1", "progress": NSNull()])
        try await provider.setFinished(false, assetID: "a1", detail: nil)
        XCTAssertEqual(client.last?.method, .delete)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/progress/a1")
        XCTAssertEqual(client.last?.headers["Authorization"], "Bearer alxd_t")
    }

    func testEpisodePlayedPostsToThePodcastRoute() async throws {
        client.enqueueJSON(200, ["position_seconds": 5.0, "played": true])
        try await provider.setFinished(true, assetID: "podcast:ep1", detail: nil)
        XCTAssertEqual(client.last?.method, .post)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts/episodes/ep1/played")
        XCTAssertEqual(client.last?.headers["Authorization"], "Bearer alxd_t")
    }

    func testEpisodeUnplayedPostsToThePodcastRoute() async throws {
        client.enqueueJSON(200, ["position_seconds": 0.0, "played": false])
        try await provider.setFinished(false, assetID: "podcast:ep1", detail: nil)
        XCTAssertEqual(client.last?.method, .post)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts/episodes/ep1/unplayed")
    }

    func testBookStillUsesTheProgressRoute() async throws {
        client.enqueueJSON(200, ["asset_id": "a1", "applied": true])
        try await provider.setFinished(true, assetID: "a1", detail: nil)
        XCTAssertEqual(client.last?.method, .put)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/progress/a1")
    }

    func testMarkUnreadOnAnOlderServerExplainsItself() async {
        client.enqueueJSON(404, ["detail": "Not Found"])
        do { try await provider.setFinished(false, assetID: "a1", detail: nil); XCTFail() } catch {
            if case .notSupported(let m) = error as? APIError { XCTAssertTrue(m.contains("newer bookdoe server")) } else { XCTFail("\(error)") }
        }
    }

    func testMarkReadWithoutDetailStillSavesFull() async throws {
        client.enqueueJSON(200, ["asset_id": "a9", "applied": true])
        try await provider.setFinished(true, assetID: "a9", detail: nil)
        XCTAssertEqual(client.last?.bodyJSON?["percent"] as? Double, 1.0)
        XCTAssertNil(client.last?.bodyJSON?["file_id"])
    }

    func testMarkReadSavesFullProgressOnTheLastFile() async throws {
        client.enqueueJSON(200, ["asset_id": "a1", "applied": true, "percent": 1.0])
        try await provider.setFinished(true, assetID: "a1", detail: audiobook())
        XCTAssertEqual(client.last?.method, .put)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/progress/a1")
        let body = client.last?.bodyJSON
        XCTAssertEqual(body?["percent"] as? Double, 1.0)
        XCTAssertEqual(body?["file_id"] as? String, "f2")
        XCTAssertEqual(body?["position"] as? Double, 50)
        XCTAssertEqual(body?["asset_id"] as? String, "a1")
    }

    func testMarkReadOnAnEbookKeepsTheLocation() async throws {
        client.enqueueJSON(200, ["asset_id": "e1", "applied": true])
        var d = MediaDetail(item: MediaItem(id: "w2", title: "Epub", kind: .ebook), assetID: "e1")
        d.position = PlaybackPosition(percent: 0.4, location: "epubcfi(/6/4)")
        try await provider.setFinished(true, assetID: "e1", detail: d)
        let body = client.last?.bodyJSON
        XCTAssertEqual(body?["location"] as? String, "epubcfi(/6/4)")
        XCTAssertNil(body?["file_id"])
    }

    func testAStaleAnswerIsAnError() async {
        client.enqueueJSON(200, ["asset_id": "a1", "applied": false])
        do { try await provider.setFinished(true, assetID: "a1", detail: audiobook()); XCTFail() } catch { XCTAssertNotNil(error as? APIError) }
    }

    func testQueueRemoveDropsOnlyThatBook() async {
        let q = ProgressQueue()
        await q.enqueue(ProgressUpdate(serverID: server.id, itemID: "w1", assetID: "a1"))
        await q.enqueue(ProgressUpdate(serverID: server.id, itemID: "w2", assetID: "a2"))
        await q.remove(serverID: server.id, assetID: "a1")
        let left = await q.pending.map(\.assetID)
        XCTAssertEqual(left, ["a2"])
    }

    func testOtherProvidersSayNotSupported() async {
        let jf = JellyfinProvider(server: makeServer(.jellyfin), client: client, credentials: creds, appVersion: "1")
        do { try await jf.setFinished(true, assetID: "a1", detail: audiobook()); XCTFail() } catch {
            if case .notSupported = error as? APIError {} else { XCTFail("\(error)") }
        }
    }
}
