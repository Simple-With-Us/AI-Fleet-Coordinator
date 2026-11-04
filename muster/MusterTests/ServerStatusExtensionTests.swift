import XCTest

/// The newer halves of the server's report:  the host's numbers,  the fleet's units and the
/// file summary.
///
/// The rule under test is that every new half is optional:  a report from a collector that
/// predates them reads exactly as it did,  and a half that is malformed costs that half alone.
final class ServerStatusExtensionTests: XCTestCase {
    private var generated: Date { FleetTime.parse("2026-10-28T15:00:00Z")! }

    /// Write a fixture to a file and read it back,  some minutes after it was written.
    private func readFixture(_ name: String, minutesAfter minutes: Double = 2) throws -> ServerStatusReading {
        let json = try fixture(name, "json")
        return try readText(json, minutesAfter: minutes)
    }

    private func readText(_ json: String, minutesAfter minutes: Double = 2) throws -> ServerStatusReading {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-server-ext-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("server-status.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return ServerStatusReportReader.read(at: url, now: generated.addingTimeInterval(minutes * 60))
    }

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    // MARK: - The current shape

    func test_the_current_report_carries_the_host_numbers() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        let metrics = try XCTUnwrap(reading.hostMetrics)
        XCTAssertEqual(metrics.cpuCount, 4)
        XCTAssertEqual(metrics.loadText, "1.50 1.25 1.00")
        XCTAssertEqual(try XCTUnwrap(metrics.loadFraction), 0.375, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(metrics.memory?.usedFraction), 0.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(metrics.swap?.usedFraction), 0.25, accuracy: 0.0001)
        XCTAssertEqual(metrics.uptimeText, "2d")
        XCTAssertEqual(metrics.disks.map(\.mount), ["/", "/mnt/example"])
        XCTAssertEqual(try XCTUnwrap(metrics.disks.first?.usedFraction), 0.9, accuracy: 0.0001)
        XCTAssertEqual(metrics.disks.first?.state, .drift)
        XCTAssertEqual(metrics.disks.last?.state, .passed)
        // The fullest disk is the worst number on the host.
        XCTAssertEqual(reading.hostState, .drift)
    }

    func test_the_current_report_carries_the_services() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertEqual(reading.services.count, 2)
        let timer = try XCTUnwrap(reading.services.first)
        XCTAssertEqual(timer.unit, "fleet-server-status.timer")
        XCTAssertEqual(timer.scope, "user")
        XCTAssertEqual(timer.id, "user/fleet-server-status.timer")
        XCTAssertTrue(timer.controllable)
        XCTAssertEqual(timer.state, .passed)
        XCTAssertEqual(timer.stateText, "active (waiting)")
        XCTAssertEqual(timer.enabled, "enabled")
        XCTAssertNotNil(timer.sinceText)
        let failed = try XCTUnwrap(reading.services.last)
        XCTAssertFalse(failed.controllable)
        XCTAssertEqual(failed.state, .missing)
        XCTAssertEqual(reading.servicesState, .missing)
        // A failed unit is the report's worst half.
        XCTAssertEqual(reading.state, .missing)
    }

    func test_the_current_report_carries_the_file_summary_and_info_files() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertTrue(reading.filesTruncated)
        XCTAssertEqual(reading.filesSummary?.found, 812)
        XCTAssertEqual(reading.filesSummary?.limit, 500)
        // A README change is information:  it is listed,  and it is not drift.
        XCTAssertEqual(reading.noticedFiles.map(\.fileName), ["README.md"])
        XCTAssertTrue(reading.driftedFiles.isEmpty)
        XCTAssertEqual(reading.noticedFiles.first?.fileDrift, .info)
        XCTAssertEqual(reading.noticedFiles.first?.state, .passed)
        XCTAssertEqual(reading.configState, .passed)
    }

    // MARK: - The old shape

    func test_an_old_report_without_the_new_halves_reads_as_before() throws {
        let reading = try readFixture("server_status_old_shape")
        XCTAssertNil(reading.problem)
        XCTAssertNil(reading.hostMetrics)
        XCTAssertTrue(reading.services.isEmpty)
        XCTAssertNil(reading.filesSummary)
        XCTAssertFalse(reading.filesTruncated)
        XCTAssertEqual(reading.state, .passed)
        // The halves it does not carry prove nothing,  and they do not pull the report down.
        XCTAssertEqual(reading.hostState, .unproven)
        XCTAssertEqual(reading.servicesState, .unproven)
    }

    // MARK: - The malformed shape

    func test_a_malformed_new_half_costs_that_half_alone() throws {
        let reading = try readFixture("server_status_malformed_halves")
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.coolify.count, 1)
        XCTAssertNil(reading.hostMetrics)
        XCTAssertTrue(reading.services.isEmpty)
        XCTAssertNil(reading.filesSummary)
        // Coolify passes,  and the empty halves are unproven rather than a pass.
        XCTAssertEqual(reading.state, .unproven)
    }

    func test_a_service_with_missing_fields_is_unproven_and_not_controllable() throws {
        let service = try decode(ServerService.self, #"{"unit": "example.service", "controllable": "yes", "enabled": true}"#)
        XCTAssertFalse(service.controllable)
        XCTAssertEqual(service.state, .unproven)
        XCTAssertEqual(service.id, "system/example.service")
        XCTAssertEqual(service.enabled, "enabled")
        XCTAssertEqual(service.stateText, "unknown")
        XCTAssertNil(service.sinceText)
    }

    func test_a_stopped_unit_reads_as_drift() throws {
        let service = try decode(ServerService.self, #"{"unit": "example.timer", "activeState": "inactive", "subState": "dead"}"#)
        XCTAssertEqual(service.state, .drift)
        XCTAssertEqual(service.stateText, "inactive (dead)")
    }

    func test_a_disk_reads_by_how_full_it_is() throws {
        let full = try decode(HostDisk.self, #"{"mount": "/", "usedBytes": 96, "freeBytes": 4}"#)
        XCTAssertEqual(full.state, .missing)
        let roomy = try decode(HostDisk.self, #"{"mount": "/", "usedBytes": 20, "freeBytes": 80, "totalBytes": 100}"#)
        XCTAssertEqual(roomy.state, .passed)
        let unread = try decode(HostDisk.self, #"{"mount": "/mnt/x"}"#)
        XCTAssertNil(unread.usedFraction)
        XCTAssertEqual(unread.state, .unproven)
    }

    func test_a_host_without_swap_has_no_swap_share() throws {
        let swap = try decode(HostSwap.self, #"{"totalBytes": 0, "freeBytes": 0}"#)
        XCTAssertNil(swap.usedFraction)
    }

    func test_host_numbers_of_the_wrong_type_are_left_out() throws {
        let metrics = try decode(
            HostMetrics.self,
            #"{"loadAverage": "high", "cpuCount": "four", "uptimeSeconds": 60, "disks": [{"mount": "/", "usedBytes": 1, "freeBytes": 1}]}"#
        )
        XCTAssertTrue(metrics.loadAverage.isEmpty)
        XCTAssertNil(metrics.cpuCount)
        XCTAssertNil(metrics.loadFraction)
        XCTAssertEqual(metrics.uptimeText, "1m")
        XCTAssertEqual(metrics.disks.count, 1)
    }

    // MARK: - The stale shape

    func test_a_stale_report_shows_no_host_or_service_state() throws {
        let reading = try readFixture("server_status_current", minutesAfter: 46)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertEqual(reading.hostState, .unproven)
        XCTAssertEqual(reading.servicesState, .unproven)
    }

    // MARK: - Redeploy

    /// A row is offered a redeploy when the server would take it and the application is still
    /// there.  `plain-app` says nothing about its uuid and passes the name test `uuid_from_label`
    /// applies in fleet-ops-control.py,  so it stays offered for a report from before `actions`
    /// existed.  `gone-app` is absent and `listed-nothing` lists no redeploy,  so neither is.
    func test_a_live_application_the_server_would_take_is_offered_a_redeploy() throws {
        let reading = try readFixture("server_status_coolify_rows")
        XCTAssertNil(reading.problem)
        let offered = reading.coolify.filter { reading.offersRedeploy($0) }.map(\.name)
        XCTAssertEqual(offered, ["labelled-app", "plain-app", "listed-redeploy"])
    }

    func test_a_row_that_is_gone_is_never_offered_a_redeploy() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let gone = try XCTUnwrap(reading.coolify.first { $0.name == "gone-app" })
        // The rule the server applies still passes,  which is why the offer has to check.
        XCTAssertTrue(gone.isRedeployable)
        XCTAssertFalse(reading.offersRedeploy(gone))
    }

    func test_a_row_that_lists_no_redeploy_is_not_offered_one_and_says_why() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let row = try XCTUnwrap(reading.coolify.first { $0.name == "listed-nothing" })
        XCTAssertTrue(row.isRedeployable)
        XCTAssertFalse(reading.offersRedeploy(row))
        XCTAssertEqual(row.actions, [])
        XCTAssertEqual(row.redeployBlockedReason, "not_allowed")
    }

    func test_a_service_or_database_row_is_not_redeployable() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let service = try XCTUnwrap(reading.coolify.first { $0.name == "a-service" })
        XCTAssertFalse(service.isRedeployable)
        let database = try XCTUnwrap(reading.coolify.first { $0.name == "a-database" })
        XCTAssertFalse(database.isRedeployable)
        let standalone = try XCTUnwrap(reading.coolify.first { $0.name == "standalone-box" })
        XCTAssertFalse(standalone.isRedeployable)
    }

    func test_a_row_whose_uuid_is_its_name_is_not_redeployable() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let named = try XCTUnwrap(reading.coolify.first { $0.name == "named-app" })
        XCTAssertFalse(named.isRedeployable)
        let suffixed = try XCTUnwrap(reading.coolify.first { $0.name == "suffixed (service)" })
        XCTAssertFalse(suffixed.isRedeployable)
    }

    func test_a_row_that_says_where_its_uuid_came_from_is_believed_only_for_a_label() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let labelled = try XCTUnwrap(reading.coolify.first { $0.name == "labelled-app" })
        XCTAssertTrue(labelled.isRedeployable)
        let fromName = try XCTUnwrap(reading.coolify.first { $0.name == "source-name" })
        XCTAssertFalse(fromName.isRedeployable)
        // A source that is not text still says something,  and it does not say `label`.
        let odd = try XCTUnwrap(reading.coolify.first { $0.name == "source-odd" })
        XCTAssertTrue(odd.declaresUUIDSource)
        XCTAssertNil(odd.uuidSource)
        XCTAssertFalse(odd.isRedeployable)
    }

    func test_the_first_row_with_a_uuid_decides_like_the_server() throws {
        let reading = try readFixture("server_status_coolify_rows")
        // The server redeploys the first row whose uuid matches,  and that row is a service.
        let app = try XCTUnwrap(reading.coolify.first { $0.name == "shared-app" })
        XCTAssertTrue(app.isRedeployable)
        XCTAssertFalse(reading.offersRedeploy(app))
    }

    func test_an_info_drift_is_a_known_drift() {
        XCTAssertEqual(ServerStatusDrift(reportValue: "info"), .info)
        XCTAssertEqual(ServerStatusDrift(reportValue: "wobble"), .unknown)
    }

    // MARK: - Leftovers

    func test_an_absent_row_says_since_when_and_keeps_reading_as_drift() throws {
        let row = try decode(
            CoolifyResource.self,
            #"{"uuid": "u", "name": "gone", "status": "absent", "absentSince": "2026-10-28T14:00:00Z", "service": "web", "container": "web-1"}"#
        )
        XCTAssertEqual(row.absentSince, "2026-10-28T14:00:00Z")
        XCTAssertEqual(row.service, "web")
        XCTAssertEqual(row.container, "web-1")
        XCTAssertEqual(row.state, .drift)
    }

    func test_two_overlapping_deploys_are_two_rows() throws {
        let one = try decode(CoolifyResource.self, #"{"uuid": "u", "name": "app", "image": "i", "container": "app-1"}"#)
        let two = try decode(CoolifyResource.self, #"{"uuid": "u", "name": "app", "image": "i", "container": "app-2"}"#)
        XCTAssertNotEqual(one.id, two.id)
    }

    func test_a_missing_file_says_since_when() throws {
        let file = try decode(WatchedFile.self, #"{"path": "/srv/a/AGENTS.md", "drift": "missing", "missingSince": "2026-10-28T14:00:00Z"}"#)
        XCTAssertEqual(file.missingSince, "2026-10-28T14:00:00Z")
        XCTAssertEqual(file.state, .missing)
    }

    func test_a_removed_checkout_is_information_and_holds_nothing_red() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-server-status", "generatedAt": "2026-10-28T15:00:00Z", "coolify": [{"uuid": "u", "name": "a", "status": "running"}], "processes": [{"pid": 1}], "files": [{"path": "/srv/a/AGENTS.md", "drift": "unchanged"}], "removedCheckouts": [{"repo": "Gone", "files": 15, "noticedAt": "2026-10-28T14:00:00Z"}]}
        """
        let reading = try readText(json)
        XCTAssertEqual(reading.removedCheckouts.map(\.repo), ["Gone"])
        XCTAssertEqual(reading.removedCheckouts.first?.files, 15)
        XCTAssertEqual(reading.configState, .passed)
        XCTAssertEqual(reading.state, .passed)
    }

    // MARK: - Container signals

    func test_a_crash_loop_is_a_failure_and_a_finished_job_is_a_pass() throws {
        let looping = try decode(CoolifyResource.self, #"{"uuid": "u", "name": "a", "status": "restarting", "restartCount": 12}"#)
        XCTAssertEqual(looping.resourceStatus, .restarting)
        XCTAssertEqual(looping.state, .missing)
        XCTAssertEqual(looping.restartCount, 12)
        let finished = try decode(CoolifyResource.self, #"{"uuid": "u", "name": "a", "status": "finished", "exitCode": 0, "oomKilled": false}"#)
        XCTAssertEqual(finished.resourceStatus, .finished)
        XCTAssertEqual(finished.state, .passed)
        XCTAssertEqual(finished.exitCode, 0)
        XCTAssertEqual(finished.oomKilled, false)
    }

    func test_a_status_this_app_does_not_know_is_still_unproven() throws {
        let row = try decode(CoolifyResource.self, #"{"uuid": "u", "name": "a", "status": "paused"}"#)
        XCTAssertEqual(row.state, .unproven)
    }

    func test_a_whole_second_time_from_the_collector_is_read() throws {
        // The collector respells docker's nine fractional digits,  so this is what arrives.
        let row = try decode(CoolifyResource.self, #"{"uuid": "u", "name": "a", "updatedAt": "2026-11-04T19:26:46Z"}"#)
        XCTAssertNotNil(row.updatedText)
    }

    func test_a_row_with_fields_of_the_wrong_type_keeps_the_rest() throws {
        let row = try decode(
            CoolifyResource.self,
            #"{"uuid": "u", "name": "a", "status": "running", "health": 3, "restartCount": "many", "exitCode": "x", "oomKilled": "no", "actions": "redeploy", "absentSince": 7}"#
        )
        XCTAssertEqual(row.state, .passed)
        XCTAssertNil(row.health)
        XCTAssertNil(row.restartCount)
        XCTAssertNil(row.exitCode)
        XCTAssertNil(row.oomKilled)
        XCTAssertNil(row.actions)
        XCTAssertNil(row.absentSince)
    }

    // MARK: - Timers and failed units

    func test_the_current_report_carries_what_a_timer_did() throws {
        let reading = try readFixture("server_status_current")
        let timer = try XCTUnwrap(reading.services.first)
        XCTAssertEqual(timer.job, "fleet-server-status.service")
        XCTAssertEqual(timer.lastRunAt, "2026-10-28T14:45:00Z")
        XCTAssertEqual(timer.lastResult, "success")
        XCTAssertEqual(timer.lastFinishedAt, "2026-10-28T14:45:07Z")
        XCTAssertEqual(timer.nextRunAt, "2026-10-28T15:15:00Z")
        XCTAssertTrue(timer.selfObserved)
        XCTAssertFalse(timer.watched)
        XCTAssertFalse(timer.lastRunFailed)
        XCTAssertEqual(timer.actions, ["start", "restart", "logs"])
        XCTAssertEqual(timer.role, "reporting")
        XCTAssertEqual(timer.stopConsequence, "Stopping this stops the server's report.")
        let watched = try XCTUnwrap(reading.services.last)
        XCTAssertTrue(watched.watched)
        XCTAssertEqual(watched.actions, [])
        XCTAssertFalse(watched.controllable)
    }

    func test_an_active_timer_whose_job_failed_is_a_failure() throws {
        let timer = try decode(
            ServerService.self,
            #"{"unit": "a.timer", "activeState": "active", "subState": "waiting", "lastResult": "exit-code", "lastExitCode": 1}"#
        )
        XCTAssertTrue(timer.lastRunFailed)
        XCTAssertEqual(timer.state, .missing)
        XCTAssertEqual(timer.lastExitCode, 1)
    }

    func test_an_active_timer_with_no_result_or_a_skipped_job_is_not_a_failure() throws {
        for fields in [#""#, #", "lastResult": "success""#, #", "lastResult": "exec-condition""#, #", "lastResult": 7"#] {
            let timer = try decode(ServerService.self, #"{"unit": "a.timer", "activeState": "active""# + fields + "}")
            XCTAssertFalse(timer.lastRunFailed, fields)
            XCTAssertEqual(timer.state, .passed, fields)
        }
    }

    func test_a_stopped_timer_stays_drift_whatever_its_job_last_did() throws {
        let timer = try decode(ServerService.self, #"{"unit": "a.timer", "activeState": "inactive", "lastResult": "exit-code"}"#)
        XCTAssertEqual(timer.state, .drift)
    }

    func test_a_failed_unit_nothing_else_names_turns_the_services_half_red() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-server-status", "generatedAt": "2026-10-28T15:00:00Z", "coolify": [{"uuid": "u", "name": "a", "status": "running"}], "processes": [{"pid": 1}], "files": [{"path": "/srv/a/AGENTS.md", "drift": "unchanged"}], "failedUnits": [{"unit": "sync.service", "scope": "system", "since": "2026-10-28T14:10:00Z", "result": "exit-code", "exitCode": 1}]}
        """
        let reading = try readText(json)
        XCTAssertEqual(reading.failedUnits?.map(\.unit), ["sync.service"])
        XCTAssertEqual(reading.failedUnits?.first?.id, "system/sync.service")
        XCTAssertEqual(reading.failedUnits?.first?.exitCode, 1)
        XCTAssertEqual(reading.servicesState, .missing)
        XCTAssertEqual(reading.state, .missing)
    }

    func test_no_failed_units_read_is_not_the_same_as_none_failed() throws {
        let old = try readFixture("server_status_old_shape")
        XCTAssertNil(old.failedUnits)
        let json = """
        {"schemaVersion": 1, "tool": "fleet-server-status", "generatedAt": "2026-10-28T15:00:00Z", "coolify": [{"uuid": "u", "name": "a", "status": "running"}], "processes": [{"pid": 1}], "files": [{"path": "/srv/a/AGENTS.md", "drift": "unchanged"}], "failedUnits": []}
        """
        let reading = try readText(json)
        XCTAssertEqual(reading.failedUnits, [])
        XCTAssertEqual(reading.state, .passed)
    }

    func test_a_stale_report_says_nothing_from_its_failed_units() throws {
        let reading = try readFixture("server_status_current", minutesAfter: 46)
        XCTAssertEqual(reading.servicesState, .unproven)
        XCTAssertEqual(reading.state, .unknown)
    }

    // MARK: - The other additions

    func test_the_current_report_carries_the_additions() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertEqual(reading.failedUnits?.count, 1)
        XCTAssertEqual(reading.removedCheckouts.first?.repo, "Example-Removed")
        XCTAssertEqual(reading.applications.first?.containers, 2)
        XCTAssertEqual(reading.applications.first?.services, ["web", "worker"])
        XCTAssertEqual(reading.processSampleSeconds, 2.0)
        XCTAssertEqual(reading.collection?.coolify?.ok, true)
        XCTAssertEqual(reading.collection?.files?.partial, true)
        XCTAssertEqual(reading.collection?.files?.errorCode, "files_truncated")
        XCTAssertEqual(reading.collection?.failedUnits?.ok, false)
        XCTAssertNil(reading.collection?.services)
    }

    func test_the_additions_of_the_wrong_shape_cost_themselves_alone() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-server-status", "generatedAt": "2026-10-28T15:00:00Z", "coolify": [{"uuid": "u", "name": "a", "status": "running"}], "processes": [{"pid": 1}], "files": [{"path": "/srv/a/AGENTS.md", "drift": "unchanged"}], "failedUnits": "none", "removedCheckouts": 3, "applications": {}, "collection": [], "processSampleSeconds": "two", "hostRole": "server"}
        """
        let reading = try readText(json)
        XCTAssertNil(reading.problem)
        XCTAssertNil(reading.failedUnits)
        XCTAssertTrue(reading.removedCheckouts.isEmpty)
        XCTAssertTrue(reading.applications.isEmpty)
        XCTAssertNil(reading.collection)
        XCTAssertNil(reading.processSampleSeconds)
        XCTAssertEqual(reading.state, .passed)
    }

    func test_host_signals_are_read_and_a_missing_one_claims_nothing() throws {
        let metrics = try decode(
            HostMetrics.self,
            #"{"bootedAt": "2026-11-04T19:26:31Z", "rebootRequired": true, "disks": [{"mount": "/", "usedBytes": 1, "freeBytes": 1, "inodesTotal": 100, "inodesFree": 90}, {"mount": "/mnt/x", "usedBytes": 1, "freeBytes": 1}]}"#
        )
        XCTAssertEqual(metrics.bootedAt, "2026-11-04T19:26:31Z")
        XCTAssertEqual(metrics.rebootRequired, true)
        XCTAssertEqual(metrics.disks.first?.inodesTotal, 100)
        XCTAssertEqual(metrics.disks.first?.inodesFree, 90)
        XCTAssertNil(metrics.disks.last?.inodesTotal)
        let silent = try decode(HostMetrics.self, #"{"disks": []}"#)
        XCTAssertNil(silent.bootedAt)
        XCTAssertNil(silent.rebootRequired)
    }

    func test_a_process_says_what_it_belongs_to() throws {
        let process = try decode(ServerProcess.self, #"{"pid": 1, "name": "next-server (v1", "belongsTo": "example-app"}"#)
        XCTAssertEqual(process.belongsTo, "example-app")
        XCTAssertNil(try decode(ServerProcess.self, #"{"pid": 1}"#).belongsTo)
    }

    func test_the_summary_names_the_checkouts_it_cut() throws {
        let summary = try decode(ServerFilesSummary.self, #"{"found": 3, "limit": 2, "truncated": true, "cutCheckouts": ["Busy"]}"#)
        XCTAssertEqual(summary.cutCheckouts, ["Busy"])
        XCTAssertEqual(try decode(ServerFilesSummary.self, #"{"found": 1}"#).cutCheckouts, [])
    }

    // MARK: - The live shape

    /// A real run of the collector on the server,  sanitized by
    /// `scripts/make-server-status-fixture.py`.  Its names are made up.  Its counts,  words and
    /// time spellings are real,  and they are what this reader has to cope with.
    private func readLiveShape() throws -> ServerStatusReading {
        let json = try fixture("server_status_live_shape", "json")
        let report = try decode(ServerStatusReport.self, json)
        let written = try XCTUnwrap(report.generatedAt.flatMap(FleetTime.parse))
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-server-live-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("server-status.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return ServerStatusReportReader.read(at: url, now: written.addingTimeInterval(120))
    }

    func test_the_live_shape_counts_only_what_exists() throws {
        let reading = try readLiveShape()
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.coolify.count, 17)
        XCTAssertTrue(reading.coolify.allSatisfy { $0.resourceStatus == .running })
        XCTAssertEqual(reading.coolifyState, .passed)
        XCTAssertEqual(Set(reading.coolify.map(\.id)).count, 17)
        XCTAssertEqual(reading.files.count, 289)
        XCTAssertTrue(reading.driftedFiles.isEmpty)
        XCTAssertEqual(reading.configState, .passed)
        XCTAssertFalse(reading.filesTruncated)
        XCTAssertEqual(reading.filesSummary?.found, 291)
    }

    func test_the_live_shape_turns_removed_checkouts_into_three_lines() throws {
        let reading = try readLiveShape()
        XCTAssertEqual(reading.removedCheckouts.count, 3)
        XCTAssertEqual(reading.removedCheckouts.compactMap(\.files).reduce(0, +), 34)
        XCTAssertFalse(reading.files.contains { $0.fileDrift == .missing })
    }

    func test_the_live_shape_offers_only_real_redeploys() throws {
        let reading = try readLiveShape()
        let offered = reading.coolify.filter { reading.offersRedeploy($0) }
        XCTAssertEqual(offered.count, 5)
        XCTAssertTrue(offered.allSatisfy { $0.uuidSource == "label" && $0.actions == ["redeploy"] })
        XCTAssertEqual(reading.coolify.filter { $0.redeployBlockedReason == "no_uuid_label" }.count, 1)
        XCTAssertEqual(reading.applications.count, 4)
        XCTAssertEqual(reading.applications.compactMap(\.containers).reduce(0, +), 5)
    }

    func test_the_live_shape_shows_the_failing_job() throws {
        let reading = try readLiveShape()
        XCTAssertEqual(reading.services.count, 8)
        XCTAssertEqual(reading.services.filter(\.watched).count, 6)
        XCTAssertTrue(reading.services.filter(\.watched).allSatisfy { $0.actions == [] && !$0.controllable })
        XCTAssertEqual(reading.services.filter(\.lastRunFailed).count, 1)
        XCTAssertEqual(reading.failedUnits?.count, 2)
        XCTAssertEqual(reading.servicesState, .missing)
        // Two units really had failed on the server when this was captured,  so red is true.
        XCTAssertEqual(reading.state, .missing)
    }

    func test_the_live_shape_carries_the_rest() throws {
        let reading = try readLiveShape()
        XCTAssertEqual(reading.collection?.coolify?.ok, true)
        XCTAssertEqual(reading.collection?.files?.ok, true)
        XCTAssertEqual(reading.processSampleSeconds, 2.0)
        XCTAssertTrue(reading.processes.contains { $0.belongsTo != nil })
        XCTAssertNotNil(reading.hostMetrics?.bootedAt)
        XCTAssertEqual(reading.hostMetrics?.disks.count, 2)
        XCTAssertNotNil(reading.hostMetrics?.disks.first?.inodesTotal)
        XCTAssertTrue(reading.coolify.allSatisfy { row in
            row.updatedAt.map { FleetTime.parse($0) != nil } ?? true
        })
    }
}
