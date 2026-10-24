import XCTest

/// The Seat Activity panel:  what it reads,  how it fails,  and how many calls it makes.
///
/// Every test here runs against a fake fleet chat CLI and a listener state file in a
/// temporary directory.  Nothing here touches the network,  Jay's home directory or a
/// real Zulip account.
final class SeatActivityTests: XCTestCase {
    // MARK: The fixtures

    /// The listener's own published state,  with one seat that has one live lease and
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

    /// The user list,  as `users --json` prints it.
    private let usersJSON = """
    {"user_id": 1212574, "full_name": "Claude", "email": "claude-bot@simplewithus.zulipchat.com", "is_bot": true, "is_active": true, "role": 400, "seat": "CLAUDE"}
    {"user_id": 1213730, "full_name": "OpenCode", "email": "opencode-bot@simplewithus.zulipchat.com", "is_bot": true, "is_active": true, "role": 400, "seat": "OPENCODE"}
    """

    /// One message,  as `search --json` prints it.
    private let messageJSON = """
    {"id": 634573003, "sender_id": 1212574, "client": "agent-sync", "channel": "Work-In-Progress", "topic": "fleet tools doctor", "dm": false, "sender_email": "claude-bot@simplewithus.zulipchat.com", "sender_full_name": "Claude", "is_bot": true, "seat": "CLAUDE", "seat_app": "claude-code", "timestamp": 1792821027, "content": "[CLAUDE] fleet-tools-doctor on fleet-hetzner-nbg1:  5 failing checks.", "flags": [], "sibling": false, "mentioned": false, "reactions": []}
    """

    /// A presence answer,  as `presence --json` prints it.
    private func presenceJSON(status: String, timestamp: Int?) -> String {
        let stamp = timestamp.map(String.init) ?? "null"
        return "{\"user_id\": 1212574, \"status\": \"\(status)\", \"timestamp\": \(stamp)}"
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
    private func healthyRunner(
        presence: String? = nil,
        search: String? = nil
    ) -> FakeChatRunner {
        FakeChatRunner { invocation in
            switch invocation.verb {
            case "users":
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON, stderr: "")
            case "presence":
                return ChatCLIResult(
                    exitCode: 0,
                    stdout: presence ?? self.presenceJSON(status: "active", timestamp: 1792821067),
                    stderr: ""
                )
            case "search":
                return ChatCLIResult(exitCode: 0, stdout: search ?? self.messageJSON, stderr: "")
            default:
                return ChatCLIResult(exitCode: 2, stdout: "", stderr: "refused")
            }
        }
    }

    // MARK: The reading

    /// A listener state location in a temporary directory.
    private func listenerLocation(
        state: String?,
        fileManager: FileManager = .default
    ) throws -> ListenerStateLocation {
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("muster-seat-activity-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        var location = ListenerStateLocation(home: root)
        location.root = root.appendingPathComponent(".agent-sync", isDirectory: true)
        if let state {
            let listener = location.root.appendingPathComponent("listener", isDirectory: true)
            try fileManager.createDirectory(at: listener, withIntermediateDirectories: true)
            try state.write(
                to: listener.appendingPathComponent("status.json"),
                atomically: true,
                encoding: .utf8
            )
        }
        addTeardownBlock {
            try? fileManager.removeItem(at: root)
        }
        return location
    }

    /// Read the panel over a roster.
    private func read(
        roster: [String] = ["CLAUDE"],
        runner: ChatRunning,
        listener: String? = nil
    ) async throws -> SeatActivityModel {
        let location = try listenerLocation(state: listener)
        let reader = SeatActivityReader(
            location: location,
            runner: runner,
            now: { Date(timeIntervalSince1970: 1792821067) }
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
        XCTAssertEqual(row.openSessions, .known(1))
        XCTAssertEqual(row.listener, .known(.connected(jobs: 1)))
        XCTAssertEqual(row.presence, .known(.active))
        XCTAssertEqual(row.atQuota, .known(false))
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
        XCTAssertEqual(row.atQuota, .known(false))
    }

    // MARK: Failing closed

    /// A CLI that fails leaves every column it answered unknown,  and never empty.
    func test_a_cli_failure_leaves_every_column_unknown_and_never_empty() async throws {
        let runner = FakeChatRunner { invocation in
            if invocation.verb == "users" {
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON, stderr: "")
            }
            throw ChatCLIError.commandNotFound("/nowhere/simple-chat")
        }
        let model = try await read(runner: runner, listener: listenerJSON)
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertFalse(row.presence.isKnown)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertNotNil(row.presence.reason)
        XCTAssertNotNil(row.lastPost.reason)
        // The known part stays known:  the listener's own file was read fine.
        XCTAssertEqual(row.openSessions, .known(1))
    }

    /// A missing binary leaves the Zulip columns unknown,  and the row says why.
    func test_a_missing_binary_leaves_the_zulip_columns_unknown() async throws {
        let runner = FakeChatRunner { _ in
            throw ChatCLIError.commandNotFound("/nowhere/simple-chat")
        }
        let model = try await read(runner: runner, listener: listenerJSON)

        XCTAssertEqual(model.rows.count, 1)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.presence.isKnown)
        XCTAssertFalse(row.lastPost.isKnown)
        XCTAssertEqual(model.problems.count, 1)
        XCTAssertTrue(model.problems[0].contains("could not be read"))
    }

    /// A user list that fails leaves every seat's Zulip columns unknown.
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
            XCTAssertFalse(row.presence.isKnown)
            XCTAssertFalse(row.lastPost.isKnown)
        }
        XCTAssertEqual(model.problems.count, 1)
    }

    /// A presence answer with no record behind it is unknown,  and never offline.
    func test_a_seat_with_no_presence_record_is_unknown_and_not_offline() async throws {
        let model = try await read(
            runner: healthyRunner(presence: presenceJSON(status: "offline", timestamp: nil)),
            listener: listenerJSON
        )
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertFalse(row.presence.isKnown)
        XCTAssertNotNil(row.presence.reason)
        XCTAssertNotEqual(row.presence, .known(.offline))
    }

    /// A listener state file that cannot be read leaves the listener columns unknown.
    func test_an_unreadable_listener_state_leaves_the_listener_columns_unknown() async throws {
        let model = try await read(runner: healthyRunner(), listener: nil)
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertFalse(row.listener.isKnown)
        XCTAssertNotEqual(row.openSessions, .known(0))
        XCTAssertEqual(model.problems.count, 1)
    }

    /// A seat the listener publishes nothing about has unknown sessions,  and not zero.
    func test_a_seat_the_listener_does_not_publish_has_unknown_sessions_and_not_zero() async throws {
        let model = try await read(
            roster: ["OPENCODE"],
            runner: healthyRunner(),
            listener: listenerJSON
        )
        let row = try XCTUnwrap(model.rows.first)

        XCTAssertEqual(row.seat, "OPENCODE")
        XCTAssertFalse(row.openSessions.isKnown)
        XCTAssertNotEqual(row.openSessions, .known(0))
        XCTAssertFalse(row.listener.isKnown)
    }

    /// A seat the realm knows no bot for has unknown Zulip columns.
    func test_a_seat_with_no_bot_has_unknown_zulip_columns() async throws {
        let model = try await read(
            roster: ["GROK"],
            runner: healthyRunner(),
            listener: listenerJSON
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.presence.isKnown)
        XCTAssertFalse(row.lastPost.isKnown)
    }

    /// A deactivated bot account is unknown,  and the row says that it is deactivated.
    func test_a_deactivated_bot_reads_as_unknown_and_says_why() async throws {
        let users = """
        {"user_id": 1212574, "full_name": "Claude", "email": "claude-bot@simplewithus.zulipchat.com", "is_bot": true, "is_active": false, "role": 400, "seat": "CLAUDE"}
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
        XCTAssertFalse(row.presence.isKnown)
        XCTAssertTrue(row.presence.reason?.contains("deactivated") == true)
        XCTAssertEqual(model.requestsMade, 1)
    }

    /// An empty roster asks the CLI nothing at all.
    func test_an_empty_roster_makes_no_calls() async throws {
        let runner = healthyRunner()
        let model = try await read(roster: [], runner: runner, listener: listenerJSON)
        XCTAssertEqual(model.rows.count, 0)
        XCTAssertEqual(model.requestsMade, 0)
        XCTAssertEqual(runner.calls.count, 0)
    }

    /// The row says which parts are unknown,  so a reader knows what not to believe.
    func test_the_row_names_the_parts_that_are_unknown() async throws {
        let model = try await read(runner: healthyRunner(), listener: nil)
        let row = try XCTUnwrap(model.rows.first)
        let columns = row.unknowns.map { $0.column }
        XCTAssertTrue(columns.contains("open sessions"))
        XCTAssertTrue(columns.contains("listener"))
        XCTAssertFalse(columns.contains("presence"))
    }

    // MARK: The listener's own states

    /// A paused seat reads as paused,  whatever else the listener says.
    func test_a_paused_seat_reads_as_paused() async throws {
        let paused = listenerJSON.replacingOccurrences(
            of: "\"pause\": {}",
            with: "\"pause\": {\"CLAUDE\": {\"by\": \"owner\", \"ts\": 1792820000}}"
        )
        let model = try await read(runner: healthyRunner(), listener: paused)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.listener, .known(.paused))
    }

    /// A seat the listener's configuration turned off reads as off,  and not unknown.
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

    /// A seat the listener could not start reads as skipped,  with the listener's reason.
    func test_a_skipped_seat_reads_as_skipped_with_the_listeners_reason() async throws {
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
    }

    // MARK: The at-quota flag

    /// A last post that reports the seat at quota raises the flag.
    func test_a_last_post_that_reports_quota_raises_the_flag() async throws {
        let message = messageJSON.replacingOccurrences(
            of: "[CLAUDE] fleet-tools-doctor on fleet-hetzner-nbg1:  5 failing checks.",
            with: "[CLAUDE] I am at quota for this week."
        )
        let model = try await read(
            runner: healthyRunner(search: message),
            listener: listenerJSON
        )
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertEqual(row.atQuota, .known(true))
    }

    /// A last post that could not be read leaves the flag unknown,  and not false.
    func test_an_unreadable_last_post_leaves_the_quota_flag_unknown() async throws {
        let runner = FakeChatRunner { invocation in
            switch invocation.verb {
            case "users":
                return ChatCLIResult(exitCode: 0, stdout: self.usersJSON, stderr: "")
            case "presence":
                return ChatCLIResult(
                    exitCode: 0,
                    stdout: self.presenceJSON(status: "active", timestamp: 1792821067),
                    stderr: ""
                )
            default:
                return ChatCLIResult(exitCode: 5, stdout: "", stderr: "Zulip API error")
            }
        }
        let model = try await read(runner: runner, listener: listenerJSON)
        let row = try XCTUnwrap(model.rows.first)
        XCTAssertFalse(row.atQuota.isKnown)
        XCTAssertNotEqual(row.atQuota, .known(false))
    }

    /// The quota phrases are the ones the panel says they are.
    func test_the_quota_markers_are_the_narrow_ones() {
        XCTAssertTrue(SeatActivityQuota.reportsQuota("I am at quota for the week."))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("the provider quota is exhausted"))
        XCTAssertTrue(SeatActivityQuota.reportsQuota("I hit quota."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("the doctor reports five failing checks."))
        XCTAssertFalse(SeatActivityQuota.reportsQuota("quota is a setting in the panel."))
    }

    // MARK: The request ceiling

    /// The ceiling is one users call and two calls per seat.
    func test_the_request_ceiling_is_one_users_call_and_two_per_seat() {
        XCTAssertEqual(SeatActivityReader.requestCeiling(forSeats: 0), 1)
        XCTAssertEqual(SeatActivityReader.requestCeiling(forSeats: 1), 3)
        XCTAssertEqual(SeatActivityReader.requestCeiling(forSeats: 15), 31)
    }

    /// A refresh stays inside its ceiling,  and the panel shows the count it made.
    func test_a_refresh_stays_inside_its_ceiling() async throws {
        let runner = healthyRunner()
        let model = try await read(
            roster: ["CLAUDE", "OPENCODE"],
            runner: runner,
            listener: listenerJSON
        )
        XCTAssertEqual(model.requestCeiling, 5)
        XCTAssertEqual(model.requestsMade, 5)
        XCTAssertLessThanOrEqual(model.requestsMade, model.requestCeiling)
        // One users call,  then one presence and one search per seat.
        XCTAssertEqual(runner.calls.filter { $0.verb == "users" }.count, 1)
        XCTAssertEqual(runner.calls.filter { $0.verb == "presence" }.count, 2)
        XCTAssertEqual(runner.calls.filter { $0.verb == "search" }.count, 2)
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

    /// The only verbs the panel may run are the read-only ones.
    func test_the_only_verbs_the_panel_may_run_are_read_only() {
        XCTAssertEqual(ChatInvocation.readOnlyVerbs, Set(["users", "presence", "search"]))
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
}
