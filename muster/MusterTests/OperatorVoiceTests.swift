import XCTest

/// The two pieces of the Operator's voice words that the Operator panel and the server rows
/// still use.
final class OperatorVoiceTests: XCTestCase {
    func test_a_known_reason_code_is_said_in_plain_words_and_an_unknown_one_is_not_guessed_at() {
        for code in ["operator_credential_rejected", "operator_owner_not_pinned", "operator_unavailable"] {
            let words = OperatorVoiceReading.cause(code)
            XCTAssertNotNil(words, code)
            XCTAssertFalse(words?.contains("_") ?? true, code)
        }
        XCTAssertNil(OperatorVoiceReading.cause("something_new"))
    }

    func test_a_time_is_said_on_the_owners_clock_with_the_day_only_when_it_is_not_today() {
        // 2:14 PM on the owner's clock, Saturday, November 7, 2026.
        let at = ISO8601DateFormatter().date(from: "2026-11-07T20:14:00Z")!
        XCTAssertEqual(OperatorVoiceReading.whenText(at, now: at), FleetTime.clock(at))
        let later = at.addingTimeInterval(3 * 86_400)
        XCTAssertTrue(OperatorVoiceReading.whenText(at, now: later).hasPrefix("Nov 7, "))
    }
}
