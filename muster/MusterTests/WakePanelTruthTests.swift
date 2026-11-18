import XCTest

/// Two lines of the Seats and Operator panels that are true whatever else the panel shows.
final class WakePanelTruthTests: XCTestCase {
    // MARK: Small pieces

    /// The Operator's line says the Mac needs no section, and names a broken one.
    func test_the_operator_line_is_true_either_way() {
        let none = OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.MM]\nwake = \"muse\"\n"))
        XCTAssertNil(none.problem)
        XCTAssertTrue(none.summary.contains("needs none"))
        XCTAssertFalse(none.summary.contains("not provisioned"))
        let broken = OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.OPERATOR]\nbot = \"X\"\ncreds = \"infisical\"\nwake = \"inbox\"\n"))
        XCTAssertTrue(broken.summary.contains("bot must be Operator"))
    }

    // MARK: The Operator truth audit (F2 to F12)

    /// F6.  A session with no letter says no letter was reported, which is true whether it never
    /// registered one or the lookup failed, and never claims it has no registered name.
    func test_a_session_without_a_letter_says_no_letter_was_reported() throws {
        let lease = try JSONDecoder().decode(
            ListenerStateDocument.Lease.self,
            from: Data(#"{"lease_id": "a", "platform": "claude-code", "alive": true, "watcher": true}"#.utf8)
        )
        let line = SeatSession(lease: lease, index: 0).line
        XCTAssertTrue(line.hasPrefix(SeatActivityText.noLetter), line)
        XCTAssertEqual(SeatActivityText.noLetter, "No letter reported")
        XCTAssertFalse(line.contains("registered name"))
        let titled = try JSONDecoder().decode(
            ListenerStateDocument.Lease.self,
            from: Data(#"{"alive": true, "watcher": true, "title": "Fix the panel"}"#.utf8)
        )
        XCTAssertTrue(SeatSession(lease: titled, index: 0).line.hasPrefix("No letter reported:  Fix the panel"))
    }
}
