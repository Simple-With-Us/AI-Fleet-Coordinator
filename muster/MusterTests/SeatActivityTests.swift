import XCTest

/// The Seat Activity panel:  what it reads,  how it fails,  and how many calls it makes.
///
/// Every test here runs against a fake fleet chat CLI,  an eavesdropper state file and a seat
/// partition in a temporary directory.  Nothing here touches the network,  Jay's home
/// directory or a real Zulip account.
final class SeatActivityTests: XCTestCase {
    // MARK: The fixtures

    /// The eavesdropper's own published state,  with one seat that has one live lease and
    /// one dead one.
    private let listenerJSON = """
    {
      "pid": 22135,
      "version": "3.2.1",
      "updated": 1792821067.0,
      "disabled": [],
      "skipped_seats": {},
      "pause": {},
      "seats": {
        "CLAUDE": {
          "bot": "Claude",
          "connected": true,
          "paused": false,
          "jobs": 1,
          "leases": [
            {
              "lease_id": "claude-code-11075-1792560932",
              "platform": "claude-code",
              "alive": true,
              "why": "ok",
              "topics": 2,
              "watcher": true,
              "pending": 0
            },
            {
              "lease_id": "claude-code-2269-1791503138",
              "platform": "claude-code",
              "alive": false,
              "why": "pid gone",
              "topics": 0,
              "watcher": false,
              "pending": 0
            }
          ]
        }
      }
    }
    """

    /// The seat partition,  in the shape of `docs/protocols/agent-sync-partition.toml`.
    private let partitionTOML = """
    version = 1

    [seats]
    CLAUDE = "mac"
    CODEX = "mac"
    OPENCODE = "mac"
    MA = "server"
    JET = "server"
    GB-FIXER = "server"
    BF-BUILDER = "none"
    """

    /// The user list,  as `users --json` prints it.
    private let usersJSON = """
    {"user_id": 9001, "full_name": "Claude", "email": "claude-bot@example.test", "is_bot": true, "is_active": true, "role": 400, "seat": "CLAUDE"}
    {"user_id": 9002, "full_name": "OpenCode", "email": "opencode-bot@example.test", "is_bot": true, "is_active": true, "role": 400, "seat": "OPENCODE"}
    {"user_id": 9004, "full_name": "Muse Assist", "email": "ma-bot@example.test", "is_bot": true, "is_active": true, "role": 400, "seat": "MA"}
    """

    /// One message,  as `search --json` prints it.
    private let messageJSON = """
    {"id": 1001, "sender_id": 9001, "client": "agent-sync", "channel": "Work-In-Progress", "topic": "fleet tools doctor", "dm": false, "sender_email": "claude-bot@example.test", "sender_full_name": "Claude", "is_bot": true, "seat": "CLAUDE", "seat_app": "claude-code", "timestamp": 1792821027, "content": "[CLAUDE] fleet-tools-doctor on worker-01.example.test:  5 failing checks.", "flags": [], "sibling": false, "mentioned": false, "reactions": []}
    """

    /// The listener fixture with one more field on the CLAUDE seat.
    private func listener(claudeField field: String) -> String {
        listenerJSON.replacingOccurrences(
            of: "\"jobs\": 1,",
            with: "\"jobs\": 1,\n          \(field),"
        )
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
            case "search":
                return ChatCLIResult(exitCode: 0, stdout: search ?? self.messageJSON, stderr: "")
            default:
                return ChatCLIResult(exitCode: 2, stdout: "", stderr: "refused")
            }
        }
    }

    // MARK: The reading

    /// A temporary directory that goes away after the test.
    private func scratchDirectory(fileManager: FileManager = .default) throws -> URL {
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("muster-seat-activity-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock {
            try? fileManager.removeItem(at: root)
        }
        return root
    }

    /// An eavesdropper state location in a temporary directory.
    private func listenerLocation(
        state: String?,
        config: String? = nil,
        fileManager: FileManager = .default
    ) throws -> ListenerStateLocation {
        let root = try scratchDirectory(fileManager: fileManager)
        var location = ListenerStateLocation(home: root)
        location.root = root.appendingPathComponent(".agent-sync", isDirectory: true)
        try fileManager.createDirectory(at: location.root, withIntermediateDirectories: true)
        if let state {
            let listener = location.root.appendingPathComponent("listener", isDirectory: true)
            try fileManager.createDirectory(at: listener, withIntermediateDirectories: true)
            try state.write(
                to: listener.appendingPathComponent("status.json"),
                atomically: true,
                encoding: .utf8
            )
        }
        if let config {
            try config.write(to: location.configURL, atomically: true, encoding: .utf8)
        }
        return location
    }

    /// A partition file in a temporary directory,  or a path to nothing.
    private func partitionFile(_ text: String?) throws -> URL {
        let root = try scratchDirectory()
        let url = root.appendingPathComponent("agent-sync-partition.toml")
        if let text {
            try text.write(to: url, atomically: true, encoding: .utf8)
        }
        return url
    }

    /// Read the panel over a roster.
    private func read(
        roster: [String] = ["CLAUDE"],
        runner: ChatRunning,
        listener: String? = nil,
        partition: String? = nil,
        config: String? = nil,
        now: TimeInterval = 1792821067
    ) async throws -> SeatActivityModel {
        let location = try listenerLocation(state: listener, config: config)
        let reader = SeatActivityReader(
            location: location,
            runner: runner,
            now: { Date(timeIntervalSince1970: now) },
            partitionURL: try partitionFile(partition ?? partitionTOML)
        )
        return await reader.read(roster: roster)
    }

    // MARK: The healthy seat

    /// A seat everything answered for reads as known in every column.
    func test_a_healthy_seat_reads_every_column_as_known() async throws {
        let model = try await read(runner: healthyRunner(), listener: listenerJSON)
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertEqual(row.seat, "CLAUDE")
        XCTAssertEqual(row.botName, "Claude")
        XCTAssertEqual(row.place, .mac)
        XCTAssertEqual(row.openSessions, .known(1))
        XCTAssertEqual(row.listener, .known(.connected(jobs: 1)))
        XCTAssertEqual(row.quota, .known(.notReported))
        XCTAssertFalse(row.postSaysAtQuota)
        XCTAssertFalse(row.hasUnknown)
        XCTAssertEqual(model.problems, [])
    }

    /// The last post carries the time and a label,  and never the body.
    func test_the_last_post_carries_a_time_and_a_label_and_never_the_body() async throws {
        let model = try await read(runner: healthyRunner(), listener: listenerJSON)
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
            runner: healthyRunner(search: ""),
            listener: listenerJSON
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
        let model = try await read(runner: runner, listener: listenerJSON)
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertNotNil(row.lastPost.reason)
        // The known part stays known:  the eavesdropper's own file was read fine.
        XCTAssertEqual(row.openSessions, .known(1))
    }

    /// A missing binary leaves the Zulip column unknown,  and the row says why.
    func test_a_missing_binary_leaves_the_zulip_column_unknown() async throws {
        let runner = FakeChatRunner { _ in
            throw ChatCLIError.commandNotFound("/nowhere/simple-chat")
        }
        let model = try await read(runner: runner, listener: listenerJSON)

        XCTAssertEqual(model.rows.count, 1)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertTrue(model.problems[0].contains("could not be read"))
    }

    /// A user list that fails leaves every seat's Zulip column unknown.
    func test_a_failed_user_list_leaves_every_seat_unknown() async throws {
        let runner = FakeChatRunner { _ in
            ChatCLIResult(exitCode: 3, stdout: "", stderr: "no credential")
        }
        let model = try await read(
            roster: ["CLAUDE", "OPENCODE"],
            runner: runner,
            listener: listenerJSON
        )
        XCTAssertEqual(model.rows.count, 2)
        for row in model.rows {
            XCTAssertFalse(row.lastPost.isKnown)
        }
        XCTAssertEqual(model.problems.count, 1)
    }

    /// An eavesdropper state file that cannot be read leaves the eavesdropper columns unknown.
    func test_an_unreadable_listener_state_leaves_the_listener_columns_unknown() async throws {
        let model = try await read(runner: healthyRunner(), listener: nil)
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertFalse(row.listener.isKnown)
        XCTAssertFalse(row.quota.isKnown)
        XCTAssertNotEqual(row.openSessions, .known(0))
        XCTAssertNotNil(row.openSessions.reason)
        XCTAssertEqual(model.problems.count, 1)
    }

    /// A seat the Mac holds that its eavesdropper publishes nothing about has unknown
    /// sessions,  and not zero.
    func test_a_mac_seat_the_listener_does_not_publish_has_unknown_sessions_and_not_zero() async throws {
        let model = try await read(
            roster: ["OPENCODE"],
            runner: healthyRunner(),
            listener: listenerJSON
        )
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertEqual(row.seat, "OPENCODE")
        XCTAssertEqual(row.place, .mac)
        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertNotNil(row.openSessions.reason)
        XCTAssertNotEqual(row.openSessions, .known(0))
        XCTAssertFalse(row.listener.isKnown)
        XCTAssertTrue(row.needsAttention)
    }

    /// A seat the realm knows no bot for has an unknown Zulip column.
    func test_a_seat_with_no_bot_has_an_unknown_zulip_column() async throws {
        let model = try await read(
            roster: ["GROK"],
            runner: healthyRunner(),
            listener: listenerJSON
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
        let model = try await read(runner: runner, listener: listenerJSON)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertTrue(row.lastPost.reason?.contains("deactivated") == true)
        XCTAssertEqual(model.requestsMade, 1)
    }

    /// The row says which parts are unknown,  so a reader knows what not to believe.
    func test_the_row_names_the_parts_that_are_unknown() async throws {
        let model = try await read(runner: healthyRunner(), listener: nil)
        let row = try XCTUnwrap(model.rows.first)
        let columns = row.unknowns.map { $0.column }
        XCTAssertTrue(columns.contains("sessions"))
        XCTAssertTrue(columns.contains("eavesdropper"))
        XCTAssertTrue(columns.contains("quota"))
        XCTAssertFalse(columns.contains("last post"))
    }

    // MARK: No seat list (DEGRADED-WALK-15)

    /// An empty roster asks the CLI nothing,  and says it has no seat list instead of
    /// showing an idle fleet.
    func test_an_empty_roster_makes_no_calls_and_says_there_is_no_seat_list() async throws {
        let runner = healthyRunner()
        let model = try await read(roster: [], runner: runner, listener: listenerJSON)
        XCTAssertEqual(model.rows.count, 0)
        XCTAssertEqual(model.requestsMade, 0)
        XCTAssertEqual(model.requestCeiling, 0)
        XCTAssertEqual(runner.calls.count, 0)
        XCTAssertEqual(model.rosterProblem, SeatActivityReader.noRosterReason)
        XCTAssertTrue(model.problems.contains(SeatActivityReader.noRosterReason))
        XCTAssertFalse(model.headline.contains("0 of"))
        XCTAssertTrue(model.headline.contains("no seat list"))
    }

    // MARK: Where each seat is held (SEATS-4, HOSTAUDIT-08)

    /// A seat the partition gives to the server reads as held by the server,  and its
    /// eavesdropper columns say they are not read here:  never unknown,  never zero.
    func test_a_server_seat_reads_as_held_by_the_server_and_not_unknown() async throws {
        let model = try await read(
            roster: ["CLAUDE", "MA"],
            runner: healthyRunner(),
            listener: listenerJSON
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

    /// The Mac's fleet-wide pause does not reach a seat the server holds.
    func test_a_mac_pause_does_not_mark_a_server_seat_paused() async throws {
        let paused = listenerJSON.replacingOccurrences(
            of: "\"pause\": {}",
            with: "\"pause\": {\"*\": {\"by\": \"owner\"}}"
        )
        let model = try await read(roster: ["MA"], runner: healthyRunner(), listener: paused)
        XCTAssertNotNil(model.rows.first?.listener.notHereReason)
    }

    /// A seat held by no eavesdropper says so,  and is not a fault.
    func test_a_seat_with_no_eavesdropper_reads_as_held_by_none() async throws {
        let model = try await read(roster: ["BF-BUILDER"], runner: healthyRunner(), listener: listenerJSON)
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

    /// The rows sit in groups by where each seat is held,  Mac first.
    func test_the_rows_are_grouped_by_where_each_seat_is_held() async throws {
        let model = try await read(
            roster: ["MA", "CLAUDE", "BF-BUILDER"],
            runner: healthyRunner(),
            listener: listenerJSON
        )
        XCTAssertEqual(model.groups.map(\.kind), [.mac, .server, .noEavesdropper])
        XCTAssertEqual(model.groups.first?.rows.map(\.seat), ["CLAUDE"])
    }

    /// Inside a group,  the rows that need a look come first.
    func test_rows_that_need_a_look_come_first_in_their_group() async throws {
        let model = try await read(
            roster: ["CLAUDE", "OPENCODE"],
            runner: healthyRunner(),
            listener: listenerJSON
        )
        XCTAssertEqual(model.groups.first?.rows.map(\.seat), ["OPENCODE", "CLAUDE"])
    }

    /// The server seats the roster does not hold are named,  so the server's half of the
    /// fleet is not silently missing.
    func test_server_seats_the_roster_does_not_hold_are_named() async throws {
        let model = try await read(roster: ["CLAUDE", "MA"], runner: healthyRunner(), listener: listenerJSON)
        XCTAssertEqual(model.otherServerSeats, ["GB-FIXER", "JET"])
        let note = try XCTUnwrap(model.otherServerSeatsNote)
        XCTAssertTrue(note.contains("GB-FIXER, JET"))
        XCTAssertTrue(note.contains("2 seats"))
    }

    /// An unreadable partition makes every seat's place not known,  and says why.
    func test_an_unreadable_partition_leaves_every_place_not_known() async throws {
        let model = try await read(
            roster: ["CLAUDE"],
            runner: healthyRunner(),
            listener: listenerJSON,
            partition: "this is not a partition"
        )
        let row = try XCTUnwrap(model.rows.first)
        if case .notKnown = row.place {} else { XCTFail("the place must be not known, got \(row.place)") }
        XCTAssertTrue(model.problems.contains { $0.contains("seat partition could not be read") })
        // The Mac's own state is still read for the seat.
        XCTAssertEqual(row.openSessions, .known(1))
        XCTAssertTrue(row.notes.contains { $0.column == "held by" && $0.isProblem })
    }

    /// The partition the Mac's eavesdropper names wins over Muster's own copy.
    func test_the_partition_the_eavesdropper_names_wins() async throws {
        let named = try partitionFile("version = 1\n\n[seats]\nCLAUDE = \"mac\"\nCODEX = \"server\"\n")
        let state = listenerJSON.replacingOccurrences(
            of: "\"pid\": 22135,",
            with: "\"pid\": 22135,\n  \"partition_file\": \"\(named.path)\","
        )
        let model = try await read(roster: ["CODEX"], runner: healthyRunner(), listener: state)
        XCTAssertEqual(model.rows.first?.place, .server)
        XCTAssertEqual(model.partitionPath, named.path)
    }

    /// A seat section in the Mac's file that names the server is refused there,  and the
    /// row says so.
    func test_a_seat_section_that_names_the_server_is_flagged() async throws {
        let model = try await read(
            roster: ["CODEX"],
            runner: healthyRunner(),
            listener: listenerJSON,
            config: "[seat.CODEX]\ninstance = \"server\"\nenabled = true\n"
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.place, .markedForTheServer)
        XCTAssertTrue(row.needsAttention)
        XCTAssertEqual(model.groups.first?.kind, .notKnown)
    }

    /// A state file another instance wrote is not taken as the Mac's.
    func test_a_state_file_from_another_instance_is_not_believed() async throws {
        let state = listenerJSON.replacingOccurrences(
            of: "\"pid\": 22135,",
            with: "\"pid\": 22135,\n  \"instance\": \"server\","
        )
        let model = try await read(runner: healthyRunner(), listener: state)
        XCTAssertFalse(model.rows.first?.openSessions.isKnown ?? true)
        XCTAssertTrue(model.problems.first?.contains("server instance") == true)
    }

    // MARK: The headline (SEATS-3, SEATS-35)

    /// The headline says how many Mac seats are connected and working,  scoped to the Mac,
    /// and how many the server holds.
    func test_the_headline_counts_mac_seats_and_names_the_server() async throws {
        let model = try await read(
            roster: ["CLAUDE", "MA"],
            runner: healthyRunner(),
            listener: listenerJSON
        )
        let shown = SeatActivityText.display(model.headline)
        XCTAssertTrue(shown.contains("1 of 1 seat held by the Mac is connected, and 1 is working on the Mac."), shown)
        XCTAssertTrue(shown.contains("3 seats are held by the server, which Muster cannot read yet."), shown)
        XCTAssertTrue(shown.contains(".\u{00A0} "), shown)
        XCTAssertFalse(shown.contains("  "))
    }

    /// The headline never counts a Mac seat whose state could not be read as connected.
    func test_the_headline_says_when_the_mac_state_could_not_be_read() async throws {
        let model = try await read(roster: ["CLAUDE", "CODEX"], runner: healthyRunner(), listener: nil)
        XCTAssertTrue(model.headline.contains("is not known"), model.headline)
        XCTAssertFalse(model.headline.contains("0 of"))
    }

    /// With one Mac seat unread,  the count says only what is known:  never a flat "of 2 are connected".
    func test_the_headline_says_known_to_be_connected_when_a_seat_is_unread() async throws {
        let model = try await read(roster: ["CLAUDE", "CODEX"], runner: healthyRunner(), listener: listenerJSON)
        XCTAssertTrue(
            model.headline.contains("1 of 2 seats held by the Mac is known to be connected"),
            model.headline
        )
        XCTAssertTrue(model.headline.contains("The state of 1 of them could not be read."), model.headline)
    }

    /// The fixture with a second seat the eavesdropper publishes without any lease list.
    private var listenerWithSeatOfUnknownSessions: String {
        listenerJSON.replacingOccurrences(
            of: "\"seats\": {",
            with: "\"seats\": {\n    \"CODEX\": {\"bot\": \"Codex\", \"connected\": true, \"paused\": false, \"jobs\": 0},"
        )
    }

    /// With one seat working and another whose sessions could not be read,  the working
    /// count is a floor,  and the headline says how many seats could not be counted.
    func test_the_headline_says_at_least_when_a_seats_sessions_are_unknown() async throws {
        let state = listenerWithSeatOfUnknownSessions
        XCTAssertNotEqual(state, listenerJSON, "the fixture edit must land")
        let model = try await read(roster: ["CLAUDE", "CODEX"], runner: healthyRunner(), listener: state)
        let codex = try XCTUnwrap(model.rows.first { $0.seat == "CODEX" })
        XCTAssertTrue(codex.listener.isKnown)
        XCTAssertFalse(codex.openSessions.isKnown)
        XCTAssertTrue(model.headline.contains("at least 1 is working on the Mac."), model.headline)
        XCTAssertFalse(model.headline.contains("and 1 is working"), model.headline)
        XCTAssertTrue(model.headline.contains("The sessions of 1 of them could not be read."), model.headline)
    }

    /// With no seat known to be working and a seat whose sessions are unknown,  the headline
    /// does not say that none is working.
    func test_the_headline_does_not_say_zero_working_when_sessions_are_unknown() async throws {
        let model = try await read(
            roster: ["CODEX"],
            runner: healthyRunner(),
            listener: listenerWithSeatOfUnknownSessions
        )
        XCTAssertTrue(model.headline.contains("none is known to be working on the Mac."), model.headline)
        XCTAssertFalse(model.headline.contains("0 are working"), model.headline)
        XCTAssertTrue(model.headline.contains("The sessions of 1 of them could not be read."), model.headline)
    }

    /// A lease whose "alive" is missing or of the wrong type is not a dead lease:  with no
    /// lease known to be alive,  the seat's sessions are unknown and not zero.
    func test_leases_that_do_not_say_alive_make_the_sessions_unknown_not_zero() async throws {
        let state = listenerJSON
            .replacingOccurrences(of: "\"alive\": true,", with: "\"alive\": \"yes\",")
            .replacingOccurrences(of: "\"alive\": false,", with: "")
        XCTAssertNotEqual(state, listenerJSON, "the fixture edit must land")
        let model = try await read(runner: healthyRunner(), listener: state)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.sessions.count, 2)
        XCTAssertTrue(row.sessions.allSatisfy { $0.alive == nil })
        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertNotEqual(row.openSessions, .known(0))
        XCTAssertNotNil(row.openSessions.reason)
        XCTAssertFalse(row.isWorking)
    }

    /// A lease that does not say alive does not hide one that does:  the count stays known.
    func test_one_live_lease_keeps_the_count_known_beside_one_that_does_not_say() async throws {
        let state = listenerJSON.replacingOccurrences(of: "\"alive\": false,", with: "")
        XCTAssertNotEqual(state, listenerJSON, "the fixture edit must land")
        let model = try await read(runner: healthyRunner(), listener: state)
        XCTAssertEqual(try XCTUnwrap(model.rows.first).openSessions, .known(1))
    }

    /// A configuration file that is there but cannot be read is said to be a problem,  because
    /// it could hide a seat the Mac refuses.  A missing one is normal and says nothing.
    func test_an_unreadable_configuration_is_a_problem_and_a_missing_one_is_not() async throws {
        let location = try listenerLocation(state: listenerJSON)
        try FileManager.default.createDirectory(at: location.configURL, withIntermediateDirectories: true)
        let reader = SeatActivityReader(
            location: location,
            runner: healthyRunner(),
            now: { Date(timeIntervalSince1970: 1792821067) },
            partitionURL: try partitionFile(partitionTOML)
        )
        let model = await reader.read(roster: ["CLAUDE"])
        XCTAssertTrue(
            model.problems.contains { $0.contains("configuration") && $0.contains("could not be read") },
            "\(model.problems)"
        )
        let missing = try await read(runner: healthyRunner(), listener: listenerJSON)
        XCTAssertEqual(missing.problems, [])
    }

    /// A partition file far larger than any real one is not read whole.
    func test_a_partition_file_far_larger_than_any_real_one_is_not_read() async throws {
        let url = try partitionFile(nil)
        XCTAssertTrue(FileManager.default.createFile(atPath: url.path, contents: nil))
        let handle = try FileHandle(forWritingTo: url)
        try handle.truncate(atOffset: UInt64(WakeSettingsReader.maximumFileBytes + 1))
        try handle.close()
        let reader = SeatActivityReader(
            location: try listenerLocation(state: listenerJSON),
            runner: healthyRunner(),
            now: { Date(timeIntervalSince1970: 1792821067) },
            partitionURL: url
        )
        let model = await reader.read(roster: ["CLAUDE"])
        XCTAssertTrue(model.problems.contains { $0.contains("larger than Muster reads") }, "\(model.problems)")
        XCTAssertEqual(model.rows.first?.place.group, .notKnown)
    }

    /// The seats with a live session are counted on the Mac only.
    func test_seats_with_sessions_counts_live_mac_leases() async throws {
        let model = try await read(roster: ["CLAUDE", "MA"], runner: healthyRunner(), listener: listenerJSON)
        XCTAssertEqual(model.seatsWithSessions.map(\.seat), ["CLAUDE"])
    }

    // MARK: Sessions and wakes (SEATS-23)

    /// A lease's letter,  title and verified machine are read,  and an ended lease is
    /// kept with its reason.
    func test_session_letters_titles_and_hosts_are_read() async throws {
        // The fixture's own indentation is gone once the literal is read,  so the first
        // lease's last field is matched by its value and the lease id after it.
        let state = listenerJSON.replacingOccurrences(
            of: "\"pending\": 0\n        },",
            with: "\"pending\": 1, \"session_id\": \"abc\", \"letter\": \"B\", "
                + "\"title\": \"Fix the panel\", \"verified_host\": \"mac\"\n        },"
        )
        XCTAssertNotEqual(state, listenerJSON, "the fixture edit must land")
        let model = try await read(runner: healthyRunner(), listener: state)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.sessions.count, 2)
        let live = try XCTUnwrap(row.sessions.first)
        XCTAssertEqual(live.letter, "B")
        XCTAssertEqual(live.title, "Fix the panel")
        XCTAssertEqual(live.verifiedHost, .mac)
        XCTAssertTrue(live.line.contains("B Fix the panel"))
        XCTAssertTrue(live.line.contains("on the Mac"))
        XCTAssertTrue(live.line.contains("1 waiting"))
        let ended = try XCTUnwrap(row.sessions.last)
        XCTAssertEqual(ended.alive, false)
        XCTAssertTrue(ended.line.contains("ended"))
        XCTAssertTrue(ended.line.contains("pid gone"))
    }

    /// A running wake makes the seat working,  even with no live session.
    func test_a_running_wake_makes_the_seat_working() async throws {
        let state = listener(claudeField: "\"running\": \"wake-123\",\n          \"last_event\": 1792821060.0")
            .replacingOccurrences(of: "\"alive\": true", with: "\"alive\": false")
        let model = try await read(runner: healthyRunner(), listener: state)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.openSessions, .known(0))
        XCTAssertTrue(row.runningWake)
        XCTAssertTrue(row.isWorking)
        XCTAssertEqual(row.lastEvent, Date(timeIntervalSince1970: 1792821060))
    }

    /// A seat with no running wake and no live session is not working.
    func test_a_seat_with_nothing_running_is_not_working() async throws {
        let state = listener(claudeField: "\"running\": null")
            .replacingOccurrences(of: "\"alive\": true", with: "\"alive\": false")
        let model = try await read(runner: healthyRunner(), listener: state)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.runningWake)
        XCTAssertFalse(row.isWorking)
    }

    // MARK: The quota (SEATS-22)

    /// The quota column is the eavesdropper's own reading:  exhausted,  with when it resets.
    func test_an_exhausted_quota_reads_from_the_listener() async throws {
        let state = listener(claudeField: """
        "quota": {"known": true, "exhausted": true, "remaining": 0, "reset_at": "2026-10-24T21:00:00Z", "reason": "weekly cap", "source": "codecaps"}
        """)
        let model = try await read(runner: healthyRunner(), listener: state)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.quota, .known(.exhausted(resetAt: FleetTime.parse("2026-10-24T21:00:00Z"))))
        XCTAssertEqual(row.quota.value?.label, "Exhausted")
        XCTAssertTrue(row.needsAttention)
        XCTAssertTrue(row.notes.contains { $0.column == "quota" && $0.text == "weekly cap" })
    }

    /// A quota with room reads as the percentage left.
    func test_a_quota_with_room_reads_as_the_percentage_left() async throws {
        let state = listener(claudeField: """
        "quota": {"known": true, "exhausted": false, "remaining": 42.7, "reset_at": null}
        """)
        let model = try await read(runner: healthyRunner(), listener: state)
        XCTAssertEqual(model.rows.first?.quota.value?.label, "42% left")
    }

    /// A quota the snapshot cannot speak for reads as not reported,  never as fine.
    func test_an_unknown_quota_reads_as_not_reported() async throws {
        let state = listener(claudeField: """
        "quota": {"known": false, "exhausted": false, "remaining": null, "reset_at": null}
        """)
        let model = try await read(runner: healthyRunner(), listener: state)
        XCTAssertEqual(model.rows.first?.quota, .known(.notReported))
        XCTAssertEqual(model.rows.first?.quota.value?.label, "Not reported")
    }

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
            runner: healthyRunner(search: message),
            listener: listenerJSON
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertTrue(row.postSaysAtQuota)
        // The quota column is still the eavesdropper's reading,  not the post's words.
        XCTAssertEqual(row.quota, .known(.notReported))
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
        let model = try await read(runner: runner, listener: listenerJSON)
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

    /// A paused seat reads as paused,  whatever else the eavesdropper says.
    func test_a_paused_seat_reads_as_paused() async throws {
        let paused = listenerJSON.replacingOccurrences(
            of: "\"pause\": {}",
            with: "\"pause\": {\"CLAUDE\": {\"by\": \"owner\", \"ts\": 1792820000}}"
        )
        let model = try await read(runner: healthyRunner(), listener: paused)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.listener, .known(.paused))
    }

    /// A seat the eavesdropper's configuration turned off reads as off,  and not unknown.
    func test_a_disabled_seat_reads_as_off() async throws {
        let disabled = listenerJSON.replacingOccurrences(
            of: "\"disabled\": []",
            with: "\"disabled\": [\"OPENCODE\"]"
        )
        let model = try await read(
            roster: ["OPENCODE"],
            runner: healthyRunner(),
            listener: disabled
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.listener, .known(.off))
    }

    /// A seat the eavesdropper could not start reads as skipped,  and the reason is shown
    /// under the row (SEATS-24, COPY-54).
    func test_a_skipped_seat_shows_the_listeners_reason_under_the_row() async throws {
        let skipped = listenerJSON.replacingOccurrences(
            of: "\"skipped_seats\": {}",
            with: "\"skipped_seats\": {\"OPENCODE\": [\"no muse binary\"]}"
        )
        let model = try await read(
            roster: ["OPENCODE"],
            runner: healthyRunner(),
            listener: skipped
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.listener, .known(.skipped("no muse binary")))
        XCTAssertTrue(row.notes.contains { $0.column == "eavesdropper" && $0.text.contains("no muse binary") })
    }

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
            runner: runner,
            listener: listenerJSON
        )
        XCTAssertEqual(model.requestCeiling, 3)
        XCTAssertEqual(model.requestsMade, 3)
        XCTAssertLessThanOrEqual(model.requestsMade, model.requestCeiling)
        XCTAssertEqual(runner.calls.filter { $0.verb == "users" }.count, 1)
        XCTAssertEqual(runner.calls.filter { $0.verb == "search" }.count, 2)
        XCTAssertEqual(runner.calls.filter { $0.verb == "presence" }.count, 0)
    }

    /// A seat the realm knows no bot for costs no calls at all.
    func test_a_seat_with_no_bot_costs_no_calls() async throws {
        let runner = healthyRunner()
        let model = try await read(
            roster: ["GROK"],
            runner: runner,
            listener: listenerJSON
        )
        XCTAssertEqual(model.requestsMade, 1)
        XCTAssertEqual(runner.calls.count, 1)
    }

    /// A failed user list stops the refresh after one call.
    func test_a_failed_user_list_costs_one_call() async throws {
        let runner = FakeChatRunner { _ in
            ChatCLIResult(exitCode: 3, stdout: "", stderr: "no credential")
        }
        let model = try await read(roster: ["CLAUDE", "OPENCODE"], runner: runner, listener: listenerJSON)
        XCTAssertEqual(model.requestsMade, 1)
        XCTAssertEqual(runner.calls.count, 1)
    }

    // MARK: The read-only guard

    /// The runner refuses a verb that writes,  before any process exists.
    func test_the_runner_refuses_a_verb_that_writes() async {
        let cli = ChatCLI(
            location: { ChatLocation(command: URL(fileURLWithPath: "/nowhere/simple-chat")) },
            seat: "CLAUDE",
            home: URL(fileURLWithPath: "/nowhere")
        )
        do {
            _ = try await cli.run(ChatInvocation(["post", "--help"]))
            XCTFail("a verb that writes must be refused")
        } catch let error as ChatCLIError {
            XCTAssertEqual(error, .writeVerbRefused("post"))
        } catch {
            XCTFail("the refusal must be a ChatCLIError,  got \(error)")
        }
    }

    /// The only verbs the panel may run are the read-only ones,  and presence is not one.
    func test_the_only_verbs_the_panel_may_run_are_read_only() {
        XCTAssertEqual(ChatInvocation.readOnlyVerbs, Set(["users", "search"]))
    }

    /// A CLI that is not on this Mac says so in a sentence that makes sense (COPY-53).
    func test_a_missing_cli_says_where_muster_looked() async {
        let cli = ChatCLI(location: { nil }, seat: "CLAUDE", home: URL(fileURLWithPath: "/nowhere"))
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

    // MARK: The freshness window (TESTS-19)

    /// The window is sixty seconds.
    func test_the_listener_freshness_window_is_sixty_seconds() {
        XCTAssertEqual(SeatActivityReader.defaultMaximumListenerAge, 60)
    }

    /// A file exactly sixty seconds old is still believed.
    func test_a_listener_state_sixty_seconds_old_is_read() async throws {
        let model = try await read(runner: healthyRunner(), listener: listenerJSON, now: 1792821067 + 60)
        XCTAssertEqual(model.rows.first?.openSessions, .known(1))
    }

    /// A file sixty-one seconds old is not.
    func test_a_listener_state_sixty_one_seconds_old_is_not_read() async throws {
        let model = try await read(runner: healthyRunner(), listener: listenerJSON, now: 1792821067 + 61)
        XCTAssertFalse(model.rows.first?.openSessions.isKnown ?? true)
        XCTAssertTrue(model.problems.first?.contains("61 seconds ago") == true)
    }

    // MARK: Review fixes (Kody and Sentry on #926)

    /// An eavesdropper state file the eavesdropper stopped writing is not believed.
    func test_a_stale_listener_state_leaves_the_listener_columns_unknown() async throws {
        let model = try await read(
            runner: healthyRunner(),
            listener: listenerJSON,
            now: 1792821067 + 3 * 3600
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertFalse(row.listener.isKnown)
        XCTAssertNotEqual(row.openSessions, .known(1))
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertTrue(model.problems[0].contains("3 hours ago"))
    }

    /// An eavesdropper state file that names no time is not believed either.
    func test_a_listener_state_with_no_time_is_unknown() async throws {
        let undated = listenerJSON.replacingOccurrences(of: "\"updated\": 1792821067.0,", with: "")
        let model = try await read(runner: healthyRunner(), listener: undated)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertFalse(row.listener.isKnown)
    }

    /// A fresh file inside the window is still believed.
    func test_a_listener_state_inside_the_window_is_read() async throws {
        let model = try await read(runner: healthyRunner(), listener: listenerJSON, now: 1792821067 + 30)
        XCTAssertEqual(model.rows.first?.openSessions, .known(1))
    }

    /// A wakes-only pause keeps the connection state and says the wakes are paused.
    func test_a_wakes_only_pause_keeps_the_connection_state() async throws {
        let paused = listenerJSON.replacingOccurrences(
            of: "\"pause\": {}",
            with: "\"pause\": {\"CLAUDE\": {\"wakes_only\": true, \"by\": \"owner\", \"ts\": 1792820000}}"
        )
        let model = try await read(runner: healthyRunner(), listener: paused)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.listener, .known(.wakesPaused(connected: true)))
        XCTAssertEqual(row.listener.value?.label, "Connected, wakes paused")
    }

    /// A full pause in the fleet-wide scope wins over a wakes-only pause for the seat.
    func test_a_full_pause_wins_over_a_wakes_only_pause() async throws {
        let paused = listenerJSON.replacingOccurrences(
            of: "\"pause\": {}",
            with: "\"pause\": {\"CLAUDE\": {\"wakes_only\": true}, \"*\": {\"by\": \"owner\"}}"
        )
        let model = try await read(runner: healthyRunner(), listener: paused)
        XCTAssertEqual(model.rows.first?.listener, .known(.paused))
    }

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
            seat: "CLAUDE",
            home: FileManager.default.temporaryDirectory
        )
        do {
            _ = try await cli.run(ChatInvocation(["users", "--json"], timeout: 1))
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
            seat: "CLAUDE",
            home: FileManager.default.temporaryDirectory
        )
        let result = try await cli.run(ChatInvocation(["users", "--json"], timeout: 5))
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
        let model = try await read(runner: runner, listener: listenerJSON)
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertEqual(model.requestsMade, 1)
    }

    /// The user list asks for deactivated accounts,  and an active account wins.
    func test_the_user_list_includes_deactivated_accounts_and_prefers_the_active_one() {
        XCTAssertTrue(ChatInvocation.users().arguments.contains("--inactive"))
        let users = """
        {"user_id": 9001, "full_name": "Claude", "email": "claude-bot@example.test", "is_bot": true, "is_active": true, "seat": "CLAUDE"}
        {"user_id": 9003, "full_name": "Claude (old)", "email": "claude-old-bot@example.test", "is_bot": true, "is_active": false, "seat": "CLAUDE"}
        """
        let bots = try? XCTUnwrap(SeatActivityReader.decodeBots(users))
        XCTAssertEqual(bots?["CLAUDE"]?.userID, 9001)
    }

    /// A reading seat that looks like a flag is refused before a process exists.
    func test_a_reading_seat_that_looks_like_a_flag_is_refused() async throws {
        let script = FileManager.default.temporaryDirectory
            .appendingPathComponent("muster-fake-chat-\(UUID().uuidString)")
        try "#!/bin/sh\necho '{}'\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        addTeardownBlock { try? FileManager.default.removeItem(at: script) }
        let cli = ChatCLI(
            location: { ChatLocation(command: script) },
            seat: "--zuliprc=/tmp/other",
            home: FileManager.default.temporaryDirectory
        )
        do {
            _ = try await cli.run(ChatInvocation.users())
            XCTFail("a seat that looks like a flag must be refused")
        } catch let error as ChatCLIError {
            XCTAssertEqual(error, .invalidArgument("--zuliprc=/tmp/other"))
        }
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
            seat: "CLAUDE",
            home: FileManager.default.temporaryDirectory
        )
        let started = Date()
        do {
            _ = try await cli.run(ChatInvocation(["users", "--json"], timeout: 1))
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
