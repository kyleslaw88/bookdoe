import XCTest
@testable import BookdoeKit

final class BookdoeBookmarkTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: BookdoeProvider!
    let server = makeServer(.bookdoe)
    let book = BookRef(itemID: "w1", assetID: "a1")

    override func setUp() {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        try? creds.save(Credential(accessToken: "alxd_t"), for: server.id)
        provider = BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1")
    }

    private func row(_ id: String, kind: String = "bookmark", extra: [String: Any] = [:]) -> [String: Any] {
        var o: [String: Any] = ["id": id, "asset_id": "a1", "work_id": "w1", "created_at": 1_790_000_000.0, "kind": kind]
        for (k, v) in extra { o[k] = v }
        return o
    }

    func testListReadsEveryPageAndTheNewFields() async throws {
        client.enqueueJSON(200, ["items": [row("b1", extra: ["location": "epubcfi(/6/4)", "title": "Chapter 2"]),
                                           row("h1", kind: "highlight", extra: ["location": "epubcfi(/6/4,/1:1,/1:9)", "title": "the words", "note": "n", "color": "yellow", "updated_at": 1_790_000_100.0])],
                                 "total": 3, "limit": 200, "offset": 0])
        client.enqueueJSON(200, ["items": [row("b2", extra: ["position": 61.5])], "total": 3, "limit": 200, "offset": 2])
        let all = try await provider.bookmarks(for: book)
        XCTAssertEqual(all.map(\.id), ["b1", "h1", "b2"])
        XCTAssertEqual(all[1].kind, .highlight)
        XCTAssertEqual(all[1].color, "yellow")
        XCTAssertEqual(all[1].note, "n")
        XCTAssertEqual(all[1].updatedAt, Date(timeIntervalSince1970: 1_790_000_100))
        XCTAssertEqual(all[0].updatedAt, all[0].createdAt)
        XCTAssertEqual(all[2].position, 61.5)
        XCTAssertEqual(client.requests[0].url.queryDict["asset_id"], "a1")
        XCTAssertEqual(client.requests[1].url.queryDict["offset"], "2")
    }

    func testAPlainBookmarkSendsNoKindOrColorSoOlderServersAcceptIt() async throws {
        client.enqueueJSON(201, row("b1", extra: ["position": 3723.0, "title": "Chapter 18"]))
        let made = try await provider.addBookmark(NewBookmark(position: 3723, percent: 0.06, title: "Chapter 18", color: "yellow"), to: book)
        XCTAssertEqual(made.id, "b1")
        let body = client.last?.bodyJSON
        XCTAssertEqual(client.last?.method, .post)
        XCTAssertEqual(body?["asset_id"] as? String, "a1")
        XCTAssertEqual(body?["position"] as? Double, 3723)
        XCTAssertNil(body?["kind"])
        XCTAssertNil(body?["color"])
    }

    func testAHighlightSendsKindColorAndTheRange() async throws {
        client.enqueueJSON(201, row("h1", kind: "highlight", extra: ["location": "epubcfi(/6/4,/1:1,/1:9)", "color": "green"]))
        let made = try await provider.addBookmark(NewBookmark(kind: .highlight, location: "epubcfi(/6/4,/1:1,/1:9)", percent: 0.3, title: String(repeating: "x", count: 500), note: "n", color: "green"), to: book)
        XCTAssertEqual(made.kind, .highlight)
        let body = client.last?.bodyJSON
        XCTAssertEqual(body?["kind"] as? String, "highlight")
        XCTAssertEqual(body?["color"] as? String, "green")
        XCTAssertEqual((body?["title"] as? String)?.count, 200, "the server's 200 character title limit")
    }

    func testPatchSendsOnlyWhatChangedAndNullToClear() async throws {
        client.enqueueJSON(200, row("h1", kind: "highlight", extra: ["note": "new", "color": "blue"]))
        let old = Bookmark(id: "h1", assetID: "a1", kind: .highlight, location: "x")
        _ = try await provider.updateBookmark(old, change: BookmarkChange(note: .set("new"), color: .set(nil)), in: book)
        XCTAssertEqual(client.last?.method, .patch)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/bookmarks/h1")
        let body = client.last?.bodyJSON
        XCTAssertEqual(body?["note"] as? String, "new")
        XCTAssertTrue(body?["color"] is NSNull)
        XCTAssertNil(body?["title"])
        let before = client.requests.count
        let same = try await provider.updateBookmark(old, change: BookmarkChange(), in: book)
        XCTAssertEqual(same, old)
        XCTAssertEqual(client.requests.count, before, "an empty change sends nothing")
    }

    func testDelete() async throws {
        client.enqueueJSON(200, ["ok": true])
        try await provider.deleteBookmark(Bookmark(id: "b9", assetID: "a1"), in: book)
        XCTAssertEqual(client.last?.method, .delete)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/bookmarks/b9")
    }

    func testHighlightSupportComesFromCapabilitiesAndIsRemembered() async {
        client.enqueueJSON(200, ["features": ["bookmarks", "highlights"]])
        let first = await provider.supportsHighlights()
        XCTAssertTrue(first)
        let second = await provider.supportsHighlights()
        XCTAssertTrue(second)
        XCTAssertEqual(client.requests.count, 1)
        let old = BookdoeProvider(server: server, client: MockHTTPClient { _ in .json(200, ["features": ["bookmarks"]]) }, credentials: creds, appVersion: "1")
        let oldOn = await old.supportsHighlights()
        XCTAssertFalse(oldOn)
    }
}

final class AudiobookshelfBookmarkTests: XCTestCase {
    var client: MockHTTPClient!
    var provider: AudiobookshelfProvider!
    let server = makeServer(.audiobookshelf, base: "https://abs.example.com")
    let book = BookRef(itemID: "li_1", assetID: "li_1")

    override func setUp() {
        client = MockHTTPClient()
        let creds = InMemoryCredentialStore()
        try? creds.save(Credential(accessToken: "tok"), for: server.id)
        provider = AudiobookshelfProvider(server: server, client: client, credentials: creds, appVersion: "1")
    }

    func testListIsSortedByTime() async throws {
        client.enqueueJSON(200, ["bookmarks": [["libraryItemId": "li_1", "title": "Late", "time": 900.5, "createdAt": 1_790_000_000_000.0],
                                               ["libraryItemId": "li_1", "title": "Early", "time": 12, "createdAt": 1_790_000_000_000.0]]])
        let items = try await provider.bookmarks(for: book)
        XCTAssertEqual(items.map { $0.title }, ["Early", "Late"])
        XCTAssertEqual(items[1].position, 900.5)
        XCTAssertEqual(items[0].createdAt, Date(timeIntervalSince1970: 1_790_000_000))
        XCTAssertEqual(client.last?.url.path, "/api/me/bookmarks/li_1")
        XCTAssertEqual(client.last?.headers["Authorization"], "Bearer tok")
    }

    func testAddRenameDelete() async throws {
        client.enqueueJSON(200, ["libraryItemId": "li_1", "title": "Chapter 3", "time": 321.0, "createdAt": 1_790_000_000_000.0])
        let made = try await provider.addBookmark(NewBookmark(position: 321, title: "Chapter 3"), to: book)
        XCTAssertEqual(client.last?.url.path, "/api/me/item/li_1/bookmark")
        XCTAssertEqual(client.last?.bodyJSON?["time"] as? Double, 321)
        XCTAssertEqual(made.position, 321)
        client.enqueueJSON(200, ["libraryItemId": "li_1", "title": "Renamed", "time": 321.0, "createdAt": 1_790_000_000_000.0])
        let renamed = try await provider.updateBookmark(made, change: BookmarkChange(title: .set("Renamed")), in: book)
        XCTAssertEqual(client.last?.method, .patch)
        XCTAssertEqual(renamed.title, "Renamed")
        client.enqueueJSON(200, [:])
        try await provider.deleteBookmark(renamed, in: book)
        XCTAssertEqual(client.last?.method, .delete)
        XCTAssertEqual(client.last?.url.path, "/api/me/item/li_1/bookmark/321.0")
    }

    func testEbookStyleBookmarksAreRefused() async {
        do { _ = try await provider.addBookmark(NewBookmark(kind: .highlight, location: "x"), to: book); XCTFail() } catch {
            if case .notSupported = error as? APIError {} else { XCTFail("\(error)") }
        }
        let on = await provider.supportsHighlights()
        XCTAssertFalse(on)
    }
}
