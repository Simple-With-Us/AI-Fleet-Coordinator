import XCTest

/// Whether the Operator can post (`status.operator_voice`):  decoded leniently,  never read as
/// able to post when the eavesdropper does not say,  and tied to dropped wakes only when the
/// ledger gives that reason.
final class OperatorVoiceTests: XCTestCase {
    private func decode(_ json: String) throws -> ListenerStatus {
        try JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
    }

    /// 2:14 PM on the owner's clock,  Saturday,  November 7,  2026.
    private let since = "2026-11-07T20:14:00Z"

    // MARK: Decoding

    /// A block of the wrong shape costs that block only:  the rest of the status is read,
    /// and the block reads as not known,  never as available and never as not reported.
    func test_malformed_costs_only_the_block() throws {
        let shapes = [
            #""operator_voice": "yes""#,
            #""operator_voice": [true]"#,
            #""operator_voice": {"available": "yes", "code": 7, "since": false}"#,
            #""operator_voice": {}"#,
        ]
        for shape in shapes {
            let status = try decode("""
            {"running": true, "pid": 42, "status": {"instance": "mac", \(shape),
              "seats": {"MM": {"bot": "MM", "wake": "muse", "connected": true}}}}
            """)
            XCTAssertEqual(status.running, true, shape)
            XCTAssertEqual(status.pid, 42, shape)
            XCTAssertEqual(status.status?.instance, "mac", shape)
            XCTAssertEqual(status.seat("MM")?.connected, true, shape)
            XCTAssertNotNil(status.status?.operator_voice, shape)
            let reading = OperatorVoiceReading(status.status)
            XCTAssertEqual(reading, .unreadable, shape)
            XCTAssertEqual(reading.label, "Not known", shape)
        }
    }

    /// A field of the wrong type inside a block that answers costs that field only.
    func test_a_mistyped_code_or_since_keeps_the_answer() throws {
        let status = try decode("""
        {"running": true, "status": {"operator_voice": {"available": false, "code": 12, "since": "soon"}}}
        """)
        let reading = OperatorVoiceReading(status.status)
        XCTAssertEqual(reading, .unavailable(code: nil, since: nil, sinceText: "soon"))
        XCTAssertEqual(reading.sentence(), "The Operator cannot post to Zulip.")
        // A time Muster could not read is quoted in the tooltip and said to be so.
        XCTAssertTrue(reading.tooltip().contains("a time Muster could not read:  soon"), reading.tooltip())
    }
}
