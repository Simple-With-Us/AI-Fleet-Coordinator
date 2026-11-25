import XCTest

// A Test Wake run whose result is missing or in another shape (AFC-825, Kody's finding on PR 2299).
// The helper's `done` always carries a `testWake` object that says `passed: true`
// (`scripts/fleet-ops-control.py`, `run_test_wake`).  Without one, the sheet must not say the router
// accepted the dry run, but it still keeps the helper's own words.

private let wakeTarget = OperatorWakeTarget(seat: "CLAUDE", letter: "BB", generation: 2, hostCode: "S")

private func testWakeRun(_ stdout: String) -> OpsOutcome {
    OpsResultParser.parse(
        OpsProcessResult(exitCode: 0, stdout: stdout, stderr: "", timedOut: false),
        mode: .yes, action: .testWake(wakeTarget), timeout: 180
    )
}

/// A `done` answer to `server test-wake` with `testWake` spelled as given (`nil` leaves the key out).
private func doneAnswer(testWake: String?) -> String {
    let field = testWake.map { ", \"testWake\": \($0)" } ?? ""
    return """
    {"ok": true, "verb": "server test-wake", "target": "CLAUDE.BB.2.S",
     "commands": [["/usr/bin/docker", "exec"]], "result": "done", "detail": "the router answered"\(field)}
    """
}

private let passedResult = """
{"target": {"seat": "CLAUDE", "letter": "BB", "generation": 2, "host": "server"},
 "passed": true, "accepted": true, "wakeId": "test-wake-20261118T053000Z-0a1b2c3d", "httpStatus": 200,
 "error": null, "router": {"healthy": true, "delivery": true, "reason": null},
 "answer": {"pick": "letter", "pickedLetter": "BB", "pickedGeneration": 2, "host": "server",
            "reason": "addressed_letter"}}
"""

final class OperatorTestWakeUnreadableTests: XCTestCase {
    private let successTitle = "The router accepted the dry run"

    func test_a_result_in_another_shape_keeps_the_answer_but_is_never_a_pass() throws {
        for shape in ["\"yes\"", "[1, 2]", "7", "true"] {
            let outcome = testWakeRun(doneAnswer(testWake: shape))
            XCTAssertEqual(outcome.kind, .done, shape)
            XCTAssertNil(outcome.testWake, shape)
            XCTAssertTrue(outcome.testWakeUnreadable, shape)
            XCTAssertTrue(outcome.testWakeUnconfirmed, shape)
            XCTAssertFalse(outcome.isSuccess(for: .yes), shape)
            // The rest of the answer still reads:  the helper's own words are kept.
            XCTAssertTrue(outcome.detail.lowercased().contains("the router answered"), outcome.detail)
            let title = outcome.resultTitle(for: .testWake(wakeTarget))
            XCTAssertFalse(title.hasPrefix(successTitle), title)
            XCTAssertTrue(title.contains("could not be read"), title)
            XCTAssertTrue(title.hasSuffix("Nothing was delivered."), title)
        }
    }

    func test_a_result_that_does_not_say_it_passed_is_never_a_pass() throws {
        // An object whose fields are all in other shapes reads field by field as not reported.
        let outcome = testWakeRun(doneAnswer(testWake: #"{"passed": "yes", "router": {"healthy": "yes"}}"#))
        XCTAssertNotNil(outcome.testWake)
        XCTAssertFalse(outcome.testWakeUnreadable)
        XCTAssertTrue(outcome.testWakeUnconfirmed)
        XCTAssertFalse(outcome.isSuccess(for: .yes))
        let title = outcome.resultTitle(for: .testWake(wakeTarget))
        XCTAssertFalse(title.hasPrefix(successTitle), title)
        XCTAssertTrue(title.contains("sent no test wake result that says it passed"), title)
    }

    func test_a_done_with_no_result_or_a_null_one_is_never_a_pass() {
        for field in [nil, "null"] {
            let outcome = testWakeRun(doneAnswer(testWake: field))
            XCTAssertNil(outcome.testWake)
            XCTAssertFalse(outcome.testWakeUnreadable, "\(String(describing: field))")
            XCTAssertTrue(outcome.testWakeUnconfirmed, "\(String(describing: field))")
            XCTAssertFalse(outcome.isSuccess(for: .yes))
            let title = outcome.resultTitle(for: .testWake(wakeTarget))
            XCTAssertFalse(title.hasPrefix(successTitle), title)
        }
    }

    func test_an_answer_for_another_verb_is_never_this_pass() {
        let text = doneAnswer(testWake: passedResult)
            .replacingOccurrences(of: "\"server test-wake\"", with: "\"server logs\"")
        let outcome = testWakeRun(text)
        XCTAssertNil(outcome.testWake)
        XCTAssertFalse(outcome.isSuccess(for: .yes))
    }

    func test_a_readable_passed_result_is_still_a_pass() throws {
        let outcome = testWakeRun(doneAnswer(testWake: passedResult))
        XCTAssertFalse(outcome.testWakeUnreadable)
        XCTAssertFalse(outcome.testWakeUnconfirmed)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(try XCTUnwrap(outcome.testWake).wakeID, "test-wake-20261118T053000Z-0a1b2c3d")
        XCTAssertTrue(outcome.resultTitle(for: .testWake(wakeTarget)).hasPrefix(successTitle))
    }

    func test_the_unreadable_mark_is_only_for_a_present_value() throws {
        let unreadable = try XCTUnwrap(OpsResultParser.decode(doneAnswer(testWake: "\"yes\"")))
        XCTAssertTrue(unreadable.testWakeUnreadable)
        XCTAssertEqual(unreadable.result, "done")
        let nullValue = try XCTUnwrap(OpsResultParser.decode(doneAnswer(testWake: "null")))
        XCTAssertFalse(nullValue.testWakeUnreadable)
        let absent = try XCTUnwrap(OpsResultParser.decode(doneAnswer(testWake: nil)))
        XCTAssertFalse(absent.testWakeUnreadable)
    }

    func test_a_plan_is_never_marked() {
        let outcome = OpsResultParser.parse(
            OpsProcessResult(exitCode: 0, stdout: doneAnswer(testWake: "\"yes\""), stderr: "", timedOut: false),
            mode: .plan, action: .testWake(wakeTarget), timeout: 90
        )
        XCTAssertFalse(outcome.testWakeUnreadable)
        XCTAssertFalse(outcome.testWakeUnconfirmed)
    }
}
