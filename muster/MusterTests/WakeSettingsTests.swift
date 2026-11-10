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

    /// The eavesdropper's own account of itself.
    private let statusJSON = """
    {
      "running": true,
      "pid": 29131,
      "status": {
        "seats": {
          "MM": {
            "bot": "MM",
            "routine": {"ready": true, "host": "routines.example"},
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

    /// What `daemon status --json` answers with no eavesdropper running and no status file.
    private let stoppedStatus = #"{"running": false, "pid": null, "status": {}}"#

    /// The sources the builder reads.
    private func sources(
        config: String? = nil,
        status: String? = nil,
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
        sources.usage = usage
        sources.seats = ["MM"]
        sources.partition = ListenerPartition(seats: ["MM": "mac", "MA": "server", "BF-BUILDER": "none"])
        return sources
    }

    /// A partition like the eavesdropper's own.
    private let partitionText = """
    version = 1

    [seats]
    # Mac seats
    MM = "mac"
    MA = "server"
    BF-BUILDER = "none"
    """

    /// A state directory and a partition file,  gone when the test ends.
    private func makeWorkspace(config: String? = nil) throws -> (home: URL, config: URL, partition: URL) {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-wake-\(UUID().uuidString)", isDirectory: true)
        let root = home.appendingPathComponent(".simple-tools", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: home) }
        let configURL = root.appendingPathComponent("listener.toml")
        try (config ?? sample).write(to: configURL, atomically: true, encoding: .utf8)
        let partitionURL = home.appendingPathComponent("partition.toml")
        try partitionText.write(to: partitionURL, atomically: true, encoding: .utf8)
        return (home, configURL, partitionURL)
    }

    private func bytes(_ url: URL) throws -> Data {
        try Data(contentsOf: url)
    }

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
        for seat in ["CLAUDE", "CODEX", "AG", "CURSOR", "GROK", "CLUTCH", "FX", "MM", "MC", "OPERATOR", "OPENCODE", "ZC", "KIMI", "AMP"] {
            XCTAssertEqual(partition.host(of: seat), .mac, seat)
        }
        for seat in ["MA", "ECHO", "WISE", "GROK-WEB", "JET", "GB-FIXER", "BF-DIRECTOR", "BF-COMPILER", "BF-ENGINEER", "BF-TRADER"] {
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

    /// A seat another eavesdropper holds is shown as elsewhere,  with nothing from the Mac's eavesdropper.
    func test_a_server_seat_shows_nothing_from_the_macs_listener() {
        var input = sources(status: """
        {"running": true, "status": {"seats": {}, "pause": {"MA": {"wakes_only": true}, "*": {}}}}
        """)
        input.seats = ["MM", "MA"]
        input.usage = ["MA": WakeUsage(wakesThisHour: 7)]
        let model = WakeSettingsBuilder.build(input)
        let seat = model.seats[1]
        XCTAssertEqual(seat.host, .server)
        XCTAssertFalse(seat.hasSection)
        XCTAssertFalse(seat.hasRefusedSection)
        XCTAssertNil(seat.pauseScope)
        XCTAssertFalse(seat.paused)
        XCTAssertEqual(seat.usage, WakeUsage())
        XCTAssertTrue(seat.problems.isEmpty)
        XCTAssertTrue(seat.settings.allSatisfy { $0.state == .heldElsewhere })
        XCTAssertFalse(WakeSettingState.heldElsewhere.isProblem)
    }

    /// A section for such a seat in the Mac's file is the thing that stops the eavesdropper,  so the
    /// panel says so,  and the seat counts as a problem.
    func test_a_section_for_a_server_seat_is_named_as_the_problem() {
        var input = sources(config: sample + "\n[seat.MA]\nbudget = { wakes_per_hour = 9 }\n", status: stoppedStatus)
        input.seats = ["MM", "MA"]
        let model = WakeSettingsBuilder.build(input)
        let seat = model.seats[1]
        XCTAssertTrue(seat.hasSection)
        XCTAssertTrue(seat.hasRefusedSection)
        XCTAssertTrue(seat.needsAttention)
        XCTAssertTrue(model.problems.contains { $0.contains("[seat.MA]") })
        XCTAssertTrue(seat.refusedSectionProblem?.contains("refuse its next start") == true)
    }

    /// The eavesdropper tests a section's own `instance` key first,  so a seat the partition gives to
    /// the Mac can still be one it refuses.  The panel says so,  and offers the repair.
    func test_a_section_marked_for_the_server_is_refused() throws {
        var input = sources(config: sample + "\ninstance = \"server\"\n", status: stoppedStatus)
        input.seats = ["MM"]
        let model = WakeSettingsBuilder.build(input)
        let seat = try XCTUnwrap(model.seats.first)
        XCTAssertEqual(seat.host, .markedForTheServer)
        XCTAssertTrue(seat.host.isElsewhere)
        XCTAssertTrue(seat.hasRefusedSection)
        XCTAssertTrue(seat.needsAttention)
        XCTAssertTrue(seat.refusedSectionProblem?.contains("instance = \"server\"") == true)
        XCTAssertTrue(seat.refusedSectionProblem?.contains("refuse its next start") == true)
        XCTAssertTrue(seat.settings.allSatisfy { $0.state == .heldElsewhere })
    }

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

    /// The eavesdropper publishes every seat under its upper-cased name,  so a `[seat.mm]` section
    /// finds what the eavesdropper says about MM.
    func test_a_lower_case_section_finds_what_the_listener_says() throws {
        let status = """
        {
          "running": true,
          "status": {
            "disabled": ["MM"],
            "skipped_seats": {"MM": ["seat.MM.bot must be the credential file code"]},
            "pause": {"MM": {"wakes_only": true}},
            "seats": {"MM": {"bot": "MM", "wake": "muse"}}
          }
        }
        """
        var input = sources(config: "[seat.mm]\nbot = \"MM\"\n", status: status)
        input.seats = ["mm"]
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(input).seats.first)
        XCTAssertEqual(seat.skippedReasons, ["seat.MM.bot must be the credential file code"])
        XCTAssertTrue(seat.disabledInFile)
        XCTAssertTrue(seat.reported)
        XCTAssertEqual(seat.pauseScope, "MM")
    }

    /// A running eavesdropper from before `partition_file` holds a seat the copy Muster read gives
    /// away,  so the two copies differ.  That seat is not called elsewhere,  so it cannot be removed.
    func test_a_seat_an_older_running_listener_holds_is_not_called_elsewhere() throws {
        let status = #"{"running": true, "status": {"seats": {"MA": {"bot": "MA"}}}}"#
        var input = sources(config: sample + "\n[seat.MA]\nwake = \"inbox\"\n", status: status)
        input.seats = ["MA"]
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(input).seats.first)
        guard case .unknown = seat.host else {
            XCTFail("expected an unknown host, got \(seat.host)")
            return
        }
        XCTAssertFalse(seat.hasRefusedSection)

        // A section it took and turned off is still a section its partition check allowed.
        let off = #"{"running": true, "status": {"disabled": ["MA"], "seats": {}}}"#
        var disabled = sources(config: sample + "\n[seat.MA]\nenabled = false\n", status: off)
        disabled.seats = ["MA"]
        XCTAssertEqual(WakeSettingsBuilder.build(disabled).seats.first?.hasRefusedSection, false)

        // Once the eavesdropper names its partition,  that copy is the one Muster read,  so a seat it
        // still runs from before the partition moved is a section the next start will refuse.
        let named = #"{"running": true, "status": {"partition_file": "/p", "seats": {"MA": {"bot": "MA"}}}}"#
        var current = sources(config: sample + "\n[seat.MA]\nwake = \"inbox\"\n", status: named)
        current.seats = ["MA"]
        XCTAssertEqual(WakeSettingsBuilder.build(current).seats.first?.hasRefusedSection, true)

        // An eavesdropper that is not running holds nothing.
        let stopped = #"{"running": false, "status": {"seats": {"MA": {"bot": "MA"}}}}"#
        var down = sources(config: sample + "\n[seat.MA]\nwake = \"inbox\"\n", status: stopped)
        down.seats = ["MA"]
        XCTAssertEqual(WakeSettingsBuilder.build(down).seats.first?.host, .server)
    }

    /// A failed status read says nothing about which copy a running eavesdropper reads,  so Muster's
    /// own copy cannot call a seat elsewhere,  and its section cannot be offered for removal.
    func test_with_no_status_no_seat_is_called_elsewhere() throws {
        var input = sources(config: sample + "\n[seat.MA]\nwake = \"inbox\"\n")
        input.seats = ["MA"]
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(input).seats.first)
        guard case .unknown(let why) = seat.host else {
            XCTFail("expected an unknown host, got \(seat.host)")
            return
        }
        XCTAssertTrue(why.contains("could not read the eavesdropper's status"))
        XCTAssertFalse(seat.hasRefusedSection)
        XCTAssertNil(seat.refusedSectionProblem)
        // A stopped eavesdropper still answers,  and then the copy Muster read decides.
        var stopped = sources(config: sample + "\n[seat.MA]\nwake = \"inbox\"\n", status: stoppedStatus)
        stopped.seats = ["MA"]
        XCTAssertEqual(WakeSettingsBuilder.build(stopped).seats.first?.hasRefusedSection, true)
    }

    /// TOML reads `[seat."MM"]` as seat MM,  so the listener holds it.  Muster keeps the quotes,  and
    /// must not call that name unlisted,  because the panel would then offer to delete a live seat.
    func test_a_quoted_section_name_is_not_called_unlisted() throws {
        let config = "[seat.\"MM\"]\nbot = \"MM\"\nwake = \"muse\"\n"
        var input = sources(config: config, status: stoppedStatus)
        input.seats = input.config.seatTags
        XCTAssertEqual(input.seats, ["\"MM\""])
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(input).seats.first)
        guard case .unknown(let why) = seat.host else {
            XCTFail("expected an unknown host, got \(seat.host)")
            return
        }
        XCTAssertTrue(why.contains("cannot read"))
        XCTAssertTrue(seat.hasSection)
        XCTAssertFalse(seat.hasRefusedSection)

        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.prepareRemoval(ofSeat: "\"MM\""))
    }

    /// With no readable partition,  a seat's holder is unknown and the panel says so.
    func test_without_the_partition_every_seat_is_unknown() {
        var input = sources(status: statusJSON)
        input.partition = ListenerPartition(unreadable: "It is missing.")
        let model = WakeSettingsBuilder.build(input)
        guard let seat = model.seats.first, case .unknown = seat.host else {
            XCTFail("expected an unknown host")
            return
        }
        XCTAssertFalse(seat.host.isTheMac)
        XCTAssertTrue(model.problems.contains { $0.contains("every seat is read only") })
        // The Mac's own readings still show,  because the seat may well be on the Mac.
        XCTAssertEqual(seat.settings.first(where: { $0.key == .wakesPerHour })?.state, .match)
    }

    // MARK: What the eavesdropper itself reports

    /// The eavesdropper's refusals and skipped seats reach the panel.
    func test_the_listeners_own_problems_are_decoded_and_shown() throws {
        let status = """
        {
          "running": true,
          "status": {
            "instance": "mac",
            "disabled": ["MM"],
            "skipped_seats": {"MM": ["seat.MM.bot must be the credential file code"]},
            "config_errors": ["seat MA is configured here (the 'mac' listener)"],
            "reload_refused": ["seat MA is configured here (the 'mac' listener)"],
            "reload_at": 1792657970.5,
            "updated": 1792657971.0,
            "seats": {}
          }
        }
        """
        let decoded = try JSONDecoder().decode(ListenerStatus.self, from: Data(status.utf8))
        XCTAssertEqual(decoded.status?.instance, "mac")
        XCTAssertEqual(decoded.status?.reload_at, 1792657970.5)
        XCTAssertEqual(decoded.status?.reload_refused?.count, 1)
        let model = WakeSettingsBuilder.build(sources(status: status))
        XCTAssertEqual(model.listenerErrors, ["seat MA is configured here (the 'mac' listener)"])
        let seat = try XCTUnwrap(model.seats.first)
        XCTAssertEqual(seat.skippedReasons, ["seat.MM.bot must be the credential file code"])
        XCTAssertTrue(seat.disabledInFile)
        XCTAssertTrue(seat.needsAttention)
    }

    /// An eavesdropper that says it is not the Mac's is called out.
    func test_a_listener_that_is_not_the_macs_is_called_out() {
        let status = #"{"running": true, "status": {"instance": "server", "seats": {}}}"#
        let model = WakeSettingsBuilder.build(sources(status: status))
        XCTAssertTrue(model.problems.contains { $0.contains("server instance") })
    }

    /// An eavesdropper from before these fields still decodes.
    func test_a_status_without_the_new_fields_still_decodes() {
        let model = WakeSettingsBuilder.build(sources(status: statusJSON))
        XCTAssertEqual(model.listenerErrors, [])
        XCTAssertEqual(model.seats.first?.skippedReasons, [])
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

    /// A status written before the eavesdropper acted says nothing yet.
    func test_a_status_older_than_the_signal_is_no_answer() {
        XCTAssertNil(ListenerReloadOutcome.answer(toReloadSentAt: 100, in: status(updated: 99)))
        XCTAssertNil(ListenerReloadOutcome.answer(toReloadSentAt: 100, in: status(updated: 200, reloadAt: 99, refused: [])))
        // A publish after the signal is not proof when the eavesdropper has its own reload time.
        XCTAssertNil(ListenerReloadOutcome.answer(toReloadSentAt: 100, in: status(updated: 200, refused: [])))
        XCTAssertNil(ListenerReloadOutcome.answer(toReloadSentAt: 100, in: ListenerStatus()))
    }

    /// The eavesdropper's own answer decides it.
    func test_a_refused_reload_is_a_failure_and_an_accepted_one_is_not() {
        let refused = ListenerReloadOutcome.answer(
            toReloadSentAt: 100,
            in: status(updated: 101, reloadAt: 100.5, refused: ["seat MA is configured here"], errors: ["seat MA is configured here"])
        )
        XCTAssertEqual(refused, .refused(["seat MA is configured here"]))
        XCTAssertEqual(refused?.isFailure, true)
        XCTAssertTrue(refused?.report.contains("refused") == true)
        XCTAssertTrue(refused?.report.contains("seat MA") == true)

        let accepted = ListenerReloadOutcome.answer(
            toReloadSentAt: 100,
            in: status(updated: 101, reloadAt: 100.5, refused: [])
        )
        XCTAssertEqual(accepted, .accepted(otherProblems: []))
        XCTAssertEqual(accepted?.isFailure, false)

        let soft = ListenerReloadOutcome.answer(
            toReloadSentAt: 100,
            in: status(updated: 101, reloadAt: 100.5, refused: [], errors: ["seat.CODEX.wake must be one of"])
        )
        XCTAssertEqual(soft, .accepted(otherProblems: ["seat.CODEX.wake must be one of"]))
        XCTAssertEqual(soft?.isFailure, false)
    }

    /// An eavesdropper too old to separate a refusal is never called accepted:  it publishes on every
    /// tick, so a status newer than the signal does not show it took the file.
    func test_an_older_listener_is_never_called_accepted() {
        XCTAssertEqual(
            ListenerReloadOutcome.answer(toReloadSentAt: 100, in: status(updated: 101, errors: ["seat MA is configured here"])),
            .reportsProblems(["seat MA is configured here"])
        )
        let quiet = ListenerReloadOutcome.answer(toReloadSentAt: 100, in: status(updated: 101))
        XCTAssertEqual(quiet, .cannotTell)
        XCTAssertEqual(quiet?.isFailure, true)
        XCTAssertFalse(quiet?.report.contains("accepted") == true)
        XCTAssertTrue(quiet?.report.contains("cannot say") == true)
    }

    /// No answer and a signal that could not be sent are failures,  and say so.
    func test_no_answer_and_no_signal_are_failures() {
        XCTAssertTrue(ListenerReloadOutcome.noAnswer(seconds: 15).isFailure)
        XCTAssertTrue(ListenerReloadOutcome.noAnswer(seconds: 15).report.contains("after 15 seconds"))
        XCTAssertTrue(ListenerReloadOutcome.notAsked("not running").isFailure)
        XCTAssertTrue(ListenerReloadOutcome.notAsked("not running").report.contains("not running"))
    }

    // MARK: The writer's gate

    /// An Apply on a seat the Mac does not hold writes nothing,  and leaves no file behind.
    func test_a_write_for_a_seat_the_mac_does_not_hold_writes_nothing() throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        let values: [ListenerConfigValue?] = [.number(9), nil]
        for seat in ["MA", "BF-BUILDER", "KIMI"] {
            for value in values {
                let edit = WakeSettingsWriter.Edit(seat: seat, key: .wakesPerHour, value: value)
                XCTAssertThrowsError(try writer.apply([edit]), seat) { error in
                    guard case WakeSettingsWriter.WriteError.notHeldByTheMac(let named, _) = error else {
                        XCTFail("the wrong refusal: \(error)")
                        return
                    }
                    XCTAssertEqual(named, seat)
                }
            }
        }
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(
            try FileManager.default.contentsOfDirectory(atPath: space.config.deletingLastPathComponent().path),
            ["listener.toml"]
        )
    }

    /// One seat the Mac does not hold stops the whole batch.
    func test_one_seat_the_mac_does_not_hold_stops_the_whole_batch() throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
            WakeSettingsWriter.Edit(seat: "MA", key: .wakesPerHour, value: .number(9)),
        ]))
        XCTAssertEqual(try bytes(space.config), before)
    }

    /// A partition that cannot be read refuses every seat,  the Mac's own included.
    func test_a_write_with_no_readable_partition_writes_nothing() throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let edit = WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9))
        let urls: [URL?] = [nil, space.home.appendingPathComponent("missing.toml")]
        for url in urls {
            let writer = WakeSettingsWriter(configURL: space.config, partitionURL: url)
            XCTAssertThrowsError(try writer.apply([edit]))
        }
        try "[seats]\nMM = \"laptop\"\n".write(to: space.partition, atomically: true, encoding: .utf8)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.apply([edit]))
        XCTAssertEqual(try bytes(space.config), before)
    }

    /// A tag that is not a seat name never reaches a header.
    func test_a_tag_that_is_not_a_seat_name_is_refused() throws {
        let space = try makeWorkspace()
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        for seat in ["MM]\n[daemon", "mm", "MM.budget"] {
            let edit = WakeSettingsWriter.Edit(seat: seat, key: .wakesPerHour, value: .number(9))
            XCTAssertThrowsError(try writer.apply([edit]), seat) { error in
                XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .notASeatName(seat))
            }
        }
    }

    /// A seat whose own section names the server is not written,  whatever the partition says.
    func test_a_write_for_a_section_marked_for_the_server_writes_nothing() throws {
        let space = try makeWorkspace(config: sample + "\ninstance = \"server\"\n")
        let before = try bytes(space.config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
        ])) { error in
            guard case WakeSettingsWriter.WriteError.notHeldByTheMac(let named, _) = error else {
                XCTFail("the wrong refusal: \(error)")
                return
            }
            XCTAssertEqual(named, "MM")
        }
        XCTAssertEqual(try bytes(space.config), before)
    }

    /// The eavesdropper folds `[seat.mm]` and `[seat.MM]` to one seat,  and one silently replaces the
    /// other,  so a second spelling is never added.
    func test_a_seat_the_file_spells_differently_is_not_written() throws {
        let config = "[seat.mm]\nbot = \"MM\"\nwake = \"muse\"\n"
        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
        ])) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .spelledDifferently(seat: "MM", inFile: "mm"))
        }
        XCTAssertEqual(try String(contentsOf: space.config, encoding: .utf8), config)
    }

    /// The gate runs again at the commit,  because the partition can change during the check.
    func test_the_gate_runs_again_at_the_commit() throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        let prepared = try writer.prepare([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
        ])
        try "[seats]\nMM = \"server\"\n".write(to: space.partition, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try writer.commit(prepared))
        XCTAssertEqual(try bytes(space.config), before)
    }

    // MARK: The writer's other refusals

    /// A section holding a line this app cannot read is not written:  the dotted key already
    /// defines `budget`,  and a second definition would make the whole file unreadable.
    func test_a_section_with_an_unread_line_is_not_written() throws {
        let config = """
        [seat.MM]
        bot = "MM"
        budget.wakes_per_hour = 5

        [seat.MC]
        bot = "MC"
        """
        let space = try makeWorkspace(config: config)
        try "[seats]\nMM = \"mac\"\nMC = \"mac\"\n".write(to: space.partition, atomically: true, encoding: .utf8)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
        ])) { error in
            guard case WakeSettingsWriter.WriteError.unreadableLines(let seat, let problems) = error else {
                XCTFail("the wrong refusal: \(error)")
                return
            }
            XCTAssertEqual(seat, "MM")
            XCTAssertEqual(problems.count, 1)
        }
        XCTAssertEqual(try String(contentsOf: space.config, encoding: .utf8), config)
        // Another seat's section is clean,  so it can still be written.
        XCTAssertNoThrow(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MC", key: .wakesPerHour, value: .number(9)),
        ]))
    }

    /// Caps the file keeps under a `[seat.X.budget]` or `[seat.X.live]` header are read from there,
    /// never shown as the default, and written there in place, never as a second `budget` line.
    func test_caps_under_their_own_header_are_read_and_written_in_place() throws {
        let config = """
        [seat.MM]
        bot = "MM"
        routine = { url_env = "MM_ROUTINE_URL" }

        [seat.MM.budget]
        wakes_per_hour = 5
        # the owner's own note

        [seat.MM.live]
        per_day = 30
        """
        let document = ListenerConfigDocument(text: config)
        XCTAssertEqual(WakeSettingKey.wakesPerHour.location(seat: "MM", in: document).section, "seat.MM.budget")
        XCTAssertEqual(WakeSettingKey.livePerDay.location(seat: "MM", in: document).key, "per_day")
        XCTAssertEqual(WakeSettingKey.wakesPerHour.location(seat: "MC", in: document).section, "seat.MC")
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(sources(config: config, status: statusJSON)).seats.first)
        XCTAssertEqual(seat.settings.first(where: { $0.key == .wakesPerHour })?.configured, .number(5))
        XCTAssertEqual(seat.settings.first(where: { $0.key == .wakesPerHour })?.state, .match)
        XCTAssertEqual(seat.settings.first(where: { $0.key == .livePerDay })?.configured, .number(30))

        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerDay, value: .number(80)),
            WakeSettingsWriter.Edit(seat: "MM", key: .livePerDay, value: nil),
        ])
        let written = try String(contentsOf: space.config, encoding: .utf8)
        let after = ListenerConfigDocument(text: written)
        XCTAssertNil(after.assignment(section: "seat.MM", key: "budget"))
        XCTAssertEqual(after.value(section: "seat.MM.budget", key: "wakes_per_hour"), .number(9))
        XCTAssertEqual(after.value(section: "seat.MM.budget", key: "wakes_per_day"), .number(80))
        XCTAssertNil(after.value(section: "seat.MM.live", key: "per_day"))
        XCTAssertTrue(written.contains("# the owner's own note"))
        XCTAssertEqual(after.problems, [])

        // A table written both inline and under its own header is one TOML refuses, so nothing is
        // written to either.
        let both = "[seat.MM]\nbudget = { wakes_per_hour = 5 }\n\n[seat.MM.budget]\nwakes_per_day = 80\n"
        let twice = try makeWorkspace(config: both)
        let refusing = WakeSettingsWriter(configURL: twice.config, partitionURL: twice.partition)
        XCTAssertThrowsError(try refusing.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
        ]))
        XCTAssertEqual(try String(contentsOf: twice.config, encoding: .utf8), both)
    }

    /// A file that changed after the panel read it is not overwritten,  at either step.
    func test_a_file_that_changed_since_it_was_read_is_not_written() throws {
        let space = try makeWorkspace()
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        let edit = WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9))
        XCTAssertThrowsError(try writer.prepare([edit], expecting: "something else")) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .changedSinceRead(space.config.path))
        }
        let prepared = try writer.prepare([edit], expecting: sample)
        let changed = sample + "\n# another writer\n"
        try changed.write(to: space.config, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try writer.commit(prepared)) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .changedSinceRead(space.config.path))
        }
        XCTAssertEqual(try String(contentsOf: space.config, encoding: .utf8), changed)
    }

    // MARK: How the file is replaced

    private func backups(_ space: (home: URL, config: URL, partition: URL)) -> URL {
        space.config.deletingLastPathComponent()
            .resolvingSymlinksInPath()
            .appendingPathComponent("listener/muster-config-backups", isDirectory: true)
    }

    /// The old file is kept,  the new one is 0600,  and no temporary file is left behind.
    func test_a_write_keeps_a_backup_and_leaves_no_temporary_file() throws {
        let space = try makeWorkspace()
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.apply([WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9))])

        let names = try FileManager.default.contentsOfDirectory(atPath: backups(space).path)
        XCTAssertEqual(names.count, 1)
        XCTAssertEqual(
            try String(contentsOf: backups(space).appendingPathComponent(names[0]), encoding: .utf8),
            sample
        )
        let mode = try FileManager.default.attributesOfItem(atPath: space.config.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o600)
        let left = try FileManager.default.contentsOfDirectory(atPath: space.config.deletingLastPathComponent().path)
        XCTAssertEqual(left.sorted(), ["listener", "listener.toml"])
    }

    /// A writer that got in after the last comparison is caught by the swap itself:  its text goes
    /// straight back,  and no temporary file is left behind.
    func test_the_swap_puts_back_another_writers_text() throws {
        let space = try makeWorkspace()
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.replaceFile(with: "muster's edit", expecting: "what Muster read")) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .changedSinceRead(space.config.path))
        }
        XCTAssertEqual(try String(contentsOf: space.config, encoding: .utf8), sample)
        XCTAssertEqual(
            try FileManager.default.contentsOfDirectory(atPath: space.config.deletingLastPathComponent().path),
            ["listener.toml"]
        )
    }

    /// Old copies past the newest few go.
    func test_old_backups_are_dropped() throws {
        let space = try makeWorkspace()
        try FileManager.default.createDirectory(at: backups(space), withIntermediateDirectories: true)
        for index in 0..<WakeSettingsWriter.backupsKept {
            let name = "listener.toml.20200101-0000\(String(format: "%02d", index))-aaaaaaaa"
            try "old".write(to: backups(space).appendingPathComponent(name), atomically: true, encoding: .utf8)
        }
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.apply([WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9))])
        let names = try FileManager.default.contentsOfDirectory(atPath: backups(space).path)
        XCTAssertEqual(names.count, WakeSettingsWriter.backupsKept)
        XCTAssertFalse(names.contains("listener.toml.20200101-000000-aaaaaaaa"))
    }

    /// A symlinked file stays a link,  and the file it names is what changes.
    func test_a_symlinked_file_stays_a_link() throws {
        let space = try makeWorkspace()
        let elsewhere = space.home.appendingPathComponent("real.toml")
        try FileManager.default.moveItem(at: space.config, to: elsewhere)
        try FileManager.default.createSymbolicLink(at: space.config, withDestinationURL: elsewhere)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.apply([WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9))])
        XCTAssertNoThrow(try FileManager.default.destinationOfSymbolicLink(atPath: space.config.path))
        let document = ListenerConfigDocument(text: try String(contentsOf: elsewhere, encoding: .utf8))
        XCTAssertEqual(document.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_hour"]), .number(9))
    }

    /// The candidate goes beside the file,  and the file itself does not change.
    func test_a_candidate_leaves_the_file_alone() throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        let prepared = try writer.prepare([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(9)),
        ])
        let candidate = try writer.writeCandidate(prepared)
        XCTAssertEqual(try String(contentsOf: candidate, encoding: .utf8), prepared.candidate)
        XCTAssertEqual(try bytes(space.config), before)
    }

    // MARK: The repair

    /// A refused section can be taken out,  and only a refused one.
    func test_only_a_section_the_mac_may_not_hold_can_be_removed() throws {
        let config = sample + "\n\n[seat.MA]\nbudget = { wakes_per_hour = 9 }\n"
        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.prepareRemoval(ofSeat: "MM")) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .noSectionToRemove("MM"))
        }
        XCTAssertThrowsError(try writer.prepareRemoval(ofSeat: "BF-BUILDER")) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .noSectionToRemove("BF-BUILDER"))
        }
        let prepared = try writer.prepareRemoval(ofSeat: "MA")
        try writer.commit(prepared)
        let document = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(document.seatTags, ["MM"])
        XCTAssertEqual(document.value(section: "seat.MM", key: "wake"), .text("http"))
    }

    /// A section that marks itself for the server can be taken out too,  and the rest stays.
    func test_a_section_marked_for_the_server_can_be_removed() throws {
        let config = "[daemon]\ninstance = \"mac\"\n\n[seat.MM]\nbot = \"MM\"\ninstance = \"server\"\n"
        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.commit(try writer.prepareRemoval(ofSeat: "MM"))
        let document = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(document.seatTags, [])
        XCTAssertEqual(document.value(section: "daemon", key: "instance"), .text("mac"))
    }

    /// With no readable partition,  nothing can be called refused,  so nothing is removed.
    func test_no_section_is_removed_without_a_readable_partition() throws {
        let space = try makeWorkspace(config: sample + "\n[seat.MA]\nwake = \"inbox\"\n")
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: nil)
        XCTAssertThrowsError(try writer.prepareRemoval(ofSeat: "MA"))
    }

    // MARK: The store

    /// A stand-in for `simple-chat`.
    private final class FakeListener: SimpleToolsRunning, @unchecked Sendable {
        private let lock = NSLock()
        private var calls: [[String]] = []
        private var reloaded = false
        var status = #"{"running": true, "status": {"seats": {}, "pause": {}}}"#
        var statusExit: Int32 = 0
        var statusAfterReload: String?
        var check = #"{"ok": true, "new_errors": [], "refused": []}"#
        var checkExit: Int32 = 0
        var reloadExit: Int32 = 0
        var reloadError = ""
        var resumeOutput = "resumed MM\n"
        var configAtCheck: Data?
        var candidateAtCheck: String?
        var configURL: URL?

        var invocations: [[String]] {
            lock.lock()
            defer { lock.unlock() }
            return calls
        }

        /// The commands that were not plain status reads.
        var actions: [String] {
            invocations.map { $0.dropFirst().first ?? "" }.filter { $0 != "status" }
        }

        func run(_ invocation: SimpleToolsInvocation) async throws -> SimpleToolsCLIResult {
            respond(to: invocation.arguments)
        }

        private func respond(to arguments: [String]) -> SimpleToolsCLIResult {
            lock.lock()
            defer { lock.unlock() }
            calls.append(arguments)
            let command = arguments.count > 1 ? arguments[1] : ""
            switch command {
            case "status":
                let text = reloaded ? (statusAfterReload ?? status) : status
                return SimpleToolsCLIResult(exitCode: statusExit, stdout: text, stderr: "")
            case "resume":
                return SimpleToolsCLIResult(exitCode: 0, stdout: resumeOutput, stderr: "")
            case "check-config":
                candidateAtCheck = try? String(contentsOfFile: arguments[3], encoding: .utf8)
                configAtCheck = configURL.flatMap { try? Data(contentsOf: $0) }
                return SimpleToolsCLIResult(exitCode: checkExit, stdout: check, stderr: "invalid choice")
            case "reload":
                reloaded = reloadExit == 0
                return SimpleToolsCLIResult(exitCode: reloadExit, stdout: "", stderr: reloadError)
            default:
                return SimpleToolsCLIResult(exitCode: 0, stdout: "", stderr: "")
            }
        }
    }

    /// A status published after any signal this test sends.
    private func answeredStatus(refused: [String] = [], errors: [String] = []) -> String {
        let later = Date().timeIntervalSince1970 + 3600
        func list(_ items: [String]) -> String {
            "[" + items.map { "\"\($0)\"" }.joined(separator: ",") + "]"
        }
        return """
        {"running": true, "status": {"seats": {}, "pause": {}, "updated": \(later), "reload_at": \(later), \
        "reload_refused": \(list(refused)), "config_errors": \(list(errors))}}
        """
    }

    @MainActor
    private func makeStore(
        _ listener: FakeListener,
        space: (home: URL, config: URL, partition: URL),
        partitionURL: URL?? = nil
    ) async -> WakeSettingsStore {
        listener.configURL = space.config
        let resolved: URL?
        if let partitionURL {
            resolved = partitionURL
        } else {
            resolved = space.partition
        }
        let store = WakeSettingsStore(
            location: ListenerStateLocation(home: space.home),
            runner: listener,
            partitionURL: resolved,
            reloadPollInterval: 0,
            reloadPollAttempts: 2,
            auditURL: space.home.appendingPathComponent("audit/ops-audit.jsonl")
        )
        await store.refresh(roster: ["MM", "MA"])
        return store
    }

    /// Apply on a server seat writes nothing and runs nothing,  even if a draft was somehow made.
    @MainActor
    func test_apply_on_a_server_seat_writes_nothing_and_says_why() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        let store = await makeStore(listener, space: space)
        store.draft["MA", default: [:]][.wakesPerHour] = "9"
        await store.apply(seat: "MA")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, [])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Held by the server eavesdropper") == true)
    }

    /// The kill switch does not claim a result it cannot have on a seat the Mac does not hold.
    @MainActor
    func test_pause_and_resume_do_not_run_on_a_server_seat() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        let store = await makeStore(listener, space: space)

        await store.pause(seat: "MA")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Nothing was paused for MA") == true)
        XCTAssertEqual(listener.actions, [])
        await store.resume(seat: "MA")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Held by the server eavesdropper") == true)
        XCTAssertEqual(listener.actions, [])
    }

    /// On a Mac seat the kill switch runs,  and the report names the Mac.
    @MainActor
    func test_pause_on_a_mac_seat_runs_and_names_the_mac() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        let store = await makeStore(listener, space: space)
        await store.pause(seat: "MM")
        XCTAssertEqual(listener.actions, ["pause"])
        XCTAssertFalse(store.messageIsFailure)
        XCTAssertEqual(store.message, "Wakes paused on the Mac's eavesdropper.")
        await store.resume(seat: "MM")
        XCTAssertEqual(listener.actions, ["pause", "resume"])
        XCTAssertEqual(store.message, "The pause was lifted on the Mac's eavesdropper.")
    }

    /// `daemon resume --seat S` exits 0 and says "nothing was paused" when no entry names the seat,
    /// so that is not a lifted pause,  and the report says what is still in force.
    @MainActor
    func test_a_resume_that_lifted_nothing_is_a_failure() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        listener.resumeOutput = "nothing was paused\n"
        let store = await makeStore(listener, space: space)
        await store.resume(seat: "MM")
        XCTAssertEqual(listener.actions, ["resume"])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertEqual(store.message, "Nothing was lifted for MM.\u{00A0} No pause names this seat.")

        // A current status, so it decides the pause:  with no pause file and an old status, the
        // panel would read the missing file as no pause.
        listener.status = #"{"running": true, "status": {"updated": \#(Date().timeIntervalSince1970), "seats": {}, "pause": {"*": {"wakes_only": true}}}}"#
        await store.refresh()
        XCTAssertEqual(store.model.seats.first(where: { $0.seat == "MM" })?.pauseScope, "*")
        await store.resume(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Nothing was lifted for MM") == true)
        XCTAssertTrue(store.message?.contains("A pause on every seat is still in force.") == true)
    }

    /// The seat's own entry can be lifted with a pause on every seat still in force,  and its wakes
    /// then stay stopped,  so that is not reported as a success either.
    @MainActor
    func test_a_resume_under_a_pause_on_every_seat_says_so() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        // A current status, so it decides the pause there is no pause file for.
        listener.status = #"{"running": true, "status": {"updated": \#(Date().timeIntervalSince1970), "seats": {}, "pause": {"MM": {}, "*": {}}}}"#
        let store = await makeStore(listener, space: space)
        await store.resume(seat: "MM")
        XCTAssertEqual(listener.actions, ["resume"])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("The pause on MM was lifted") == true)
        XCTAssertTrue(store.message?.contains("A pause on every seat is still in force.") == true)
    }

    /// With no partition the pause stays,  because the seat may be the Mac's,  and it says what it
    /// cannot know.  Nothing that starts or changes a seat runs,  and that includes the resume.
    @MainActor
    func test_with_no_partition_only_the_pause_runs() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        let store = await makeStore(listener, space: space, partitionURL: .some(nil))

        store.draft["MM", default: [:]][.wakesPerHour] = "9"
        await store.apply(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        await store.resume(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Nothing was resumed") == true)
        XCTAssertEqual(listener.actions, [])
        XCTAssertEqual(try bytes(space.config), before)

        await store.pause(seat: "MM")
        XCTAssertEqual(listener.actions, ["pause"])
        XCTAssertFalse(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("cannot say the Mac holds this seat") == true)
    }

    /// The eavesdropper's own check runs on the candidate before the real file changes.
    @MainActor
    func test_apply_checks_the_candidate_writes_and_reports_acceptance() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        listener.statusAfterReload = answeredStatus()
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")

        XCTAssertEqual(listener.actions, ["check-config", "reload"])
        XCTAssertEqual(listener.configAtCheck, before, "the real file changed before the check")
        XCTAssertTrue(listener.candidateAtCheck?.contains("timeout_seconds = 20") == true)
        // The check judges by the partition the writer goes by,  not the command line's own copy.
        let check = try XCTUnwrap(listener.invocations.first(where: { $0.contains("check-config") }))
        let flag = try XCTUnwrap(check.firstIndex(of: "--partition"))
        XCTAssertEqual(check[flag + 1], space.partition.path)
        let document = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(document.value(section: "seat.MM", key: "routine", subPath: ["timeout_seconds"]), .number(20))
        // The wake line is left exactly as it was.
        XCTAssertEqual(document.value(section: "seat.MM", key: "wake"), .text("http"))
        XCTAssertFalse(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("The eavesdropper accepted it.") == true)
        // The candidate is gone.
        let left = try FileManager.default.contentsOfDirectory(atPath: space.config.deletingLastPathComponent().path)
        XCTAssertFalse(left.contains { $0.contains("muster-check") })
    }

    /// The check names the partition only when there is one to name.
    func test_the_check_passes_the_partition_it_is_given() {
        XCTAssertEqual(
            SimpleToolsInvocation.checkConfig(file: "/c", partition: "/p").arguments,
            ["daemon", "check-config", "--file", "/c", "--partition", "/p", "--json"]
        )
        XCTAssertEqual(
            SimpleToolsInvocation.checkConfig(file: "/c", partition: nil).arguments,
            ["daemon", "check-config", "--file", "/c", "--json"]
        )
    }

    /// The one swap that leaves the file changed does not say it was not changed,  and the others do.
    @MainActor
    func test_a_failed_swap_does_not_say_the_file_was_not_changed() async {
        let moved = WakeSettingsWriter.WriteError.anotherWritersTextMoved(path: "/c", keptAt: "/kept")
        let report = WakeSettingsStore.commitFailure(moved)
        XCTAssertFalse(report.contains("was not changed"))
        XCTAssertTrue(report.contains("/kept"))
        XCTAssertTrue(report.contains("now holds Muster's edit"))
        let changed = WakeSettingsWriter.WriteError.changedSinceRead("/c")
        XCTAssertTrue(WakeSettingsStore.commitFailure(changed).hasPrefix("The configuration file was not changed:"))
    }

    /// A candidate the eavesdropper's rules refuse never reaches the file.
    @MainActor
    func test_a_candidate_the_listener_refuses_is_not_written() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        listener.check = #"{"ok": false, "new_errors": ["seat.MM.routine.key_env names no variable this listener has"], "refused": []}"#
        listener.checkExit = 1
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineKeyEnv] = "MM_OTHER_KEY"
        await store.apply(seat: "MM")

        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, ["check-config"])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("seat.MM.routine.key_env names no variable") == true)
    }

    /// A check that cannot answer stops the write:  an older checkout has no such command.
    @MainActor
    func test_a_check_that_cannot_answer_stops_the_write() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        listener.check = ""
        listener.checkExit = 2
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, ["check-config"])
        XCTAssertTrue(store.messageIsFailure)
    }

    /// A refusal the file already holds stops an edit too,  since no reload of it would land.
    @MainActor
    func test_a_refusal_already_in_the_file_stops_an_edit() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        listener.check = #"{"ok": false, "new_errors": [], "refused": ["seat MA is configured here"]}"#
        listener.checkExit = 1
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("seat MA is configured here") == true)
    }

    /// A refused reload shows as a failure,  not as "asked to re-read it".
    @MainActor
    func test_a_refused_reload_shows_as_a_failure() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        listener.statusAfterReload = answeredStatus(
            refused: ["seat MA is configured here"],
            errors: ["seat MA is configured here"]
        )
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("refused it") == true)
        XCTAssertTrue(store.message?.contains("seat MA is configured here") == true)
        XCTAssertFalse(store.message?.contains("asked to re-read") == true)
    }

    /// An eavesdropper that never answers is reported as exactly that.
    @MainActor
    func test_a_listener_that_does_not_answer_is_reported() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("No answer from the eavesdropper") == true)
    }

    /// A signal that could not be sent is reported,  and the file is said to be written.
    @MainActor
    func test_a_listener_that_is_not_running_is_reported() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        listener.reloadExit = 1
        listener.reloadError = "simple-chat: the listener is not running"
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("The configuration file was written.") == true)
        XCTAssertTrue(store.message?.contains("the listener is not running") == true)
    }

    /// A file another writer changed after the panel read it is not written.
    @MainActor
    func test_apply_stops_when_the_file_changed_after_the_panel_read_it() async throws {
        let space = try makeWorkspace()
        let listener = FakeListener()
        let store = await makeStore(listener, space: space)
        let changed = sample + "\n# another writer\n"
        try changed.write(to: space.config, atomically: true, encoding: .utf8)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertEqual(try String(contentsOf: space.config, encoding: .utf8), changed)
        XCTAssertEqual(listener.actions, [])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("changed after Muster read it") == true)
    }

    /// The repair takes out a section the eavesdropper refuses,  through the same check and report.
    @MainActor
    func test_the_repair_removes_a_refused_section() async throws {
        let space = try makeWorkspace(config: sample + "\n\n[seat.MA]\nbudget = { wakes_per_hour = 9 }\n")
        let listener = FakeListener()
        // The check still counts another refusal the file had,  and a repair is not stopped by it.
        listener.check = #"{"ok": false, "new_errors": [], "refused": ["seat KIMI is configured here"]}"#
        listener.checkExit = 1
        listener.statusAfterReload = answeredStatus()
        let store = await makeStore(listener, space: space)
        XCTAssertEqual(store.model.seats.first(where: { $0.seat == "MA" })?.hasRefusedSection, true)

        await store.removeRefusedSection(seat: "MA")
        let document = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(document.seatTags, ["MM"])
        XCTAssertEqual(listener.actions, ["check-config", "reload"])
        XCTAssertFalse(store.messageIsFailure)
        XCTAssertEqual(store.model.seats.first(where: { $0.seat == "MA" })?.hasRefusedSection, false)
    }

    /// The partition the eavesdropper says it reads decides,  over the copy in Muster's own checkout:
    /// the panel,  the repair and the writer's gate all go by it,  so a stale checkout cannot take
    /// out a seat the eavesdropper holds.
    @MainActor
    func test_the_listeners_own_partition_decides_who_holds_a_seat() async throws {
        let space = try makeWorkspace(config: sample + "\n\n[seat.MA]\nbot = \"MA\"\n")
        let before = try bytes(space.config)
        let listeners = space.home.appendingPathComponent("listeners-partition.toml")
        try "[seats]\nMM = \"mac\"\nMA = \"mac\"\n".write(to: listeners, atomically: true, encoding: .utf8)
        let listener = FakeListener()
        listener.status = #"{"running": true, "status": {"seats": {}, "pause": {}, "partition_file": "\#(listeners.path)"}}"#
        let store = await makeStore(listener, space: space)

        let seat = try XCTUnwrap(store.model.seats.first(where: { $0.seat == "MA" }))
        XCTAssertEqual(seat.host, .mac)
        XCTAssertFalse(seat.hasRefusedSection)

        await store.removeRefusedSection(seat: "MA")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, [])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Nothing was removed for MA") == true)
    }

    /// A partition the eavesdropper names and Muster cannot read holds no seat here,  the Mac's included.
    @MainActor
    func test_a_listeners_partition_that_cannot_be_read_holds_no_seat() async throws {
        let space = try makeWorkspace()
        let before = try bytes(space.config)
        let listener = FakeListener()
        let missing = space.home.appendingPathComponent("gone.toml").path
        listener.status = #"{"running": true, "status": {"seats": {}, "pause": {}, "partition_file": "\#(missing)"}}"#
        let store = await makeStore(listener, space: space)
        store.draft["MM", default: [:]][.routineTimeout] = "20"
        await store.apply(seat: "MM")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, [])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("Muster cannot say who holds this seat") == true)
    }

    /// The repair of a seat an older running eavesdropper holds is refused before anything is written.
    @MainActor
    func test_the_repair_stops_on_a_seat_an_older_running_listener_holds() async throws {
        let space = try makeWorkspace(config: sample + "\n\n[seat.MA]\nbot = \"MA\"\n")
        let before = try bytes(space.config)
        let listener = FakeListener()
        listener.status = #"{"running": true, "status": {"seats": {"MA": {"bot": "MA"}}, "pause": {}}}"#
        let store = await makeStore(listener, space: space)
        await store.removeRefusedSection(seat: "MA")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, [])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("running eavesdropper holds this seat") == true)
    }

    /// With no status,  the panel does not offer a repair and the store does not run one:  a running
    /// eavesdropper may read another copy that gives this seat to the Mac.
    @MainActor
    func test_the_repair_stops_when_the_status_cannot_be_read() async throws {
        let space = try makeWorkspace(config: sample + "\n\n[seat.MA]\nbot = \"MA\"\n")
        let before = try bytes(space.config)
        let listener = FakeListener()
        listener.statusExit = 1
        let store = await makeStore(listener, space: space)
        XCTAssertEqual(store.model.seats.first(where: { $0.seat == "MA" })?.hasRefusedSection, false)
        await store.removeRefusedSection(seat: "MA")
        XCTAssertEqual(try bytes(space.config), before)
        XCTAssertEqual(listener.actions, [])
        XCTAssertTrue(store.messageIsFailure)
        XCTAssertTrue(store.message?.contains("could not read the eavesdropper's status") == true)
    }

    // MARK: What the reader reads

    /// The reader and the builder agree on who holds a seat:  a seat the builder shows live data for
    /// has its ledger read,  so no empty reading stands in for one Muster never made.
    func test_the_reader_reads_the_ledger_for_a_seat_the_builder_does_not_call_elsewhere() async throws {
        let space = try makeWorkspace(config: sample + "\n\n[seat.MA]\nbot = \"MA\"\nwake = \"muse\"\n")
        let location = ListenerStateLocation(home: space.home)
        let ledgerURL = location.ledgerURL(seat: "MA")
        try FileManager.default.createDirectory(
            at: ledgerURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try "{\"ts\": 1.0, \"wake_id\": \"a\", \"state\": \"accepted\"}\n"
            .write(to: ledgerURL, atomically: true, encoding: .utf8)

        // A running eavesdropper too old to name its partition holds MA,  and this copy gives it away.
        let older = FakeListener()
        older.status = #"{"running": true, "status": {"seats": {"MA": {"bot": "MA"}}, "pause": {}}}"#
        let held = await WakeSettingsReader(
            location: location, runner: older, roster: ["MA"], partitionURL: space.partition
        ).read()
        XCTAssertNotNil(held.usage["MA"])
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(held).seats.first(where: { $0.seat == "MA" }))
        XCTAssertFalse(seat.host.isElsewhere)
        // An old wake line with no routine is one today's listener still wakes the seat by.
        XCTAssertEqual(seat.kind, .oldWakeSetting(.stillWakes("muse")))
        XCTAssertEqual(seat.leftoverWake, "muse")

        // Once it names the partition,  that copy gives MA away,  so nothing is read for it.
        let named = FakeListener()
        named.status = #"{"running": true, "status": {"seats": {"MA": {"bot": "MA"}}, "pause": {}, "partition_file": "\#(space.partition.path)"}}"#
        let elsewhere = await WakeSettingsReader(
            location: location, runner: named, roster: ["MA"], partitionURL: space.partition
        ).read()
        XCTAssertNil(elsewhere.usage["MA"])
    }
    // MARK: What a draft asks for

    /// A dash and an empty field both take the setting out.
    func test_clearing_a_setting_removes_it() async {
        let reading = await WakeSettingsStore.readDraft(
            seat: "MM",
            values: [.routineTimeout: WakeSetting.unsetText, .wakesPerHour: ""],
            before: [.routineTimeout: "15", .wakesPerHour: "5"]
        )
        XCTAssertEqual(reading.edits, [
            WakeSettingsWriter.Edit(seat: "MM", key: .routineTimeout, value: nil),
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: nil),
        ])
        XCTAssertEqual(reading.refusals, [])
    }

    /// The routine's variables are names the eavesdropper accepts, never a value and never the
    /// CLI's own single-seat variable, and its timeout is from 1 to 60 seconds, as `_routine` says.
    func test_a_routine_field_the_listener_would_refuse_is_refused() async {
        let good = await WakeSettingsStore.readDraft(
            seat: "MM",
            values: [.routineURLEnv: "MM_ROUTER_URL", .routineKeyEnv: "_KEY_2", .routineTimeout: "60"],
            before: [.routineURLEnv: "MM_ROUTINE_URL", .routineKeyEnv: "MM_ROUTINE_KEY", .routineTimeout: "15"]
        )
        XCTAssertEqual(good.refusals, [])
        XCTAssertEqual(good.edits.count, 3)
        for bad in ["https://router.example", "mm_routine_url", "1MM", "MM-URL", "ZULIP_API_KEY", "ZULIP_EMAIL"] {
            let reading = await WakeSettingsStore.readDraft(
                seat: "MM", values: [.routineURLEnv: bad], before: [.routineURLEnv: "MM_ROUTINE_URL"]
            )
            XCTAssertEqual(reading.edits, [], bad)
            XCTAssertEqual(reading.refusals.count, 1, bad)
            XCTAssertTrue(reading.refusals.first?.contains("never its value") == true, bad)
        }
        for bad in ["0", "0.5", "61", "-1", "soon"] {
            let reading = await WakeSettingsStore.readDraft(
                seat: "MM", values: [.routineTimeout: bad], before: [.routineTimeout: "15"]
            )
            XCTAssertEqual(reading.edits, [], bad)
            XCTAssertEqual(reading.refusals.count, 1, bad)
        }
    }

    /// The method, the signing and the header are fixed by the router contract, so a draft of them
    /// is refused by name and never written.
    func test_the_contract_keys_are_refused() async {
        let reading = await WakeSettingsStore.readDraft(
            seat: "MM",
            values: [.routineMethod: "PUT", .routineAuth: "header", .routineHeader: "X-Key"],
            before: [.routineMethod: WakeSetting.unsetText, .routineAuth: WakeSetting.unsetText, .routineHeader: WakeSetting.unsetText]
        )
        XCTAssertEqual(reading.edits, [])
        XCTAssertEqual(reading.refusals.count, 3)
        XCTAssertTrue(reading.refusals.allSatisfy { $0.contains("router contract") })
    }

    /// An unchanged value writes nothing.
    func test_an_unchanged_draft_asks_for_nothing() async {
        let reading = await WakeSettingsStore.readDraft(seat: "MM", values: [.routineURLEnv: "X"], before: [.routineURLEnv: "X"])
        XCTAssertEqual(reading, WakeSettingsStore.DraftReading())
    }

    /// A live cap is a whole number,  because the eavesdropper ignores anything else.
    func test_a_live_cap_must_be_whole_and_a_budget_need_not_be() {
        XCTAssertNil(WakeSettingKey.livePerHour.value(from: "2.5"))
        XCTAssertEqual(WakeSettingKey.livePerHour.value(from: "2"), .number(2))
        XCTAssertEqual(WakeSettingKey.wakesPerHour.value(from: "2.5"), .number(2.5))
        XCTAssertEqual(WakeSettingKey.routineTimeout.value(from: "2.5"), .number(2.5))
        XCTAssertEqual(WakeSettingKey.routineURLEnv.value(from: " MM_URL "), .text("MM_URL"))
    }
    /// One seat's row.
    private func row(
        _ key: WakeSettingKey,
        config: String? = nil,
        status: String? = nil
    ) -> WakeSetting {
        let model = WakeSettingsBuilder.build(
            sources(config: config, status: status)
        )
        guard let seat = model.seats.first,
              let setting = seat.settings.first(where: { $0.key == key }) else {
            fatalError("the row for \(key.rawValue) should exist")
        }
        return setting
    }

    // MARK: The two sources

    /// A setting the file and the eavesdropper agree on is a pass.
    func test_a_setting_the_listener_matches_is_a_pass() {
        let setting = row(.wakesPerHour, status: statusJSON)
        XCTAssertEqual(setting.state, .match)
        XCTAssertEqual(setting.configured, .number(5))
        XCTAssertEqual(setting.live, .number(5))
    }

    /// A setting the eavesdropper does not match is DRIFT,  and never a quiet green.
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

    /// A setting the file does not hold is the eavesdropper's default,  and says so.
    func test_a_setting_the_file_does_not_hold_is_the_default() throws {
        let without = """
        [seat.GB-COMPILER]
        bot = "GB-Compiler"
        routine = {}
        """
        var input = sources(config: without, status: statusJSON)
        input.seats = ["GB-COMPILER"]
        input.partition = ListenerPartition(seats: ["GB-COMPILER": "mac"])
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(input).seats.first)
        let url = try XCTUnwrap(seat.settings.first(where: { $0.key == .routineURLEnv }))
        XCTAssertEqual(url.state, .unset)
        // The eavesdropper's own default, `env_code(seat) + "_ROUTINE_URL"`.
        XCTAssertTrue(url.note.contains("GB_COMPILER_ROUTINE_URL"), url.note)
        let header = try XCTUnwrap(seat.settings.first(where: { $0.key == .routineHeader }))
        XCTAssertTrue(header.note.contains("Authorization"), header.note)
        XCTAssertNil(header.contractDifference)
    }

    /// The eavesdropper does not publish the routine's settings,  only whether it can be reached,
    /// so the panel says so instead of claiming the file's value is in effect.
    func test_a_setting_the_listener_does_not_publish_is_not_reported() {
        let setting = row(.routineURLEnv, status: statusJSON)
        XCTAssertEqual(setting.state, .unpublished)
        XCTAssertNil(setting.live)
        XCTAssertEqual(setting.configured, .text("MM_ROUTINE_URL"))
        XCTAssertFalse(setting.state.isProblem)
    }

    /// The dollar cap is read but never enforced,  so Muster will not write it.
    func test_the_dollar_cap_is_not_owned_and_is_refused() {
        let setting = row(.usdPerDay, status: statusJSON)
        XCTAssertEqual(setting.state, .notOwned)
        XCTAssertEqual(WakeSettingKey.usdPerDay.isEnforced, false)
        XCTAssertNotNil(WakeSettingKey.usdPerDay.refusalReason)
        XCTAssertNil(WakeSettingKey.wakesPerHour.refusalReason)
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

    /// Without the eavesdropper's status,  every live value is unknown,  and that is a problem:  it
    /// never reads as the eavesdropper not publishing the setting.
    func test_without_the_status_the_panel_shows_no_live_value() {
        let setting = row(.wakesPerHour)
        XCTAssertNil(setting.live)
        XCTAssertEqual(setting.state, .unknown)
        XCTAssertTrue(setting.state.isProblem)
        XCTAssertFalse(setting.note.contains("does not publish"))
    }

    /// A status that does not mention the seat is unknown for that seat too.
    func test_a_status_that_does_not_report_the_seat_is_unknown() {
        let status = #"{"running": true, "status": {"seats": {}}}"#
        XCTAssertEqual(row(.wakesPerHour, status: status).state, .unknown)
    }

    /// A status that reports the seat without a setting really does not publish it.  That is not
    /// a setting the engine does not enforce, so it is NOT REPORTED, not NOT ENFORCED.
    func test_a_status_that_reports_the_seat_without_the_setting_does_not_publish_it() {
        let status = #"{"running": true, "status": {"seats": {"MM": {"bot": "MM"}}}}"#
        let setting = row(.wakesPerHour, status: status)
        XCTAssertEqual(setting.state, .unpublished)
        XCTAssertFalse(setting.state.isProblem)
        XCTAssertFalse(setting.note.contains("enforce"))
    }

    /// A running eavesdropper lists a seat the file turns off under `disabled` and not under
    /// `seats`,  so its settings are neither unknown nor a problem.  A stopped one has said nothing.
    func test_a_seat_the_file_turns_off_is_not_a_problem() throws {
        let config = "[seat.MM]\nbot = \"MM\"\nenabled = false\nroutine = { url_env = \"MM_ROUTINE_URL\" }\nbudget = { wakes_per_hour = 5 }\n"
        let running = #"{"running": true, "status": {"disabled": ["MM"], "seats": {}}}"#
        let setting = row(.wakesPerHour, config: config, status: running)
        XCTAssertEqual(setting.state, .seatOff)
        XCTAssertFalse(setting.state.isProblem)
        XCTAssertFalse(setting.note.contains("not known"))
        // The routine a seat that is off holds is not in use either.
        XCTAssertEqual(row(.routineURLEnv, config: config, status: running).state, .seatOff)
        let seat = try XCTUnwrap(WakeSettingsBuilder.build(sources(config: config, status: running)).seats.first)
        XCTAssertTrue(seat.disabledInFile)
        XCTAssertFalse(seat.needsAttention)
        // A setting the file does not hold is still the default.
        XCTAssertEqual(row(.wakesPerDay, config: config, status: running).state, .unset)

        let stopped = #"{"running": false, "status": {"disabled": ["MM"], "seats": {}}}"#
        XCTAssertEqual(row(.wakesPerHour, config: config, status: stopped).state, .unknown)
    }

    /// With no status,  the dollar cap is still the setting the engine does not enforce.
    func test_without_the_status_the_dollar_cap_is_still_not_owned() {
        XCTAssertEqual(row(.usdPerDay).state, .notOwned)
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
        let space = try makeWorkspace()
        let url = space.config

        let writer = WakeSettingsWriter(configURL: url, partitionURL: space.partition)
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
        XCTAssertEqual(document.value(section: "seat.MM", key: "wake"), .text("http"))
    }

    /// The file the writer leaves behind is one the runtime can read.
    func test_a_written_file_reads_back_the_way_the_runtime_writes_it() throws {
        let space = try makeWorkspace()
        let url = space.config

        let writer = WakeSettingsWriter(configURL: url, partitionURL: space.partition)
        try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .routineURLEnv, value: .text("MM_ROUTER_URL")),
            WakeSettingsWriter.Edit(seat: "MM", key: .routineTimeout, value: .number(30)),
        ])

        let document = ListenerConfigDocument(
            text: try String(contentsOf: url, encoding: .utf8)
        )
        XCTAssertEqual(document.value(section: "seat.MM", key: "routine", subPath: ["url_env"]), .text("MM_ROUTER_URL"))
        XCTAssertEqual(document.value(section: "seat.MM", key: "routine", subPath: ["timeout_seconds"]), .number(30))
        XCTAssertEqual(document.value(section: "seat.MM", key: "routine", subPath: ["key_env"]), .text("MM_ROUTINE_KEY"))
        XCTAssertEqual(document.problems, [])
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

    /// The CLI executable and commandLine use `simple-chat`.
    func test_the_command_line_uses_simple_chat() {
        let root = URL(fileURLWithPath: "/tmp/fake-repo", isDirectory: true)
        let interpreter = URL(fileURLWithPath: "/usr/bin/python3")
        let location = EngineLocation(root: root, interpreter: interpreter)
        XCTAssertEqual(
            SimpleToolsCLI.commandURL(at: location).path,
            "/tmp/fake-repo/scripts/simple-chat"
        )
        XCTAssertEqual(
            SimpleToolsInvocation.status().commandLine,
            "simple-chat daemon status --json"
        )
        XCTAssertTrue(
            SimpleToolsError.commandNotFound("/tmp/fake-repo/scripts/simple-chat")
                .errorDescription?
                .contains("simple-chat") == true
        )
    }

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

    /// Operator red warnings, held-back queue totals, chat nudges, and quota are decoded into WakeSeat.
    func test_operator_telemetry_populates_wake_seat() {
        let richStatus = """
        {
          "running": true,
          "pid": 59735,
          "status": {
            "seats": {
              "MM": {
                "bot": "MM",
                "wake": "muse",
                "connected": true,
                "paused": false,
                "wakes_paused": false,
                "red": ["wake pin is stale"],
                "held_back": {
                  "count": 2,
                  "totals": { "held": 15, "surfaced": 13, "expired": 0, "evicted": 0, "overflowed": 0 }
                },
                "chat_nudges": {
                  "accepted": 3,
                  "pending": 4,
                  "blocked": 1
                },
                "quota": {
                  "known": true,
                  "exhausted": false,
                  "remaining": 85.0,
                  "source": "codecaps"
                }
              }
            }
          }
        }
        """
        let model = WakeSettingsBuilder.build(sources(status: richStatus))
        guard let seat = model.seats.first else {
            XCTFail("expected MM seat")
            return
        }
        XCTAssertEqual(seat.redWarnings, ["wake pin is stale"])
        XCTAssertEqual(seat.heldBackQueueCount, 2)
        XCTAssertEqual(seat.heldBackTotalCount, 15)
        XCTAssertEqual(seat.heldBackSurfacedCount, 13)
        XCTAssertEqual(seat.chatNudgesAccepted, 3)
        XCTAssertEqual(seat.chatNudgesPending, 4)
        XCTAssertEqual(seat.chatNudgesBlocked, 1)
        XCTAssertEqual(seat.quotaRemaining, 85.0)
        XCTAssertFalse(seat.quotaExhausted)
        XCTAssertEqual(seat.quotaSource, "codecaps")

        let exhaustedNoPercentStatus = """
        {
          "running": true,
          "status": {
            "seats": {
              "MM": {
                "bot": "MM",
                "quota": {
                  "known": true,
                  "exhausted": true,
                  "remaining": null,
                  "source": "provider_quota"
                }
              }
            }
          }
        }
        """
        let exhaustedModel = WakeSettingsBuilder.build(sources(status: exhaustedNoPercentStatus))
        guard let exhaustedSeat = exhaustedModel.seats.first else {
            XCTFail("expected MM seat in exhausted status")
            return
        }
        XCTAssertNil(exhaustedSeat.quotaRemaining)
        XCTAssertTrue(exhaustedSeat.quotaExhausted)
        XCTAssertEqual(exhaustedSeat.quotaSource, "provider_quota")
    }

    // MARK: The routine table

    /// `_routine`'s own rules, as Muster mirrors them:  every problem the listener names, in its
    /// own words, and none for a table it accepts.
    func test_the_routine_rules_match_the_listener() {
        XCTAssertEqual(WakeRoutineRules.envCode("GB-COMPILER"), "GB_COMPILER")
        XCTAssertEqual(WakeRoutineRules.envCode("mm"), "MM")
        XCTAssertEqual(WakeRoutineRules.problems(seat: "MM", routine: [:]), [])
        XCTAssertEqual(WakeRoutineRules.problems(seat: "MM", routine: [
            "url_env": .text("MM_ROUTINE_URL"), "key_env": .text("MM_ROUTINE_KEY"),
            "method": .text("post"), "auth": .text("bearer"), "header": .text("Authorization"),
            "timeout_seconds": .number(60), "cost_usd": .number(0.5),
        ]), [])
        func problems(_ routine: [String: ListenerConfigValue]) -> [String] {
            WakeRoutineRules.problems(seat: "MM", routine: routine)
        }
        XCTAssertEqual(problems(["colour": .text("x")]), ["seat.MM.routine.colour is not a known routine setting"])
        XCTAssertEqual(problems(["url_env": .text("lower")]), ["seat.MM.routine.url_env must be an environment variable name (upper case letters, digits and _)"])
        XCTAssertEqual(problems(["key_env": .number(5)]), ["seat.MM.routine.key_env must be an environment variable name (upper case letters, digits and _)"])
        XCTAssertTrue(problems(["key_env": .text("ZULIP_RC")]).first?.contains("single-seat variable") == true)
        XCTAssertEqual(problems(["url_env": .text("SAME"), "key_env": .text("SAME")]), ["seat.MM.routine.url_env and key_env must be different variables"])
        // The defaults count too:  naming the default key variable as the address is a clash.
        XCTAssertEqual(problems(["url_env": .text("MM_ROUTINE_KEY")]), ["seat.MM.routine.url_env and key_env must be different variables"])
        XCTAssertEqual(problems(["method": .text("GET")]), ["seat.MM.routine.method must be one of POST, PUT, PATCH"])
        XCTAssertEqual(problems(["auth": .text("basic")]), ["seat.MM.routine.auth must be one of header, bearer, hmac-sha256"])
        XCTAssertTrue(problems(["header": .text("Content-Type")]).first?.contains("header must be an HTTP header name") == true)
        XCTAssertTrue(problems(["header": .text("X Key")]).first?.contains("header must be an HTTP header name") == true)
        XCTAssertEqual(problems(["signature_prefix": .text("v1=")]), ["seat.MM.routine.signature_prefix applies only to auth = \"hmac-sha256\""])
        XCTAssertEqual(problems(["signature_prefix": .text("v1="), "auth": .text("hmac-sha256")]), [])
        XCTAssertEqual(problems(["signature_prefix": .text("a b")]), ["seat.MM.routine.signature_prefix must be up to 32 letters, digits or =:_.-"])
        XCTAssertEqual(problems(["timeout_seconds": .number(0.5)]), ["seat.MM.routine.timeout_seconds must be a number of at least 1"])
        XCTAssertEqual(problems(["timeout_seconds": .number(61)]), ["seat.MM.routine.timeout_seconds must be 60 or less"])
        XCTAssertEqual(problems(["timeout_seconds": .bool(true)]), ["seat.MM.routine.timeout_seconds must be a number of at least 1"])
        XCTAssertEqual(problems(["cost_usd": .number(-1)]), ["seat.MM.routine.cost_usd must be a number of at least 0"])
    }

    /// Muster never writes a `wake` line:  no setting it knows lives there, and a write to a seat
    /// whose section still holds one leaves that line exactly as it was.
    func test_muster_never_writes_a_wake_key() throws {
        for key in WakeSettingKey.allCases {
            XCTAssertNotEqual(key.configLocation.key, "wake", key.rawValue)
            XCTAssertFalse(key.configLocation.subPath.contains("wake"), key.rawValue)
        }
        let config = "[seat.MM]\nbot = \"MM\"\nwake = \"muse\"\nroutine = { url_env = \"MM_ROUTINE_URL\" }\n"
        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .routineKeyEnv, value: .text("MM_SIGNING_KEY")),
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(4)),
        ])
        let written = try String(contentsOf: space.config, encoding: .utf8)
        XCTAssertTrue(written.contains("wake = \"muse\"\n"))
        XCTAssertEqual(written.components(separatedBy: "wake =").count, 2, written)
        let document = ListenerConfigDocument(text: written)
        XCTAssertEqual(document.value(section: "seat.MM", key: "routine", subPath: ["key_env"]), .text("MM_SIGNING_KEY"))
        XCTAssertEqual(document.problems, [])
    }

    /// A routine under its own `[seat.X.routine]` header is written there, in the form the file
    /// already has, and never as a second, inline table.
    func test_a_routine_under_its_own_header_keeps_its_form() throws {
        let config = """
        [seat.MM]
        bot = "MM"

        [seat.MM.routine]
        url_env = "MM_ROUTINE_URL"
        # the key the router signs with
        key_env = "MM_ROUTINE_KEY"
        """
        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .routineKeyEnv, value: .text("MM_SIGNING_KEY")),
            WakeSettingsWriter.Edit(seat: "MM", key: .routineTimeout, value: .number(20)),
        ])
        let written = try String(contentsOf: space.config, encoding: .utf8)
        let document = ListenerConfigDocument(text: written)
        XCTAssertNil(document.assignment(section: "seat.MM", key: "routine"))
        XCTAssertEqual(document.routineForm(seat: "MM"), .subTable)
        XCTAssertEqual(document.value(section: "seat.MM.routine", key: "key_env"), .text("MM_SIGNING_KEY"))
        XCTAssertEqual(document.value(section: "seat.MM.routine", key: "timeout_seconds"), .number(20))
        XCTAssertTrue(written.contains("# the key the router signs with"))

        // Taking a key out of a table under its own header leaves the header, so the seat still Wakes.
        try writer.apply([WakeSettingsWriter.Edit(seat: "MM", key: .routineTimeout, value: nil)])
        let after = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(after.routineForm(seat: "MM"), .subTable)
        XCTAssertNil(after.value(section: "seat.MM.routine", key: "timeout_seconds"))
    }

    /// A seat that Captures Only is never given a routine here, a routine Muster cannot read is
    /// never written, the last setting of an inline table is never taken out, and a table the
    /// listener would refuse never reaches the file.
    func test_the_writer_never_changes_what_a_seat_is() throws {
        let config = """
        [seat.MM]
        bot = "MM"
        wake = "muse"

        [seat.MC]
        bot = "MC"
        routine = { url_env = "MC_ROUTINE_URL" }

        [seat.ZC]
        bot = "ZC"
        routine = "https://router.example"
        """
        let space = try makeWorkspace(config: config)
        try "[seats]\nMM = \"mac\"\nMC = \"mac\"\nZC = \"mac\"\n".write(to: space.partition, atomically: true, encoding: .utf8)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .routineURLEnv, value: .text("MM_ROUTINE_URL")),
        ])) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .noRoutineTable("MM"))
        }
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "ZC", key: .routineTimeout, value: .number(20)),
        ])) { error in
            guard case WakeSettingsWriter.WriteError.unreadableRoutine(let seat, _) = error else {
                return XCTFail("the wrong refusal: \(error)")
            }
            XCTAssertEqual(seat, "ZC")
        }
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MC", key: .routineURLEnv, value: nil),
        ])) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .lastRoutineSetting("MC"))
        }
        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MC", key: .routineKeyEnv, value: .text("MC_ROUTINE_URL")),
        ])) { error in
            XCTAssertEqual(
                error as? WakeSettingsWriter.WriteError,
                .routineRefused(seat: "MC", problems: ["seat.MC.routine.url_env and key_env must be different variables"])
            )
        }
        XCTAssertEqual(try String(contentsOf: space.config, encoding: .utf8), config)
        // A cap on a seat that Captures Only is still written, as it was before.
        XCTAssertNoThrow(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .wakesPerHour, value: .number(3)),
        ]))
        let document = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(document.routineForm(seat: "MM"), .absent)
    }

    // MARK: The review round on #1720

    /// The eavesdropper refuses a wake once its count is at or over a cap (`wakes.py` and
    /// `live.py` compare with `>=`), so 0 stops every wake, and Muster refuses it with why.  The
    /// live loop turns and minutes per topic treat 0 as off and as no spacing, so 0 stays a real
    /// value for those two.
    func test_a_zero_cap_that_would_stop_every_wake_is_refused() async {
        let stopping: [WakeSettingKey] = [
            .wakesPerHour, .wakesPerDay, .perTopicPerHour, .ownerPerDay, .ownerPerTopicPerHour,
            .livePerHour, .livePerDay, .liveOwnerPerDay,
        ]
        for key in stopping {
            let reading = await WakeSettingsStore.readDraft(seat: "MM", values: [key: "0"], before: [key: "5"])
            XCTAssertEqual(reading.edits, [], key.rawValue)
            XCTAssertEqual(reading.refusals, ["\(key.title): 0 would stop every wake.\u{00A0} A no-limit setting waits on the listener change."], key.rawValue)
            XCTAssertTrue(key.zeroStopsEveryWake, key.rawValue)
            // Any other number is still a cap.
            let one = await WakeSettingsStore.readDraft(seat: "MM", values: [key: "1"], before: [key: "5"])
            XCTAssertEqual(one.edits, [WakeSettingsWriter.Edit(seat: "MM", key: key, value: .number(1))], key.rawValue)
        }
        for key in [WakeSettingKey.liveLoopTurns, .livePerTopicMinutes] {
            XCTAssertFalse(key.zeroStopsEveryWake, key.rawValue)
            let reading = await WakeSettingsStore.readDraft(seat: "MM", values: [key: "0"], before: [key: "3"])
            XCTAssertEqual(reading.edits, [WakeSettingsWriter.Edit(seat: "MM", key: key, value: .number(0))], key.rawValue)
        }
    }

    /// One batch that clears the only key of an inline routine table and sets another is judged on
    /// the table it leaves:  every set lands before any clear, so the table keeps the new key and is
    /// never taken out.  Clearing the only key with nothing set beside it is still refused.
    func test_a_clear_and_a_set_in_one_batch_keep_a_one_key_routine_table() throws {
        let config = "[seat.MM]\nbot = \"MM\"\nroutine = { url_env = \"MM_ROUTINE_URL\" }\n"
        let space = try makeWorkspace(config: config)
        let writer = WakeSettingsWriter(configURL: space.config, partitionURL: space.partition)
        // The clear is listed first, as the draft lists the keys:  the writer still sets first.
        try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .routineURLEnv, value: nil),
            WakeSettingsWriter.Edit(seat: "MM", key: .routineKeyEnv, value: .text("MM_SIGNING_KEY")),
        ])
        let document = ListenerConfigDocument(text: try String(contentsOf: space.config, encoding: .utf8))
        XCTAssertEqual(document.routineForm(seat: "MM"), .inline)
        XCTAssertEqual(document.routineTable(seat: "MM"), ["key_env": .text("MM_SIGNING_KEY")])
        XCTAssertEqual(document.problems, [])

        XCTAssertThrowsError(try writer.apply([
            WakeSettingsWriter.Edit(seat: "MM", key: .routineKeyEnv, value: nil),
        ])) { error in
            XCTAssertEqual(error as? WakeSettingsWriter.WriteError, .lastRoutineSetting("MM"))
        }
    }
}
