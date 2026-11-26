import XCTest

/// Regression (#725): with the mini player showing, the Discover message field sits above it and above the tab bar
/// and takes typing. Runs without a server or secret: ALX_DISCOVER_DEMO seeds a signed-in server, Discover and a mini player.
final class DiscoverLayoutUITests: XCTestCase {
    func testDiscoverFieldSitsAboveTheMiniPlayer() throws {
        let app = XCUIApplication()
        app.launchEnvironment["ALX_DISCOVER_DEMO"] = "1"
        app.launch()
        let pad = UIDevice.current.userInterfaceIdiom == .pad
        let entry = pad ? app.descendants(matching: .any)["sidebar.discover"].firstMatch : app.tabBars.buttons["Discover"]
        XCTAssertTrue(entry.waitForExistence(timeout: 30), "Discover is not offered")
        entry.tap()

        let mini = app.descendants(matching: .any)["miniplayer"].firstMatch
        XCTAssertTrue(mini.waitForExistence(timeout: 10), "mini player did not show")
        let field = app.textFields["discover.input"]
        XCTAssertTrue(field.waitForExistence(timeout: 10), "the Discover message field is missing")
        XCTAssertTrue(field.isHittable, "the Discover message field is covered")
        XCTAssertLessThanOrEqual(field.frame.maxY, mini.frame.minY + 1, "field must sit above the mini player")
        // The mini player background extends under the floating tab bar, so compare its top edge, not its bottom.
        if !pad { XCTAssertLessThan(mini.frame.minY, app.tabBars.firstMatch.frame.minY, "mini player must start above the tab bar"); XCTAssertLessThanOrEqual(field.frame.maxY, app.tabBars.firstMatch.frame.minY + 1, "field must sit above the tab bar") }
        add(XCTAttachment(screenshot: app.screenshot()))

        field.tap()
        field.typeText("a quiet mystery")
        XCTAssertEqual(field.value as? String, "a quiet mystery")
        if app.keyboards.firstMatch.waitForExistence(timeout: 5) {
            XCTAssertLessThanOrEqual(field.frame.maxY, app.keyboards.firstMatch.frame.minY + 1, "field must sit above the keyboard")
        }
    }
}
