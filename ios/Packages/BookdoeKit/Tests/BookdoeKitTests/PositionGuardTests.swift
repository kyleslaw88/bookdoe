import XCTest
@testable import BookdoeKit

final class PositionGuardTests: XCTestCase {
    func testResetToZeroIsSuspicious() {
        XCTAssertTrue(PositionGuard.isSuspiciousRegression(previous: 5000, new: 0))
        XCTAssertTrue(PositionGuard.isSuspiciousRegression(previous: 100, new: 2))
        XCTAssertTrue(PositionGuard.isSuspiciousRegression(previous: 100, new: .nan))
    }

    func testNormalProgressAndSmallRewindAreFine() {
        XCTAssertFalse(PositionGuard.isSuspiciousRegression(previous: 100, new: 101))
        XCTAssertFalse(PositionGuard.isSuspiciousRegression(previous: 100, new: 85))
        XCTAssertFalse(PositionGuard.isSuspiciousRegression(previous: 0, new: 0))
    }

    func testResolvePrefersNewerLocal() {
        let sid = UUID()
        let server = PlaybackPosition(absolutePosition: 100, updatedAt: Date(timeIntervalSince1970: 1000))
        let local = LocalPosition(serverID: sid, assetID: "a", position: PlaybackPosition(absolutePosition: 500), savedAt: Date(timeIntervalSince1970: 2000))
        XCTAssertEqual(LocalPosition.resolve(server: server, local: local)?.absolutePosition, 500)
    }

    func testResolveKeepsNewerServer() {
        let server = PlaybackPosition(absolutePosition: 900, updatedAt: Date(timeIntervalSince1970: 3000))
        let local = LocalPosition(serverID: UUID(), assetID: "a", position: PlaybackPosition(absolutePosition: 500), savedAt: Date(timeIntervalSince1970: 2000))
        XCTAssertEqual(LocalPosition.resolve(server: server, local: local)?.absolutePosition, 900)
    }

    func testResolveHandlesMissingSides() {
        let local = LocalPosition(serverID: UUID(), assetID: "a", position: PlaybackPosition(absolutePosition: 7))
        XCTAssertEqual(LocalPosition.resolve(server: nil, local: local)?.absolutePosition, 7)
        XCTAssertNil(LocalPosition.resolve(server: nil, local: nil))
        XCTAssertEqual(LocalPosition.resolve(server: PlaybackPosition(absolutePosition: 3), local: nil)?.absolutePosition, 3)
        XCTAssertEqual(LocalPosition.resolve(server: PlaybackPosition(absolutePosition: 3), local: local)?.absolutePosition, 7, "server without a timestamp loses")
    }

    func testStoreRoundTrip() {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("lp-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: file) }
        let sid = UUID()
        let store = LocalPositionStore(fileURL: file)
        store.save(LocalPosition(serverID: sid, assetID: "a", position: PlaybackPosition(absolutePosition: 1)))
        store.save(LocalPosition(serverID: sid, assetID: "a", position: PlaybackPosition(absolutePosition: 2)))
        let reloaded = LocalPositionStore(fileURL: file)
        XCTAssertEqual(reloaded.position(serverID: sid, assetID: "a")?.position.absolutePosition, 2)
        XCTAssertNil(reloaded.position(serverID: sid, assetID: "b"))
    }
}
