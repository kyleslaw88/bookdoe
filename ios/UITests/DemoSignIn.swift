import XCTest

/// Signs in to the demo server from the Add server sheet. Shared by the store shots and the size-class test.
enum DemoSignIn {
    static func secret() throws -> String {
        guard let s = ProcessInfo.processInfo.environment["ALX_SHOTS_SECRET"] else { throw XCTSkip("no ALX_SHOTS_SECRET") }
        return s
    }

    static func run(_ app: XCUIApplication, secret: String) {
        let env = ProcessInfo.processInfo.environment
        let wantURL = env["ALX_SHOTS_URL"] ?? "https://demo.bookdoe.com"
        let wantUser = env["ALX_SHOTS_USER"] ?? "appreview"
        let url = app.textFields["server-url"]
        if !url.waitForExistence(timeout: 20) {
            // Signed in already, maybe to another server from an earlier test or session: keep only the shots server.
            XCTAssertTrue(app.descendants(matching: .any)["sidebar.library"].firstMatch.exists || app.tabBars.firstMatch.exists,
                          "neither the Add server sheet nor a signed-in UI is showing")
            if onlyShotsServer(app, host: URL(string: wantURL)?.host ?? wantURL, user: wantUser) { return }
            if !url.waitForExistence(timeout: 10) { app.navigationBars.buttons["plus"].firstMatch.tap() }
            XCTAssertTrue(url.waitForExistence(timeout: 20), "removing the other servers did not bring up the Add server sheet")
        }
        url.tap(); url.typeText(wantURL)
        let user = app.textFields["server-username"]; user.tap(); user.typeText(wantUser)
        let field = app.secureTextFields["server-password"]; field.tap(); field.typeText(secret)
        app.buttons["Sign in"].tap()
    }

    /// Opens Settings > Manage servers and removes every server that is not the shots server (or all of them when the
    /// shots server is not there). Returns true when exactly the shots server is left, now active.
    private static func onlyShotsServer(_ app: XCUIApplication, host: String, user: String) -> Bool {
        let side = app.descendants(matching: .any)["sidebar.settings"].firstMatch
        if side.exists { side.tap() } else { app.tabBars.buttons["Settings"].tap() }
        let manage = app.buttons["Manage servers"].firstMatch
        let link = manage.waitForExistence(timeout: 10) ? manage : app.staticTexts["Manage servers"]
        link.tap()
        let isShots = NSPredicate(format: "label CONTAINS %@ AND label CONTAINS %@", host, user)
        let others = app.buttons.matching(NSPredicate(format: "(label CONTAINS 'bookdoe' OR label CONTAINS 'Jellyfin' OR label CONTAINS 'Audiobookshelf') AND NOT (label CONTAINS %@ AND label CONTAINS %@)", host, user))
        for _ in 0..<12 where others.firstMatch.exists {
            others.firstMatch.swipeLeft()
            let del = app.buttons["Delete"].firstMatch
            if del.waitForExistence(timeout: 3) { del.tap() }
            sleep(1)
        }
        let keep = app.buttons.matching(isShots).firstMatch
        guard keep.exists else { return false }
        keep.tap()
        return true
    }
}
