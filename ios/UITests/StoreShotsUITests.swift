import XCTest

/// Signs in to the demo server and attaches App Store screenshots. Skipped without ALX_SHOTS_SECRET
/// (pass it as TEST_RUNNER_ALX_SHOTS_SECRET; optional TEST_RUNNER_ALX_SHOTS_URL and TEST_RUNNER_ALX_SHOTS_USER).
final class StoreShotsUITests: XCTestCase {
    func testStoreShots() throws {
        let secret = try DemoSignIn.secret()
        let app = XCUIApplication()
        app.launch()
        func shot(_ n: String) {
            let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot()); a.name = n; a.lifetime = .keepAlways; add(a)
        }
        DemoSignIn.run(app, secret: secret)
        sleep(12); shot("01-library")
        if UIDevice.current.userInterfaceIdiom == .pad {
            try ipadShots(app, shot: shot)
            return
        }
        for (i, tab) in app.tabBars.buttons.allElementsBoundByIndex.enumerated() {
            tab.tap(); sleep(4); shot(String(format: "%02d-tab-%@", i + 2, tab.label))
        }
    }

    /// Waits until every cover on screen is a loaded image, so listing shots never show grey placeholders.
    private func waitForCovers(_ app: XCUIApplication, timeout: TimeInterval = 30) {
        let loaded = app.images.matching(identifier: "cover.loaded").firstMatch
        XCTAssertTrue(loaded.waitForExistence(timeout: timeout), "no cover finished loading")
        let placeholders = app.images.matching(identifier: "cover.placeholder")
        let end = Date().addingTimeInterval(timeout)
        while placeholders.count > 0 && Date() < end { sleep(1) }
        XCTAssertEqual(placeholders.count, 0, "covers still show placeholders; the listing shots must show real covers")
        sleep(1)
    }

    private func openSection(_ app: XCUIApplication, _ name: String) {
        let row = app.descendants(matching: .any)["sidebar.\(name.lowercased())"].firstMatch
        // A pushed detail page hides the sidebar: go back until the row shows.
        var backs = 0
        while !row.exists && backs < 4 {
            let back = app.navigationBars.buttons.element(boundBy: 0)
            guard back.exists else { break }
            back.tap(); sleep(1); backs += 1
        }
        XCTAssertTrue(row.waitForExistence(timeout: 10), "sidebar row \(name)")
        row.tap(); sleep(3)
    }

    /// Opens library books until one offers `labels`; returns with that book's detail page showing.
    private func openBook(_ app: XCUIApplication, offering labels: [String]) -> XCUIElement? {
        openSection(app, "Library")
        waitForCovers(app)
        let count = min(app.scrollViews.descendants(matching: .button).count, 12)
        for i in 0..<count {
            let cover = app.scrollViews.descendants(matching: .button).element(boundBy: i)
            guard cover.exists else { continue }
            cover.tap(); sleep(3)
            for l in labels {
                let b = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", l)).firstMatch
                if b.waitForExistence(timeout: 5) { return b }
            }
            app.navigationBars.buttons.element(boundBy: 0).tap(); sleep(1)
        }
        return nil
    }

    /// The regular-width detail page: the actions sit in a bounded column to the right of the cover, not full width.
    private func assertRegularDetailLayout(_ app: XCUIApplication, action: XCUIElement) {
        let column = app.descendants(matching: .any)["detail.column"].firstMatch
        XCTAssertTrue(column.waitForExistence(timeout: 10), "regular-width detail must use the cover-left column layout")
        let cover = app.images.matching(NSPredicate(format: "identifier BEGINSWITH 'cover.'")).firstMatch
        XCTAssertTrue(cover.exists)
        XCTAssertLessThanOrEqual(action.frame.width, 380, "the primary button must not be stretched full width")
        XCTAssertLessThanOrEqual(column.frame.width, 580, "the metadata column must stay readable")
        XCTAssertLessThan(cover.frame.maxX, column.frame.minX + 1, "the cover must sit left of the column")
    }

    private func ipadShots(_ app: XCUIApplication, shot: (String) -> Void) throws {
        // Regular width: a sidebar replaces the tab bar. 13" iPad is 2064x2752 portrait.
        XCTAssertFalse(app.tabBars.firstMatch.exists, "iPad must not show the stretched iPhone tab bar")
        XCUIDevice.shared.orientation = .portrait
        openSection(app, "Home"); sleep(5); shot("02-ipad-home")
        openSection(app, "Library"); waitForCovers(app); shot("03-ipad-library")
        openSection(app, "Podcasts"); sleep(4); shot("04-ipad-podcasts")
        if app.descendants(matching: .any)["sidebar.discover"].firstMatch.exists { openSection(app, "Discover"); sleep(3); shot("05-ipad-discover") }
        openSection(app, "Settings"); sleep(3); shot("06-ipad-settings")

        // Audiobook: detail page, then the player. Fail if the shot is only a cover.
        if let play = openBook(app, offering: ["Play", "Resume"]) {
            waitForCovers(app); assertRegularDetailLayout(app, action: play); shot("07-ipad-audiobook-detail")
            play.tap()
            XCTAssertTrue(app.buttons["player.bookmark"].waitForExistence(timeout: 30), "the audiobook player did not open; the shot would be just a cover")
            XCTAssertTrue(app.sliders.firstMatch.exists, "player must show its scrubber")
            sleep(2); shot("08-ipad-player")
            app.buttons["Close"].tap(); sleep(1)
        } else { XCTFail("no audiobook in the demo library offers Play or Resume") }

        // Ebook: detail page, then the reader with its readable column.
        if let read = openBook(app, offering: ["Read", "Continue reading"]) {
            waitForCovers(app); assertRegularDetailLayout(app, action: read); shot("09-ipad-ebook-detail")
            read.tap()
            XCTAssertTrue(app.staticTexts["reader.mode"].waitForExistence(timeout: 30), "the ebook reader did not open; the shot would be just a cover")
            sleep(6)
            // Page 1 of the EPUB is the cover image: turn forward (right-edge taps) until the web view shows text.
            var hasText = false
            for _ in 0..<8 {
                if app.webViews.staticTexts.count > 0 { hasText = true; break }
                app.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap(); sleep(2)
            }
            XCTAssertTrue(hasText, "the reader shot is a cover-only page; no text page was reached")
            shot("10-ipad-reader")
            XCUIDevice.shared.orientation = .landscapeLeft; sleep(3); shot("11-ipad-reader-landscape")
            XCUIDevice.shared.orientation = .portrait; sleep(2)
        } else { XCTFail("no ebook in the demo library offers Read") }

        // Landscape listing shots. Split View 1/2 width cannot be driven from XCUITest; see the PR.
        XCUIDevice.shared.orientation = .landscapeLeft; sleep(3)
        app.terminate(); app.launch(); sleep(5)
        openSection(app, "Library"); waitForCovers(app); shot("12-ipad-library-landscape")
        XCUIDevice.shared.orientation = .portrait
    }
}
