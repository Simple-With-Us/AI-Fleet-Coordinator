import XCTest

// The wake router's health, its unit and its dry-run wake (`RouterControl.swift`), the route that
// carries them, the card and rows that show them, and the store that runs a dry run.  The answers
// are the shapes the server's control helper writes (`scripts/fleet-ops-control.py`,
// `reduce_router_health`, `router_unit_state` and `reduce_router_test_wake`), with `now` at noon on
// Nov 12, 2026 in Chicago.

private let now = FleetTime.parse("2026-11-12T18:00:00Z")!

private func at(_ secondsAgo: Int) -> String {
    let date = now.addingTimeInterval(-Double(secondsAgo))
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.string(from: date)
}

private func result(_ stdout: String, exit: Int32 = 0, timedOut: Bool = false) -> OpsProcessResult {
    OpsProcessResult(exitCode: exit, stdout: stdout, stderr: "", timedOut: timedOut)
}

private struct FixedRunner: OpsRunning {
    let result: OpsProcessResult
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult { result }
}

private func builder() -> OpsCommandBuilder {
    OpsCommandBuilder(
        repoRoot: URL(fileURLWithPath: "/tmp/example-checkout"),
        home: URL(fileURLWithPath: "/tmp/example-home"),
        sshAuthSocket: nil
    )
}

// MARK: - Fixtures

private func unitJSON(_ active: String = "active", sub: String = "running", result: String = "success") -> String {
    """
    {"unit": "afc-wake-router.service", "activeState": "\(active)", "subState": "\(sub)",
     "unitResult": "\(result)", "lastExitAt": null, "checkedAt": "\(at(2))"}
    """
}

private let defaultRouterSeats = """
{"seat": "AG", "installed": true, "healthy": true, "delivery": false, "registry_configured": true, "reason": null},
{"seat": "CLAUDE", "installed": true, "healthy": true, "delivery": true, "registry_configured": true, "reason": null},
{"seat": "CODEX", "installed": true, "healthy": false, "delivery": true, "registry_configured": false, "reason": "registry_not_configured"},
{"seat": "MC", "installed": false, "healthy": false, "delivery": null, "registry_configured": null, "reason": "not_installed"}
"""

private func healthAnswer(
    healthy: String = "false", seats: String? = nil, unit: String? = nil, schema: Int = 1,
    instance: String = "server"
) -> String {
    """
    {"ok": true, "verb": "server router-health", "target": "server", "commands": [], "result": "planned",
     "detail": "read-only health of the server's wake router",
     "routerHealth": {"schema": \(schema), "generated_at": "\(at(5))", "instance": "\(instance)", "healthy": \(healthy),
       "counts": {"total": 4, "installed": 3, "not_installed": 1, "healthy": 2, "delivery_on": 2, "registry_configured": 2},
       "truncated": false, "dropped": 0, "seats": [\(seats ?? defaultRouterSeats)]},
     "routerUnit": \(unit ?? unitJSON())}
    """
}

private func notAnsweringAnswer(code: String = "router_not_running", unit: String? = nil) -> String {
    """
    {"ok": false, "verb": "server router-health", "target": "server", "commands": [], "result": "unavailable",
     "detail": "the wake router is not listening on loopback 8794", "errorCode": "\(code)",
     "routerUnit": \(unit ?? "null")}
    """
}

private func answering(
    healthy: String = "false", seats: String? = nil, unit: String? = nil
) throws -> RouterHealthReading {
    let reading = RouterHealthDecoder.reading(result(healthAnswer(healthy: healthy, seats: seats, unit: unit)))
    guard case .answering = reading else { throw FixtureDidNotRead() }
    return reading
}

private struct FixtureDidNotRead: Error {}

private func testWakeAnswer(
    seat: String = "CLAUDE", passed: Bool = true, error: String? = nil, dryRun: Bool = true,
    picked: String = "true", addressed: String = "true", delivery: String = "false",
    answer: String? = nil
) -> String {
    let defaultAnswer = """
    {"test": true, "status": "test", "wake_id": "test-wake-20261112T180000Z-abcd1234", "receipt_id": "0123456789abcdef",
     "seat": "\(seat)", "letter": "II", "letter_live": true, "picked": true, "picked_letter": "II", "picked_generation": 2,
     "host": "mac", "reason": "letter", "delivered": false}
    """
    return """
    {"ok": true, "verb": "server router-test-wake", "target": "\(seat)", "commands": [], "result": "planned",
     "detail": "a dry run of one wake for the seat:  the router delivers nothing",
     "routerTestWake": {"schema": 1, "seat": "\(seat)", "passed": \(passed), "dry_run": \(dryRun),
       "picked": \(picked), "addressed": \(addressed), "http_status": 200,
       "error": \(error.map { "\"\($0)\"" } ?? "null"),
       "routine": {"url_env": "CLAUDE_ROUTINE_URL", "key_env": "WAKE_ROUTER_KEY", "auth": "bearer", "method": "POST", "host": "127.0.0.1:8794"},
       "router_health": {"healthy": true, "delivery": \(delivery), "reason": null},
       "answer": \(answer ?? defaultAnswer)}}
    """
}

private func report(_ text: String, seat: String = "CLAUDE") throws -> RouterTestWakeReport {
    guard case .report(let report) = RouterTestWakeDecoder.reading(result(text), seat: seat) else {
        throw FixtureDidNotRead()
    }
    return report
}

// MARK: - The route

final class RouterRouteTests: XCTestCase {
    /// Both reads go through the one server route every other read uses:  ssh to the helper's
    /// alias, `--` ending ssh's own options, and the helper's verb.  There is no sudo and no second
    /// ssh builder, and neither takes a flag that could change anything.
    func test_the_router_reads_use_the_one_server_route() throws {
        let health = builder().routerHealthInvocation()
        let dryRun = try builder().routerTestWakeInvocation(seat: "CLAUDE")
        let sessions = builder().operatorSessionsInvocation()
        XCTAssertEqual(Array(health.arguments.suffix(6)), ["--", "server", "router-health", "server", "--plan", "--json"])
        XCTAssertEqual(Array(dryRun.arguments.suffix(6)), ["--", "server", "router-test-wake", "CLAUDE", "--plan", "--json"])
        for invocation in [health, dryRun] {
            XCTAssertEqual(invocation.executable, sessions.executable)
            XCTAssertEqual(invocation.environment, sessions.environment)
            XCTAssertNil(invocation.standardInput)
            XCTAssertFalse(invocation.arguments.contains("sudo"))
            XCTAssertFalse(invocation.arguments.contains("--yes"))
            XCTAssertEqual(invocation.timeout, OpsControlRoute.planTimeout)
        }
        XCTAssertEqual(Array(health.arguments.dropLast(5)), Array(sessions.arguments.dropLast(5)))
    }

    /// Only a seat tag reaches the server:  nothing that could be read as a flag, a second verb or
    /// a shell word.
    func test_only_a_seat_tag_can_be_tested() {
        for good in ["CLAUDE", "AG", "GB-ROUTER", "Z_1", "7", String(repeating: "A", count: 32)] {
            XCTAssertNoThrow(try builder().routerTestWakeInvocation(seat: good), good)
        }
        for bad in ["", "claude", "A B", "-x", "A;B", "A\nB", "A$(x)", "..", "ÄÖ", String(repeating: "A", count: 33), "_A"] {
            XCTAssertThrowsError(try builder().routerTestWakeInvocation(seat: bad), bad)
        }
    }

    func test_the_readers_run_the_route_and_read_the_answer() async {
        let health = await RouterHealthReader(runner: FixedRunner(result: result(healthAnswer())), makeBuilder: { builder() }).read()
        XCTAssertEqual(health.document?.seats.count, 4)
        let dryRun = await RouterTestWakeReader(runner: FixedRunner(result: result(testWakeAnswer())), makeBuilder: { builder() }).run(seat: "CLAUDE")
        guard case .report(let report) = dryRun else { return XCTFail("\(dryRun)") }
        XCTAssertEqual(report.passed, true)
        let refused = await RouterTestWakeReader(runner: FixedRunner(result: result(testWakeAnswer())), makeBuilder: { builder() }).run(seat: "claude")
        XCTAssertEqual(refused, .unavailable("This seat's name cannot be tested."))
    }
}

// MARK: - Decoding the router's health

final class RouterHealthDecodingTests: XCTestCase {
    func test_a_full_answer_reads_the_seats_and_the_unit() throws {
        guard case .answering(let document, let unit) = RouterHealthDecoder.reading(result(healthAnswer())) else {
            return XCTFail("not answering")
        }
        XCTAssertEqual(document.healthy, false)
        XCTAssertEqual(document.seats.map(\.seat), ["AG", "CLAUDE", "CODEX", "MC"])
        let claude = document.seats[1]
        XCTAssertEqual(claude.installed, true)
        XCTAssertEqual(claude.healthy, true)
        XCTAssertEqual(claude.delivery, true)
        XCTAssertEqual(claude.registryConfigured, true)
        XCTAssertNil(claude.reason)
        XCTAssertEqual(document.seats[2].reason, "registry_not_configured")
        XCTAssertEqual(document.seats[3].installed, false)
        XCTAssertNil(document.seats[3].delivery)
        XCTAssertEqual(unit, RouterUnitState(activeState: "active", subState: "running", unitResult: "success"))
        XCTAssertEqual(document.installedSeats.map(\.seat), ["AG", "CLAUDE", "CODEX"])
    }

    /// An older helper answers the health and no unit:  the unit reads as not reported, never as active.
    func test_an_answer_with_no_unit_leaves_the_unit_unreported() throws {
        let text = healthAnswer(unit: "null")
        guard case .answering(_, let unit) = RouterHealthDecoder.reading(result(text)) else { return XCTFail("not answering") }
        XCTAssertNil(unit)
    }

    /// A key Muster does not know is ignored, a field of the wrong type reads as not reported, and a
    /// seat with no name is left out and costs no other.
    func test_unknown_fields_are_ignored_and_a_bad_field_costs_only_itself() throws {
        let seats = """
        {"seat": "CLAUDE", "installed": "yes", "healthy": 1, "delivery": null, "reason": 5, "future": {"a": 1}},
        {"installed": true},
        {"seat": "CODEX", "installed": true, "healthy": true, "delivery": true}
        """
        guard case .answering(let document, _) = RouterHealthDecoder.reading(result(healthAnswer(seats: seats))) else {
            return XCTFail("not answering")
        }
        XCTAssertEqual(document.seats.map(\.seat), ["CLAUDE", "CODEX"])
        XCTAssertNil(document.seats[0].installed)
        XCTAssertNil(document.seats[0].healthy)
        XCTAssertNil(document.seats[0].reason)
        XCTAssertEqual(document.seats[1].delivery, true)
    }

    func test_a_router_that_does_not_answer_keeps_the_unit_and_a_plain_cause() {
        let reading = RouterHealthDecoder.reading(
            result(notAnsweringAnswer(unit: unitJSON("failed", sub: "failed", result: "exit-code")), exit: 1))
        guard case .notAnswering(let cause, let unit) = reading else { return XCTFail("\(reading)") }
        XCTAssertEqual(cause, "Nothing is listening on the wake router's port on the server.")
        XCTAssertEqual(unit?.activeState, "failed")
        XCTAssertTrue(unit?.isDown == true)
        let noUnit = RouterHealthDecoder.reading(result(notAnsweringAnswer(), exit: 1))
        XCTAssertEqual(noUnit, .notAnswering(cause: "Nothing is listening on the wake router's port on the server.", unit: nil))
    }

    func test_every_error_code_the_helper_answers_has_a_sentence_and_no_raw_word() {
        for code in ["router_not_running", "timeout", "bad_status", "too_large", "not_json", "read_failed", "something_new"] {
            let reading = RouterHealthDecoder.reading(result(notAnsweringAnswer(code: code), exit: 1))
            guard case .notAnswering(let cause, _) = reading else { return XCTFail("\(code): \(reading)") }
            XCTAssertFalse(cause.contains("_"), cause)
            XCTAssertTrue(cause.hasSuffix("."), cause)
        }
    }

    /// An older helper refuses the verb:  that is not a router that is down.
    func test_an_older_helper_that_lacks_the_verb_is_said_plainly() {
        let text = #"{"ok": false, "verb": "server router-health", "target": "server", "commands": [], "result": "refused", "detail": "unknown server verb"}"#
        let reading = RouterHealthDecoder.reading(result(text, exit: 2))
        guard case .unavailable(let why) = reading else { return XCTFail("\(reading)") }
        XCTAssertEqual(why, "The server's control helper does not have this read yet, so the Operator's wake router state is not read.")
    }

    func test_a_route_that_failed_is_unreachable_and_never_a_raw_error() {
        XCTAssertEqual(RouterHealthDecoder.reading(result("", timedOut: true)), .unreachable("The server did not answer in time."))
        guard case .unreachable(let ssh) = RouterHealthDecoder.reading(result("", exit: 255)) else { return XCTFail("255") }
        XCTAssertTrue(ssh.contains("ssh"), ssh)
        guard case .unavailable(let failed) = RouterHealthDecoder.reading(result("Traceback (most recent call last)", exit: 1)) else { return XCTFail("exit 1") }
        XCTAssertEqual(failed, "The server's helper failed without an answer (exit 1).")
        XCTAssertFalse(failed.contains("Traceback"))
        guard case .unavailable(let odd) = RouterHealthDecoder.reading(result("not json")) else { return XCTFail("odd") }
        XCTAssertEqual(odd, "The server's answer could not be read.")
    }

    func test_an_answer_of_another_version_or_instance_or_verb_is_not_shown() {
        guard case .unavailable(let version) = RouterHealthDecoder.reading(result(healthAnswer(schema: 2))) else { return XCTFail("version") }
        XCTAssertTrue(version.contains("Update Muster"), version)
        guard case .unavailable(let instance) = RouterHealthDecoder.reading(result(healthAnswer(instance: "mac"))) else { return XCTFail("instance") }
        XCTAssertTrue(instance.contains("does not say it is from the server"), instance)
        let other = healthAnswer().replacingOccurrences(of: "server router-health", with: "server operator-wakes")
        guard case .unavailable(let verb) = RouterHealthDecoder.reading(result(other)) else { return XCTFail("verb") }
        XCTAssertTrue(verb.contains("another read"), verb)
        let noDocument = #"{"ok": true, "verb": "server router-health", "result": "planned"}"#
        XCTAssertEqual(RouterHealthDecoder.reading(result(noDocument)), .unavailable("The server answered without the wake router's health."))
    }
}

// MARK: - The card

final class RouterCardTests: XCTestCase {
    private let wakes: OperatorReading<OperatorWakesDocument> = .read(
        OperatorWakesDocument(totals: OperatorWakeTotals(recent: 3, heldBack: 0, heldBackLost: 0, ownerQueue: 0))
    )

    func test_before_the_first_read_the_card_says_reading() {
        let card = RouterPresentation.card(health: nil, wakes: wakes, now: now)
        XCTAssertEqual(card.state, "Reading\u{2026}")
        XCTAssertEqual(card.tone, .neutral)
        XCTAssertEqual(card.lines, [])
    }

    func test_a_router_that_answers_with_a_running_unit_says_running() throws {
        let allReady = #"""
        {"seat": "CLAUDE", "installed": true, "healthy": true, "delivery": true, "registry_configured": true, "reason": null},
        {"seat": "CODEX", "installed": true, "healthy": true, "delivery": false, "registry_configured": true, "reason": null},
        {"seat": "MC", "installed": false, "healthy": false, "delivery": null, "reason": "not_installed"}
        """#
        let card = RouterPresentation.card(health: try answering(healthy: "true", seats: allReady), wakes: wakes, now: now)
        XCTAssertEqual(card.title, "Wake router")
        XCTAssertEqual(card.state, "Running")
        XCTAssertEqual(card.tone, .good)
        XCTAssertEqual(card.lines, [
            "Its systemd unit is running.",
            "Serves 2 seats.\u{00A0} Delivery is on for 1.",
            "Every seat is ready.",
            "3 wakes in the last 24 hours.",
        ])
    }

    func test_a_seat_that_is_not_ready_is_counted_and_the_card_is_not_green() throws {
        let card = RouterPresentation.card(health: try answering(), wakes: nil, now: now)
        XCTAssertEqual(card.state, "Running")
        XCTAssertEqual(card.tone, .warning)
        XCTAssertEqual(card.lines, [
            "Its systemd unit is running.",
            "Serves 3 seats.\u{00A0} Delivery is on for 2.",
            "1 seat is not ready.",
        ])
    }

    /// The router answers, so it runs, and a unit that is not active is said beside it and never
    /// left out:  a router started by hand is not the unit's.
    func test_a_router_that_answers_while_its_unit_is_down_is_running_with_a_warning() throws {
        let card = RouterPresentation.card(health: try answering(healthy: "true", unit: unitJSON("inactive", sub: "dead")), wakes: nil, now: now)
        XCTAssertEqual(card.state, "Running")
        XCTAssertEqual(card.tone, .warning)
        XCTAssertEqual(card.lines.first, "It answers, but its systemd unit is stopped.")
        let unknown = RouterPresentation.card(health: try answering(healthy: "true", unit: "null"), wakes: nil, now: now)
        XCTAssertEqual(unknown.state, "Running")
        XCTAssertEqual(unknown.lines.first, "Its systemd unit's state is not reported.")
    }

    func test_a_router_with_no_verdict_is_neutral_not_green() throws {
        let card = RouterPresentation.card(health: try answering(healthy: "null"), wakes: nil, now: now)
        XCTAssertEqual(card.state, "Running")
        XCTAssertEqual(card.tone, .neutral)
    }

    func test_a_router_that_does_not_answer_with_a_down_unit_is_stopped() {
        for (active, sub, word) in [("inactive", "dead", "stopped"), ("failed", "failed", "failed")] {
            let reading = RouterHealthDecoder.reading(result(notAnsweringAnswer(unit: unitJSON(active, sub: sub)), exit: 1))
            let card = RouterPresentation.card(health: reading, wakes: nil, now: now)
            XCTAssertEqual(card.state, "Stopped", active)
            XCTAssertEqual(card.tone, .bad)
            XCTAssertEqual(card.lines, ["Its systemd unit is \(word).", "Nothing is listening on the wake router's port on the server."])
        }
    }

    /// Stopped needs the unit to say so:  a router that does not answer while its unit is active, or
    /// while the unit's state is not known, is not reporting, and is never called stopped or running.
    func test_a_router_that_does_not_answer_otherwise_is_not_reporting() {
        let active = RouterHealthDecoder.reading(result(notAnsweringAnswer(unit: unitJSON()), exit: 1))
        let activeCard = RouterPresentation.card(health: active, wakes: nil, now: now)
        XCTAssertEqual(activeCard.state, "Not reporting")
        XCTAssertEqual(activeCard.tone, .bad)
        XCTAssertEqual(activeCard.lines.first, "Its systemd unit is running, and the router does not answer.")
        let starting = RouterHealthDecoder.reading(result(notAnsweringAnswer(unit: unitJSON("activating", sub: "start")), exit: 1))
        XCTAssertEqual(RouterPresentation.card(health: starting, wakes: nil, now: now).state, "Not reporting")
        let none = RouterHealthDecoder.reading(result(notAnsweringAnswer(), exit: 1))
        let noneCard = RouterPresentation.card(health: none, wakes: nil, now: now)
        XCTAssertEqual(noneCard.state, "Not reporting")
        XCTAssertEqual(noneCard.lines.first, "Its systemd unit's state is not reported.")
    }

    func test_a_failed_read_says_not_reporting_with_its_own_cause() {
        let down = RouterPresentation.card(health: .unreachable("The server did not answer in time."), wakes: nil, now: now)
        XCTAssertEqual(down.state, "Not reporting")
        XCTAssertEqual(down.tone, .warning)
        XCTAssertEqual(down.lines, ["The server did not answer in time."])
        let old = RouterPresentation.card(health: .unavailable("The server's control helper does not have this read yet."), wakes: nil, now: now)
        XCTAssertEqual(old.state, "Not reporting")
    }

    func test_the_wakes_line_counts_one_a_floor_a_missing_count_and_a_failed_read() throws {
        func lines(_ wakes: OperatorReading<OperatorWakesDocument>?) throws -> [String] {
            RouterPresentation.card(health: try answering(healthy: "true"), wakes: wakes, now: now).lines
        }
        let one = OperatorReading.read(OperatorWakesDocument(totals: OperatorWakeTotals(recent: 1)))
        XCTAssertEqual(try lines(one).last, "1 wake in the last 24 hours.")
        let floor = OperatorReading.read(OperatorWakesDocument(totals: OperatorWakeTotals(recent: 3), unreadable: ["GROK"]))
        XCTAssertEqual(try lines(floor).last, "At least 3 wakes in the last 24 hours.")
        let none = OperatorReading.read(OperatorWakesDocument(totals: nil))
        XCTAssertEqual(try lines(none).last, "The number of wakes in the last 24 hours is not reported.")
        // A wake record that failed adds nothing, and costs the router card nothing else.
        XCTAssertFalse(try lines(.unavailable("The Operator's wake record was busy.")).contains { $0.contains("wake") && $0.contains("hours") })
        XCTAssertEqual(RouterPresentation.card(health: try answering(), wakes: .unreachable("x"), now: now).state, "Running")
    }
}

// MARK: - The seats

final class RouterSeatRowTests: XCTestCase {
    private func wakes() -> OperatorReading<OperatorWakesDocument> {
        .read(OperatorWakesDocument(
            totals: OperatorWakeTotals(recent: 3),
            seats: [
                OperatorWakeSeat(seat: "CLAUDE", recentTotal: 2, recent: [
                    OperatorWake(wakeID: "a", state: "delivered", at: at(600)),
                    OperatorWake(wakeID: "b", state: "held_back", at: at(120), reason: "wakes_per_hour"),
                ]),
                OperatorWakeSeat(seat: "AG", recentTotal: 0, recent: []),
            ],
            unreadable: ["CODEX"]
        ))
    }

    func test_every_seat_the_router_reports_has_a_row_in_its_order() throws {
        let rows = RouterPresentation.seatRows(health: try answering(), wakes: wakes(), now: now)
        XCTAssertEqual(rows.map(\.seat), ["AG", "CLAUDE", "CODEX", "MC"])
        let byseat = Dictionary(uniqueKeysWithValues: rows.map { ($0.seat, $0) })
        XCTAssertEqual(byseat["CLAUDE"]?.state.text, "Ready")
        XCTAssertEqual(byseat["CLAUDE"]?.state.tone, .good)
        XCTAssertEqual(byseat["CLAUDE"]?.delivery.text, "On")
        XCTAssertEqual(byseat["AG"]?.delivery.text, "Off")
        XCTAssertTrue(byseat["AG"]?.delivery.help.contains("held") == true)
        XCTAssertEqual(byseat["CODEX"]?.state.text, "Registry not set")
        XCTAssertEqual(byseat["CODEX"]?.state.tone, .warning)
        XCTAssertEqual(byseat["MC"]?.state.text, "Not installed")
        XCTAssertEqual(byseat["MC"]?.delivery.text, "Not reported")
    }

    /// Only a seat the router has installed can be tested:  the router answers any other seat 503.
    func test_only_an_installed_seat_can_be_tested() throws {
        let rows = RouterPresentation.seatRows(health: try answering(), wakes: nil, now: now)
        XCTAssertEqual(rows.filter(\.canTest).map(\.seat), ["AG", "CLAUDE", "CODEX"])
    }

    func test_the_last_wake_says_the_newest_outcome_or_why_there_is_none() throws {
        let rows = RouterPresentation.seatRows(health: try answering(), wakes: wakes(), now: now)
        let byseat = Dictionary(uniqueKeysWithValues: rows.map { ($0.seat, $0.lastWake) })
        XCTAssertEqual(byseat["CLAUDE"], "Held back, 2m ago")
        XCTAssertEqual(byseat["AG"], "None in 24 hours")
        XCTAssertEqual(byseat["CODEX"], "Not read")
        XCTAssertEqual(byseat["MC"], "None in 24 hours")
        let unread = RouterPresentation.seatRows(health: try answering(), wakes: nil, now: now)
        XCTAssertEqual(unread.first?.lastWake, "Not read yet")
        let failed = RouterPresentation.seatRows(health: try answering(), wakes: .unavailable("x"), now: now)
        XCTAssertEqual(failed.first?.lastWake, "Not read")
    }

    func test_no_row_is_made_up_while_the_router_has_not_answered() {
        XCTAssertEqual(RouterPresentation.seatRows(health: nil, wakes: nil, now: now), [])
        XCTAssertEqual(RouterPresentation.seatRows(health: .unreachable("x"), wakes: nil, now: now), [])
        let down = RouterHealthDecoder.reading(result(notAnsweringAnswer(), exit: 1))
        XCTAssertEqual(RouterPresentation.seatRows(health: down, wakes: nil, now: now), [])
    }
}

// MARK: - The dry run

final class RouterTestWakeTests: XCTestCase {
    func test_a_passing_dry_run_says_which_chat_it_would_wake_and_that_it_delivers_nothing() throws {
        let summary = RouterPresentation.summary(.report(try report(testWakeAnswer(delivery: "true"))))
        XCTAssertEqual(summary.headline, "The dry run passed")
        XCTAssertEqual(summary.tone, .good)
        XCTAssertEqual(summary.lines, [
            "The router would wake chat II.2 on the Mac.",
            "Why: the sender addressed its letter.",
            RouterPresentation.dryRunNote,
        ])
    }

    func test_a_held_seat_says_a_real_wake_would_be_held() throws {
        let summary = RouterPresentation.summary(.report(try report(testWakeAnswer(delivery: "false"))))
        XCTAssertTrue(summary.lines.contains("Delivery is off for this seat, so a real wake would be held."))
        let on = RouterPresentation.summary(.report(try report(testWakeAnswer(delivery: "true"))))
        XCTAssertFalse(on.lines.contains { $0.contains("held") })
    }

    func test_a_dry_run_that_picked_no_chat_fails_with_the_reason() throws {
        let answer = #"{"test": true, "status": "test", "picked": false, "picked_letter": "", "picked_generation": 0, "host": "", "reason": "no-live-session", "delivered": false}"#
        let text = testWakeAnswer(passed: false, picked: "false", answer: answer)
        let summary = RouterPresentation.summary(.report(try report(text)))
        XCTAssertEqual(summary.headline, "The dry run did not pass")
        XCTAssertEqual(summary.tone, .warning)
        XCTAssertEqual(summary.lines.first, "The router answered and found no chat to wake.\u{00A0} Why: no live chat is registered for the seat.")
        XCTAssertEqual(summary.lines.last, RouterPresentation.dryRunNote)
    }

    func test_a_dry_run_that_picked_another_chat_than_the_addressed_one_says_so() throws {
        let text = testWakeAnswer(passed: false, addressed: "false")
        let summary = RouterPresentation.summary(.report(try report(text)))
        XCTAssertEqual(summary.lines.first, "The router picked chat II.2 on the Mac, not the chat that was addressed.")
    }

    /// The three words the command's own error can carry are said as sentences, and any other is
    /// said without its raw text.
    func test_a_dry_run_that_could_not_send_says_why_in_plain_words() throws {
        let cases: [(String, String)] = [
            ("not_the_router", "This seat's wake route does not point at the wake router, so nothing was sent."),
            ("router_unreachable", "The router did not answer, so nothing was sent."),
            ("routine_not_configured", "This seat has no wake route configured on the server, so nothing was sent."),
            ("other", "The router did not accept the dry run."),
        ]
        for (code, sentence) in cases {
            let text = testWakeAnswer(passed: false, error: code, dryRun: false, picked: "false", addressed: "false", answer: "{}")
            let summary = RouterPresentation.summary(.report(try report(text)))
            XCTAssertEqual(summary.lines.first, sentence, code)
            XCTAssertFalse(summary.lines.joined().contains("_"), code)
            XCTAssertEqual(summary.lines.last, RouterPresentation.dryRunNote)
        }
    }

    func test_a_dry_run_that_did_not_run_gives_the_cause_and_no_dry_run_claim() {
        let summary = RouterPresentation.summary(.unreachable("The server did not answer in time."))
        XCTAssertEqual(summary, RouterTestWakeSummary(headline: "The dry run did not run", tone: .warning, lines: ["The server did not answer in time."]))
    }

    func test_the_decoder_refuses_what_is_not_this_seats_or_this_versions() {
        guard case .unavailable(let other) = RouterTestWakeDecoder.reading(result(testWakeAnswer(seat: "AG")), seat: "CLAUDE") else { return XCTFail("seat") }
        XCTAssertTrue(other.contains("another seat"), other)
        let v2 = testWakeAnswer().replacingOccurrences(of: #""schema": 1"#, with: #""schema": 2"#)
        guard case .unavailable(let version) = RouterTestWakeDecoder.reading(result(v2), seat: "CLAUDE") else { return XCTFail("version") }
        XCTAssertTrue(version.contains("Update Muster"), version)
        XCTAssertEqual(RouterTestWakeDecoder.reading(result("", timedOut: true), seat: "CLAUDE"), .unreachable("The server did not answer in time."))
        guard case .unreachable = RouterTestWakeDecoder.reading(result("", exit: 255), seat: "CLAUDE") else { return XCTFail("255") }
    }

    func test_every_error_code_the_helper_answers_has_a_sentence_and_no_raw_word() {
        for code in ["docker_unavailable", "timeout", "container_not_running", "seat_unknown", "too_large", "not_json", "read_failed", "newer"] {
            let text = #"{"ok": false, "verb": "server router-test-wake", "result": "unavailable", "errorCode": "\#(code)"}"#
            guard case .unavailable(let why) = RouterTestWakeDecoder.reading(result(text, exit: 1), seat: "CLAUDE") else { return XCTFail(code) }
            XCTAssertFalse(why.contains("_"), why)
            XCTAssertTrue(why.hasSuffix("."), why)
        }
        let old = #"{"ok": false, "verb": "server router-test-wake", "result": "refused", "detail": "unknown server verb"}"#
        XCTAssertEqual(RouterTestWakeDecoder.reading(result(old, exit: 2), seat: "CLAUDE"), .unavailable("The server's control helper does not have the dry run yet."))
    }

    /// A router that names who it picked with a word, not a flag, still reads as picked.
    func test_the_picked_field_reads_as_a_flag_or_a_word() throws {
        let word = testWakeAnswer(answer: #"{"test": true, "picked": "II", "picked_letter": "II"}"#)
        XCTAssertEqual(try report(word).answer?.picked, true)
        let empty = testWakeAnswer(answer: #"{"test": true, "picked": ""}"#)
        XCTAssertEqual(try report(empty).answer?.picked, false)
    }

    @MainActor
    func test_the_store_runs_one_dry_run_per_seat_at_a_time_and_keeps_the_last() async throws {
        let gate = DryRunGate()
        let reading = RouterTestWakeReading.report(try report(testWakeAnswer()))
        let store = RouterTestWakeStore(run: { _ in
            await gate.wait()
            return reading
        }, now: { now })
        XCTAssertFalse(store.isRunning("CLAUDE"))
        async let first: Void = store.test(seat: "CLAUDE")
        while store.runsStarted == 0 { await Task.yield() }
        XCTAssertTrue(store.isRunning("CLAUDE"))
        XCTAssertEqual(store.states["CLAUDE"], .running)
        // A second request for the same seat while it runs starts nothing.
        await store.test(seat: "CLAUDE")
        XCTAssertEqual(store.runsStarted, 1)
        await gate.open()
        _ = await first
        XCTAssertFalse(store.isRunning("CLAUDE"))
        XCTAssertEqual(store.states["CLAUDE"], .finished(reading, at: now))
        // The next request replaces the kept result.
        await store.test(seat: "CLAUDE")
        XCTAssertEqual(store.runsStarted, 2)
    }
}

private actor DryRunGate {
    private var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if isOpen { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func open() {
        isOpen = true
        for waiter in waiters { waiter.resume() }
        waiters.removeAll()
    }
}

// MARK: - The panel's store and its words

@MainActor
final class RouterStoreTests: XCTestCase {
    func test_a_refresh_stores_the_router_and_counts_it_as_read() async throws {
        let health = try answering()
        let store = OperatorPanelStore(
            statusRead: { .notRead("x") },
            sessionsRead: { .unreachable("y") },
            wakesRead: { .unreachable("z") },
            routerRead: { health },
            now: { now }
        )
        XCTAssertNil(store.router)
        await store.refresh()
        XCTAssertEqual(store.router, health)
        // The router answered, so the refresh read something.
        XCTAssertEqual(store.readAt, now)
    }

    /// A router that is not running is still an answer from the server, and a route that failed is not.
    func test_a_stopped_router_is_a_read_and_a_failed_route_is_not() async {
        let stopped = RouterHealthDecoder.reading(result(notAnsweringAnswer(unit: unitJSON("inactive", sub: "dead")), exit: 1))
        let down = OperatorPanelStore(
            statusRead: { .notRead("x") }, sessionsRead: { .unreachable("y") },
            wakesRead: { .unreachable("z") }, routerRead: { stopped }, now: { now })
        await down.refresh()
        XCTAssertEqual(down.readAt, now)
        let route = OperatorPanelStore(
            statusRead: { .notRead("x") }, sessionsRead: { .unreachable("y") },
            wakesRead: { .unreachable("z") }, routerRead: { .unreachable("w") }, now: { now })
        await route.refresh()
        XCTAssertNil(route.readAt)
        XCTAssertEqual(route.triedAt, now)
    }

    func test_a_failed_router_read_does_not_blank_the_others() async throws {
        let wakes = OperatorWakesDocument(totals: OperatorWakeTotals(recent: 2))
        let store = OperatorPanelStore(
            statusRead: { .notRead("x") },
            sessionsRead: { .unavailable("y") },
            wakesRead: { .read(wakes) },
            routerRead: { .unavailable("The router was busy.") }
        )
        await store.refresh()
        XCTAssertEqual(store.router, .unavailable("The router was busy."))
        XCTAssertEqual(store.wakes, .read(wakes))
    }
}

final class RouterCopyTests: XCTestCase {
    /// Shipped copy keeps a real U+00A0 and a space between sentences, never two plain spaces, and
    /// never says successfully or shows a raw exception or a raw code word.
    func test_router_copy_has_the_sentence_gap_and_no_forbidden_words() throws {
        var copy: [String] = [RouterPresentation.heading, RouterPresentation.dryRunNote]
        for code in ["router_not_running", "timeout", "bad_status", "too_large", "not_json", "read_failed", "nope"] {
            copy.append(RouterHealthDecoder.cause(errorCode: code, detail: nil))
        }
        for code in ["docker_unavailable", "timeout", "container_not_running", "seat_unknown", "too_large", "read_failed", "nope"] {
            copy.append(RouterTestWakeDecoder.cause(errorCode: code, detail: nil))
        }
        let readings: [RouterHealthReading] = [
            try answering(), try answering(healthy: "true", unit: unitJSON("failed")),
            RouterHealthDecoder.reading(result(notAnsweringAnswer(unit: unitJSON("failed")), exit: 1)),
            .unreachable("x"), .unavailable("y"),
        ]
        for reading in readings {
            let card = RouterPresentation.card(health: reading, wakes: nil, now: now)
            copy.append(card.state)
            copy.append(contentsOf: card.lines)
            for row in RouterPresentation.seatRows(health: reading, wakes: nil, now: now) {
                copy.append(contentsOf: [row.state.text, row.state.help, row.delivery.text, row.delivery.help, row.lastWake])
            }
        }
        for text in [testWakeAnswer(), testWakeAnswer(passed: false, addressed: "false"),
                     testWakeAnswer(passed: false, error: "not_the_router", dryRun: false, answer: "{}")] {
            let summary = RouterPresentation.summary(.report(try report(text)))
            copy.append(summary.headline)
            copy.append(contentsOf: summary.lines)
        }
        for text in copy {
            XCTAssertFalse(text.contains(".  "), text)
            XCTAssertFalse(text.contains(":  "), text)
            XCTAssertFalse(text.lowercased().contains("successfully"), text)
            XCTAssertFalse(text.lowercased().contains("exception"), text)
            XCTAssertFalse(text.contains("not_the_router"), text)
        }
        XCTAssertEqual(RouterPresentation.heading, "Wake Router")
        XCTAssertTrue(RouterPresentation.dryRunNote.contains("dry run"))
    }
}
