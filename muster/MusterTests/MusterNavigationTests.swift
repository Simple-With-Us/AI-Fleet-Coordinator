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
        XCTAssertEqual(MusterDestination.hosts.title, "Tools On Both Machines")
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
        XCTAssertEqual(SidebarBadges.services(healthy.entries, readAt: nil, now: now), "?")
        let old = now.addingTimeInterval(-(MachineMenuCopy.macReadWindow + 1))
        XCTAssertEqual(SidebarBadges.services(healthy.entries, readAt: old, now: now), "?")

        let failing = FleetServices(serverState: .running, states: [
            "botfleet-mcp": .failed, "app.botfleet.server": .running,
        ])
        let unknownCount = failing.entries.filter { $0.state == .unknown }.count
        XCTAssertEqual(SidebarBadges.services(failing.entries, readAt: now, now: now), "\(1 + unknownCount)")

        let allRead = Dictionary(uniqueKeysWithValues: FleetServiceRegistry.all.map { ($0.id, ManagedServiceState.running) })
        let clean = FleetServices(serverState: .running, states: allRead)
        XCTAssertNil(SidebarBadges.services(clean.entries, readAt: now, now: now))
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

    func test_grid_filter_matches_the_state_it_names() {
        XCTAssertTrue(GridStateFilter.all.includes([.passed]))
        XCTAssertTrue(GridStateFilter.red.includes([.passed, .missing]))
        XCTAssertTrue(GridStateFilter.red.includes([.unknown]))
        XCTAssertFalse(GridStateFilter.red.includes([.drift, .unproven]))
        XCTAssertTrue(GridStateFilter.drift.includes([.drift]))
        XCTAssertFalse(GridStateFilter.drift.includes([.missing]))
        XCTAssertTrue(GridStateFilter.unproven.includes([.unproven]))
        XCTAssertTrue(GridStateFilter.attention.includes([.unproven]))
        XCTAssertFalse(GridStateFilter.attention.includes([.passed, .covered, .unsupported]))
    }

    /// A card's filter is taken once, so the next visit to the grid opens on every seat.
    func test_a_requested_filter_is_taken_once() {
        _ = GridStateFilter.takeRequested()
        GridStateFilter.request(.drift)
        XCTAssertEqual(GridStateFilter.takeRequested(), .drift)
        XCTAssertNil(GridStateFilter.takeRequested())
    }

    // MARK: Server Status when the report is not current

    private func service(_ unit: String, _ state: String) throws -> ServerService {
        try JSONDecoder().decode(ServerService.self, from: Data(#"{"unit":"\#(unit)","activeState":"\#(state)","controllable":true}"#.utf8))
    }

    func test_last_known_lists_the_report_timer_even_when_the_report_did_not() throws {
        let services = [try service("fleet-cli-git-update.timer", "active")]
        XCTAssertEqual(ServerLastKnown.units(services), ["fleet-cli-git-update.timer", OpsNames.reportTimerUnit])
        let withTimer = [try service(OpsNames.reportTimerUnit, "active")]
        XCTAssertEqual(ServerLastKnown.units(withTimer), [OpsNames.reportTimerUnit])
        XCTAssertEqual(ServerLastKnown.lastState(unit: OpsNames.reportTimerUnit, services: withTimer), "last known active")
        XCTAssertEqual(ServerLastKnown.lastState(unit: "other.timer", services: withTimer), "not in the last report")
    }

    /// The repairs come from the allowlist only, and never Start or Stop on an old state.
    func test_last_known_offers_only_the_allowlists_restart_and_logs() {
        let unit = OpsNames.reportTimerUnit
        let full = OpsAllowlist(serverUnits: [OpsAllowlist.Entry(name: unit, actions: ["start", "stop", "restart"])])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, allowlist: full),
                       [.serverUnit(verb: .restart, unit: unit), .logs(unit: unit)])
        let startOnly = OpsAllowlist(serverUnits: [OpsAllowlist.Entry(name: unit, actions: ["start"])])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, allowlist: startOnly), [.logs(unit: unit)])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, allowlist: OpsAllowlist()), [])
        XCTAssertEqual(ServerLastKnown.actions(unit: unit, allowlist: nil), [])
        for action in ServerLastKnown.actions(unit: unit, allowlist: full) {
            XCTAssertEqual(action.runsOn, .server)
        }
    }

    // MARK: Overview counts

    private func cell(_ state: CellState, rules: [CheckResult] = []) -> GridCell {
        GridCell(seat: "MM", kind: .skills, state: state, rules: rules, coverReason: nil)
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
