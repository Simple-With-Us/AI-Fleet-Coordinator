import Foundation

// The Mac status report that Hog Hunter writes,  as Muster reads it.
//
// The file is `~/.local/state/fleet-tools/hoghunter-mac.json`:  the running Hog Hunter app
// writes it about once a minute,  and it says how this Mac is doing (load,  CPU,  memory,
// swap,  disks),  which processes are the busiest (names only),  how the last cleanup went,
// whether the housekeeper lock is held,  what the cleaning regimen is doing,  and how old the
// server snapshot Hog Hunter holds is.
//
// This is a read only integration.  Muster reads one local file and nothing else:  it sends
// Hog Hunter no command,  opens no connection to it and holds no credential for it.  The
// file's authentication is the file system:  it is read only when it is a regular file,  owned
// by the current user,  and writable by nobody else.  Any other file reads as unknown.
//
// The doctor's rule applies:  a report that cannot be trusted reads as unknown,  never as a
// pass.  A missing file,  a file that fails the ownership check,  a malformed file,  a version
// or tool this app does not know,  and a report older than five minutes all read as unknown.
// Hog Hunter closed is the usual cause,  and the reading says so.

// MARK: - The contract

enum HogHunterMacContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "hoghunter-mac-status"

    /// The app writes every minute,  so five minutes is four missed writes.
    static let maxAgeSeconds = 5 * 60

    /// The most a report may be,  so a hostile or runaway file cannot fill memory.
    static let maxBytes = 256 * 1024
}

// MARK: - The rows

/// The last cleanup,  as the app summarised it.  Only words and numbers:  the app never sends
/// a step,  a reason or a path.
struct HogHunterMaintain: Decodable, Equatable {
    let health: String?
    let lastRunEndedAt: String?
    let lastRunOutcome: String?
    let lastRunExitCode: Int?
    let bytesFreed: Int?

    enum CodingKeys: String, CodingKey {
        case health, lastRunEndedAt, lastRunOutcome, lastRunExitCode, bytesFreed
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        health = try? container.decodeIfPresent(String.self, forKey: .health)
        lastRunEndedAt = try? container.decodeIfPresent(String.self, forKey: .lastRunEndedAt)
        lastRunOutcome = try? container.decodeIfPresent(String.self, forKey: .lastRunOutcome)
        lastRunExitCode = try? container.decodeIfPresent(Int.self, forKey: .lastRunExitCode)
        bytesFreed = try? container.decodeIfPresent(Int.self, forKey: .bytesFreed)
    }

    /// The state the cleanup reads as.  A word this app does not know is unproven,  not a pass.
    var state: CellState {
        switch health ?? "" {
        case "healthy": return .passed
        case "overdue", "degraded", "unloaded": return .drift
        case "failed", "critical": return .missing
        default: return .unproven
        }
    }

    var healthText: String? {
        switch health ?? "" {
        case "healthy": return "On schedule"
        case "overdue": return "Overdue"
        case "failed": return "Needs attention"
        case "unloaded": return "Not running in the background"
        case "critical": return "Disk critically low"
        case "degraded": return "Disk low"
        default: return nil
        }
    }
}

/// The automatic disk clean's own state.
struct HogHunterRegimen: Decodable, Equatable {
    let enabled: Bool?
    let lastRunAt: String?
    let waiting: Bool?

    enum CodingKeys: String, CodingKey { case enabled, lastRunAt, waiting }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try? container.decodeIfPresent(Bool.self, forKey: .enabled)
        lastRunAt = try? container.decodeIfPresent(String.self, forKey: .lastRunAt)
        waiting = try? container.decodeIfPresent(Bool.self, forKey: .waiting)
    }
}

/// The housekeeper lock.  A word this app does not know is unknown.
enum HogHunterLockState: String {
    case free
    case held
    case stale
    case unknown

    init(reportValue: String?) {
        self = reportValue.flatMap(HogHunterLockState.init(rawValue:)) ?? .unknown
    }
}

// MARK: - The report

struct HogHunterMacReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let host: String?
    let appVersion: String?
    let hostMetrics: HostMetrics?
    /// Nil when the report carries no process list (not reported).
    let processes: [ServerProcess]?
    /// True when the report carries a process list this app cannot read (not proven).
    let processesMalformed: Bool
    let maintain: HogHunterMaintain?
    let housekeeperLock: String?
    let regimen: HogHunterRegimen?
    let serverSnapshotAgeSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, host, appVersion, hostMetrics, processes
        case maintain, housekeeperLock, regimen, serverSnapshotAgeSeconds
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate;  everything else costs only itself.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        host = try? container.decodeIfPresent(String.self, forKey: .host)
        appVersion = try? container.decodeIfPresent(String.self, forKey: .appVersion)
        hostMetrics = try? container.decodeIfPresent(HostMetrics.self, forKey: .hostMetrics)
        // Absent is not reported;  present but unreadable is not proven.  Neither is an empty list.
        if container.contains(.processes), (try? container.decodeNil(forKey: .processes)) != true {
            if let list = try? container.decode([ServerProcess].self, forKey: .processes) {
                processes = list
                processesMalformed = false
            } else {
                processes = nil
                processesMalformed = true
            }
        } else {
            processes = nil
            processesMalformed = false
        }
        maintain = try? container.decodeIfPresent(HogHunterMaintain.self, forKey: .maintain)
        housekeeperLock = try? container.decodeIfPresent(String.self, forKey: .housekeeperLock)
        regimen = try? container.decodeIfPresent(HogHunterRegimen.self, forKey: .regimen)
        serverSnapshotAgeSeconds = try? container.decodeIfPresent(Int.self, forKey: .serverSnapshotAgeSeconds)
    }
}

// MARK: - The reading

/// One line of the panel.
struct HogHunterMacRow: Equatable, Identifiable {
    let label: String
    let value: String
    let state: CellState?

    var id: String { label }
}

struct HogHunterMacReading {
    var path: String = ""
    var generatedAt: Date?
    var age: String?
    var isCurrent: Bool = false
    /// Why there is no reading,  in a sentence.  Set whenever the report is not current.
    var problem: String?
    var host: String = ""
    var appVersion: String?
    var hostMetrics: HostMetrics?
    var processes: [ServerProcess]?
    var processesMalformed = false
    var maintain: HogHunterMaintain?
    var lock: HogHunterLockState = .unknown
    var regimen: HogHunterRegimen?
    var serverSnapshotAgeSeconds: Int?
    var state: CellState = .unknown

    /// The words for Hog Hunter being closed,  or never having reported.
    static let notReportingText =
        "Hog Hunter is not running or has not reported.\(ServerStatusCopy.sentenceGap)Open Hog Hunter on this Mac to see its numbers here."

    /// The lines under the heading,  in the order the panel shows them.
    ///
    /// A value the report did not carry reads "Not reported",  never a zero.
    var rows: [HogHunterMacRow] {
        guard isCurrent else { return [] }
        let notReported = "Not reported"
        var out: [HogHunterMacRow] = []
        let metrics = hostMetrics

        var load = notReported
        if let text = metrics?.loadText {
            load = text
            if let cpus = metrics?.cpuCount { load += " on \(cpus) cores" }
        }
        let loadState: CellState? = metrics?.loadFraction.map { $0 >= 2 ? .missing : ($0 >= 1 ? .drift : .passed) }
        out.append(.init(label: "Load", value: load, state: loadState))

        out.append(.init(label: "CPU", value: metrics?.cpuText ?? notReported, state: nil))

        var memory = notReported
        var memoryState: CellState?
        if let mem = metrics?.memory, let used = mem.usedFraction {
            memory = "\(Self.percent(used)) used,\(ServerStatusCopy.sentenceGap)\(ServerStatusFormat.bytes(mem.availableBytes ?? 0)) of \(ServerStatusFormat.bytes(mem.totalBytes ?? 0)) available"
            memoryState = used >= 0.95 ? .missing : (used >= 0.85 ? .drift : .passed)
        }
        out.append(.init(label: "Memory", value: memory, state: memoryState))

        var swap = notReported
        if let s = metrics?.swap, let used = s.usedFraction {
            swap = "\(Self.percent(used)) used,\(ServerStatusCopy.sentenceGap)\(ServerStatusFormat.bytes(s.freeBytes ?? 0)) free of \(ServerStatusFormat.bytes(s.totalBytes ?? 0))"
        }
        out.append(.init(label: "Swap", value: swap, state: nil))

        out.append(.init(label: "Uptime", value: metrics?.uptimeText ?? notReported, state: nil))

        let disks = metrics?.disks ?? []
        if disks.isEmpty {
            out.append(.init(label: "Disks", value: notReported, state: nil))
        }
        for disk in disks {
            var value = notReported
            if let used = disk.usedFraction {
                value = "\(Self.percent(used)) used"
                if let free = disk.freeBytes { value += ",\(ServerStatusCopy.sentenceGap)\(ServerStatusFormat.bytes(free)) free" }
                if let total = disk.totalBytes { value += " of \(ServerStatusFormat.bytes(total))" }
            }
            out.append(.init(label: "Disk \(disk.mount)", value: value, state: disk.state))
        }

        out.append(.init(label: "Cleanup", value: maintainText ?? notReported, state: maintain?.state))
        out.append(.init(label: "Housekeeper lock", value: lockText, state: lock == .stale ? .drift : nil))
        out.append(.init(label: "Cleaning regimen", value: regimenText ?? notReported, state: nil))
        out.append(.init(
            label: "Server snapshot",
            value: serverSnapshotAgeSeconds.flatMap { $0 >= 0 ? "Fetched \(FleetTime.age($0)) ago" : nil } ?? notReported,
            state: nil))
        out.append(.init(label: "Hog Hunter", value: appVersion.map { "Version \($0)" } ?? notReported, state: nil))
        return out
    }

    /// The busiest processes,  busiest first,  as the report listed them.
    var hogs: [ServerProcess] { isCurrent ? (processes ?? []) : [] }

    var maintainText: String? {
        guard let maintain else { return nil }
        var parts: [String] = []
        if let health = maintain.healthText { parts.append(health) }
        if let outcome = maintain.lastRunOutcome {
            var run = "last run \(outcome)"
            if let ended = maintain.lastRunEndedAt.flatMap(FleetTime.parse) {
                run += ",\(ServerStatusCopy.sentenceGap)\(FleetTime.ageText(since: ended, now: generatedAt ?? Date())) before this report"
            }
            if let freed = maintain.bytesFreed { run += ",\(ServerStatusCopy.sentenceGap)freed \(ServerStatusFormat.bytes(freed))" }
            parts.append(run)
        }
        return parts.isEmpty ? nil : parts.joined(separator: ";\(ServerStatusCopy.sentenceGap)")
    }

    var lockText: String {
        switch lock {
        case .free: return "Free"
        case .held: return "Held by a running cleanup"
        case .stale: return "Stale,\(ServerStatusCopy.sentenceGap)left by a run that did not finish"
        case .unknown: return "Not reported"
        }
    }

    var regimenText: String? {
        guard let regimen, let enabled = regimen.enabled else { return nil }
        guard enabled else { return "Off" }
        var text = "On"
        if let last = regimen.lastRunAt.flatMap(FleetTime.parse) {
            text += ",\(ServerStatusCopy.sentenceGap)last ran \(FleetTime.ageText(since: last, now: generatedAt ?? Date())) before this report"
        }
        if regimen.waiting == true { text += ",\(ServerStatusCopy.sentenceGap)waiting to run" }
        return text
    }

    private static func percent(_ fraction: Double) -> String { "\(Int((fraction * 100).rounded()))%" }
}

// MARK: - The reader

enum HogHunterMacReportReader {
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("hoghunter-mac.json", isDirectory: false)
    }

    /// Why a file may not be believed,  or nil when it passes.
    ///
    /// The file must be a regular file (not a link),  owned by the current user,  and not
    /// writable by group or others.  This is the integration's authentication:  anything else
    /// could have been written by someone other than the app.
    static func trustProblem(_ info: stat, uid: uid_t = getuid()) -> String? {
        if info.st_mode & S_IFMT != S_IFREG { return "is not a regular file" }
        if info.st_uid != uid { return "is not owned by this user" }
        if info.st_mode & 0o022 != 0 { return "is writable by other users" }
        if info.st_size > off_t(HogHunterMacContract.maxBytes) { return "is larger than a report can be" }
        return nil
    }

    /// Read the report at a path and say what it means.
    static func read(at url: URL, now: Date = Date(), uid: uid_t = getuid()) -> HogHunterMacReading {
        var reading = HogHunterMacReading()
        reading.path = url.path
        let gap = ServerStatusCopy.sentenceGap

        // Open without following a link,  then judge the open file,  so the file that was
        // checked is the file that is read.
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else {
            if errno == ENOENT {
                reading.problem = HogHunterMacReading.notReportingText
            } else if errno == ELOOP {
                reading.problem = "The Hog Hunter report is a link,\(gap)so it is not read."
            } else {
                reading.problem = "The Hog Hunter report could not be opened."
            }
            return reading
        }
        defer { close(fd) }

        var info = stat()
        guard fstat(fd, &info) == 0 else {
            reading.problem = "The Hog Hunter report could not be inspected."
            return reading
        }
        if let why = trustProblem(info, uid: uid) {
            reading.problem = "The Hog Hunter report \(why),\(gap)so it is not read."
            return reading
        }
        let data = FileHandle(fileDescriptor: fd, closeOnDealloc: false).readData(ofLength: HogHunterMacContract.maxBytes + 1)
        guard data.count <= HogHunterMacContract.maxBytes else {
            reading.problem = "The Hog Hunter report is larger than a report can be,\(gap)so it is not read."
            return reading
        }
        return parse(data, path: url.path, now: now)
    }

    static func parse(_ data: Data, path: String, now: Date = Date()) -> HogHunterMacReading {
        var reading = HogHunterMacReading()
        reading.path = path
        let gap = ServerStatusCopy.sentenceGap

        let report: HogHunterMacReport
        do {
            report = try JSONDecoder().decode(HogHunterMacReport.self, from: data)
        } catch {
            reading.problem = "The Hog Hunter report could not be read:\(gap)\(error.localizedDescription)"
            return reading
        }
        guard report.schemaVersion == HogHunterMacContract.supportedSchemaVersion else {
            reading.problem =
                "The Hog Hunter report is version \(report.schemaVersion);\(gap)this app reads version \(HogHunterMacContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == HogHunterMacContract.expectedTool else {
            reading.problem =
                "The report came from \"\(report.tool)\";\(gap)this app reads \"\(HogHunterMacContract.expectedTool)\"."
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem =
                "The Hog Hunter report carries no time this app can read,\(gap)so nothing in it is current."
            return reading
        }
        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem = "The Hog Hunter report is dated in the future,\(gap)so nothing in it is current."
            return reading
        }
        guard seconds <= HogHunterMacContract.maxAgeSeconds else {
            // Stale numbers are not shown:  Hog Hunter closed reads as unknown.
            reading.problem = HogHunterMacReading.notReportingText
            return reading
        }

        reading.isCurrent = true
        reading.host = report.host ?? ""
        reading.appVersion = report.appVersion
        reading.hostMetrics = report.hostMetrics
        reading.processes = report.processes
        reading.processesMalformed = report.processesMalformed
        reading.maintain = report.maintain
        reading.lock = HogHunterLockState(reportValue: report.housekeeperLock)
        reading.regimen = report.regimen
        reading.serverSnapshotAgeSeconds = report.serverSnapshotAgeSeconds
        reading.state = overall(of: reading)
        return reading
    }

    static func readDefault(now: Date = Date()) -> HogHunterMacReading {
        read(at: reportURL(), now: now)
    }

    /// The worst of the host's numbers,  the last cleanup,  the process list and the lock.
    ///
    /// A part that is present but unproven stays in the count,  so partial data cannot read as
    /// a pass.  A report with nothing measured reads unknown:  a free or held lock never
    /// proves a pass on its own,  and a stale lock is an observation and does.
    private static func overall(of reading: HogHunterMacReading) -> CellState {
        var states: [CellState] = []
        var proven = false
        if let metrics = reading.hostMetrics {
            states.append(metrics.state)
            if metrics.state != .unproven { proven = true }
        }
        if let maintain = reading.maintain {
            states.append(maintain.state)
            if maintain.state != .unproven { proven = true }
        }
        if reading.processesMalformed { states.append(.unproven) }
        switch reading.lock {
        case .stale: states.append(.drift); proven = true
        case .free, .held: states.append(.passed)
        case .unknown: break
        }
        guard proven else { return .unknown }
        var worst = CellState.passed
        for state in states where GridBuilder.severityIndex(of: state) < GridBuilder.severityIndex(of: worst) {
            worst = state
        }
        return worst
    }
}
