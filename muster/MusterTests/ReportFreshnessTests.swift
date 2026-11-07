import XCTest

/// A report's age and verdict,  worked out again at every moment rather than once at read time,
/// and the words that say what the report is.
///
/// Times are on Jay's clock:  2026-11-07 is after the change back from daylight time,  so
/// Chicago is six hours behind UTC and 20:14Z is 2:14 PM.
final class ReportFreshnessTests: XCTestCase {
    private let gap = "\u{00A0} "

    private func at(_ text: String) -> Date { FleetTime.parse(text)! }

    /// 2:14 PM on Saturday,  November 7,  2026.
    private var now: Date { at("2026-11-07T20:14:00Z") }
    /// 2:02 PM the same day.
    private var written: Date { at("2026-11-07T20:02:00Z") }

    // MARK: - A report crossing its window while the panel is open

    func test_a_report_crosses_its_window_without_being_read_again() {
        let window = TimeInterval(ServerStatusContract.maxAgeSeconds)
        let at44 = ReportFreshness(generatedAt: written, maxAge: window, now: written.addingTimeInterval(44 * 60))
        let at46 = ReportFreshness(generatedAt: written, maxAge: window, now: written.addingTimeInterval(46 * 60))
        XCTAssertEqual(at44.verdict, .current)
        XCTAssertEqual(at46.verdict, .stale)
        XCTAssertEqual(at44.ageWords, "44 minutes ago")
        XCTAssertEqual(at46.ageWords, "46 minutes ago")
    }

    func test_a_vm_reading_goes_stale_on_screen_when_its_report_does() {
        var reading = VmStatusReading()
        reading.generatedAt = written
        reading.isCurrent = true
        reading.state = .passed
        reading.age = "1m"

        let early = reading.aged(by: reading.freshness(now: written.addingTimeInterval(10 * 60)))
        XCTAssertTrue(early.isCurrent)
        XCTAssertEqual(early.state, .passed)
        XCTAssertEqual(early.age, "10m")

        let late = reading.aged(by: reading.freshness(now: written.addingTimeInterval(46 * 60)))
        XCTAssertFalse(late.isCurrent)
        XCTAssertEqual(late.state, .unknown)
        XCTAssertEqual(late.age, "46m")
    }

    func test_ageing_never_makes_an_old_reading_current() {
        var reading = MacServicesReading()
        reading.generatedAt = written
        reading.isCurrent = false
        reading.state = .unknown
        let aged = reading.aged(by: reading.freshness(now: written.addingTimeInterval(60)))
        XCTAssertFalse(aged.isCurrent)
        XCTAssertEqual(aged.state, .unknown)
    }

    func test_a_vm_copy_carries_the_time_the_doctor_copied_it() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-vm-copy-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("vm-one.json")
        try #"{"schemaVersion": 1, "tool": "fleet-vm-status", "generatedAt": "2026-11-07T20:02:00Z"}"#
            .write(to: url, atomically: true, encoding: .utf8)
        let copied = at("2026-11-07T20:10:00Z")
        try FileManager.default.setAttributes([.modificationDate: copied], ofItemAtPath: url.path)

        let reading = VmStatusReportReader.read(at: url, now: now)
        XCTAssertEqual(reading.copiedAt, copied)
        let freshness = reading.freshness(now: now)
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.vmCopiedByDoctor, freshness),
                       "VM report written 12 minutes ago, copied to this Mac by the doctor at 2:10 PM.")
    }

    func test_a_mac_services_reading_crosses_its_fifteen_minute_window() {
        var reading = MacServicesReading()
        reading.generatedAt = written
        reading.isCurrent = true
        reading.state = .passed
        XCTAssertTrue(reading.aged(by: reading.freshness(now: written.addingTimeInterval(14 * 60))).isCurrent)
        let lapsed = reading.aged(by: reading.freshness(now: written.addingTimeInterval(16 * 60)))
        XCTAssertFalse(lapsed.isCurrent)
        XCTAssertEqual(lapsed.state, .unknown)
    }

    func test_a_doctor_reading_lapses_every_host_to_unproven() {
        var reading = FleetToolsReading()
        reading.generatedAt = written
        reading.isCurrent = true
        reading.verdict = .passed
        reading.hosts = [
            FleetToolsHostVerdict(host: "mac", reachable: true, state: .passed, failing: 0, unknown: 0,
                                  stale: 0, current: 3, absent: 0, notableRows: [], reason: nil,
                                  generatedAt: written, age: "0s", expected: nil, doctorCommit: nil),
        ]
        let current = reading.aged(by: reading.freshness(now: written.addingTimeInterval(20 * 60)))
        XCTAssertTrue(current.isCurrent)
        XCTAssertEqual(current.verdict, .passed)
        XCTAssertEqual(current.hosts.first?.state, .passed)
        XCTAssertEqual(current.hosts.first?.age, "20m")

        let lapsed = reading.aged(by: reading.freshness(now: written.addingTimeInterval(50 * 60)))
        XCTAssertFalse(lapsed.isCurrent)
        XCTAssertEqual(lapsed.verdict, .unknown)
        XCTAssertEqual(lapsed.hosts.first?.state, .unproven)
        XCTAssertEqual(lapsed.age, "50m")
    }

    func test_a_worker_health_reading_lapses_to_unknown() {
        var reading = WorkerHealthReading()
        reading.generatedAt = written
        reading.isCurrent = true
        reading.state = .passed
        let lapsed = reading.aged(by: reading.freshness(now: written.addingTimeInterval(46 * 60)))
        XCTAssertFalse(lapsed.isCurrent)
        XCTAssertEqual(lapsed.state, .unknown)
    }

    func test_a_report_dated_ahead_of_the_clock_is_not_current() {
        let freshness = ReportFreshness(generatedAt: now.addingTimeInterval(5 * 60), maxAge: 45 * 60, now: now)
        XCTAssertEqual(freshness.verdict, .futureDated)
        XCTAssertNil(ReportFreshnessCopy.ageLine(.serverCopiedByDoctor, freshness))
        XCTAssertEqual(ReportFreshnessCopy.verdictNote(.serverCopiedByDoctor, freshness),
                       "It is dated ahead of this Mac's clock, so nothing in it is current.")
    }

    // MARK: - Wake from sleep

    func test_a_read_from_before_the_wake_is_checking_until_the_file_is_read_again() {
        let readBefore = written.addingTimeInterval(60)
        let woke = written.addingTimeInterval(4 * 60)
        let moment = written.addingTimeInterval(5 * 60)
        let stale = ReportFreshness(generatedAt: written, maxAge: 45 * 60, now: moment, readAt: readBefore, wokeAt: woke)
        XCTAssertEqual(stale.verdict, .checking)
        XCTAssertFalse(stale.verdict.isCurrent)
        XCTAssertEqual(ReportFreshnessCopy.verdictNote(.vmCopiedByDoctor, stale),
                       "This Mac woke since the last read, so the file is being read again.")

        let readAgain = ReportFreshness(generatedAt: written, maxAge: 45 * 60, now: moment, readAt: moment, wokeAt: woke)
        XCTAssertEqual(readAgain.verdict, .current)
    }

    func test_a_vm_card_read_before_the_wake_is_not_current() {
        var reading = VmStatusReading()
        reading.generatedAt = written
        reading.isCurrent = true
        reading.state = .passed
        let freshness = reading.freshness(now: written.addingTimeInterval(5 * 60),
                                          readAt: written.addingTimeInterval(60),
                                          wokeAt: written.addingTimeInterval(4 * 60))
        let aged = reading.aged(by: freshness)
        XCTAssertFalse(aged.isCurrent)
        XCTAssertEqual(aged.state, .unknown)
    }

    func test_after_a_long_sleep_the_menu_no_longer_reads_the_mac_as_ok() {
        // The services were just read again on wake,  so only the seat check is old.
        func menu(seatCheckedAt: Date) -> MachineMenu {
            MachineMenu(MachineMenuInputs(services: nil, lastReadAt: now, redCells: [],
                                          lastRunWasTrusted: true, seatCheckedAt: seatCheckedAt,
                                          status: nil, tools: nil, now: now))
        }
        let fresh = menu(seatCheckedAt: now.addingTimeInterval(-2 * 60))
        XCTAssertEqual(fresh.mac.health, .ok)
        XCTAssertTrue(fresh.plainText.contains("Seat Setup:  no red or drifted cells"))

        // Last checked at 11:32 PM last night.
        let slept = menu(seatCheckedAt: at("2026-11-07T05:32:00Z"))
        XCTAssertEqual(slept.mac.health, .notKnown)
        XCTAssertEqual(slept.barTitle(short: false), "Mac ? \u{00B7} Server ?")
        XCTAssertTrue(slept.plainText.contains("Seat Setup:  not current, last checked at 11:32 PM yesterday"),
                      slept.plainText)
        XCTAssertFalse(slept.plainText.contains("no red or drifted cells"))
    }

    func test_the_overview_says_an_old_seat_check_is_not_current() {
        XCTAssertEqual(SeatCheckFreshness.overviewLine(checkedAt: now.addingTimeInterval(-3 * 60), now: now),
                       "Last checked at 2:11 PM, 3 minutes ago")
        XCTAssertEqual(SeatCheckFreshness.overviewLine(checkedAt: at("2026-11-07T05:32:00Z"), now: now),
                       "Not current:  last checked at 11:32 PM yesterday, 14 hours ago")
        XCTAssertNil(SeatCheckFreshness.overviewLine(checkedAt: nil, now: now))

        let passing = RunVerdict(words: "All Passing", tone: .pass)
        XCTAssertEqual(SeatCheckFreshness.verdict(passing, isCurrent: true), passing)
        XCTAssertEqual(SeatCheckFreshness.verdict(passing, isCurrent: false),
                       RunVerdict(words: "Not Current", tone: .notKnown))
        let checking = RunVerdict(words: "Checking\u{2026}", tone: .neutral)
        XCTAssertEqual(SeatCheckFreshness.verdict(checking, isCurrent: false), checking)
    }

    // MARK: - Unreachable states

    func test_a_server_the_mac_could_not_reach_turns_its_actions_off_with_the_reason() {
        let checked = at("2026-11-07T20:10:00Z")
        let reach = ServerReach.unreachable(checkedAt: checked, lastReportAt: nil)
        let gate = ServerActionGate.make(reach: reach, asked: nil)
        XCTAssertTrue(gate.blocked)
        XCTAssertEqual(gate.note,
                       "The Mac could not reach the server at 2:10 PM, so this cannot run.\(gap)Ask The Server Now tries the connection again and changes nothing.")
    }

    func test_a_later_answer_to_ask_the_server_now_turns_the_actions_back_on() {
        let checked = at("2026-11-07T20:10:00Z")
        let reach = ServerReach.unreachable(checkedAt: checked, lastReportAt: nil)
        let answered = ServerAskResult(answered: true, at: at("2026-11-07T20:13:00Z"))
        let gate = ServerActionGate.make(reach: reach, asked: answered)
        XCTAssertFalse(gate.blocked)
        XCTAssertEqual(gate.note, "The server answered at 2:13 PM, after the Mac's last failed try.")

        let earlier = ServerAskResult(answered: true, at: at("2026-11-07T20:05:00Z"))
        XCTAssertTrue(ServerActionGate.make(reach: reach, asked: earlier).blocked)
    }

    func test_a_stopped_fetch_warns_and_does_not_turn_actions_off() {
        let reach = ServerReach.fetchStopped(lastFetchAt: nil, lastReportAt: nil)
        let gate = ServerActionGate.make(reach: reach, asked: nil)
        XCTAssertFalse(gate.blocked)
        XCTAssertEqual(gate.note,
                       "The Mac has no current word from the server, so it may not be reachable.\(gap)The plan asks the server first, and nothing runs if it cannot.")
    }

    func test_a_current_report_leaves_actions_on_with_nothing_to_say() {
        let reach = ServerReach.current(reportAt: written, copiedAt: nil)
        XCTAssertEqual(ServerActionGate.make(reach: reach, asked: nil), .open)
        let failed = ServerActionGate.make(reach: reach, asked: ServerAskResult(answered: false, at: now))
        XCTAssertFalse(failed.blocked)
        XCTAssertEqual(failed.note,
                       "The server did not answer at 2:14 PM.\(gap)The plan asks the server first, and nothing runs if it cannot.")
    }

    func test_fetch_is_offered_where_the_menu_offers_it() {
        XCTAssertTrue(ServerReach.fetchStopped(lastFetchAt: nil, lastReportAt: nil).offersFetch)
        XCTAssertTrue(ServerReach.unreachable(checkedAt: nil, lastReportAt: nil).offersFetch)
        XCTAssertTrue(ServerReach.noReport.offersFetch)
        XCTAssertTrue(ServerReach.cannotRead(.notFromServer).offersFetch)
        XCTAssertTrue(ServerReach.cannotRead(.unreadable).offersFetch)
        XCTAssertFalse(ServerReach.cannotRead(.appOutOfDate).offersFetch)
        XCTAssertFalse(ServerReach.cannotRead(.reporterOutOfDate).offersFetch)
        XCTAssertFalse(ServerReach.reportStopped(reachedAt: nil, reportAt: written).offersFetch)
        XCTAssertFalse(ServerReach.futureDated.offersFetch)
        XCTAssertFalse(ServerReach.notReadYet.offersFetch)
        XCTAssertFalse(ServerReach.current(reportAt: written, copiedAt: nil).offersFetch)

        // The menu offers the fetch from the same rule.
        var status = ServerStatusReading()
        status.fileMissing = true
        var tools = FleetToolsReading()
        tools.generatedAt = now
        tools.isCurrent = true
        let menu = MachineMenu(MachineMenuInputs(status: status, tools: tools, now: now))
        XCTAssertEqual(menu.server.reach, .noReport)
        XCTAssertTrue(menu.plainText.contains("Fetch From The Server Again\u{2026}"))
    }

    func test_a_stale_or_unreachable_server_says_what_to_do_next() {
        XCTAssertEqual(ServerNextStep.sentence(.fetchStopped(lastFetchAt: nil, lastReportAt: nil)),
                       "The Mac's copy is not being refreshed.\(gap)Fetch From The Server Again restarts the Fleet Tools Doctor on this Mac, which asks the server for a new copy.")
        XCTAssertEqual(ServerNextStep.sentence(.unreachable(checkedAt: nil, lastReportAt: nil)),
                       "The Mac could not reach the server on its last try.\(gap)Ask The Server Now tries the connection and changes nothing;  once it answers, Fetch From The Server Again asks for a new copy.")
        XCTAssertEqual(ServerNextStep.sentence(.reportStopped(reachedAt: nil, reportAt: written)),
                       "The server answered, but its report stopped.\(gap)Restart the Server Report Timer below, or read its logs.")
        XCTAssertNil(ServerNextStep.sentence(.current(reportAt: written, copiedAt: nil)))
        XCTAssertNil(ServerNextStep.sentence(.notReadYet))
    }

    func test_the_doctor_line_names_the_job_and_its_next_run() {
        XCTAssertEqual(DoctorSchedule.serverCopyLine(lastRunAt: at("2026-11-07T20:10:00Z"), now: now),
                       "The Fleet Tools Doctor on this Mac copies the server's report every 15 minutes.\(gap)Its next run is due at 2:25 PM.")
        XCTAssertEqual(DoctorSchedule.serverCopyLine(lastRunAt: at("2026-11-07T19:30:00Z"), now: now),
                       "The Fleet Tools Doctor on this Mac copies the server's report every 15 minutes.\(gap)Its next run was due at 1:45 PM and has not come.")
        XCTAssertEqual(DoctorSchedule.serverCopyLine(lastRunAt: nil, now: now),
                       "The Fleet Tools Doctor on this Mac copies the server's report every 15 minutes.")
    }

    // MARK: - The exact words

    func test_each_origin_says_who_wrote_the_report_and_how_it_came_here() {
        let copied = at("2026-11-07T20:10:00Z")
        let server = ReportFreshness(generatedAt: written, copiedAt: copied, maxAge: 45 * 60, now: now)
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.serverCopiedByDoctor, server),
                       "Server report written 12 minutes ago, copied to this Mac by the doctor at 2:10 PM.")
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.vmCopiedByDoctor, server),
                       "VM report written 12 minutes ago, copied to this Mac by the doctor at 2:10 PM.")

        let local = ReportFreshness(generatedAt: written, maxAge: 45 * 60, now: now)
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.serverCopiedByDoctor, local),
                       "Server report written 12 minutes ago, copied to this Mac by the doctor.")
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.doctorOnMac, local),
                       "Doctor's report written on this Mac 12 minutes ago.")
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.macCollector("Hog Hunter"), local),
                       "Written by Hog Hunter on this Mac 12 minutes ago.")
        XCTAssertEqual(ReportFreshnessCopy.ageLine(.collectedNow, local),
                       "Collected live from this Mac 12 minutes ago, without writing a file.")

        XCTAssertNil(ReportFreshnessCopy.ageLine(.serverCopiedByDoctor,
                                                 ReportFreshness(generatedAt: nil, maxAge: 45 * 60, now: now)))
        // No line says "Report read",  which named the age of a copy as if it were a live look.
        for origin in [ReportOrigin.serverCopiedByDoctor, .vmCopiedByDoctor, .doctorOnMac, .collectedNow] {
            XCTAssertFalse(ReportFreshnessCopy.ageLine(origin, server)?.contains("Report read") ?? true)
        }
    }

    func test_a_stale_copy_says_what_it_means_and_what_to_do() {
        let stale = ReportFreshness(generatedAt: written, maxAge: 45 * 60, now: written.addingTimeInterval(50 * 60))
        XCTAssertEqual(ReportFreshnessCopy.verdictNote(.serverCopiedByDoctor, stale),
                       "It is past its 45 minute window, so its rows are old evidence, not current health.\(gap)The doctor copies a new one every 15 minutes;  if none arrives, use Fetch From The Server Again.")
        XCTAssertEqual(ReportFreshnessCopy.verdictNote(.vmCopiedByDoctor, stale),
                       "It is past its 45 minute window, so its rows are old evidence, not current health.\(gap)The doctor copies VM reports when it runs with --vms;  docs/protocols/fleet-vm-status.md has the steps.")
        XCTAssertEqual(ReportFreshnessCopy.verdictNote(.macCollector("Hog Hunter"), stale),
                       "It is past its 45 minute window, so its rows are old evidence, not current health.")
        XCTAssertNil(ReportFreshnessCopy.verdictNote(.serverCopiedByDoctor,
                                                     ReportFreshness(generatedAt: written, maxAge: 45 * 60, now: now)))
    }

    func test_ages_are_said_in_words() {
        XCTAssertEqual(FreshnessWords.ago(0), "less than a minute ago")
        XCTAssertEqual(FreshnessWords.ago(-30), "less than a minute ago")
        XCTAssertEqual(FreshnessWords.ago(59), "less than a minute ago")
        XCTAssertEqual(FreshnessWords.ago(60), "1 minute ago")
        XCTAssertEqual(FreshnessWords.ago(5 * 60), "5 minutes ago")
        XCTAssertEqual(FreshnessWords.ago(3600), "1 hour ago")
        XCTAssertEqual(FreshnessWords.ago(8 * 3600 + 59 * 60), "8 hours ago")
        XCTAssertEqual(FreshnessWords.ago(86400), "1 day ago")
        XCTAssertEqual(FreshnessWords.ago(3 * 86400), "3 days ago")
        XCTAssertEqual(FreshnessWords.window(45 * 60), "45 minute")
        XCTAssertEqual(FreshnessWords.window(5 * 60), "5 minute")
        XCTAssertEqual(FreshnessWords.window(3600), "1 hour")
    }

    func test_a_time_on_another_day_carries_its_day() {
        XCTAssertEqual(FreshnessWords.when(written, now: now), "at 2:02 PM")
        XCTAssertEqual(FreshnessWords.when(at("2026-11-07T05:32:00Z"), now: now), "at 11:32 PM yesterday")
        XCTAssertEqual(FreshnessWords.when(at("2026-11-04T05:32:00Z"), now: now), "on Nov 3 at 11:32 PM")
        XCTAssertEqual(FreshnessWords.when(at("2025-11-04T05:32:00Z"), now: now), "on Nov 3, 2025 at 11:32 PM")
        XCTAssertEqual(FreshnessWords.stamp(written, now: now), "2:02 PM")
        XCTAssertEqual(FreshnessWords.stamp(at("2026-11-07T05:32:00Z"), now: now), "11:32 PM yesterday")
        XCTAssertEqual(FreshnessWords.stamp(at("2026-11-04T05:32:00Z"), now: now), "Nov 3, 11:32 PM")
    }

    func test_the_seat_menu_line_names_the_last_check() {
        XCTAssertEqual(SeatCheckFreshness.menuLine(checkedAt: at("2026-11-07T05:32:00Z"), now: now),
                       "Seat Setup:  not current, last checked at 11:32 PM yesterday")
        XCTAssertTrue(SeatCheckFreshness.isCurrent(checkedAt: now.addingTimeInterval(-15 * 60), now: now))
        XCTAssertFalse(SeatCheckFreshness.isCurrent(checkedAt: now.addingTimeInterval(-16 * 60), now: now))
    }

    func test_apps_and_work_says_its_age_and_turns_not_current() {
        var clock = FleetViewRefreshClock()
        XCTAssertNil(clock.headerText(now: now))
        clock.recordSuccess(generatedAt: "2026-11-07T19:15:00Z", at: at("2026-11-07T19:16:00Z"))
        XCTAssertEqual(clock.headerText(now: at("2026-11-07T19:18:00Z")), "Collected 1:15 PM, 3 minutes ago")
        XCTAssertEqual(FleetViewFreshness.tone(collectedAt: at("2026-11-07T19:15:00Z"), now: at("2026-11-07T19:18:00Z")), .fresh)
        XCTAssertEqual(FleetViewFreshness.tone(collectedAt: at("2026-11-07T19:15:00Z"), now: at("2026-11-07T19:45:00Z")), .aging)
        XCTAssertEqual(clock.headerText(now: at("2026-11-07T21:15:00Z")), "Collected 1:15 PM, 2 hours ago, not current")
        clock.recordFailure(at: at("2026-11-07T21:20:00Z"))
        XCTAssertEqual(clock.headerText(now: at("2026-11-07T21:20:00Z")),
                       "Last attempt 3:20 PM failed \u{00B7} Collected 1:15 PM, 2 hours ago, not current")
    }

    func test_low_power_mode_reads_less_often() {
        XCTAssertEqual(RereadSchedule.interval(base: 60, lowPower: false), 60)
        XCTAssertEqual(RereadSchedule.interval(base: 60, lowPower: true), 240)
    }

    // MARK: - The server's file list

    func test_the_file_list_says_what_changed_is_measured_against() {
        XCTAssertEqual(ServerConfigCopy.comparisonNote,
                       "Each file is compared with the server's previous report only, not with the checked-in version.\(gap)An edit made before the previous report no longer shows here.")
    }

    // MARK: - What a redeploy restarts

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    func test_a_redeploy_says_it_restarts_every_container_of_the_application() throws {
        var reading = ServerStatusReading()
        reading.applications = [try decode(ServerApplication.self,
            #"{"uuid": "abc123def456", "name": "congress-trade", "containers": 2, "services": ["web", "worker"]}"#)]
        let web = try decode(CoolifyResource.self,
            #"{"uuid": "abc123def456", "name": "congress-trade-web", "type": "application", "status": "running", "service": "web"}"#)
        reading.coolify = [web]
        let scope = RedeployScope.make(reading: reading, resource: web)
        XCTAssertEqual(scope.sentence, "This redeploys all 2 containers of congress-trade:  web and worker.")

        RedeployScopeNotes.shared.note(scope, uuid: "abc123def456")
        XCTAssertEqual(RedeployScopeNotes.shared.scope(for: "abc123def456"), scope)
    }

    func test_a_redeploy_without_an_application_list_names_its_rows() throws {
        var reading = ServerStatusReading()
        let web = try decode(CoolifyResource.self,
            #"{"uuid": "abc123def456", "name": "web-1", "type": "application", "status": "running", "service": "web"}"#)
        let worker = try decode(CoolifyResource.self,
            #"{"uuid": "abc123def456", "name": "worker-1", "type": "application", "status": "running", "service": "worker"}"#)
        reading.coolify = [web, worker]
        XCTAssertEqual(RedeployScope.make(reading: reading, resource: web).sentence,
                       "This redeploys all of web-1, not only one container:  web and worker.")

        var single = ServerStatusReading()
        single.applications = [try decode(ServerApplication.self,
            #"{"uuid": "abc123def456", "name": "muster-web", "containers": 1, "services": ["web"]}"#)]
        single.coolify = [web]
        XCTAssertEqual(RedeployScope.make(reading: single, resource: web).sentence,
                       "This redeploys the one container of muster-web:  web.")
        XCTAssertEqual(RedeployScope.list(["a", "b", "c"]), "a, b and c")
    }

    // MARK: - Background Agents:  the newer reading wins

    private func agents(at date: Date, problem: String? = nil) -> MacServicesReading {
        var reading = MacServicesReading()
        reading.generatedAt = problem == nil ? date : nil
        reading.problem = problem
        reading.isCurrent = problem == nil
        reading.state = problem == nil ? .passed : .unknown
        return reading
    }

    func test_a_newer_saved_file_replaces_older_rows() {
        var shown = MacAgentsShown()
        shown.takeSaved(agents(at: written))
        XCTAssertEqual(shown.reading?.generatedAt, written)

        let newer = written.addingTimeInterval(5 * 60)
        shown.takeSaved(agents(at: newer))
        XCTAssertEqual(shown.reading?.generatedAt, newer)
        XCTAssertEqual(shown.source, .savedFile)

        // An older file,  or one that could not be read,  never replaces newer good rows.
        shown.takeSaved(agents(at: written))
        XCTAssertEqual(shown.reading?.generatedAt, newer)
        shown.takeSaved(agents(at: written, problem: "The file could not be read."))
        XCTAssertEqual(shown.reading?.generatedAt, newer)
        XCTAssertNil(shown.reading?.problem)
    }

    func test_a_failed_collection_keeps_the_last_good_rows_and_says_why() {
        var shown = MacAgentsShown()
        shown.takeSaved(agents(at: written))
        shown.takeCollected(agents(at: now, problem: "The Mac services collector timed out; no current agent state was obtained."))
        XCTAssertEqual(shown.reading?.generatedAt, written)
        XCTAssertEqual(shown.collectProblem,
                       "The Mac services collector timed out; no current agent state was obtained.")

        shown.takeCollected(agents(at: now))
        XCTAssertEqual(shown.reading?.generatedAt, now)
        XCTAssertEqual(shown.source, .collectedLive)
        XCTAssertNil(shown.collectProblem)

        // A saved file newer than the collection takes its place.
        let later = now.addingTimeInterval(5 * 60)
        shown.takeSaved(agents(at: later))
        XCTAssertEqual(shown.reading?.generatedAt, later)
        XCTAssertEqual(shown.source, .savedFile)
    }

    func test_a_failed_first_collection_shows_its_problem() {
        var shown = MacAgentsShown()
        shown.takeCollected(agents(at: now, problem: "The collector failed."))
        XCTAssertEqual(shown.reading?.problem, "The collector failed.")
        XCTAssertNil(shown.collectProblem)
    }
}
