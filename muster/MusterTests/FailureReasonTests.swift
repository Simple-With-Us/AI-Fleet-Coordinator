import XCTest

final class FailureReasonTests: XCTestCase {
    func testAShortReasonIsShownWhole() {
        let reason = "simple-chat exited 2: unknown seat"
        XCTAssertEqual(FailureReason.shown(reason), reason)
        XCTAssertFalse(FailureReason.isShortened(reason))
    }

    func testSurroundingWhitespaceIsNotCountedAsShortening() {
        XCTAssertEqual(FailureReason.shown("  exit 1\n\n"), "exit 1")
        XCTAssertFalse(FailureReason.isShortened("  exit 1\n\n"))
    }

    func testALongTracebackKeepsItsLastLinesAndSaysHowManyWereLeftOut() {
        let lines = (1...40).map { "  File \"engine.py\", line \($0)" } + ["ValueError: bad manifest"]
        let shown = FailureReason.shown(lines.joined(separator: "\n"))
        let shownLines = shown.components(separatedBy: "\n")
        XCTAssertEqual(shownLines.first, "\u{2026} 35 earlier lines not shown")
        XCTAssertEqual(shownLines.count, FailureReason.shownLines + 1)
        XCTAssertEqual(shownLines.last, "ValueError: bad manifest")
        XCTAssertTrue(FailureReason.isShortened(lines.joined(separator: "\n")))
    }

    func testOneLeftOutLineIsSingular() {
        let reason = (1...7).map { "line \($0)" }.joined(separator: "\n")
        XCTAssertTrue(FailureReason.shown(reason).hasPrefix("\u{2026} 1 earlier line not shown\n"))
    }

    func testOneVeryLongLineKeepsItsEnd() {
        let reason = String(repeating: "x", count: 5000) + "END"
        let shown = FailureReason.shown(reason)
        XCTAssertEqual(shown.count, FailureReason.shownCharacters + 1)
        XCTAssertTrue(shown.hasPrefix("\u{2026}"))
        XCTAssertTrue(shown.hasSuffix("END"))
        XCTAssertTrue(FailureReason.isShortened(reason))
    }

    func testWindowsLineEndingsCountAsOneLineBreak() {
        let reason = (1...6).map { "line \($0)" }.joined(separator: "\r\n")
        XCTAssertEqual(FailureReason.shown(reason).components(separatedBy: "\n").count, 6)
        XCTAssertFalse(FailureReason.isShortened(reason))
    }
}
