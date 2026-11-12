import XCTest

/// The one-listener cutover of Nov 10:  the server listener holds every seat, and the Mac's
/// listener is retired.  The Wake & Listener panel says so, never shows the retired listener's
/// last status as current, and offers no edit for a seat the server holds.  Every partition here
/// is a fixture, as the other partition tests use.
final class OneListenerCutoverTests: XCTestCase {
    // MARK: Fixtures

    /// Only the Operator's service identity stays on the Mac.
    private let allServer = ListenerPartition(seats: [
        "OPERATOR": "mac", "CLAUDE": "server", "MM": "server", "BF-BUILDER": "none",
    ])

    /// The partition from before the cutover:  MM still on the Mac.
    private let beforeCutover = ListenerPartition(seats: [
        "OPERATOR": "mac", "MM": "mac", "CLAUDE": "server",
    ])

    /// When the retired listener last wrote its status.
    private let lastWritten: Double = 1_794_374_880

    /// The Mac's file after the cutover:  no `[seat.*]` table.
    private let config = "[daemon]\nhost_wake_load_per_core = 0\n"

    /// The retired listener's last status:  not running, and still listing the seats it held.
    private func staleStatus(updated: Double) -> String {
        """
        {"running": false, "pid": null, "status": {"instance": "mac", "updated": \(updated),
          "config_errors": ["seat CLAUDE is configured here (the 'mac' listener)"],
          "operator_voice": {"available": true, "code": null, "since": null},
          "seats": {"CLAUDE": {"bot": "CLAUDE", "connected": true, "wakes_24h": 4},
                    "MM": {"bot": "MM", "connected": true}}}}
        """
    }

    /// A running listener's status, written at `updated`.
    private func runningStatus(updated: Double) -> String {
        """
        {"running": true, "pid": 4242, "status": {"instance": "mac", "updated": \(updated),
          "config_errors": ["seat CLAUDE is configured here (the 'mac' listener)"],
          "operator_voice": {"available": true, "code": null, "since": null},
          "seats": {"CLAUDE": {"bot": "CLAUDE", "connected": true}}}}
        """
    }

    private func sources(status json: String?, partition: ListenerPartition, readAt: Double) -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        sources.config = ListenerConfigDocument(text: config)
        sources.configReadable = true
        sources.partition = partition
        sources.seats = ["CLAUDE", "MM"]
        sources.readAt = Date(timeIntervalSince1970: readAt)
        if let json {
            sources.status = try? JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
            XCTAssertNotNil(sources.status, "the fixture status must decode")
        }
        return sources
    }

    // MARK: The partition

    /// The Operator is a service identity, not an agent seat, so a partition that gives the Mac
    /// only the Operator gives it no agent seat.  A partition that could not be read says nothing.
    func test_the_partition_says_when_the_mac_holds_no_agent_seat() {
        XCTAssertEqual(allServer.macAgentSeats, [])
        XCTAssertTrue(allServer.givesTheMacNoAgentSeat)
        XCTAssertEqual(beforeCutover.macAgentSeats, ["MM"])
        XCTAssertFalse(beforeCutover.givesTheMacNoAgentSeat)
        let unread = ListenerPartition(unreadable: "It is missing.")
        XCTAssertNil(unread.macAgentSeats)
        XCTAssertFalse(unread.givesTheMacNoAgentSeat)
        // The repository's own partition is the one-listener partition.
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let repository = ListenerPartition.load(from: root.appendingPathComponent(ListenerPartition.relativePath))
        XCTAssertNil(repository.problem)
        XCTAssertTrue(repository.givesTheMacNoAgentSeat)
    }

    // MARK: A stale status under an all-server partition

    /// The retired listener's last status reads as retired:  not a live listener with stale seats,
    /// and not an error.  Nothing in it is shown as current.
    func test_a_stale_mac_status_under_an_all_server_partition_reads_as_retired() throws {
        let model = WakeSettingsBuilder.build(
            sources(status: staleStatus(updated: lastWritten), partition: allServer, readAt: lastWritten + 36_000))
        XCTAssertTrue(model.macListenerRetired)
        XCTAssertEqual(model.runState, .stopped)
        XCTAssertFalse(model.statusIsCurrent)
        XCTAssertEqual(model.listenerTitle, "The Mac's eavesdropper is retired:  the server listener holds every seat")
        XCTAssertNotEqual(model.listenerTitle, ListenerRunState.stopped.title)
        XCTAssertEqual(model.retiredNote, WakeSettingsModel.retiredNoteText)
        // Its old problems and its old word on the Operator are not shown as current.
        XCTAssertEqual(model.listenerErrors, [])
        XCTAssertEqual(model.operatorVoice, .notRead(WakeSettingsModel.retiredOperatorVoice))
        XCTAssertFalse(model.operatorVoice.isProblem)
        XCTAssertTrue(model.operatorVoice.problemLines(seats: model.seats).isEmpty)
        XCTAssertTrue(model.operatorVoice.sentence().contains("Seats panel"))
        XCTAssertNil(model.rewake)
        XCTAssertEqual(model.problems, [])
        // Its seats are the server's:  nothing live from the Mac's last status, and no connection.
        XCTAssertEqual(model.reportingSeats, 0)
        XCTAssertEqual(model.connectedSeats, 0)
        XCTAssertEqual(model.seats.map(\.seat), ["CLAUDE", "MM"])
        for seat in model.seats {
            XCTAssertEqual(seat.host, .server, seat.seat)
            XCTAssertNil(seat.connected, seat.seat)
            XCTAssertFalse(seat.reported, seat.seat)
            XCTAssertFalse(seat.needsAttention, seat.seat)
            XCTAssertTrue(seat.settings.allSatisfy { $0.state == .heldElsewhere }, seat.seat)
        }
        // The panel points at the server listener's own status.
        XCTAssertTrue(WakeSettingsModel.serverPointer.contains("Seats panel"))
        XCTAssertTrue(WakeSettingsModel.serverPointer.contains("server's control helper"))
    }

    /// The same stale status under the partition from before the cutover is still the stopped
    /// listener it was:  the retired reading is the partition's, not the status's.
    func test_a_mac_seat_in_the_partition_keeps_the_old_reading() {
        let model = WakeSettingsBuilder.build(
            sources(status: staleStatus(updated: lastWritten), partition: beforeCutover, readAt: lastWritten + 36_000))
        XCTAssertFalse(model.macListenerRetired)
        XCTAssertEqual(model.listenerTitle, ListenerRunState.stopped.title)
        XCTAssertNil(model.retiredNote)
        XCTAssertEqual(model.listenerErrors, ["seat CLAUDE is configured here (the 'mac' listener)"])
        XCTAssertEqual(model.operatorVoice.label, "Not known")
        XCTAssertNotEqual(model.operatorVoice, .notRead(WakeSettingsModel.retiredOperatorVoice))
    }

    /// A retired listener found running with a current status is read as before, and the
    /// headline says plainly that it runs and should hold no seat.
    func test_a_running_retired_listener_is_said_plainly() {
        let now = lastWritten + 5
        let model = WakeSettingsBuilder.build(
            sources(status: runningStatus(updated: lastWritten), partition: allServer, readAt: now))
        XCTAssertTrue(model.macListenerRetired)
        XCTAssertTrue(model.statusIsCurrent)
        XCTAssertEqual(model.listenerTitle, "The Mac's eavesdropper is running, but it is retired and should hold no seat")
        XCTAssertTrue(model.retiredNote?.contains("nothing should be running here") == true)
        // A current status is the listener's own word now, so its problems are shown.
        XCTAssertEqual(model.listenerErrors, ["seat CLAUDE is configured here (the 'mac' listener)"])
        XCTAssertEqual(model.operatorVoice.label, "Can post")
    }

    /// A status that could not be read is still retired, not an error about the listener:  the
    /// reason stays where the panel says what could not be read.
    func test_an_unread_status_under_an_all_server_partition_is_retired() {
        var input = sources(status: nil, partition: allServer, readAt: lastWritten)
        input.statusProblem = "The eavesdropper's status command failed:  boom"
        let model = WakeSettingsBuilder.build(input)
        XCTAssertTrue(model.macListenerRetired)
        XCTAssertEqual(model.runState, .unknown("The eavesdropper's status command failed:  boom"))
        XCTAssertEqual(model.listenerTitle, WakeSettingsModel.retiredTitle)
        XCTAssertEqual(model.operatorVoice, .notRead(WakeSettingsModel.retiredOperatorVoice))
        XCTAssertTrue(model.seats.allSatisfy { !$0.settingsEditable })
    }

    /// The reader does not add the seats a retired listener's stale status still lists, and adds
    /// them again when that status is current.
    func test_the_reader_does_not_list_a_stale_status_s_old_seats() async throws {
        let home = try makeHome()
        let stale = await read(home: home, status: staleStatusWithOldSeat(updated: lastWritten))
        XCTAssertEqual(stale.seats, ["CLAUDE"])
        let current = await read(home: home, status: runningStatusWithOldSeat(updated: Date().timeIntervalSince1970))
        XCTAssertEqual(current.seats, ["CLAUDE", "OLDSEAT"])
    }

    // MARK: Seat edits

    /// A seat the partition gives the server has no editable setting, and says its settings live
    /// on the server listener.
    func test_seat_edits_are_disabled_for_server_seats() throws {
        let model = WakeSettingsBuilder.build(
            sources(status: staleStatus(updated: lastWritten), partition: allServer, readAt: lastWritten + 36_000))
        let claude = try XCTUnwrap(model.seats.first { $0.seat == "CLAUDE" })
        XCTAssertFalse(claude.settingsEditable)
        let reason = try XCTUnwrap(claude.settingsReadOnlyReason)
        XCTAssertEqual(reason, "Held by the server eavesdropper.\u{00A0} Its settings live on the server listener, not in the Mac's file, so they are not edited here.")
        // A Mac seat under the old partition is still editable.
        let old = WakeSettingsBuilder.build(
            sources(status: staleStatus(updated: lastWritten), partition: beforeCutover, readAt: lastWritten + 36_000))
        XCTAssertEqual(old.seats.first { $0.seat == "MM" }?.settingsEditable, true)
    }

    /// The writer refuses a server seat on its own, whatever the panel showed:  it reads the
    /// partition again before it writes.
    func test_the_writer_refuses_a_server_seat() throws {
        let home = try makeHome()
        let writer = WakeSettingsWriter(
            configURL: ListenerStateLocation(home: home).configURL,
            partitionURL: home.appendingPathComponent("partition.toml")
        )
        let edit = WakeSettingsWriter.Edit(seat: "CLAUDE", key: .wakesPerHour, value: .number(5))
        XCTAssertThrowsError(try writer.prepare([edit])) { error in
            XCTAssertEqual(
                error as? WakeSettingsWriter.WriteError,
                .notHeldByTheMac(seat: "CLAUDE", reason: ListenerSeatHost.server.readOnlyReason ?? "")
            )
        }
    }

    // MARK: The reader's workspace

    private final class FakeStatus: SimpleToolsRunning, @unchecked Sendable {
        let status: String
        init(_ status: String) { self.status = status }
        func run(_ invocation: SimpleToolsInvocation) async throws -> SimpleToolsCLIResult {
            let verb = invocation.arguments.count > 1 ? invocation.arguments[1] : ""
            return SimpleToolsCLIResult(exitCode: 0, stdout: verb == "status" ? status : "", stderr: "")
        }
    }

    /// A home with the Mac's file after the cutover and an all-server partition, gone when the
    /// test ends.
    private func makeHome() throws -> URL {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-cutover-\(UUID().uuidString)", isDirectory: true)
        let root = home.appendingPathComponent(".simple-tools", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: home) }
        try config.write(to: root.appendingPathComponent("listener.toml"), atomically: true, encoding: .utf8)
        try "[seats]\nOPERATOR = \"mac\"\nCLAUDE = \"server\"\nOLDSEAT = \"server\"\n"
            .write(to: home.appendingPathComponent("partition.toml"), atomically: true, encoding: .utf8)
        return home
    }

    private func read(home: URL, status: String) async -> WakeSettingsSources {
        await WakeSettingsReader(
            location: ListenerStateLocation(home: home),
            runner: FakeStatus(status),
            roster: ["CLAUDE"],
            partitionURL: home.appendingPathComponent("partition.toml")
        ).read()
    }

    private func staleStatusWithOldSeat(updated: Double) -> String {
        """
        {"running": false, "status": {"instance": "mac", "updated": \(updated),
          "seats": {"CLAUDE": {"bot": "CLAUDE"}, "OLDSEAT": {"bot": "OLDSEAT"}}}}
        """
    }

    private func runningStatusWithOldSeat(updated: Double) -> String {
        """
        {"running": true, "status": {"instance": "mac", "updated": \(updated),
          "seats": {"CLAUDE": {"bot": "CLAUDE"}, "OLDSEAT": {"bot": "OLDSEAT"}}}}
        """
    }
}
