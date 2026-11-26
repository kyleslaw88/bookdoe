import XCTest
@testable import BookdoeKit

final class HTMLTextTests: XCTestCase {
    func testNumericAndNamedEntities() {
        XCTAssertEqual(HTMLText.plain("excelled&#8212;winning a Rhodes"), "excelled\u{2014}winning a Rhodes")
        XCTAssertEqual(HTMLText.plain("Tom &amp; Jerry &#x2019;s &lt;3"), "Tom & Jerry \u{2019}s <3")
        XCTAssertEqual(HTMLText.plain("caf&eacute; &unknown; &#xZZ;"), "caf\u{00E9} &unknown; &#xZZ;")
    }

    func testTagsAreStrippedAndParagraphsKept() {
        XCTAssertEqual(HTMLText.plain("<p>One</p><p>Two<br/>three</p>"), "One\n\nTwo\nthree")
        XCTAssertEqual(HTMLText.plain("<b>bold</b> and <a href=\"x\">link</a>"), "bold and link")
    }

    func testEscapedTagsStayText() {
        XCTAssertEqual(HTMLText.plain("a &lt;b&gt; c"), "a <b> c")
    }

    func testPlainTextIsUntouched() {
        XCTAssertEqual(HTMLText.plain("Just words.\n\nTwo paragraphs."), "Just words.\n\nTwo paragraphs.")
    }
}
