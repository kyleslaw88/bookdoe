import XCTest
@testable import BookdoeKit

final class ItemDurationTests: XCTestCase {
    private func detail(trackDuration: Double?, itemDuration: Double? = nil) -> MediaDetail {
        let t = Track(id: "t", index: 0, title: "Ep", url: URL(string: "https://x/e.mp3")!, durationSeconds: trackDuration, startOffset: 0)
        return MediaDetail(item: MediaItem(id: "i", title: "Ep", kind: .audiobook, durationSeconds: itemDuration), tracks: [t], assetID: "i")
    }

    func testNilDurationTakesItemDuration() {
        let d = detail(trackDuration: nil)
        XCTAssertEqual(d.totalDuration, 0)
        XCTAssertEqual(d.learningDuration(600, ofTrack: 0).totalDuration, 600)
    }

    func testZeroDurationTakesItemDuration() {
        XCTAssertEqual(detail(trackDuration: 0, itemDuration: 0).learningDuration(600, ofTrack: 0).totalDuration, 600)
    }

    func testKnownDurationUnchanged() {
        let d = detail(trackDuration: 300, itemDuration: 300)
        XCTAssertEqual(d.learningDuration(600, ofTrack: 0), d)
        XCTAssertEqual(d.totalDuration, 300)
    }

    func testInvalidItemDurationIgnored() {
        let d = detail(trackDuration: nil)
        XCTAssertEqual(d.learningDuration(.nan, ofTrack: 0), d)
        XCTAssertEqual(d.learningDuration(0, ofTrack: 0), d)
    }

    func testSkipForwardUnknownDurationIsRelative() {
        XCTAssertEqual(detail(trackDuration: nil).skipTarget(from: 10, by: 30), 40)
    }

    func testSkipAfterLearningDuration() {
        XCTAssertEqual(detail(trackDuration: nil).learningDuration(600, ofTrack: 0).skipTarget(from: 10, by: 30), 40)
    }

    func testSkipBackUnknownDurationNeverBelowZeroNorToZero() {
        let d = detail(trackDuration: nil)
        XCTAssertEqual(d.skipTarget(from: 100, by: -15), 85)
        XCTAssertEqual(d.skipTarget(from: 5, by: -15), 0)
    }

    func testKnownDurationClampsNearEnd() {
        let d = detail(trackDuration: 100)
        XCTAssertEqual(d.skipTarget(from: 90, by: 30), 99)
        XCTAssertEqual(d.skipTarget(from: 10, by: 30), 40)
    }

    func testLaterTrackOffsetsShiftWhenTotalDerived() {
        let a = Track(id: "a", index: 0, title: "A", url: URL(string: "https://x/a")!, durationSeconds: nil, startOffset: 0)
        let b = Track(id: "b", index: 1, title: "B", url: URL(string: "https://x/b")!, durationSeconds: 50, startOffset: 0)
        let d = MediaDetail(item: MediaItem(id: "i", title: "T", kind: .audiobook), tracks: [a, b], assetID: "i").learningDuration(100, ofTrack: 0)
        XCTAssertEqual(d.tracks[1].startOffset, 100)
        XCTAssertEqual(d.totalDuration, 150)
    }
}
