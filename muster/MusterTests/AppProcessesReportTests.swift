import XCTest

/// The fleet apps' processes on the Mac and the server,  and what Muster can say about them.
///
/// The rule under test is the doctor's rule:  a report that cannot be trusted reads as not
/// known,  never as running.  An old report,  a machine the doctor did not reach and a machine
/// read only in part all read as Not Known,  and only a full,  current read may say an app
/// has No Process Seen.
final class AppProcessesReportTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    /// An ISO time `seconds` before the test's now.
    private func time(_ secondsAgo: Int) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: now.addingTimeInterval(TimeInterval(-secondsAgo)))
    }

    private func host(
        _ name: String,
        reachable: Bool = true,
        complete: Bool = true,
        truncated: Bool = false,
        error: String? = nil,
        processes: String = "[]"
    ) -> String {
        """
        {"host": "\(name)", "reachable": \(reachable), "error": \(error.map { "\"\($0)\"" } ?? "null"),
         "generatedAt": "\(time(120))", "complete": \(complete), "truncated": \(truncated),
         "notes": [], "processes": \(processes)}
        """
    }

    private let macRows = """
        [{"app": "BF", "pid": 502, "executable": "node", "state": "sleeping",
          "startedAt": "2027-01-15T06:00:00Z", "matchedBy": "parent process"},
         {"app": "BF", "pid": 501, "executable": "node", "launchdLabel": "app.botfleet.server",
          "state": "running", "startedAt": "2027-01-15T06:00:00Z", "matchedBy": "launchd label"},
         {"app": "C", "pid": 505, "executable": "node", "pm2Name": "clutch-web", "state": "zombie"}]
        """

    private let serverRows = """
        [{"app": "AFC", "pid": 801, "executable": "/usr/bin/python3.12",
          "systemdUnit": "fleet-coding-memory.service", "state": "sleeping", "matchedBy": "systemd unit"}]
        """

    private func report(
        generatedAt: String? = nil,
        schemaVersion: Int = 1,
        tool: String = "fleet-app-processes",
        hosts: [String]? = nil
    ) -> String {
        let hostList = hosts ?? [host("mac", processes: macRows), host("server", processes: serverRows)]
        return """
            {"schemaVersion": \(schemaVersion), "tool": "\(tool)", "generatedAt": "\(generatedAt ?? time(60))",
             "apps": [{"acronym": "BF", "displayName": "BotFleet.app"},
                      {"acronym": "AFC", "displayName": "AI Fleet Coordinator"},
                      {"acronym": "C", "displayName": "Clutch"},
                      {"acronym": "HH", "displayName": "Hog Hunter"}],
             "hosts": [\(hostList.joined(separator: ","))]}
            """
    }

    private func parse(_ json: String) -> AppProcessesReading {
        AppProcessesReportReader.parse(Data(json.utf8), path: "/fixture/app-processes.json", now: now)
    }

    private func app(_ reading: AppProcessesReading, host: Int, _ acronym: String) -> AppProcessesAppReading? {
        reading.hosts[host].apps.first { $0.acronym == acronym }
    }

    // MARK: A full, current read

    func testACurrentReportListsEachAppOnEachHost() throws {
        let reading = parse(report())
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.hosts.map(\.hostWord), ["mac", "server"])
        XCTAssertEqual(reading.hosts.map(\.heading), ["The Mac", "The Server"])
        XCTAssertEqual(reading.hosts.map(\.state), [.read, .read])
        XCTAssertEqual(reading.hosts[0].apps.map(\.acronym), ["BF", "AFC", "C", "HH"])

        let botfleet = try XCTUnwrap(app(reading, host: 0, "BF"))
        XCTAssertEqual(botfleet.state, .seen(2))
        XCTAssertEqual(botfleet.state.words, "2 Processes Seen")
        XCTAssertEqual(botfleet.processes.map(\.pid), [501, 502], "rows read in pid order")
        XCTAssertEqual(botfleet.title, "BotFleet.app (BF)")
        XCTAssertEqual(reading.hosts[0].appsSeen, 2)

        // An app the full read found nothing for is information,  not a fault.
        let afcOnMac = try XCTUnwrap(app(reading, host: 0, "AFC"))
        XCTAssertEqual(afcOnMac.state, .noProcessSeen)
        XCTAssertEqual(afcOnMac.state.words, "No Process Seen")
        XCTAssertEqual(app(reading, host: 1, "AFC")?.state, .seen(1))
        XCTAssertEqual(app(reading, host: 1, "BF")?.state, .noProcessSeen)
    }

    func testARowsNameStateAndUptime() throws {
        let reading = parse(report())
        let botfleet = try XCTUnwrap(app(reading, host: 0, "BF"))
        let server = botfleet.processes[0]
        XCTAssertEqual(server.name, "app.botfleet.server")
        XCTAssertEqual(server.stateWords, "Running")
        XCTAssertEqual(server.uptimeText(now: now), "2h")
        XCTAssertEqual(server.matchedBy, "launchd label")
        XCTAssertEqual(botfleet.processes[1].name, "node")
        let clutch = try XCTUnwrap(app(reading, host: 0, "C")?.processes.first)
        XCTAssertEqual(clutch.name, "clutch-web")
        XCTAssertEqual(clutch.stateWords, "Exited, Not Reaped")
        XCTAssertTrue(clutch.isStoppedOrGone)
        XCTAssertNil(clutch.uptimeText(now: now), "no start time is never a zero")
        let unit = try XCTUnwrap(app(reading, host: 1, "AFC")?.processes.first)
        XCTAssertEqual(unit.name, "fleet-coding-memory.service")
        XCTAssertEqual(unit.executable, "/usr/bin/python3.12")
    }

    func testAStateThisAppDoesNotKnowIsNotKnown() throws {
        let rows = #"[{"app": "BF", "pid": 1, "executable": "/opt/botfleet/bin/botfleet-worker", "state": "dancing"}]"#
        let reading = parse(report(hosts: [host("mac", processes: rows)]))
        let row = try XCTUnwrap(app(reading, host: 0, "BF")?.processes.first)
        XCTAssertEqual(row.stateWords, "State Not Known")
        XCTAssertEqual(row.name, "botfleet-worker")
    }

    func testAnAppTheListDoesNotNameIsStillShown() throws {
        let rows = #"[{"app": "ZZ", "pid": 7, "executable": "zz", "state": "sleeping"}]"#
        let reading = parse(report(hosts: [host("mac", processes: rows)]))
        let unknownApp = try XCTUnwrap(app(reading, host: 0, "ZZ"))
        XCTAssertEqual(unknownApp.state, .seen(1))
        XCTAssertEqual(unknownApp.title, "ZZ")
    }

    func testAnExtraKeyInARowIsNeverKept() throws {
        // The doctor never writes an argv;  if one ever arrived,  the row has nowhere to keep it.
        let rows = #"[{"app": "BF", "pid": 9, "executable": "node", "argv": ["node", "--token=ghp_x"], "state": "sleeping"}]"#
        let reading = parse(report(hosts: [host("mac", processes: rows)]))
        let row = try XCTUnwrap(app(reading, host: 0, "BF")?.processes.first)
        let labels = Mirror(reflecting: row).children.compactMap(\.label)
        XCTAssertFalse(labels.contains("argv"))
        XCTAssertFalse(String(describing: row).contains("ghp_x"))
    }

    // MARK: Every way of not knowing

    func testAnOldReportReadsNotKnownEverywhere() {
        let reading = parse(report(generatedAt: time(AppProcessesContract.maxAgeSeconds + 60)))
        XCTAssertNil(reading.problem)
        XCTAssertFalse(reading.isCurrent)
        for host in reading.hosts {
            XCTAssertEqual(host.state, .reportOld)
            XCTAssertEqual(host.state.words, "Not Known:  Report Is Old")
            for app in host.apps {
                XCTAssertEqual(app.state, .notKnown, "\(host.hostWord) \(app.acronym)")
                XCTAssertTrue(app.processes.isEmpty, "an old report shows no row as running")
            }
        }
    }

    func testAHostNotReachedReadsNotKnownWithTheDoctorsReason() {
        let reading = parse(report(hosts: [
            host("mac", processes: macRows),
            host("server", reachable: false, error: "ssh fleet-tools-ro exited 255", processes: serverRows),
        ]))
        let server = reading.hosts[1]
        XCTAssertEqual(server.state, .notReached("ssh fleet-tools-ro exited 255"))
        XCTAssertFalse(server.state.showsRows)
        XCTAssertTrue(server.apps.allSatisfy { $0.state == .notKnown && $0.processes.isEmpty })
        XCTAssertEqual(server.appsSeen, 0)
    }

    func testAPartReadSaysWhatItSawAndNothingAboutTheRest() {
        for document in [host("mac", complete: false, processes: macRows),
                         host("mac", truncated: true, processes: macRows)] {
            let reading = parse(report(hosts: [document]))
            let mac = reading.hosts[0]
            XCTAssertEqual(mac.state, .partlyRead)
            XCTAssertEqual(app(reading, host: 0, "BF")?.state, .seen(2))
            // Not "No Process Seen":  the part that was not read could have held them.
            XCTAssertEqual(app(reading, host: 0, "HH")?.state, .notKnown)
        }
    }

    func testAHostWithMissingFieldsIsNeitherReachedNorComplete() {
        let reading = parse(report(hosts: [#"{"host": "mac", "processes": []}"#]))
        XCTAssertEqual(reading.hosts[0].state, .notReached("the doctor could not read this host"))
    }

    func testAReportThatCannotBeTrustedHasAProblemAndNoHosts() {
        let cases: [(String, String)] = [
            ("not json", "could not be read"),
            (report(schemaVersion: 2), "version 2"),
            (report(tool: "fleet-tools-doctor"), "this app reads \"fleet-app-processes\""),
            (report(generatedAt: "yesterday"), "no time"),
            (report(generatedAt: time(-3600)), "in the future"),
            (#"{"tool": "fleet-app-processes"}"#, "could not be read"),
        ]
        for (json, why) in cases {
            let reading = parse(json)
            XCTAssertTrue(reading.problem?.contains(why) ?? false, "\(why): \(reading.problem ?? "no problem")")
            XCTAssertTrue(reading.hosts.isEmpty, why)
            XCTAssertFalse(reading.isCurrent, why)
        }
    }

    // MARK: The file

    func testAMissingFileSaysSo() {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-app-processes-\(UUID().uuidString)")
            .appendingPathComponent("app-processes.json")
        let reading = AppProcessesReportReader.read(at: url, now: now)
        XCTAssertTrue(reading.problem?.contains("not on the Mac yet") ?? false)
        XCTAssertTrue(reading.hosts.isEmpty)
    }

    func testAFileReadsAndAnOversizedOneIsRefused() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-app-processes-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent(AppProcessesContract.fileName)
        try report().write(to: url, atomically: true, encoding: .utf8)
        let reading = AppProcessesReportReader.read(at: url, now: now)
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.path, url.path)
        XCTAssertEqual(reading.hosts.count, 2)

        try Data(count: AppProcessesContract.maxReportBytes + 1).write(to: url)
        let big = AppProcessesReportReader.read(at: url, now: now)
        XCTAssertTrue(big.problem?.contains("bytes") ?? false)
        XCTAssertTrue(big.hosts.isEmpty)
    }

    func testTheReportSitsBesideTheDoctorsOwn() {
        let home = URL(fileURLWithPath: "/Users/example", isDirectory: true)
        XCTAssertEqual(AppProcessesReportReader.reportURL(home: home).path,
                       "/Users/example/.local/state/fleet-tools/app-processes.json")
        XCTAssertEqual(AppProcessesReportReader.reportURL(home: home).deletingLastPathComponent(),
                       FleetToolsReportReader.reportURL(home: home).deletingLastPathComponent())
    }
}
