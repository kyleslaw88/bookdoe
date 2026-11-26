import XCTest
@testable import BookdoeKit

final class PlaybackSpeedTests: XCTestCase {
    func testCycleAdvancesThroughOptions() {
        XCTAssertEqual(PlaybackSpeed.next(after: 0.75), 1.0)
        XCTAssertEqual(PlaybackSpeed.next(after: 1.0), 1.25)
        XCTAssertEqual(PlaybackSpeed.next(after: 1.25), 1.5)
        XCTAssertEqual(PlaybackSpeed.next(after: 1.5), 1.75)
        XCTAssertEqual(PlaybackSpeed.next(after: 1.75), 2.0)
    }

    func testCycleWrapsToNormal() {
        XCTAssertEqual(PlaybackSpeed.next(after: 2.0), 1.0)
        XCTAssertEqual(PlaybackSpeed.next(after: 3.0), 1.0)
    }

    func testOddSavedRateMovesToNextOption() {
        XCTAssertEqual(PlaybackSpeed.next(after: 1.1), 1.25)
    }

    func testLabel() {
        XCTAssertEqual(PlaybackSpeed.label(for: 1.25), "1.25x")
    }

    func testNowPlayingRatesWhilePlaying() {
        let r = PlaybackSpeed.nowPlayingRates(rate: 1.5, isPlaying: true)
        XCTAssertEqual(r.playback, 1.5)
        XCTAssertEqual(r.defaultRate, 1.5)
    }

    func testNowPlayingRatesWhilePaused() {
        let r = PlaybackSpeed.nowPlayingRates(rate: 1.5, isPlaying: false)
        XCTAssertEqual(r.playback, 0)
        XCTAssertEqual(r.defaultRate, 1.5)
    }
}
