import XCTest

/// The doctor's report,  and what Muster can say about a host.
///
/// The rule under test is the protocol's rule:  a report that cannot be trusted reads
/// as unknown,  never as a pass.  A missing file,  a malformed file,  a version this
/// app does not read and a report past forty-five minutes all read as unknown,  and a
/// host the doctor could not reach reads as unknown rather than as a failure.
final class FleetToolsReportTests: XCTestCase {
    /// A directory the test can write a report into.
    private func makeDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-fleet-tools-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// Write a report and read it back.
    private func read(_ json: String, now: Date = Date()) throws -> FleetToolsReading {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("report.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return FleetToolsReportReader.read(at: url, now: now)
    }

    /// One row of a host's half of the report.
    private func row(
        _ check: String,
        _ status: String = "OK",
        _ state: String = "current",
        _ category: String = "fleet-tool"
    ) -> String {
        """
        {"check": "\(check)", "status": "\(status)", "state": "\(state)", "category": "\(category)", "tool": "\(check)", "detail": "a detail about \(check)"}
        """
    }

    /// One host's half of the report.
    private func host(
        _ name: String,
        reachable: Bool = true,
        checks: [String] = [],
        error: String? = nil,
        generatedAt: Date? = nil
    ) -> String {
        let errorField = error.map { "\"error\": \"\($0)\", " } ?? ""
        let timeField = generatedAt.map {
            "\"generatedAt\": \"\(ISO8601DateFormatter().string(from: $0))\", "
        } ?? ""
        return """
        {"host": "\(name)", "reachable": \(reachable), \(errorField)\(timeField)"schemaVersion": 1, "doctorCommit": "e996b473225767b7bd8320dfbf2f4fe54f55f0d7", "expected": {"repo": "https://github.com/Simple-With-Us/AI-Fleet-Coordinator", "ref": "refs/heads/main", "source": "git ls-remote", "rev": "1a2f97d20e8e0b1ec7fa05e4650e2dc55dc473a0"}, "checks": [\(checks.joined(separator: ","))]}
        """
    }

    /// The report,  written at a time.
    private func report(
        at date: Date,
        schemaVersion: Int = 1,
        tool: String = "fleet-tools-doctor",
        hosts: [String]
    ) -> String {
        let stamp = ISO8601DateFormatter().string(from: date)
        return """
        {"schemaVersion": \(schemaVersion), "tool": "\(tool)", "generatedAt": "\(stamp)", "hosts": [\(hosts.joined(separator: ","))]}
        """
    }

    // MARK: - A report that can be believed

    /// A fresh report with two hosts reads as two passing hosts.
    func test_a_good_report_reads_two_passing_hosts() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-120),
            hosts: [
                host("mac", checks: [row("tool:recall"), row("tool:lane")]),
                host("server", checks: [row("tool:simple-chat"), row("tool:simple-board")]),
            ]
        )
        let reading = try read(json, now: now)
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.hosts.map { $0.host }, ["mac", "server"])
        XCTAssertEqual(reading.hosts.map { $0.state }, [.passed, .passed])
        XCTAssertEqual(reading.hosts[0].current, 2)
        XCTAssertEqual(reading.hosts[0].failing, 0)
        XCTAssertEqual(reading.verdict, .passed)
        XCTAssertEqual(reading.age, "2m")
    }

    /// The panel shows the tip the doctor compared against,  and the commit it ran from.
    func test_a_host_carries_its_tip_and_the_doctors_commit() throws {
        let now = Date()
        let json = report(at: now, hosts: [host("mac", checks: [row("tool:recall")])])
        let reading = try read(json, now: now)
        let mac = reading.hosts.first
        XCTAssertEqual(mac?.tip, "1a2f97d")
        XCTAssertEqual(mac?.tipSource, "git ls-remote")
        XCTAssertEqual(mac?.doctor, "e996b47")
    }

    /// Each host carries its own time,  which the panel shows beside the report's.
    ///
    /// The server's doctor runs on its own timer,  so its half of the report is
    /// routinely older than the Mac's,  and the panel says so instead of hiding it.
    func test_a_host_carries_its_own_age() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-30),
            hosts: [
                host("mac", checks: [row("tool:recall")], generatedAt: now.addingTimeInterval(-90)),
            ]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.age, "30s")
        XCTAssertEqual(reading.hosts.first?.age, "1m")
    }

    // MARK: - The ways a report cannot be believed

    /// A report that is not there is unknown,  never a pass.
    func test_a_missing_report_reads_as_unknown() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let reading = FleetToolsReportReader.read(
            at: directory.appendingPathComponent("report.json")
        )
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.hosts.isEmpty)
        XCTAssertEqual(reading.verdict, .unknown)
        XCTAssertFalse(reading.isCurrent)
    }

    /// A file that is not JSON is unknown,  never a pass.
    func test_a_malformed_report_reads_as_unknown() throws {
        let reading = try read("{ this is not a report")
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.hosts.isEmpty)
        XCTAssertEqual(reading.verdict, .unknown)
    }

    /// A report of a version this app does not read is unknown,  never a pass.
    func test_a_wrong_schema_version_reads_as_unknown() throws {
        let now = Date()
        let json = report(
            at: now,
            schemaVersion: 2,
            hosts: [host("mac", checks: [row("tool:recall")])]
        )
        let reading = try read(json, now: now)
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(
            reading.problem?.contains("2") ?? false,
            "the reason names the version it found: \(reading.problem ?? "")"
        )
        XCTAssertTrue(reading.hosts.isEmpty)
        XCTAssertEqual(reading.verdict, .unknown)
    }

    /// A report from another tool is not this report.
    func test_a_report_from_another_tool_reads_as_unknown() throws {
        let now = Date()
        let json = report(
            at: now,
            tool: "something-else",
            hosts: [host("mac", checks: [row("tool:recall")])]
        )
        let reading = try read(json, now: now)
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.hosts.isEmpty)
        XCTAssertEqual(reading.verdict, .unknown)
    }

    /// A report past the doctor's window is unknown,  never a pass.
    ///
    /// The window is the doctor's:  the timer runs every fifteen minutes,  so three of
    /// its runs are as old as a report may be.
    func test_a_stale_report_reads_as_unknown() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-46 * 60),
            hosts: [host("mac", checks: [row("tool:recall")])]
        )
        let reading = try read(json, now: now)
        XCTAssertNil(reading.problem)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.verdict, .unknown)
        // The host is not proved either:  an old report says nothing about it now.
        XCTAssertEqual(reading.hosts.first?.state, .unproven)
    }

    /// A report inside the window is current.
    func test_a_report_inside_the_window_is_current() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-44 * 60),
            hosts: [host("mac", checks: [row("tool:recall")])]
        )
        let reading = try read(json, now: now)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.verdict, .passed)
        XCTAssertEqual(reading.hosts.first?.state, .passed)
    }

    /// A report with no time on it is not a reading.
    func test_a_report_with_no_time_reads_as_unknown() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-tools-doctor", "hosts": []}
        """
        let reading = try read(json)
        XCTAssertNotNil(reading.problem)
        XCTAssertEqual(reading.verdict, .unknown)
    }

    // MARK: - The hosts

    /// A host the doctor could not reach is unknown,  and not a failure.
    ///
    /// The server's half of the report today is a placeholder for every check the
    /// doctor would have made,  so counting those rows would report observations that
    /// were never made.  The host reads unproven and says why.
    func test_an_unreachable_host_reads_as_unknown_and_not_as_a_failure() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [
                host("mac", checks: [row("tool:recall")]),
                host(
                    "server",
                    reachable: false,
                    checks: [row("tool:simple-chat", "WARN", "unknown"), row("tool:simple-board", "WARN", "unknown")],
                    error: "no read-only route to the server (the ssh alias fleet-tools-ro is not configured)"
                ),
            ]
        )
        let reading = try read(json, now: now)
        let server = reading.hosts.last
        XCTAssertEqual(server?.host, "server")
        XCTAssertFalse(server?.reachable ?? true)
        XCTAssertEqual(server?.state, .unproven)
        XCTAssertEqual(server?.failing, 0)
        XCTAssertEqual(server?.unknown, 0)
        XCTAssertTrue(server?.reason?.contains("no read-only route to the server") ?? false)
        // The report's own verdict is not a pass:  the server is not proved.
        XCTAssertEqual(reading.verdict, .unproven)
    }

    /// A host with failing rows reads as a failure and counts them.
    func test_a_host_with_failing_rows_counts_them() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [
                host(
                    "mac",
                    checks: [
                        row("tool:recall", "FAIL", "stale"),
                        row("tool:gitleaks", "FAIL", "missing"),
                        row("tool:lane", "WARN", "stale"),
                        row("tool:simple-board", "OK", "current"),
                    ]
                ),
            ]
        )
        let reading = try read(json, now: now)
        let mac = reading.hosts.first
        XCTAssertEqual(mac?.failing, 2)
        XCTAssertEqual(mac?.stale, 1)
        XCTAssertEqual(mac?.current, 1)
        XCTAssertEqual(mac?.state, .missing)
        XCTAssertEqual(reading.verdict, .missing)
        // The drill-in lists the failures first.
        XCTAssertEqual(mac?.notableRows.map { $0.check }, ["tool:gitleaks", "tool:recall", "tool:lane"])
    }

    /// A host with rows behind and none failing reads as drift.
    func test_a_host_with_only_stale_rows_reads_as_drift() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [host("mac", checks: [row("tool:recall", "WARN", "stale"), row("tool:lane")])]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.hosts.first?.state, .drift)
        XCTAssertEqual(reading.hosts.first?.failing, 0)
        XCTAssertEqual(reading.verdict, .drift)
    }

    /// A row the doctor could not tell is unknown,  and never a pass.
    func test_an_unknown_row_is_not_a_pass() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [host("mac", checks: [row("tool:recall", "WARN", "unknown"), row("tool:lane")])]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.hosts.first?.unknown, 1)
        XCTAssertEqual(reading.hosts.first?.state, .unproven)
        XCTAssertEqual(reading.verdict, .unproven)
    }

    /// A row whose state this app does not know is unknown,  never a pass.
    ///
    /// The doctor's vocabulary is five words.  A sixth one from a newer doctor is not
    /// read as a healthy row,  and it is not read as a failure either:  it is unknown.
    func test_a_state_the_app_does_not_know_is_unknown() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [host("mac", checks: [row("tool:recall", "OK", "wobble")])]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.hosts.first?.unknown, 1)
        XCTAssertEqual(reading.hosts.first?.current, 0)
        XCTAssertEqual(reading.hosts.first?.state, .unproven)
    }

    /// A status this app does not know is unknown,  never a pass.
    func test_a_status_the_app_does_not_know_is_unknown() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [host("mac", checks: [row("tool:recall", "WOBBLE", "current")])]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.hosts.first?.unknown, 1)
        XCTAssertEqual(reading.hosts.first?.current, 0)
    }

    /// An optional tool that is not installed is information,  and not a failure.
    func test_an_absent_optional_tool_is_not_a_failure() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [host("mac", checks: [row("tool:droid", "OK", "absent", "platform-cli")])]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.hosts.first?.absent, 1)
        XCTAssertEqual(reading.hosts.first?.failing, 0)
        XCTAssertEqual(reading.hosts.first?.state, .passed)
    }

    // MARK: - The categories a reader leaves out

    /// The three categories the protocol leaves out of the verdict stay out of it.
    ///
    /// A failing `human-tree`,  `presence` or `harness` row is not the fleet's tool
    /// health,  so it is not counted and it does not make the host a failure.
    func test_the_three_excluded_categories_are_left_out_of_the_verdict() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [
                host(
                    "mac",
                    checks: [
                        row("checkout:AI-Fleet-Coordinator", "FAIL", "stale", "human-tree"),
                        row("command:simple-chat", "FAIL", "missing", "presence"),
                        row("updater:com.jay.agent-sync-runtime-sync", "FAIL", "missing", "harness"),
                        row("tool:recall"),
                    ]
                ),
            ]
        )
        let reading = try read(json, now: now)
        let mac = reading.hosts.first
        XCTAssertEqual(mac?.failing, 0)
        XCTAssertEqual(mac?.stale, 0)
        XCTAssertEqual(mac?.current, 1)
        XCTAssertEqual(mac?.state, .passed)
        XCTAssertEqual(reading.verdict, .passed)
        XCTAssertTrue(mac?.notableRows.isEmpty ?? false)
    }

    /// A category this app does not know is not one of the three,  so it counts.
    func test_an_unknown_category_counts() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [host("mac", checks: [row("tool:recall", "FAIL", "stale", "something-new")])]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.hosts.first?.failing, 1)
        XCTAssertEqual(reading.hosts.first?.state, .missing)
    }

    /// The three categories are the ones the protocol names.
    func test_the_excluded_categories_are_the_three_the_protocol_names() {
        XCTAssertEqual(
            FleetToolsRow.excludedCategories,
            ["harness", "human-tree", "presence"]
        )
    }

    // MARK: - The contract

    /// The window a report is current in is the doctor's own.
    func test_the_window_is_forty_five_minutes() {
        XCTAssertEqual(FleetToolsContract.maxAgeSeconds, 45 * 60)
        XCTAssertEqual(FleetToolsContract.supportedSchemaVersion, 1)
        XCTAssertEqual(FleetToolsContract.expectedTool, "fleet-tools-doctor")
    }

    /// The report the doctor writes lands where the protocol says it does.
    func test_the_report_is_read_from_the_state_directory() {
        let home = URL(fileURLWithPath: "/Users/somebody")
        let url = FleetToolsReportReader.reportURL(home: home)
        XCTAssertEqual(
            url.path,
            "/Users/somebody/.local/state/fleet-tools/report.json"
        )
    }

    /// Category filters expose coding app CLIs, fleet tools, dev tools, services, and checkouts without changing the host verdict.
    func test_category_filters_expose_all_categories_without_changing_verdict() throws {
        let now = Date()
        let json = report(
            at: now,
            hosts: [
                host(
                    "mac",
                    checks: [
                        row("tool:agy", "OK", "current", "platform-cli"),
                        row("tool:claude", "WARN", "stale", "platform-cli"),
                        row("tool:simple-chat", "OK", "current", "fleet-tool"),
                        row("tool:gh", "OK", "current", "dev-tool"),
                        row("notes:runner", "OK", "current", "service"),
                        row("mcp:codex", "WARN", "unknown", "harness"),
                        row("checkout:AI-Fleet-Coordinator", "OK", "current", "human-tree"),
                        row("command:simple-chat", "OK", "current", "presence"),
                    ]
                ),
            ]
        )
        let reading = try read(json, now: now)
        guard let mac = reading.hosts.first else {
            XCTFail("expected mac host")
            return
        }
        // Only tool:claude is stale among verdict-included rows; mcp:codex (harness) is excluded from verdict.
        XCTAssertEqual(mac.state, .drift)
        XCTAssertEqual(mac.notableRows.map(\.check), ["tool:claude"])
        XCTAssertEqual(mac.allRows.count, 8)
        XCTAssertEqual(mac.rows(matching: .codingApps).map(\.check), ["tool:claude", "tool:agy"])
        XCTAssertEqual(mac.rows(matching: .fleetTools).map(\.check), ["tool:simple-chat"])
        XCTAssertEqual(mac.rows(matching: .devTools).map(\.check), ["tool:gh"])
        XCTAssertEqual(mac.rows(matching: .servicesAndHarnesses).map(\.check), ["mcp:codex", "notes:runner"])
        XCTAssertEqual(
            mac.rows(matching: .checkoutsAndPresence).map(\.check),
            ["checkout:AI-Fleet-Coordinator", "command:simple-chat"]
        )
        XCTAssertEqual(mac.rows(matching: .all, search: "agy").map(\.check), ["tool:agy"])
    }
}
