import XCTest

/// The sidebar's places, the saved place, the window's words for each place and the
/// overview's counts.
final class MusterNavigationTests: XCTestCase {
    private var directory: URL!
    /// 2:46 PM on the owner's clock.
    private let now = MusterNavigationTests.time("2026-11-04T20:46:00Z")

    override func setUpWithError() throws {
        directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-navigation-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private static func time(_ text: String) -> Date {
        guard let date = FleetTime.parse(text) else { fatalError("bad time \(text)") }
        return date
    }

    // MARK: Destinations

    /// Every panel is named once:  the overview and fifteen panels, each under exactly one
    /// section, with Operations split into three.
    func test_sidebar_names_every_place_once() {
        let named = [MusterDestination.overview]
            + MusterDestination.sidebarPlaces.flatMap { MusterDestination.listed(under: $0) }
        XCTAssertEqual(named.count, 16)
        XCTAssertEqual(Set(named), Set(MusterDestination.allCases))
        XCTAssertEqual(MusterDestination.allCases.count, 16)
    }

    /// The rule, stated per panel:  where each panel's data and actions live.  The sidebar
    /// files them from this alone, so this table is the sidebar.
    func test_each_panel_sits_under_the_machine_it_runs_on() {
        let expected: [MusterDestination: FleetPlace?] = [
            .overview: nil,
            .services: .machine(.mac),
            .macAgents: .machine(.mac),
            .complianceGrid: .machine(.mac),
            .driftHistory: .machine(.mac),
            .wakeAndListener: .machine(.mac),
            .seatDetail: .machine(.mac),
            .serverStatus: .machine(.server),
            .serverResourceLimits: .machine(.server),
            .serverUpdates: .machine(.server),
            .appsAndWork: .acrossFleet,
            .seats: .acrossFleet,
            .controls: .acrossFleet,
            .hosts: .acrossFleet,
            .virtualMachines: .acrossFleet,
            .actionHistory: .acrossFleet,
        ]
        XCTAssertEqual(expected.count, MusterDestination.allCases.count)
        for destination in MusterDestination.allCases {
            XCTAssertEqual(destination.place, expected[destination] ?? nil, "\(destination)")
        }
    }

    /// The sections read in the design's order, and the Mac's agents are under The Mac.
    func test_sections_read_in_order() {
        XCTAssertEqual(MusterDestination.sidebarPlaces.map(\.sectionHeading),
                       ["The Mac", "The Server", "Across The Fleet"])
        XCTAssertEqual(MusterDestination.listed(under: .machine(.mac)),
                       [.services, .macAgents, .complianceGrid, .driftHistory, .wakeAndListener, .seatDetail])
        XCTAssertEqual(MusterDestination.listed(under: .machine(.server)),
                       [.serverStatus, .serverResourceLimits, .serverUpdates])
        XCTAssertEqual(MusterDestination.listed(under: .acrossFleet),
                       [.appsAndWork, .seats, .controls, .hosts, .virtualMachines, .actionHistory])
    }

    /// One name per panel:  no two panels share a title.
    func test_every_panel_has_its_own_name() {
        let titles = MusterDestination.allCases.map(\.title)
        XCTAssertEqual(Set(titles).count, titles.count)
        XCTAssertEqual(MusterDestination.macAgents.title, "Background Agents")
        XCTAssertEqual(MusterDestination.serverUpdates.title, "Server Updates")
        XCTAssertEqual(MusterDestination.actionHistory.title, "Action History")
        XCTAssertEqual(MusterDestination.hosts.title, "Tools And Worker Health")
        XCTAssertEqual(MusterDestination.seatDetail.title, "MiniMax On The Mac")
    }

    /// The window's subtitle is the badge's own words, so the two cannot disagree.
    func test_subtitle_is_the_place_in_the_badges_words() {
        for destination in MusterDestination.allCases {
            if let place = destination.place {
                XCTAssertEqual(destination.subtitle, place.title)
            } else {
                XCTAssertEqual(destination.subtitle, "The Mac And The Server")
            }
        }
        XCTAssertEqual(MusterDestination.services.subtitle, "On The Mac")
        XCTAssertEqual(MusterDestination.serverUpdates.subtitle, "On The Server")
        XCTAssertEqual(MusterDestination.actionHistory.subtitle, "Across The Fleet")
    }

    func test_saved_spelling_restores_its_place() {
        for destination in MusterDestination.allCases {
            XCTAssertEqual(MusterDestination.restored(from: destination.rawValue), destination)
        }
    }

    /// Operations was split.  Its saved spelling opens the part that was the server's.
    func test_saved_operations_restores_to_server_updates() {
        XCTAssertNil(MusterDestination(rawValue: "operations"))
        XCTAssertEqual(MusterDestination.restored(from: "operations"), .serverUpdates)
    }

    func test_unsaved_or_unnamed_place_restores_the_overview() {
        XCTAssertEqual(MusterDestination.restored(from: nil), .overview)
        XCTAssertEqual(MusterDestination.restored(from: ""), .overview)
        XCTAssertEqual(MusterDestination.restored(from: "aPlaceTheAppDropped"), .overview)
    }

    /// The saved spellings are a contract with the owner's defaults:  renaming one
    /// silently would put every window back on the overview once.
    func test_saved_spellings_stay_stable() {
        XCTAssertEqual(MusterDestination.overview.rawValue, "overview")
        XCTAssertEqual(MusterDestination.complianceGrid.rawValue, "complianceGrid")
        XCTAssertEqual(MusterDestination.services.rawValue, "services")
        XCTAssertEqual(MusterDestination.seatDetail.rawValue, "seatDetail")
        XCTAssertEqual(MusterDestination.serverStatus.rawValue, "serverStatus")
        XCTAssertEqual(MusterDestination.hosts.rawValue, "hosts")
        XCTAssertEqual(MusterDestination.virtualMachines.rawValue, "virtualMachines")
        XCTAssertEqual(MusterDestination.macAgents.rawValue, "macAgents")
        XCTAssertEqual(MusterDestination.serverUpdates.rawValue, "serverUpdates")
        XCTAssertEqual(MusterDestination.actionHistory.rawValue, "actionHistory")
        XCTAssertEqual(MusterSettings.sidebarSelectionDefaultsKey, "musterSidebarSelection")
    }

    // MARK: Source lines

    /// A server report on disk,  read back through the app's own reader.
    private func statusReading(generatedAt: String, copiedAt: String) throws -> ServerStatusReading {
        let json = #"{"schemaVersion":1,"tool":"fleet-server-status","generatedAt":"\#(generatedAt)","coolify":[],"processes":[{"pid":1,"name":"init"}],"files":[],"services":[]}"#
        let url = directory.appendingPathComponent("server-status-\(UUID().uuidString).json")
        try Data(json.utf8).write(to: url)
        try FileManager.default.setAttributes([.modificationDate: Self.time(copiedAt)], ofItemAtPath: url.path)
        return ServerStatusReportReader.read(at: url, now: now)
    }

    /// Every panel says where its data comes from, and the overview, which is both, says
    /// nothing of its own.
    func test_every_panel_has_a_source_line() {
        let inputs = PlaceSourceInputs()
        for destination in MusterDestination.allCases {
            let line = destination.sourceLine(inputs)
            if destination == .overview {
                XCTAssertNil(line)
                continue
            }
            guard let text = line else {
                XCTFail("\(destination) has no source line")
                continue
            }
            XCTAssertFalse(text.isEmpty, "\(destination)")
            // A sentence gap is a real U+00A0 and a space, never two plain spaces.
            XCTAssertFalse(text.contains(".  "), "\(destination)")
            XCTAssertFalse(text.contains("this Mac"), "\(destination)")
        }
    }

    /// A time is said only when the window holds one, on the owner's clock.
    func test_services_line_says_when_the_mac_was_read() {
        XCTAssertEqual(MusterDestination.services.sourceLine(PlaceSourceInputs(servicesReadAt: now)),
                       "Read live from the Mac at 2:46 PM.")
        XCTAssertEqual(MusterDestination.services.sourceLine(PlaceSourceInputs()),
                       "Not read from the Mac yet.")
        XCTAssertEqual(MusterDestination.complianceGrid.sourceLine(PlaceSourceInputs(seatCheckAt: now)),
                       "Seat setup checked on the Mac at 2:46 PM.")
    }

    /// The server's line names both times:  when the server wrote the report and when the
    /// Mac copied it.  It replaces the old "Report read 5m ago".
    func test_server_line_names_the_two_times() throws {
        let reading = try statusReading(generatedAt: "2026-11-04T20:45:00Z", copiedAt: "2026-11-04T20:46:00Z")
        XCTAssertEqual(MusterDestination.serverSourceLine(reading),
                       "Report written by the server at 2:45 PM.\u{00A0} The Mac copied it at 2:46 PM.")
        let missing = ServerStatusReportReader.read(at: directory.appendingPathComponent("absent.json"), now: now)
        XCTAssertEqual(MusterDestination.serverSourceLine(missing), "No server report on the Mac yet.")
        XCTAssertEqual(MusterDestination.serverSourceLine(nil), "Reading the server's report on the Mac\u{2026}")
    }

    // MARK: The two places

    private func emptyInputs() -> MachineMenuInputs {
        MachineMenuInputs(services: nil, lastReadAt: nil, redCells: nil, status: nil, tools: nil, now: now)
    }

    /// The Overview's cards are the menu's own summary of each place, so they cannot
    /// disagree with it.
    func test_place_cards_are_the_menus_own_words() {
        let menu = MachineMenu(emptyInputs())
        XCTAssertEqual(menu.macCard.health, menu.mac.health)
        XCTAssertEqual(menu.serverCard.health, menu.server.health)
        XCTAssertEqual(menu.macCard.lines.first, "Reading the Mac\u{2026}")
        XCTAssertEqual(menu.serverCard.lines.first, "Reading the server's report on the Mac\u{2026}")
        XCTAssertEqual(menu.serverCard.stateWords, "Not known")
        XCTAssertEqual(menu.macCard.destination, .services)
        XCTAssertEqual(menu.serverCard.destination, .serverStatus)
        // The menu's clicks are not words about the place.
        XCTAssertFalse(menu.serverCard.lines.contains("Open Server Status"))
        XCTAssertFalse(menu.serverCard.lines.contains("Ask The Server Now"))
        XCTAssertFalse(menu.macCard.lines.contains("Check The Mac Again"))
    }

    func test_place_card_keeps_lines_rows_and_groups_and_drops_clicks() {
        let items: [MenuItem] = [
            .line("Checked at 2:46 PM"),
            .command(title: "Check The Mac Again", .checkMac),
            .row(MenuRow(title: "Shellular:  failed", actions: [])),
            .group(title: "Seat Setup:  1 red cell", items: [.line("inside")]),
        ]
        let card = PlaceCard(host: .mac, health: .attention(2, someUnread: true), items: items)
        XCTAssertEqual(card.lines, ["Checked at 2:46 PM", "Shellular:  failed", "Seat Setup:  1 red cell"])
        XCTAssertEqual(card.stateWords, "2 things need attention, and some could not be read")

        let many = (0..<20).map { MenuItem.line("line \($0)") }
        XCTAssertEqual(PlaceCard(host: .server, health: .ok, items: many).lines.count, PlaceCard.lineLimit)
        XCTAssertEqual(PlaceCard(host: .server, health: .ok, items: many).stateWords, "OK")
    }

    /// A sidebar badge never shows a place as clean when it was not read.
    func test_health_badge_words() {
        XCTAssertNil(PlaceHealthWords.badge(.ok))
        XCTAssertEqual(PlaceHealthWords.badge(.attention(2, someUnread: false)), "2")
        XCTAssertEqual(PlaceHealthWords.badge(.attention(2, someUnread: true)), "2+")
        XCTAssertEqual(PlaceHealthWords.badge(.notKnown), "?")
        XCTAssertEqual(PlaceHealthWords.sentence(.attention(1, someUnread: false)), "1 thing needs attention")
    }

    func test_services_badge_is_unknown_until_read_and_counts_failed_or_unknown() {
        let healthy = FleetServices(serverState: .running, states: [
            "botfleet-mcp": .running, "app.botfleet.server": .running,
        ])
        XCTAssertEqual(SidebarBadges.services(healthy.entries, pm2Answered: true, readAt: nil, now: now), "?")
        let old = now.addingTimeInterval(-(MachineMenuCopy.macReadWindow + 1))
        XCTAssertEqual(SidebarBadges.services(healthy.entries, pm2Answered: true, readAt: old, now: now), "?")

        var failingStates = Dictionary(uniqueKeysWithValues: FleetServiceRegistry.all.map { ($0.id, ManagedServiceState.running) })
        failingStates["botfleet-mcp"] = .failed
        failingStates["shellular"] = .unknown
        let failing = FleetServices(serverState: .running, states: failingStates)
        XCTAssertTrue(failing.pm2Answered)
        XCTAssertEqual(SidebarBadges.services(failing.entries, pm2Answered: failing.pm2Answered, readAt: now, now: now), "2")

        let allRead = Dictionary(uniqueKeysWithValues: FleetServiceRegistry.all.map { ($0.id, ManagedServiceState.running) })
        let clean = FleetServices(serverState: .running, states: allRead)
        XCTAssertNil(SidebarBadges.services(clean.entries, pm2Answered: clean.pm2Answered, readAt: now, now: now))
    }

    /// When pm2 does not answer, its rows are not known and are not counted, as the menu
    /// says:  the badge is `?` with nothing else wrong, and the count with a plus beside a
    /// real problem, the same as the Mac's card and the menu bar for the same read.
    func test_services_badge_does_not_count_rows_pm2_did_not_answer_for() {
        var states: [String: ManagedServiceState] = [:]
        for descriptor in FleetServiceRegistry.all {
            if case .pm2 = descriptor.kind, descriptor.policy == .alwaysOn {
                states[descriptor.id] = .unknown
            } else {
                states[descriptor.id] = .running
            }
        }
        let silent = FleetServices(serverState: .running, states: states)
        XCTAssertFalse(silent.pm2Answered)
        XCTAssertEqual(SidebarBadges.services(silent.entries, pm2Answered: silent.pm2Answered, readAt: now, now: now), "?")

        let agent = FleetServiceRegistry.all.first { descriptor in
            if case .launchAgent = descriptor.kind { return true }
            return false
        }
        guard let agent else { return XCTFail("the registry names no LaunchAgent") }
        states[agent.id] = .failed
        let oneFailed = FleetServices(serverState: .running, states: states)
        XCTAssertFalse(oneFailed.pm2Answered)
        XCTAssertEqual(SidebarBadges.services(oneFailed.entries, pm2Answered: oneFailed.pm2Answered, readAt: now, now: now), "1+")

        // The menu says the same thing about the same read.
        let menu = MachineMenu(MachineMenuInputs(services: oneFailed, lastReadAt: now, redCells: [],
                                                 status: nil, tools: nil, now: now))
        XCTAssertEqual(menu.mac.health, .attention(1, someUnread: true))
        XCTAssertEqual(PlaceHealthWords.badge(menu.mac.health), "1+")
    }

    // MARK: Action history

    func test_action_history_names_the_machine_in_words() {
        XCTAssertEqual(ActionHistoryWords.host("mac", via: nil), "The Mac")
        XCTAssertEqual(ActionHistoryWords.host("server", via: "muster"), "The Server via muster")
        XCTAssertEqual(ActionHistoryWords.host(nil, via: nil), "\u{2014}")
        XCTAssertEqual(ActionHistoryWords.host("some-box", via: nil), "some-box")
    }

    // MARK: The run's verdict

    func test_verdict_words() {
        XCTAssertEqual(RunVerdict.make(phase: .idle, verdict: nil), RunVerdict(words: "Not Checked Yet", tone: .neutral))
        XCTAssertEqual(RunVerdict.make(phase: .running, verdict: "PASS").tone, .neutral)
        XCTAssertEqual(RunVerdict.make(phase: .failed("x"), verdict: "PASS"), RunVerdict(words: "Not Known", tone: .notKnown))
        XCTAssertEqual(RunVerdict.make(phase: .loaded, verdict: "PASS"), RunVerdict(words: "All Passing", tone: .pass))
        XCTAssertEqual(RunVerdict.make(phase: .loaded, verdict: "FAIL"), RunVerdict(words: "Problems Found", tone: .fail))
        XCTAssertEqual(RunVerdict.make(phase: .loaded, verdict: "SOMETHING"), RunVerdict(words: "Not Known", tone: .notKnown))
        XCTAssertEqual(RunVerdict.make(phase: .loaded, verdict: nil).tone, .notKnown)
    }

    func test_failing_count_has_its_noun() {
        let summary = EnvelopeSummary(total: 120, byState: [:], overallVerdict: "FAIL", failing: 12)
        XCTAssertEqual(RunVerdict.failingText(summary), "12 of 120 checks failing")
        let one = EnvelopeSummary(total: 1, byState: [:], overallVerdict: "PASS", failing: 0)
        XCTAssertEqual(RunVerdict.failingText(one), "0 of 1 check failing")
        XCTAssertNil(RunVerdict.failingText(nil))
    }

    // MARK: The grid's filter

    private func includes(_ filter: GridStateFilter, _ states: [CellState]) -> Bool {
        filter.includes(states.map { cell($0) }, commonCheckedAt: nil)
    }

    func test_grid_filter_matches_the_state_it_names() {
        XCTAssertTrue(includes(.all, [.passed]))
        XCTAssertTrue(includes(.red, [.passed, .missing]))
        XCTAssertTrue(includes(.red, [.unknown]))
        XCTAssertFalse(includes(.red, [.drift, .unproven]))
        XCTAssertTrue(includes(.drift, [.drift]))
        XCTAssertFalse(includes(.drift, [.missing]))
        XCTAssertTrue(includes(.unproven, [.unproven]))
        XCTAssertTrue(includes(.attention, [.unproven]))
        XCTAssertFalse(includes(.attention, [.passed, .covered, .unsupported]))
    }

    /// Checked At Another Time opens on the same cells the card counts, by the same rule.
    func test_checked_at_another_time_filter_matches_the_count() {
        let common = FleetTime.parse("2026-10-21T09:00:00Z")
        let earlier = checkedCell("2026-10-21T08:58:59Z")
        let later = checkedCell("2026-10-21T09:01:01Z")
        let same = checkedCell("2026-10-21T08:59:30Z")
        XCTAssertTrue(GridStateFilter.checkedAtAnotherTime.includes([same, earlier], commonCheckedAt: common))
        XCTAssertTrue(GridStateFilter.checkedAtAnotherTime.includes([same, later], commonCheckedAt: common))
        XCTAssertFalse(GridStateFilter.checkedAtAnotherTime.includes([same], commonCheckedAt: common))
        // With no shared time the count is unknown, so the grid keeps every seat and says
        // why, never an empty grid that reads as zero.
        XCTAssertTrue(GridStateFilter.checkedAtAnotherTime.includes([same], commonCheckedAt: nil))
        let note = GridStateFilter.checkedAtAnotherTime.cannotNarrowNote(commonCheckedAt: nil)
        XCTAssertNotNil(note)
        XCTAssertFalse(note?.contains(".  ") ?? true)
        XCTAssertNil(GridStateFilter.checkedAtAnotherTime.cannotNarrowNote(commonCheckedAt: common))
        XCTAssertNil(GridStateFilter.red.cannotNarrowNote(commonCheckedAt: nil))
        let counts = OverviewCounts.make(cells: [same, earlier, later], trusted: true, commonCheckedAt: common)
        XCTAssertEqual(counts.stale, 2)
    }

    /// The rule is two-sided like the grid's, so the name says another time, not earlier.
    func test_the_filter_is_named_for_its_two_sided_rule() {
        XCTAssertEqual(GridStateFilter.checkedAtAnotherTime.rawValue, "Checked At Another Time")
        XCTAssertFalse(GridStateFilter.allCases.map(\.rawValue).contains("Checked Earlier"))
    }

    // MARK: Server Status when the report is not current

    private func service(_ unit: String, _ state: String, scope: String = "system") throws -> ServerService {
        try JSONDecoder().decode(ServerService.self, from: Data(#"{"unit":"\#(unit)","scope":"\#(scope)","activeState":"\#(state)","controllable":true}"#.utf8))
    }

    func test_last_known_lists_the_report_timer_even_when_the_report_did_not() throws {
        let services = [try service("fleet-cli-git-update.timer", "active")]
        let rows = ServerLastKnown.rows(services, offersRepairs: true)
        XCTAssertEqual(rows.map(\.unit), ["fleet-cli-git-update.timer", OpsNames.reportTimerUnit])
        XCTAssertEqual(rows.map(\.lastState), ["last known active", "not in the last report"])
        XCTAssertNil(rows.last?.scope)
        let withTimer = [try service(OpsNames.reportTimerUnit, "active", scope: "user")]
        XCTAssertEqual(ServerLastKnown.rows(withTimer, offersRepairs: true).map(\.unit), [OpsNames.reportTimerUnit])
    }

    /// With no repairs to offer, the unlisted timer row would say nothing the report said,
    /// so only the report's own rows are listed, and no row has a button.
    func test_last_known_without_repairs_lists_only_the_reports_rows_and_offers_nothing() throws {
        let services = [try service("fleet-cli-git-update.timer", "active")]
        XCTAssertEqual(ServerLastKnown.rows(services, offersRepairs: false).map(\.unit), ["fleet-cli-git-update.timer"])
        let unit = OpsNames.reportTimerUnit
        let full = OpsAllowlist(serverUnits: [
            OpsAllowlist.Entry(name: unit, actions: ["start", "stop", "restart"], scope: "user"),
        ])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "user", allowlist: full, offersRepairs: false), [])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: nil, allowlist: full, offersRepairs: false), [])
    }

    /// One unit in two scopes is two rows, each with its own state and its own identity.
    func test_last_known_keeps_a_unit_in_two_scopes_apart() throws {
        let services = [
            try service("same.timer", "active", scope: "user"),
            try service("same.timer", "failed", scope: "system"),
        ]
        let rows = ServerLastKnown.rows(services, offersRepairs: true).filter { $0.unit == "same.timer" }
        XCTAssertEqual(rows.count, 2)
        XCTAssertEqual(Set(rows.map(\.id)).count, 2)
        XCTAssertEqual(rows.map(\.scope), ["user", "system"])
        XCTAssertEqual(rows.map(\.lastState), ["last known active", "last known failed"])
    }

    /// The repairs come from the allowlist only, and never Start or Stop on an old state.
    func test_last_known_offers_only_the_allowlists_restart_and_logs() {
        let unit = OpsNames.reportTimerUnit
        let full = OpsAllowlist(serverUnits: [
            OpsAllowlist.Entry(name: unit, actions: ["start", "stop", "restart"], scope: "user"),
        ])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "user", allowlist: full, offersRepairs: true),
                       [.serverUnit(verb: .restart, unit: unit), .logs(unit: unit)])
        let startOnly = OpsAllowlist(serverUnits: [OpsAllowlist.Entry(name: unit, actions: ["start"], scope: "user")])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "user", allowlist: startOnly, offersRepairs: true),
                       [.logs(unit: unit)])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "user", allowlist: OpsAllowlist(), offersRepairs: true), [])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "user", allowlist: nil, offersRepairs: true), [])
        for action in ServerLastKnown.actions(unit: unit, scope: "user", allowlist: full, offersRepairs: true) {
            XCTAssertEqual(action.runsOn, .server)
        }
    }

    /// An old report is no reason to restart some other unit it names, even one the
    /// allowlist allows:  only the report timer, the menu's own repair, gets a button.
    func test_last_known_offers_nothing_on_a_unit_that_is_not_the_report_timer() {
        let production = "fleet-recall.service"
        let allowlist = OpsAllowlist(serverUnits: [
            OpsAllowlist.Entry(name: production, actions: ["start", "stop", "restart"], scope: "system"),
            OpsAllowlist.Entry(name: OpsNames.reportTimerUnit, actions: ["restart"], scope: "user"),
        ])
        XCTAssertEqual(ServerLastKnown.actions(unit: production, scope: "system", allowlist: allowlist, offersRepairs: true), [])
        XCTAssertEqual(ServerLastKnown.actions(unit: production, scope: nil, allowlist: allowlist, offersRepairs: true), [])
        XCTAssertFalse(ServerLastKnown.actions(unit: OpsNames.reportTimerUnit, scope: "user", allowlist: allowlist,
                                               offersRepairs: true).isEmpty)
    }

    /// A report row with no scope keeps no scope:  it is not shown as `system`.
    func test_last_known_does_not_invent_a_scope_for_a_row_with_none() throws {
        let json = #"{"unit":"no-scope.timer","activeState":"active","controllable":true}"#
        let service = try JSONDecoder().decode(ServerService.self, from: Data(json.utf8))
        let rows = ServerLastKnown.rows([service], offersRepairs: false)
        XCTAssertEqual(rows.count, 1)
        XCTAssertNil(rows.first?.scope)
    }

    /// An action names the unit and not its scope, and the server acts in the allowlist's
    /// scope.  A row in the other scope gets no button, because it would act on a unit the
    /// row does not show.  A row the report did not name runs in the allowlist's scope.
    func test_last_known_offers_nothing_on_a_row_in_another_scope() {
        let unit = OpsNames.reportTimerUnit
        let allowlist = OpsAllowlist(serverUnits: [
            OpsAllowlist.Entry(name: unit, actions: ["restart"], scope: "user"),
        ])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "system", allowlist: allowlist, offersRepairs: true), [])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: nil, allowlist: allowlist, offersRepairs: true),
                       [.serverUnit(verb: .restart, unit: unit), .logs(unit: unit)])
        let noScope = OpsAllowlist(serverUnits: [OpsAllowlist.Entry(name: unit, actions: ["restart"])])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, scope: "user", allowlist: noScope, offersRepairs: true), [])
    }

    // MARK: Server Status follows the menu's reach

    /// The doctor's report on disk, read back through the app's own reader.
    private func toolsReading(generatedAt: String = "2026-11-04T20:45:00Z",
                              serverReachable: Bool = true) throws -> FleetToolsReading {
        let json = #"{"schemaVersion":1,"tool":"fleet-tools-doctor","generatedAt":"\#(generatedAt)","hosts":[{"host":"mac","reachable":true,"checks":[]},{"host":"server","reachable":\#(serverReachable),"checks":[]}]}"#
        let url = directory.appendingPathComponent("report-\(UUID().uuidString).json")
        try Data(json.utf8).write(to: url)
        return FleetToolsReportReader.read(at: url, now: now)
    }

    private func panelMode(status: ServerStatusReading?, tools: FleetToolsReading?) -> (ServerPanelMode, MachineMenu) {
        let menu = MachineMenu(MachineMenuInputs(services: nil, lastReadAt: nil, redCells: nil,
                                                 status: status, tools: tools, now: now))
        return (ServerPanelMode.make(reach: menu.server.reach, reading: status), menu)
    }

    /// A report still inside its window is not current when the doctor could not reach the
    /// server:  the panel follows the menu, so it shows no current sections and no repairs.
    func test_panel_is_not_current_when_the_server_could_not_be_reached() throws {
        let status = try statusReading(generatedAt: "2026-11-04T20:45:00Z", copiedAt: "2026-11-04T20:46:00Z")
        XCTAssertTrue(status.isCurrent)
        let (mode, menu) = panelMode(status: status, tools: try toolsReading(serverReachable: false))
        XCTAssertNotEqual(mode, .current)
        XCTAssertEqual(mode, .lastKnown(offersRepairs: false))
        XCTAssertEqual(menu.serverCard.stateWords, "Not known")
        let note = try XCTUnwrap(mode.lastKnownNote)
        XCTAssertTrue(note.contains("with no repairs"))
        XCTAssertTrue(note.contains(".\u{00A0} "))
        XCTAssertFalse(note.contains(".  "))
    }

    /// When the Mac's own fetch stopped, the menu offers a fetch and no restart, so the
    /// panel offers no restart either.
    func test_panel_offers_no_repair_when_the_macs_fetch_stopped() throws {
        let status = try statusReading(generatedAt: "2026-11-04T20:45:00Z", copiedAt: "2026-11-04T20:46:00Z")
        let (mode, menu) = panelMode(status: status, tools: try toolsReading(generatedAt: "2026-11-04T17:02:00Z"))
        XCTAssertTrue(menu.server.fetchStopped)
        XCTAssertEqual(mode, .lastKnown(offersRepairs: false))
    }

    /// The server was reached and its report stopped:  the one case where the menu offers
    /// the report timer's restart, and so the one case the panel does.
    func test_panel_offers_the_repairs_when_the_report_stopped() throws {
        let status = try statusReading(generatedAt: "2026-11-04T19:02:00Z", copiedAt: "2026-11-04T20:46:00Z")
        let (mode, menu) = panelMode(status: status, tools: try toolsReading())
        if case .reportStopped = menu.server.reach {} else { XCTFail("\(menu.server.reach)") }
        XCTAssertEqual(mode, .lastKnown(offersRepairs: true))
        XCTAssertTrue(mode.lastKnownNote?.contains("the repairs the allowlist names") ?? false)
    }

    /// Only a current reach shows the report's sections.
    func test_panel_shows_the_report_only_when_the_menu_calls_it_current() throws {
        let status = try statusReading(generatedAt: "2026-11-04T20:45:00Z", copiedAt: "2026-11-04T20:46:00Z")
        let (mode, menu) = panelMode(status: status, tools: try toolsReading())
        if case .current = menu.server.reach {} else { XCTFail("\(menu.server.reach)") }
        XCTAssertEqual(mode, .current)
        XCTAssertNil(mode.lastKnownNote)
    }

    /// A report that is not on the Mac shows its reason, and before the first read nothing
    /// is shown beyond the state line.
    func test_panel_shows_the_problem_or_nothing_before_a_read() throws {
        let missing = ServerStatusReportReader.read(at: directory.appendingPathComponent("absent.json"), now: now)
        let (mode, _) = panelMode(status: missing, tools: try toolsReading())
        guard case .problem(let text) = mode else { return XCTFail("\(mode)") }
        XCTAssertFalse(text.isEmpty)
        XCTAssertEqual(panelMode(status: nil, tools: nil).0, .reading)
    }

    /// The allowlist's scope for a unit comes from the file the helper reads.
    func test_the_allowlist_carries_each_units_scope() throws {
        let json = #"{"schemaVersion":1,"server":{"units":[{"unit":"a.timer","scope":"user","actions":["restart"]},{"unit":"b.timer","scope":"system","actions":["restart"]}]}}"#
        let list = try XCTUnwrap(OpsAllowlist.parse(Data(json.utf8)))
        XCTAssertEqual(list.scope(forServerUnit: "a.timer"), "user")
        XCTAssertEqual(list.scope(forServerUnit: "b.timer"), "system")
        XCTAssertNil(list.scope(forServerUnit: "c.timer"))
    }

    /// The server refuses the whole allowlist when one unit's scope is missing, not a string,
    /// or not `user` or `system`, so the Mac does not read it either:  no buttons are drawn
    /// from a file the server would not accept.
    func test_the_allowlist_is_not_read_when_a_units_scope_is_malformed() throws {
        let ok = #"{"unit":"ok.timer","scope":"user","actions":["restart"]}"#
        for bad in [
            #"{"unit":"odd.timer","scope":"global","actions":["restart"]}"#,
            #"{"unit":"typed.timer","scope":3,"actions":["restart"]}"#,
            #"{"unit":"null.timer","scope":null,"actions":["restart"]}"#,
            #"{"unit":"none.timer","actions":["restart"]}"#,
        ] {
            let json = #"{"schemaVersion":1,"server":{"units":["# + ok + "," + bad + "]}}"
            XCTAssertNil(OpsAllowlist.parse(Data(json.utf8)), bad)
        }
        let fine = #"{"schemaVersion":1,"server":{"units":["# + ok + "]}}"
        let list = try XCTUnwrap(OpsAllowlist.parse(Data(fine.utf8)))
        XCTAssertEqual(list.scope(forServerUnit: "ok.timer"), "user")
    }

    // MARK: Overview counts

    private func cell(_ state: CellState, rules: [CheckResult] = []) -> GridCell {
        GridCell(seat: "MM", kind: .skills, state: state, rules: rules, coverReason: nil)
    }

    /// A passing cell whose one rule was checked at a given time.
    private func checkedCell(_ checkedAt: String) -> GridCell {
        cell(.passed, rules: [
            CheckResult(
                ruleId: "r", seat: "MM", platform: "minimax", kind: "skills",
                state: "PASS", reason: "ok", reasonCode: "ok",
                expected: [:], observed: [:],
                proofLevelRequired: "L0", proofLevelAchieved: "L0",
                collector: "mac", checkType: "marked_block_hash",
                checkedAt: checkedAt, expiresAt: nil, ttlSeconds: 900,
                stale: false, failing: false)
        ])
    }

    func test_counts_read_off_the_cells() {
        let cells = [cell(.unknown), cell(.missing), cell(.unknown), cell(.drift), cell(.passed), cell(.unproven)]
        let counts = OverviewCounts.make(cells: cells, trusted: true, commonCheckedAt: Date())
        XCTAssertEqual(counts.red, 3)
        XCTAssertEqual(counts.unknown, 2)
        XCTAssertEqual(counts.missing, 1)
        XCTAssertEqual(counts.drift, 1)
        XCTAssertEqual(counts.unproven, 1)
    }

    /// Red is unknown plus missing, so the two show under the one count, never beside it.
    func test_red_breakdown_is_the_parts_of_red() {
        let cells = [cell(.unknown), cell(.missing), cell(.unknown)]
        let counts = OverviewCounts.make(cells: cells, trusted: true, commonCheckedAt: Date())
        XCTAssertEqual(counts.redBreakdown, ["2 could not be checked", "1 is missing"])
        XCTAssertNil(OverviewCounts.make(cells: cells, trusted: false, commonCheckedAt: nil).redBreakdown)
    }

    /// The fail-closed rule:  a run that could not be trusted counts nothing.
    ///
    /// Zero here would be a fleet measured and found clean, which is the one claim a
    /// failed run cannot make.  Nil is the only honest answer, and it is what the
    /// overview renders as unknown.
    func test_untrusted_run_counts_nothing_never_zero() {
        let cells = [cell(.unknown), cell(.passed)]
        let counts = OverviewCounts.make(cells: cells, trusted: false, commonCheckedAt: Date())
        XCTAssertNil(counts.red)
        XCTAssertNil(counts.unknown)
        XCTAssertNil(counts.missing)
        XCTAssertNil(counts.drift)
        XCTAssertNil(counts.unproven)
        XCTAssertNil(counts.stale)
    }

    /// Stale is only counted against a shared check time.
    ///
    /// Without one there is nothing to be stale against, so the count stays unknown
    /// rather than reading as none.
    func test_stale_stays_unknown_without_a_shared_check_time() {
        let counts = OverviewCounts.make(cells: [cell(.passed)], trusted: true, commonCheckedAt: nil)
        XCTAssertEqual(counts.red, 0)
        XCTAssertNil(counts.stale)
    }

    /// Stale uses the grid's own tolerance:  a cell checked seconds before the
    /// shared time is the same pass, not a cached exception.
    func test_stale_uses_the_grids_sixty_second_tolerance() {
        func checkedCell(_ checkedAt: String) -> GridCell {
            cell(.passed, rules: [
                CheckResult(
                    ruleId: "r", seat: "MM", platform: "minimax", kind: "skills",
                    state: "PASS", reason: "ok", reasonCode: "ok",
                    expected: [:], observed: [:],
                    proofLevelRequired: "L0", proofLevelAchieved: "L0",
                    collector: "mac", checkType: "marked_block_hash",
                    checkedAt: checkedAt, expiresAt: nil, ttlSeconds: 900,
                    stale: false, failing: false)
            ])
        }
        let common = FleetTime.parse("2026-10-21T09:00:00Z")
        let counts = OverviewCounts.make(
            cells: [checkedCell("2026-10-21T08:59:01Z"), checkedCell("2026-10-21T08:58:59Z")],
            trusted: true, commonCheckedAt: common)
        XCTAssertEqual(counts.stale, 1)
    }

    /// Stale counts a cell checked after the shared time beyond the same tolerance
    func test_stale_counts_cells_checked_after_the_shared_time_like_the_grid() {
        func checkedCell(_ checkedAt: String) -> GridCell {
            cell(.passed, rules: [
                CheckResult(
                    ruleId: "r", seat: "MM", platform: "minimax", kind: "skills",
                    state: "PASS", reason: "ok", reasonCode: "ok",
                    expected: [:], observed: [:],
                    proofLevelRequired: "L0", proofLevelAchieved: "L0",
                    collector: "mac", checkType: "marked_block_hash",
                    checkedAt: checkedAt, expiresAt: nil, ttlSeconds: 900,
                    stale: false, failing: false)
            ])
        }
        let common = FleetTime.parse("2026-10-21T09:00:00Z")
        let counts = OverviewCounts.make(
            cells: [checkedCell("2026-10-21T09:00:59Z"), checkedCell("2026-10-21T09:01:01Z")],
            trusted: true, commonCheckedAt: common)
        XCTAssertEqual(counts.stale, 1)
    }
}
