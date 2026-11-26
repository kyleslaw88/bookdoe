import XCTest

/// Regression: iPad (regular width) shows the sidebar, iPhone keeps the tab bar. Needs a signed-in
/// demo server like StoreShotsUITests, so it is skipped without ALX_SHOTS_SECRET.
final class SizeClassUITests: XCTestCase {
    func testNavigationMatchesSizeClass() throws {
        let secret = try DemoSignIn.secret()
        let app = XCUIApplication()
        app.launch()
        DemoSignIn.run(app, secret: secret)
        XCTAssertFalse(app.staticTexts["The Keychain refused"].exists)
        if UIDevice.current.userInterfaceIdiom == .pad {
            XCTAssertTrue(app.descendants(matching: .any)["sidebar.library"].firstMatch.waitForExistence(timeout: 30))
            XCTAssertFalse(app.tabBars.firstMatch.exists)
        } else {
            XCTAssertTrue(app.tabBars.firstMatch.waitForExistence(timeout: 30))
        }
    }
}
