import XCTest

// The server eavesdropper's status, read on the Mac:  the reduced_status v1 document, the
// `server eavesdropper-status` route and answers, the per-seat readings, and the store.
//
// No test here starts a process or reaches the network:  the runner is a fake, and the answers
// are fixed text.  `plannedAnswer` is a real answer:  it is the helper's planned envelope around
// the output of `reduce_status` (scripts/simple_tools/reduced_status.py, PR 1479) run on a status
// document shaped like that PR's own test fixture (`status_document` in
// scripts/simple_tools/tests/test_reduced_status.py), with `now` at 2:00 PM on Nov 8, 2026.

/// Answers every run with one result, and records the argv it was asked to run.
private final class EavesdropperRunner: OpsRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [[String]] = []
    private let result: OpsProcessResult

    init(_ result: OpsProcessResult) { self.result = result }

    var recorded: [[String]] { lock.lock(); defer { lock.unlock() }; return calls }

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        lock.lock()
        calls.append(invocation.arguments)
        lock.unlock()
        return result
    }
}

private let plannedAnswer = #"""
{"commands":[],"detail":"read-only status of the server's eavesdropper","ok":true,"result":"planned","status":{"disabled":["GROK-WEB"],"generated_at":"2026-11-08T20:00:00Z","instance":"server","operator_voice":{"available":false,"code":"operator_credential_rejected","since":"2026-11-08T19:00:00Z"},"pause":{"paused":true,"set_by":"cli","since":"2026-11-08T19:50:00Z","wakes_only":true},"pause_file_error":null,"schema":1,"seats":[{"bot":"Claude","connected":true,"held_back":2,"jobs":1,"last_event":"2026-11-08T19:59:30Z","leases":[{"alive":true,"letter":"AA","pending":1,"platform":"claude-code","title":"Letters Work","topics":2,"verified_host":"server","watcher":true,"why":"ok"},{"alive":false,"letter":null,"pending":1,"platform":"cursor","title":null,"topics":2,"verified_host":null,"watcher":false,"why":"pid_gone"}],"letters":{"known":false,"why":"partial"},"paused":false,"quota":{"cap_day":null,"cap_hour":null,"exhausted":true,"known":true,"reason":null,"remaining":0,"reset_at":"2026-11-09T05:00:00Z","source":"codecaps","used":null},"running":"running","seat":"CLAUDE","wake":"claude","wakes_24h":3},{"bot":"MA","connected":false,"held_back":2,"jobs":1,"last_event":null,"leases":[],"letters":{"known":true,"why":null},"paused":false,"quota":{"cap_day":null,"cap_hour":null,"exhausted":null,"known":false,"reason":null,"remaining":null,"reset_at":null,"source":null,"used":null},"running":"stopped","seat":"DOWN","wake":"inbox","wakes_24h":3},{"bot":"MA","connected":true,"held_back":2,"jobs":1,"last_event":"2026-11-08T19:59:30Z","leases":[],"letters":{"known":true,"why":null},"paused":true,"quota":{"cap_day":null,"cap_hour":null,"exhausted":null,"known":false,"reason":null,"remaining":null,"reset_at":null,"source":null,"used":null},"running":"running","seat":"HALTED","wake":"inbox","wakes_24h":3},{"bot":"JET","connected":true,"held_back":85,"jobs":1,"last_event":"2026-11-08T19:59:30Z","leases":[],"letters":{"known":true,"why":null},"paused":false,"quota":{"cap_day":null,"cap_hour":null,"exhausted":null,"known":false,"reason":null,"remaining":null,"reset_at":null,"source":null,"used":null},"running":"running","seat":"JET","wake":"http","wakes_24h":7},{"bot":"MA","connected":true,"held_back":2,"jobs":1,"last_event":"2026-11-08T19:59:30Z","leases":[],"letters":{"known":true,"why":null},"paused":false,"quota":{"cap_day":null,"cap_hour":null,"exhausted":null,"known":false,"reason":null,"remaining":null,"reset_at":null,"source":null,"used":null},"running":"running","seat":"MA","wake":"inbox","wakes_24h":3},{"bot":"MiniMax","connected":true,"held_back":2,"jobs":1,"last_event":"2026-11-08T19:59:30Z","leases":[],"letters":{"known":true,"why":null},"paused":false,"quota":{"cap_day":null,"cap_hour":null,"exhausted":null,"known":false,"reason":null,"remaining":null,"reset_at":null,"source":"unknown","used":null},"running":"running","seat":"MM","wake":"fallback","wakes_24h":3}],"skipped":[{"reason":"invalid_credentials","seat":"BAD"}],"status_updated":"2026-11-08T19:59:58Z"},"target":"server","verb":"server eavesdropper-status"}
"""#

/// The same document with every scalar null, a seat with a wrong-typed field in each place, a
/// seat with no name and an entry that is not a seat at all.
private let allNullDocument = #"""
{"schema":1,"generated_at":null,"instance":null,"status_updated":null,
 "operator_voice":{"available":null,"code":null,"since":null},
 "pause":{"paused":null,"since":null,"wakes_only":null,"set_by":null},
 "pause_file_error":null,
 "seats":[
  {"seat":"CLAUDE","bot":null,"connected":null,"running":null,"wake":null,"paused":null,
   "wakes_24h":null,"held_back":null,"jobs":null,"last_event":null,
   "quota":{"known":null,"exhausted":null,"remaining":null,"reset_at":null,"used":null,
            "cap_day":null,"cap_hour":null,"source":null,"reason":null},
   "leases":[{"letter":null,"platform":null,"alive":null,"pending":null,"title":null,
              "verified_host":null,"why":null,"topics":null,"watcher":null}],
   "letters":{"known":null,"why":null}},
  {"seat":"ODD","bot":7,"connected":"yes","running":3,"wake":["inbox"],"paused":"no",
   "wakes_24h":"three","held_back":-1,"jobs":1.5,"last_event":"yesterday",
   "quota":"none","leases":"none","letters":[]},
  {"seat":null,"connected":true},
  5
 ],
 "skipped":[{"seat":null,"reason":"invalid_bot"}],
 "disabled":[null, 4]}
"""#

private func date(_ text: String) throws -> Date {
    try XCTUnwrap(FleetTime.parse(text), text)
}

private func document(_ text: String) throws -> ReducedEavesdropperStatus {
    try JSONDecoder().decode(ReducedEavesdropperStatus.self, from: Data(text.utf8))
}

private func outcome(_ stdout: String, exit: Int32 = 0) -> OpsOutcome {
    OpsResultParser.parse(
        OpsProcessResult(exitCode: exit, stdout: stdout, stderr: ""),
        mode: .plan, action: .eavesdropperStatus, timeout: 90
    )
}

/// The reading of `plannedAnswer`.  A plain function, so a reader closure can call it from any
/// thread.
private func sampleReading() -> ServerEavesdropperReading {
    ServerEavesdropperReading(outcome: outcome(plannedAnswer))
}

/// A planned answer around `status`, a document given as JSON text.
private func planned(_ status: String) -> String {
    #"{"ok":true,"verb":"server eavesdropper-status","target":"server","commands":[],"result":"planned","detail":"read-only status of the server's eavesdropper","status":"# + status + "}"
}

/// A minimal current document generated at `generated` with the listener's own status written
/// at `updated`, holding one running seat.
private func timedDocument(generated: String, updated: String) -> String {
    #"{"schema":1,"generated_at":""# + generated + #"","instance":"server","status_updated":""# + updated
        + #"","seats":[{"seat":"MA","connected":true,"running":"running","wake":"inbox"}],"skipped":[],"disabled":[]}"#
}

final class ServerEavesdropperRouteTests: XCTestCase {
    private let sshPrefix = [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "fleet-ops-ctl", "--",
    ]

    private func builder() -> OpsCommandBuilder {
        OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/tmp/example-repo", isDirectory: true),
                          home: URL(fileURLWithPath: "/Users/example", isDirectory: true),
                          sshAuthSocket: nil)
    }

    /// The read goes over the fixed ssh route with the one argv the helper accepts, as a plan.
    func test_the_read_is_one_fixed_plan_over_the_ssh_route() throws {
        let action = OpsAction.eavesdropperStatus
        let invocation = try builder().invocation(for: action, mode: .plan)
        XCTAssertEqual(invocation.executable, "/usr/bin/ssh")
        XCTAssertEqual(invocation.arguments, sshPrefix + [
            "server", "eavesdropper-status", "server", "--plan", "--json",
        ])
        XCTAssertEqual(invocation.timeout, OpsControlRoute.planTimeout)
        XCTAssertEqual(Set(invocation.environment.keys), ["PATH", "HOME", "LANG"])
        XCTAssertTrue(action.runsOnServer)
        XCTAssertTrue(action.changesNothing)
        XCTAssertNil(builder().requiredFile(for: action))
        XCTAssertEqual(action.auditVerb, "server eavesdropper status")
        XCTAssertEqual(action.auditTarget, "server")
        XCTAssertEqual(action.caseTag, .eavesdropperStatus)
        XCTAssertEqual(action.title, "Read the eavesdropper's status on the server")
        XCTAssertNil(action.stopWarning)
    }

    /// The helper refuses `--yes` for this verb, and Muster never builds one.
    func test_a_run_of_the_read_is_never_built() {
        XCTAssertThrowsError(try builder().invocation(for: .eavesdropperStatus, mode: .yes)) { error in
            XCTAssertEqual(error as? OpsControlError, .invalidArgument("read-only eavesdropper status mode"))
        }
    }

    /// Muster never takes this read on its own as a start.
    @MainActor
    func test_the_unattended_route_never_runs_the_read() async {
        let runner = EavesdropperRunner(OpsProcessResult(exitCode: 0, stdout: plannedAnswer, stderr: ""))
        let controller = OpsController(
            runner: runner,
            makeBuilder: { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/tmp/example-repo")) },
            auditURL: nil,
            checksInstall: false
        )
        let outcome = await controller.runUnattended(.eavesdropperStatus)
        XCTAssertNil(outcome)
        XCTAssertTrue(runner.recorded.isEmpty)
    }

    /// A planned answer carries the document;  the reader turns it into a reading.
    func test_the_reader_runs_the_route_and_reads_the_document() async {
        let runner = EavesdropperRunner(OpsProcessResult(exitCode: 0, stdout: plannedAnswer, stderr: ""))
        var reader = ServerEavesdropperStatusReader()
        reader.runner = runner
        reader.makeBuilder = { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/tmp/example-repo")) }
        let reading = await reader.read()
        guard case .read(let status) = reading else { return XCTFail("\(reading)") }
        XCTAssertEqual(status.seats.count, 6)
        XCTAssertEqual(runner.recorded.count, 1)
        XCTAssertEqual(Array(runner.recorded[0].suffix(5)),
                       ["server", "eavesdropper-status", "server", "--plan", "--json"])
        XCTAssertFalse(runner.recorded[0].contains("--yes"))
    }

    /// A route that times out reads as not read, never as a reading.
    func test_a_timed_out_read_is_not_read() async {
        var reader = ServerEavesdropperStatusReader()
        reader.runner = EavesdropperRunner(OpsProcessResult(exitCode: 0, stdout: "", stderr: "", timedOut: true))
        reader.makeBuilder = { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/tmp/example-repo")) }
        let reading = await reader.read()
        guard case .notRead(let why) = reading else { return XCTFail("\(reading)") }
        XCTAssertTrue(why.contains("90 seconds"), why)
    }
}

final class ServerEavesdropperAnswerTests: XCTestCase {
    /// The planned answer reads as planned, carries the document, and every v1 field decodes.
    func test_a_full_v1_document_decodes_through_the_parser() throws {
        let parsed = outcome(plannedAnswer)
        XCTAssertEqual(parsed.kind, .planned)
        XCTAssertTrue(parsed.isSuccess(for: .plan))
        XCTAssertTrue(parsed.isPlan)
        XCTAssertNil(parsed.errorCode)
        let reading = ServerEavesdropperReading(outcome: parsed)
        let status = try XCTUnwrap(reading.status, "\(reading)")

        XCTAssertEqual(status.schema, 1)
        XCTAssertTrue(status.isThisAppsVersion)
        XCTAssertEqual(status.instance, "server")
        XCTAssertEqual(status.generatedAt, try date("2026-11-08T20:00:00Z"))
        XCTAssertEqual(status.statusUpdated, try date("2026-11-08T19:59:58Z"))
        XCTAssertEqual(status.seats.map(\.seat), ["CLAUDE", "DOWN", "HALTED", "JET", "MA", "MM"])
        XCTAssertEqual(status.disabled, ["GROK-WEB"])
        XCTAssertEqual(status.skipped, [ServerSkippedSeat(seat: "BAD", reason: "invalid_credentials")])
        XCTAssertNil(status.pauseFileError)
        XCTAssertEqual(status.pause?.paused, true)
        XCTAssertEqual(status.pause?.wakesOnly, true)
        XCTAssertEqual(status.pause?.setBy, "cli")
        XCTAssertEqual(status.pause?.since, try date("2026-11-08T19:50:00Z"))
        XCTAssertEqual(status.operatorVoiceReading,
                       .cannotPost(code: "operator_credential_rejected", since: try date("2026-11-08T19:00:00Z")))
        XCTAssertEqual(status.operatorVoiceReading.label, "Cannot post")
        XCTAssertNotNil(status.operatorVoiceReading.cause)

        let claude = try XCTUnwrap(status.seat(named: "CLAUDE"))
        XCTAssertEqual(claude.bot, "Claude")
        XCTAssertEqual(claude.connection, .connected)
        XCTAssertEqual(claude.runState, .running)
        // The server still sends the old wake words.  Each is read, and one the panel gives no
        // meaning to is shown as the server wrote it.
        XCTAssertEqual(claude.wakeKind, .other("claude"))
        XCTAssertEqual(claude.wakeKind.label, "claude")
        XCTAssertEqual(claude.wakeKind.rowText, "Wake: claude")
        XCTAssertEqual(claude.pauseState, .notPaused)
        XCTAssertEqual(claude.wakesLastDay, 3)
        XCTAssertEqual(claude.heldBack, 2)
        XCTAssertEqual(claude.jobs, 1)
        XCTAssertEqual(claude.lastEvent, try date("2026-11-08T19:59:30Z"))
        XCTAssertEqual(claude.quotaReading,
                       .known(exhausted: true, remainingPercent: 0, resetsAt: try date("2026-11-09T05:00:00Z")))
        XCTAssertEqual(claude.quotaReading.label, "Used up")
        XCTAssertEqual(claude.quota?.source, "codecaps")
        XCTAssertNil(claude.quota?.used)
        XCTAssertEqual(claude.letterState, .partial)
        XCTAssertEqual(claude.sessions.count, 2)
        let lettered = claude.sessions[0]
        XCTAssertEqual(lettered.letter, "AA")
        // The fixture comes from an older reducer that still sent a title.  A server lease's
        // title is never kept on the Mac:  nothing decoded holds it.
        XCTAssertFalse(String(describing: lettered).contains("Letters Work"), String(describing: lettered))
        XCTAssertFalse(String(describing: claude).contains("Letters Work"))
        XCTAssertEqual(lettered.platform, "claude-code")
        XCTAssertEqual(lettered.verifiedHostLabel, "The server")
        XCTAssertEqual(lettered.aliveLabel, "Alive")
        XCTAssertEqual(lettered.pending, 1)
        XCTAssertEqual(lettered.topics, 2)
        XCTAssertEqual(lettered.watcher, true)
        let gone = claude.sessions[1]
        XCTAssertNil(gone.letter)
        XCTAssertEqual(gone.alive, false)
        XCTAssertEqual(gone.why, "pid_gone")
        XCTAssertEqual(gone.whyText, "Its process has ended.")
        XCTAssertEqual(gone.verifiedHostLabel, "Not reported")

        let down = try XCTUnwrap(status.seat(named: "DOWN"))
        XCTAssertEqual(down.connection, .notConnected)
        XCTAssertEqual(down.runState, .stopped)
        XCTAssertNil(down.lastEvent)
        XCTAssertEqual(down.lastEventLabel(now: try date("2026-11-08T20:00:00Z")), "Not reported")
        XCTAssertEqual(down.quotaReading, .notKnown)
        XCTAssertEqual(try XCTUnwrap(status.seat(named: "HALTED")).pauseState, .paused)
        let jet = try XCTUnwrap(status.seat(named: "JET"))
        XCTAssertEqual(jet.wake, "http")
        XCTAssertEqual(jet.wakeKind, .wakes)
        XCTAssertEqual(jet.wakeKind.label, "Wakes")
        XCTAssertEqual(jet.wakeKind.rowText, "Wakes")
        XCTAssertEqual(try XCTUnwrap(status.seat(named: "JET")).heldBack, 85)
        XCTAssertEqual(try XCTUnwrap(status.seat(named: "MM")).wakeKind, .other("fallback"))
        XCTAssertEqual(try XCTUnwrap(status.seat(named: "MM")).quota?.source, "unknown")
    }

    /// The answer the deployed helper gave through the Mac's real route (session BB, November 7,
    /// 2026 at 7:21 PM:  `server eavesdropper-status server --plan --json`) reads in full:  every
    /// seat is read, the Operator's voice the older container does not send reads Not reported,
    /// and a quota that says it is not known stays not known.
    func test_the_deployed_helpers_answer_reads_in_full() throws {
        let parsed = outcome(try fixture("server_eavesdropper_status_live", "json"))
        XCTAssertEqual(parsed.kind, .planned)
        XCTAssertNil(parsed.errorCode)
        let reading = ServerEavesdropperReading(outcome: parsed)
        let status = try XCTUnwrap(reading.status, "\(reading)")
        XCTAssertTrue(status.isThisAppsVersion)
        XCTAssertEqual(status.instance, "server")
        XCTAssertEqual(status.generatedAt, try date("2026-11-08T01:21:05Z"))
        XCTAssertEqual(status.statusUpdated, try date("2026-11-08T01:21:03Z"))
        XCTAssertEqual(status.seats.count, 29)
        XCTAssertEqual(status.operatorVoiceReading, .notReported)
        XCTAssertEqual(status.pause?.paused, false)
        for seat in status.seats {
            XCTAssertEqual(seat.connection, .connected, seat.seat)
            XCTAssertEqual(seat.runState, .running, seat.seat)
            XCTAssertEqual(seat.quotaReading, .notKnown, seat.seat)
            XCTAssertTrue(seat.wakeKind == .wakes || seat.wakeKind == .capturesOnly, seat.seat)
        }
        let now = try date("2026-11-08T01:22:00Z")
        guard case .read(let compiler) = reading.seat("BF-COMPILER", now: now) else {
            return XCTFail("\(reading.seat("BF-COMPILER", now: now))")
        }
        XCTAssertEqual(compiler.wakeKind, .capturesOnly)
        XCTAssertEqual(compiler.wakeKind.label, "Captures Only")
        XCTAssertEqual(ServerSeatWake(token: "muse"), .other("muse"))
        XCTAssertEqual(ServerSeatWake(token: "mcode").label, "mcode")
        XCTAssertEqual(ServerSeatWake(token: nil).rowText, "Wake: Not reported")
        XCTAssertEqual(compiler.bot, "BF-Compiler")
    }

    /// Each seat reads per seat:  a listed seat with its facts, and a seat the document does
    /// not hold says why, in words.
    func test_each_seat_reads_on_its_own() throws {
        let reading = ServerEavesdropperReading(outcome: outcome(plannedAnswer))
        let now = try date("2026-11-08T20:01:00Z")
        guard case .read(let claude) = reading.seat("CLAUDE", now: now) else {
            return XCTFail("\(reading.seat("CLAUDE", now: now))")
        }
        XCTAssertEqual(claude.seat, "CLAUDE")
        XCTAssertEqual(reading.seat("claude", now: now).facts?.seat, "CLAUDE")
        XCTAssertTrue(reading.seat("CLAUDE", now: now).isCurrent)

        let skipped = reading.seat("BAD", now: now)
        guard case .notHeld(let why) = skipped else { return XCTFail("\(skipped)") }
        XCTAssertTrue(why.contains("did not load this seat"), why)
        XCTAssertTrue(why.contains("Its credential setting is not valid."), why)
        XCTAssertFalse(why.contains("invalid_credentials"), why)
        XCTAssertTrue(why.contains("\u{00A0} "), why)

        guard case .notHeld(let off) = reading.seat("GROK-WEB", now: now) else { return XCTFail("disabled") }
        XCTAssertTrue(off.contains("turned off"), off)
        guard case .notHeld(let missing) = reading.seat("NOPE", now: now) else { return XCTFail("missing") }
        XCTAssertEqual(missing, "The server's eavesdropper does not list this seat.")
        XCTAssertNil(reading.seat("NOPE", now: now).facts)
    }

    /// Every scalar can be null:  each fact reads as not reported, never as zero, healthy or
    /// connected, and an odd field costs only that field.
    /// A line break or tab keeps the words apart, and control, bidirectional and zero-width
    /// characters never reach the screen (Sentry, PR 1483).
    func test_text_from_the_document_is_made_plain() {
        XCTAssertEqual(EavesdropperStatusText.plain("Fix\nbug", limit: 40), "Fix bug")
        XCTAssertEqual(EavesdropperStatusText.plain("a\tb\r\nc", limit: 40), "a b  c")
        XCTAssertEqual(EavesdropperStatusText.plain("a\u{2028}b\u{2029}c", limit: 40), "a b c")
        XCTAssertEqual(EavesdropperStatusText.plain("ab\u{202E}cd\u{200B}e\u{2066}f", limit: 40), "abcdef")
        XCTAssertEqual(EavesdropperStatusText.plain("x\u{0007}y", limit: 40), "xy")
        XCTAssertNil(EavesdropperStatusText.plain(" \n\u{200B}\t ", limit: 40))
        XCTAssertEqual(EavesdropperStatusText.plain("abcdef", limit: 3), "abc")
    }

    func test_every_scalar_null_reads_as_not_reported() throws {
        let status = try document(allNullDocument)
        XCTAssertNil(status.instance)
        // With no instance the document does not vouch for being the server's, so it is not
        // shown as the server's;  its facts are checked on the decoded document itself.
        XCTAssertEqual(ServerEavesdropperReading.reading(of: status),
                       .notRead("The answer does not say it is from the server's eavesdropper, so it is not shown."))
        XCTAssertNil(status.generatedAt)
        XCTAssertNil(status.statusUpdated)
        XCTAssertNil(status.age(now: Date()))
        XCTAssertEqual(status.operatorVoiceReading, .notReported)
        XCTAssertEqual(status.operatorVoiceReading.label, "Not reported")
        XCTAssertNil(status.pause?.paused)
        XCTAssertNil(status.pause?.since)
        XCTAssertNil(status.pause?.setBy)
        // A seat with no name and an entry that is not a seat are left out;  the others stay.
        XCTAssertEqual(status.seats.map(\.seat), ["CLAUDE", "ODD"])
        XCTAssertTrue(status.skipped.isEmpty)
        XCTAssertTrue(status.disabled.isEmpty)

        let now = try date("2026-11-08T20:00:00Z")
        for seat in status.seats {
            XCTAssertNil(seat.bot, seat.seat)
            XCTAssertEqual(seat.botText, "Not reported", seat.seat)
            XCTAssertEqual(seat.connection, .notReported, seat.seat)
            XCTAssertEqual(seat.connection.label, "Not reported", seat.seat)
            XCTAssertEqual(seat.runState, .notReported, seat.seat)
            XCTAssertEqual(seat.wakeKind, .notReported, seat.seat)
            XCTAssertEqual(seat.pauseState, .notReported, seat.seat)
            XCTAssertNil(seat.wakesLastDay, seat.seat)
            XCTAssertNil(seat.heldBack, seat.seat)
            XCTAssertNil(seat.jobs, seat.seat)
            XCTAssertEqual(seat.wakesLastDayText, "Not reported", seat.seat)
            XCTAssertEqual(seat.heldBackText, "Not reported", seat.seat)
            XCTAssertEqual(seat.jobsText, "Not reported", seat.seat)
            XCTAssertNil(seat.lastEvent, seat.seat)
            XCTAssertEqual(seat.lastEventLabel(now: now), "Not reported", seat.seat)
            XCTAssertEqual(seat.quotaReading, .notReported, seat.seat)
            XCTAssertEqual(seat.quotaReading.label, "Not reported", seat.seat)
            XCTAssertEqual(seat.letterState, .notReported, seat.seat)
        }
        let claude = try XCTUnwrap(status.seat(named: "CLAUDE"))
        XCTAssertEqual(claude.sessions.count, 1)
        let session = claude.sessions[0]
        XCTAssertNil(session.letter)
        XCTAssertNil(session.platform)
        XCTAssertNil(session.alive)
        XCTAssertNil(session.pending)
        XCTAssertNil(session.topics)
        XCTAssertNil(session.watcher)
        XCTAssertNil(session.whyText)
        XCTAssertEqual(session.aliveLabel, "Not reported")
        XCTAssertEqual(session.verifiedHostLabel, "Not reported")
        XCTAssertTrue(try XCTUnwrap(status.seat(named: "ODD")).sessions.isEmpty)

        // With no time on it, the reading is never current, and each seat says so.
        let seatReading = ServerEavesdropperReading.read(status).seat("CLAUDE", now: now)
        guard case .notCurrent(let facts?, let why) = seatReading else { return XCTFail("\(seatReading)") }
        XCTAssertEqual(facts.seat, "CLAUDE")
        XCTAssertEqual(why, "The server's answer does not say when it was read, so it is not current.")
        XCTAssertFalse(seatReading.isCurrent)
        XCTAssertEqual(seatReading.label, "Not current")
        // Facts from a reading that is not current are never offered as the present state.
        XCTAssertNil(seatReading.facts)
        XCTAssertEqual(seatReading.lastKnownFacts?.seat, "CLAUDE")
        XCTAssertNil(ServerEavesdropperReading.read(status).currentStatus(now: now))
    }

    /// A document that is not current never says a seat is not listed, skipped or turned off:
    /// an undated document with no seats (the reducer's empty document) and a stale one each
    /// say only that they are not current.
    func test_a_document_that_is_not_current_never_says_a_seat_is_not_listed() throws {
        let empty = try document(#"{"schema":1,"generated_at":null,"instance":"server","status_updated":null,"seats":[],"skipped":[],"disabled":[]}"#)
        let emptyReading = ServerEavesdropperReading.reading(of: empty)
        XCTAssertEqual(emptyReading, .read(empty))
        let now = try date("2026-11-08T20:00:00Z")
        let undated = emptyReading.seat("CLAUDE", now: now)
        XCTAssertEqual(undated, .notCurrent(nil, why: "The server's answer does not say when it was read, so it is not current."))
        XCTAssertEqual(undated.label, "Not current")
        XCTAssertNil(undated.facts)
        XCTAssertNil(undated.lastKnownFacts)

        // The sample answer, long after it was read:  a listed seat, a skipped one, a turned-off
        // one and a missing one all read as not current, never as not listed.
        let reading = ServerEavesdropperReading(outcome: outcome(plannedAnswer))
        let later = try date("2026-11-08T20:30:00Z")
        for name in ["BAD", "GROK-WEB", "NOPE"] {
            let seat = reading.seat(name, now: later)
            guard case .notCurrent(nil, let why) = seat else { return XCTFail("\(name): \(seat)") }
            XCTAssertTrue(why.contains("more than 5 minutes ago"), why)
            XCTAssertNotEqual(seat.label, "Not listed")
        }
        let claude = reading.seat("CLAUDE", now: later)
        XCTAssertNil(claude.facts)
        XCTAssertEqual(claude.lastKnownFacts?.connection, .connected)
        XCTAssertNil(reading.currentStatus(now: later))
        XCTAssertNotNil(reading.currentStatus(now: try date("2026-11-08T20:01:00Z")))
    }

    /// A known quota with nothing in it still says not reported, and a known quota that is not
    /// used up shows what is left.
    func test_a_quota_reads_only_what_it_says() {
        XCTAssertEqual(ServerSeatQuota.known(exhausted: nil, remainingPercent: nil, resetsAt: nil).label, "Not reported")
        XCTAssertEqual(ServerSeatQuota.known(exhausted: false, remainingPercent: 42, resetsAt: nil).label, "42% left")
        XCTAssertEqual(ServerSeatQuota.known(exhausted: false, remainingPercent: nil, resetsAt: nil).label, "Not used up")
        XCTAssertEqual(ServerSeatQuota.notKnown.label, "Not known")
    }

    /// A document of another version, or one that does not say its version, is refused.
    func test_a_document_of_another_version_is_refused() throws {
        for (schema, said) in [("2", "version 2"), ("0", "version 0"), ("\"1\"", "no version"), ("null", "no version")] {
            let status = try document(#"{"schema":"# + schema + #","seats":[]}"#)
            let reading = ServerEavesdropperReading.reading(of: status)
            guard case .notThisAppsVersion(let why) = reading else { return XCTFail("\(schema): \(reading)") }
            XCTAssertTrue(why.contains("(\(said))"), why)
            XCTAssertTrue(why.contains("\u{00A0} Update Muster"), why)
            XCTAssertEqual(reading.seat("CLAUDE", now: Date()), .notThisAppsVersion(why))
        }
        let missing = try document(#"{"seats":[]}"#)
        guard case .notThisAppsVersion = ServerEavesdropperReading.reading(of: missing) else {
            return XCTFail("a document with no version must be refused")
        }
        // Through the parser too.
        let newer = ServerEavesdropperReading(outcome: outcome(planned(#"{"schema":2,"seats":[]}"#)))
        guard case .notThisAppsVersion = newer else { return XCTFail("\(newer)") }
    }

    /// A document that says it is the Mac's is never shown as the server's.
    func test_a_document_for_the_mac_is_not_shown_as_the_servers() {
        let reading = ServerEavesdropperReading(outcome: outcome(planned(#"{"schema":1,"instance":"mac","seats":[]}"#)))
        guard case .notRead(let why) = reading else { return XCTFail("\(reading)") }
        XCTAssertTrue(why.contains("not the server's"), why)
    }

    /// The helper always writes `instance: "server"`, so a document that does not say so, or
    /// names something else, is not shown as the server's.
    func test_a_document_that_does_not_say_it_is_the_servers_is_not_shown() {
        let missing = ServerEavesdropperReading(outcome: outcome(planned(#"{"schema":1,"seats":[]}"#)))
        XCTAssertEqual(missing, .notRead("The answer does not say it is from the server's eavesdropper, so it is not shown."))
        let nullInstance = ServerEavesdropperReading(outcome: outcome(planned(#"{"schema":1,"instance":null,"seats":[]}"#)))
        XCTAssertEqual(nullInstance, missing)
        let other = ServerEavesdropperReading(outcome: outcome(planned(#"{"schema":1,"instance":"cloud","seats":[]}"#)))
        XCTAssertEqual(other, .notRead("The answer says it is from another eavesdropper, not the server's, so it is not shown."))
        let server = ServerEavesdropperReading(outcome: outcome(planned(#"{"schema":1,"instance":"server","seats":[]}"#)))
        guard case .read = server else { return XCTFail("\(server)") }
    }

    /// Each word the helper answers for an unreadable status maps to its own cause in plain
    /// words, through the parser, and nothing is called a success or changed.
    func test_each_unavailable_word_maps_to_its_cause() throws {
        // The helper's own sentence for each word (EAVESDROPPER_UNAVAILABLE in fleet-ops-control.py).
        let details = [
            "docker_unavailable": "docker could not be run",
            "timeout": "reading the status took too long",
            "container_not_running": "the eavesdropper container is not running",
            "read_failed": "the eavesdropper container or its status file could not be read",
            "too_large": "the status file is larger than the allowed size",
            "not_json": "the status file is not a JSON object",
            "module_unavailable": "the status reduction module is not installed beside the helper",
        ]
        XCTAssertEqual(Set(details.keys), Set(ServerEavesdropperUnavailable.knownCodes))
        var causes: Set<String> = []
        for code in ServerEavesdropperUnavailable.knownCodes {
            let detail = try XCTUnwrap(details[code])
            let answer = #"{"ok":false,"verb":"server eavesdropper-status","target":"server","commands":[],"result":"unavailable","detail":""#
                + detail + #"","errorCode":""# + code + #""}"#
            let parsed = outcome(answer, exit: 1)
            XCTAssertEqual(parsed.kind, .unavailable, code)
            XCTAssertFalse(parsed.isSuccess(for: .plan), code)
            XCTAssertEqual(parsed.errorCode, code)
            XCTAssertNil(parsed.eavesdropperStatus, code)
            XCTAssertFalse(parsed.runNotKnown, code)
            XCTAssertEqual(parsed.resultTitle(for: .eavesdropperStatus),
                           "The server could not read it, so nothing is known yet.\u{00A0} Nothing was changed.")

            let reading = ServerEavesdropperReading(outcome: parsed)
            guard case .unavailable(let reason) = reading else { return XCTFail("\(code): \(reading)") }
            XCTAssertEqual(reason.errorCode, code)
            XCTAssertNotEqual(reason, .other(code), code)
            let cause = reason.cause
            XCTAssertFalse(cause.isEmpty, code)
            XCTAssertTrue(cause.hasSuffix("."), code)
            XCTAssertFalse(cause.contains(code), "the cause names no reason word:  \(cause)")
            XCTAssertFalse(cause.contains("  "), cause)
            XCTAssertTrue(reason.help.contains(code), reason.help)
            causes.insert(cause)
            XCTAssertEqual(reading.seat("CLAUDE", now: Date()), .unavailable(reason))
            XCTAssertEqual(reading.seat("CLAUDE", now: Date()).explanation, cause)
        }
        XCTAssertEqual(causes.count, ServerEavesdropperUnavailable.knownCodes.count, "each word has its own cause")
        XCTAssertEqual(ServerEavesdropperUnavailable(errorCode: "container_not_running").cause,
                       "The eavesdropper is not running on the server.")
        XCTAssertTrue(ServerEavesdropperUnavailable(errorCode: "module_unavailable").cause
            .contains(".\u{00A0} It has to be installed"))
        let unknown = ServerEavesdropperUnavailable(errorCode: "something_new")
        XCTAssertEqual(unknown, .other("something_new"))
        XCTAssertFalse(causes.contains(unknown.cause))
        XCTAssertEqual(ServerEavesdropperUnavailable(errorCode: nil), .other(nil))
    }

    /// A helper from before the verb refuses it, and the reading says so in words.
    func test_an_older_helper_without_the_verb_reads_as_not_read_in_words() {
        let refused = #"{"ok":false,"verb":"server eavesdropper-status","target":"server","commands":[],"result":"refused","detail":"unknown server verb"}"#
        let parsed = outcome(refused, exit: 2)
        XCTAssertEqual(parsed.kind, .refused)
        let reading = ServerEavesdropperReading(outcome: parsed)
        XCTAssertEqual(reading, .notRead("The server's control helper does not have the eavesdropper status read yet, so the server's eavesdropper is not read."))
    }

    /// An answer that names another verb, or carries no document, is not read as one.
    func test_an_answer_without_this_verbs_document_is_not_read() {
        let otherVerb = #"{"ok":true,"verb":"server resource-status","result":"planned","commands":[],"status":{"schema":1,"seats":[]}}"#
        XCTAssertNil(outcome(otherVerb).eavesdropperStatus)
        XCTAssertEqual(ServerEavesdropperReading(outcome: outcome(otherVerb)),
                       .notRead("The server answered without its eavesdropper's status."))
        let noDocument = #"{"ok":true,"verb":"server eavesdropper-status","result":"planned","commands":[]}"#
        XCTAssertEqual(ServerEavesdropperReading(outcome: outcome(noDocument)),
                       .notRead("The server answered without its eavesdropper's status."))
        let notAnObject = planned("[1,2]")
        XCTAssertEqual(ServerEavesdropperReading(outcome: outcome(notAnObject)),
                       .notRead("The server answered without its eavesdropper's status."))
        // A planned answer that is not ok is not a reading either.
        let notOK = #"{"ok":false,"verb":"server eavesdropper-status","result":"planned","commands":[],"status":{"schema":1,"seats":[]}}"#
        guard case .notRead = ServerEavesdropperReading(outcome: outcome(notOK)) else {
            return XCTFail("an answer that is not ok must not be read")
        }
    }

    /// Only this verb's answer carries the document:  another action's answer never does.
    func test_another_actions_answer_never_carries_the_document() {
        let parsed = OpsResultParser.parse(
            OpsProcessResult(exitCode: 0, stdout: plannedAnswer, stderr: ""),
            mode: .plan, action: .resourceStatus, timeout: 90
        )
        XCTAssertNil(parsed.eavesdropperStatus)
    }
}

final class ServerEavesdropperCurrencyTests: XCTestCase {
    /// The window is the reducer's own five minutes.
    func test_the_window_is_five_minutes() {
        XCTAssertEqual(ReducedEavesdropperStatus.freshnessWindow, 300)
    }

    /// A reading is current inside the window and not current after it, by the age of the
    /// server's answer.
    func test_a_reading_goes_stale_after_the_window() throws {
        let status = try document(timedDocument(generated: "2026-11-08T20:00:00Z", updated: "2026-11-08T19:59:58Z"))
        let generated = try date("2026-11-08T20:00:00Z")
        let reading = ServerEavesdropperReading.reading(of: status)

        let inside = generated.addingTimeInterval(298)
        XCTAssertTrue(status.isCurrent(now: inside))
        XCTAssertEqual(status.age(now: inside), 298)
        XCTAssertEqual(status.ageText(now: inside), "4m")
        XCTAssertTrue(reading.seat("MA", now: inside).isCurrent)

        let after = generated.addingTimeInterval(301)
        XCTAssertFalse(status.isCurrent(now: after))
        let why = try XCTUnwrap(status.notCurrentReason(now: after))
        XCTAssertTrue(why.hasPrefix("The server read this at 2:00 PM"), why)
        XCTAssertTrue(why.contains("more than 5 minutes ago"), why)
        let seat = reading.seat("MA", now: after)
        guard case .notCurrent(let facts?, let seatWhy) = seat else { return XCTFail("\(seat)") }
        XCTAssertEqual(facts.runState, .running)
        XCTAssertEqual(seatWhy, why)
        XCTAssertEqual(seat.explanation, why)
        // Running and Connected are the last known facts only, never the present state.
        XCTAssertNil(seat.facts)
        XCTAssertEqual(seat.lastKnownFacts?.connection, .connected)
        XCTAssertNil(reading.currentStatus(now: after))
        XCTAssertEqual(reading.currentStatus(now: inside), status)
    }

    /// The listener's own status must be current too, even inside a current answer.
    func test_an_old_listener_status_is_not_current() throws {
        let status = try document(timedDocument(generated: "2026-11-08T20:00:00Z", updated: "2026-11-08T19:50:00Z"))
        let why = try XCTUnwrap(status.notCurrentReason(now: try date("2026-11-08T20:00:30Z")))
        XCTAssertTrue(why.hasPrefix("The server's eavesdropper last wrote its status at 1:50 PM"), why)
        let unstamped = try document(#"{"schema":1,"generated_at":"2026-11-08T20:00:00Z","seats":[]}"#)
        XCTAssertEqual(unstamped.notCurrentReason(now: try date("2026-11-08T20:00:30Z")),
                       "The server's eavesdropper does not say when it last wrote its status, so it is not current.")
    }

    /// An answer dated well ahead of this Mac's clock is not current either.
    func test_an_answer_from_the_future_is_not_current() throws {
        let status = try document(timedDocument(generated: "2026-11-08T20:00:00Z", updated: "2026-11-08T20:00:00Z"))
        let early = try date("2026-11-08T19:54:00Z")
        let why = try XCTUnwrap(status.notCurrentReason(now: early))
        XCTAssertTrue(why.contains("ahead of this Mac's clock"), why)
        // The reducer's rule (PR 1479):  up to 5 seconds ahead is current, more is not.
        XCTAssertTrue(status.isCurrent(now: try date("2026-11-08T19:59:57Z")))
        XCTAssertFalse(status.isCurrent(now: try date("2026-11-08T19:59:54Z")))
        XCTAssertFalse(status.isCurrent(now: try date("2026-11-08T19:56:00Z")))
    }
}

/// The registry's stale sweep (AFC-721), as `reduce_status` sends it.  `liveSweep` is the
/// reducer's output for the server's live block of November 11, 2026, whose source carried the
/// letters, record ids and machines this document never has.
private let liveSweep = #"""
{"ran_at":"2026-11-12T03:13:23Z","ok":false,"ended":11,"error":"sweep_share_exceeded","stale_after_minutes":2160,"refused":1,"refused_platforms":["antigravity"]}
"""#

/// A current server document generated at 9:13:30 PM Chicago time on November 11, 2026, with
/// `sweep` as its `registry_sweep` (JSON text), or with no such key when it is nil.
private func sweepDocument(_ sweep: String?) -> String {
    let block = sweep.map { #","registry_sweep":"# + $0 } ?? ""
    return #"{"schema":1,"generated_at":"2026-11-12T03:13:30Z","instance":"server","status_updated":"2026-11-12T03:13:28Z","seats":[],"skipped":[],"disabled":[]"#
        + block + "}"
}

final class ServerRegistrySweepTests: XCTestCase {
    private let gap = "\u{00A0} "

    /// The live block reads in full:  when it ran, its error, what it closed and the platform the
    /// share guard held back, in sentences with the house gap.
    func test_the_live_block_reads_in_full() throws {
        let status = try document(sweepDocument(liveSweep))
        let sweep = try XCTUnwrap(status.registrySweep)
        XCTAssertEqual(sweep.ranAt, try date("2026-11-12T03:13:23Z"))
        XCTAssertEqual(sweep.ok, false)
        XCTAssertEqual(sweep.ended, 11)
        XCTAssertEqual(sweep.error, "sweep_share_exceeded")
        XCTAssertEqual(sweep.staleAfterMinutes, 2160)
        XCTAssertEqual(sweep.refused, 1)
        XCTAssertEqual(sweep.refusedPlatforms, ["antigravity"])
        XCTAssertTrue(sweep.isReported)
        XCTAssertTrue(sweep.isProblem)
        XCTAssertEqual(sweep.symbolName, "exclamationmark.triangle")
        XCTAssertEqual(sweep.label, "sweep_share_exceeded")
        let now = try date("2026-11-12T03:20:00Z")
        XCTAssertEqual(
            sweep.sentence(now: now),
            "It last ran at 9:13 PM." + gap
                + "It closed 11 chats quiet for more than 36 hours." + gap
                + "Held back: antigravity." + gap
                + "The share guard stopped it closing too many of a platform's open chats on one machine at once, so it left those chats open."
        )
        // Shown only from a current reading, as the panel does.
        let reading = ServerEavesdropperReading.reading(of: status)
        XCTAssertEqual(reading.currentStatus(now: try date("2026-11-12T03:14:00Z"))?.registrySweep, sweep)
        XCTAssertNil(reading.currentStatus(now: try date("2026-11-12T03:30:00Z")))
        // Through the parser too.
        let parsed = ServerEavesdropperReading(outcome: outcome(planned(sweepDocument(liveSweep))))
        XCTAssertEqual(parsed.status?.registrySweep, sweep)
    }

    /// A clean pass reads OK, and a store problem says so in words.
    func test_a_clean_pass_and_a_store_problem() throws {
        let clean = try XCTUnwrap(try document(sweepDocument(
            #"{"ran_at":"2026-11-12T03:13:23Z","ok":true,"ended":1,"error":null,"stale_after_minutes":90,"refused":0,"refused_platforms":[]}"#
        )).registrySweep)
        XCTAssertFalse(clean.isProblem)
        XCTAssertEqual(clean.symbolName, "checkmark.circle")
        XCTAssertEqual(clean.label, "OK")
        let now = try date("2026-11-12T03:20:00Z")
        XCTAssertEqual(clean.sentence(now: now), "It last ran at 9:13 PM." + gap + "It closed 1 chat quiet for more than 90 minutes.")

        let missing = try XCTUnwrap(try document(sweepDocument(
            #"{"ran_at":"2026-11-12T03:13:23Z","ok":false,"ended":0,"error":"registry_unavailable","stale_after_minutes":2160,"refused":0,"refused_platforms":[]}"#
        )).registrySweep)
        XCTAssertEqual(missing.label, "registry_unavailable")
        XCTAssertEqual(
            missing.sentence(now: now),
            "It last ran at 9:13 PM." + gap
                + "It closed no chats quiet for more than 36 hours." + gap
                + "The session registry was not there, so it closed nothing."
        )
        XCTAssertFalse(missing.sentence(now: now).contains("Held back"))
        XCTAssertEqual(ServerRegistrySweepFields.quietText(60), "1 hour")
        XCTAssertEqual(ServerRegistrySweepFields.quietText(1), "1 minute")
        XCTAssertNil(ServerRegistrySweepFields.quietText(0))
        XCTAssertNil(ServerRegistrySweepFields.quietText(nil))
    }

    /// An older helper sends no block, and the Mac or a server that has not swept yet sends one
    /// with every field null:  neither is shown.
    func test_an_absent_or_all_null_block_is_not_reported() throws {
        XCTAssertNil(try document(sweepDocument(nil)).registrySweep)
        XCTAssertNil(try XCTUnwrap(sampleReading().status).registrySweep)
        let empty = try XCTUnwrap(try document(sweepDocument(
            #"{"ran_at":null,"ok":null,"ended":null,"error":null,"stale_after_minutes":null,"refused":null,"refused_platforms":[]}"#
        )).registrySweep)
        XCTAssertFalse(empty.isReported)
        XCTAssertFalse(empty.isProblem)
        XCTAssertEqual(empty.label, "Not reported")
        XCTAssertEqual(empty.sentence(now: Date()), "It does not say when it last ran.")
    }

    /// A field of the wrong type is nil and costs no other field;  a block that is not an object
    /// is nil and costs nothing else in the document.
    func test_a_malformed_block_degrades_field_by_field() throws {
        let odd = try XCTUnwrap(try document(sweepDocument(
            #"{"ran_at":"yesterday","ok":"yes","ended":"11","error":7,"stale_after_minutes":-5,"refused":1.5,"refused_platforms":["cursor",5,null,"  ","a‮b","p1","p2","p3","p4","p5","p6","p7","p8","p9","p10","p11","p12","p13","p14","p15","p16"]}"#
        )).registrySweep)
        XCTAssertNil(odd.ranAt)
        XCTAssertNil(odd.ok)
        XCTAssertNil(odd.ended)
        XCTAssertNil(odd.error)
        XCTAssertNil(odd.staleAfterMinutes)
        XCTAssertNil(odd.refused)
        XCTAssertEqual(odd.refusedPlatforms.count, ServerRegistrySweepFields.maxPlatforms)
        XCTAssertEqual(Array(odd.refusedPlatforms.prefix(3)), ["cursor", "ab", "p1"])
        // `ran_at` was text, so the block still says something.
        XCTAssertTrue(odd.isReported)
        XCTAssertEqual(odd.label, "Not reported")
        // Shown, but an unknown outcome is never drawn as healthy (Kody, PR #1884).
        XCTAssertEqual(odd.symbolName, "questionmark.circle")
        XCTAssertFalse(odd.isProblem)
        // `refused` counts platform-and-machine groups, never platforms (Kody, PR #1884).
        let twoGroups = try XCTUnwrap(try document(sweepDocument(
            #"{"ran_at":"2026-11-12T03:13:23Z","ok":false,"ended":0,"error":"sweep_share_exceeded","stale_after_minutes":2160,"refused":2,"refused_platforms":[]}"#
        )).registrySweep)
        XCTAssertTrue(twoGroups.sentence(now: try date("2026-11-12T03:20:00Z"))
            .contains("It held back 2 groups of chats, each one platform on one machine."))

        for block in [#""ok""#, "5", "[]", "true", "null"] {
            let status = try document(sweepDocument(block))
            XCTAssertNil(status.registrySweep, block)
            XCTAssertEqual(status.instance, "server", block)
            XCTAssertEqual(status.schema, 1, block)
        }
    }
}

/// A current server document generated at 12:00 PM Chicago time on November 12, 2026, holding
/// the one seat `seat` (JSON text).
private func routineDocument(_ seat: String) -> String {
    #"{"schema":1,"generated_at":"2026-11-12T18:00:00Z","instance":"server","status_updated":"2026-11-12T17:59:58Z","seats":["#
        + seat + #"],"skipped":[],"disabled":[]}"#
}

/// Whether a seat's wakes go to a ready router:  the reducer's per-seat `routine` (a flag and two
/// fixed words), and the one line a seat's row shows for it.
final class ServerSeatRouterTests: XCTestCase {
    private func seat(_ text: String) throws -> ServerSeatFacts {
        try XCTUnwrap(try document(routineDocument(text)).seat(named: "CLAUDE"))
    }

    /// The live server's CLAUDE seat on November 12, as the reducer sends it.
    func test_a_ready_routine_reads_as_ready() throws {
        let facts = try seat(
            #"{"seat":"CLAUDE","connected":true,"running":"running","wake":"http","routine":{"ready":true,"auth":"bearer","why":null}}"#
        )
        XCTAssertTrue(facts.routineReported)
        XCTAssertEqual(facts.routine, ServerSeatRoutineFields(ready: true, auth: "bearer", why: nil))
        XCTAssertEqual(facts.router, .ready)
        XCTAssertEqual(facts.router.line, "Wakes go to the router, ready")
        XCTAssertFalse(facts.router.isProblem)
        // Readiness only:  the line never claims a wake was delivered or accepted.
        let line = try XCTUnwrap(facts.router.line)
        for word in ["202", "accepted", "delivered", "reached"] {
            XCTAssertFalse(line.contains(word), word)
        }
        // Shown from a current reading only.
        let reading = ServerEavesdropperReading.reading(of: try document(routineDocument(
            #"{"seat":"CLAUDE","wake":"http","routine":{"ready":true,"auth":"bearer","why":null}}"#
        )))
        XCTAssertEqual(reading.seat("CLAUDE", now: try date("2026-11-12T18:01:00Z")).facts?.router, .ready)
        XCTAssertNil(reading.seat("CLAUDE", now: try date("2026-11-12T18:30:00Z")).facts)
    }

    /// A routine that is not ready says why in plain words, and is a problem.
    func test_a_routine_that_is_not_ready_says_why() throws {
        let cases: [(String, String)] = [
            (#""routine_not_configured""#, "its address or key is not set on the server"),
            (#""routine_key_refused""#, "its key cannot be used"),
            (#""routine_url_refused""#, "its address cannot be used"),
            (#""other""#, "the server did not say why"),
            ("null", "the server did not say why"),
        ]
        for (why, words) in cases {
            let facts = try seat(
                #"{"seat":"CLAUDE","wake":"http","routine":{"ready":false,"auth":"bearer","why":"# + why + "}}"
            )
            XCTAssertEqual(facts.router, .notReady(words), why)
            XCTAssertEqual(facts.router.line, "Wakes go to the router, not ready: " + words, why)
            XCTAssertTrue(facts.router.isProblem, why)
        }
    }

    /// A reducer that sends the key with null says the seat has no routine:  a seat whose mentions
    /// stay in its inbox only captures, and gets no router line, because its listener cell already
    /// says Captures Only;  a seat that wakes some other way gets no line either.
    func test_a_null_routine_is_capture_only_for_an_inbox_seat() throws {
        let inbox = try seat(#"{"seat":"CLAUDE","wake":"inbox","routine":null}"#)
        XCTAssertTrue(inbox.routineReported)
        XCTAssertNil(inbox.routine)
        XCTAssertEqual(inbox.router, .captureOnly)
        XCTAssertNil(inbox.router.line)
        XCTAssertFalse(inbox.router.isProblem)
        XCTAssertEqual(inbox.wakeKind.rowText, "Captures Only")

        let headless = try seat(#"{"seat":"CLAUDE","wake":"claude","routine":null}"#)
        XCTAssertEqual(headless.router, .notSaid)
        XCTAssertNil(headless.router.line)

        // A seat that wakes through the router always has a routine, so a null one is a routine
        // the reducer could not read, never a ready one.
        let unread = try seat(#"{"seat":"CLAUDE","wake":"http","routine":null}"#)
        XCTAssertEqual(unread.router, .readinessNotReported)
        XCTAssertEqual(unread.router.line, "Wakes go to the router, readiness not reported")
    }

    /// An older reducer sends no `routine` key at all:  Muster then says nothing about the
    /// router, whatever the seat's wake is.
    func test_an_absent_routine_from_an_older_reducer_says_nothing() throws {
        for wake in [#""http""#, #""inbox""#, #""claude""#, "null"] {
            let facts = try seat(#"{"seat":"CLAUDE","wake":"# + wake + "}")
            XCTAssertFalse(facts.routineReported, wake)
            XCTAssertNil(facts.routine, wake)
            XCTAssertEqual(facts.router, .notSaid, wake)
            XCTAssertNil(facts.router.line, wake)
        }
        let status = try XCTUnwrap(sampleReading().status)
        for facts in status.seats {
            XCTAssertNil(facts.router.line, facts.seat)
        }
    }

    /// A block of the wrong shape reads field by field:  a wrong-typed field is nil, a block
    /// that is not an object is no routine, and a key the reducer never sends is not read.
    func test_a_malformed_routine_degrades_field_by_field() throws {
        let odd = try seat(
            #"{"seat":"CLAUDE","wake":"http","routine":{"ready":"true","auth":7,"why":["x"],"host":"router.invalid","detail":"not set"}}"#
        )
        XCTAssertEqual(odd.routine, ServerSeatRoutineFields(ready: nil, auth: nil, why: nil))
        XCTAssertEqual(odd.router, .readinessNotReported)

        for block in [#""ready""#, "5", "[]", "true"] {
            let facts = try seat(#"{"seat":"CLAUDE","wake":"inbox","routine":"# + block + "}")
            XCTAssertNil(facts.routine, block)
            XCTAssertTrue(facts.routineReported, block)
            XCTAssertEqual(facts.router, .captureOnly, block)
        }
        // The rest of the seat still reads.
        let rest = try seat(#"{"seat":"CLAUDE","connected":true,"wake":"http","routine":"bad"}"#)
        XCTAssertEqual(rest.connected, true)
        XCTAssertEqual(rest.wakeKind, .wakes)
    }
}

@MainActor
final class ServerEavesdropperStoreTests: XCTestCase {
    actor Counter {
        private(set) var value = 0
        func bump() { value += 1 }
    }

    /// Before the first read, a seat says it has not been read.
    func test_nothing_is_known_before_the_first_read() {
        let store = ServerEavesdropperStatusStore(reader: { sampleReading() })
        XCTAssertNil(store.reading)
        XCTAssertEqual(store.seat("CLAUDE"), .notRead("The server's eavesdropper has not been read yet."))
    }

    /// Two refreshes at once start one read, and both see its answer.
    func test_two_refreshes_at_once_start_one_read() async {
        let counter = Counter()
        let store = ServerEavesdropperStatusStore(reader: {
            await counter.bump()
            try? await Task.sleep(nanoseconds: 200_000_000)
            return sampleReading()
        })
        async let first: Void = store.refresh()
        async let second: Void = store.refresh()
        _ = await (first, second)
        let reads = await counter.value
        XCTAssertEqual(reads, 1)
        XCTAssertEqual(store.readsStarted, 1)
        XCTAssertEqual(store.reading, sampleReading())
        XCTAssertFalse(store.loading)
    }

    /// A caller that waits on another caller's read finds the reading stored as soon as its own
    /// refresh returns, whichever of the two resumes first.
    func test_a_waiting_refresh_returns_with_the_reading_stored() async {
        let store = ServerEavesdropperStatusStore(reader: {
            try? await Task.sleep(nanoseconds: 200_000_000)
            return sampleReading()
        })
        let first = Task { () -> ServerEavesdropperReading? in
            await store.refresh()
            return store.reading
        }
        let second = Task { () -> ServerEavesdropperReading? in
            await store.refresh()
            return store.reading
        }
        let seenBySecond = await second.value
        let seenByFirst = await first.value
        XCTAssertEqual(seenBySecond, sampleReading())
        XCTAssertEqual(seenByFirst, sampleReading())
        XCTAssertEqual(store.readsStarted, 1)
        XCTAssertNotNil(store.readAt)
        XCTAssertFalse(store.loading)
    }

    /// The last reading is kept with its time and reused inside the window the caller names;
    /// each seat is judged against the clock when it is asked for.
    func test_the_last_reading_is_cached_with_its_time() async throws {
        var clock = try date("2026-11-08T20:00:30Z")
        let store = ServerEavesdropperStatusStore(reader: { sampleReading() }, now: { clock })
        await store.refresh()
        XCTAssertEqual(store.readAt, clock)
        XCTAssertTrue(store.seat("CLAUDE").isCurrent)

        clock = clock.addingTimeInterval(30)
        await store.refreshIfOlder(than: 60)
        XCTAssertEqual(store.readsStarted, 1)

        // The same reading, asked for after the window, is not current.
        clock = try date("2026-11-08T20:06:00Z")
        guard case .notCurrent = store.seat("CLAUDE") else { return XCTFail("\(store.seat("CLAUDE"))") }
        await store.refreshIfOlder(than: 60)
        XCTAssertEqual(store.readsStarted, 2)
        XCTAssertEqual(store.readAt, clock)
    }
}
