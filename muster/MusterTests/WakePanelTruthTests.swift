import XCTest

/// Kody on #1469:  "not tried yet" from a status that is not current, or a stopped
/// eavesdropper's, is not known now, and says why.
final class OperatorVoiceStaleNotTriedTests: XCTestCase {
}

/// The wake panel tells the truth:  a status it could not read is not a stopped eavesdropper, an old
/// status is not current, a seat Wakes or Captures Only by its routine table alone, a ledger it
/// could not read is not zero use, and edits survive a read.
final class WakePanelTruthTests: XCTestCase {
    // MARK: Fixtures

    /// MM holds its routine inline, CLAUDE under its own header, OLD keeps a wake line left over
    /// from the old wake types and no routine, and QUIET has neither.
    private let config = """
    [daemon]
    host_wake_load_per_core = 0

    [seat.MM]
    bot = "MM"
    wake = "http"
    routine = { url_env = "MM_ROUTINE_URL", key_env = "MM_ROUTINE_KEY" }
    budget = { wakes_per_hour = 5, wakes_per_day = 80 }

    [seat.CLAUDE]
    bot = "CLAUDE"
    wake = "http"

    [seat.CLAUDE.routine]
    url_env = "CLAUDE_ROUTINE_URL"
    key_env = "CLAUDE_ROUTINE_KEY"

    [seat.OLD]
    bot = "OLD"
    wake = "openrouter"
    model = "openai/gpt-5"

    [seat.QUIET]
    bot = "QUIET"
    wake = "inbox"
    """

    private let partition = ListenerPartition(seats: [
        "MM": "mac", "CLAUDE": "mac", "OLD": "mac", "QUIET": "mac", "MA": "server",
    ])

    private func status(_ json: String) -> ListenerStatus {
        try! JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
    }

    // MARK: Whether the eavesdropper is running

    /// One field in a shape Muster does not expect costs that field only, not the whole status.
    func test_a_mistyped_field_does_not_blank_the_status() throws {
        let json = """
        {"running": true, "pid": "not a number", "status": {"updated": "soon", "instance": "mac",
          "seats": {"MM": {"bot": "MM", "wake": "muse", "wakes_24h": "many", "connected": true,
                           "budget": {"wakes_per_hour": 5}}}}}
        """
        let decoded = try JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.running, true)
        XCTAssertNil(decoded.pid)
        XCTAssertNil(decoded.status?.updated)
        XCTAssertEqual(decoded.status?.instance, "mac")
        XCTAssertNil(decoded.seat("MM")?.wakes24h)
        XCTAssertEqual(decoded.seat("MM")?.connected, true)
        XCTAssertEqual(decoded.seat("MM")?.budget?["wakes_per_hour"], .number(5))
    }

    // MARK: The ledger

    /// A topic holding U+2028 is one row, as the eavesdropper wrote it, and is counted.
    func test_a_unicode_line_separator_in_a_topic_does_not_split_a_row() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let ledger = "{\"ts\": 999900.0, \"wake_id\": \"a\", \"state\": \"accepted\", \"owner\": false, \"topic_key\": \"k\", \"topic\": \"one\u{2028}two\u{0085}three\"}\n"
        let parsed = WakeLedger.parse(ledger, now: now)
        XCTAssertEqual(parsed.malformed, 0)
        XCTAssertEqual(parsed.outcomes.first?.topic, "one\u{2028}two\u{0085}three")
        XCTAssertEqual(WakeLedger.usage(from: parsed, now: now).wakesThisHour, 1)
    }

    /// An old torn row says nothing about today's counts;  a torn last row could.
    func test_only_a_torn_row_inside_the_window_counts() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let ledger = """
        {"ts": 700000.0, "wake_id": "old", "state": "accepted"}
        {"ts": 700010.0, "wake_id": "old", "sta
        {"ts": 800000.0, "wake_id": "older", "state": "done"}
        {"ts": 999900.0, "wake_id": "a", "state": "accepted"}
        {"ts": 999950.0, "wake_id": "b", "st
        """
        let parsed = WakeLedger.parse(ledger, now: now)
        XCTAssertEqual(parsed.malformed, 2)
        XCTAssertEqual(parsed.malformedRecent, 1)
        XCTAssertEqual(parsed.oldest, Date(timeIntervalSince1970: 700_000))
    }

    /// Ledger states and reasons read as words.
    func test_ledger_states_read_as_words() {
        XCTAssertEqual(WakeLedger.words(forState: "held_back"), "held back")
        XCTAssertEqual(WakeLedger.words(forState: "done"), "answered")
        XCTAssertEqual(WakeLedger.words(forState: "brand_new"), "brand new")
        XCTAssertEqual(WakeLedger.words(forReason: "operator_unavailable"), "operator unavailable")
    }

    // MARK: Small pieces

    /// The Operator's line says the Mac needs no section, and names a broken one.
    func test_the_operator_line_is_true_either_way() {
        let none = OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.MM]\nwake = \"muse\"\n"))
        XCTAssertNil(none.problem)
        XCTAssertTrue(none.summary.contains("needs none"))
        XCTAssertFalse(none.summary.contains("not provisioned"))
        let broken = OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.OPERATOR]\nbot = \"X\"\ncreds = \"infisical\"\nwake = \"inbox\"\n"))
        XCTAssertTrue(broken.summary.contains("bot must be Operator"))
    }

    // MARK: The Operator truth audit (F2 to F12)

    /// F6.  A session with no letter says no letter was reported, which is true whether it never
    /// registered one or the lookup failed, and never claims it has no registered name.
    func test_a_session_without_a_letter_says_no_letter_was_reported() throws {
        let lease = try JSONDecoder().decode(
            ListenerStateDocument.Lease.self,
            from: Data(#"{"lease_id": "a", "platform": "claude-code", "alive": true, "watcher": true}"#.utf8)
        )
        let line = SeatSession(lease: lease, index: 0).line
        XCTAssertTrue(line.hasPrefix(SeatActivityText.noLetter), line)
        XCTAssertEqual(SeatActivityText.noLetter, "No letter reported")
        XCTAssertFalse(line.contains("registered name"))
        let titled = try JSONDecoder().decode(
            ListenerStateDocument.Lease.self,
            from: Data(#"{"alive": true, "watcher": true, "title": "Fix the panel"}"#.utf8)
        )
        XCTAssertTrue(SeatSession(lease: titled, index: 0).line.hasPrefix("No letter reported:  Fix the panel"))
    }
}
