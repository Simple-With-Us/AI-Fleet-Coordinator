import Foundation
import XCTest

/// Shared presentation survives removal of the legacy ListenerStatus envelope.
final class EavesdropperStatusTextTests: XCTestCase {
    func test_existing_voice_causes_are_preserved_and_unknown_is_not_guessed() {
        let causes = [
            "operator_credential_rejected": "Zulip refused the Operator's credential.",
            "operator_owner_not_pinned": "The owner is not pinned in the listener's settings, so the Operator does not post.",
            "operator_unavailable": "The listener could not get the Operator ready to post.",
        ]
        for (code, cause) in causes {
            XCTAssertEqual(EavesdropperStatusText.cause(code), cause)
            XCTAssertEqual(ServerOperatorVoice.cannotPost(code: code, since: nil).cause, cause)
        }
        XCTAssertNil(EavesdropperStatusText.cause("something_new"))
        XCTAssertNil(EavesdropperStatusText.cause(""))
    }

    func test_clock_keeps_chicago_day_boundary_and_missing_time() throws {
        let now = try XCTUnwrap(FleetTime.parse("2026-11-22T06:05:00Z"))
        let sameDay = try XCTUnwrap(FleetTime.parse("2026-11-22T06:01:00Z"))
        let priorDay = try XCTUnwrap(FleetTime.parse("2026-11-22T05:59:00Z"))
        XCTAssertEqual(EavesdropperStatusText.whenText(sameDay, now: now), "12:01 AM")
        XCTAssertEqual(EavesdropperStatusText.whenText(priorDay, now: now), "Nov 21, 11:59 PM")
        XCTAssertEqual(EavesdropperStatusText.time(sameDay, now: now), "12:01 AM")
        XCTAssertEqual(EavesdropperStatusText.time(nil, now: now), "Not reported")
    }
}
