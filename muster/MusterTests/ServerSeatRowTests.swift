import XCTest

// What a server seat's row in Seat Activity shows, for each thing the server eavesdropper's
// reading can say about the seat (ServerSeatRowPresentation).  The document below is shaped like
// the reducer's own output (scripts/simple_tools/reduced_status.py), generated at 2:00 PM on
// Nov 8, 2026;  the live one is session BB's capture of the deployed helper's answer.

private let rowDocumentText = #"""
{"schema":1,"generated_at":"2026-11-08T20:00:00Z","instance":"server","status_updated":"2026-11-08T19:59:58Z",
 "pause":{"paused":false,"since":null,"wakes_only":null,"set_by":null},
 "seats":[
  {"seat":"CLAUDE","bot":"Claude","connected":true,"running":"running","wake":"claude","paused":false,
   "wakes_24h":3,"held_back":2,"jobs":1,"last_event":"2026-11-08T19:59:30Z",
   "quota":{"known":true,"exhausted":true,"remaining":0,"reset_at":"2026-11-09T05:00:00Z"},
   "leases":[{"letter":"AA","platform":"claude-code","alive":true,"pending":1,"verified_host":"server","why":"ok","topics":2,"watcher":true},
             {"letter":null,"platform":"cursor","alive":false,"pending":1,"verified_host":null,"why":"pid_gone","topics":2,"watcher":false}],
   "letters":{"known":false,"why":"partial"}},
  {"seat":"DOWN","bot":"MA","connected":false,"running":"stopped","wake":"inbox","paused":false,"leases":[],"quota":{"known":false}},
  {"seat":"HALTED","bot":"MA","connected":true,"running":"running","wake":"inbox","paused":true,"leases":[],"quota":{"known":false}}
 ],
 "skipped":[{"seat":"BAD","reason":"invalid_credentials"}],
 "disabled":["GROK-WEB"]}
"""#

private func rowDate(_ text: String) throws -> Date {
    try XCTUnwrap(FleetTime.parse(text), text)
}

private func rowDocument(_ text: String = rowDocumentText) throws -> ReducedEavesdropperStatus {
    try JSONDecoder().decode(ReducedEavesdropperStatus.self, from: Data(text.utf8))
}

private let rowGap = ServerStatusCopy.sentenceGap

final class ServerSeatRowTests: XCTestCase {
    private func presentation(
        _ seat: String,
        at now: String,
        readAt: String? = "2026-11-08T20:00:30Z",
        loading: Bool = false,
        document text: String = rowDocumentText
    ) throws -> ServerSeatRowPresentation {
        let status = try rowDocument(text)
        let reading = ServerEavesdropperReading.reading(of: status)
        let clock = try rowDate(now)
        return ServerSeatRowPresentation(
            reading: reading.seat(seat, now: clock),
            status: reading.status,
            readAt: try readAt.map(rowDate),
            loading: loading,
            now: clock
        )
    }

    // MARK: A current reading

    /// A current reading shows the seat's state, wake, last event, sessions and quota as the
    /// server said them, with no sentence under the row.
    func test_a_current_reading_shows_the_servers_facts() throws {
        let row = try presentation("CLAUDE", at: "2026-11-08T20:01:00Z")
        XCTAssertTrue(row.isCurrent)
        XCTAssertEqual(row.state, "Connected")
        XCTAssertFalse(row.stateIsProblem)
        XCTAssertEqual(row.details, ["Running", "Wake: Claude", "Last event 1:59 PM"])
        XCTAssertEqual(row.sessions, "1 session")
        XCTAssertTrue(row.sessionsLive)
        XCTAssertEqual(row.sessionLines, [
            "AA, claude-code, verified on the server, 2 topics, 1 waiting",
            "No letter reported, cursor, 2 topics, 1 waiting, ended",
        ])
        XCTAssertEqual(row.quota, "Used up")
        XCTAssertTrue(row.quotaIsProblem)
        XCTAssertEqual(row.quotaReset, try rowDate("2026-11-09T05:00:00Z"))
        XCTAssertNil(row.note)
        XCTAssertFalse(row.noteIsProblem)
        XCTAssertTrue(row.help.hasPrefix("What the server's eavesdropper says about this seat, read at 2:00 PM."), row.help)
        XCTAssertTrue(row.help.contains("Wakes in the last day: 3."), row.help)
        XCTAssertTrue(row.help.contains("Held back: 2."), row.help)
        XCTAssertTrue(row.help.contains("Jobs: 1."), row.help)
        XCTAssertTrue(row.help.contains("Session letters: Some known."), row.help)
        XCTAssertTrue(row.help.contains(".\(rowGap)"), row.help)
    }

    /// A seat the server says is not connected and stopped is flagged, and an absent last
    /// event is left out rather than shown as a time.
    func test_a_disconnected_seat_is_flagged() throws {
        let row = try presentation("DOWN", at: "2026-11-08T20:01:00Z")
        XCTAssertEqual(row.state, "Not connected")
        XCTAssertTrue(row.stateIsProblem)
        XCTAssertEqual(row.details, ["Stopped", "Wake: Inbox"])
        XCTAssertEqual(row.sessions, "None")
        XCTAssertFalse(row.sessionsLive)
        XCTAssertEqual(row.quota, "Not known")
        XCTAssertFalse(row.quotaIsProblem)
        XCTAssertNil(row.quotaReset)
    }

    /// A paused seat reads Paused first, with its connection still said under it.
    func test_a_paused_seat_reads_paused_with_its_connection() throws {
        let row = try presentation("HALTED", at: "2026-11-08T20:01:00Z")
        XCTAssertEqual(row.state, "Paused")
        XCTAssertTrue(row.stateIsProblem)
        XCTAssertEqual(row.details, ["Connected", "Running", "Wake: Inbox"])
    }

    /// A session whose liveness is not said is never counted as ended, and no session list
    /// reads as None.
    func test_the_session_count_never_turns_unsaid_into_ended() throws {
        let unsaid = try JSONDecoder().decode(ServerSeatSession.self, from: Data(#"{"letter":"B","alive":null}"#.utf8))
        let ended = try JSONDecoder().decode(ServerSeatSession.self, from: Data(#"{"letter":"C","alive":false}"#.utf8))
        XCTAssertEqual(ServerSeatRowPresentation.sessionCount([]), "None")
        XCTAssertEqual(ServerSeatRowPresentation.sessionCount([ended]), "None live")
        XCTAssertEqual(ServerSeatRowPresentation.sessionCount([ended, unsaid]), "Not reported")
        XCTAssertEqual(ServerSeatRowPresentation.sessionLine(unsaid), "B, rewake watcher not said, alive or not is not said")
    }

    // MARK: A reading that is not current

    /// A reading that is not current puts no state in the cells:  the row says why, how old
    /// the answer is, and what it last said, as old evidence.
    func test_a_reading_that_is_not_current_is_shown_only_as_old_evidence() throws {
        let row = try presentation("CLAUDE", at: "2026-11-08T20:10:00Z")
        XCTAssertFalse(row.isCurrent)
        XCTAssertEqual(row.state, "Not current")
        XCTAssertTrue(row.stateIsProblem)
        XCTAssertEqual(row.details, ["Reported 10m ago"])
        XCTAssertEqual(row.sessions, "Not current")
        XCTAssertFalse(row.sessionsLive)
        XCTAssertTrue(row.sessionLines.isEmpty)
        XCTAssertEqual(row.quota, "Not current")
        XCTAssertFalse(row.quotaIsProblem)
        XCTAssertNil(row.quotaReset)
        XCTAssertTrue(row.noteIsProblem)
        XCTAssertEqual(row.note,
            "The server read this at 2:00 PM, more than 5 minutes ago, so it is not current."
                + rowGap + "The last reading said: Connected, Running, wake Claude, quota Used up."
                + rowGap + "That is old evidence, not the seat's state now.")
        XCTAssertEqual(row.help, "The server read this at 2:00 PM, more than 5 minutes ago, so it is not current.")
    }

    /// An old reading that does not hold the seat never says the seat is gone.
    func test_an_old_reading_without_the_seat_does_not_say_it_is_gone() throws {
        let row = try presentation("NOPE", at: "2026-11-08T20:10:00Z")
        XCTAssertEqual(row.state, "Not current")
        let note = try XCTUnwrap(row.note)
        XCTAssertTrue(note.hasPrefix("The server read this at 2:00 PM"), note)
        XCTAssertTrue(note.hasSuffix("That old reading does not hold this seat, which says nothing about the seat now."), note)
        XCTAssertFalse(note.contains("does not list"), note)
    }

    // MARK: No current reading

    /// A current document that does not hold the seat says why, and that the partition gives
    /// the seat to the server, so the mismatch between the two is a thing to look at.
    func test_a_seat_the_server_does_not_hold_says_why() throws {
        let skipped = try presentation("BAD", at: "2026-11-08T20:01:00Z")
        XCTAssertEqual(skipped.state, "Not held")
        XCTAssertEqual(skipped.sessions, "Not held")
        XCTAssertEqual(skipped.quota, "Not held")
        XCTAssertTrue(skipped.stateIsProblem)
        XCTAssertTrue(skipped.noteIsProblem)
        XCTAssertEqual(skipped.note,
            "The server's eavesdropper did not load this seat." + rowGap + "Its credential setting is not valid."
                + rowGap + "The seat partition gives this seat to the server.")

        let off = try presentation("GROK-WEB", at: "2026-11-08T20:01:00Z")
        XCTAssertEqual(off.note,
            "The server's eavesdropper has this seat turned off in its settings." + rowGap
                + "The seat partition gives this seat to the server.")

        let missing = try presentation("NOPE", at: "2026-11-08T20:01:00Z")
        XCTAssertEqual(missing.note,
            "The server's eavesdropper does not list this seat." + rowGap + "The seat partition gives this seat to the server.")
    }

    /// Before the first read the row is not a fault:  it says it has not been read yet, or that
    /// it is being read.
    func test_before_the_first_read_the_row_is_not_a_fault() throws {
        let reading = ServerSeatReading.notRead("The server's eavesdropper has not been read yet.")
        let now = try rowDate("2026-11-08T20:01:00Z")
        let idle = ServerSeatRowPresentation(reading: reading, status: nil, readAt: nil, loading: false, now: now)
        XCTAssertEqual(idle.state, "Not read yet")
        XCTAssertEqual(idle.sessions, "Not read yet")
        XCTAssertEqual(idle.quota, "Not read yet")
        XCTAssertFalse(idle.stateIsProblem)
        XCTAssertFalse(idle.noteIsProblem)
        XCTAssertEqual(idle.note, "The server's eavesdropper has not been read yet.")

        let reading2 = ServerSeatRowPresentation(reading: reading, status: nil, readAt: nil, loading: true, now: now)
        XCTAssertEqual(reading2.state, "Reading\u{2026}")
        XCTAssertEqual(reading2.note, "Muster is reading the server's eavesdropper now.")
        XCTAssertFalse(reading2.noteIsProblem)
    }

    /// A read that came back with no document says so, with the reason, as a problem.
    func test_a_read_that_failed_says_why() throws {
        let now = try rowDate("2026-11-08T20:01:00Z")
        let why = "The server's control helper does not have the eavesdropper status read yet, so the server's eavesdropper is not read."
        let row = ServerSeatRowPresentation(reading: .notRead(why), status: nil, readAt: now, loading: false, now: now)
        XCTAssertEqual(row.state, "Not read")
        XCTAssertTrue(row.stateIsProblem)
        XCTAssertEqual(row.note, why)
        XCTAssertTrue(row.noteIsProblem)
        XCTAssertFalse(row.isCurrent)
    }

    /// When the server could not read its eavesdropper, the row gives the cause in words and the
    /// helper's own word only in the tooltip.
    func test_an_unavailable_reading_gives_the_cause() throws {
        let now = try rowDate("2026-11-08T20:01:00Z")
        let reason = ServerEavesdropperUnavailable(errorCode: "container_not_running")
        let row = ServerSeatRowPresentation(reading: .unavailable(reason), status: nil, readAt: now, loading: false, now: now)
        XCTAssertEqual(row.state, "Not available")
        XCTAssertEqual(row.sessions, "Not available")
        XCTAssertEqual(row.quota, "Not available")
        XCTAssertEqual(row.note, "The eavesdropper is not running on the server.")
        XCTAssertFalse(row.note?.contains("container_not_running") ?? true)
        XCTAssertTrue(row.help.contains("container_not_running"), row.help)
        XCTAssertTrue(row.noteIsProblem)
    }

    /// An answer in a version this app does not read says to update Muster.
    func test_an_answer_this_version_cannot_read_says_to_update_muster() throws {
        let newer = rowDocumentText.replacingOccurrences(of: #"{"schema":1,"#, with: #"{"schema":2,"#)
        let row = try presentation("CLAUDE", at: "2026-11-08T20:01:00Z", document: newer)
        XCTAssertEqual(row.state, "Not readable")
        XCTAssertTrue(row.stateIsProblem)
        let note = try XCTUnwrap(row.note)
        XCTAssertTrue(note.contains("version 2"), note)
        XCTAssertTrue(note.hasSuffix("Update Muster to read it."), note)
        XCTAssertNil(row.quotaReset)
        XCTAssertTrue(row.sessionLines.isEmpty)
    }

    /// No sentence a server row shows has two plain spaces:  sentences are joined by the sentence gap.
    func test_every_sentence_uses_the_gap() throws {
        let rows = try [
            presentation("CLAUDE", at: "2026-11-08T20:01:00Z"),
            presentation("CLAUDE", at: "2026-11-08T20:10:00Z"),
            presentation("NOPE", at: "2026-11-08T20:10:00Z"),
            presentation("BAD", at: "2026-11-08T20:01:00Z"),
        ]
        for row in rows {
            XCTAssertFalse(row.help.contains("  "), row.help)
            if let note = row.note { XCTAssertFalse(note.contains("  "), note) }
        }
    }

    // MARK: The deployed helper's answer

    /// The deployed helper's answer (session BB, November 7, 2026 at 7:21 PM) shows every seat
    /// as current and connected while it is current, and as old evidence once it is not.
    func test_the_deployed_helpers_answer_shows_each_seat() throws {
        let parsed = OpsResultParser.parse(
            OpsProcessResult(exitCode: 0, stdout: try fixture("server_eavesdropper_status_live", "json"), stderr: ""),
            mode: .plan, action: .eavesdropperStatus, timeout: 90
        )
        let reading = ServerEavesdropperReading(outcome: parsed)
        let status = try XCTUnwrap(reading.status, "\(reading)")
        let now = try rowDate("2026-11-08T01:22:00Z")

        for seat in status.seats {
            let row = ServerSeatRowPresentation(
                reading: reading.seat(seat.seat, now: now), status: status, readAt: now, loading: false, now: now
            )
            XCTAssertTrue(row.isCurrent, seat.seat)
            XCTAssertEqual(row.state, "Connected", seat.seat)
            XCTAssertNil(row.note, seat.seat)
        }

        let compiler = ServerSeatRowPresentation(
            reading: reading.seat("BF-COMPILER", now: now), status: status, readAt: now, loading: false, now: now
        )
        XCTAssertEqual(compiler.details, ["Running", "Wake: Inbox", "Last event 6:50 PM"])
        XCTAssertEqual(compiler.sessions, "None")
        XCTAssertEqual(compiler.quota, "Not known")

        let later = try rowDate("2026-11-08T01:40:00Z")
        let old = ServerSeatRowPresentation(
            reading: reading.seat("BF-COMPILER", now: later), status: status, readAt: now, loading: false, now: later
        )
        XCTAssertEqual(old.state, "Not current")
        XCTAssertEqual(old.details, ["Reported 18m ago"])
        XCTAssertTrue(old.note?.contains("That is old evidence") == true, old.note ?? "")
    }

    // MARK: The group's line

    /// The line under the server's heading says when Muster read the server and whether the
    /// answer is current;  the rows say the rest.
    func test_the_group_line_says_when_the_server_was_read() throws {
        let status = try rowDocument()
        let reading = ServerEavesdropperReading.reading(of: status)
        let readAt = try rowDate("2026-11-08T20:00:30Z")
        let now = try rowDate("2026-11-08T20:01:00Z")

        XCTAssertEqual(
            ServerSeatRowPresentation.groupLine(reading: nil, readAt: nil, loading: false, now: now),
            "Muster has not read the server's eavesdropper yet."
        )
        XCTAssertEqual(
            ServerSeatRowPresentation.groupLine(reading: nil, readAt: nil, loading: true, now: now),
            "Muster is reading the server's eavesdropper now."
        )
        XCTAssertEqual(
            ServerSeatRowPresentation.groupLine(reading: reading, readAt: readAt, loading: false, now: now),
            "Muster read the server's eavesdropper at 2:00 PM, and its answer is current."
        )
        XCTAssertEqual(
            ServerSeatRowPresentation.groupLine(reading: reading, readAt: readAt, loading: false, now: try rowDate("2026-11-08T20:10:00Z")),
            "Muster read the server's eavesdropper at 2:00 PM, but its answer is not current, so these rows show the last reading only as old evidence."
        )
        XCTAssertEqual(
            ServerSeatRowPresentation.groupLine(
                reading: .unavailable(ServerEavesdropperUnavailable(errorCode: "timeout")),
                readAt: readAt, loading: true, now: now
            ),
            "Muster read the server's eavesdropper at 2:00 PM, but no reading came back, so each row says why."
                + rowGap + "Muster is reading it again now."
        )
        // An answer this version cannot read did come back, so the heading says that, as the
        // rows do, and never "no reading came back".
        XCTAssertEqual(
            ServerSeatRowPresentation.groupLine(
                reading: .notThisAppsVersion("The answer is a later contract."),
                readAt: readAt, loading: false, now: now
            ),
            "Muster read the server's eavesdropper at 2:00 PM, and it answered in a form this version of Muster cannot read, so each row says to update Muster."
        )
    }

    /// The heading and the rows judge currency at the caller's one moment.
    func test_a_seat_reading_is_judged_at_the_callers_moment() throws {
        XCTAssertEqual(
            ServerSeatRowPresentation.seatReading("CLAUDE", reading: nil, now: Date()),
            .notRead("The server's eavesdropper has not been read yet.")
        )
        let reading = ServerEavesdropperReading.notThisAppsVersion("later contract")
        XCTAssertEqual(
            ServerSeatRowPresentation.seatReading("CLAUDE", reading: reading, now: Date()),
            reading.seat("CLAUDE", now: Date())
        )
    }

    /// The open panel reads the server well inside the answer's own window.
    func test_the_server_is_read_again_well_inside_its_window() {
        XCTAssertLessThan(TimeInterval(ServerSeatRowPresentation.serverRereadSeconds) * 2,
                          ReducedEavesdropperStatus.freshnessWindow)
    }

    /// A current global pause is said once, under the heading.
    func test_the_group_line_names_a_current_global_pause() throws {
        let paused = rowDocumentText.replacingOccurrences(
            of: #""pause":{"paused":false,"since":null,"wakes_only":null,"set_by":null}"#,
            with: #""pause":{"paused":true,"since":"2026-11-08T19:50:00Z","wakes_only":true,"set_by":"cli"}"#
        )
        let reading = ServerEavesdropperReading.reading(of: try rowDocument(paused))
        let line = ServerSeatRowPresentation.groupLine(
            reading: reading, readAt: try rowDate("2026-11-08T20:00:30Z"), loading: false, now: try rowDate("2026-11-08T20:01:00Z")
        )
        XCTAssertEqual(line, "Muster read the server's eavesdropper at 2:00 PM, and its answer is current."
            + rowGap + "The server's eavesdropper has every seat's wakes paused.")
    }
}
