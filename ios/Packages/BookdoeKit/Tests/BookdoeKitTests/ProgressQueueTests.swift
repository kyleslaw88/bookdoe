import XCTest
@testable import BookdoeKit

/// A provider stub that records progress reports and answers from a script.
final class StubProvider: MediaProvider, @unchecked Sendable {
    let server: ServerRecord
    var kind: ServerKind { server.kind }
    var reports: [ProgressUpdate] = []
    var script: [(ProgressUpdate) throws -> ProgressReport] = []

    init(server: ServerRecord) { self.server = server }

    func probe() async throws -> ServerInfo { ServerInfo(name: "stub", supportsPasswordLogin: true) }
    func login(username: String, password: String, deviceName: String) async throws -> AuthResult { throw APIError.notSupported("stub") }
    func libraries() async throws -> [LibraryRef] { [] }
    func browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int) async throws -> LibraryPage { LibraryPage(items: [], total: 0, offset: 0, limit: limit) }
    func continueListening() async throws -> [MediaItem] { [] }
    func detail(id: String) async throws -> MediaDetail { throw APIError.notFound }
    func startPlayback(_ detail: MediaDetail) async throws -> PlaybackSession { PlaybackSession(sessionID: nil) }
    func reportProgress(_ update: ProgressUpdate, session: PlaybackSession?) async throws -> ProgressReport {
        reports.append(update)
        guard !script.isEmpty else { return ProgressReport(applied: true) }
        return try script.removeFirst()(update)
    }
    func stopPlayback(_ update: ProgressUpdate, session: PlaybackSession?) async throws {}
    func mediaHeaders() throws -> [String: String] { [:] }
    func logout() async throws {}
}

final class ProgressQueueTests: XCTestCase {
    let server = makeServer(.bookdoe)

    func update(_ asset: String, at: TimeInterval, listened: Double = 0) -> ProgressUpdate {
        ProgressUpdate(serverID: server.id, itemID: "w", assetID: asset, absolutePosition: at, clientUpdatedAt: Date(timeIntervalSince1970: at), listened: listened)
    }

    func testEnqueueCoalescesPerAssetAndSumsListened() async {
        let q = ProgressQueue()
        await q.enqueue(update("a", at: 1, listened: 10))
        await q.enqueue(update("a", at: 2, listened: 5))
        await q.enqueue(update("b", at: 3))
        let pending = await q.pending
        XCTAssertEqual(pending.count, 2)
        XCTAssertEqual(pending.first { $0.assetID == "a" }?.listened, 15)
        XCTAssertEqual(pending.first { $0.assetID == "a" }?.absolutePosition, 2)
    }

    func testFlushSendsInOrderAndReportsConflicts() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("b", at: 5))
        await q.enqueue(update("a", at: 1))
        p.script = [{ _ in ProgressReport(applied: false, serverPosition: PlaybackPosition(absolutePosition: 99)) }, { _ in ProgressReport(applied: true) }]
        let result = await q.flush(serverID: server.id, provider: p)
        XCTAssertEqual(p.reports.map(\.assetID), ["a", "b"], "oldest client timestamp first")
        XCTAssertEqual(result.sent, 2)
        XCTAssertEqual(result.conflicts.count, 1)
        XCTAssertEqual(result.conflicts.first?.1.absolutePosition, 99)
        let left = await q.pending
        XCTAssertTrue(left.isEmpty)
    }

    func testNetworkFailureKeepsItemsAndStopsTheBatch() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("a", at: 1))
        await q.enqueue(update("b", at: 2))
        p.script = [{ _ in throw APIError.network("offline") }]
        let result = await q.flush(serverID: server.id, provider: p)
        XCTAssertEqual(result.sent, 0)
        XCTAssertEqual(result.failed, 1)
        XCTAssertEqual(p.reports.count, 1)
        let left = await q.pending
        XCTAssertEqual(left.count, 2)
    }

    func testNotFoundDropsTheItem() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("gone", at: 1))
        p.script = [{ _ in throw APIError.notFound }]
        let result = await q.flush(serverID: server.id, provider: p)
        XCTAssertEqual(result.failed, 1)
        let left = await q.pending
        XCTAssertTrue(left.isEmpty)
    }

    func testPersistenceRoundTrip() async {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("pq-\(UUID().uuidString).json")
        let q = ProgressQueue(fileURL: file)
        await q.enqueue(update("a", at: 1))
        let reloaded = ProgressQueue(fileURL: file)
        let pending = await reloaded.pending
        XCTAssertEqual(pending.map(\.assetID), ["a"])
        try? FileManager.default.removeItem(at: file)
    }

    func testOtherServersAreUntouched() async {
        let q = ProgressQueue()
        let other = makeServer(.jellyfin)
        await q.enqueue(update("a", at: 1))
        await q.enqueue(ProgressUpdate(serverID: other.id, itemID: "x", assetID: "x"))
        _ = await q.flush(serverID: server.id, provider: StubProvider(server: server))
        let left = await q.pending
        XCTAssertEqual(left.map(\.serverID), [other.id])
    }
}

final class ProgressQueueAuthTests: XCTestCase {
    let server = makeServer(.bookdoe)

    func update(_ asset: String, at: TimeInterval) -> ProgressUpdate {
        ProgressUpdate(serverID: server.id, itemID: "w", assetID: asset, absolutePosition: at, clientUpdatedAt: Date(timeIntervalSince1970: at))
    }

    func testUnauthorizedKeepsUpdatesAndFlagsAuthRequired() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("a", at: 1))
        await q.enqueue(update("b", at: 2))
        p.script = [{ _ in throw APIError.unauthorized }]
        let result = await q.flush(serverID: server.id, provider: p)
        XCTAssertTrue(result.authRequired)
        XCTAssertEqual(result.sent, 0)
        XCTAssertEqual(p.reports.count, 1, "stops after the first auth failure")
        let left = await q.pending
        XCTAssertEqual(left.count, 2)
    }

    func testForbiddenKeepsUpdates() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("a", at: 1))
        p.script = [{ _ in throw APIError.forbidden(nil) }]
        let result = await q.flush(serverID: server.id, provider: p)
        XCTAssertTrue(result.authRequired)
        let left = await q.pending
        XCTAssertEqual(left.count, 1)
    }

    func testFlushSucceedsAfterReauth() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("a", at: 1))
        p.script = [{ _ in throw APIError.unauthorized }]
        _ = await q.flush(serverID: server.id, provider: p)
        let retry = await q.flush(serverID: server.id, provider: p)
        XCTAssertFalse(retry.authRequired)
        XCTAssertEqual(retry.sent, 1)
        let left = await q.pending
        XCTAssertTrue(left.isEmpty)
    }

    func testNetworkFailureDoesNotFlagAuth() async {
        let q = ProgressQueue()
        let p = StubProvider(server: server)
        await q.enqueue(update("a", at: 1))
        p.script = [{ _ in throw APIError.network("offline") }]
        let result = await q.flush(serverID: server.id, provider: p)
        XCTAssertFalse(result.authRequired)
    }
}
