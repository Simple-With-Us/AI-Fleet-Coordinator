import XCTest

/// The wake panel tells the truth:  a status it could not read is not a stopped eavesdropper, an old
/// status is not current, each seat's model and pin are judged by its own runtime, a ledger it could
/// not read is not zero use, edits survive a read, and a test wake's answer is kept for the owner
/// to read before pinning.
final class WakePanelTruthTests: XCTestCase {
    // MARK: Fixtures

    private let config = """
    [daemon]
    host_wake_load_per_core = 0

    [seat.MM]
    bot = "MM"
    wake = "muse"
    model = "muse-spark-1.3-contributor"
    fallback = "mcode"
    budget = { wakes_per_hour = 5, wakes_per_day = 80 }

    [seat.CLAUDE]
    bot = "CLAUDE"
    wake = "claude"
    model = "sonnet"
    fallback = "mcode"

    [seat.ROUTER]
    bot = "ROUTER"
    wake = "openrouter"
    model = "openai/gpt-5"
    fallback = "openrouter"
    fallback_model = "anthropic/claude-sonnet-4"

    [seat.QUIET]
    bot = "QUIET"
    wake = "inbox"
    """

    private let partition = ListenerPartition(seats: [
        "MM": "mac", "CLAUDE": "mac", "ROUTER": "mac", "QUIET": "mac", "MA": "server",
    ])

    private func status(_ json: String) -> ListenerStatus {
        try! JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
    }

    private func sources(status json: String?, pins: [String: [String: PinEvidence]] = [:]) -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        sources.config = ListenerConfigDocument(text: config)
        sources.configReadable = true
        sources.partition = partition
        sources.seats = ["MM", "CLAUDE", "ROUTER", "QUIET"]
        sources.status = json.map(status)
        sources.pins = pins
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

    private func pin(_ runtime: String, binary: String? = "/bin/x", model: String? = nil) -> PinEvidence {
        PinEvidence(
            runtime: runtime, binary: binary, model: model,
            verifiedAt: Date(timeIntervalSince1970: 1_792_535_804), pinned: true, why: nil
        )
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
        XCTAssertEqual(decoded.seat("MM")?.wake, "muse")
        XCTAssertNil(decoded.seat("MM")?.wakes24h)
        XCTAssertEqual(decoded.seat("MM")?.connected, true)
        XCTAssertEqual(decoded.seat("MM")?.budget?["wakes_per_hour"], .number(5))
    }

    /// The newer fields the eavesdropper publishes are read:  the model, the fallback, the routine
    /// and the pin's own verdict.
    func test_the_published_model_fallback_routine_and_pin_are_read() throws {
        let json = """
        {"running": true, "status": {"seats": {"ROUTER": {"wake": "openrouter", "model": "openai/gpt-5",
          "pin": {"pinned": false, "why": "pin_stale", "pinned_model": "openai/gpt-4", "model": "openai/gpt-5"},
          "fallback": {"runtime": "openrouter", "model": "anthropic/claude-sonnet-4", "pinned": true, "ready": true},
          "routine": {"ready": false, "why": "no_target", "detail": "no routine host"},
          "route_enabled": true,
          "held_back": {"count": 2, "oldest_age": 840, "reasons": {"host_busy": 2}}}}}}
        """
        let seat = try XCTUnwrap(status(json).seat("ROUTER"))
        XCTAssertEqual(seat.model, "openai/gpt-5")
        XCTAssertEqual(seat.pin?.why, "pin_stale")
        XCTAssertEqual(seat.pin?.pinned_model, "openai/gpt-4")
        XCTAssertEqual(seat.fallback?.pinned, true)
        XCTAssertEqual(seat.routine?.detail, "no routine host")
        XCTAssertEqual(seat.routeEnabled, true)
        XCTAssertEqual(seat.heldBack?.oldest_age, 840)
    }

    // MARK: How old the status is

    /// An old status cannot make a pass or a drift:  those read NOT CURRENT, and say how old.
    func test_an_old_status_is_not_current() {
        let published = 1_792_657_970.0
        let json = """
        {"running": true, "status": {"updated": \(published), "seats": {"MM": {"wake": "muse",
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

    /// A stopped eavesdropper's file is its last word, not what anything uses now.
    func test_a_stopped_eavesdroppers_values_are_not_current() {
        let json = #"{"running": false, "status": {"seats": {"MM": {"wake": "muse", "budget": {"wakes_per_hour": 5}}}}}"#
        let model = WakeSettingsBuilder.build(sources(status: json))
        let row = setting(.wakesPerHour, of: seat("MM", in: model))
        XCTAssertEqual(row.state, .stale)
        XCTAssertTrue(row.note.contains("not running"))
    }

    /// A row whose value Muster read from a pin file is not the eavesdropper's word, so a stopped
    /// eavesdropper does not turn it into NOT CURRENT with a note that it published the value.
    func test_pin_backed_rows_keep_their_own_state_when_the_status_is_old() {
        let json = #"{"running": false, "status": {"seats": {"MM": {"wake": "muse", "budget": {"wakes_per_hour": 5}}}}}"#
        let pins = ["MM": ["muse": pin("muse", model: "muse-spark-1.3-contributor")]]
        let mm = seat("MM", in: WakeSettingsBuilder.build(sources(status: json, pins: pins)))
        let model = setting(.model, of: mm)
        XCTAssertEqual(model.state, .match)
        XCTAssertFalse(model.note.contains("last value it published"))
        XCTAssertEqual(setting(.wakesPerHour, of: mm).state, .stale)
        XCTAssertTrue(WakeSettingsBuilder.liveComesFromStatus(.model, wake: "openrouter"))
        XCTAssertFalse(WakeSettingsBuilder.liveComesFromStatus(.model, wake: "muse"))
        XCTAssertFalse(WakeSettingsBuilder.liveComesFromStatus(.mcodePath, wake: "claude"))
        XCTAssertFalse(WakeSettingsBuilder.liveComesFromStatus(.fallback, wake: "claude"))
    }

    // MARK: The model and the pin, by the seat's own runtime

    /// A Claude seat's model is reported by nothing, so it is not reported:  never UNPROVEN, and
    /// never compared with the mcode fallback's model.
    func test_a_claude_seat_model_is_not_compared_with_the_fallback() {
        let json = #"{"running": true, "status": {"seats": {"CLAUDE": {"wake": "claude", "claude": "/bin/claude"}}}}"#
        let pins = ["CLAUDE": ["mcode": pin("mcode", model: "minimax/minimax-m2"), "claude": pin("claude")]]
        let model = WakeSettingsBuilder.build(sources(status: json, pins: pins))
        let claude = seat("CLAUDE", in: model)
        let row = setting(.model, of: claude)
        XCTAssertEqual(row.state, .unpublished)
        XCTAssertNil(row.live)
        XCTAssertFalse(row.state.isProblem)
        XCTAssertFalse(row.note.contains("minimax"))
        XCTAssertEqual(claude.claudeBinary, "/bin/claude")
    }

    /// An OpenRouter wake's model is the one the eavesdropper publishes.
    func test_an_openrouter_model_is_the_published_one() {
        let same = #"{"running": true, "status": {"seats": {"ROUTER": {"wake": "openrouter", "model": "openai/gpt-5"}}}}"#
        XCTAssertEqual(setting(.model, of: seat("ROUTER", in: WakeSettingsBuilder.build(sources(status: same)))).state, .match)
        let other = #"{"running": true, "status": {"seats": {"ROUTER": {"wake": "openrouter", "model": "openai/gpt-4"}}}}"#
        XCTAssertEqual(setting(.model, of: seat("ROUTER", in: WakeSettingsBuilder.build(sources(status: other)))).state, .drift)
    }

    /// The pin is chosen by the seat's wake:  a leftover muse pin on a Claude seat proves nothing.
    func test_the_pin_is_the_seat_wakes_own() {
        let json = #"{"running": true, "status": {"seats": {"CLAUDE": {"wake": "claude", "pin": {"pinned": false, "why": "not_pinned"}}}}}"#
        let pins = ["CLAUDE": ["muse": pin("muse"), "mcode": pin("mcode")]]
        let claude = seat("CLAUDE", in: WakeSettingsBuilder.build(sources(status: json, pins: pins)))
        XCTAssertEqual(claude.pinState, .missing(runtime: "claude", why: "not_pinned"))
        XCTAssertNil(claude.pin)
        XCTAssertTrue(claude.needsAttention)
    }

    /// The eavesdropper's own verdict decides whether a pin holds, not the file being there.
    func test_a_stale_pin_does_not_read_as_holding() {
        let json = #"{"running": true, "status": {"seats": {"MM": {"wake": "muse", "pin": {"pinned": false, "why": "pin_stale"}}}}}"#
        let pins = ["MM": ["muse": pin("muse", model: "muse-spark-1.3-contributor")]]
        let mm = seat("MM", in: WakeSettingsBuilder.build(sources(status: json, pins: pins)))
        guard case .notHolding(let evidence, let why) = mm.pinState else {
            return XCTFail("expected a pin that does not hold, got \(mm.pinState)")
        }
        XCTAssertEqual(why, "pin_stale")
        XCTAssertFalse(evidence.pinned)
        XCTAssertTrue(PinEvidence.words(forWhy: why).contains("stale"))

        let holding = #"{"running": true, "status": {"seats": {"MM": {"wake": "muse", "pin": {"pinned": true}}}}}"#
        let pinned = seat("MM", in: WakeSettingsBuilder.build(sources(status: holding, pins: pins)))
        XCTAssertEqual(pinned.pinState, .pinned(pins["MM"]!["muse"]!))
    }

    /// A pin file that is there is never a pin that holds without the eavesdropper's current word:
    /// not when the status could not be read, not when it is old, and not when it gave no verdict.
    func test_a_pin_file_without_a_current_verdict_is_not_pinned() {
        let pins = ["MM": ["muse": pin("muse", model: "muse-spark-1.3-contributor")]]

        var unread = sources(status: nil, pins: pins)
        unread.statusProblem = "The eavesdropper's status command failed:  boom"
        let blind = seat("MM", in: WakeSettingsBuilder.build(unread))
        guard case .fileOnly(let evidence, let why, let needsLook) = blind.pinState else {
            return XCTFail("expected a pin file with no verdict, got \(blind.pinState)")
        }
        XCTAssertEqual(evidence, pins["MM"]!["muse"]!)
        XCTAssertTrue(why.contains("could not read"))
        XCTAssertTrue(needsLook)
        XCTAssertTrue(blind.pinState.isProblem)
        XCTAssertEqual(blind.pin, pins["MM"]!["muse"]!)

        let published = 1_792_657_970.0
        let holding = #"{"running": true, "status": {"updated": \#(published), "seats": {"MM": {"wake": "muse", "pin": {"pinned": true}}}}}"#
        var old = sources(status: holding, pins: pins)
        old.readAt = Date(timeIntervalSince1970: published + 14 * 60)
        guard case .fileOnly(_, let oldWhy, true) = seat("MM", in: WakeSettingsBuilder.build(old)).pinState else {
            return XCTFail("an old verdict must not say the pin holds now")
        }
        XCTAssertTrue(oldWhy.contains("not current"))

        let silent = #"{"running": true, "status": {"seats": {"MM": {"wake": "muse"}}}}"#
        let quiet = seat("MM", in: WakeSettingsBuilder.build(sources(status: silent, pins: pins)))
        XCTAssertEqual(
            quiet.pinState,
            .fileOnly(pins["MM"]!["muse"]!, why: "The eavesdropper did not say whether it holds.", needsLook: false)
        )
        XCTAssertFalse(quiet.pinState.isProblem)
    }

    /// The fallback's own pin:  an mcode pin file is only a file, one that cannot be read is
    /// unreadable and not missing, and an OpenRouter fallback goes by the eavesdropper's verdict.
    func test_the_fallback_pin_state_is_its_own() {
        let json = #"{"running": true, "status": {"seats": {"MM": {"wake": "muse"}}}}"#
        let filed = seat("MM", in: WakeSettingsBuilder.build(sources(status: json, pins: ["MM": ["mcode": pin("mcode")]])))
        guard case .fileOnly(_, let why, false) = filed.fallbackPinState else {
            return XCTFail("expected an mcode pin file with no verdict, got \(String(describing: filed.fallbackPinState))")
        }
        XCTAssertTrue(why.contains("does not publish"))
        XCTAssertEqual(setting(.fallback, of: filed).state, .unpublished)

        var unreadable = sources(status: json)
        unreadable.unreadablePins = ["MM": ["mcode": "boom"]]
        let broken = seat("MM", in: WakeSettingsBuilder.build(unreadable))
        XCTAssertEqual(broken.fallbackPinState, .unreadable(runtime: "mcode", problem: "boom"))

        let missing = seat("MM", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertEqual(missing.fallbackPinState, .missing(runtime: "mcode", why: nil))

        let routerKey = WakeType.fallbackPinKey("openrouter")
        let routerPins = ["ROUTER": [routerKey: pin(routerKey, binary: nil, model: "anthropic/claude-sonnet-4")]]
        let pinned = #"{"running": true, "status": {"seats": {"ROUTER": {"wake": "openrouter", "fallback": {"pinned": true}}}}}"#
        let router = seat("ROUTER", in: WakeSettingsBuilder.build(sources(status: pinned, pins: routerPins)))
        XCTAssertEqual(router.fallbackPinState, .pinned(routerPins["ROUTER"]![routerKey]!))

        let quiet = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json)))
        XCTAssertNil(quiet.fallbackPinState)
    }

    /// An inbox seat needs no pin and has nothing to test, and says so.
    func test_an_inbox_seat_needs_no_pin_and_no_test() {
        let json = #"{"running": true, "status": {"seats": {"QUIET": {"wake": "inbox"}}}}"#
        let quiet = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json)))
        guard case .notNeeded(let why) = quiet.pinState else {
            return XCTFail("expected no pin needed, got \(quiet.pinState)")
        }
        XCTAssertTrue(why.contains("runs no worker"))
        XCTAssertNil(quiet.testRuntime)
        XCTAssertNil(WakeType.http.testRuntime)
        XCTAssertEqual(WakeType.openrouter.testRuntime, "openrouter")
        XCTAssertFalse(quiet.needsAttention)
    }

    /// A fallback is proved by its own pin;  an OpenRouter fallback by the eavesdropper's verdict.
    /// A seat with no fallback has no fallback pin, whatever files lie about.
    func test_the_fallback_is_proved_by_its_own_pin() {
        let unpinned = #"{"running": true, "status": {"seats": {"ROUTER": {"wake": "openrouter", "fallback": {"pinned": false, "pin": {"why": "not_pinned"}}}}}}"#
        let router = seat("ROUTER", in: WakeSettingsBuilder.build(sources(status: unpinned)))
        let row = setting(.fallback, of: router)
        XCTAssertEqual(row.state, .unproven)
        XCTAssertTrue(row.note.contains("No passing test wake"))

        let pinned = #"{"running": true, "status": {"seats": {"ROUTER": {"wake": "openrouter", "fallback": {"pinned": true}}}}}"#
        XCTAssertEqual(setting(.fallback, of: seat("ROUTER", in: WakeSettingsBuilder.build(sources(status: pinned)))).state, .match)

        let json = #"{"running": true, "status": {"seats": {"QUIET": {"wake": "inbox"}}}}"#
        let quiet = seat("QUIET", in: WakeSettingsBuilder.build(sources(status: json, pins: ["QUIET": ["mcode": pin("mcode")]])))
        XCTAssertNil(quiet.fallbackPin)
    }

    /// Only the settings a wake uses apply to it, by the eavesdropper's own rules.
    func test_settings_apply_by_wake_type() {
        XCTAssertTrue(WakeSettingKey.musePath.applies(toWake: "muse", fallback: nil))
        XCTAssertFalse(WakeSettingKey.musePath.applies(toWake: "claude", fallback: nil))
        XCTAssertFalse(WakeSettingKey.mcodePath.applies(toWake: "openrouter", fallback: nil))
        XCTAssertTrue(WakeSettingKey.fallbackUncontained.applies(toWake: "claude", fallback: "mcode"))
        XCTAssertFalse(WakeSettingKey.fallbackUncontained.applies(toWake: "claude", fallback: "openrouter"))
        XCTAssertFalse(WakeSettingKey.wakesPerHour.applies(toWake: "inbox", fallback: nil))
        XCTAssertFalse(WakeSettingKey.wakesPerHour.applies(toWake: nil, fallback: nil))
        XCTAssertTrue(WakeSettingKey.wakesPerHour.applies(toWake: "http", fallback: nil))
        XCTAssertTrue(WakeSettingKey.wakeType.applies(toWake: nil, fallback: nil))
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
        let root = home.appendingPathComponent(".agent-sync", isDirectory: true)
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

    /// A stand-in for `simple-chat` that answers the status and the test wake.
    private final class Fake: AgentSyncRunning, @unchecked Sendable {
        private let lock = NSLock()
        private var calls: [[String]] = []
        var status = #"{"running": true, "status": {"seats": {}, "pause": {}}}"#
        var statusExit: Int32 = 0
        var testWake = AgentSyncCLIResult(exitCode: 0, stdout: "", stderr: "")
        var pin = AgentSyncCLIResult(exitCode: 0, stdout: "", stderr: "")

        var commands: [String] {
            lock.lock()
            defer { lock.unlock() }
            return calls.map { $0.count > 1 ? $0[1] : "" }
        }

        func run(_ invocation: AgentSyncInvocation) async throws -> AgentSyncCLIResult {
            lock.lock()
            defer { lock.unlock() }
            calls.append(invocation.arguments)
            switch invocation.arguments.count > 1 ? invocation.arguments[1] : "" {
            case "status": return AgentSyncCLIResult(exitCode: statusExit, stdout: status, stderr: "")
            case "test-wake": return invocation.arguments.contains("--pin") ? pin : testWake
            default: return AgentSyncCLIResult(exitCode: 0, stdout: "", stderr: "")
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

    /// A pin file that cannot be read is said to be unreadable, not missing.
    func test_an_unreadable_pin_is_not_called_missing() async throws {
        let home = try makeHome(config: "[seat.MM]\nbot = \"MM\"\nwake = \"muse\"\n")
        let location = ListenerStateLocation(home: home)
        let pinURL = location.pinURL(seat: "MM", runtime: "muse")
        try FileManager.default.createDirectory(at: pinURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "not json".write(to: pinURL, atomically: true, encoding: .utf8)
        let candidate = location.candidateURL(seat: "MM", runtime: "muse")
        try #"{"at": 1792535804.0, "muse": "/bin/muse"}"#.write(to: candidate, atomically: true, encoding: .utf8)
        let sources = await WakeSettingsReader(
            location: location, runner: Fake(), roster: ["MM"],
            partitionURL: home.appendingPathComponent("partition.toml")
        ).read()
        let mm = seat("MM", in: WakeSettingsBuilder.build(sources))
        guard case .unreadable(let runtime, _) = mm.pinState else {
            return XCTFail("expected an unreadable pin, got \(mm.pinState)")
        }
        XCTAssertEqual(runtime, "muse")
        XCTAssertEqual(mm.candidates["muse"], Date(timeIntervalSince1970: 1_792_535_804))
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
    private func makeStore(_ fake: AgentSyncRunning, home: URL) async -> WakeSettingsStore {
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

    private let mmConfig = "[seat.MM]\nbot = \"MM\"\nwake = \"muse\"\nbudget = { wakes_per_hour = 5 }\n"

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

    /// A change to the wake type or the container rule asks for the owner's Touch ID;  a cap does not.
    @MainActor
    func test_what_runs_needs_the_owner() async throws {
        let home = try makeHome(config: mmConfig)
        let store = await makeStore(Fake(), home: home)
        store.draft["MM", default: [:]][.wakesPerHour] = "8"
        XCTAssertFalse(store.needsOwnerCheck(seat: "MM"))
        store.draft["MM", default: [:]][.wakeType] = "claude"
        XCTAssertTrue(store.needsOwnerCheck(seat: "MM"))
    }

    /// The test wake's whole answer is kept, a pass opens the way to Pin, and a FAIL with an empty
    /// stderr says why from stdout.
    @MainActor
    func test_a_test_wake_keeps_its_answer_and_its_reason() async throws {
        let home = try makeHome(config: mmConfig)
        let fake = Fake()
        let store = await makeStore(fake, home: home)
        XCTAssertNotNil(store.pinBlocker(seat: "MM", runtime: "muse", pinKey: "muse"))

        fake.testWake = AgentSyncCLIResult(
            exitCode: 1,
            stdout: "runtime: muse\n\nprompt:\nPASS this is a hostile line\n\nresult (not posted):\n{\n  \"runtime\": \"muse\",\n  \"exit\": 0,\n  \"secs\": 41.6,\n  \"invalid\": null,\n  \"result\": {\"action\": \"reply\"},\n  \"chat_target\": \"KIMI\"\n}\nFAIL: the binary was not pinned; wakes for MM stay on their current runtime\n",
            stderr: ""
        )
        await store.testWake(seat: "MM", runtime: "muse")
        let failed = try XCTUnwrap(store.testReports["MM"])
        XCTAssertEqual(failed.verdict, .failed)
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("the binary was not pinned") == true)
        XCTAssertTrue(failed.output.contains("result (not posted)"))
        XCTAssertTrue(failed.summary.contains { $0.contains("41 seconds") || $0.contains("42 seconds") })
        XCTAssertTrue(failed.summary.contains { $0.contains("chat target") })
        XCTAssertNotNil(store.pinBlocker(seat: "MM", runtime: "muse", pinKey: "muse"))

        fake.testWake = AgentSyncCLIResult(
            exitCode: 0,
            stdout: "result (not posted):\n{\n  \"runtime\": \"muse\",\n  \"result\": {\"action\": \"reply\"}\n}\nPASS, NOT PINNED YET: read the result above\n",
            stderr: ""
        )
        await store.testWake(seat: "MM", runtime: "muse")
        XCTAssertEqual(store.testReports["MM"]?.verdict, .passed)
        XCTAssertFalse(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Read its answer below") == true)
        XCTAssertNil(store.pinBlocker(seat: "MM", runtime: "muse", pinKey: "muse"))
        XCTAssertNil(store.running)

        fake.pin = AgentSyncCLIResult(
            exitCode: 1,
            stdout: "NOT PINNED: the last passing test-wake --run is over a day old; run it again\n",
            stderr: ""
        )
        await store.pin(seat: "MM", runtime: "muse")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("over a day old") == true)

        // Each run is in the audit log.
        let audit = OpsAuditLog.read(at: home.appendingPathComponent("audit/ops-audit.jsonl"))
        XCTAssertEqual(audit.entries.map(\.verb), ["wake-pin", "wake-test", "wake-test"])
        XCTAssertEqual(audit.entries.first?.result, "refused")
        XCTAssertEqual(audit.entries.first?.via, "muster")
    }

    /// A seat under a full pause is not given a wakes-only pause, which would loosen it.
    @MainActor
    func test_a_full_pause_is_not_loosened() async throws {
        let home = try makeHome(config: mmConfig)
        let fake = Fake()
        fake.status = #"{"running": true, "status": {"seats": {"MM": {"wake": "muse", "paused": true, "wakes_paused": true}}, "pause": {"MM": {"by": "JAY", "ts": 1792535804.0}}}}"#
        let store = await makeStore(fake, home: home)
        let mm = try XCTUnwrap(store.model.seats.first)
        XCTAssertEqual(mm.pauseBy, "JAY")
        await store.pause(seat: "MM")
        XCTAssertFalse(fake.commands.contains("pause"))
        XCTAssertTrue(store.message?.contains("would loosen it") == true)
    }

    /// With the status unread, the pause file itself still stops a wakes-only pause over a full
    /// one, and a pause file that cannot be read stops the pause with its reason.
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
        XCTAssertEqual(store.model.seats.first(where: { $0.seat == "MM" })?.paused, false)
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

    /// A stand-in whose test wake runs until the test lets it finish.
    private final class SlowFake: AgentSyncRunning, @unchecked Sendable {
        private let lock = NSLock()
        private var calls: [String] = []
        private var released = false
        private var started = false

        var commands: [String] {
            lock.lock()
            defer { lock.unlock() }
            return calls
        }

        var testWakeStarted: Bool {
            lock.lock()
            defer { lock.unlock() }
            return started
        }

        private var isReleased: Bool {
            lock.lock()
            defer { lock.unlock() }
            return released
        }

        func release() {
            lock.lock()
            released = true
            lock.unlock()
        }

        private func note(_ command: String) {
            lock.lock()
            calls.append(command)
            if command == "test-wake" { started = true }
            lock.unlock()
        }

        func run(_ invocation: AgentSyncInvocation) async throws -> AgentSyncCLIResult {
            let command = invocation.arguments.count > 1 ? invocation.arguments[1] : ""
            note(command)
            switch command {
            case "status":
                return AgentSyncCLIResult(exitCode: 0, stdout: #"{"running": true, "status": {"seats": {}, "pause": {}}}"#, stderr: "")
            case "test-wake":
                var waited = 0
                while !isReleased, waited < 1000 {
                    try await Task.sleep(nanoseconds: 10_000_000)
                    waited += 1
                }
                return AgentSyncCLIResult(exitCode: 0, stdout: "PASS, NOT PINNED YET: read the result above\n", stderr: "")
            default:
                return AgentSyncCLIResult(exitCode: 0, stdout: "", stderr: "")
            }
        }
    }

    /// A test wake runs for minutes, so it holds only its own gate:  the panel is not busy, and the
    /// pause, the kill switch, still runs while it does.
    @MainActor
    func test_a_running_test_wake_leaves_the_pause_working() async throws {
        let home = try makeHome(config: mmConfig)
        let fake = SlowFake()
        let store = await makeStore(fake, home: home)
        let run = Task { await store.testWake(seat: "MM", runtime: "muse") }
        var waited = 0
        while !fake.testWakeStarted, waited < 500 {
            try await Task.sleep(nanoseconds: 10_000_000)
            waited += 1
        }
        XCTAssertTrue(fake.testWakeStarted)
        XCTAssertNotNil(store.running)
        XCTAssertFalse(store.isBusy)
        await store.pause(seat: "MM")
        XCTAssertTrue(fake.commands.contains("pause"))
        XCTAssertFalse(store.messageIsFailure)
        fake.release()
        await run.value
        XCTAssertNil(store.running)
        XCTAssertEqual(store.testReports["MM"]?.verdict, .passed)
    }

    /// Pin let through by a candidate from a test run elsewhere says Muster never saw its answer;
    /// one let through by a test shown here does not need to.
    @MainActor
    func test_a_candidate_from_elsewhere_is_named_as_unseen() async throws {
        let home = try makeHome(config: mmConfig)
        let location = ListenerStateLocation(home: home)
        let candidate = location.candidateURL(seat: "MM", runtime: "muse")
        try FileManager.default.createDirectory(at: candidate.deletingLastPathComponent(), withIntermediateDirectories: true)
        let at = (Date().timeIntervalSince1970 - 3600).rounded()
        try #"{"at": \#(at), "muse": "/bin/muse"}"#.write(to: candidate, atomically: true, encoding: .utf8)
        let fake = Fake()
        let store = await makeStore(fake, home: home)

        let gate = store.pinGate(seat: "MM", runtime: "muse", pinKey: "muse")
        XCTAssertEqual(gate, .candidate(Date(timeIntervalSince1970: at)))
        XCTAssertNil(store.pinBlocker(seat: "MM", runtime: "muse", pinKey: "muse"))
        let note = try XCTUnwrap(gate.confirmationNote)
        XCTAssertTrue(note.contains("Muster did not see this test's answer"))
        XCTAssertTrue(note.contains(FleetTime.clock(Date(timeIntervalSince1970: at))))
        XCTAssertTrue(note.contains("\u{00A0} Pin only if you read it there."))

        fake.testWake = AgentSyncCLIResult(exitCode: 0, stdout: "PASS, NOT PINNED YET: read the result above\n", stderr: "")
        await store.testWake(seat: "MM", runtime: "muse")
        let shown = store.pinGate(seat: "MM", runtime: "muse", pinKey: "muse")
        XCTAssertEqual(shown, .inSession)
        XCTAssertNil(shown.confirmationNote)
    }

    /// A field cleared over a setting the file never held is no edit:  the summary and the write
    /// agree with the Edited mark.
    @MainActor
    func test_a_cleared_field_over_nothing_is_no_edit() {
        let reading = WakeSettingsStore.readDraft(
            seat: "MM",
            values: [.model: "", .musePath: " ", .usdPerDay: ""],
            before: [.model: WakeSetting.unsetText, .musePath: WakeSetting.unsetText, .usdPerDay: WakeSetting.unsetText]
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

    // MARK: Small pieces

    /// A run that failed with nothing on stderr shows the end of stdout instead of nothing.
    func test_a_failure_with_an_empty_stderr_shows_stdout() {
        let result = AgentSyncCLIResult(exitCode: 1, stdout: "line one\nNOT PINNED: why\n", stderr: "")
        XCTAssertEqual(result.failureTail, "line one\nNOT PINNED: why")
        let both = AgentSyncCLIResult(exitCode: 1, stdout: "out", stderr: "err")
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
}
