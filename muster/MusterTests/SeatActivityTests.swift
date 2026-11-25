import XCTest

/// The Seat Activity panel:  what it reads,  how it fails,  and how many calls it makes.
///
/// Every test here runs against a fake fleet chat CLI and a fixed server eavesdropper reading
/// (the reduced_status v1 document).  Nothing here touches the network,  a Mac file,  Jay's
/// home directory or a real Zulip account.
final class SeatActivityTests: XCTestCase {
    // MARK: The fixtures

    /// The user list,  as `muster-service users` prints it.
    private let usersJSON = """
    {"user_id": 9001, "full_name": "Claude", "email": "claude-bot@example.test", "is_bot": true, "is_active": true, "role": 400, "seat": "CLAUDE"}
    {"user_id": 9002, "full_name": "OpenCode", "email": "opencode-bot@example.test", "is_bot": true, "is_active": true, "role": 400, "seat": "OPENCODE"}
    {"user_id": 9004, "full_name": "Muse Assist", "email": "ma-bot@example.test", "is_bot": true, "is_active": true, "role": 400, "seat": "MA"}
    """

    /// One message,  as `muster-service last-post` prints it.
    private let messageJSON = """
    {"id": 1001, "sender_id": 9001, "channel": "Work-In-Progress", "topic": "fleet tools doctor", "dm": false, "sender_email": "claude-bot@example.test", "sender_full_name": "Claude", "timestamp": 1792821027, "content": "[CLAUDE] fleet-tools-doctor on worker-01.example.test:  5 failing checks."}
    """

    /// The server eavesdropper's reduced status,  current at the tests' clock (05:51:07 UTC on
    /// Oct 24, 2026):  four seats it holds,  one it skipped and one its settings turn off.
    static let serverJSON = #"""
    {"schema": 1, "generated_at": "2026-10-24T05:50:37Z", "instance": "server",
     "status_updated": "2026-10-24T05:51:00Z",
     "seats": [{"seat": "CLAUDE", "bot": "Claude", "connected": true},
               {"seat": "MA", "bot": "Muse Assist", "connected": true},
               {"seat": "JET", "connected": true},
               {"seat": "GB-FIXER", "connected": false}],
     "skipped": [{"seat": "OPENCODE", "reason": "routine_missing"}],
     "disabled": ["CODEX"]}
    """#

    /// The clock every read here is judged against.
    static let clock: TimeInterval = 1792821067

    /// The fixture,  read as the store reads it.
    static func serverReading(_ text: String = serverJSON) throws -> ServerEavesdropperReading {
        let status = try JSONDecoder().decode(ReducedEavesdropperStatus.self, from: Data(text.utf8))
        return ServerEavesdropperReading.reading(of: status)
    }

    /// How many times a read asked the server.  It is this suite's own:  the store suite's
    /// `Counter` is nested there and is not in scope here.
    actor ServerReadCounter {
        private(set) var value = 0
        func bump() { value += 1 }
    }

    // MARK: The fake CLI

    /// A fake fleet chat CLI,  so a test never touches the network.
    final class FakeChatRunner: ChatRunning, @unchecked Sendable {
        typealias Handler = @Sendable (ChatInvocation) throws -> ChatCLIResult

        private let handler: Handler
        private let lock = NSLock()
        private var recorded: [ChatInvocation] = []

        init(handler: @escaping Handler) {
            self.handler = handler
        }

        /// The calls this runner was asked for,  in the order they arrived.
        var calls: [ChatInvocation] {
            lock.lock()
            defer { lock.unlock() }
            return recorded
        }

        func run(_ invocation: ChatInvocation) async throws -> ChatCLIResult {
            lock.lock()
            recorded.append(invocation)
            lock.unlock()
            return try handler(invocation)
        }
    }

    /// A runner that answers every verb with a fixture.
    private func healthyRunner(search: String? = nil) -> FakeChatRunner {
        FakeChatRunner { invocation in
            switch invocation.verb {
            case "users":
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON, stderr: "")
            case "last-post":
                return ChatCLIResult(exitCode: 0, stdout: search ?? self.messageJSON, stderr: "")
            default:
                return ChatCLIResult(exitCode: 2, stdout: "", stderr: "refused")
            }
        }
    }

    // MARK: The reading

    /// Read the panel over a roster,  with the server reading given,  or none at all.
    private func read(
        roster: [String] = ["CLAUDE"],
        runner: ChatRunning,
        server: ServerEavesdropperReading?? = nil,
        serverReads: ServerReadCounter? = nil,
        now: TimeInterval = SeatActivityTests.clock
    ) async throws -> SeatActivityModel {
        let reading: ServerEavesdropperReading? = try server ?? Self.serverReading()
        let reader = SeatActivityReader(
            runner: runner,
            now: { Date(timeIntervalSince1970: now) },
            serverReading: {
                await serverReads?.bump()
                return reading
            }
        )
        return await reader.read(roster: roster)
    }

    // MARK: The healthy seat

    /// A seat the server's current reading holds is placed with the server,  its Mac columns
    /// say they are not read here,  and nothing is a problem (AFC-837).
    func test_a_seat_the_server_holds_is_placed_by_the_servers_reading() async throws {
        let model = try await read(runner: healthyRunner())
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertEqual(row.seat, "CLAUDE")
        XCTAssertEqual(row.botName, "Claude")
        XCTAssertEqual(row.place, .server)
        for fact in [row.openSessions.notHereReason, row.listener.notHereReason, row.quota.notHereReason] {
            let reason = try XCTUnwrap(fact)
            XCTAssertTrue(reason.contains("server"), reason)
            XCTAssertTrue(reason.contains("not read here"), reason)
        }
        XCTAssertTrue(row.sessions.isEmpty)
        XCTAssertFalse(row.runningWake)
        XCTAssertFalse(row.hasUnknown)
        XCTAssertFalse(row.needsAttention)
        XCTAssertTrue(row.lastPost.isKnown)
        XCTAssertEqual(model.problems, [])
    }

    /// The last post carries the time and a label,  and never the body.
    func test_the_last_post_carries_a_time_and_a_label_and_never_the_body() async throws {
        let model = try await read(runner: healthyRunner())
        let row = try XCTUnwrap(model.rows.first)
        let post = try XCTUnwrap(row.lastPost.value)

        XCTAssertEqual(post.label, "fleet tools doctor")
        XCTAssertEqual(post.channel, "Work-In-Progress")
        XCTAssertEqual(post.at, Date(timeIntervalSince1970: 1792821027))
        XCTAssertFalse(post.label.contains("failing checks"))
    }

    /// A seat with no post at all reads as no post,  and not as a failure.
    func test_a_seat_with_no_post_reads_as_no_post() async throws {
        let model = try await read(
            runner: healthyRunner(search: "")
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.lastPost, .known(.none))
        XCTAssertFalse(row.postSaysAtQuota)
    }

    // MARK: Failing closed

    /// A CLI that fails leaves the column it answered unknown,  and never empty.
    func test_a_cli_failure_leaves_the_post_column_unknown_and_never_empty() async throws {
        let runner = FakeChatRunner { invocation in
            if invocation.verb == "users" {
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON, stderr: "")
            }
            throw ChatCLIError.commandNotFound("/nowhere/simple-chat")
        }
        let model = try await read(runner: runner)
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertNotNil(row.lastPost.reason)
        // Where the seat is held stays known:  the server's reading was read fine.
        XCTAssertEqual(row.place, .server)
    }

    /// A missing binary leaves the Zulip column unknown,  and the row says why.
    func test_a_missing_binary_leaves_the_zulip_column_unknown() async throws {
        let runner = FakeChatRunner { _ in
            throw ChatCLIError.commandNotFound("/nowhere/simple-chat")
        }
        let model = try await read(runner: runner)

        XCTAssertEqual(model.rows.count, 1)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertTrue(model.problems[0].contains("Operator read is unavailable"), model.problems[0])
        XCTAssertTrue(model.problems[0].contains("No agent seat is read instead"), model.problems[0])
    }

    /// A user list that fails leaves every seat's Zulip column unknown.
    func test_a_failed_user_list_leaves_every_seat_unknown() async throws {
        let runner = FakeChatRunner { _ in
            ChatCLIResult(exitCode: 3, stdout: "", stderr: "muster-service: refused (credential-unreadable)\n")
        }
        let model = try await read(
            roster: ["CLAUDE", "OPENCODE"],
            runner: runner
        )
        XCTAssertEqual(model.rows.count, 2)
        for row in model.rows {
            XCTAssertFalse(row.lastPost.isKnown)
        }
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertTrue(model.problems[0].contains("/zulip_operator"), model.problems[0])
    }

    // MARK: The Operator read (AFC-654)

    /// Missing pins say plainly that the Operator read is unavailable and why,  and no
    /// other identity is tried:  the one call made was the Operator service's.
    func test_missing_operator_pins_say_so_and_read_no_seat_instead() async throws {
        let runner = FakeChatRunner { _ in
            ChatCLIResult(exitCode: 3, stdout: "", stderr: "muster-service: refused (pins-missing)\n")
        }
        let model = try await read(roster: ["CLAUDE", "OPENCODE"], runner: runner)
        XCTAssertEqual(runner.calls.count, 1)
        XCTAssertEqual(runner.calls.first?.processArguments.first, "muster-service")
        let problem = try XCTUnwrap(model.problems.first)
        XCTAssertTrue(problem.contains("Operator read is unavailable"), problem)
        XCTAssertTrue(problem.contains("has not pinned the Operator"), problem)
        XCTAssertTrue(problem.contains("operator_email"), problem)
        XCTAssertTrue(problem.contains("No agent seat is read instead"), problem)
        XCTAssertEqual(model.readingSeat, "Operator")
    }

    /// Every refusal code the service prints has its own plain reason,  and anything else is
    /// not taken for one.
    func test_operator_refusal_codes_are_read_from_the_fixed_line_only() {
        XCTAssertEqual(OperatorReadRefusal.code(in: "muster-service: refused (pins-missing)\n"), "pins-missing")
        XCTAssertNil(OperatorReadRefusal.code(in: "usage: simple-chat [-h] ...\ninvalid choice: 'muster-service'"))
        XCTAssertNil(OperatorReadRefusal.code(in: "muster-service: refused (Bad Code!)"))
        for code in ["pins-missing", "pins-unreadable", "credential-unreadable", "realm-mismatch",
                     "identity-mismatch", "credential-rejected", "zulip-unreachable", "zulip-error",
                     "launched-or-remote", "bad-request"] {
            XCTAssertNotEqual(OperatorReadRefusal.reason(for: code), OperatorReadRefusal.reason(for: "failed"), code)
        }
        let old = OperatorReadRefusal.describe(ChatCLIResult(exitCode: 2, stdout: "", stderr: "invalid choice"))
        XCTAssertTrue(old.contains("may predate it"), old)
    }

    /// The child is always the Operator service:  no `--as`,  no seat,  no ordinary verb.
    func test_every_call_runs_the_operator_service_and_names_no_seat() throws {
        let users = ChatInvocation.users()
        XCTAssertEqual(users.processArguments, ["muster-service", "users", "--limit", "500"])
        let post = try ChatInvocation.lastPost(sender: "claude-bot@example.test")
        XCTAssertEqual(post.processArguments, ["muster-service", "last-post", "--sender", "claude-bot@example.test"])
        for invocation in [users, post] {
            XCTAssertFalse(invocation.processArguments.contains { $0.hasPrefix("--as") })
            XCTAssertFalse(invocation.processArguments.contains("CLAUDE"))
        }
    }

    /// A seat the realm knows no bot for has an unknown Zulip column.
    func test_a_seat_with_no_bot_has_an_unknown_zulip_column() async throws {
        let model = try await read(
            roster: ["GROK"],
            runner: healthyRunner()
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertTrue(row.lastPost.reason?.contains("knows no bot") == true)
    }

    /// A deactivated bot account is unknown,  and the row says that it is deactivated.
    func test_a_deactivated_bot_reads_as_unknown_and_says_why() async throws {
        let users = """
        {"user_id": 9001, "full_name": "Claude", "email": "claude-bot@example.test", "is_bot": true, "is_active": false, "role": 400, "seat": "CLAUDE"}
        """
        let runner = FakeChatRunner { invocation in
            switch invocation.verb {
            case "users":
                return ChatCLIResult(exitCode: 0, stdout: users, stderr: "")
            default:
                return ChatCLIResult(exitCode: 0, stdout: "", stderr: "")
            }
        }
        let model = try await read(runner: runner)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertTrue(row.lastPost.reason?.contains("deactivated") == true)
        XCTAssertEqual(model.requestsMade, 1)
    }

    // MARK: No seat list (DEGRADED-WALK-15)

    /// An empty roster asks the CLI nothing,  and says it has no seat list instead of
    /// showing an idle fleet.
    func test_an_empty_roster_makes_no_calls_and_says_there_is_no_seat_list() async throws {
        let runner = healthyRunner()
        let model = try await read(roster: [], runner: runner)
        XCTAssertEqual(model.rows.count, 0)
        XCTAssertEqual(model.requestsMade, 0)
        XCTAssertEqual(model.requestCeiling, 0)
        XCTAssertEqual(runner.calls.count, 0)
        XCTAssertEqual(model.rosterProblem, SeatActivityReader.noRosterReason)
        XCTAssertTrue(model.problems.contains(SeatActivityReader.noRosterReason))
        XCTAssertFalse(model.headline.contains("0 of"))
        XCTAssertTrue(model.headline.contains("no seat list"))
    }

    // MARK: Where each seat is held,  from the server (AFC-837)

    /// A seat the server skipped or turns off is still the server's:  its row shows what the
    /// server says about it,  never a Mac answer.
    func test_a_skipped_or_turned_off_seat_is_still_the_servers() async throws {
        let model = try await read(roster: ["OPENCODE", "CODEX"], runner: healthyRunner())
        XCTAssertEqual(model.rows.map(\.place), [.server, .server])
        XCTAssertEqual(model.problems, [])
    }

    /// With no server reading,  every place is not known and says why under the row,  the Mac
    /// columns are never filled in,  and the panel says once that the server was not read.
    func test_no_server_reading_leaves_every_place_not_known_and_never_a_mac_value() async throws {
        let model = try await read(roster: ["CLAUDE", "MA"], runner: healthyRunner(), server: .some(nil))
        for row in model.rows {
            guard case .notKnown(let why) = row.place else {
                return XCTFail("\(row.seat) must be not known,  got \(row.place)")
            }
            XCTAssertTrue(why.contains("was not read"), why)
            XCTAssertTrue(row.notes.contains { $0.column == "held by" && $0.isProblem })
            XCTAssertTrue(row.needsAttention)
            for fact in [row.openSessions.isKnown, row.listener.isKnown, row.quota.isKnown] {
                XCTAssertFalse(fact, row.seat)
            }
            XCTAssertNotNil(row.listener.notHereReason)
            XCTAssertTrue(row.lastPost.isKnown, "the last post is read whatever the server says")
        }
        XCTAssertEqual(model.groups.map(\.kind), [.notKnown])
        XCTAssertTrue(model.problems.contains { $0.contains("server's eavesdropper was not read") }, "\(model.problems)")
        XCTAssertTrue(model.otherServerSeats.isEmpty)
        XCTAssertTrue(model.headline.contains("Muster cannot say who holds 2 seats."), model.headline)
    }

    /// A refusal from the server's helper leaves every place not known,  with the helper's
    /// own reason,  and no Mac source is read instead.
    func test_a_server_refusal_leaves_places_not_known_with_its_reason() async throws {
        let why = "The server's control helper does not have the eavesdropper status read yet."
        let model = try await read(runner: healthyRunner(), server: .some(.notRead(why)))
        XCTAssertEqual(model.rows.first?.place, .notKnown(why))
        XCTAssertTrue(model.problems.contains { $0.contains(why) }, "\(model.problems)")

        let unavailable = try await read(runner: healthyRunner(), server: .some(.unavailable(.containerNotRunning)))
        XCTAssertEqual(unavailable.rows.first?.place, .notKnown(ServerEavesdropperUnavailable.containerNotRunning.cause))
    }

    /// A reading that is not current still places the seats it lists with the server,  whose
    /// rows then say the answer is old,  but never calls an unlisted seat unheld.
    func test_a_stale_reading_keeps_listed_seats_on_the_server_and_unlisted_ones_not_known() async throws {
        let model = try await read(
            roster: ["CLAUDE", "BF-BUILDER"],
            runner: healthyRunner(),
            now: SeatActivityTests.clock + 600
        )
        XCTAssertEqual(model.rows.first { $0.seat == "CLAUDE" }?.place, .server)
        let unlisted = try XCTUnwrap(model.rows.first { $0.seat == "BF-BUILDER" })
        guard case .notKnown(let why) = unlisted.place else {
            return XCTFail("an unlisted seat in a stale reading must be not known,  got \(unlisted.place)")
        }
        XCTAssertTrue(why.contains("not current"), why)
        XCTAssertTrue(model.problems.contains { $0.contains("not current") }, "\(model.problems)")
        XCTAssertTrue(model.otherServerSeats.isEmpty, "a stale reading names no other seat")
    }

    /// The rows sit in groups by where the server says each seat is held.
    func test_the_rows_are_grouped_by_where_the_server_holds_each_seat() async throws {
        let model = try await read(roster: ["MA", "CLAUDE", "BF-BUILDER"], runner: healthyRunner())
        XCTAssertEqual(model.groups.map(\.kind), [.server, .noEavesdropper])
        XCTAssertEqual(model.groups.first?.rows.map(\.seat), ["MA", "CLAUDE"])
    }

    /// The seats the server holds that the roster does not are named,  skipped and turned-off
    /// seats included,  so the server's half of the fleet is not silently missing.
    func test_server_seats_the_roster_does_not_hold_are_named() async throws {
        let model = try await read(roster: ["CLAUDE", "MA"], runner: healthyRunner())
        XCTAssertEqual(model.otherServerSeats, ["CODEX", "GB-FIXER", "JET", "OPENCODE"])
        let note = try XCTUnwrap(model.otherServerSeatsNote)
        XCTAssertTrue(note.contains("server's eavesdropper also holds 4 seats"), note)
        XCTAssertTrue(note.contains("CODEX, GB-FIXER, JET, OPENCODE"), note)
    }

    /// The headline counts the server's seats and never speaks for a Mac seat.
    func test_the_headline_counts_the_servers_seats() async throws {
        let model = try await read(roster: ["CLAUDE", "MA"], runner: healthyRunner())
        let shown = SeatActivityText.display(model.headline)
        XCTAssertTrue(shown.contains("6 seats are held by the server."), shown)
        XCTAssertFalse(shown.contains("held by the Mac"), shown)
        XCTAssertFalse(shown.contains("  "))
    }

    /// One read asks the server once,  and an empty roster asks it nothing.
    func test_a_read_asks_the_server_once_and_an_empty_roster_not_at_all() async throws {
        let reads = ServerReadCounter()
        _ = try await read(roster: ["CLAUDE", "MA"], runner: healthyRunner(), serverReads: reads)
        let afterOne = await reads.value
        XCTAssertEqual(afterOne, 1)
        _ = try await read(roster: [], runner: healthyRunner(), serverReads: reads)
        let afterEmpty = await reads.value
        XCTAssertEqual(afterEmpty, 1)
    }

    /// A user list that fails still leaves every seat placed by the server.
    func test_a_failed_user_list_still_places_every_seat() async throws {
        let runner = FakeChatRunner { _ in
            ChatCLIResult(exitCode: 3, stdout: "", stderr: "muster-service: refused (credential-unreadable)\n")
        }
        let model = try await read(roster: ["CLAUDE", "BF-BUILDER"], runner: runner)
        XCTAssertEqual(model.rows.map(\.place), [.server, .noEavesdropper])
    }

    // MARK: Where each seat is held (SEATS-4, HOSTAUDIT-08)

    /// A seat the partition gives to the server reads as held by the server,  and its
    /// eavesdropper columns say they are not read here:  never unknown,  never zero.
    func test_a_server_seat_reads_as_held_by_the_server_and_not_unknown() async throws {
        let model = try await read(
            roster: ["CLAUDE", "MA"],
            runner: healthyRunner()
        )
        let row = try XCTUnwrap(model.rows.first { $0.seat == "MA" })
        XCTAssertEqual(row.place, .server)
        XCTAssertNotNil(row.openSessions.notHereReason)
        XCTAssertNotNil(row.listener.notHereReason)
        XCTAssertNotNil(row.quota.notHereReason)
        XCTAssertNotEqual(row.openSessions, .known(0))
        XCTAssertFalse(row.hasUnknown)
        XCTAssertFalse(row.needsAttention)
        XCTAssertTrue(row.openSessions.notHereReason?.contains("server") == true)
        // Its last post is still read:  Zulip is the same from either machine.
        XCTAssertTrue(row.lastPost.isKnown)
    }

    /// A seat held by no eavesdropper says so,  and is not a fault.
    func test_a_seat_with_no_eavesdropper_reads_as_held_by_none() async throws {
        let model = try await read(roster: ["BF-BUILDER"], runner: healthyRunner())
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.place, .noEavesdropper)
        XCTAssertNotNil(row.openSessions.notHereReason)
        XCTAssertNotNil(row.listener.notHereReason)
        XCTAssertNotNil(row.quota.notHereReason)
        // Only the last post can be unknown here:  the fixture has no Zulip bot for this seat.
        let unknownColumns = row.unknowns.map(\.column)
        XCTAssertTrue(unknownColumns.contains("last post"))
        XCTAssertFalse(unknownColumns.contains("eavesdropper"))
        XCTAssertFalse(unknownColumns.contains("sessions"))
        XCTAssertFalse(unknownColumns.contains("quota"))
    }

    // MARK: The quota (SEATS-22)

    /// A zero that is not the provider's own verdict is not shown as an empty quota.
    func test_a_zero_that_is_not_exhausted_is_not_shown_as_empty() {
        XCTAssertEqual(SeatQuotaReading.available(remaining: 0, resetAt: nil).label, "Not exhausted")
        XCTAssertEqual(SeatQuotaReading.available(remaining: nil, resetAt: nil).label, "Not exhausted")
    }

    /// The reset time is the fleet's clock,  with the day when it is not today.
    func test_the_reset_text_uses_the_fleet_clock() throws {
        let now = try XCTUnwrap(FleetTime.parse("2026-11-05T18:00:00Z"))
        let sameDay = try XCTUnwrap(FleetTime.parse("2026-11-05T22:00:00Z"))
        let later = try XCTUnwrap(FleetTime.parse("2026-11-07T22:00:00Z"))
        XCTAssertEqual(SeatActivityText.resetText(sameDay, now: now), "resets 4:00 PM")
        XCTAssertEqual(SeatActivityText.resetText(later, now: now), "resets Nov 7, 4:00 PM")
    }

    /// A last post that says the seat is at quota is kept as what the post said.
    func test_a_last_post_that_says_quota_is_marked_as_said() async throws {
        let message = messageJSON.replacingOccurrences(
            of: "[CLAUDE] fleet-tools-doctor on worker-01.example.test:  5 failing checks.",
            with: "[CLAUDE] I am at quota for this week."
        )
        let model = try await read(
            runner: healthyRunner(search: message)
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertTrue(row.postSaysAtQuota)
        // The quota column never takes the post's words.
        XCTAssertFalse(row.quota.isKnown)
    }

    /// A last post that could not be read never says the seat is at quota.
    func test_an_unreadable_last_post_says_nothing_about_quota() async throws {
        let runner = FakeChatRunner { invocation in
            switch invocation.verb {
            case "users":
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON, stderr: "")
            default:
                return ChatCLIResult(exitCode: 5, stdout: "", stderr: "Zulip API error")
            }
        }
        let model = try await read(runner: runner)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertFalse(row.postSaysAtQuota)
    }

    /// The quota phrases are the ones the panel says they are.
    func test_the_quota_markers_are_the_narrow_ones() {
        XCTAssertTrue(SeatActivityQuota.reportsQuota("I am at quota for the week."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("the provider quota is exhausted"))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("I hit quota."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("the doctor reports five failing checks."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("quota is a setting in the panel."))
    }

    // MARK: The eavesdropper's own states

    /// A connected seat with no jobs reads as plain Connected.
    func test_a_connected_seat_with_no_jobs_reads_as_connected() {
        XCTAssertEqual(SeatListenerState.connected(jobs: 0).label, "Connected")
        XCTAssertEqual(SeatListenerState.connected(jobs: 1).label, "Connected, 1 job")
        XCTAssertEqual(SeatListenerState.connected(jobs: 3).label, "Connected, 3 jobs")
    }

    // MARK: The request ceiling

    /// The ceiling is one users call and one call per seat,  and nothing for no seats.
    func test_the_request_ceiling_is_one_users_call_and_one_per_seat() {
        XCTAssertEqual(SeatActivityReader.requestCeiling(forSeats: 0), 0)
        XCTAssertEqual(SeatActivityReader.requestCeiling(forSeats: 1), 2)
        XCTAssertEqual(SeatActivityReader.requestCeiling(forSeats: 15), 16)
    }

    /// A refresh stays inside its ceiling,  and never asks for presence (SEATS-6).
    func test_a_refresh_stays_inside_its_ceiling_and_never_asks_for_presence() async throws {
        let runner = healthyRunner()
        let model = try await read(
            roster: ["CLAUDE", "OPENCODE"],
            runner: runner
        )
        XCTAssertEqual(model.requestCeiling, 3)
        XCTAssertEqual(model.requestsMade, 3)
        XCTAssertLessThanOrEqual(model.requestsMade, model.requestCeiling)
        XCTAssertEqual(runner.calls.filter { $0.verb == "users" }.count, 1)
        XCTAssertEqual(runner.calls.filter { $0.verb == "last-post" }.count, 2)
        XCTAssertEqual(runner.calls.filter { $0.verb == "presence" }.count, 0)
    }

    /// A seat the realm knows no bot for costs no calls at all.
    func test_a_seat_with_no_bot_costs_no_calls() async throws {
        let runner = healthyRunner()
        let model = try await read(
            roster: ["GROK"],
            runner: runner
        )
        XCTAssertEqual(model.requestsMade, 1)
        XCTAssertEqual(runner.calls.count, 1)
    }

    /// A failed user list stops the refresh after one call.
    func test_a_failed_user_list_costs_one_call() async throws {
        let runner = FakeChatRunner { _ in
            ChatCLIResult(exitCode: 3, stdout: "", stderr: "no credential")
        }
        let model = try await read(roster: ["CLAUDE", "OPENCODE"], runner: runner)
        XCTAssertEqual(model.requestsMade, 1)
        XCTAssertEqual(runner.calls.count, 1)
    }

    // MARK: The read-only guard

    /// The runner refuses a verb that writes,  and every ordinary `simple-chat` verb,
    /// before any process exists.
    func test_the_runner_refuses_a_verb_that_writes() async {
        let cli = ChatCLI(
            location: { ChatLocation(command: URL(fileURLWithPath: "/nowhere/simple-chat")) },
            home: URL(fileURLWithPath: "/nowhere")
        )
        for verb in ["post", "search", "dm", "react"] {
            do {
                _ = try await cli.run(ChatInvocation([verb, "--help"]))
                XCTFail("\(verb) must be refused")
            } catch let error as ChatCLIError {
                XCTAssertEqual(error, .writeVerbRefused(verb))
            } catch {
                XCTFail("the refusal must be a ChatCLIError,  got \(error)")
            }
        }
    }

    /// The only subcommands the panel may run are the Operator service's two reads.
    func test_the_only_verbs_the_panel_may_run_are_read_only() {
        XCTAssertEqual(ChatInvocation.readOnlyVerbs, Set(["users", "last-post"]))
        XCTAssertEqual(ChatInvocation.serviceMode, "muster-service")
    }

    /// A CLI that is not on this Mac says so in a sentence that makes sense (COPY-53).
    func test_a_missing_cli_says_where_muster_looked() async {
        let cli = ChatCLI(location: { nil }, home: URL(fileURLWithPath: "/nowhere"))
        do {
            _ = try await cli.run(ChatInvocation.users())
            XCTFail("a missing command must be reported")
        } catch let error as ChatCLIError {
            XCTAssertEqual(error, .notInstalled)
            let text = error.localizedDescription
            XCTAssertTrue(text.contains("could not find simple-chat on this Mac"), text)
            XCTAssertFalse(text.contains("at the fleet chat command line"), text)
        } catch {
            XCTFail("the error must be a ChatCLIError,  got \(error)")
        }
    }

    /// The child environment carries no credential and no launcher.
    func test_the_child_environment_carries_no_credential_and_no_launcher() {
        let environment = ChatCLI.childEnvironment(home: URL(fileURLWithPath: "/tmp/home"))
        XCTAssertEqual(environment["HOME"], "/tmp/home")
        XCTAssertNil(environment["AGENT_LAUNCHER"])
        XCTAssertNil(environment["AGENT_LAUNCH_SEAT"])
        XCTAssertNil(environment["AGENT_SEAT"])
        XCTAssertNil(environment["ZULIP_RC"])
        for (key, value) in environment {
            XCTAssertFalse(
                key.uppercased().contains("KEY") || value.uppercased().contains("ZULIP_API_KEY"),
                "\(key) must not carry a credential"
            )
        }
    }

    // MARK: The labels

    /// A topic is one line,  free of control characters,  and short.
    func test_a_label_is_one_line_and_short() {
        let label = SeatActivityText.label("a topic\nwith a break\u{07} and a tail")
        XCTAssertFalse(label.contains("\n"))
        XCTAssertFalse(label.contains("\u{07}"))
        XCTAssertEqual(label, "a topic with a break and a tail")

        let long = String(repeating: "x", count: 120)
        XCTAssertEqual(SeatActivityText.label(long).count, SeatActivityText.maximumLabelLength + 1)
    }

    // MARK: Review fixes (Kody and Sentry on #926)

    /// The quota markers match whole words,  and a negation turns them off.
    func test_the_quota_markers_match_whole_words_and_respect_negation() {
        XCTAssertFalse(SeatActivityQuota.reportsQuota("I raised that quota question yesterday."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("I am not at quota anymore."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("We are no longer out of credits."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("At quota until Monday."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("Usage limit reached, back at 9."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("I am not yet at quota."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("Still not  quite at quota."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("Not today.  I am at quota now."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("No joke, I am at quota."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("No: at quota until Monday."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("I could not post, at quota until Monday."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("We are no longer at quota."))
        let long = String(repeating: "status line ok ", count: 20_000) + "at quota"
        XCTAssertTrue(SeatActivityQuota.reportsQuota(long))
    }

    /// A child that catches SIGTERM and exits with a status (the CLI exits 130) is still
    /// reported as a timeout.
    func test_a_child_that_exits_130_on_sigterm_is_reported_as_a_timeout() async throws {
        let script = FileManager.default.temporaryDirectory
            .appendingPathComponent("muster-term-chat-\(UUID().uuidString)")
        try "#!/bin/sh\ntrap 'exit 130' TERM\nwhile :; do sleep 0.2; done\n"
            .write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        addTeardownBlock { try? FileManager.default.removeItem(at: script) }
        let cli = ChatCLI(
            location: { ChatLocation(command: script) },
            home: FileManager.default.temporaryDirectory
        )
        do {
            _ = try await cli.run(ChatInvocation(["users"], timeout: 1))
            XCTFail("a child stopped by the timer must report the timeout")
        } catch let error as ChatCLIError {
            XCTAssertEqual(error, .timedOut(1))
        }
    }

    /// A child that finishes on its own before the deadline keeps its output.
    func test_a_child_that_finishes_in_time_keeps_its_output() async throws {
        let script = FileManager.default.temporaryDirectory
            .appendingPathComponent("muster-quick-chat-\(UUID().uuidString)")
        try "#!/bin/sh\necho '{\"ok\": true}'\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        addTeardownBlock { try? FileManager.default.removeItem(at: script) }
        let cli = ChatCLI(
            location: { ChatLocation(command: script) },
            home: FileManager.default.temporaryDirectory
        )
        let result = try await cli.run(ChatInvocation(["users"], timeout: 5))
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertFalse(result.timedOut)
        XCTAssertTrue(result.stdout.contains("\"ok\""))
    }

    /// A record that holds a Unicode line separator is still one record.
    func test_a_unicode_line_separator_inside_a_record_does_not_split_it() {
        let message = messageJSON.replacingOccurrences(
            of: "5 failing checks.",
            with: "5 failing\u{2028}checks\u{0085}here."
        )
        let fact = SeatActivityReader.decodeLastPost(message)
        XCTAssertEqual(fact.value?.label, "fleet tools doctor")
    }

    /// Output that is not a message is unknown,  and never "no post".
    func test_malformed_post_output_is_unknown_and_not_no_post() {
        XCTAssertFalse(SeatActivityReader.decodeLastPost("Traceback (most recent call last):").isKnown)
        XCTAssertFalse(SeatActivityReader.decodeLastPost("{\"id\": ").isKnown)
        XCTAssertFalse(SeatActivityReader.decodeLastPost(messageJSON + "\nnot json").isKnown)
        XCTAssertEqual(SeatActivityReader.decodeLastPost(""), .known(.none))
        XCTAssertEqual(SeatActivityReader.decodeLastPost("[]\n"), .known(.none))
    }

    /// A line too long to be a message is not decoded.
    func test_an_oversized_line_is_not_decoded() {
        let huge = "{\"topic\": \"" + String(repeating: "x", count: SeatActivityReader.maximumLineBytes) + "\"}"
        XCTAssertFalse(SeatActivityReader.decodeLastPost(huge).isKnown)
    }

    /// The newest post is the last line,  because the CLI prints oldest first.
    func test_the_newest_post_is_the_last_line() {
        let older = messageJSON.replacingOccurrences(of: "fleet tools doctor", with: "older topic")
        let fact = SeatActivityReader.decodeLastPost(older + "\n" + messageJSON)
        XCTAssertEqual(fact.value?.label, "fleet tools doctor")
    }

    /// A user list with a line that is not a user is a failed list,  not a shorter one.
    func test_a_malformed_user_list_is_a_problem_and_not_a_missing_bot() async throws {
        let runner = FakeChatRunner { invocation in
            if invocation.verb == "users" {
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON + "\nWarning: something", stderr: "")
            }
            return ChatCLIResult(exitCode: 0, stdout: "", stderr: "")
        }
        let model = try await read(runner: runner)
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertEqual(model.requestsMade, 1)
    }

    /// The user list asks for deactivated accounts,  and an active account wins.
    func test_the_user_list_includes_deactivated_accounts_and_prefers_the_active_one() {
        // The service always includes deactivated accounts:  there is no flag to forget.
        XCTAssertEqual(ChatInvocation.users().arguments, ["users", "--limit", "500"])
        let users = """
        {"user_id": 9001, "full_name": "Claude", "email": "claude-bot@example.test", "is_bot": true, "is_active": true, "seat": "CLAUDE"}
        {"user_id": 9003, "full_name": "Claude (old)", "email": "claude-old-bot@example.test", "is_bot": true, "is_active": false, "seat": "CLAUDE"}
        """
        let bots = try? XCTUnwrap(SeatActivityReader.decodeBots(users))
        XCTAssertEqual(bots?["CLAUDE"]?.userID, 9001)
    }

    /// A sender that looks like a flag is refused before a process exists.
    func test_a_sender_that_looks_like_a_flag_is_refused() {
        XCTAssertThrowsError(try ChatInvocation.lastPost(sender: "--zuliprc=/tmp/other")) { error in
            XCTAssertEqual(error as? ChatCLIError, .invalidArgument("--zuliprc=/tmp/other"))
        }
    }

    /// The child process gets exactly the service mode and the subcommand:  a fake CLI that
    /// prints its own arguments shows no `--as` and no seat.
    func test_the_child_is_given_the_service_mode_and_no_seat() async throws {
        let script = FileManager.default.temporaryDirectory
            .appendingPathComponent("muster-args-chat-\(UUID().uuidString)")
        try "#!/bin/sh\nfor a in \"$@\"; do echo \"$a\"; done\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        addTeardownBlock { try? FileManager.default.removeItem(at: script) }
        let cli = ChatCLI(
            location: { ChatLocation(command: script) },
            home: FileManager.default.temporaryDirectory
        )
        let result = try await cli.run(ChatInvocation.users(limit: 7))
        XCTAssertEqual(result.stdout.split(separator: "\n").map(String.init), ["muster-service", "users", "--limit", "7"])
    }

    /// A child that ignores SIGTERM is killed,  and the call reports the timeout.
    func test_a_child_that_ignores_sigterm_is_killed_and_times_out() async throws {
        let script = FileManager.default.temporaryDirectory
            .appendingPathComponent("muster-stuck-chat-\(UUID().uuidString)")
        try "#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n"
            .write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        addTeardownBlock { try? FileManager.default.removeItem(at: script) }
        let cli = ChatCLI(
            location: { ChatLocation(command: script) },
            home: FileManager.default.temporaryDirectory
        )
        let started = Date()
        do {
            _ = try await cli.run(ChatInvocation(["users"], timeout: 1))
            XCTFail("a stuck child must time out")
        } catch let error as ChatCLIError {
            XCTAssertEqual(error, .timedOut(1))
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 1 + ChatInvocation.terminateGrace + 5)
    }

    /// On-screen text uses the sentence gap,  and one space after a comma or colon.
    func test_display_text_uses_the_sentence_gap_and_single_comma_spaces() {
        let shown = SeatActivityText.display("It failed,  so it is unknown.  Try again:  later.")
        XCTAssertEqual(shown, "It failed, so it is unknown.\u{00A0} Try again: later.")
        XCTAssertFalse(shown.contains("  "))
    }

    /// Every group's explanation reads with the sentence gap on screen.
    func test_group_explanations_use_the_sentence_gap() {
        for kind in SeatPlaceGroup.allCases {
            let shown = SeatActivityText.display(kind.explanation)
            XCTAssertFalse(shown.contains("  "), kind.rawValue)
        }
        XCTAssertTrue(SeatActivityText.display(SeatPlaceGroup.server.explanation).contains("seats.\u{00A0} Muster"))
    }

    func test_row_sorts_sessions_live_first_groups_by_platform_and_filters_by_family_and_search() {
        let deadClaude = SeatSession(id: "s1", letter: "AA", title: "Old", platform: "claude", alive: false)
        let liveCodex = SeatSession(id: "s2", letter: "BB", title: "ActiveWork", platform: "codex", alive: true)
        let liveClaude = SeatSession(id: "s3", letter: "CC", title: "LiveClaude", platform: "claude-code", alive: true)

        let ccRow = SeatActivityRow(
            seat: "CLAUDE",
            botName: "Claude Bot",
            place: .mac,
            openSessions: .known(2),
            sessions: [deadClaude, liveCodex, liveClaude],
            listener: .known(.connected(jobs: 0)),
            lastPost: .known(.none)
        )
        let gbRow = SeatActivityRow(
            seat: "GB-ALPHA",
            place: .server,
            openSessions: .known(0),
            listener: .known(.skipped("paused")),
            lastPost: .known(.none)
        )
        let bfRow = SeatActivityRow(
            seat: "BF-ONE",
            place: .noEavesdropper,
            openSessions: .known(0),
            listener: .known(.connected(jobs: 0)),
            lastPost: .known(.post(at: Date(timeIntervalSince1970: 100), channel: "Work-In-Progress", label: "#Work-In-Progress > demo", isAtQuota: false))
        )

        XCTAssertEqual(ccRow.sortedSessions.map(\.id), ["s2", "s3", "s1"])
        XCTAssertEqual(ccRow.sessionsByPlatform.map(\.platform), ["Codex", "Claude"])
        XCTAssertEqual(ccRow.sessionsByPlatform.last?.sessions.map(\.id), ["s3", "s1"])

        var model = SeatActivityModel()
        model.rows = [ccRow, gbRow, bfRow]

        let workingGroups = model.filteredGroups(filter: .working, search: "")
        XCTAssertEqual(workingGroups.flatMap(\.rows).map(\.seat), ["CLAUDE"])

        let searchedGroups = model.filteredGroups(filter: .all, search: "Work-In-Progress")
        XCTAssertEqual(searchedGroups.flatMap(\.rows).map(\.seat), ["BF-ONE"])

        let sections = model.familySections(filter: .all, search: "")
        XCTAssertEqual(sections.map(\.kind), [.core, .grokBots, .botFleet])
        XCTAssertEqual(sections[0].rows.map(\.seat), ["CLAUDE"])
        XCTAssertEqual(sections[1].rows.map(\.seat), ["GB-ALPHA"])
        XCTAssertEqual(sections[2].rows.map(\.seat), ["BF-ONE"])
    }
}

/// The app-level store:  one reading,  one read in flight,  and no reading in the
/// background (SEATS-31, PERF-7).
@MainActor
final class SeatActivityStoreTests: XCTestCase {
    /// A count the fake reader keeps across tasks.
    actor Counter {
        private(set) var value = 0
        func bump() { value += 1 }
    }

    /// Two refreshes at once start one read,  and both see its answer.
    func test_two_refreshes_at_once_start_one_read() async {
        let counter = Counter()
        let store = SeatActivityStore(reader: { _ in
            await counter.bump()
            try? await Task.sleep(nanoseconds: 200_000_000)
            return SeatActivityModel(readAt: Date(timeIntervalSince1970: 1000))
        })
        async let first: Void = store.refresh(roster: ["CLAUDE"])
        async let second: Void = store.refresh(roster: ["CLAUDE"])
        _ = await (first, second)
        let reads = await counter.value
        XCTAssertEqual(reads, 1)
        XCTAssertEqual(store.readsStarted, 1)
        XCTAssertEqual(store.model?.readAt, Date(timeIntervalSince1970: 1000))
        XCTAssertFalse(store.loading)
    }

    /// A return inside the window shows the last reading and reads nothing.
    func test_a_fresh_reading_is_not_read_again() async {
        var clock: TimeInterval = 1030
        let store = SeatActivityStore(
            reader: { _ in SeatActivityModel(readAt: Date(timeIntervalSince1970: 1000)) },
            now: { Date(timeIntervalSince1970: clock) }
        )
        await store.refresh(roster: ["CLAUDE"])
        await store.refreshIfOlder(than: 60, roster: ["CLAUDE"])
        XCTAssertEqual(store.readsStarted, 1)
        clock = 1100
        await store.refreshIfOlder(than: 60, roster: ["CLAUDE"])
        XCTAssertEqual(store.readsStarted, 2)
    }

    /// A read that never ends on its own,  until it is stopped:  it counts its start and
    /// whether it saw the stop.
    private func stoppableStore(started: Counter, stopped: Counter) -> SeatActivityStore {
        SeatActivityStore(reader: { _ in
            await started.bump()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000)
            }
            await stopped.bump()
            return SeatActivityModel(readAt: Date(timeIntervalSince1970: 2000), problems: ["partial"])
        })
    }

    /// Leaving the panel stops the read in flight:  the stop reaches the reader,  and the
    /// partial answer is dropped (Sentry on #1378).
    func test_stopping_a_read_reaches_the_reader_and_drops_its_answer() async {
        let started = Counter()
        let stopped = Counter()
        let store = stoppableStore(started: started, stopped: stopped)
        async let read: Void = store.refresh(roster: ["CLAUDE"])
        while await started.value == 0 {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        store.stopReading()
        await read
        let sawStop = await stopped.value
        XCTAssertEqual(sawStop, 1)
        XCTAssertNil(store.model)
        XCTAssertFalse(store.loading)
    }

    /// Going to the background stops the read in flight too.
    func test_going_to_the_background_stops_the_read() async {
        let started = Counter()
        let stopped = Counter()
        let store = stoppableStore(started: started, stopped: stopped)
        async let read: Void = store.refresh(roster: ["CLAUDE"])
        while await started.value == 0 {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        store.setActive(false)
        await read
        let sawStop = await stopped.value
        XCTAssertEqual(sawStop, 1)
        XCTAssertNil(store.model)
    }

    /// Returning to the panel while the app is in the background reads nothing either.
    func test_returning_to_the_panel_in_the_background_reads_nothing() async {
        let store = SeatActivityStore(reader: { _ in SeatActivityModel(readAt: Date(timeIntervalSince1970: 0)) })
        store.setActive(false)
        await store.refreshIfOlder(than: 60, roster: ["CLAUDE"])
        XCTAssertEqual(store.readsStarted, 0)
        XCTAssertNil(store.model)
    }

    /// The timer reads nothing while the app is not in front.
    func test_the_timer_reads_nothing_in_the_background() async {
        let store = SeatActivityStore(reader: { _ in SeatActivityModel(readAt: Date(timeIntervalSince1970: 0)) })
        store.setActive(false)
        await store.timerTick(roster: ["CLAUDE"], minimumAge: 60)
        XCTAssertEqual(store.readsStarted, 0)
        XCTAssertNil(store.model)
        store.setActive(true)
        await store.timerTick(roster: ["CLAUDE"], minimumAge: 60)
        XCTAssertEqual(store.readsStarted, 1)
    }
}
