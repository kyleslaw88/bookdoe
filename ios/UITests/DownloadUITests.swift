import XCTest

/// Needs a slow large file to download. Run with TEST_RUNNER_ALX_TEST_DOWNLOAD_URL=http://127.0.0.1:PORT/big.bin set
/// (a throttled file server is easy: see ios/scripts/slow-file-server.py); without it the test is skipped.
final class DownloadUITests: XCTestCase {
    func testProgressMovesWhileTheScreenStaysOpen() throws {
        guard let url = ProcessInfo.processInfo.environment["ALX_TEST_DOWNLOAD_URL"] else { throw XCTSkip("no ALX_TEST_DOWNLOAD_URL") }
        let app = XCUIApplication()
        app.launchEnvironment["ALX_DOWNLOAD_DEMO"] = url
        app.launch()
        let start = app.buttons["download.start"]
        XCTAssertTrue(start.waitForExistence(timeout: 15))
        start.tap()
        let summary = app.staticTexts["download.summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 20), "no progress text appeared after tapping Download")
        var seen: [String] = []
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline {
            if summary.exists, let last = seen.last, summary.label != last { seen.append(summary.label) }
            else if summary.exists, seen.isEmpty { seen.append(summary.label) }
            if seen.count >= 4 { break }
            RunLoop.current.run(until: Date().addingTimeInterval(0.4))
        }
        let a = XCTAttachment(string: "labels seen without leaving the screen:\n" + seen.joined(separator: "\n") + "\nold-style parent says: " + app.staticTexts["download.old.summary"].label)
        a.name = "progress-labels"; a.lifetime = .keepAlways; add(a)
        XCTAssertGreaterThanOrEqual(seen.count, 3, "progress stayed put on the screen: \(seen)")
    }
}
