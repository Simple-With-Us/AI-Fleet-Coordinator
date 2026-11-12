import XCTest

/// The VMs' status reports,  and what Muster can say about them.
///
/// The rule under test is the doctor's rule:  a report that cannot be trusted reads
/// as unknown,  never as a pass.  A report past forty-five minutes,  a malformed file
/// and a version this app does not read all read as unknown,  and a measurement the
/// collector could not take is never shown as a zero.
final class VmStatusReportTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    private func makeDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-vm-status-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// An ISO time `seconds` before the test's now.
    private func time(_ secondsAgo: Int) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: now.addingTimeInterval(TimeInterval(-secondsAgo)))
    }

    private func fullReport(generatedAt: String) -> String {
        """
        {
          "schemaVersion": 1,
          "tool": "fleet-vm-status",
          "generatedAt": "\(generatedAt)",
          "hostLabel": "fixture-vm",
          "seats": ["MC", "CODEX"],
          "uptimeSec": 90061,
          "load1": 1.5, "load5": 1.0, "load15": 0.5,
          "cpuCount": 4,
          "memory": {"totalBytes": 8000, "availableBytes": 2000},
          "swap": {"totalBytes": 2000, "freeBytes": 2000},
          "disks": [{"mount": "/", "totalBytes": 1000, "usedBytes": 400, "freeBytes": 600}],
          "processes": [{"name": "muse", "count": 3, "oldestStartedAt": "\(generatedAt)"}],
          "harnessSessions": {"muse": {"sessionFileCount": 12, "newestMtime": "\(generatedAt)"}},
          "checkouts": [{"name": "repo", "branch": "main", "headShort": "abc1234",
                         "dirtyTracked": 2, "dirtyUntracked": 1, "unpushed": true}]
        }
        """
    }

    private func read(_ json: String, label: String = "fixture-vm") throws -> VmStatusReading {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("\(label).json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return VmStatusReportReader.read(at: url, now: now)
    }

    func testFullReportDecodesEveryHalf() throws {
        let reading = try read(fullReport(generatedAt: time(60)))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.label, "fixture-vm")
        let report = try XCTUnwrap(reading.report)
        XCTAssertEqual(report.hostLabel, "fixture-vm")
        XCTAssertEqual(report.seats, ["MC", "CODEX"])
        XCTAssertEqual(report.loadText, "1.50 1.00 0.50")
        XCTAssertEqual(report.cpuCount, 4)
        XCTAssertEqual(report.memory?.usedFraction, 0.75)
        XCTAssertEqual(report.disks.first?.mount, "/")
        XCTAssertEqual(report.processes.first?.name, "muse")
        XCTAssertEqual(report.processes.first?.count, 3)
        XCTAssertEqual(report.harnessSessions.first?.harness, "muse")
        XCTAssertEqual(report.harnessSessions.first?.sessionFileCount, 12)
        XCTAssertNotNil(report.harnessSessions.first?.newestMtime)
        XCTAssertEqual(report.checkouts.first?.branch, "main")
        XCTAssertEqual(report.checkouts.first?.dirtyTracked, 2)
        XCTAssertEqual(report.checkouts.first?.unpushed, true)
        XCTAssertEqual(reading.state, .passed)
    }

    func testOmittedFieldsStayOmittedNeverZero() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-vm-status", "generatedAt": "\(time(60))"}
        """
        let reading = try read(json)
        XCTAssertNil(reading.problem)
        let report = try XCTUnwrap(reading.report)
        XCTAssertNil(report.hostLabel)
        XCTAssertNil(report.loadText)
        XCTAssertNil(report.loadFraction)
        XCTAssertNil(report.memory)
        XCTAssertNil(report.uptimeText)
        XCTAssertTrue(report.disks.isEmpty)
        XCTAssertTrue(report.processes.isEmpty)
        XCTAssertTrue(report.checkouts.isEmpty)
    }

    func testStaleReportReadsUnknownNeverPass() throws {
        let reading = try read(fullReport(generatedAt: time(45 * 60 + 1)))
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
    }

    func testReportAtTheFortyFiveMinuteMarkIsStillCurrent() throws {
        let reading = try read(fullReport(generatedAt: time(45 * 60)))
        XCTAssertTrue(reading.isCurrent)
    }

    func testWrongToolAndVersionAreRefused() throws {
        let wrongTool = fullReport(generatedAt: time(60))
            .replacingOccurrences(of: "fleet-vm-status", with: "fleet-server-status")
        XCTAssertNotNil(try read(wrongTool).problem)
        XCTAssertNil(try read(wrongTool).report)
        let wrongVersion = fullReport(generatedAt: time(60))
            .replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 2")
        XCTAssertNotNil(try read(wrongVersion).problem)
    }

    func testFutureDatedReportIsRefused() throws {
        let reading = try read(fullReport(generatedAt: time(-600)))
        XCTAssertNotNil(reading.problem)
        XCTAssertFalse(reading.isCurrent)
    }

    func testDirectorySkipsBadFilesAndSortsByLabel() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        try "not json".write(
            to: directory.appendingPathComponent("broken.json"), atomically: true, encoding: .utf8)
        try fullReport(generatedAt: time(60)).write(
            to: directory.appendingPathComponent("zeta.json"), atomically: true, encoding: .utf8)
        try fullReport(generatedAt: time(60)).write(
            to: directory.appendingPathComponent("alpha.json"), atomically: true, encoding: .utf8)
        try "ignored".write(
            to: directory.appendingPathComponent("notes.txt"), atomically: true, encoding: .utf8)
        let fleet = VmStatusReportReader.readDirectory(at: directory, now: now)
        XCTAssertTrue(fleet.directoryExists)
        XCTAssertEqual(fleet.vms.map(\.label), ["alpha", "broken", "zeta"])
        XCTAssertNotNil(fleet.vms[1].problem)
        XCTAssertEqual(fleet.vms[1].state, .unknown)
        XCTAssertNil(fleet.vms[0].problem)
    }

    func testNegativeLoadIsUnmeasuredNeverAPass() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-vm-status", "generatedAt": "\(time(60))",
         "load1": -1.5, "load5": -1.0, "load15": -0.5, "cpuCount": 4}
        """
        let reading = try read(json)
        let report = try XCTUnwrap(reading.report)
        XCTAssertNil(report.loadFraction)
        XCTAssertEqual(reading.state, .unproven)
    }

    func testMemoryAvailableAboveTotalIsUnmeasured() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-vm-status", "generatedAt": "\(time(60))",
         "memory": {"totalBytes": 8000, "availableBytes": 9000}}
        """
        let reading = try read(json)
        let report = try XCTUnwrap(reading.report)
        XCTAssertNil(report.memory?.usedFraction)
        XCTAssertEqual(reading.state, .unproven)
    }

    func testUnlistableDirectoryCarriesAProblemNotAnEmptyFleet() throws {
        let directory = try makeDirectory()
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
            try? FileManager.default.removeItem(at: directory)
        }
        try fullReport(generatedAt: time(60)).write(
            to: directory.appendingPathComponent("fixture-vm.json"), atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: directory.path)
        let fleet = VmStatusReportReader.readDirectory(at: directory, now: now)
        XCTAssertTrue(fleet.directoryExists)
        XCTAssertNotNil(fleet.problem)
        XCTAssertTrue(fleet.vms.isEmpty)
    }

    func testOversizeReportIsRefusedBeforeReading() throws {
        // A valid report padded past the doctor's own cap with one long seat name:
        // over the cap it reads as a problem, never as a decoded report.
        let padding = String(repeating: "x", count: VmStatusContract.maxReportBytes)
        let json = fullReport(generatedAt: time(60))
            .replacingOccurrences(of: "\"MC\", \"CODEX\"", with: "\"\(padding)\"")
        let reading = try read(json)
        XCTAssertNotNil(reading.problem)
        XCTAssertNil(reading.report)
        XCTAssertEqual(reading.state, .unknown)
    }

    func testMissingDirectoryReadsAsNoVms() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let fleet = VmStatusReportReader.readDirectory(
            at: directory.appendingPathComponent("vms", isDirectory: true), now: now)
        XCTAssertFalse(fleet.directoryExists)
        XCTAssertTrue(fleet.vms.isEmpty)
    }
}
