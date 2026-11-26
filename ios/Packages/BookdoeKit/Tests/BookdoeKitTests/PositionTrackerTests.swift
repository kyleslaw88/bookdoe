import XCTest
@testable import BookdoeKit

final class PositionTrackerTests: XCTestCase {
    func testStalePreSeekTickAfterBackwardSeekIsIgnored() {
        var t = PositionTracker(position: 1200)
        let token = t.beginSeek(to: 600)
        XCTAssertEqual(t.tick(1201), .ignore)
        XCTAssertEqual(t.position, 600)
        t.finishSeek(token: token, finished: true, playerPosition: 600.1)
        XCTAssertEqual(t.position, 600.1, accuracy: 0.001)
    }

    func testPostSeekTicksAreAccepted() {
        var t = PositionTracker(position: 1200)
        let token = t.beginSeek(to: 600)
        _ = t.tick(1201)
        t.finishSeek(token: token, finished: true, playerPosition: 600)
        XCTAssertEqual(t.tick(601), .accept)
        XCTAssertEqual(t.tick(602), .accept)
        XCTAssertEqual(t.position, 602)
        XCTAssertNil(t.userTarget)
    }

    func testSupersededSeekCompletionIsIgnored() {
        var t = PositionTracker(position: 100)
        let first = t.beginSeek(to: 50)
        let second = t.beginSeek(to: 300)
        t.finishSeek(token: first, finished: false, playerPosition: 50)
        XCTAssertTrue(t.isSeeking)
        t.finishSeek(token: second, finished: true, playerPosition: 300)
        XCTAssertFalse(t.isSeeking)
        XCTAssertEqual(t.position, 300)
    }

    func testSingleNearZeroTickIsIgnored() {
        var t = PositionTracker(position: 5000)
        XCTAssertEqual(t.tick(0), .ignore)
        XCTAssertEqual(t.position, 5000)
        XCTAssertEqual(t.tick(5001), .accept)
    }

    func testNonFiniteTickIsIgnored() {
        var t = PositionTracker(position: 100)
        XCTAssertEqual(t.tick(.nan), .ignore)
        XCTAssertEqual(t.tick(.nan), .ignore)
        XCTAssertEqual(t.position, 100)
    }

    func testTwoConsistentRegressedTicksAreAccepted() {
        var t = PositionTracker(position: 1200)
        XCTAssertEqual(t.tick(600), .ignore)
        XCTAssertEqual(t.tick(601), .accept)
        XCTAssertEqual(t.position, 601)
        XCTAssertEqual(t.tick(602), .accept)
    }

    func testInconsistentRegressedTicksStayIgnored() {
        var t = PositionTracker(position: 1200)
        XCTAssertEqual(t.tick(600), .ignore)
        XCTAssertEqual(t.tick(100), .ignore)
        XCTAssertEqual(t.position, 1200)
    }

    func testRecoveryUsesSeekTarget() {
        var t = PositionTracker(position: 1200)
        t.beginSeek(to: 600)
        XCTAssertEqual(t.recoveryPosition, 600)
        let token = t.pendingSeek!
        t.finishSeek(token: token, finished: true, playerPosition: 600)
        XCTAssertEqual(t.recoveryPosition, 600)
        _ = t.tick(605)
        XCTAssertEqual(t.recoveryPosition, 605)
    }
}
