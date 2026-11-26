import XCTest

/// Regression (#725): with something playing, the Discover message field must be visible above the mini player
/// and the tab bar, and accept typing. Needs the demo server like StoreShotsUITests; skipped without ALX_SHOTS_SECRET.
final class DiscoverInputUITests: XCTestCase {
    func testDiscoverInputVisibleAndTypableWhilePlaying() throws {
        let secret = try DemoSignIn.secret()
        let app = XCUIApplication()
        app.launch()
        DemoSignIn.run(app, secret: secret)
        let pad = UIDevice.current.userInterfaceIdiom == .pad
        func open(_ name: String) {
            if pad { app.descendants(matching: .any)["sidebar.\(name.lowercased())"].firstMatch.tap() }
            else { app.tabBars.buttons[name].tap() }
        }
        XCTAssertTrue((pad ? app.descendants(matching: .any)["sidebar.library"].firstMatch : app.tabBars.firstMatch).waitForExistence(timeout: 30))

        // Play the first audiobook or episode that offers Play/Resume.
        open("Library")
        var playing = false
        sleep(8)
        let count = min(app.scrollViews.descendants(matching: .button).count, 12)
        for i in 0..<count {
            let cover = app.scrollViews.descendants(matching: .button).element(boundBy: i)
            guard cover.exists else { continue }
            cover.tap()
            let play = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Play' OR label BEGINSWITH 'Resume'")).firstMatch
            if play.waitForExistence(timeout: 5) {
                play.tap()
                if app.buttons["player.bookmark"].waitForExistence(timeout: 30) { app.buttons["Close"].tap(); playing = true; break }
            }
            app.navigationBars.buttons.element(boundBy: 0).tap()
        }
        XCTAssertTrue(playing, "nothing in the demo library would play")
        XCTAssertTrue(app.descendants(matching: .any)["miniplayer"].firstMatch.waitForExistence(timeout: 10))

        open("Discover")
        let field = app.textFields["discover.input"]
        XCTAssertTrue(field.waitForExistence(timeout: 20), "the Discover message field is missing")
        XCTAssertTrue(field.isHittable, "the Discover message field is covered")
        let mini = app.descendants(matching: .any)["miniplayer"].firstMatch
        if mini.exists { XCTAssertLessThanOrEqual(field.frame.maxY, mini.frame.minY + 1, "field must sit above the mini player") }
        if !pad { XCTAssertLessThanOrEqual(field.frame.maxY, app.tabBars.firstMatch.frame.minY + 1) }
        field.tap()
        field.typeText("a quiet mystery")
        XCTAssertEqual(field.value as? String, "a quiet mystery")
        if app.keyboards.firstMatch.waitForExistence(timeout: 5) {
            XCTAssertLessThanOrEqual(field.frame.maxY, app.keyboards.firstMatch.frame.minY + 1, "field must sit above the keyboard")
        }
    }
}
