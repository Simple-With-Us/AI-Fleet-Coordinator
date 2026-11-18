import XCTest

/// The Eavesdropper's pause and resume (AFC-832):  the words Muster sends, the pause it reads
/// back, and that the result says done only when the server's read-back shows the change.
final class OperatorPauseTests: XCTestCase {
    private let gap = ServerStatusCopy.sentenceGap

    private func parse(_ object: [String: Any], exit: Int32, mode: OpsMode, action: OpsAction) throws -> OpsOutcome {
        let text = String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
        return OpsResultParser.parse(OpsProcessResult(exitCode: exit, stdout: text, stderr: ""),
                                     mode: mode, action: action, timeout: 90)
    }

    func test_each_kind_sends_one_verb_and_one_target_word() {
        let cases: [(OperatorPauseRequest, [String])] = [
            (OperatorPauseRequest(kind: .pause, seat: nil), ["server", "pause", "server"]),
            (OperatorPauseRequest(kind: .pauseWakes, seat: "CLAUDE"), ["server", "pause-wakes", "CLAUDE"]),
            (OperatorPauseRequest(kind: .resume, seat: nil), ["server", "resume", "server"]),
            (OperatorPauseRequest(kind: .resume, seat: "CODEX"), ["server", "resume", "CODEX"]),
        ]
        for (request, words) in cases {
            let action = OpsAction.eavesdropperPause(request)
            XCTAssertEqual(action.verbArguments, words)
            XCTAssertEqual(action.auditVerb, "server \(request.verbWord)")
            XCTAssertEqual(action.auditTarget, request.targetWord)
            XCTAssertEqual(action.runsOn, .server)
            XCTAssertFalse(action.changesNothing)
            XCTAssertTrue(action.effectSentence.hasSuffix("."), "\(request)")
            XCTAssertFalse(action.effectSentence.contains("  "), "\(request)")
            XCTAssertFalse(action.effectSentence.contains("Nothing changes."), "\(request)")
        }
    }

    func test_a_seat_must_be_in_the_helpers_seat_shape() {
        XCTAssertTrue(OperatorPauseRequest(kind: .pause, seat: "CLAUDE").isValid)
        XCTAssertTrue(OperatorPauseRequest(kind: .pause, seat: nil).isValid)
        for bad in ["claude", "CLAUDE CODEX", "--seat", "", "A/B", String(repeating: "A", count: 33)] {
            XCTAssertFalse(OperatorPauseRequest(kind: .pause, seat: bad).isValid, bad)
        }
    }

    func test_the_title_names_the_target_and_the_place() {
        XCTAssertEqual(
            OpsAction.eavesdropperPause(OperatorPauseRequest(kind: .pauseWakes, seat: "CLAUDE")).title,
            "Pause wakes for CLAUDE \(FleetPlace.machine(.server).inProse)"
        )
        XCTAssertTrue(OpsAction.eavesdropperPause(OperatorPauseRequest(kind: .resume, seat: nil)).title
            .hasPrefix("Resume every seat"))
    }

    func test_the_pause_in_force_decodes_and_drops_what_is_not_a_seat() throws {
        let json = """
        {"all": {"wakesOnly": true, "setBy": "cli", "since": "2026-11-18T10:00:00Z"},
         "seats": {"CODEX": {"wakesOnly": false, "setBy": null, "since": null},
                   "not a seat": {"wakesOnly": true}}}
        """
        let state = try JSONDecoder().decode(OperatorPauseState.self, from: Data(json.utf8))
        XCTAssertEqual(state.all, OperatorPauseEntry(wakesOnly: true, setBy: "cli", since: "2026-11-18T10:00:00Z"))
        XCTAssertEqual(Array(state.seats.keys), ["CODEX"])
        XCTAssertFalse(state.isClear)
        XCTAssertEqual(state.lines.count, 2)
        XCTAssertTrue(state.lines[0].hasPrefix("Every seat: wakes are paused"))
        XCTAssertTrue(state.lines[1].hasPrefix("CODEX: wakes and live interrupts are paused"))
        for line in state.lines { XCTAssertFalse(line.contains("  "), line) }
        let clear = try JSONDecoder().decode(OperatorPauseState.self, from: Data(#"{"all": null, "seats": {}}"#.utf8))
        XCTAssertTrue(clear.isClear)
        XCTAssertEqual(clear.lines, ["Nothing is paused."])
    }

    /// A malformed pause is unreadable, never "Nothing is paused.", and a pause whose kind was not
    /// reported is never shown as a full one.
    func test_a_malformed_pause_is_unreadable_and_an_unreported_kind_is_said_so() throws {
        for json in [#"{"all": null, "seats": ["CODEX"]}"#, #"{"all": "yes", "seats": {}}"#,
                     #"{"all": null, "seats": {"CODEX": "paused"}}"#] {
            XCTAssertThrowsError(try JSONDecoder().decode(OperatorPauseState.self, from: Data(json.utf8)), json)
        }
        let unreported = OperatorPauseEntry(wakesOnly: nil, setBy: nil, since: nil).sentence(for: "CODEX")
        XCTAssertEqual(unreported, "CODEX: paused, and whether live interrupts stop too is not reported.")
        // The answer's `pause` is then nil:  the sheet shows no block rather than an empty one.
        let action = OpsAction.eavesdropperPause(OperatorPauseRequest(kind: .pause, seat: nil))
        let plan = try parse([
            "ok": true, "verb": "server pause", "target": "server", "result": "planned", "detail": "x",
            "pause": ["all": NSNull(), "seats": ["CODEX"]],
        ], exit: 0, mode: .plan, action: action)
        XCTAssertNil(plan.pause)
    }

    func test_a_plan_shows_the_pause_in_force_and_an_answer_for_another_verb_does_not() throws {
        let action = OpsAction.eavesdropperPause(OperatorPauseRequest(kind: .pause, seat: nil))
        var answer: [String: Any] = [
            "ok": true, "verb": "server pause", "target": "server", "result": "planned",
            "detail": "every seat:  wakes and live interrupts stop",
            "commands": [["/usr/bin/docker", "exec", "--user", "simpletools", "eavesdropper",
                          "/app/scripts/simple_tools/server/entrypoint.sh", "daemon", "pause"]],
            "pause": ["all": NSNull(), "seats": ["CODEX": ["wakesOnly": true, "setBy": "cli", "since": NSNull()]]],
        ]
        let plan = try parse(answer, exit: 0, mode: .plan, action: action)
        XCTAssertEqual(plan.pause?.seats["CODEX"]?.wakesOnly, true)
        answer["verb"] = "server resume"
        XCTAssertNil(try parse(answer, exit: 0, mode: .plan, action: action).pause)
    }

    func test_a_run_is_done_only_when_the_status_shows_it() throws {
        let action = OpsAction.eavesdropperPause(OperatorPauseRequest(kind: .pauseWakes, seat: "CLAUDE"))
        let done = try parse([
            "ok": true, "verb": "server pause-wakes", "target": "CLAUDE", "result": "done", "detail": "x",
            "pause": ["all": NSNull(), "seats": ["CLAUDE": ["wakesOnly": true, "setBy": "cli", "since": NSNull()]]],
        ], exit: 0, mode: .yes, action: action)
        XCTAssertEqual(done.resultTitle(for: action), "Paused.\(gap)The Eavesdropper's status shows it, and capture goes on.")
        let unconfirmed = try parse([
            "ok": false, "verb": "server pause-wakes", "target": "CLAUDE", "result": "unknown",
            "detail": "x", "errorCode": "not_confirmed",
        ], exit: 1, mode: .yes, action: action)
        XCTAssertTrue(unconfirmed.resultTitle(for: action).hasPrefix("The command ran, but the Eavesdropper's status did not show the change"))
        XCTAssertTrue(unconfirmed.runNotKnown)
        // A timeout is not known either:  it reads as "may or may not have run", never as failed.
        let timedOut = try parse([
            "ok": false, "verb": "server pause-wakes", "target": "CLAUDE", "result": "unknown",
            "detail": "x", "errorCode": "timeout",
        ], exit: 1, mode: .yes, action: action)
        XCTAssertEqual(timedOut.kind, .unknown)
        XCTAssertTrue(timedOut.resultTitle(for: action).hasPrefix("This may or may not have run"))
    }

    func test_every_code_the_pause_verbs_answer_has_advice() throws {
        let action = OpsAction.eavesdropperPause(OperatorPauseRequest(kind: .resume, seat: "CLAUDE"))
        for code in ["seat_unknown", "status_unavailable", "not_paused", "not_confirmed", "pause_failed",
                     "pause_unreadable", "drain_active", "would_loosen",
                     "usage", "docker_unavailable", "container_not_running", "read_failed", "timeout",
                     "too_large"] {
            let outcome = try parse([
                "ok": false, "verb": "server resume", "target": "CLAUDE", "result": "failed",
                "detail": "x", "errorCode": code,
            ], exit: 1, mode: .yes, action: action)
            XCTAssertNotNil(outcome.advice(for: action), code)
        }
        // The pause advice for `usage` is not the test wake's.
        let usage = try parse([
            "ok": false, "verb": "server resume", "target": "CLAUDE", "result": "failed",
            "detail": "x", "errorCode": "usage",
        ], exit: 1, mode: .yes, action: action)
        XCTAssertFalse(usage.advice(for: action)?.contains("test wake") ?? true)
    }
}
