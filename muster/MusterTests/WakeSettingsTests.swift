import XCTest

/// The wake settings panel:  what it shows,  when it says DRIFT,  and what it refuses to
/// write.
final class WakeSettingsTests: XCTestCase {
    /// A file like the eavesdropper's own.  The wake line is today's, left over from the old wake
    /// types:  Muster reads the routine table, and leaves that line as it is.
    private let sample = """
    [daemon]
    host_wake_load_per_core = 0

    [seat.MM]
    bot = "MM"
    wake = "http"
    routine = { url_env = "MM_ROUTINE_URL", key_env = "MM_ROUTINE_KEY", timeout_seconds = 15 }
    budget = { wakes_per_hour = 5, wakes_per_day = 80, per_topic_per_hour = 6, owner_per_day = 40, owner_per_topic_per_hour = 12, usd_per_day = 4.0 }
    live = { per_hour = 6, per_day = 30, per_topic_minutes = 5, owner_per_day = 20, loop_turns = 3 }
    """

    /// A partition like the eavesdropper's own.
    private let partitionText = """
    version = 1

    [seats]
    # Mac seats
    MM = "mac"
    MA = "server"
    BF-BUILDER = "none"
    """

    // MARK: The seat partition

    /// The partition reads the way the eavesdropper reads it,  and every kind of seat has its host.
    func test_the_partition_gives_each_seat_a_host() {
        let partition = ListenerPartition.parse(partitionText, path: "/p")
        XCTAssertNil(partition.problem)
        XCTAssertEqual(partition.host(of: "MM"), .mac)
        XCTAssertEqual(partition.host(of: "MA"), .server)
        XCTAssertEqual(partition.host(of: "BF-BUILDER"), .noListener)
        XCTAssertEqual(partition.host(of: "KIMI"), .unlisted)
        // The eavesdropper upper-cases a section's name before it looks the seat up.
        XCTAssertEqual(partition.host(of: "ma"), .server)
        XCTAssertTrue(partition.host(of: "MM").isTheMac)
        for seat in ["MA", "BF-BUILDER", "KIMI"] {
            XCTAssertTrue(partition.host(of: seat).isElsewhere, seat)
            XCTAssertFalse(partition.host(of: seat).isTheMac, seat)
            XCTAssertNotNil(partition.host(of: seat).readOnlyReason, seat)
        }
    }

    /// A partition that is not read cleanly holds no seat,  as `load_partition` fails closed.
    func test_a_partition_that_is_not_read_cleanly_holds_no_seat() throws {
        let broken = [
            "version = 1\n",
            "[seats]\nMM = \"laptop\"\n",
            "[seats]\nMM = 3\n",
            "[seats]\nMM = \"mac\"\nMM = \"server\"\n",
            "[seats]\nnot a line\nMM = \"mac\"\n",
            "[seats]\nMM = \"mac\"\n[seats]\nMA = \"server\"\n",
            "[seats]\nbad.seat = \"mac\"\n",
        ]
        for text in broken {
            let partition = ListenerPartition.parse(text, path: "/p")
            XCTAssertNotNil(partition.problem, text)
            guard case .unknown = partition.host(of: "MM") else {
                XCTFail("expected unknown for: \(text)")
                continue
            }
            XCTAssertFalse(partition.host(of: "MM").isTheMac, text)
            XCTAssertFalse(partition.host(of: "MM").isElsewhere, text)
        }
        XCTAssertNotNil(ListenerPartition.load(from: nil).problem)
        let missing = URL(fileURLWithPath: "/nonexistent/partition.toml")
        XCTAssertNotNil(ListenerPartition.load(from: missing).problem)
    }

    /// The repository's own partition parses here,  and says what the eavesdropper's tests say.
    func test_the_repository_partition_reads_as_documented() {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let partition = ListenerPartition.load(from: root.appendingPathComponent(ListenerPartition.relativePath))
        XCTAssertNil(partition.problem)
        // One listener, on the server, since Nov 10:  only the Operator's service identity stays on the Mac.
        XCTAssertEqual(partition.host(of: "OPERATOR"), .mac)
        for seat in ["CLAUDE", "CODEX", "AG", "CURSOR", "GROK", "CLUTCH", "FX", "MM", "MC", "OPENCODE", "ZC", "KIMI", "AMP",
                     "MA", "ECHO", "WISE", "GROK-WEB", "JET", "GB-FIXER", "BF-DIRECTOR", "BF-COMPILER", "BF-ENGINEER", "BF-TRADER"] {
            XCTAssertEqual(partition.host(of: seat), .server, seat)
        }
        XCTAssertEqual(partition.host(of: "BF-BUILDER"), .noListener)
    }

    /// A tag is one the eavesdropper accepts,  or it never reaches a `[seat.X]` header.
    func test_only_a_real_seat_name_counts() {
        for good in ["MM", "GROK-WEB", "BF_X", "A1"] {
            XCTAssertTrue(ListenerPartition.isSeatName(good), good)
        }
        for bad in ["", "mm", "-MM", "MM.budget", "MM]", "MM\n", "M M", String(repeating: "A", count: 33)] {
            XCTAssertFalse(ListenerPartition.isSeatName(bad), bad)
        }
    }

    // MARK: A seat the Mac does not hold

    /// A section that names this instance,  or a value the eavesdropper does not take as an instance,
    /// is not that refusal,  so the partition still decides.
    func test_an_instance_key_that_is_not_the_other_instance_changes_nothing() {
        let partition = ListenerPartition(seats: ["MM": "mac", "MA": "server"])
        let mine = ListenerConfigDocument(text: "[seat.MM]\ninstance = \"mac\"\n")
        XCTAssertEqual(partition.host(of: "MM", in: mine), .mac)
        let odd = ListenerConfigDocument(text: "[seat.MM]\ninstance = \"laptop\"\n")
        XCTAssertEqual(partition.host(of: "MM", in: odd), .mac)
        let server = ListenerConfigDocument(text: "[seat.MA]\ninstance = \"server\"\n")
        XCTAssertEqual(partition.host(of: "MA", in: server), .server)
        let unread = ListenerPartition(unreadable: "It is missing.")
        guard case .unknown = unread.host(of: "MM", in: ListenerConfigDocument(text: "[seat.MM]\ninstance = \"server\"\n")) else {
            XCTFail("a partition that cannot be read still holds no seat")
            return
        }
    }

    // MARK: What a reload did

    private func status(
        updated: Double? = nil,
        reloadAt: Double? = nil,
        refused: [String]? = nil,
        errors: [String]? = nil
    ) -> ListenerStatus {
        var state = ListenerStatus.State()
        state.updated = updated
        state.reload_at = reloadAt
        state.reload_refused = refused
        state.config_errors = errors
        var status = ListenerStatus()
        status.running = true
        status.status = state
        return status
    }

    // MARK: How the file is replaced

    private func backups(_ space: (home: URL, config: URL, partition: URL)) -> URL {
        space.config.deletingLastPathComponent()
            .resolvingSymlinksInPath()
            .appendingPathComponent("listener/muster-config-backups", isDirectory: true)
    }

    // MARK: The ledger

    /// The ledger's wakes are counted the way the runtime counts them.
    func test_the_ledger_counts_the_wakes_the_runtime_counts() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let ledger = """
        {"ts": 999900.0, "wake_id": "a", "state": "accepted", "owner": false, "topic_key": "c\\u0000t1", "topic": "t1"}
        {"ts": 999910.0, "wake_id": "a", "state": "done", "owner": false, "topic_key": "c\\u0000t1", "topic": "t1"}
        {"ts": 999500.0, "wake_id": "b", "state": "accepted", "owner": true, "topic_key": "c\\u0000t2", "topic": "t2"}
        {"ts": 900000.0, "wake_id": "c", "state": "accepted", "owner": false, "topic_key": "c\\u0000t1", "topic": "t1"}
        """
        let usage = WakeLedger.usage(from: ledger, now: now)
        // The runtime counts the owner's wakes apart from everybody else's,  so the day
        // count here is the seat's own and the owner's is its own.
        XCTAssertEqual(usage.wakesThisHour, 1)
        XCTAssertEqual(usage.wakesThisDay, 1)
        XCTAssertEqual(usage.ownerThisDay, 1)
        XCTAssertEqual(usage.worstTopicThisHour, 1)
        XCTAssertEqual(usage.worstOwnerTopicThisHour, 1)
    }

    /// A wake that was never accepted is not counted.
    func test_a_wake_that_was_never_accepted_is_not_counted() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let ledger = """
        {"ts": 999900.0, "wake_id": "a", "state": "queued", "owner": false, "topic_key": "c\\u0000t1"}
        """
        let usage = WakeLedger.usage(from: ledger, now: now)
        XCTAssertEqual(usage.wakesThisDay, 0)
        XCTAssertEqual(usage.wakesThisHour, 0)
    }

    /// The panel shows the seat's last wakes.
    func test_the_ledger_gives_the_last_wakes() {
        let ledger = """
        {"ts": 999900.0, "wake_id": "a", "state": "accepted", "owner": false, "topic_key": "c\\u0000t1", "topic": "first"}
        {"ts": 999950.0, "wake_id": "b", "state": "done", "owner": true, "topic_key": "c\\u0000t2", "topic": "second"}
        """
        let outcomes = WakeLedger.outcomes(from: ledger)
        XCTAssertEqual(outcomes.count, 2)
        XCTAssertEqual(outcomes.first?.id, "b")
        XCTAssertEqual(outcomes.first?.topic, "second")
        XCTAssertEqual(outcomes.first?.owner, true)
    }

    // MARK: The ledger,  read once

    /// One read of the ledger feeds both the counters and the recent wakes.
    func test_the_ledger_is_parsed_once_for_both_views() {
        let ledger = """
        {"ts": 999900.0, "wake_id": "a", "state": "accepted", "owner": false, "topic_key": "c\\u0000t1", "topic": "first"}
        {"ts": 999950.0, "wake_id": "b", "state": "accepted", "owner": true, "topic_key": "c\\u0000t2", "topic": "second"}
        """
        let parsed = WakeLedger.parse(ledger)
        XCTAssertEqual(parsed.malformed, 0)
        XCTAssertEqual(parsed.wakes.count, 2)
        XCTAssertEqual(parsed.outcomes.count, 2)
        XCTAssertEqual(parsed.outcomes.first?.id, "b")
        XCTAssertEqual(parsed.outcomes.first?.topic, "second")
        // The counters come out of that same pass.
        let usage = WakeLedger.usage(from: parsed, now: Date(timeIntervalSince1970: 1_000_000))
        XCTAssertEqual(usage.wakesThisDay, 1)
        XCTAssertEqual(usage.ownerThisDay, 1)
    }

    /// A row that is not a ledger row is counted and said out loud,  not silently dropped.
    func test_a_malformed_ledger_row_is_counted_rather_than_hidden() {
        let ledger = """
        {"ts": 999900.0, "wake_id": "a", "state": "accepted", "owner": false, "topic_key": "c\\u0000t1"}
        {"ts": 999950.0, "wake_id": "b", "state":
        not json at all
        {"ts": 999960.0, "state": "accepted"}
        {"wake_id": "d", "state": "done"}
        """
        let parsed = WakeLedger.parse(ledger)
        XCTAssertEqual(parsed.malformed, 4)
        XCTAssertEqual(parsed.wakes.count, 1)
    }

    // MARK: The command line

    /// Deferred, dropped, skipped, failed, and expired wakes are counted in 24h usage, and reasons are preserved.
    func test_deferred_and_failed_wakes_are_counted_and_reasons_preserved() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let ledger = """
        {"ts": 999900.0, "wake_id": "w1", "state": "held_back", "reason": "host_busy", "owner": false, "topic": "AFC-536"}
        {"ts": 999905.0, "wake_id": "w1", "state": "accepted", "owner": false, "topic": "AFC-536"}
        {"ts": 999915.0, "wake_id": "w1", "state": "done", "owner": false, "topic": "AFC-536"}
        {"ts": 999910.0, "wake_id": "w2", "state": "skipped", "reason": "leased", "owner": false, "topic": "AFC-536"}
        {"ts": 999920.0, "wake_id": "w3", "state": "dropped", "why": "budget", "owner": false, "topic": "AFC-536"}
        {"ts": 999930.0, "wake_id": "w4", "state": "accepted", "owner": false, "topic": "AFC-536"}
        {"ts": 999940.0, "wake_id": "w4", "state": "failed", "reason": "exit_1", "owner": false, "topic": "AFC-536"}
        """
        let parsed = WakeLedger.parse(ledger)
        let usage = WakeLedger.usage(from: parsed, now: now)
        XCTAssertEqual(usage.heldBackThisDay, 1)
        XCTAssertEqual(usage.skippedThisDay, 1)
        XCTAssertEqual(usage.droppedThisDay, 1)
        XCTAssertEqual(usage.failedThisDay, 1)
        XCTAssertEqual(usage.deferredTotalThisDay, 3)

        let byID = Dictionary(uniqueKeysWithValues: parsed.outcomes.map { ($0.id, $0) })
        XCTAssertEqual(byID["w1"]?.reason, "host_busy")
        XCTAssertEqual(byID["w2"]?.reason, "leased")
        XCTAssertEqual(byID["w3"]?.reason, "budget")
        XCTAssertEqual(byID["w4"]?.reason, "exit_1")
    }

    func test_deferred_states_outside_24h_window_are_not_counted() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        // 3 days ago: 1_000_000 - 3 * 86400 = 740_800
        // held_back 3 days ago, accepted 1 hour ago (996_400)
        let ledger = """
        {"ts": 740800.0, "wake_id": "wOld", "state": "held_back", "reason": "host_busy", "owner": false, "topic": "AFC-536"}
        {"ts": 996400.0, "wake_id": "wOld", "state": "accepted", "owner": false, "topic": "AFC-536"}
        {"ts": 996500.0, "wake_id": "wOld", "state": "done", "owner": false, "topic": "AFC-536"}
        """
        let parsed = WakeLedger.parse(ledger)
        let usage = WakeLedger.usage(from: parsed, now: now)
        XCTAssertEqual(usage.heldBackThisDay, 0, "States reached before the 24h window must not count in daily deferred totals")
        XCTAssertEqual(usage.wakesThisDay, 1)
    }
}
