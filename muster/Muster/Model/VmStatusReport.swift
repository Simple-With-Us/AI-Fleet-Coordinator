import Foundation

// The fleet VMs' status reports,  as Muster reads them.
//
// Each report is one file under `~/.local/state/fleet-tools/vms/<label>.json`:  the
// VM's own collector (`fleet-vm-status.py`) made it,  and the doctor's `--vms` pull
// carried it here over a read-only forced-command key.  It says how the VM itself is
// (load,  memory,  disks),  which harness processes and sessions live on it,  and what
// shape its checkouts are in.
//
// The rule this file follows is the doctor's rule:  a report that cannot be trusted
// reads as unknown,  never as a pass.  A malformed file,  a version this app does not
// read and a report older than forty-five minutes all read as unknown,  and a
// measurement the collector could not take is omitted rather than shown as a zero.
//
// Nothing here reaches the network and nothing here runs anything:  the reports are
// local files the doctor already wrote.  Telemetry is read-only by the owner's call,
// so there is no control verb anywhere in this file.

// MARK: - The contract

/// The pinned contract for a VM's report.
enum VmStatusContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-vm-status"

    /// The window a report is current in,  the same window the server's report has.
    static let maxAgeSeconds = 45 * 60

    /// The config the owner writes so the doctor pulls the VMs at all.
    static let configPathText = "~/.config/fleet-tools/vm-hosts.json"
}

// MARK: - The rows

/// One group of like-named harness processes,  as the collector counted them.
struct VmProcess: Decodable, Identifiable, Equatable {
    let name: String
    let count: Int?
    let oldestStartedAt: String?

    enum CodingKeys: String, CodingKey {
        case name, count, oldestStartedAt
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = (try? container.decodeIfPresent(String.self, forKey: .name)) ?? ""
        count = try? container.decodeIfPresent(Int.self, forKey: .count)
        oldestStartedAt = try? container.decodeIfPresent(String.self, forKey: .oldestStartedAt)
    }

    var id: String { name }

    /// How long the oldest of them has been running,  when the collector could tell.
    var oldestText: String? {
        guard let oldestStartedAt, let date = FleetTime.parse(oldestStartedAt) else { return nil }
        return FleetTime.ageText(since: date)
    }
}

/// One harness's session files on the VM:  how many,  and the newest one's mtime.
struct VmHarnessSessions: Decodable, Identifiable, Equatable {
    let harness: String
    let sessionFileCount: Int?
    let newestMtime: String?

    var id: String { harness }

    /// The newest session's age,  when the collector could tell.
    var newestText: String? {
        guard let newestMtime, let date = FleetTime.parse(newestMtime) else { return nil }
        return FleetTime.ageText(since: date)
    }
}

/// One checkout on the VM,  as local git plumbing described it.
struct VmCheckout: Decodable, Identifiable, Equatable {
    let name: String
    let branch: String?
    let headShort: String?
    let dirtyTracked: Int?
    let dirtyUntracked: Int?
    let unpushed: Bool?

    enum CodingKeys: String, CodingKey {
        case name, branch, headShort, dirtyTracked, dirtyUntracked, unpushed
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = (try? container.decodeIfPresent(String.self, forKey: .name)) ?? ""
        branch = try? container.decodeIfPresent(String.self, forKey: .branch)
        headShort = try? container.decodeIfPresent(String.self, forKey: .headShort)
        dirtyTracked = try? container.decodeIfPresent(Int.self, forKey: .dirtyTracked)
        dirtyUntracked = try? container.decodeIfPresent(Int.self, forKey: .dirtyUntracked)
        unpushed = try? container.decodeIfPresent(Bool.self, forKey: .unpushed)
    }

    var id: String { name }

    /// True only when the report says in as many words that HEAD is unpushed.
    var showsUnpushed: Bool { unpushed == true }

    /// The dirty counts,  when `git status` could be read.
    var dirtyText: String? {
        guard let dirtyTracked, let dirtyUntracked else { return nil }
        if dirtyTracked == 0 && dirtyUntracked == 0 { return "clean" }
        return "\(dirtyTracked) tracked, \(dirtyUntracked) untracked dirty"
    }
}

// MARK: - The report

/// One VM's report,  as the collector writes it.
///
/// Every measurement is optional,  because a number the collector could not read is
/// left out of the report rather than written as a zero.  Host halves reuse the server
/// report's rows (`HostMemory`, `HostSwap`, `HostDisk`),  which carry the same fields.
struct VmStatusReport: Decodable, Equatable {
    let hostLabel: String?
    let seats: [String]
    let uptimeSec: Int?
    let load1: Double?
    let load5: Double?
    let load15: Double?
    let cpuCount: Int?
    let memory: HostMemory?
    let swap: HostSwap?
    let disks: [HostDisk]
    let processes: [VmProcess]
    let harnessSessions: [VmHarnessSessions]
    let checkouts: [VmCheckout]

    /// One harness's row,  as written inside the report's harness-keyed object.
    private struct SessionsRow: Decodable {
        let sessionFileCount: Int?
        let newestMtime: String?

        enum CodingKeys: String, CodingKey {
            case sessionFileCount, newestMtime
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            sessionFileCount = try? container.decodeIfPresent(Int.self, forKey: .sessionFileCount)
            newestMtime = try? container.decodeIfPresent(String.self, forKey: .newestMtime)
        }
    }

    enum CodingKeys: String, CodingKey {
        case hostLabel, seats, uptimeSec, load1, load5, load15, cpuCount
        case memory, swap, disks, processes, harnessSessions, checkouts
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        hostLabel = try? container.decodeIfPresent(String.self, forKey: .hostLabel)
        seats = (try? container.decodeIfPresent([String].self, forKey: .seats)) ?? []
        uptimeSec = try? container.decodeIfPresent(Int.self, forKey: .uptimeSec)
        load1 = try? container.decodeIfPresent(Double.self, forKey: .load1)
        load5 = try? container.decodeIfPresent(Double.self, forKey: .load5)
        load15 = try? container.decodeIfPresent(Double.self, forKey: .load15)
        cpuCount = try? container.decodeIfPresent(Int.self, forKey: .cpuCount)
        memory = try? container.decodeIfPresent(HostMemory.self, forKey: .memory)
        swap = try? container.decodeIfPresent(HostSwap.self, forKey: .swap)
        disks = (try? container.decodeIfPresent([HostDisk].self, forKey: .disks)) ?? []
        processes = (try? container.decodeIfPresent([VmProcess].self, forKey: .processes)) ?? []
        let sessions =
            (try? container.decodeIfPresent([String: SessionsRow].self, forKey: .harnessSessions)) ?? [:]
        harnessSessions = sessions.keys.sorted().map { harness in
            VmHarnessSessions(
                harness: harness,
                sessionFileCount: sessions[harness]?.sessionFileCount,
                newestMtime: sessions[harness]?.newestMtime)
        }
        checkouts = (try? container.decodeIfPresent([VmCheckout].self, forKey: .checkouts)) ?? []
    }

    /// The three load numbers,  as `uptime` prints them,  when all three were read.
    var loadText: String? {
        guard let load1, let load5, let load15 else { return nil }
        return [load1, load5, load15].map { String(format: "%.2f", $0) }.joined(separator: " ")
    }

    /// The one-minute load over the cpu count,  when both were read.
    ///
    /// A negative load is not a measurement (load averages cannot be negative),  so
    /// it reads as unmeasured rather than clamping to a zero that passes.
    var loadFraction: Double? {
        guard let load1, load1 >= 0, let cpuCount, cpuCount > 0 else { return nil }
        return load1 / Double(cpuCount)
    }

    /// How long the VM has been up,  in the app's own words.
    var uptimeText: String? {
        guard let uptimeSec, uptimeSec >= 0 else { return nil }
        return FleetTime.age(uptimeSec)
    }

    /// The state the VM reads as:  the worst of its load,  memory and disks.
    ///
    /// A report that measured none of them proves nothing,  so it reads unproven.
    var state: CellState {
        var states: [CellState] = disks.map(\.state)
        if let loadFraction {
            states.append(loadFraction >= 2 ? .missing : (loadFraction >= 1 ? .drift : .passed))
        }
        if let used = memory?.usedFraction {
            states.append(used >= 0.95 ? .missing : (used >= 0.85 ? .drift : .passed))
        }
        guard !states.isEmpty else { return .unproven }
        var worst = CellState.passed
        for state in states where GridBuilder.severityIndex(of: state) < GridBuilder.severityIndex(of: worst) {
            worst = state
        }
        return worst
    }
}

/// The report's gate fields,  read before the report itself is trusted.
private struct VmReportGate: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
}

// MARK: - The reading

/// Everything the panel shows for one VM,  from one read of its report.
struct VmStatusReading: Identifiable {
    /// The label the report was filed under (the file's own stem).
    var label: String = ""
    /// Where the report is.
    var path: String = ""
    /// The report,  when it could be read and trusted enough to decode.
    var report: VmStatusReport?
    /// When the report says it was written.
    var generatedAt: Date?
    /// How old the report is,  in the app's own words.
    var age: String?
    /// Whether the report is inside the forty-five minute window.
    var isCurrent: Bool = false
    /// Why the report could not be read at all,  when it could not be.
    var problem: String?
    /// The verdict for the whole report.
    var state: CellState = .unknown

    var id: String { label }

    /// The name the card shows:  the report's own host label,  else its file label.
    var displayName: String {
        if let hostLabel = report?.hostLabel, !hostLabel.isEmpty { return hostLabel }
        return label
    }
}

/// Everything the panel shows,  from one read of the reports' directory.
struct VmFleetReading {
    /// Where the reports live.
    var directoryPath: String = ""
    /// Whether the directory is there at all.
    var directoryExists: Bool = false
    /// One reading per report file,  in label order.  A file that cannot be read is a
    /// reading with a problem,  never a skipped VM.
    var vms: [VmStatusReading] = []
    /// Why the directory itself could not be listed,  when it exists but its
    /// contents could not be read.  Distinct from an empty directory:  reports may
    /// well be there,  Muster just cannot see them.
    var problem: String?
}

// MARK: - The reader

/// Reads the VMs' reports and says what they mean.
enum VmStatusReportReader {
    /// Where the doctor's `--vms` pull writes the reports.
    static func directoryURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("vms", isDirectory: true)
    }

    /// Read every report in a directory and say what they mean.
    static func readDirectory(at url: URL, now: Date = Date()) -> VmFleetReading {
        var fleet = VmFleetReading()
        fleet.directoryPath = url.path
        var isDirectory: ObjCBool = false
        fleet.directoryExists =
            FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue
        guard fleet.directoryExists else { return fleet }
        let names: [String]
        do {
            names =
                try FileManager.default.contentsOfDirectory(atPath: url.path)
                .filter { $0.hasSuffix(".json") }
                .sorted()
        } catch {
            fleet.problem =
                "The reports directory is there but could not be listed:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
            return fleet
        }
        fleet.vms = names.map { read(at: url.appendingPathComponent($0), now: now) }
        return fleet
    }

    /// Read the reports the doctor writes for this user.
    static func readDefault(now: Date = Date()) -> VmFleetReading {
        readDirectory(at: directoryURL(), now: now)
    }

    /// Read one report at a path and say what it means.
    ///
    /// Every way of not knowing ends the same way:  the reading carries a problem and
    /// no report,  so nothing on screen can be read as a pass.
    static func read(at url: URL, now: Date = Date()) -> VmStatusReading {
        var reading = VmStatusReading()
        reading.label = url.deletingPathExtension().lastPathComponent
        reading.path = url.path

        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            reading.problem =
                "The report could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
            return reading
        }

        let gate: VmReportGate
        let report: VmStatusReport
        do {
            gate = try JSONDecoder().decode(VmReportGate.self, from: data)
            report = try JSONDecoder().decode(VmStatusReport.self, from: data)
        } catch {
            reading.problem =
                "The report could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
            return reading
        }

        guard gate.schemaVersion == VmStatusContract.supportedSchemaVersion else {
            reading.problem =
                "The report is version \(gate.schemaVersion);\(ServerStatusCopy.sentenceGap)this app reads version \(VmStatusContract.supportedSchemaVersion)."
            return reading
        }
        guard gate.tool == VmStatusContract.expectedTool else {
            reading.problem =
                "The report came from \"\(gate.tool)\";\(ServerStatusCopy.sentenceGap)this app reads \"\(VmStatusContract.expectedTool)\"."
            return reading
        }
        guard let generated = gate.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem =
                "The report carries no time this app can read,\(ServerStatusCopy.sentenceGap)so nothing in it is current."
            return reading
        }

        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem =
                "The report is dated in the future,\(ServerStatusCopy.sentenceGap)so nothing in it is current."
            return reading
        }
        reading.isCurrent = seconds <= VmStatusContract.maxAgeSeconds
        reading.report = report
        // A report out of its window is not a reading:  the whole report reads unknown,
        // even where every number in it looks fine.
        reading.state = reading.isCurrent ? report.state : .unknown
        return reading
    }
}
