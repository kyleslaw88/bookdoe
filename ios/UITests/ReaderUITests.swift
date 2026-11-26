import XCTest

/// Drives the reader on a simulator against a small fixture EPUB (debug builds open it
/// straight from `ALX_READER_DEMO`). Run with:
///   xcodebuild test -project bookdoe.xcodeproj -scheme Bookdoe \
///     -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:BookdoeUITests CODE_SIGNING_ALLOWED=NO
final class ReaderUITests: XCTestCase {
    override func setUp() { continueAfterFailure = false }

    private func launch(turn: String = "curl") -> XCUIApplication {
        let app = XCUIApplication()
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("Fixtures/sample.epub")
        app.launchEnvironment["ALX_READER_DEMO"] = fixture.path
        app.launchArguments += ["-reader.turn", turn]
        app.launch()
        return app
    }

    private func tap(_ app: XCUIApplication, x: CGFloat, y: CGFloat = 0.5) {
        app.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)).tap()
    }

    /// Waits until the reader is in the given mode ("live" right away with the curl off, "curl" once the page pictures exist).
    private func waitForMode(_ app: XCUIApplication, _ mode: String, timeout: TimeInterval = 40) -> Bool {
        let label = app.staticTexts["reader.mode"]
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if label.exists, label.label == mode { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        return false
    }

    /// The bars start hidden; a tap in the middle shows them (the percent lives in the bottom bar).
    private func showBars(_ app: XCUIApplication) {
        Thread.sleep(forTimeInterval: 1.5)   // let the book open before tapping
        if !app.buttons["Done"].exists { tap(app, x: 0.5) }
        if !app.buttons["Done"].waitForExistence(timeout: 4) { tap(app, x: 0.5) }
        XCTAssertTrue(app.buttons["Done"].waitForExistence(timeout: 5), "the bars did not appear")
    }

    /// Every change of the percent label (and of the mode) over `seconds`, attached to the result.
    private func timeline(_ app: XCUIApplication, _ name: String, seconds: Double) {
        var lines: [String] = []
        var last = ""
        let start = Date()
        while Date().timeIntervalSince(start) < seconds {
            let now = "\(app.staticTexts["reader.percent"].exists ? app.staticTexts["reader.percent"].label : "-") / \(app.staticTexts["reader.mode"].exists ? app.staticTexts["reader.mode"].label : "-")"
            if now != last { lines.append(String(format: "%.1fs  %@", Date().timeIntervalSince(start), now)); last = now }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        let a = XCTAttachment(string: lines.joined(separator: "\n"))
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    /// The reader's percent label once it differs from `old`, or nil on timeout.
    private func percent(_ app: XCUIApplication, differentFrom old: String? = nil, timeout: TimeInterval = 25) -> String? {
        let label = app.staticTexts["reader.percent"]
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if label.exists, !label.label.isEmpty, label.label != old { return label.label }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        return nil
    }

    /// The reader's own running log (debug builds), kept with the result.
    private func attachLog(_ app: XCUIApplication, _ name: String = "reader-log") {
        let a = XCTAttachment(string: app.staticTexts["reader.debug"].exists ? app.staticTexts["reader.debug"].label : "(no log)")
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    private func attach(_ name: String) {
        let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    func testPageStartsWithTheBarsHiddenAndTextClearOfTheEdges() {
        let app = launch(turn: "none")
        XCTAssertTrue(waitForMode(app, "live"))
        sleep(3)
        XCTAssertFalse(app.buttons["Done"].exists, "the bars should start hidden")
        attach("page-bars-hidden")
        showBars(app)
        attach("page-bars-shown")
        tap(app, x: 0.5)
        let gone = NSPredicate(format: "exists == false")
        expectation(for: gone, evaluatedWith: app.buttons["Done"])
        waitForExpectations(timeout: 5)
    }

    func testSwipeLeftTurnsForwardAndSwipeRightTurnsBack() {
        let app = launch()
        XCTAssertTrue(waitForMode(app, "curl"), "the page pictures never became ready")
        showBars(app)
        guard let start = percent(app) else { return XCTFail("the reader never showed a position") }
        app.swipeLeft()
        timeline(app, "after-swipe-left", seconds: 5)
        guard let forward = percent(app, differentFrom: start) else { return XCTFail("swipe left did not turn the page (still \(start))") }
        XCTAssertTrue(waitForMode(app, "curl"))
        app.swipeRight()
        timeline(app, "after-swipe-right", seconds: 5)
        let a = XCTAttachment(string: app.staticTexts["reader.debug"].label)
        a.name = "reader-log"
        a.lifetime = .keepAlways
        add(a)
        XCTAssertEqual(percent(app, differentFrom: forward), start, "swipe right should go back to the first page")
    }

    func testSeveralTurnsInARowAllLand() {
        let app = launch()
        XCTAssertTrue(waitForMode(app, "curl"))
        showBars(app)
        guard var last = percent(app) else { return XCTFail("the reader never showed a position") }
        for n in 1...3 {
            app.swipeLeft()
            guard let next = percent(app, differentFrom: last) else { return XCTFail("turn \(n) did not happen (still \(last))") }
            last = next
            Thread.sleep(forTimeInterval: 2.5)
        }
    }

    func testTapOnTheRightEdgeTurnsForward() {
        let app = launch()
        XCTAssertTrue(waitForMode(app, "curl"))
        showBars(app)
        guard let start = percent(app) else { return XCTFail("no position") }
        tap(app, x: 0.93, y: 0.4)
        XCTAssertNotNil(percent(app, differentFrom: start), "tapping the right edge did not turn the page")
    }

    func testSwipeStillTurnsWithTheCurlAnimationOff() {
        let app = launch(turn: "none")
        XCTAssertTrue(waitForMode(app, "live"))
        showBars(app)
        guard let start = percent(app) else { return XCTFail("the reader never showed a position") }
        app.swipeLeft()
        XCTAssertNotNil(percent(app, differentFrom: start), "swipe left did not turn the page without the animation")
    }
    // MARK: bookmarks and highlights

    func testBookmarkingAPageAddsItToTheListAndCanBeUndone() {
        let app = launch(turn: "none")
        XCTAssertTrue(waitForMode(app, "live"))
        showBars(app)
        XCTAssertNotNil(percent(app), "the reader never showed a position")
        let add = app.buttons["Add bookmark"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        add.tap()
        let filled = app.buttons["Remove bookmark"].waitForExistence(timeout: 10)
        attachLog(app, "bookmark-log")
        XCTAssertTrue(filled, "the ribbon did not fill after bookmarking")
        app.buttons["reader.contents"].tap()
        app.segmentedControls.buttons["Bookmarks"].tap()
        XCTAssertTrue(app.staticTexts["Bookmarks"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["No bookmarks yet. Tap the ribbon at the top of a page."].waitForExistence(timeout: 2), "the bookmark is not in the list")
        attach("bookmark-list")
        app.navigationBars["Bookmarks"].buttons["Done"].tap()
        XCTAssertTrue(app.buttons["Remove bookmark"].waitForExistence(timeout: 5))
        app.buttons["Remove bookmark"].tap()
        XCTAssertTrue(app.buttons["Add bookmark"].waitForExistence(timeout: 10), "removing the bookmark did not empty the ribbon")
    }

    func testHighlightingWordsWithAColourAndANote() {
        let app = launch(turn: "none")
        XCTAssertTrue(waitForMode(app, "live"))
        showBars(app)
        let markup = app.buttons["reader.markup"]
        XCTAssertTrue(markup.waitForExistence(timeout: 10), "the highlighter never appeared (the memory source supports highlights)")
        markup.tap()
        sleep(2)
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.35, dy: 0.32)).press(forDuration: 1.3)
        let yellow = app.buttons["highlight.yellow"]
        let offered = yellow.waitForExistence(timeout: 12)
        attachLog(app, "highlight-log")
        XCTAssertTrue(offered, "selecting text did not offer the highlight colours")
        attach("selection-panel")
        yellow.tap()
        XCTAssertTrue(app.staticTexts["Select text to highlight it. Tap a highlight to change it."].waitForExistence(timeout: 8), "the panel did not go away after highlighting")
        sleep(1)
        attach("after-highlight")
        // a second passage, this time with a note
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.62, dy: 0.5)).press(forDuration: 1.3)
        let note = app.buttons["highlight.note"]
        XCTAssertTrue(note.waitForExistence(timeout: 12), "the second selection did not offer a note")
        note.tap()
        let editor = app.textViews["note.editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 8), "the note editor did not open")
        editor.tap()
        editor.typeText("remember this")
        app.buttons["Save"].tap()
        app.buttons["reader.contents"].tap()
        app.segmentedControls.buttons["Highlights"].tap()
        XCTAssertTrue(app.staticTexts["remember this"].waitForExistence(timeout: 8), "the note is not in the highlights list")
        XCTAssertFalse(app.staticTexts["No highlights yet. Tap the highlighter, then select text."].exists)
        attach("highlights-list")
    }

    func testLeavingMarkupModeBringsTheCurlBack() {
        let app = launch()
        XCTAssertTrue(waitForMode(app, "curl"))
        showBars(app)
        let markup = app.buttons["reader.markup"]
        XCTAssertTrue(markup.waitForExistence(timeout: 10))
        markup.tap()
        XCTAssertTrue(waitForMode(app, "live"), "markup mode should show the live page")
        markup.tap()
        XCTAssertTrue(waitForMode(app, "curl"), "turning markup off did not rebuild the page curl")
        guard let start = percent(app) else { return XCTFail("no position") }
        app.swipeLeft()
        XCTAssertNotNil(percent(app, differentFrom: start), "a swipe should turn the page again after markup")
    }
}
