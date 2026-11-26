import XCTest

/// Plays a demo-server audiobook, closes the mini player and checks the bar is gone and the position survived.
final class MiniPlayerUITests: XCTestCase {
    func testClosingTheMiniPlayerHidesItAndKeepsThePosition() throws {
        let secret = try DemoSignIn.secret()
        let app = XCUIApplication()
        app.launch()
        DemoSignIn.run(app, secret: secret)
        let cover = app.scrollViews.descendants(matching: .button).firstMatch
        XCTAssertTrue(cover.waitForExistence(timeout: 30), "no library item to play")
        let tab = app.tabBars.buttons["Library"]
        if tab.exists { tab.tap() }
        var play: XCUIElement?
        for i in 0..<8 {
            let c = app.scrollViews.descendants(matching: .button).element(boundBy: i)
            guard c.waitForExistence(timeout: 10) else { break }
            c.tap()
            let b = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Play' OR label BEGINSWITH 'Resume'")).firstMatch
            if b.waitForExistence(timeout: 5) { play = b; break }
            app.navigationBars.buttons.element(boundBy: 0).tap()
        }
        let button = try XCTUnwrap(play, "no audiobook offers Play or Resume")
        button.tap()
        XCTAssertTrue(app.buttons["player.bookmark"].waitForExistence(timeout: 30))
        sleep(5)
        app.buttons["Close"].tap()
        let bar = app.descendants(matching: .any)["miniplayer"].firstMatch
        XCTAssertTrue(bar.waitForExistence(timeout: 10), "mini player did not show")
        bar.press(forDuration: 1.2)
        let close = app.buttons["miniplayer.close"]
        XCTAssertTrue(close.waitForExistence(timeout: 5), "no Close player in the mini player menu")
        close.tap()
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: bar)
        XCTAssertEqual(XCTWaiter().wait(for: [gone], timeout: 15), .completed, "mini player still showing")
        // The saved position makes the book offer Resume rather than Play.
        let resume = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Resume'")).firstMatch
        XCTAssertTrue(resume.waitForExistence(timeout: 10), "position was not saved: the book does not offer Resume")
    }
}
