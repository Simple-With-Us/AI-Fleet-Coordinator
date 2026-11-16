import XCTest

// The Operator panel's reads, cards, tables and store.  The answers are the shapes the server's
// control helper writes (`scripts/fleet-ops-control.py`, `reduce_operator_sessions` and
// `reduce_operator_wakes`, and the reduced status document), with `now` at noon on Nov 12, 2026
// in Chicago.

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

private func sessionsAnswer(counts: String = #"{"active": 3, "closed": 5}"#, truncated: Bool = false, rows: String? = nil) -> String {
    let defaultRows = """
    {"letter": "II", "generation": 2, "seat": "CLAUDE", "platform": "claude", "host": "mac", "host_code": "M",
     "status": "active", "wake": "none", "registered_at": "\(at(7200))", "last_seen_at": "\(at(600))", "issues": ["AFC-704"]},
    {"letter": null, "generation": null, "seat": "CODEX", "platform": "codex", "host": "server", "host_code": "S",
     "status": "active", "wake": "code-supported", "registered_at": "\(at(7200))", "last_seen_at": "\(at(30))", "issues": []},
    {"letter": "B", "generation": 1, "seat": "CURSOR", "platform": "cursor", "host": "cloud", "host_code": "C",
     "status": "active", "wake": "unknown", "registered_at": "\(at(9000))", "last_seen_at": "\(at(3000))", "issues": []}
    """
    return """
    {"ok": true, "verb": "server operator-sessions", "target": "server", "commands": [], "result": "planned",
     "detail": "Read the Operator's letter registry.",
     "sessions": {"schema": 1, "generated_at": "\(at(1))", "instance": "server", "counts": \(counts),
                  "truncated": \(truncated), "dropped": 0, "sessions": [\(rows ?? defaultRows)]}}
    """
}

private func wakesAnswer(totals: String = #"{"recent": 3, "held_back": 1, "held_back_lost": 0, "owner_queue": 1}"#,
                         seats: String? = nil, unreadable: String = "[]", truncated: Bool = false) -> String {
    let defaultSeats = """
    {"seat": "CLAUDE",
     "recent": {"total": 2, "by_state": {"done": 1, "held_back": 1},
                "rows": [{"wake_id": "w-1", "state": "held_back", "at": "\(at(60))", "reason": "wakes_per_hour",
                          "dm": false, "owner": true, "catch_up": false, "message_ids": [639558944]},
                         {"wake_id": "w-0", "state": "done", "at": "\(at(900))", "reason": null,
                          "dm": true, "owner": false, "catch_up": false, "message_ids": []}]},
     "held_back": {"count": 1, "lost": 0, "reasons": {"wakes_per_hour": 1}, "rows": []},
     "owner_queue": {"pending": 1, "rows": []}},
    {"seat": "CODEX",
     "recent": {"total": 1, "by_state": {"done": 1},
                "rows": [{"wake_id": "w-9", "state": "done", "at": "\(at(300))", "reason": null,
                          "dm": false, "owner": false, "catch_up": true, "message_ids": []}]},
     "held_back": {"count": 0, "lost": 0, "reasons": {}, "rows": []},
     "owner_queue": {"pending": 0, "rows": []}}
    """
    return """
    {"ok": true, "verb": "server operator-wakes", "target": "server", "commands": [], "result": "planned",
     "detail": "Read the Operator's wake record.",
     "wakes": {"schema": 1, "generated_at": "\(at(1))", "instance": "server", "window_hours": 24,
               "totals": \(totals), "truncated": \(truncated), "seats": [\(seats ?? defaultSeats)],
               "unreadable": \(unreadable)}}
    """
}

private func unavailableAnswer(verb: String, code: String) -> String {
    """
    {"ok": false, "verb": "\(verb)", "target": "server", "commands": [], "result": "unavailable",
     "detail": "the Operator's store was locked by a writer", "errorCode": "\(code)"}
    """
}

private func statusDocument(
    updatedAgo: Int = 3, paused: String = "false", wakesOnly: String = "false",
    seats: String? = nil, sweep: String = "null", voice: String = #"{"available": true, "code": null, "since": null}"#,
    pauseFileError: String = "null"
) -> String {
    let defaultSeats = """
    {"seat": "CLAUDE", "bot": "claude-bot", "connected": true, "running": "running", "wake": "http", "paused": false,
     "wakes_24h": 2, "held_back": 0, "jobs": 0, "last_event": "\(at(20))",
     "routine": {"ready": true, "auth": "bearer", "why": null}},
    {"seat": "CODEX", "bot": "codex-bot", "connected": true, "running": "running", "wake": "http", "paused": false,
     "wakes_24h": 1, "held_back": 0, "jobs": 0, "last_event": "\(at(500))",
     "routine": {"ready": false, "auth": null, "why": "routine_not_configured"}},
    {"seat": "CURSOR", "bot": "cursor-bot", "connected": true, "running": "running", "wake": "inbox", "paused": false,
     "wakes_24h": 0, "held_back": 0, "jobs": 0, "last_event": null, "routine": null}
    """
    return """
    {"schema": 1, "generated_at": "\(at(5))", "instance": "server", "status_updated": "\(at(updatedAgo))",
     "operator_voice": \(voice),
     "pause": {"paused": \(paused), "since": "\(at(1800))", "wakes_only": \(wakesOnly), "set_by": "cli"},
     "pause_file_error": \(pauseFileError),
     "seats": [\(seats ?? defaultSeats)], "skipped": [], "disabled": [],
     "registry_sweep": \(sweep)}
    """
}

private func statusReading(_ text: String) throws -> ServerEavesdropperReading {
    let status = try JSONDecoder().decode(ReducedEavesdropperStatus.self, from: Data(text.utf8))
    return ServerEavesdropperReading.reading(of: status)
}

private struct FixtureDidNotRead: Error {}

private func sessionsDocument(_ text: String = sessionsAnswer()) throws -> OperatorSessionsDocument {
    guard case .read(let document) = OperatorSessionsDecoder.reading(result(text)) else {
        throw FixtureDidNotRead()
    }
    return document
}

private func wakesDocument(_ text: String = wakesAnswer()) throws -> OperatorWakesDocument {
    guard case .read(let document) = OperatorWakesDecoder.reading(result(text)) else {
        throw FixtureDidNotRead()
    }
    return document
}

// MARK: - The route

final class OperatorRouteTests: XCTestCase {
    /// Both verbs go through the one server route every other read uses:  ssh to the helper's
    /// alias, `--` ending ssh's own options, and the helper's verb.  There is no sudo and no second
    /// ssh builder.
    func test_the_reads_use_the_one_server_route() {
        let seats = builder().operatorSessionsInvocation()
        let wakes = builder().operatorWakesInvocation()
        let settings = builder().listenerSettingsReadInvocation()
        XCTAssertEqual(Array(seats.arguments.suffix(6)), ["--", "server", "operator-sessions", "server", "--plan", "--json"])
        XCTAssertEqual(Array(wakes.arguments.suffix(6)), ["--", "server", "operator-wakes", "server", "--plan", "--json"])
        for invocation in [seats, wakes] {
            XCTAssertEqual(invocation.executable, settings.executable)
            XCTAssertEqual(invocation.environment, settings.environment)
            XCTAssertNil(invocation.standardInput)
            XCTAssertFalse(invocation.arguments.contains("sudo"))
            XCTAssertEqual(invocation.timeout, OpsControlRoute.planTimeout)
        }
        // The same ssh options and alias lead every server read.
        XCTAssertEqual(Array(seats.arguments.dropLast(5)), Array(settings.arguments.dropLast(5)))
    }

    func test_a_reader_runs_the_route_and_reads_the_answer() async {
        let sessions = await OperatorSessionsReader(runner: FixedRunner(result: result(sessionsAnswer())), makeBuilder: { builder() }).read()
        XCTAssertEqual(sessions.value?.sessions.count, 3)
        let wakes = await OperatorWakesReader(runner: FixedRunner(result: result(wakesAnswer())), makeBuilder: { builder() }).read()
        XCTAssertEqual(wakes.value?.seats.count, 2)
    }
}

// MARK: - Decoding

final class OperatorDecodingTests: XCTestCase {
    func test_a_full_sessions_answer_reads_every_field() throws {
        let document = try sessionsDocument()
        XCTAssertEqual(document.activeCount, 3)
        XCTAssertEqual(document.closedCount, 5)
        XCTAssertEqual(document.truncated, false)
        XCTAssertEqual(document.sessions.map(\.seat), ["CLAUDE", "CODEX", "CURSOR"])
        let first = document.sessions[0]
        XCTAssertEqual(first.letter, "II")
        XCTAssertEqual(first.generation, 2)
        XCTAssertEqual(first.hostCode, "M")
        XCTAssertEqual(first.status, "active")
        XCTAssertEqual(first.wake, "none")
        XCTAssertEqual(first.issues, ["AFC-704"])
        XCTAssertNotNil(first.lastSeen)
        XCTAssertNil(document.sessions[1].letter)
        XCTAssertNil(document.sessions[1].generation)
    }

    func test_an_empty_sessions_answer_is_an_empty_registry() throws {
        let text = sessionsAnswer(counts: #"{"active": 0, "closed": 0}"#, rows: "")
        let document = try sessionsDocument(text)
        XCTAssertEqual(document.sessions, [])
        XCTAssertEqual(document.activeCount, 0)
    }

    /// A key Muster does not know is ignored, a field of the wrong type reads as not reported, a
    /// row with no seat is left out and costs no other row.
    func test_unknown_fields_are_ignored_and_a_bad_field_costs_only_itself() throws {
        let rows = """
        {"letter": "A", "generation": "two", "seat": "CLAUDE", "status": "active", "wake": "none",
         "title": "a title the server never sends", "future": {"a": 1}, "last_seen_at": 12, "issues": "no"},
        {"letter": "Q", "status": "active"},
        {"letter": "C", "seat": "CODEX", "status": 5, "wake": ["x"]}
        """
        let text = sessionsAnswer(rows: rows).replacingOccurrences(of: #""commands": [],"#, with: #""commands": [], "extra": true,"#)
        let document = try sessionsDocument(text)
        XCTAssertEqual(document.sessions.map(\.seat), ["CLAUDE", "CODEX"])
        XCTAssertNil(document.sessions[0].generation)
        XCTAssertNil(document.sessions[0].lastSeen)
        XCTAssertEqual(document.sessions[0].issues, [])
        XCTAssertNil(document.sessions[1].status)
        XCTAssertNil(document.sessions[1].wake)
    }

    func test_a_sessions_answer_without_counts_says_not_reported() throws {
        let text = sessionsAnswer(counts: "null")
        let document = try sessionsDocument(text)
        XCTAssertNil(document.activeCount)
        XCTAssertNil(document.closedCount)
    }

    func test_an_unavailable_answer_gives_its_error_code_in_plain_words() {
        let reading = OperatorSessionsDecoder.reading(
            result(unavailableAnswer(verb: "server operator-sessions", code: "store_busy"), exit: 1))
        guard case .unavailable(let why) = reading else { return XCTFail("\(reading)") }
        XCTAssertTrue(why.contains("busy"), why)
        XCTAssertFalse(why.contains("store_busy"))
        let wakes = OperatorWakesDecoder.reading(
            result(unavailableAnswer(verb: "server operator-wakes", code: "container_not_running"), exit: 1))
        XCTAssertEqual(wakes, .unavailable("The Eavesdropper is not running on the server."))
    }

    func test_every_error_code_the_helper_answers_has_a_sentence_and_no_raw_word() {
        for code in ["docker_unavailable", "timeout", "container_not_running", "read_failed", "too_large",
                     "not_json", "store_missing", "store_busy", "store_recovering", "reader_failed"] {
            let said = OperatorUnavailable.cause(errorCode: code, detail: "a detail", what: "letter registry")
            XCTAssertNotEqual(said, "a detail", code)
            XCTAssertFalse(said.contains(code), "\(code): \(said)")
            XCTAssertTrue(said.hasSuffix("."), said)
        }
        let unknownCode = OperatorUnavailable.cause(errorCode: "brand_new", detail: "The server said this.", what: "wake record")
        XCTAssertEqual(unknownCode, "The server said this.")
    }

    func test_an_older_helper_that_lacks_the_verb_is_said_plainly() {
        let refused = #"{"ok": false, "verb": "", "target": "server", "commands": [], "result": "refused", "detail": "unknown server verb"}"#
        guard case .unavailable(let why) = OperatorSessionsDecoder.reading(result(refused, exit: 2)) else {
            return XCTFail("not unavailable")
        }
        XCTAssertTrue(why.contains("does not have this read yet"), why)
        XCTAssertFalse(why.lowercased().contains("exception"))
    }

    func test_a_route_that_failed_is_unreachable_and_never_a_raw_error() {
        // Only exit 255 is ssh failing to connect.
        XCTAssertEqual(OperatorSessionsDecoder.reading(result("", exit: 255)).isUnreachable, true)
        // Any other failure without an answer reached the server:  its helper failed, and the
        // route is not blamed.
        let helperFailed = OperatorSessionsDecoder.reading(result("", exit: 1))
        XCTAssertEqual(helperFailed.isUnreachable, false)
        XCTAssertEqual(helperFailed, .unavailable("The server's helper failed without an answer (exit 1)."))
        XCTAssertEqual(OperatorWakesDecoder.reading(result("", exit: 0, timedOut: true)),
                       .unreachable("The server did not answer in time."))
        // The server answered, but with something that is not an answer.
        XCTAssertEqual(OperatorWakesDecoder.reading(result("not json at all")),
                       .unavailable("The server's answer could not be read."))
    }

    func test_an_answer_of_another_version_or_instance_or_verb_is_not_shown() {
        let newer = sessionsAnswer().replacingOccurrences(of: #""schema": 1"#, with: #""schema": 2"#)
        guard case .unavailable(let why) = OperatorSessionsDecoder.reading(result(newer)) else { return XCTFail("shown") }
        XCTAssertTrue(why.contains("version 2"), why)
        let mac = sessionsAnswer().replacingOccurrences(of: #""instance": "server""#, with: #""instance": "mac""#)
        XCTAssertNotNil(OperatorSessionsDecoder.reading(result(mac)).failure)
        let wrongVerb = sessionsAnswer().replacingOccurrences(of: "server operator-sessions", with: "server operator-wakes")
        XCTAssertNotNil(OperatorSessionsDecoder.reading(result(wrongVerb)).failure)
        let noDocument = #"{"ok": true, "verb": "server operator-wakes", "result": "planned"}"#
        XCTAssertEqual(OperatorWakesDecoder.reading(result(noDocument)),
                       .unavailable("The server answered without the Operator's wake record."))
    }

    func test_a_full_wakes_answer_reads_totals_seats_and_rows() throws {
        let document = try wakesDocument()
        XCTAssertEqual(document.totals, OperatorWakeTotals(recent: 3, heldBack: 1, heldBackLost: 0, ownerQueue: 1))
        XCTAssertEqual(document.windowHours, 24)
        XCTAssertEqual(document.seats.map(\.seat), ["CLAUDE", "CODEX"])
        XCTAssertEqual(document.seats[0].recentTotal, 2)
        XCTAssertEqual(document.seats[0].recent.map(\.state), ["held_back", "done"])
        XCTAssertEqual(document.seats[0].recent[0].reason, "wakes_per_hour")
        XCTAssertEqual(document.seats[0].recent[0].owner, true)
        XCTAssertEqual(document.seats[1].recent[0].catchUp, true)
    }

    func test_an_empty_wakes_answer_and_one_with_extra_fields_read() throws {
        let empty = try wakesDocument(wakesAnswer(totals: #"{"recent": 0, "held_back": 0, "held_back_lost": 0, "owner_queue": 0}"#, seats: ""))
        XCTAssertEqual(empty.seats, [])
        XCTAssertEqual(empty.totals?.recent, 0)
        let odd = try wakesDocument(wakesAnswer(totals: #"{"recent": "many", "extra": 1}"#,
                                                seats: #"{"seat": "CLAUDE", "newkey": [1], "recent": {"total": 1, "rows": [{"wake_id": "a", "at": 7, "body": "never sent"}]}}, {"nope": 1}"#))
        XCTAssertNil(odd.totals?.recent)
        XCTAssertEqual(odd.seats.map(\.seat), ["CLAUDE"])
        XCTAssertNil(odd.seats[0].recent[0].at)
    }
}

// MARK: - The cards

final class OperatorCardTests: XCTestCase {
    private func listening(_ text: String = statusDocument()) throws -> ServerEavesdropperReading { try statusReading(text) }

    func test_before_the_first_read_every_card_says_reading() {
        for card in [OperatorPanelPresentation.eavesdropper(nil, now: now),
                     OperatorPanelPresentation.router(wakes: nil, status: nil, now: now),
                     OperatorPanelPresentation.registry(sessions: nil, status: nil, now: now)] {
            XCTAssertEqual(card.state, "Reading\u{2026}")
            XCTAssertEqual(card.tone, .neutral)
        }
    }

    func test_the_eavesdropper_listens_with_the_last_zulip_event() throws {
        let card = OperatorPanelPresentation.eavesdropper(try listening(), now: now)
        XCTAssertEqual(card.title, "Eavesdropper")
        XCTAssertEqual(card.state, "Listening")
        XCTAssertEqual(card.tone, .good)
        XCTAssertEqual(card.lines, ["Last Zulip event 20s ago."])
    }

    /// The colour must not claim a state the server did not report.
    func test_the_eavesdropper_does_not_go_green_when_it_does_not_say_whether_it_is_paused() throws {
        let card = OperatorPanelPresentation.eavesdropper(try listening(statusDocument(paused: "null")), now: now)
        XCTAssertEqual(card.tone, .neutral)
        XCTAssertTrue(card.lines.contains("Whether it is paused is not reported."))
    }

    func test_the_eavesdropper_paused_says_so_and_when() throws {
        let paused = OperatorPanelPresentation.eavesdropper(try listening(statusDocument(paused: "true")), now: now)
        XCTAssertEqual(paused.state, "Paused")
        XCTAssertEqual(paused.tone, .warning)
        XCTAssertTrue(paused.lines.contains { $0.hasPrefix("Paused since ") })
        let wakesOnly = OperatorPanelPresentation.eavesdropper(try listening(statusDocument(paused: "true", wakesOnly: "true")), now: now)
        XCTAssertTrue(wakesOnly.lines.first?.contains("Mentions are still read") == true)
    }

    /// A status the listener wrote more than five minutes ago is a stopped listener, not a quiet one.
    func test_the_eavesdropper_with_an_old_status_is_not_reporting() throws {
        let old = OperatorPanelPresentation.eavesdropper(try listening(statusDocument(updatedAgo: 900)), now: now)
        XCTAssertEqual(old.state, "Not reporting")
        XCTAssertEqual(old.tone, .bad)
        XCTAssertTrue(old.lines.first?.contains("not current") == true)
        let route = OperatorPanelPresentation.eavesdropper(.notRead("The server could not be asked."), now: now)
        XCTAssertEqual(route.state, "Not reporting")
        XCTAssertEqual(route.lines, ["The server could not be asked."])
        let unavailable = OperatorPanelPresentation.eavesdropper(.unavailable(.containerNotRunning), now: now)
        XCTAssertEqual(unavailable.state, "Not reporting")
        XCTAssertEqual(unavailable.lines, ["The eavesdropper is not running on the server."])
        let version = OperatorPanelPresentation.eavesdropper(.notThisAppsVersion("A newer form."), now: now)
        XCTAssertEqual(version.lines, ["A newer form."])
    }

    func test_the_eavesdropper_warns_when_it_cannot_post_or_no_seat_is_connected() throws {
        let cannot = statusDocument(voice: #"{"available": false, "code": "operator_credential_rejected", "since": null}"#)
        let card = OperatorPanelPresentation.eavesdropper(try listening(cannot), now: now)
        XCTAssertEqual(card.state, "Listening")
        XCTAssertEqual(card.tone, .warning)
        XCTAssertTrue(card.lines.contains { $0.contains("cannot post to Zulip") && $0.contains("refused the Operator's credential") })
        let down = statusDocument(seats: #"{"seat": "CLAUDE", "connected": false, "wake": "http"}"#)
        let none = OperatorPanelPresentation.eavesdropper(try listening(down), now: now)
        XCTAssertEqual(none.tone, .warning)
        XCTAssertTrue(none.lines.contains("No seat is connected."))
        let unknownPause = OperatorPanelPresentation.eavesdropper(try listening(statusDocument(paused: "null")), now: now)
        XCTAssertTrue(unknownPause.lines.contains("Whether it is paused is not reported."))
        let fileError = OperatorPanelPresentation.eavesdropper(try listening(statusDocument(pauseFileError: #""invalid""#)), now: now)
        XCTAssertEqual(fileError.tone, .warning)
    }

    func test_the_router_is_ready_only_when_every_seat_on_it_is_ready() throws {
        let allReady = statusDocument(seats: """
        {"seat": "CLAUDE", "connected": true, "wake": "http", "routine": {"ready": true, "auth": "bearer", "why": null}},
        {"seat": "CODEX", "connected": true, "wake": "http", "routine": {"ready": true, "auth": "bearer", "why": null}},
        {"seat": "CURSOR", "connected": true, "wake": "inbox", "routine": null}
        """)
        let wakes = OperatorReading.read(try wakesDocument())
        let ready = OperatorPanelPresentation.router(wakes: wakes, status: try statusReading(allReady), now: now)
        XCTAssertEqual(ready.state, "Ready")
        XCTAssertEqual(ready.tone, .good)
        XCTAssertEqual(ready.lines, ["All 2 seats that wake through it are ready.", "3 wakes in the last 24 hours."])
        XCTAssertFalse(ready.state.contains("Running"))

        let some = OperatorPanelPresentation.router(wakes: wakes, status: try statusReading(statusDocument()), now: now)
        XCTAssertEqual(some.state, "Some not ready")
        XCTAssertEqual(some.tone, .warning)
        XCTAssertEqual(some.lines.first, "1 of 2 seats that wake through it is not ready.")
    }

    /// The router's own run state is not in these reads, so with no readiness to go on the card
    /// says so and still gives the wakes' count.
    func test_the_router_says_not_read_here_when_the_reads_do_not_say() throws {
        let wakes = OperatorReading.read(try wakesDocument())
        let noStatus = OperatorPanelPresentation.router(wakes: wakes, status: nil, now: now)
        XCTAssertEqual(noStatus.state, "Not read here")
        XCTAssertEqual(noStatus.tone, .neutral)
        XCTAssertEqual(noStatus.lines, ["The router's own state is not in what the server reports.", "3 wakes in the last 24 hours."])
        let oldStatus = OperatorPanelPresentation.router(wakes: wakes, status: try statusReading(statusDocument(updatedAgo: 900)), now: now)
        XCTAssertEqual(oldStatus.state, "Not read here")
        let noRoutines = statusDocument(seats: #"{"seat": "CURSOR", "connected": true, "wake": "inbox", "routine": null}"#)
        XCTAssertEqual(OperatorPanelPresentation.router(wakes: wakes, status: try statusReading(noRoutines), now: now).state, "Not read here")
    }

    func test_the_router_card_counts_one_wake_and_a_floor_and_a_failure() throws {
        let one = OperatorReading.read(try wakesDocument(wakesAnswer(totals: #"{"recent": 1, "held_back": 0, "held_back_lost": 0, "owner_queue": 0}"#)))
        XCTAssertEqual(OperatorPanelPresentation.router(wakes: one, status: nil, now: now).lines, ["The router's own state is not in what the server reports.", "1 wake in the last 24 hours."])
        let floor = OperatorReading.read(try wakesDocument(wakesAnswer(unreadable: #"["GROK"]"#)))
        XCTAssertEqual(OperatorPanelPresentation.router(wakes: floor, status: nil, now: now).lines, ["The router's own state is not in what the server reports.", "At least 3 wakes in the last 24 hours."])
        let noTotals = OperatorReading.read(try wakesDocument(wakesAnswer(totals: "null")))
        XCTAssertEqual(OperatorPanelPresentation.router(wakes: noTotals, status: nil, now: now).lines,
                       ["The router's own state is not in what the server reports.", "The number of wakes in the last 24 hours is not reported."])
        let failed = OperatorPanelPresentation.router(wakes: .unavailable("The Operator's wake record was busy."), status: nil, now: now)
        // Only the wake record failed, so the card does not say the router is silent.
        XCTAssertEqual(failed.state, "Not read")
        XCTAssertEqual(failed.lines, ["The wake record was not read.", "The Operator's wake record was busy."])
        // A section always gives its own cause:  the banner says what to do, and never replaces it.
        let down = OperatorPanelPresentation.router(wakes: .unreachable("The server did not answer in time."), status: nil, now: now)
        XCTAssertEqual(down.lines, ["The wake record was not read.", "The server did not answer in time."])
        let downRegistry = OperatorPanelPresentation.registry(sessions: .unreachable("The server did not answer in time."), status: nil, now: now)
        XCTAssertEqual(downRegistry.lines, ["The server did not answer in time."])
    }

    func test_the_registry_counts_live_and_gone_and_its_warnings() throws {
        let sessions = OperatorReading.read(try sessionsDocument())
        // The fixture's sessions: one with no wake route, one not known to be wakeable.
        let clean = OperatorPanelPresentation.registry(sessions: sessions, status: nil, now: now)
        XCTAssertEqual(clean.state, "3 live")
        XCTAssertEqual(clean.lines, ["5 gone", "1 warning"])
        XCTAssertEqual(clean.tone, .warning)
        let quiet = OperatorReading.read(try sessionsDocument(sessionsAnswer(rows: """
        {"letter": "B", "generation": 1, "seat": "CODEX", "status": "active", "wake": "code-supported"}
        """)))
        let ok = OperatorPanelPresentation.registry(sessions: quiet, status: nil, now: now)
        XCTAssertEqual(ok.lines, ["5 gone", "No warnings"])
        XCTAssertEqual(ok.tone, .good)
        let noCounts = OperatorPanelPresentation.registry(sessions: .read(try sessionsDocument(sessionsAnswer(counts: "null"))), status: nil, now: now)
        XCTAssertEqual(noCounts.state, "Not reported")
        XCTAssertEqual(noCounts.tone, .neutral)
        let failed = OperatorPanelPresentation.registry(sessions: .unavailable("The registry was busy."), status: nil, now: now)
        XCTAssertEqual(failed.state, "Not reporting")
    }

    func test_registry_warnings_cover_the_sweep_and_the_chats_that_cannot_be_woken() throws {
        let sessions = OperatorReading.read(try sessionsDocument())
        func warnings(_ sweep: String) throws -> [String] {
            OperatorPanelPresentation.registryWarnings(sessions: sessions, status: try statusReading(statusDocument(sweep: sweep)), now: now)
        }
        let clean = try warnings(#"{"ran_at": "\#(at(120))", "ok": true, "ended": 0, "error": null, "stale_after_minutes": 2160, "refused": 0, "refused_platforms": []}"#)
        XCTAssertEqual(clean, ["2 live chats have no known wake route."])
        let notOk = try warnings(#"{"ran_at": "\#(at(120))", "ok": false, "ended": 0, "error": "registry_error", "stale_after_minutes": 2160, "refused": 0, "refused_platforms": []}"#)
        XCTAssertEqual(notOk.count, 2)
        XCTAssertTrue(notOk[0].contains("did not finish cleanly"))
        let late = try warnings(#"{"ran_at": "\#(at(7200))", "ok": true, "ended": 1, "error": null, "stale_after_minutes": 2160, "refused": 0, "refused_platforms": []}"#)
        XCTAssertTrue(late.contains { $0.contains("more than an hour ago") })
        let held = try warnings(#"{"ran_at": "\#(at(120))", "ok": true, "ended": 0, "error": null, "stale_after_minutes": 2160, "refused": 1, "refused_platforms": ["codex"]}"#)
        XCTAssertTrue(held.contains { $0.contains("share guard") && $0.contains("codex") })
        // An all-null block is a server that has not swept yet:  it says nothing.
        let none = try warnings(#"{"ran_at": null, "ok": null, "ended": null, "error": null, "stale_after_minutes": null, "refused": null, "refused_platforms": []}"#)
        XCTAssertEqual(none, ["2 live chats have no known wake route."])
        // A status that is not current says nothing of the sweep.
        let old = OperatorPanelPresentation.registryWarnings(
            sessions: sessions,
            status: try statusReading(statusDocument(updatedAgo: 900, sweep: #"{"ran_at": null, "ok": false, "ended": 0}"#)),
            now: now)
        XCTAssertEqual(old, ["2 live chats have no known wake route."])
    }

    func test_the_banner_shows_only_when_every_read_failed_on_the_route() {
        let down = OperatorReading<OperatorSessionsDocument>.unreachable("The server did not answer in time.")
        let downWakes = OperatorReading<OperatorWakesDocument>.unreachable("The server did not answer in time.")
        // The banner says the status read's own words:  which ssh failure it was.
        XCTAssertEqual(OperatorPanelPresentation.unreachableReason(status: .notRead("The key was refused."), sessions: down, wakes: downWakes),
                       "The key was refused.")
        XCTAssertEqual(OperatorPanelPresentation.unreachableReason(status: .notRead(""), sessions: down, wakes: downWakes),
                       "The server did not answer in time.")
        // One that was answered, or is not read yet, shows no banner.
        XCTAssertNil(OperatorPanelPresentation.unreachableReason(status: .notRead("x"), sessions: down, wakes: .unavailable("busy")))
        XCTAssertNil(OperatorPanelPresentation.unreachableReason(status: .unavailable(.timeout), sessions: down, wakes: downWakes))
        XCTAssertNil(OperatorPanelPresentation.unreachableReason(status: nil, sessions: down, wakes: downWakes))
        XCTAssertTrue(OperatorPanelPresentation.unreachableTitle.contains("could not be reached"))
        XCTAssertTrue(OperatorPanelPresentation.unreachableTitle.contains("press Refresh"))
    }
}

// MARK: - Seats and chats

final class OperatorSeatsTableTests: XCTestCase {
    private func table(_ answer: String = sessionsAnswer(), status: ServerEavesdropperReading? = nil, limit: Int = 30) throws -> OperatorSeatsTable {
        OperatorPanelPresentation.seatsTable(try sessionsDocument(answer), status: status, now: now, limit: limit)
    }

    func test_rows_are_active_first_then_newest_first() throws {
        let rows = """
        {"letter": "A", "seat": "OLD", "status": "active", "last_seen_at": "\(at(5000))"},
        {"letter": "B", "seat": "GONE", "status": "closed", "last_seen_at": "\(at(10))"},
        {"letter": "C", "seat": "NEW", "status": "active", "last_seen_at": "\(at(20))"},
        {"letter": "D", "seat": "NOTIME", "status": "active"},
        {"letter": "E", "seat": "UNKNOWN", "last_seen_at": "\(at(1))"}
        """
        let t = try table(sessionsAnswer(rows: rows))
        XCTAssertEqual(t.rows.map(\.seat), ["NEW", "OLD", "NOTIME", "UNKNOWN", "GONE"])
    }

    func test_the_table_lists_at_most_its_limit_and_says_how_many_more() throws {
        let rows = (1...40).map { #"{"letter": "A", "seat": "S\#($0)", "status": "active", "last_seen_at": "\#(at($0))"}"# }.joined(separator: ",")
        let t = try table(sessionsAnswer(counts: #"{"active": 40, "closed": 0}"#, rows: rows))
        XCTAssertEqual(OperatorPanelPresentation.seatRowLimit, 30)
        XCTAssertEqual(t.rows.count, 30)
        XCTAssertEqual(t.moreCount, 10)
        XCTAssertEqual(t.rows.first?.seat, "S1")
        XCTAssertNil(t.note)
        XCTAssertEqual(try table(limit: 2).rows.count, 2)
        XCTAssertEqual(try table(limit: 2).moreCount, 1)
    }

    func test_a_truncated_registry_says_so() throws {
        let t = try table(sessionsAnswer(counts: #"{"active": 412, "closed": 9}"#, truncated: true))
        XCTAssertEqual(t.note, "The server lists only the newest 3 of 412 live chats, so older live chats are not here.")
    }

    func test_the_cells_say_the_registrys_words_in_plain_ones() throws {
        let t = try table()
        let byseat = Dictionary(uniqueKeysWithValues: t.rows.map { ($0.seat, $0) })
        let claude = try XCTUnwrap(byseat["CLAUDE"])
        XCTAssertEqual(claude.letter, "II.2")
        XCTAssertEqual(claude.host, "Mac")
        XCTAssertEqual(claude.state.text, "Live")
        XCTAssertEqual(claude.state.tone, .good)
        XCTAssertEqual(claude.wake.text, "No wake route")
        XCTAssertEqual(claude.wake.tone, .warning)
        XCTAssertEqual(claude.lastSeen, "10m ago")
        let codex = try XCTUnwrap(byseat["CODEX"])
        XCTAssertEqual(codex.letter, OperatorPanelText.none)
        XCTAssertEqual(codex.host, "Server")
        XCTAssertEqual(codex.wake.text, "Can be woken")
        XCTAssertEqual(codex.lastSeen, "30s ago")
        let cursor = try XCTUnwrap(byseat["CURSOR"])
        XCTAssertEqual(cursor.letter, "B.1")
        XCTAssertEqual(cursor.host, "Cloud")
        XCTAssertEqual(cursor.wake.text, "Not known")
        XCTAssertEqual(OperatorPanelPresentation.stateCell("closed").text, "Gone")
        XCTAssertEqual(OperatorPanelPresentation.stateCell(nil).text, "Not known")
        XCTAssertEqual(OperatorPanelPresentation.letterText(letter: "Q", generation: nil), "Q")
        XCTAssertEqual(OperatorPanelPresentation.hostLabel(code: nil, host: nil), "Not known")
        XCTAssertEqual(OperatorPanelPresentation.hostLabel(code: nil, host: "server"), "Server")
    }

    /// PEER NOTE:  each seat's `routine` readiness stays visible, as On the router or Not
    /// configured, with the reason in words.
    func test_the_router_column_shows_each_seats_routine_readiness() throws {
        let status = try statusReading(statusDocument())
        let t = try table(status: status)
        let byseat = Dictionary(uniqueKeysWithValues: t.rows.map { ($0.seat, $0.router) })
        XCTAssertEqual(byseat["CLAUDE"]?.text, "On the router")
        XCTAssertEqual(byseat["CLAUDE"]?.tone, .good)
        XCTAssertEqual(byseat["CODEX"]?.text, "Not configured")
        XCTAssertEqual(byseat["CODEX"]?.tone, .warning)
        XCTAssertTrue(byseat["CODEX"]?.help.contains("its address or key is not set on the server") == true)
        XCTAssertEqual(byseat["CURSOR"]?.text, "Inbox only")
    }

    func test_the_router_column_degrades_with_the_status() throws {
        func cell(_ status: ServerEavesdropperReading?) throws -> OperatorCell {
            try XCTUnwrap(try table(status: status).rows.first { $0.seat == "CLAUDE" }).router
        }
        XCTAssertEqual(try cell(nil).text, "Not read yet")
        XCTAssertEqual(try cell(.notRead("x")).text, "Not read")
        XCTAssertEqual(try cell(.unavailable(.timeout)).text, "Not read")
        XCTAssertEqual(try cell(try statusReading(statusDocument(updatedAgo: 900))).text, "Not current")
        let notListed = statusDocument(seats: #"{"seat": "OTHER", "connected": true, "wake": "http", "routine": null}"#)
        XCTAssertEqual(try cell(try statusReading(notListed)).text, "Not listed")
        let reasons: [(String, String)] = [
            ("routine_key_refused", "Key refused"), ("routine_url_refused", "Address refused"), ("other", "Not ready"),
        ]
        for (why, text) in reasons {
            let doc = statusDocument(seats: #"{"seat": "CLAUDE", "connected": true, "wake": "http", "routine": {"ready": false, "auth": null, "why": "\#(why)"}}"#)
            XCTAssertEqual(try cell(try statusReading(doc)).text, text, why)
        }
        let unsure = statusDocument(seats: #"{"seat": "CLAUDE", "connected": true, "wake": "http", "routine": {"ready": null}}"#)
        XCTAssertEqual(try cell(try statusReading(unsure)).text, "Not reported")
    }

    func test_seats_table_groups_by_platform_and_by_seat_and_filters_search_and_issues() throws {
        let rows = """
        {"letter": "A", "generation": 1, "seat": "CC", "platform": "claude-code", "host": "mac", "host_code": "M", "status": "active", "wake": "code-supported", "last_seen_at": "\(at(20))", "issues": []},
        {"letter": "B", "generation": 1, "seat": "CX", "platform": "codex", "host": "server", "host_code": "S", "status": "active", "wake": "none", "last_seen_at": "\(at(10))", "issues": ["stale_heartbeat"]},
        {"letter": "C", "generation": 1, "seat": "CR", "platform": "claude", "host": "mac", "host_code": "M", "status": "closed", "wake": "code-supported", "last_seen_at": "\(at(500))", "issues": []},
        {"letter": "D", "generation": 1, "seat": "AG", "host": "server", "host_code": "S", "status": "closed", "wake": "code-supported", "last_seen_at": "\(at(900))", "issues": []}
        """
        let doc = try sessionsDocument(sessionsAnswer(rows: rows))

        let byPlatform = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, grouping: .byPlatform)
        XCTAssertEqual(byPlatform.totalMatchingCount, 4)
        XCTAssertEqual(byPlatform.groups.map(\.title), ["Claude", "Codex", "Other"])
        XCTAssertEqual(byPlatform.groups.first?.liveCount, 1)
        XCTAssertEqual(byPlatform.groups.first?.goneCount, 1)
        XCTAssertEqual(byPlatform.groups.first?.seatsSummary, "CC, CR")
        XCTAssertEqual(byPlatform.groups[1].problemCount, 1)
        XCTAssertEqual(byPlatform.groups[1].rows.first?.issues, ["Stale heartbeat"])

        let bySeat = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, grouping: .bySeat)
        XCTAssertEqual(bySeat.groups.count, 4)

        let flat = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, grouping: .flat)
        XCTAssertTrue(flat.groups.isEmpty)

        let liveOnly = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, filter: .live)
        XCTAssertEqual(Set(liveOnly.rows.map(\.seat)), ["CC", "CX"])

        let problemsOnly = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, filter: .problems)
        XCTAssertEqual(problemsOnly.rows.map(\.seat), ["CX"])

        let searched = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, search: "heartbeat")
        XCTAssertEqual(searched.rows.map(\.seat), ["CX"])

        let unlimited = OperatorPanelPresentation.seatsTable(doc, status: nil, now: now, limit: Int.max)
        XCTAssertEqual(unlimited.rows.count, 4)
        XCTAssertEqual(unlimited.moreCount, 0)
    }
}

// MARK: - Recent wakes

final class OperatorWakesTableTests: XCTestCase {
    func test_wakes_are_newest_first_across_seats() throws {
        let t = OperatorPanelPresentation.wakesTable(try wakesDocument(), now: now)
        XCTAssertEqual(t.rows.map(\.seat), ["CLAUDE", "CODEX", "CLAUDE"])
        XCTAssertEqual(t.rows.map(\.state.text), ["Held back", "Done", "Done"])
        XCTAssertEqual(t.rows[0].reason, "Wakes per hour, Owner")
        XCTAssertEqual(t.rows[1].reason, "Catch-up")
        XCTAssertEqual(t.rows[2].reason, "DM")
        XCTAssertEqual(t.rows[0].state.tone, .warning)
        XCTAssertEqual(t.rows[1].state.tone, .neutral)
        XCTAssertEqual(t.rows[0].time, FleetTime.when(FleetTime.parse(at(60))!, now: now))
        XCTAssertEqual(t.moreCount, 0)
    }

    func test_the_list_stops_at_fifteen_and_the_count_says_how_many_more() throws {
        XCTAssertEqual(OperatorPanelPresentation.wakeRowLimit, 15)
        let rows = (1...20).map { #"{"wake_id": "w-\#($0)", "state": "done", "at": "\#(at($0 * 10))"}"# }.joined(separator: ",")
        let seats = #"{"seat": "CLAUDE", "recent": {"total": 40, "rows": [\#(rows)]}}"#
        let document = try wakesDocument(wakesAnswer(totals: #"{"recent": 40, "held_back": 0, "held_back_lost": 0, "owner_queue": 0}"#, seats: seats))
        let t = OperatorPanelPresentation.wakesTable(document, now: now)
        XCTAssertEqual(t.rows.count, 15)
        XCTAssertEqual(t.moreCount, 25)
        XCTAssertEqual(t.rows.first?.id.split(separator: "|").last, "w-1")
    }

    func test_the_summary_shows_held_back_and_the_owner_queue_only_when_not_zero() throws {
        let busy = OperatorPanelPresentation.wakesTable(try wakesDocument(), now: now)
        XCTAssertEqual(busy.summary, "1 held back, 1 waiting for the owner.")
        let lost = OperatorPanelPresentation.wakesTable(
            try wakesDocument(wakesAnswer(totals: #"{"recent": 3, "held_back": 4, "held_back_lost": 2, "owner_queue": 0}"#)), now: now)
        XCTAssertEqual(lost.summary, "4 held back (2 lost).")
        let quiet = OperatorPanelPresentation.wakesTable(
            try wakesDocument(wakesAnswer(totals: #"{"recent": 3, "held_back": 0, "held_back_lost": 0, "owner_queue": 0}"#)), now: now)
        XCTAssertNil(quiet.summary)
        let missing = OperatorPanelPresentation.wakesTable(try wakesDocument(wakesAnswer(totals: "null")), now: now)
        XCTAssertNil(missing.summary)
    }

    func test_a_seat_that_could_not_be_read_and_a_cut_list_are_said() throws {
        let t = OperatorPanelPresentation.wakesTable(
            try wakesDocument(wakesAnswer(unreadable: #"["GROK", "MM"]"#, truncated: true)), now: now)
        XCTAssertEqual(t.notes.count, 2)
        XCTAssertTrue(t.notes[0].contains("GROK, MM"))
        XCTAssertTrue(t.notes[0].contains("floor"))
        XCTAssertTrue(t.notes[1].contains("only some seats"))
        let clean = OperatorPanelPresentation.wakesTable(try wakesDocument(), now: now)
        XCTAssertEqual(clean.notes, [])
    }

    func test_a_wake_with_no_state_or_time_reads_as_a_dash() throws {
        let seats = #"{"seat": "CLAUDE", "recent": {"total": 1, "rows": [{"wake_id": "w-1"}]}}"#
        let t = OperatorPanelPresentation.wakesTable(try wakesDocument(wakesAnswer(seats: seats)), now: now)
        XCTAssertEqual(t.rows.first?.state.text, OperatorPanelText.none)
        XCTAssertEqual(t.rows.first?.time, OperatorPanelText.none)
        XCTAssertEqual(t.rows.first?.reason, OperatorPanelText.none)
        XCTAssertEqual(OperatorPanelText.humanized("held_back"), "Held back")
    }

    func test_wakes_table_filters_searches_groups_by_seat_and_tracks_loaded_vs_server_counts() throws {
        let wakeRows = (1...20).map { i -> String in
            let state = (i == 2 || i == 5) ? "dropped" : "done"
            let dm = (i == 3) ? "true" : "false"
            return #"{"wake_id": "w-\#(i)", "state": "\#(state)", "at": "\#(at(i * 10))", "reason": "mention", "dm": \#(dm), "owner": false, "catch_up": false}"#
        }.joined(separator: ",")
        let seats = """
        {"seat": "CLAUDE", "recent": {"total": 20, "rows": [\(wakeRows)]}},
        {"seat": "CODEX", "recent": {"total": 1, "rows": [{"wake_id": "w-cx", "state": "done", "at": "\(at(5))", "reason": "topic", "dm": false, "owner": true, "catch_up": false}]}}
        """
        let answer = wakesAnswer(totals: #"{"recent": 25, "held_back": 0, "held_back_lost": 0, "owner_queue": 0}"#, seats: seats)
        let doc = try wakesDocument(answer)

        let capped = OperatorPanelPresentation.wakesTable(doc, now: now)
        XCTAssertEqual(capped.rows.count, 15)
        XCTAssertEqual(capped.totalLoadedCount, 21)
        XCTAssertEqual(capped.loadedMoreCount, 6)
        XCTAssertEqual(capped.unloadedServerCount, 4)
        XCTAssertEqual(capped.moreCount, 10)
        XCTAssertEqual(capped.seatGroups.first?.seat, "CODEX")
        XCTAssertEqual(capped.seatGroups.last?.seat, "CLAUDE")

        let expanded = OperatorPanelPresentation.wakesTable(doc, now: now, limit: Int.max)
        XCTAssertEqual(expanded.rows.count, 21)
        XCTAssertEqual(expanded.loadedMoreCount, 0)
        XCTAssertEqual(expanded.unloadedServerCount, 4)

        let problems = OperatorPanelPresentation.wakesTable(doc, now: now, filter: .problems)
        XCTAssertEqual(problems.rows.count, 2)
        XCTAssertTrue(problems.rows.allSatisfy(\.isProblem))

        let ownerDM = OperatorPanelPresentation.wakesTable(doc, now: now, filter: .ownerOrDM)
        XCTAssertEqual(ownerDM.rows.count, 2)

        let searched = OperatorPanelPresentation.wakesTable(doc, now: now, search: "CODEX")
        XCTAssertEqual(searched.rows.map(\.seat), ["CODEX"])
    }
}

// MARK: - Words

final class OperatorPanelWordsTests: XCTestCase {
    /// Shipped copy keeps a real U+00A0 and a space between sentences, never two plain spaces, and
    /// never says successfully or shows a raw exception.
    func test_fixed_copy_has_the_sentence_gap_and_no_forbidden_words() throws {
        var copy = [
            OperatorPanelPresentation.unreachableTitle, OperatorPanelCopy.retiredListener, OperatorPanelCopy.controlsPointer,
            OperatorPanelCopy.heading, OperatorPanelCopy.seatsHeading, OperatorPanelCopy.wakesHeading,
        ]
        for code in ["docker_unavailable", "timeout", "container_not_running", "read_failed", "store_busy", "store_recovering", "nope"] {
            copy.append(OperatorUnavailable.cause(errorCode: code, detail: nil, what: "letter registry"))
        }
        for text in copy {
            XCTAssertFalse(text.contains(".  "), text)
            XCTAssertFalse(text.lowercased().contains("successfully"), text)
            XCTAssertFalse(text.lowercased().contains("exception"), text)
        }
        XCTAssertTrue(OperatorPanelPresentation.unreachableTitle.contains(".\u{00A0} Check"))
        XCTAssertEqual(OperatorPanelCopy.retiredListener, "The Mac's own listener is retired and no longer has a panel.")
        XCTAssertEqual(OperatorPanelCopy.controlsPointer, "Operator knobs are in Controls")
    }

    func test_ages_read_as_relative_words() {
        XCTAssertEqual(OperatorPanelText.relative(now.addingTimeInterval(-12), now: now), "12s ago")
        XCTAssertEqual(OperatorPanelText.relative(now.addingTimeInterval(-300), now: now), "5m ago")
        XCTAssertEqual(OperatorPanelText.relative(now.addingTimeInterval(30), now: now), "0s ago")
        XCTAssertEqual(OperatorPanelText.relative(nil, now: now), OperatorPanelText.none)
    }
}

// MARK: - The store

@MainActor
final class OperatorPanelStoreTests: XCTestCase {
    private final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func add() { lock.lock(); value += 1; lock.unlock() }
        var count: Int { lock.lock(); defer { lock.unlock() }; return value }
    }

    func test_a_refresh_stores_each_answer_and_when_it_finished() async throws {
        let document = try sessionsDocument()
        let wakes = try wakesDocument()
        let reading = try statusReading(statusDocument())
        let store = OperatorPanelStore(
            statusRead: { reading },
            sessionsRead: { .read(document) },
            wakesRead: { .read(wakes) },
            now: { now }
        )
        XCTAssertNil(store.status)
        XCTAssertNil(store.readAt)
        await store.refresh()
        XCTAssertEqual(store.status, reading)
        XCTAssertEqual(store.sessions, .read(document))
        XCTAssertEqual(store.wakes, .read(wakes))
        XCTAssertEqual(store.readAt, now)
        XCTAssertEqual(store.triedAt, now)
        XCTAssertFalse(store.loading)
    }

    /// A refresh that read nothing is not "Refreshed":  it says only that it tried.
    func test_a_refresh_that_read_nothing_sets_only_when_it_tried() async {
        let store = OperatorPanelStore(
            statusRead: { .notRead("x") },
            sessionsRead: { .unreachable("y") },
            wakesRead: { .unreachable("z") },
            now: { now }
        )
        await store.refresh()
        XCTAssertNil(store.readAt)
        XCTAssertEqual(store.triedAt, now)
    }

    /// A read that failed never blanks another:  each section degrades on its own.
    func test_a_failed_read_does_not_blank_the_others() async throws {
        let wakes = try wakesDocument()
        let store = OperatorPanelStore(
            statusRead: { .notRead("The server could not be asked.") },
            sessionsRead: { .unavailable("The registry was busy.") },
            wakesRead: { .read(wakes) }
        )
        await store.refresh()
        XCTAssertEqual(store.sessions, .unavailable("The registry was busy."))
        XCTAssertEqual(store.wakes, .read(wakes))
        XCTAssertEqual(store.status, .notRead("The server could not be asked."))
        // The next refresh replaces only what it read, and a good answer replaces a failed one.
        let document = try sessionsDocument()
        let again = OperatorPanelStore(
            statusRead: { .notRead("x") },
            sessionsRead: { .read(document) },
            wakesRead: { .unreachable("The server did not answer in time.") }
        )
        await again.refresh()
        XCTAssertEqual(again.sessions, .read(document))
        XCTAssertEqual(again.wakes, .unreachable("The server did not answer in time."))
    }

    func test_a_second_request_while_a_refresh_runs_starts_nothing() async throws {
        let sessionReads = Counter()
        let gate = AsyncGate()
        let store = OperatorPanelStore(
            statusRead: { .notRead("x") },
            sessionsRead: {
                sessionReads.add()
                await gate.wait()
                return .unavailable("busy")
            },
            wakesRead: { .unavailable("busy") }
        )
        async let first: Void = store.refresh()
        // Let the first refresh start, then ask again while its read is held.
        while store.refreshesStarted == 0 { await Task.yield() }
        async let second: Void = store.refresh()
        await Task.yield()
        await gate.open()
        _ = await (first, second)
        XCTAssertEqual(store.refreshesStarted, 1)
        XCTAssertEqual(sessionReads.count, 1)
        XCTAssertFalse(store.loading)
        // Once it has ended, the next request starts a new one.
        await gate.open()
        await store.refresh()
        XCTAssertEqual(store.refreshesStarted, 2)
    }
}

/// Holds a read until the test opens the gate.
private actor AsyncGate {
    private var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if isOpen { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func open() {
        isOpen = true
        let held = waiters
        waiters = []
        for waiter in held { waiter.resume() }
    }
}
