import XCTest

/// Kody on #1469:  "not tried yet" from a status that is not current, or a stopped
/// eavesdropper's, is not known now, and says why.
final class OperatorVoiceStaleNotTriedTests: XCTestCase {
    func test_a_stale_not_tried_yet_reads_as_not_known() throws {
        let json = #"{"running": false, "status": {"instance": "mac", "operator_voice": {"available": null, "code": null, "since": null}}}"#
        let status = try JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
        XCTAssertEqual(OperatorVoiceReading(status.status), .notTriedYet)
        let stopped = OperatorVoiceReading.current(status, runState: .stopped, statusIsCurrent: false)
        XCTAssertEqual(stopped.label, "Not known")
        XCTAssertNotEqual(stopped, .notTriedYet)
        XCTAssertTrue(stopped.sentence().hasPrefix("The eavesdropper is not running, and its last status had not tried the Operator yet"),
                      stopped.sentence())
        XCTAssertFalse(stopped.isProblem)
        // A current status still says Not tried yet.
        let current = OperatorVoiceReading.current(status, runState: .running, statusIsCurrent: true)
        XCTAssertEqual(current, .notTriedYet)
    }
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

    private func sources(status json: String?) -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        sources.config = ListenerConfigDocument(text: config)
        sources.configReadable = true
        sources.partition = partition
        sources.seats = ["MM", "CLAUDE", "OLD", "QUIET"]
        sources.status = json.map(status)
        return sources
    }

    private func seat(_ tag: String, in model: WakeSettingsModel) -> WakeSeat {
        guard let seat = model.seats.first(where: { $0.seat == tag }) else {
            fatalError("no seat \(tag)")
        }
        return seat
    }

    private func setting(_ key: WakeSettingKey, of seat: WakeSeat) -> WakeSetting {
        guard let setting = seat.settings.first(where: { $0.key == key }) else {
            fatalError("no \(key.rawValue)")
        }
        return setting
    }
    // MARK: Whether the eavesdropper is running

    /// A status that could not be read is a third state with its reason, never "not running".
    func test_a_failed_status_read_is_not_called_stopped() {
        var input = sources(status: nil)
        input.statusProblem = "The eavesdropper's status command failed:  boom"
        let model = WakeSettingsBuilder.build(input)
        XCTAssertEqual(model.runState, .unknown("The eavesdropper's status command failed:  boom"))
        XCTAssertFalse(model.running)
        XCTAssertNotEqual(model.runState.title, ListenerRunState.stopped.title)

        let stopped = WakeSettingsBuilder.build(sources(status: #"{"running": false, "status": {}}"#))
        XCTAssertEqual(stopped.runState, .stopped)
        let running = WakeSettingsBuilder.build(sources(status: #"{"running": true, "status": {}}"#))
        XCTAssertEqual(running.runState, .running)
    }

    /// A status that does not say whether the eavesdropper runs, or says it in another type, is not
    /// a stopped eavesdropper either, and an old value it holds is not called a stopped one's.
    func test_a_status_without_a_running_flag_is_unknown() {
        for json in [#"{"status": {}}"#, #"{"running": "yes", "status": {}}"#] {
            let model = WakeSettingsBuilder.build(sources(status: json))
            guard case .unknown(let why) = model.runState else {
                return XCTFail("expected unknown for \(json), got \(model.runState)")
            }
            XCTAssertTrue(why.contains("did not say whether it is running"))
            XCTAssertNotEqual(model.runState.title, ListenerRunState.stopped.title)
        }
        let json = #"{"status": {"seats": {"MM": {"wake": "muse", "budget": {"wakes_per_hour": 5}}}}}"#
        let row = setting(.wakesPerHour, of: seat("MM", in: WakeSettingsBuilder.build(sources(status: json))))
        XCTAssertEqual(row.state, .stale)
        XCTAssertFalse(row.note.contains("is not running"))
    }

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

    /// The routine's readiness and the seat's other newer fields are read.  The old fields an
    /// eavesdropper still publishes, such as the wake word, a model, a pin or a fallback, are not
    /// part of the model at all, so they cannot change what the panel says.
    func test_the_published_routine_is_read() throws {
        let json = """
        {"running": true, "status": {"seats": {"MM": {"wake": "openrouter", "model": "openai/gpt-5",
          "pin": {"pinned": false, "why": "pin_stale"},
          "fallback": {"runtime": "openrouter", "pinned": true, "stands_in": true},
          "routine": {"ready": false, "why": "no_target", "detail": "no routine host"},
          "route_enabled": true,
          "held_back": {"count": 2, "oldest_age": 840, "reasons": {"host_busy": 2}}}}}}
        """
        let decoded = try XCTUnwrap(status(json).seat("MM"))
        XCTAssertEqual(decoded.routine?.detail, "no routine host")
        XCTAssertEqual(decoded.routeEnabled, true)
        XCTAssertEqual(decoded.heldBack?.oldest_age, 840)

        let mm = seat("MM", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertEqual(mm.kind, .wakes)
        XCTAssertTrue(mm.routineNotReady)
        XCTAssertTrue(mm.needsAttention)
        XCTAssertEqual(mm.routineReadiness, WakeFact(text: "Routine not ready:  no routine host", emphasised: true))

        let ready = #"{"running": true, "status": {"seats": {"MM": {"routine": {"ready": true, "host": "routines.example"}}}}}"#
        let fine = seat("MM", in: WakeSettingsBuilder.build(sources(status: ready)))
        XCTAssertEqual(fine.routineReadiness, WakeFact(text: "Routine ready at routines.example", emphasised: false))
        XCTAssertFalse(fine.routineNotReady)

        // A seat that Wakes, reported with no routine at all, is said to be so.
        let none = #"{"running": true, "status": {"seats": {"MM": {"bot": "MM"}}}}"#
        let missing = seat("MM", in: WakeSettingsBuilder.build(sources(status: none)))
        XCTAssertTrue(missing.routineReadiness?.text.contains("reports no routine") == true)
        // A seat that Captures Only has no routine to report.
        XCTAssertNil(seat("QUIET", in: WakeSettingsBuilder.build(sources(status: ready))).routineReadiness)
    }

    // MARK: How old the status is

    /// An old status cannot make a pass or a drift:  those read NOT CURRENT, and say how old.
    func test_an_old_status_is_not_current() {
        let published = 1_792_657_970.0
        let json = """
        {"running": true, "status": {"updated": \(published), "seats": {"MM": {
          "budget": {"wakes_per_hour": 5, "wakes_per_day": 80}}}}}
        """
        var old = sources(status: json)
        old.readAt = Date(timeIntervalSince1970: published + 14 * 60)
        let model = WakeSettingsBuilder.build(old)
        XCTAssertFalse(model.statusIsCurrent)
        let row = setting(.wakesPerHour, of: seat("MM", in: model))
        XCTAssertEqual(row.state, .stale)
        XCTAssertTrue(row.state.isProblem)
        XCTAssertTrue(row.note.contains("14 minutes ago"))

        var fresh = sources(status: json)
        fresh.readAt = Date(timeIntervalSince1970: published + 5)
        let current = WakeSettingsBuilder.build(fresh)
        XCTAssertTrue(current.statusIsCurrent)
        XCTAssertEqual(setting(.wakesPerHour, of: seat("MM", in: current)).state, .match)
    }

    /// A stopped eavesdropper's file is its last word, not what anything uses now.  A routine that
    /// was not ready then is not called not ready now.
    func test_a_stopped_eavesdroppers_values_are_not_current() {
        let json = #"{"running": false, "status": {"seats": {"MM": {"budget": {"wakes_per_hour": 5}, "routine": {"ready": false, "why": "no_target"}}}}}"#
        let model = WakeSettingsBuilder.build(sources(status: json))
        let mm = seat("MM", in: model)
        let row = setting(.wakesPerHour, of: mm)
        XCTAssertEqual(row.state, .stale)
        XCTAssertTrue(row.note.contains("not running"))
        XCTAssertFalse(mm.routineNotReady)
        XCTAssertTrue(mm.routineReadiness?.text.hasSuffix("(not current)") == true)
    }

    /// A seat's connected flag is a last-known value under an old or stopped status, so it is not
    /// shown as connected, and it is not counted in the connected seats.
    func test_connected_is_not_claimed_from_an_old_status() {
        let body = #""seats": {"MM": {"connected": true}}"#
        let stopped = WakeSettingsBuilder.build(sources(status: #"{"running": false, "status": {\#(body)}}"#))
        XCTAssertNil(seat("MM", in: stopped).connected)
        XCTAssertEqual(stopped.reportingSeats, 0)
        XCTAssertEqual(stopped.connectedSeats, 0)

        let published = 1_792_657_970.0
        var old = sources(status: #"{"running": true, "status": {"updated": \#(published), \#(body)}}"#)
        old.readAt = Date(timeIntervalSince1970: published + 14 * 60)
        let aged = WakeSettingsBuilder.build(old)
        XCTAssertNil(seat("MM", in: aged).connected)
        XCTAssertEqual(aged.reportingSeats, 0)

        let running = WakeSettingsBuilder.build(sources(status: #"{"running": true, "status": {\#(body)}}"#))
        XCTAssertEqual(seat("MM", in: running).connected, true)
        XCTAssertEqual(running.reportingSeats, 1)
        XCTAssertEqual(running.connectedSeats, 1)
    }

    // MARK: Wakes or Captures Only

    /// A seat Wakes when its section holds a routine table, inline or under its own header.  With
    /// no routine, an old wake line other than inbox is one today's listener still wakes the seat
    /// by, so the seat is an Old Wake Setting, and only a seat with neither Captures Only.  What the
    /// eavesdropper publishes as the seat's wake decides nothing.
    func test_a_seat_wakes_by_its_routine_table_alone() {
        let json = #"{"running": true, "status": {"seats": {"OLD": {"wake": "openrouter"}, "QUIET": {"wake": "http"}}}}"#
        let model = WakeSettingsBuilder.build(sources(status: json))
        let mm = seat("MM", in: model)
        XCTAssertEqual(mm.kind, .wakes)
        XCTAssertEqual(mm.routineForm, .inline)
        XCTAssertEqual(mm.kind.title, "Wakes")
        let claude = seat("CLAUDE", in: model)
        XCTAssertEqual(claude.kind, .wakes)
        XCTAssertEqual(claude.routineForm, .subTable)
        XCTAssertEqual(setting(.routineURLEnv, of: claude).configured, .text("CLAUDE_ROUTINE_URL"))
        let old = seat("OLD", in: model)
        XCTAssertEqual(old.kind, .oldWakeSetting("openrouter"))
        XCTAssertEqual(old.kind.title, "Old Wake Setting")
        XCTAssertEqual(
            old.kind.help,
            "The listener still wakes this seat through its old wake = \"openrouter\" setting until the listener change lands.\u{00A0} Muster no longer edits it."
        )
        XCTAssertEqual(old.leftoverWake, "openrouter")
        let quiet = seat("QUIET", in: model)
        XCTAssertEqual(quiet.kind, .capturesOnly)
        XCTAssertEqual(quiet.leftoverWake, "inbox")
        XCTAssertFalse(quiet.needsAttention)
        XCTAssertEqual(mm.leftoverWake, "http")
        XCTAssertTrue(WakeSeatKind.wakes.help.contains("wakes the right chat"))
        XCTAssertTrue(WakeSeatKind.capturesOnly.help.contains("kept in the seat's inbox"))
    }

    /// With no routine, every old wake value but inbox is an Old Wake Setting:  `http` with no
    /// routine too, which the listener would refuse anyway.  `wake = "inbox"`, an empty one, or no
    /// wake line at all Captures Only.  A routine table decides over any wake line.
    func test_an_old_wake_line_with_no_routine_is_an_old_wake_setting() {
        for wake in ["claude", "muse", "openrouter", "http", "brand_new"] {
            let document = ListenerConfigDocument(text: "[seat.MM]\nbot = \"MM\"\nwake = \"\(wake)\"\n")
            XCTAssertEqual(document.routineForm(seat: "MM"), .absent, wake)
            XCTAssertEqual(document.leftoverWake(seat: "MM"), wake)
            let kind = WakeSettingsBuilder.kind(
                form: document.routineForm(seat: "MM"), configProblem: nil,
                leftoverWake: document.leftoverWake(seat: "MM")
            )
            XCTAssertEqual(kind, .oldWakeSetting(wake), wake)
            XCTAssertTrue(kind.isWoken, wake)
            XCTAssertFalse(kind.hasRoutine, wake)
        }
        for wake in ["inbox", "", nil] as [String?] {
            XCTAssertEqual(WakeSettingsBuilder.kind(form: .absent, configProblem: nil, leftoverWake: wake), .capturesOnly, wake ?? "no line")
        }
        XCTAssertEqual(WakeSettingsBuilder.kind(form: .inline, configProblem: nil, leftoverWake: "muse"), .wakes)
        // An empty inline table is still a routine table:  every key takes its default.
        let empty = ListenerConfigDocument(text: "[seat.MM]\nroutine = {}\n")
        XCTAssertEqual(empty.routineForm(seat: "MM"), .inline)
        XCTAssertEqual(empty.routineTable(seat: "MM"), [:])
    }

    /// Muster cannot say what a seat is when another eavesdropper holds it, when the Mac's file has
    /// no section for it, or when its section holds a line Muster cannot read, such as a dotted
    /// `routine.url_env` key:  each is Not Known, never Captures Only.
    func test_a_seat_muster_cannot_describe_is_not_known() {
        var input = sources(status: #"{"running": true, "status": {"seats": {}}}"#)
        input.config = ListenerConfigDocument(text: "[seat.MM]\nbot = \"MM\"\nrouting = 1\nroutine.url_env = \"MM_ROUTINE_URL\"\n")
        input.seats = ["MM", "QUIET", "MA"]
        let model = WakeSettingsBuilder.build(input)

        let dotted = seat("MM", in: model)
        guard case .notKnown(let why) = dotted.kind else {
            return XCTFail("a dotted routine key is not one Muster can read, got \(dotted.kind)")
        }
        XCTAssertTrue(why.contains("a line Muster can't read"), why)
        XCTAssertEqual(dotted.kind.title, "Not Known")

        let noSection = seat("QUIET", in: model)
        XCTAssertFalse(noSection.hasSection)
        guard case .notKnown(let none) = noSection.kind else {
            return XCTFail("a Mac seat with no section is not Captures Only, got \(noSection.kind)")
        }
        XCTAssertTrue(none.contains("no section"), none)

        let server = seat("MA", in: model)
        XCTAssertTrue(server.host.isElsewhere)
        guard case .notKnown(let elsewhere) = server.kind else {
            return XCTFail("a seat the server holds is not Captures Only, got \(server.kind)")
        }
        XCTAssertTrue(elsewhere.contains("Another eavesdropper holds this seat"), elsewhere)

        XCTAssertEqual(
            WakeSettingsBuilder.kind(form: .absent, configProblem: nil, host: .server),
            .notKnown("Another eavesdropper holds this seat, and Muster does not read its file, so whether it Wakes is not known here.")
        )
        XCTAssertEqual(WakeSettingsBuilder.kind(form: .inline, configProblem: nil, hasSection: false).title, "Not Known")
    }
    /// A routine Muster cannot read is not known, never Captures Only, and needs a look:  a
    /// routine line that is not a table, or both forms at once.  A file that was not read leaves
    /// every seat's kind not known.
    func test_a_routine_muster_cannot_read_is_not_known() {
        let line = ListenerConfigDocument(text: "[seat.MM]\nroutine = \"https://x\"\n")
        guard case .unreadable = line.routineForm(seat: "MM") else {
            return XCTFail("a routine line that is not a table is not one Muster can read")
        }
        let both = ListenerConfigDocument(text: "[seat.MM]\nroutine = { url_env = \"A\" }\n\n[seat.MM.routine]\nkey_env = \"B\"\n")
        guard case .unreadable(let why) = both.routineForm(seat: "MM") else {
            return XCTFail("both forms at once are not one Muster can read")
        }
        XCTAssertTrue(why.contains("TOML refuses"))
        XCTAssertNil(both.routineTable(seat: "MM"))

        var input = sources(status: #"{"running": true, "status": {"seats": {}}}"#)
        input.config = line
        input.seats = ["MM"]
        let mm = seat("MM", in: WakeSettingsBuilder.build(input))
        guard case .notKnown = mm.kind else { return XCTFail("expected a kind not known, got \(mm.kind)") }
        XCTAssertTrue(mm.needsAttention)

        guard case .notKnown(let unread) = WakeSettingsBuilder.kind(form: .inline, configProblem: "denied") else {
            return XCTFail("a file that was not read says nothing about the kind")
        }
        XCTAssertTrue(unread.contains("was not read"))
    }

    /// The routine and the caps apply to a seat that Wakes, the caps alone to one the listener
    /// still wakes through an old wake line, and none of them to one that Captures Only, whose
    /// file values are still shown when it holds them.
    func test_settings_apply_by_kind() {
        XCTAssertTrue(WakeSettingKey.wakesPerHour.applies(to: .oldWakeSetting("muse")))
        XCTAssertTrue(WakeSettingKey.liveLoopTurns.applies(to: .oldWakeSetting("claude")))
        XCTAssertFalse(WakeSettingKey.routineURLEnv.applies(to: .oldWakeSetting("muse")))
        XCTAssertFalse(WakeSettingKey.routineTimeout.applies(to: .oldWakeSetting("openrouter")))
        XCTAssertTrue(WakeSettingKey.routineURLEnv.applies(to: .wakes))
        XCTAssertTrue(WakeSettingKey.wakesPerHour.applies(to: .wakes))
        XCTAssertTrue(WakeSettingKey.liveLoopTurns.applies(to: .wakes))
        XCTAssertFalse(WakeSettingKey.routineURLEnv.applies(to: .capturesOnly))
        XCTAssertFalse(WakeSettingKey.wakesPerHour.applies(to: .capturesOnly))
        XCTAssertFalse(WakeSettingKey.wakesPerHour.applies(to: .notKnown("x")))
        XCTAssertTrue(WakeSettingKey.hostLoadGate.applies(to: .capturesOnly))
    }

    /// The method, the signing and the header are the router contract's, so they are shown and
    /// refused, and a value other than the contract's is named plainly.  A file that holds none
    /// takes the eavesdropper's defaults, which are the contract.
    @MainActor
    func test_the_router_contract_keys_are_shown_and_never_offered() {
        for key in [WakeSettingKey.routineMethod, .routineAuth, .routineHeader] {
            XCTAssertTrue(key.isFixedByContract, key.rawValue)
            XCTAssertNotNil(key.refusalReason, key.rawValue)
        }
        for key in [WakeSettingKey.routineURLEnv, .routineKeyEnv, .routineTimeout] {
            XCTAssertFalse(key.isFixedByContract, key.rawValue)
            XCTAssertNil(key.refusalReason, key.rawValue)
        }
        let model = WakeSettingsBuilder.build(sources(status: nil))
        XCTAssertTrue(seat("MM", in: model).contractDifferences.isEmpty)

        var input = sources(status: nil)
        input.config = ListenerConfigDocument(text: "[seat.MM]\nroutine = { url_env = \"A\", key_env = \"B\", auth = \"hmac-sha256\", method = \"post\" }\n")
        input.seats = ["MM"]
        let mm = seat("MM", in: WakeSettingsBuilder.build(input))
        let differing = mm.contractDifferences.map(\.key)
        // The method is upper-cased by the eavesdropper, so "post" is POST.  The header the file
        // leaves out follows the signing, so it differs too.
        XCTAssertEqual(Set(differing), [.routineAuth, .routineHeader])
        XCTAssertTrue(setting(.routineAuth, of: mm).contractDifference?.contains("Differs from the router contract") == true)
        XCTAssertTrue(mm.needsAttention)
        let reading = WakeSettingsStore.readDraft(seat: "MM", values: [.routineAuth: "bearer"], before: [.routineAuth: "hmac-sha256"])
        XCTAssertEqual(reading.edits, [])
        XCTAssertEqual(reading.refusals.count, 1)
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

    // MARK: Reading the files

    private func makeHome(config: String?) throws -> URL {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-truth-\(UUID().uuidString)", isDirectory: true)
        let root = home.appendingPathComponent(".simple-tools", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock {
            // A test makes a file unreadable;  give it back before removing it.
            if let walker = FileManager.default.enumerator(atPath: home.path) {
                for case let path as String in walker {
                    try? FileManager.default.setAttributes(
                        [.posixPermissions: 0o700],
                        ofItemAtPath: home.appendingPathComponent(path).path
                    )
                }
            }
            try? FileManager.default.removeItem(at: home)
        }
        if let config {
            try config.write(to: root.appendingPathComponent("listener.toml"), atomically: true, encoding: .utf8)
        }
        try "[seats]\nMM = \"mac\"\n".write(to: home.appendingPathComponent("partition.toml"), atomically: true, encoding: .utf8)
        return home
    }

    /// A stand-in for `simple-chat` that answers the status.
    private final class Fake: SimpleToolsRunning, @unchecked Sendable {
        private let lock = NSLock()
        private var calls: [[String]] = []
        var status = #"{"running": true, "status": {"seats": {}, "pause": {}}}"#
        var statusExit: Int32 = 0

        var commands: [String] {
            lock.lock()
            defer { lock.unlock() }
            return calls.map { $0.count > 1 ? $0[1] : "" }
        }

        func run(_ invocation: SimpleToolsInvocation) async throws -> SimpleToolsCLIResult {
            lock.lock()
            defer { lock.unlock() }
            calls.append(invocation.arguments)
            switch invocation.arguments.count > 1 ? invocation.arguments[1] : "" {
            case "status": return SimpleToolsCLIResult(exitCode: statusExit, stdout: status, stderr: "")
            default: return SimpleToolsCLIResult(exitCode: 0, stdout: "", stderr: "")
            }
        }
    }

    /// No configuration file still asks the eavesdropper, says the file is missing rather than
    /// unreadable, and leaves the load gate unknown rather than off.
    func test_a_missing_file_still_reads_the_status() async throws {
        let home = try makeHome(config: nil)
        let fake = Fake()
        let sources = await WakeSettingsReader(
            location: ListenerStateLocation(home: home), runner: fake, roster: ["MM"],
            partitionURL: home.appendingPathComponent("partition.toml")
        ).read()
        XCTAssertEqual(fake.commands, ["status"])
        XCTAssertNotNil(sources.status)
        XCTAssertTrue(sources.configProblem?.contains("No eavesdropper configuration file exists") == true)
        let model = WakeSettingsBuilder.build(sources)
        XCTAssertEqual(model.runState, .running)
        XCTAssertEqual(model.daemonSettings.first?.state, .unknown)
    }

    /// A ledger that is there and cannot be read is not zero use.
    func test_an_unreadable_ledger_is_not_zero_use() async throws {
        let home = try makeHome(config: "[seat.MM]\nbot = \"MM\"\nwake = \"muse\"\n")
        let location = ListenerStateLocation(home: home)
        let ledger = location.ledgerURL(seat: "MM")
        try FileManager.default.createDirectory(at: ledger.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "{\"ts\": 1.0, \"wake_id\": \"a\", \"state\": \"accepted\"}\n".write(to: ledger, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: ledger.path)
        if FileManager.default.isReadableFile(atPath: ledger.path) {
            throw XCTSkip("This user can read a file of mode 000, so the case cannot be made here.")
        }
        let sources = await WakeSettingsReader(
            location: location, runner: Fake(), roster: ["MM"],
            partitionURL: home.appendingPathComponent("partition.toml")
        ).read()
        XCTAssertNotNil(sources.usageProblems["MM"])
        XCTAssertNil(sources.usage["MM"])
        let mm = seat("MM", in: WakeSettingsBuilder.build(sources))
        XCTAssertNotNil(mm.usageProblem)
        XCTAssertTrue(mm.needsAttention)
    }

    /// A ledger too large to read whole is read from its end, from the first whole row.
    func test_a_large_file_is_read_from_its_end() throws {
        let home = try makeHome(config: nil)
        let url = home.appendingPathComponent("ledger.jsonl")
        try "first row\nsecond row\nthird\n".write(to: url, atomically: true, encoding: .utf8)
        let reader = WakeSettingsReader(location: ListenerStateLocation(home: home), runner: Fake(), roster: [])
        XCTAssertEqual(reader.readFile(url, limit: 12, tail: true), .text("third\n", truncated: true))
        guard case .unreadable = reader.readFile(url, limit: 12) else {
            return XCTFail("a whole read over the limit must not pretend the file is empty")
        }
        XCTAssertEqual(reader.readFile(home.appendingPathComponent("nothing"), limit: 12), .missing)
    }

    // MARK: The store

    @MainActor
    private func makeStore(_ fake: SimpleToolsRunning, home: URL) async -> WakeSettingsStore {
        let store = WakeSettingsStore(
            location: ListenerStateLocation(home: home),
            runner: fake,
            partitionURL: home.appendingPathComponent("partition.toml"),
            reloadPollInterval: 0,
            reloadPollAttempts: 1,
            auditURL: home.appendingPathComponent("audit/ops-audit.jsonl")
        )
        await store.refresh(roster: ["MM"])
        return store
    }

    private let mmConfig = "[seat.MM]\nbot = \"MM\"\nwake = \"http\"\nroutine = { url_env = \"MM_ROUTINE_URL\", key_env = \"MM_ROUTINE_KEY\" }\nbudget = { wakes_per_hour = 5 }\n"

    /// A read keeps an edit the owner has not applied, marks it when the file moved under it,
    /// and Revert puts the file's value back.
    @MainActor
    func test_a_refresh_keeps_unapplied_edits() async throws {
        let home = try makeHome(config: mmConfig)
        let store = await makeStore(Fake(), home: home)
        XCTAssertFalse(store.hasChanges(seat: "MM"))
        XCTAssertEqual(store.applySummary(seat: "MM"), "Nothing is edited, so nothing would be written.")

        store.draft["MM", default: [:]][.wakesPerHour] = "8"
        await store.refresh()
        XCTAssertEqual(store.draft["MM"]?[.wakesPerHour], "8")
        XCTAssertEqual(store.changedKeys(seat: "MM"), [.wakesPerHour])
        XCTAssertTrue(store.applySummary(seat: "MM").contains("Wakes per hour: 5 to 8"))
        XCTAssertNil(store.movedUnderneath["MM"])

        // Another writer changes the file under the edit.
        let file = ListenerStateLocation(home: home).configURL
        try mmConfig.replacingOccurrences(of: "wakes_per_hour = 5", with: "wakes_per_hour = 6")
            .write(to: file, atomically: true, encoding: .utf8)
        await store.refresh()
        XCTAssertEqual(store.draft["MM"]?[.wakesPerHour], "8")
        XCTAssertEqual(store.movedUnderneath["MM"], [.wakesPerHour])
        XCTAssertTrue(store.applySummary(seat: "MM").contains("Wakes per hour: 6 to 8"))

        store.revert(seat: "MM")
        XCTAssertEqual(store.draft["MM"]?[.wakesPerHour], "6")
        XCTAssertFalse(store.hasChanges(seat: "MM"))
        XCTAssertNil(store.movedUnderneath["MM"])
    }

    /// A change to the routine's address or key variable asks for the owner's Touch ID, and so
    /// does taking one out;  a cap or the timeout does not, and the signing is never offered.
    @MainActor
    func test_where_wakes_go_needs_the_owner() async throws {
        let home = try makeHome(config: mmConfig)
        let store = await makeStore(Fake(), home: home)
        store.draft["MM", default: [:]][.wakesPerHour] = "8"
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        XCTAssertFalse(store.needsOwnerCheck(seat: "MM"))
        XCTAssertFalse(store.applySummary(seat: "MM").contains("Touch ID"))
        store.draft["MM", default: [:]][.routineAuth] = "header"
        XCTAssertFalse(store.needsOwnerCheck(seat: "MM"), "the signing is refused, so it never reaches the owner check")
        store.revert(seat: "MM")
        store.draft["MM", default: [:]][.routineURLEnv] = "MM_OTHER_URL"
        XCTAssertTrue(store.needsOwnerCheck(seat: "MM"))
        XCTAssertTrue(store.applySummary(seat: "MM").contains("Touch ID"))
        store.revert(seat: "MM")
        store.draft["MM", default: [:]][.routineKeyEnv] = ""
        XCTAssertTrue(store.needsOwnerCheck(seat: "MM"))
        XCTAssertTrue(WakeSettingKey.routineURLEnv.needsOwnerCheck)
        XCTAssertTrue(WakeSettingKey.routineKeyEnv.needsOwnerCheck)
        XCTAssertEqual(WakeSettingKey.allCases.filter(\.needsOwnerCheck), [.routineURLEnv, .routineKeyEnv])
    }

    /// Nothing the panel does runs `daemon test-wake`:  an apply, a pause and a resume ask the
    /// eavesdropper for their own commands only.
    @MainActor
    func test_nothing_runs_a_test_wake() async throws {
        let home = try makeHome(config: mmConfig)
        let fake = Fake()
        let store = await makeStore(fake, home: home)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        await store.pause(seat: "MM")
        await store.resume(seat: "MM")
        XCTAssertFalse(fake.commands.contains("test-wake"), "\(fake.commands)")
        XCTAssertTrue(WakeSeatKind.testWakeWaits.contains("without waking it"))
    }

    /// A seat under a full pause is not given a wakes-only pause, which would loosen it.
    @MainActor
    func test_a_full_pause_is_not_loosened() async throws {
        let home = try makeHome(config: mmConfig)
        let fake = Fake()
        // A current status, so it decides the pause:  there is no pause file here, and one that is
        // not there is no pause.
        fake.status = #"{"running": true, "status": {"updated": \#(Date().timeIntervalSince1970), "seats": {"MM": {"wake": "muse", "paused": true, "wakes_paused": true}}, "pause": {"MM": {"by": "TEST_OWNER", "ts": 1792535804.0}}}}"#
        let store = await makeStore(fake, home: home)
        let mm = try XCTUnwrap(store.model.seats.first)
        XCTAssertEqual(mm.pauseBy, "TEST_OWNER")
        await store.pause(seat: "MM")
        XCTAssertFalse(fake.commands.contains("pause"))
        XCTAssertTrue(store.message?.contains("would loosen it") == true)
    }

    /// With the status unread, the pause file itself still stops a wakes-only pause over a full
    /// one, and a pause file that cannot be read stops the pause with its reason.  The panel shows
    /// the seat as paused, from the pause file, and never as unpaused.
    @MainActor
    func test_the_pause_file_guards_a_full_pause_when_the_status_is_not_read() async throws {
        let home = try makeHome(config: mmConfig)
        let pauseURL = ListenerStateLocation(home: home).pauseURL
        try FileManager.default.createDirectory(at: pauseURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try #"{"MM": {"wakes_only": false, "by": "owner-dm", "ts": 1792535804.0}}"#
            .write(to: pauseURL, atomically: true, encoding: .utf8)
        let fake = Fake()
        fake.statusExit = 1
        let store = await makeStore(fake, home: home)
        let shown = try XCTUnwrap(store.model.seats.first(where: { $0.seat == "MM" }))
        XCTAssertEqual(shown.paused, true)
        XCTAssertEqual(shown.wakesPaused, true)
        XCTAssertEqual(shown.pauseScope, "MM")
        XCTAssertEqual(shown.pauseBy, "owner-dm")
        XCTAssertEqual(shown.pauseSource, .pauseFile)
        await store.pause(seat: "MM")
        XCTAssertFalse(fake.commands.contains("pause"))
        XCTAssertTrue(store.message?.contains("would loosen it") == true)

        try "not json".write(to: pauseURL, atomically: true, encoding: .utf8)
        await store.pause(seat: "MM")
        XCTAssertFalse(fake.commands.contains("pause"))
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("pause file could not be read") == true)

        XCTAssertEqual(WakeSettingsStore.ownPauseEntry(seat: "MM", at: home.appendingPathComponent("nothing.json")), .noEntry)
        try #"{"*": {"wakes_only": false}, "MM": {"wakes_only": true}}"#.write(to: pauseURL, atomically: true, encoding: .utf8)
        XCTAssertEqual(WakeSettingsStore.ownPauseEntry(seat: "MM", at: pauseURL), .wakesOnly)
        XCTAssertEqual(WakeSettingsStore.ownPauseEntry(seat: "CLAUDE", at: pauseURL), .noEntry)
        try #"{"MM": {}}"#.write(to: pauseURL, atomically: true, encoding: .utf8)
        XCTAssertEqual(WakeSettingsStore.ownPauseEntry(seat: "mm", at: pauseURL), .full)
    }

    /// Resume reads a pause on every seat from the pause file, as Pause reads a seat's own entry,
    /// so an old status that does not show it cannot turn it into a lifted pause.
    @MainActor
    func test_resume_reads_the_every_seat_pause_from_the_file() async throws {
        let home = try makeHome(config: mmConfig)
        let partitionURL = home.appendingPathComponent("partition.toml")
        try "version = 1\n\n[seats]\nMM = \"mac\"\n".write(to: partitionURL, atomically: true, encoding: .utf8)
        let pauseURL = ListenerStateLocation(home: home).pauseURL
        try FileManager.default.createDirectory(at: pauseURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try #"{"*": {"wakes_only": true}}"#.write(to: pauseURL, atomically: true, encoding: .utf8)
        let fake = Fake()
        fake.statusExit = 1
        let store = await makeStore(fake, home: home)
        // The status was not read, so the panel reads the pause on every seat from the file too.
        XCTAssertEqual(store.model.seats.first(where: { $0.seat == "MM" })?.everySeatPaused, true)
        await store.resume(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("A pause on every seat is still in force.") == true)

        XCTAssertTrue(WakeSettingsStore.everySeatPauseHolds(at: pauseURL, statusSays: false))
        try #"{"MM": {"wakes_only": true}}"#.write(to: pauseURL, atomically: true, encoding: .utf8)
        XCTAssertFalse(WakeSettingsStore.everySeatPauseHolds(at: pauseURL, statusSays: true))
        try "not json".write(to: pauseURL, atomically: true, encoding: .utf8)
        XCTAssertTrue(WakeSettingsStore.everySeatPauseHolds(at: pauseURL, statusSays: true))
        XCTAssertFalse(WakeSettingsStore.everySeatPauseHolds(at: pauseURL, statusSays: false))
    }

    /// A field cleared over a setting the file never held is no edit:  the summary and the write
    /// agree with the Edited mark.
    @MainActor
    func test_a_cleared_field_over_nothing_is_no_edit() {
        let reading = WakeSettingsStore.readDraft(
            seat: "MM",
            values: [.routineURLEnv: "", .routineTimeout: " ", .usdPerDay: ""],
            before: [.routineURLEnv: WakeSetting.unsetText, .routineTimeout: WakeSetting.unsetText, .usdPerDay: WakeSetting.unsetText]
        )
        XCTAssertEqual(reading, WakeSettingsStore.DraftReading())
    }

    /// A seat's settings follow the owner's own choice once made, so typing an edit, or the seat
    /// starting to need attention, never folds them away;  before a choice they follow the seat.
    func test_a_seat_the_owner_opened_stays_open() {
        let json = #"{"running": true, "status": {"seats": {"QUIET": {"wake": "inbox"}}}}"#
        let quiet = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertFalse(quiet.needsAttention)
        XCTAssertFalse(quiet.settingsOpen(choice: nil, hasEdits: false))
        XCTAssertTrue(quiet.settingsOpen(choice: nil, hasEdits: true))
        XCTAssertTrue(quiet.settingsOpen(choice: true, hasEdits: false))
        XCTAssertTrue(quiet.settingsOpen(choice: true, hasEdits: true))
        XCTAssertFalse(quiet.settingsOpen(choice: false, hasEdits: true))
    }

    // MARK: The first review round

    /// A configuration file that was not read is never DEFAULT:  each row is not known, and the
    /// eavesdropper's own value is still shown.
    func test_an_unread_file_is_not_the_default() {
        var unread = sources(status: #"{"running": true, "status": {"seats": {"MM": {"wake": "muse", "budget": {"wakes_per_hour": 5}}}}}"#)
        unread.config = ListenerConfigDocument(text: "")
        unread.configReadable = false
        unread.configProblem = "The configuration file at /tmp/x could not be read:  denied"
        let row = setting(.wakesPerHour, of: seat("MM", in: WakeSettingsBuilder.build(unread)))
        XCTAssertEqual(row.state, .unknown)
        XCTAssertNil(row.configured)
        XCTAssertEqual(row.live, .number(5))
        XCTAssertTrue(row.note.contains("was not read"))
    }

    /// An unchanged ledger is not decoded again on the next read, and a changed one is.  The
    /// counts are worked out for each read's own time either way.
    func test_an_unchanged_ledger_is_not_parsed_again() throws {
        let home = try makeHome(config: nil)
        let url = home.appendingPathComponent("ledger.jsonl")
        let now = Date()
        let first = "{\"ts\": \(now.timeIntervalSince1970 - 60), \"wake_id\": \"a\", \"state\": \"accepted\", \"owner\": false}\n"
        try first.write(to: url, atomically: true, encoding: .utf8)
        let cache = LedgerParseCache()
        let reader = WakeSettingsReader(location: ListenerStateLocation(home: home), runner: Fake(), roster: [], ledgerCache: cache)
        guard case .parsed(let parsed, _) = reader.readLedger(url, at: now) else { return XCTFail("expected rows") }
        XCTAssertEqual(WakeLedger.usage(from: parsed, now: now).wakesThisHour, 1)
        guard case .parsed = reader.readLedger(url, at: now.addingTimeInterval(60)) else { return XCTFail("expected rows") }
        XCTAssertEqual(cache.hits, 1)

        let second = first + "{\"ts\": \(now.timeIntervalSince1970 - 30), \"wake_id\": \"b\", \"state\": \"accepted\", \"owner\": false}\n"
        try second.write(to: url, atomically: true, encoding: .utf8)
        guard case .parsed(let again, _) = reader.readLedger(url, at: now.addingTimeInterval(61)) else { return XCTFail("expected rows") }
        XCTAssertEqual(cache.hits, 1)
        XCTAssertEqual(WakeLedger.usage(from: again, now: now).wakesThisHour, 2)

        // Past the cache's age the ledger is parsed again even when it has not changed.
        guard case .parsed = reader.readLedger(url, at: now.addingTimeInterval(61 + LedgerParseCache.maximumAge)) else {
            return XCTFail("expected rows")
        }
        XCTAssertEqual(cache.hits, 1)
    }

    /// Every way out of an action writes one audit row, a refusal before anything ran included.
    @MainActor
    func test_a_refused_action_still_writes_its_audit_row() async throws {
        let home = try makeHome(config: mmConfig)
        let store = await makeStore(Fake(), home: home)
        await store.apply(seat: "MM")
        await store.resume(seat: "ELSEWHERE")
        let audit = OpsAuditLog.read(at: home.appendingPathComponent("audit/ops-audit.jsonl"))
        XCTAssertEqual(audit.entries.map(\.verb), ["wake-resume", "wake-settings-apply"])
        XCTAssertEqual(audit.entries.map(\.result), ["refused", "refused"])
    }

    // MARK: Small pieces

    /// A run that failed with nothing on stderr shows the end of stdout instead of nothing.
    func test_a_failure_with_an_empty_stderr_shows_stdout() {
        let result = SimpleToolsCLIResult(exitCode: 1, stdout: "line one\nFAIL: why\n", stderr: "")
        XCTAssertEqual(result.failureTail, "line one\nFAIL: why")
        let both = SimpleToolsCLIResult(exitCode: 1, stdout: "out", stderr: "err")
        XCTAssertEqual(both.failureTail, "err")
    }

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

    /// Saturday, November 7, 2026, at noon on Jay's clock.
    private let auditNow = FleetTime.parse("2026-11-07T18:00:00Z")!

    /// F2.  A stopped eavesdropper's held-back count, red warnings, chat nudges and quota are its
    /// last word:  each says when it was published and that it is not current, and none says
    /// "now".  The file turning a seat off is believed only from a running eavesdropper.
    func test_a_stopped_eavesdroppers_seat_facts_are_not_called_current() throws {
        let published = auditNow.timeIntervalSince1970 - 30
        let body = """
        "updated": \(published), "disabled": ["QUIET"],
        "seats": {"MM": {"wake": "muse", "red": ["down for 5 minutes"],
          "held_back": {"count": 3, "totals": {"held": 9, "surfaced": 6}},
          "chat_nudges": {"pending": 1},
          "quota": {"known": true, "exhausted": true, "source": "codecaps"}}}
        """
        var stopped = sources(status: "{\"running\": false, \"status\": {\(body)}}")
        stopped.readAt = auditNow
        let model = WakeSettingsBuilder.build(stopped)
        let mm = seat("MM", in: model)
        XCTAssertFalse(mm.liveIsCurrent)
        XCTAssertEqual(mm.liveAsOf, Date(timeIntervalSince1970: published))
        let facts = mm.liveFacts(now: auditNow)
        XCTAssertFalse(facts.contains { $0.text.contains(" now") }, "\(facts)")
        let asOf = "as of \(FleetTime.when(Date(timeIntervalSince1970: published), now: auditNow)), not current"
        for prefix in ["held for later:", "held since start:", "chat nudges:", "quota exhausted"] {
            let fact = try XCTUnwrap(facts.first { $0.text.hasPrefix(prefix) }, prefix)
            XCTAssertTrue(fact.text.hasSuffix(asOf), fact.text)
        }
        XCTAssertEqual(mm.notCurrentSuffix(now: auditNow), ", " + asOf)
        XCTAssertFalse(seat("QUIET", in: model).disabledInFile)

        var running = sources(status: "{\"running\": true, \"status\": {\(body)}}")
        running.readAt = auditNow
        let current = WakeSettingsBuilder.build(running)
        let live = seat("MM", in: current)
        XCTAssertTrue(live.liveIsCurrent)
        XCTAssertEqual(live.notCurrentSuffix(now: auditNow), "")
        XCTAssertTrue(live.liveFacts(now: auditNow).contains { $0.text.hasPrefix("held for later now:  3") })
        XCTAssertTrue(seat("QUIET", in: current).disabledInFile)
    }

    /// F3.  When the status is not current or not read, the pause comes from the pause file, by
    /// the eavesdropper's rule.  A pause file that cannot be read leaves the pause not known, and
    /// never unpaused.  A pause file that is not there is no pause.  Only when the file was not
    /// read is the last status shown, marked not current.
    func test_the_pause_file_decides_the_pause_when_the_status_is_not_current() throws {
        let pausedAt = auditNow.timeIntervalSince1970 - 3 * 86_400
        var unread = sources(status: nil)
        unread.pauseFile = .entries(["MM": ListenerStatus.PauseEntry(wakes_only: false, by: "owner-dm", ts: pausedAt)])
        let mm = seat("MM", in: WakeSettingsBuilder.build(unread))
        XCTAssertTrue(mm.paused)
        XCTAssertTrue(mm.wakesPaused)
        XCTAssertEqual(mm.pauseScope, "MM")
        XCTAssertEqual(mm.pauseBy, "owner-dm")
        XCTAssertEqual(mm.pauseSource, .pauseFile)
        XCTAssertEqual(mm.pauseText(now: auditNow), "Paused by owner-dm at Wed, Nov 4, 12:00 PM, from the pause file")
        XCTAssertFalse(seat("CLAUDE", in: WakeSettingsBuilder.build(unread)).paused)

        var unreadable = sources(status: nil)
        unreadable.pauseFile = .unreadable("The pause file at /x could not be read:  denied")
        let notKnownModel = WakeSettingsBuilder.build(unreadable)
        let notKnown = seat("MM", in: notKnownModel)
        XCTAssertFalse(notKnown.paused)
        XCTAssertEqual(notKnown.pauseSource.notKnownReason, "The pause file at /x could not be read:  denied")
        XCTAssertTrue(notKnown.pauseText()?.hasPrefix("Pause not known") == true)
        XCTAssertTrue(notKnown.needsAttention)
        XCTAssertTrue(notKnownModel.problems.contains { $0.hasPrefix("Whether a pause is in force is not known") })

        // An old status's word gives way to the file.
        let stale = #"{"running": false, "status": {"pause": {}, "seats": {"MM": {"wake": "muse", "paused": false}}}}"#
        var old = sources(status: stale)
        old.pauseFile = .entries(["*": ListenerStatus.PauseEntry(wakes_only: false, by: "owner", ts: pausedAt)])
        let fromFile = seat("MM", in: WakeSettingsBuilder.build(old))
        XCTAssertTrue(fromFile.paused)
        XCTAssertEqual(fromFile.pauseScope, "*")
        XCTAssertTrue(fromFile.everySeatPaused)

        // No pause file is no pause, as the eavesdropper reads it, whatever an old status lists:
        // the file was read, and it decides.  Nothing is offered to resume.
        let lastWord = #"{"running": false, "status": {"updated": \#(auditNow.timeIntervalSince1970 - 60), "pause": {"MM": {"wakes_only": true, "by": "cli"}}, "seats": {"MM": {"wake": "muse", "wakes_paused": true}}}}"#
        var missing = sources(status: lastWord)
        missing.pauseFile = .missing
        let lifted = seat("MM", in: WakeSettingsBuilder.build(missing))
        XCTAssertEqual(lifted.pauseSource, .pauseFile)
        XCTAssertFalse(lifted.paused)
        XCTAssertFalse(lifted.wakesPaused)
        XCTAssertNil(lifted.pauseScope)
        XCTAssertNil(lifted.pauseText(now: auditNow))

        // A pause file that was not read leaves the last status, marked as such.
        let last = seat("MM", in: WakeSettingsBuilder.build(sources(status: lastWord)))
        XCTAssertEqual(last.pauseSource, .lastStatus)
        XCTAssertTrue(last.wakesPaused)
        XCTAssertTrue(last.pauseText(now: auditNow)?.hasSuffix("not current") == true)

        // A current status decides, whatever the file holds.
        var current = sources(status: #"{"running": true, "status": {"pause": {}, "seats": {"MM": {"wake": "muse", "paused": false}}}}"#)
        current.pauseFile = .entries(["MM": ListenerStatus.PauseEntry(wakes_only: false, by: "x", ts: pausedAt)])
        let live = seat("MM", in: WakeSettingsBuilder.build(current))
        XCTAssertFalse(live.paused)
        XCTAssertEqual(live.pauseSource, .currentStatus)
        XCTAssertNil(live.pauseText())
    }

    /// F3.  The reader reads the pause file:  its entries, a file that is not there, and one
    /// that cannot be read.
    func test_the_reader_reads_the_pause_file() async throws {
        let home = try makeHome(config: mmConfig)
        let location = ListenerStateLocation(home: home)
        let reader = WakeSettingsReader(
            location: location, runner: Fake(), roster: ["MM"],
            partitionURL: home.appendingPathComponent("partition.toml")
        )
        XCTAssertEqual(reader.readPauseFile(), .missing)
        try FileManager.default.createDirectory(at: location.pauseURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try #"{"MM": {"wakes_only": true, "by": "cli", "ts": 1792535804}, "*": "not an entry"}"#
            .write(to: location.pauseURL, atomically: true, encoding: .utf8)
        XCTAssertEqual(reader.readPauseFile(), .entries([
            "MM": ListenerStatus.PauseEntry(wakes_only: true, by: "cli", ts: 1_792_535_804),
        ]))
        let sources = await reader.read()
        XCTAssertEqual(sources.pauseFile, reader.readPauseFile())
        try "not json".write(to: location.pauseURL, atomically: true, encoding: .utf8)
        guard case .unreadable(let why) = reader.readPauseFile() else {
            return XCTFail("a pause file that is not JSON is not no pause")
        }
        XCTAssertTrue(why.contains("not a JSON object"))
    }

    /// F4.  A configuration file that was not read says nothing about its Operator section, and
    /// a lower-case `[seat.operator]` section is the Operator's, as it is to the eavesdropper.
    func test_the_operator_section_is_not_known_when_the_file_was_not_read() async throws {
        let unread = OperatorOptions.build(from: ListenerConfigDocument(text: ""), configProblem: "It could not be read.")
        XCTAssertFalse(unread.isKnown)
        XCTAssertEqual(unread.notRead, "It could not be read.")
        XCTAssertTrue(unread.summary.contains("was not read"))
        XCTAssertFalse(unread.summary.contains("has no [seat.OPERATOR] section"))
        XCTAssertTrue(unread.summary.contains("\u{00A0} "))

        let lower = OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.operator]\nbot = \"X\"\ncreds = \"file\"\nwake = \"inbox\"\n"))
        XCTAssertTrue(lower.isKnown)
        XCTAssertTrue(lower.isConfigured)
        XCTAssertEqual(lower.bot, "X")
        XCTAssertEqual(lower.brokenRules.count, 2)
        XCTAssertNotNil(lower.problem)
        // A table inside another seat is not the Operator's section.
        XCTAssertFalse(OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.operator.budget]\nx = 1\n")).isConfigured)

        // A file that is not there is known to hold no Operator section, so the reader says so.
        let home = try makeHome(config: nil)
        let reader = WakeSettingsReader(
            location: ListenerStateLocation(home: home), runner: Fake(), roster: ["MM"],
            partitionURL: home.appendingPathComponent("partition.toml")
        )
        let missing = await reader.read()
        XCTAssertNotNil(missing.configProblem)
        XCTAssertTrue(missing.operatorOptions.isKnown)
        XCTAssertFalse(missing.operatorOptions.isConfigured)
        XCTAssertTrue(WakeSettingsBuilder.build(missing).operatorOptions.summary.contains("has no [seat.OPERATOR] section"))

        // A file that is there and cannot be read leaves the section not known, with why.
        let configURL = ListenerStateLocation(home: home).configURL
        try "[seat.MM]\nwake = \"muse\"\n".write(to: configURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: configURL.path)
        if FileManager.default.isReadableFile(atPath: configURL.path) {
            throw XCTSkip("This user can read a file of mode 000, so the case cannot be made here.")
        }
        let unreadable = await reader.read()
        XCTAssertFalse(unreadable.operatorOptions.isKnown)
        XCTAssertEqual(unreadable.operatorOptions.notRead, unreadable.configProblem)
        XCTAssertFalse(WakeSettingsBuilder.build(unreadable).operatorOptions.summary.contains("has no [seat.OPERATOR] section"))
    }

    /// F5.  Held-back wakes lost past both caps and still owed are read, shown as a problem, and
    /// put the seat under Problems Only, with the overflow and the lifetime counts.
    func test_held_back_wakes_lost_past_the_caps_are_shown() {
        let json = """
        {"running": true, "status": {"seats": {"QUIET": {"wake": "inbox",
          "held_back": {"count": 4, "overflow": 1, "evicted": 2,
            "totals": {"held": 20, "surfaced": 14, "expired": 3, "evicted": 5}}}}}}
        """
        let quiet = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertEqual(quiet.heldBackEvicted, 2)
        XCTAssertEqual(quiet.heldBackOverflow, 1)
        XCTAssertEqual(quiet.heldBackExpiredTotal, 3)
        XCTAssertEqual(quiet.heldBackEvictedTotal, 5)
        XCTAssertTrue(quiet.needsAttention)
        let facts = quiet.liveFacts(now: auditNow)
        XCTAssertTrue(facts.contains(WakeFact(text: "2 held wakes lost past the caps and still owed", emphasised: true)), "\(facts)")
        XCTAssertTrue(facts.contains { $0.text.contains("1 of them in the overflow") })
        XCTAssertTrue(facts.contains { $0.text.contains("3 expired") && $0.text.contains("5 lost past the caps") })
        XCTAssertEqual(quiet.heldBackSummary, ["4 held for later", "2 held wakes lost and still owed"])

        // A stopped eavesdropper's counts are its last word, and the summary line says so for
        // the lost wakes as for the held ones.
        let stopped = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json.replacingOccurrences(of: "\"running\": true", with: "\"running\": false"))))
        XCTAssertFalse(stopped.liveIsCurrent)
        XCTAssertEqual(stopped.heldBackSummary, ["4 held for later (not current)", "2 held wakes lost and still owed (not current)"])

        let none = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: #"{"running": true, "status": {"seats": {"QUIET": {"wake": "inbox", "held_back": {"count": 0, "evicted": 0}}}}}"#)))
        XCTAssertFalse(none.needsAttention)
    }

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

    /// F7.  A live session whose rewake watcher is gone says so, a watcher the eavesdropper did not
    /// report is said to be not said, and whether resume-wake is verified on the Mac is read from
    /// both panels' sources.
    func test_rewake_state_is_shown() throws {
        func session(_ json: String) throws -> SeatSession {
            SeatSession(lease: try JSONDecoder().decode(ListenerStateDocument.Lease.self, from: Data(json.utf8)), index: 0)
        }
        let gone = try session(#"{"letter": "B", "alive": true, "watcher": false}"#)
        XCTAssertEqual(gone.watcher, false)
        XCTAssertTrue(gone.line.contains("no rewake watcher"))
        XCTAssertTrue(try session(#"{"letter": "B", "alive": true}"#).line.contains("rewake watcher not said"))
        XCTAssertFalse(try session(#"{"letter": "B", "alive": true, "watcher": true}"#).line.contains("rewake"))
        // An ended session is not woken again either way, so its watcher is not named.
        XCTAssertFalse(try session(#"{"letter": "B", "alive": false, "watcher": false}"#).line.contains("rewake"))

        let document = try JSONDecoder().decode(
            ListenerStateDocument.self,
            from: Data(#"{"updated": 1.0, "rewake_verified_cli": true, "rewake_verified_desktop": false}"#.utf8)
        )
        XCTAssertEqual(document.rewake, RewakeVerification(terminal: true, desktop: false))
        XCTAssertTrue(document.rewake.isProblem)
        XCTAssertEqual(document.rewake.sentence, "Resume-wake on this Mac:  terminal sessions verified, Desktop sessions not verified.")
        var activity = SeatActivityBuilder.Sources()
        activity.listener = document
        XCTAssertEqual(SeatActivityBuilder.build(activity).rewake, document.rewake)
        XCTAssertNil(SeatActivityBuilder.build(SeatActivityBuilder.Sources()).rewake)

        let legacy = try JSONDecoder().decode(ListenerStateDocument.self, from: Data(#"{"rewake_verified": true}"#.utf8))
        XCTAssertEqual(legacy.rewake, RewakeVerification(terminal: true, desktop: true))
        let silent = try JSONDecoder().decode(ListenerStateDocument.self, from: Data("{}".utf8))
        XCTAssertTrue(silent.rewake.sentence.contains("does not say"))
        XCTAssertFalse(silent.rewake.isProblem)

        let wake = WakeSettingsBuilder.build(sources(status: #"{"running": true, "status": {"rewake_verified_cli": false, "rewake_verified_desktop": true, "seats": {}}}"#))
        XCTAssertEqual(wake.rewake, RewakeVerification(terminal: false, desktop: true))
        XCTAssertNil(WakeSettingsBuilder.build(sources(status: nil)).rewake)
        // A stopped eavesdropper resumes nothing, and an old status cannot say what resumes a
        // session now, so neither gives the Wake panel a resume-wake line.
        let stopped = WakeSettingsBuilder.build(sources(status: #"{"running": false, "status": {"rewake_verified_cli": true, "rewake_verified_desktop": true, "seats": {}}}"#))
        XCTAssertNil(stopped.rewake)
        var old = sources(status: #"{"running": true, "status": {"updated": \#(auditNow.timeIntervalSince1970 - 600), "rewake_verified_cli": true, "seats": {}}}"#)
        old.readAt = auditNow
        XCTAssertNil(WakeSettingsBuilder.build(old).rewake)
    }

    /// F8.  With a full pause on every seat and a wakes-only entry for the seat, the panel names
    /// the pause that has the effect, and Seat Activity's rule picks the same one.
    func test_the_pause_named_is_the_one_that_has_the_effect() {
        let json = """
        {"running": true, "status": {"pause": {"*": {"wakes_only": false, "by": "owner", "ts": 1792535804.0},
          "MM": {"wakes_only": true, "by": "cli", "ts": 1792539404.0}},
          "seats": {"MM": {"wake": "muse", "paused": true, "wakes_paused": true}}}}
        """
        let mm = seat("MM", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertEqual(mm.pauseScope, "*")
        XCTAssertEqual(mm.pauseBy, "owner")
        XCTAssertEqual(mm.pauseAt, Date(timeIntervalSince1970: 1_792_535_804))
        XCTAssertTrue(mm.pauseText()?.contains("by the pause on every seat by owner") == true)

        let entries: [String: ListenerStatus.PauseEntry] = [
            "*": ListenerStatus.PauseEntry(wakes_only: true, by: "a", ts: nil),
            "MM": ListenerStatus.PauseEntry(wakes_only: nil, by: "b", ts: nil),
        ]
        let covering = SeatActivityBuilder.coveringPause(for: "MM", in: entries, wakesOnly: { $0.wakes_only })
        XCTAssertEqual(covering?.scope, "MM")
        XCTAssertEqual(covering?.kind, .full)
        var file = sources(status: nil)
        file.pauseFile = .entries(entries)
        XCTAssertEqual(seat("MM", in: WakeSettingsBuilder.build(file)).pauseScope, "MM")
        XCTAssertNil(SeatActivityBuilder.coveringPause(for: "CLAUDE", in: ["MM": entries["MM"]!], wakesOnly: { $0.wakes_only }))
    }

    /// F9.  A status another instance wrote is not current here, so its values are NOT CURRENT
    /// with a reason that names the instance, and no seat is called connected from it.
    func test_a_status_from_another_instance_is_not_current() {
        let json = #"{"running": true, "status": {"instance": "server", "disabled": ["QUIET"], "seats": {"MM": {"wake": "muse", "connected": true, "budget": {"wakes_per_hour": 5}}}}}"#
        var input = sources(status: json)
        input.readAt = nil
        let model = WakeSettingsBuilder.build(input)
        XCTAssertFalse(model.statusIsCurrent)
        let mm = seat("MM", in: model)
        XCTAssertNil(mm.connected)
        XCTAssertEqual(model.reportingSeats, 0)
        XCTAssertFalse(mm.liveIsCurrent)
        let row = setting(.wakesPerHour, of: mm)
        XCTAssertEqual(row.state, .stale)
        XCTAssertTrue(row.note.contains("server instance"), row.note)
        XCTAssertNil(model.rewake)
        // Its list of seats turned off is about its own file, not the Mac's.
        let quiet = seat("QUIET", in: model)
        XCTAssertFalse(quiet.disabledInFile)
        XCTAssertFalse(quiet.settings.contains { $0.state == .seatOff })
        // The header names the instance as the reason, not the status's age.
        XCTAssertEqual(model.foreignInstance, "server")
        XCTAssertTrue(model.notCurrentReason?.contains("server instance") == true, model.notCurrentReason ?? "")
        XCTAssertFalse(model.notCurrentReason?.contains("every few seconds") == true)

        // The Mac's own status, running and old, keeps the age as its reason.
        var old = sources(status: #"{"running": true, "status": {"updated": \#(auditNow.timeIntervalSince1970 - 600), "seats": {}}}"#)
        old.readAt = auditNow
        let oldModel = WakeSettingsBuilder.build(old)
        XCTAssertNil(oldModel.foreignInstance)
        XCTAssertTrue(oldModel.notCurrentReason?.contains("every few seconds") == true)
        XCTAssertNil(WakeSettingsBuilder.build(sources(status: #"{"running": true, "status": {"seats": {}}}"#)).notCurrentReason)
    }

    /// F10.  A time that is not today on Jay's clock carries its weekday and date, and its year
    /// when that differs, so a pause set days ago or a weekly reset never reads as today.
    func test_a_time_that_is_not_today_carries_its_date() throws {
        XCTAssertEqual(FleetTime.when(try XCTUnwrap(FleetTime.parse("2026-11-07T21:00:00Z")), now: auditNow), "3:00 PM")
        // 3:00 AM UTC on the 8th is still the 7th in Chicago.
        XCTAssertEqual(FleetTime.when(try XCTUnwrap(FleetTime.parse("2026-11-08T03:00:00Z")), now: auditNow), "9:00 PM")
        XCTAssertEqual(FleetTime.when(try XCTUnwrap(FleetTime.parse("2026-11-09T21:00:00Z")), now: auditNow), "Mon, Nov 9, 3:00 PM")
        XCTAssertEqual(FleetTime.when(try XCTUnwrap(FleetTime.parse("2027-01-01T15:00:00Z")), now: auditNow), "Fri, Jan 1, 2027, 9:00 AM")

        let json = #"{"running": true, "status": {"seats": {"QUIET": {"wake": "inbox", "quota": {"known": true, "exhausted": false, "remaining": 40, "reset_at": "2026-11-09T21:00:00Z"}}}}}"#
        let quiet = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertTrue(quiet.liveFacts(now: auditNow).contains { $0.text.contains("resets Mon, Nov 9, 3:00 PM") })
    }

    /// F12.  A file with no host load gate shows nothing in the file's column, and says the
    /// eavesdropper's default, off, is in effect.  A file that holds 0 shows it.
    func test_the_host_load_gate_default_is_not_shown_as_the_files() throws {
        var input = sources(status: #"{"running": true, "status": {"seats": {}}}"#)
        input.config = ListenerConfigDocument(text: "[seat.MM]\nwake = \"muse\"\n")
        let gate = try XCTUnwrap(WakeSettingsBuilder.build(input).daemonSettings.first)
        XCTAssertNil(gate.configured)
        XCTAssertEqual(gate.configuredText, WakeSetting.unsetText)
        XCTAssertEqual(gate.state, .off)
        XCTAssertTrue(gate.note.contains("holds nothing"), gate.note)

        let held = try XCTUnwrap(WakeSettingsBuilder.build(sources(status: nil)).daemonSettings.first)
        XCTAssertNotNil(held.configured)
        XCTAssertEqual(held.state, .off)
        XCTAssertFalse(held.note.contains("holds nothing"))
    }
}
