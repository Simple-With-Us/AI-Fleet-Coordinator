import XCTest

final class FailureReasonTests: XCTestCase {
    func testAShortReasonIsShownWhole() {
        let reason = "simple-chat exited 2: unknown seat"
        XCTAssertEqual(FailureReason.shown(reason), reason)
        XCTAssertFalse(FailureReason.display(reason).isShortened)
    }

    func testSurroundingWhitespaceIsNotCountedAsShortening() {
        XCTAssertEqual(FailureReason.shown("  exit 1\n\n"), "exit 1")
        XCTAssertFalse(FailureReason.display("  exit 1\n\n").isShortened)
    }

    func testALongTracebackKeepsItsLastLinesAndSaysHowManyWereLeftOut() {
        let lines = (1...40).map { "  File \"engine.py\", line \($0)" } + ["ValueError: bad manifest"]
        let reason = lines.joined(separator: "\n")
        let shown = FailureReason.shown(reason)
        let shownLines = shown.components(separatedBy: "\n")
        XCTAssertEqual(shownLines.first, "\u{2026} 35 earlier lines not shown")
        XCTAssertEqual(shownLines.count, FailureReason.shownLines + 1)
        XCTAssertEqual(shownLines.last, "ValueError: bad manifest")
        XCTAssertEqual(FailureReason.display(reason).text, shown)
        XCTAssertTrue(FailureReason.display(reason).isShortened)
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
        XCTAssertTrue(FailureReason.display(reason).isShortened)
    }

    func testLinesTheCharacterCapRemovesWholeAreCounted() {
        // Six lines, within the line cap, but 1805 characters:  the last 600 hold the end of
        // line 5 and all of line 6, so lines 1 to 4 are gone and line 5 is cut.
        let reason = (1...6).map { String(repeating: "\($0)", count: 300) }.joined(separator: "\n")
        let shownLines = FailureReason.shown(reason).components(separatedBy: "\n")
        XCTAssertEqual(shownLines.count, 3)
        XCTAssertEqual(shownLines[0], "\u{2026} 4 earlier lines not shown")
        XCTAssertEqual(shownLines[1], "\u{2026}" + String(repeating: "5", count: 299))
        XCTAssertEqual(shownLines[2], String(repeating: "6", count: 300))
    }

    func testALineTheCapRemovesUpToItsLineBreakIsCountedAndNotCut() {
        // 299 + 1 + 299 + 1 + 299 characters:  the last 299 are line 3 alone, so the removed
        // part ends exactly at line 2's end and both earlier lines are counted.
        let reason = (1...3).map { String(repeating: "\($0)", count: 299) }.joined(separator: "\n")
        let shown = FailureReason.shown(reason, characters: 300)
        XCTAssertEqual(shown, "\u{2026} 2 earlier lines not shown\n" + String(repeating: "3", count: 299))
    }

    func testWhatIsShownIsBoundedHoweverTheReasonIsShaped() {
        let reasons = [
            String(repeating: "y", count: 20_000),
            (1...500).map { String(repeating: "z", count: $0 % 300) }.joined(separator: "\n"),
            String(repeating: "\n", count: 1000) + "tail",
        ]
        for reason in reasons {
            let shown = FailureReason.shown(reason)
            // The marker line, the kept lines, and one ellipsis at most.
            XCTAssertLessThanOrEqual(shown.components(separatedBy: "\n").count, FailureReason.shownLines + 1)
            XCTAssertLessThanOrEqual(shown.count, FailureReason.shownCharacters + 40)
        }
    }

    func testWindowsLineEndingsCountAsOneLineBreak() {
        let reason = (1...6).map { "line \($0)" }.joined(separator: "\r\n")
        XCTAssertEqual(FailureReason.shown(reason).components(separatedBy: "\n").count, 6)
        XCTAssertFalse(FailureReason.display(reason).isShortened)
    }
}
