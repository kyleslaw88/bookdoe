import XCTest
@testable import BookdoeKit

final class StoreAndModelTests: XCTestCase {
    func testServerStorePersistsWithoutCredentials() throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("servers-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: file) }
        let store = ServerStore(fileURL: file)
        let a = makeServer(.bookdoe)
        let b = makeServer(.jellyfin)
        store.add(a)
        store.add(b)
        XCTAssertEqual(store.active?.id, a.id)
        store.setActive(id: b.id)
        var renamed = b; renamed.name = "Living room"
        store.update(renamed)
        let reloaded = ServerStore(fileURL: file)
        XCTAssertEqual(reloaded.servers.map(\.name), ["Test", "Living room"])
        XCTAssertEqual(reloaded.activeServerID, b.id)
        reloaded.remove(id: b.id)
        XCTAssertEqual(reloaded.active?.id, a.id)
        let raw = try String(contentsOf: file, encoding: .utf8)
        XCTAssertFalse(raw.contains("accessToken"))
    }

    func testInMemoryCredentialStore() throws {
        let store = InMemoryCredentialStore()
        let id = UUID()
        XCTAssertNil(try store.credential(for: id))
        try store.save(Credential(accessToken: "x", refreshToken: "y"), for: id)
        XCTAssertEqual(try store.credential(for: id)?.refreshToken, "y")
        try store.remove(for: id)
        XCTAssertNil(try store.credential(for: id))
    }

    func testURLAppendingHandlesBasePathsAndQuery() {
        let base = URL(string: "https://host.example.com/jellyfin/")!
        let u = base.appending(path: "/Items", query: ["userId": "u", "limit": "5", "skip": nil])
        XCTAssertEqual(u.path, "/jellyfin/Items")
        XCTAssertEqual(u.queryDict, ["userId": "u", "limit": "5"])
        XCTAssertEqual(URL(string: "https://h.example.com")!.appending(path: "api/x").absoluteString, "https://h.example.com/api/x")
        XCTAssertEqual(base.resolving("/public/session/1/track/0")?.absoluteString, "https://host.example.com/jellyfin/public/session/1/track/0")
        XCTAssertEqual(base.resolving("https://cdn.example.com/a.mp3")?.absoluteString, "https://cdn.example.com/a.mp3")
    }

    func testDownloadPlanNamesFilesSafely() {
        let server = makeServer(.bookdoe)
        let tracks = [Track(id: "f/1", index: 0, title: "One", url: URL(string: "https://x/api/v1/library/w/files/f1")!, durationSeconds: 10, mimeType: "audio/mp4", sizeBytes: 100),
                      Track(id: "f2", index: 1, title: "Two", url: URL(string: "https://x/a/b.mp3")!, durationSeconds: 20, sizeBytes: 50)]
        let detail = MediaDetail(item: MediaItem(id: "work 1", title: "T", kind: .audiobook), tracks: tracks, assetID: "as", ebookDownloadURL: URL(string: "https://x/f/e"), ebookFormat: "epub")
        let plan = DownloadPlan(serverID: server.id, detail: detail, headers: ["Authorization": "Bearer t"])
        XCTAssertEqual(plan.files.count, 3)
        XCTAssertEqual(plan.files[0].localRelativePath, "\(server.id.uuidString)/work_1/000-f_1.m4b")
        XCTAssertEqual(plan.files[1].localRelativePath, "\(server.id.uuidString)/work_1/001-f2.mp3")
        XCTAssertEqual(plan.files[2].localRelativePath, "\(server.id.uuidString)/work_1/book.epub")
        XCTAssertEqual(plan.totalExpectedBytes, 150)
        XCTAssertEqual(plan.files[0].headers["Authorization"], "Bearer t")
    }

    func testDownloadIndexRoundTripAndOfflineDetail() {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("dl-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: file) }
        let server = makeServer(.audiobookshelf)
        let detail = MediaDetail(item: MediaItem(id: "li1", title: "Dune", authors: ["F"], kind: .audiobook, durationSeconds: 30), chapters: [Chapter(title: "c", start: 0)], assetID: "li1")
        let item = DownloadedItem(serverID: server.id, detail: detail, tracks: [DownloadedTrack(trackID: "a", title: "A", relativePath: "s/li1/000-a.mp3", durationSeconds: 10),
                                                                                  DownloadedTrack(trackID: "b", title: "B", relativePath: "s/li1/001-b.mp3", durationSeconds: 20)], bytes: 99)
        let index = DownloadIndex(fileURL: file)
        index.upsert(item)
        let reloaded = DownloadIndex(fileURL: file)
        XCTAssertEqual(reloaded.item(serverID: server.id, itemID: "li1")?.bytes, 99)
        let offline = reloaded.items[0].detail(root: URL(fileURLWithPath: "/tmp/dl"))
        XCTAssertEqual(offline.tracks.count, 2)
        XCTAssertEqual(offline.tracks[1].startOffset, 10)
        XCTAssertEqual(offline.tracks[1].url.path, "/tmp/dl/s/li1/001-b.mp3")
        XCTAssertEqual(offline.chapters.count, 1)
        XCTAssertEqual(offline.assetID, "li1")
        XCTAssertNotNil(reloaded.remove(serverID: server.id, itemID: "li1"))
        XCTAssertNil(reloaded.item(serverID: server.id, itemID: "li1"))
    }

    func testLibraryPageNextOffset() {
        XCTAssertEqual(LibraryPage(items: [MediaItem(id: "1", title: "a", kind: .ebook)], total: 3, offset: 0, limit: 1).nextOffset, 1)
        XCTAssertNil(LibraryPage(items: [MediaItem(id: "3", title: "c", kind: .ebook)], total: 3, offset: 2, limit: 1).nextOffset)
        XCTAssertNil(LibraryPage(items: [], total: 3, offset: 0, limit: 1).nextOffset)
    }

    func testAPIErrorFromResponse() {
        XCTAssertEqual(APIError.from(HTTPResponse(status: 401)), .unauthorized)
        XCTAssertEqual(APIError.from(.json(403, ["detail": "no"])), .forbidden("no"))
        XCTAssertEqual(APIError.from(.json(500, ["message": "boom"])), .server(status: 500, message: "boom"))
        XCTAssertEqual(APIError.from(HTTPResponse(status: 429, headers: ["retry-after": "7"])), .rateLimited(retryAfter: 7))
    }

    func testProviderFactoryPicksByKind() {
        let f = ProviderFactory(client: MockHTTPClient(), credentials: InMemoryCredentialStore())
        XCTAssertEqual(f.make(makeServer(.bookdoe)).kind, .bookdoe)
        XCTAssertEqual(f.make(makeServer(.jellyfin)).kind, .jellyfin)
        XCTAssertEqual(f.make(makeServer(.audiobookshelf)).kind, .audiobookshelf)
    }
}

final class CredentialStoreErrorTests: XCTestCase {
    func testKeychainErrorIsReadable() {
        let text = CredentialStoreError.keychain(status: -34018).errorDescription ?? ""
        XCTAssertTrue(text.contains("-34018"))
        XCTAssertTrue(text.contains("Keychain"))
    }
}
