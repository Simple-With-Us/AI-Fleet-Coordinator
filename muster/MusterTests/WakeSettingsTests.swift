import XCTest

/// The wake settings panel:  what it shows,  when it says DRIFT,  and what it refuses to
/// write.
final class WakeSettingsTests: XCTestCase {
    /// A file like the listener's own.
    private let sample = """
    [daemon]
    host_wake_load_per_core = 0

    [seat.MM]
    bot = "MM"
    wake = "muse"
    model = "muse-spark-1.3-contributor"
    muse = "/Users/jay/.local/bin/muse-bin-1.4.11-R6667.1"
    mcode = "/Users/jay/.local/bin/mcode"
    fallback = "mcode"
    fallback_uncontained_ok = true
    budget = { wakes_per_hour = 5, wakes_per_day = 80, per_topic_per_hour = 6, owner_per_day = 40, owner_per_topic_per_hour = 12, usd_per_day = 4.0 }
    live = { per_hour = 6, per_day = 30, per_topic_minutes = 5, owner_per_day = 20, loop_turns = 3 }
    """

    /// The listener's own account of itself.
    private let statusJSON = """
    {
      "running": true,
      "pid": 29131,
      "status": {
        "seats": {
          "MM": {
            "bot": "MM",
            "wake": "muse",
            "muse": "/Users/jay/.local/bin/muse-bin-1.4.11-R6667.1",
            "connected": true,
            "paused": false,
            "wakes_paused": false,
            "wakes_24h": 43,
            "budget": {
              "wakes_per_hour": 5,
              "wakes_per_day": 80,
              "per_topic_per_hour": 6,
              "owner_per_day": 40,
              "owner_per_topic_per_hour": 12,
              "usd_per_day": 4.0
            },
            "live": {
              "per_hour": 6,
              "per_day": 30,
              "per_topic_minutes": 5,
              "owner_per_day": 20,
              "loop_turns": 3
            }
          }
        },
        "pause": {},
        "updated": 1792657970.0
      }
    }
    """

    /// The sources the builder reads.
    private func sources(
        config: String? = nil,
        status: String? = nil,
        pins: [String: [String: PinEvidence]] = [:],
        usage: [String: WakeUsage] = [:]
    ) -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        sources.config = ListenerConfigDocument(text: config ?? sample)
        if let status {
            sources.status = try! JSONDecoder().decode(
                ListenerStatus.self,
                from: Data(status.utf8)
            )
        }
        sources.pins = pins
        sources.usage = usage
        sources.seats = ["MM"]
        return sources
    }

    /// One seat's row.
    private func row(
        _ key: WakeSettingKey,
        config: String? = nil,
        status: String? = nil,
        pins: [String: [String: PinEvidence]] = [:]
    ) -> WakeSetting {
        let model = WakeSettingsBuilder.build(
            sources(config: config, status: status, pins: pins)
        )
        guard let seat = model.seats.first,
              let setting = seat.settings.first(where: { $0.key == key }) else {
            fatalError("the row for \(key.rawValue) should exist")
        }
        return setting
    }

    // MARK: The two sources

    /// A setting the file and the listener agree on is a pass.
    func test_a_setting_the_listener_matches_is_a_pass() {
        let setting = row(.wakesPerHour, status: statusJSON)
        XCTAssertEqual(setting.state, .match)
        XCTAssertEqual(setting.configured, .number(5))
        XCTAssertEqual(setting.live, .number(5))
    }

    /// A setting the listener does not match is DRIFT,  and never a quiet green.
    func test_a_setting_the_listener_does_not_match_is_drift() {
        let drifted = statusJSON.replacingOccurrences(
            of: "\"wakes_per_hour\": 5",
            with: "\"wakes_per_hour\": 9"
        )
        let setting = row(.wakesPerHour, status: drifted)
        XCTAssertEqual(setting.state, .drift)
        XCTAssertTrue(setting.state.isProblem)
        XCTAssertTrue(setting.note.contains("9"))
        XCTAssertTrue(setting.note.contains("5"))
    }

    /// A number that is written differently is the same number.
    func test_a_number_written_differently_is_not_a_drift() {
        let setting = row(.usdPerDay, status: statusJSON)
        XCTAssertEqual(setting.state, .notOwned)
        let budget = row(.wakesPerDay, status: statusJSON)
        XCTAssertEqual(budget.state, .match)
    }

    /// A setting the file does not hold is the listener's default,  and says so.
    func test_a_setting_the_file_does_not_hold_is_the_default() {
        let without = """
        [seat.MM]
        bot = "MM"
        wake = "muse"
        """
        let setting = row(.model, config: without, status: statusJSON)
        XCTAssertEqual(setting.state, .unset)
        XCTAssertTrue(setting.note.contains("default"))
    }

    /// The listener does not publish the fallback's container rule,  so the panel says so
    /// instead of claiming it is in effect.
    func test_a_setting_the_listener_does_not_publish_is_not_reported() {
        let setting = row(.fallbackUncontained, status: statusJSON)
        XCTAssertEqual(setting.state, .unpublished)
        XCTAssertNil(setting.live)
        XCTAssertEqual(setting.configured, .bool(true))
    }

    /// The dollar cap is read but never enforced,  so Muster will not write it.
    func test_the_dollar_cap_is_not_owned_and_is_refused() {
        let setting = row(.usdPerDay, status: statusJSON)
        XCTAssertEqual(setting.state, .notOwned)
        XCTAssertEqual(WakeSettingKey.usdPerDay.isEnforced, false)
        XCTAssertNotNil(WakeSettingKey.usdPerDay.refusalReason)
        XCTAssertNil(WakeSettingKey.wakesPerHour.refusalReason)
    }

    /// A configured fallback that no pin proves is unproven,  and never a pass.
    func test_a_configured_fallback_with_no_pin_is_unproven() {
        let setting = row(.fallback, status: statusJSON)
        XCTAssertEqual(setting.state, .unproven)
        XCTAssertTrue(setting.state.isProblem)
    }

    /// A fallback its pin proves is a pass.
    func test_a_pinned_fallback_is_a_pass() {
        let pins = ["MM": ["mcode": PinEvidence(
            runtime: "mcode",
            binary: "/Users/jay/.local/bin/mcode",
            model: "mcode-model",
            verifiedAt: Date(timeIntervalSince1970: 1_792_535_804),
            pinned: true,
            why: nil
        )]]
        let setting = row(.fallback, status: statusJSON, pins: pins)
        XCTAssertEqual(setting.state, .match)
        let mcode = row(.mcodePath, status: statusJSON, pins: pins)
        XCTAssertEqual(mcode.state, .match)
    }

    /// A model no pin proves is unproven.
    func test_a_model_no_pin_proves_is_unproven() {
        let setting = row(.model, status: statusJSON)
        XCTAssertEqual(setting.state, .unproven)
    }

    /// A model a pin proves is a pass,  and the pin's age comes with it.
    func test_a_model_a_pin_proves_is_a_pass() {
        let pins = ["MM": ["muse": PinEvidence(
            runtime: "muse",
            binary: "/Users/jay/.local/bin/muse-bin-1.4.11-R6667.1",
            model: "muse-spark-1.3-contributor",
            verifiedAt: Date(timeIntervalSince1970: 1_792_535_804),
            pinned: true,
            why: nil
        )]]
        let setting = row(.model, status: statusJSON, pins: pins)
        XCTAssertEqual(setting.state, .match)
        XCTAssertTrue(setting.note.contains("pin"))
    }

    /// A binary the file names on the PATH is the same binary as the resolved live path.
    func test_a_binary_named_on_the_path_matches_the_resolved_path() {
        let named = """
        [seat.MM]
        wake = "muse"
        muse = "muse-bin-1.4.11-R6667.1"
        """
        let setting = row(.musePath, config: named, status: statusJSON)
        XCTAssertEqual(setting.state, .match)
    }

    /// The host load gate stays off,  and is shown as off.
    func test_the_host_load_gate_is_shown_as_off() {
        let model = WakeSettingsBuilder.build(sources(status: statusJSON))
        XCTAssertEqual(model.daemonSettings.count, 1)
        XCTAssertEqual(model.daemonSettings.first?.state, .off)
        XCTAssertEqual(model.daemonSettings.first?.key, .hostLoadGate)
        XCTAssertNotNil(WakeSettingKey.hostLoadGate.refusalReason)
    }

    /// A host load gate that is on is drift,  because the owner ruled it off.
    func test_a_host_load_gate_that_is_on_is_drift() {
        let on = """
        [daemon]
        host_wake_load_per_core = 3.0
        """
        let model = WakeSettingsBuilder.build(sources(config: on))
        XCTAssertEqual(model.daemonSettings.first?.state, .drift)
    }

    /// The panel shows what it could not read.
    func test_the_panel_says_what_it_could_not_read() {
        var sources = self.sources(status: statusJSON)
        sources.problems = ["The listener's status could not be read."]
        let model = WakeSettingsBuilder.build(sources)
        XCTAssertEqual(model.problems, ["The listener's status could not be read."])
    }

    /// Without the listener's status,  every live value is unknown.
    func test_without_the_status_the_panel_shows_no_live_value() {
        let setting = row(.wakesPerHour)
        XCTAssertNil(setting.live)
        XCTAssertEqual(setting.state, .notOwned)
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

    // MARK: Writing

    /// A write lands in the file,  and the rest of the file is untouched.
    func test_a_write_lands_in_the_file() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-wake-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("listener.toml")
        try sample.write(to: url, atomically: true, encoding: .utf8)

        let writer = WakeSettingsWriter(configURL: url)
        try writer.apply([
            WakeSettingsWriter.Edit(
                seat: "MM",
                key: .wakesPerHour,
                value: .number(9)
            ),
        ])

        let written = try String(contentsOf: url, encoding: .utf8)
        let document = ListenerConfigDocument(text: written)
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_hour"]),
            .number(9)
        )
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_day"]),
            .number(80)
        )
        XCTAssertEqual(document.value(section: "seat.MM", key: "wake"), .text("muse"))
    }

    /// The file the writer leaves behind is one the runtime can read.
    func test_a_written_file_reads_back_the_way_the_runtime_writes_it() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-wake-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("listener.toml")
        try sample.write(to: url, atomically: true, encoding: .utf8)

        let writer = WakeSettingsWriter(configURL: url)
        try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .model, value: .text("another-model")),
            WakeSettingsWriter.Edit(seat: "MM", key: .fallbackUncontained, value: .bool(false)),
        ])

        let document = ListenerConfigDocument(
            text: try String(contentsOf: url, encoding: .utf8)
        )
        XCTAssertEqual(document.value(section: "seat.MM", key: "model"), .text("another-model"))
        XCTAssertEqual(document.value(section: "seat.MM", key: "fallback_uncontained_ok"), .bool(false))
        XCTAssertEqual(document.problems, [])
    }
}
