import Foundation

// The server status report,  as Muster reads it.
//
// The file is `~/.local/state/fleet-tools/server-status.json`:  the server's own
// collector writes it,  and it says three things:  how Coolify is working,  what the
// busiest processes on the host are,  and which of the fleet's config files have
// drifted since the last report.
//
// The rule this file follows is the doctor's rule:  a report that cannot be trusted
// reads as unknown,  never as a pass.  A missing file,  a malformed file,  a version
// this app does not read and a report older than forty-five minutes all read as
// unknown.
//
// Nothing here reaches the network and nothing here reads a credential:  the report
// is one local file the collector already wrote.

// MARK: - The copy

enum ServerStatusCopy {
    /// A real U+00A0 followed by a space:  the sentence gap for rendered product copy.
    static let sentenceGap = "\u{00A0} "
}

// MARK: - The contract

/// The pinned contract for the collector's report.
///
/// The version is an exact integer,  never a range:  a change to the report's shape
/// raises it deliberately,  and this app refuses a report it does not know instead of
/// reading a part of it and calling the rest a pass.
enum ServerStatusContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-server-status"

    /// The window a report is current in,  the same window the doctor's report has.
    static let maxAgeSeconds = 45 * 60
}

/// Why a report that is there was not read,  so a surface can say what to do about it.
enum ReportRefusal: Equatable {
    /// The report is newer than this app reads,  or from a tool it does not know.
    case appOutOfDate
    /// This app is newer than the report.
    case reporterOutOfDate
    /// The report says it was not written on the server.
    case notFromServer
    /// The file is there and could not be understood.
    case unreadable

    /// The refusal for a report's version.
    init(reportVersion: Int, supported: Int) {
        self = reportVersion > supported ? .appOutOfDate : .reporterOutOfDate
    }
}

// MARK: - The vocabularies

/// The kind of a Coolify resource.
///
/// A kind this app does not know is `unknown`,  never a healthy one:  an unreadable
/// kind is not an application.
enum CoolifyResourceType: String {
    case application
    case service
    case database
    case standalone
    case unknown

    init(reportValue: String?) {
        guard
            let value = reportValue,
            let type = CoolifyResourceType(rawValue: value)
        else {
            self = .unknown
            return
        }
        self = type
    }
}

/// The state of a Coolify resource.
///
/// A state this app does not know is `unknown`,  never `running`:  an unreadable
/// state is not a running one.
enum CoolifyResourceStatus: String {
    case running
    case exited
    case unhealthy
    /// Docker keeps restarting it:  a crash loop.
    case restarting
    /// It exited cleanly and nothing would restart it:  a one-shot job that did its work.
    case finished
    case absent
    case unknown

    init(reportValue: String?) {
        guard
            let value = reportValue,
            let status = CoolifyResourceStatus(rawValue: value)
        else {
            self = .unknown
            return
        }
        self = status
    }
}

/// How a watched file has drifted since the previous report.
///
/// A drift this app does not know is `unknown`,  never `unchanged`:  an unreadable
/// drift is not an unchanged file.
enum ServerStatusDrift: String {
    case unchanged
    case changed
    /// A change the collector reports for information only, such as a README or a
    /// package manifest: it is shown, and it is not drift.
    case info
    case new
    case missing
    case unknown

    init(reportValue: String?) {
        guard
            let value = reportValue,
            let drift = ServerStatusDrift(rawValue: value)
        else {
            self = .unknown
            return
        }
        self = drift
    }
}

// MARK: - The rows

/// One Coolify resource,  as the collector saw it.
struct CoolifyResource: Decodable, Identifiable, Equatable {
    let uuid: String
    let name: String
    let type: String?
    let status: String?
    let cpuPercent: Double?
    let memoryBytes: Int?
    let image: String?
    let updatedAt: String?
    /// Where the collector says `uuid` came from (`label` for a Coolify uuid label),
    /// when it says and says it as text.
    let uuidSource: String?
    /// True when the row carries a `uuidSource` that is not null,  readable as text or not.
    ///
    /// The server believes a row that says where its uuid came from only when it says
    /// `label`,  so a value of the wrong type must still count as saying something.
    let declaresUUIDSource: Bool
    /// The container's own name,  which can hold a per-deploy stamp,  so it is not in `name`.
    let container: String?
    /// The compose service,  when this is one part of an application.
    let service: String?
    /// When the collector first saw this row gone.  It drops the row after seven days.
    let absentSince: String?
    /// `healthy`,  `unhealthy`,  `starting`,  or `none` when the container has no health check.
    let health: String?
    let restartCount: Int?
    let exitCode: Int?
    let oomKilled: Bool?
    /// What the helper would really do for this row,  when the collector asked it.
    let actions: [String]?
    /// Why an application row cannot be redeployed,  as a fixed code.
    let redeployBlockedReason: String?

    enum CodingKeys: String, CodingKey {
        case uuid, name, type, status, cpuPercent, memoryBytes, image, updatedAt, uuidSource
        case container, service, absentSince, health, restartCount, exitCode, oomKilled
        case actions, redeployBlockedReason
    }

    /// A resource decodes field by field,  so one field of a type this app did not
    /// expect costs that field and not the whole report.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        uuid = (try? container.decodeIfPresent(String.self, forKey: .uuid)) ?? ""
        name = (try? container.decodeIfPresent(String.self, forKey: .name)) ?? ""
        type = try? container.decodeIfPresent(String.self, forKey: .type)
        status = try? container.decodeIfPresent(String.self, forKey: .status)
        cpuPercent = try? container.decodeIfPresent(Double.self, forKey: .cpuPercent)
        memoryBytes = try? container.decodeIfPresent(Int.self, forKey: .memoryBytes)
        image = try? container.decodeIfPresent(String.self, forKey: .image)
        updatedAt = try? container.decodeIfPresent(String.self, forKey: .updatedAt)
        uuidSource = try? container.decodeIfPresent(String.self, forKey: .uuidSource)
        declaresUUIDSource = container.contains(.uuidSource)
            && (try? container.decodeNil(forKey: .uuidSource)) == false
        self.container = try? container.decodeIfPresent(String.self, forKey: .container)
        service = try? container.decodeIfPresent(String.self, forKey: .service)
        absentSince = try? container.decodeIfPresent(String.self, forKey: .absentSince)
        health = try? container.decodeIfPresent(String.self, forKey: .health)
        restartCount = try? container.decodeIfPresent(Int.self, forKey: .restartCount)
        exitCode = try? container.decodeIfPresent(Int.self, forKey: .exitCode)
        oomKilled = try? container.decodeIfPresent(Bool.self, forKey: .oomKilled)
        actions = try? container.decodeIfPresent([String].self, forKey: .actions)
        redeployBlockedReason = try? container.decodeIfPresent(String.self, forKey: .redeployBlockedReason)
    }

    /// The row's identity.
    ///
    /// The image is in it because two services of one Coolify application share the
    /// application's uuid and the resource name,  and a row must still be one row.  The
    /// container is in it because two deploys of one application can overlap for a moment.
    var id: String { "\(uuid)/\(name)/\(image ?? "")/\(container ?? "")" }

    /// The kind of resource,  in this app's vocabulary.
    var resourceType: CoolifyResourceType { CoolifyResourceType(reportValue: type) }

    /// The state of the resource,  in this app's vocabulary.
    var resourceStatus: CoolifyResourceStatus { CoolifyResourceStatus(reportValue: status) }

    /// The state the resource reads as,  in the grid's own vocabulary.
    ///
    /// A resource that is running or that finished its work reads as a pass,  one that exited,
    /// is unhealthy or is in a crash loop reads as a failure,  one that is gone reads as drift,
    /// and one nobody could tell reads as unproven rather than as a pass.
    var state: CellState {
        switch resourceStatus {
        case .running, .finished: return .passed
        case .exited, .unhealthy, .restarting: return .missing
        case .absent: return .drift
        case .unknown: return .unproven
        }
    }

    /// False for a row the server refuses to redeploy because of its state:  one that is gone,
    /// or one whose state nobody could read.
    var isOnHost: Bool { resourceStatus != .absent && resourceStatus != .unknown }

    /// True when the server would agree to redeploy this row.
    ///
    /// This mirrors `coolify_resource` and `uuid_from_label` in
    /// `scripts/fleet-ops-control.py`:  only a Coolify application can be redeployed,  and
    /// only when its `uuid` came from a Coolify uuid label.  A row that says where its uuid
    /// came from is believed only when it says `label`;  a row that does not say is taken
    /// to carry a name when its uuid is its own name,  or the start of its name followed by
    /// `" ("`.  A row with no name is not offered,  which is the cautious side of the rule.
    var isRedeployable: Bool {
        guard type == "application", !uuid.isEmpty else { return false }
        if declaresUUIDSource { return uuidSource == "label" }
        guard !name.isEmpty else { return false }
        return name != uuid && !name.hasPrefix(uuid + " (")
    }

    /// What a badge alone cannot say,  in words:  the state when it is not plain running,  a
    /// crash loop's restarts,  a missing health check and how it exited.  Nil adds nothing.
    func signalText(now: Date = Date()) -> String? {
        var parts: [String] = []
        if resourceStatus == .absent, let since = absentSince.flatMap(FleetTime.parse) {
            parts.append("gone for \(FleetTime.ageText(since: since, now: now))")
        } else if resourceStatus != .running {
            parts.append(resourceStatus.rawValue)
        }
        if let restartCount, restartCount > 0 {
            parts.append(restartCount == 1 ? "1 restart" : "\(restartCount) restarts")
        }
        if resourceStatus == .running, health == "none" { parts.append("no health check") }
        if resourceStatus == .running, health == "starting" { parts.append("health check starting") }
        if let exitCode, resourceStatus != .running { parts.append("exit \(exitCode)") }
        if oomKilled == true { parts.append("out of memory") }
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }

    /// Why this row offers no Redeploy,  in words,  when the collector asked the helper.
    var redeployBlockedText: String? {
        guard let reason = redeployBlockedReason else { return nil }
        switch reason {
        case "no_uuid_label": return "No Redeploy: the container does not say which application it is."
        case "not_seen": return "No Redeploy: it is not on the server now."
        case "state_unknown": return "No Redeploy: the collector could not read its state."
        case "not_allowed": return "No Redeploy: the server's allowlist does not allow it."
        case "first_row_refused": return "No Redeploy: the server acts on the first row with this id, and that row cannot be redeployed."
        default: return "No Redeploy."
        }
    }

    /// The cpu,  when the collector could measure it.
    ///
    /// A measurement that was not taken is not shown as a zero.
    var cpuText: String? {
        guard let cpuPercent = cpuPercent else { return nil }
        return String(format: "%.1f%%", cpuPercent)
    }

    /// The memory,  when the collector could measure it.
    var memoryText: String? { memoryBytes.map { ServerStatusFormat.bytes($0) } }

    /// The time the container last changed state,  as the collector wrote it.
    var updatedText: String? {
        guard let updatedAt = updatedAt, let date = FleetTime.parse(updatedAt) else { return nil }
        return FleetTime.ageText(since: date)
    }

    /// The same time,  saying what happened then.
    var updatedLabel: String? {
        updatedText.map { age in
            // The collector writes the finish time only for a container that has stopped,
            // and the start time for every other state,  a crash loop and a pause included.
            let isDown = resourceStatus == .exited || resourceStatus == .finished
            return "\(isDown ? "stopped" : "started") \(age) ago"
        }
    }
}

/// One process on the server,  as the collector saw it.
struct ServerProcess: Decodable, Identifiable, Equatable {
    let pid: Int
    let owner: String?
    let name: String?
    let cpuPercent: Double?
    let memoryBytes: Int?
    /// The container or the systemd unit it runs in,  when its cgroup names one.
    let belongsTo: String?

    enum CodingKeys: String, CodingKey {
        case pid, owner, name, cpuPercent, memoryBytes, belongsTo
    }

    /// A process decodes field by field,  for the same reason a resource does.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pid = (try? container.decodeIfPresent(Int.self, forKey: .pid)) ?? 0
        owner = try? container.decodeIfPresent(String.self, forKey: .owner)
        name = try? container.decodeIfPresent(String.self, forKey: .name)
        cpuPercent = try? container.decodeIfPresent(Double.self, forKey: .cpuPercent)
        memoryBytes = try? container.decodeIfPresent(Int.self, forKey: .memoryBytes)
        belongsTo = try? container.decodeIfPresent(String.self, forKey: .belongsTo)
    }

    var id: Int { pid }

    /// The cpu as a count of cores,  because a share of one core reads past 100 percent.
    var coresText: String? {
        guard let cpuPercent = cpuPercent else { return nil }
        return String(format: "%.1f cores", cpuPercent / 100)
    }

    /// The name of the executable,  or the pid when the collector could not read it.
    var displayName: String {
        if let name = name, !name.isEmpty { return name }
        return "pid \(pid)"
    }

    /// The cpu,  when the collector could measure it.
    var cpuText: String? {
        guard let cpuPercent = cpuPercent else { return nil }
        return String(format: "%.1f%%", cpuPercent)
    }

    /// The memory,  when the collector could measure it.
    var memoryText: String? { memoryBytes.map { ServerStatusFormat.bytes($0) } }
}

/// One watched config file,  as the collector saw it.
struct WatchedFile: Decodable, Identifiable, Equatable {
    let path: String
    let bytes: Int?
    let modifiedAt: String?
    let sha256: String?
    let drift: String?
    /// When the collector first saw this file gone.  It drops the row after seven days.
    let missingSince: String?

    enum CodingKeys: String, CodingKey {
        case path, bytes, modifiedAt, sha256, drift, missingSince
    }

    /// A file decodes field by field,  for the same reason a resource does.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        path = (try? container.decodeIfPresent(String.self, forKey: .path)) ?? ""
        bytes = try? container.decodeIfPresent(Int.self, forKey: .bytes)
        modifiedAt = try? container.decodeIfPresent(String.self, forKey: .modifiedAt)
        sha256 = try? container.decodeIfPresent(String.self, forKey: .sha256)
        drift = try? container.decodeIfPresent(String.self, forKey: .drift)
        missingSince = try? container.decodeIfPresent(String.self, forKey: .missingSince)
    }

    var id: String { path }

    /// How long ago this file went or last changed,  when the report says.
    func whenText(now: Date = Date()) -> String? {
        if let since = missingSince.flatMap(FleetTime.parse) {
            return "gone for \(FleetTime.ageText(since: since, now: now))"
        }
        return modifiedAt.flatMap(FleetTime.parse).map { "modified \(FleetTime.ageText(since: $0, now: now)) ago" }
    }

    /// The drift of the file,  in this app's vocabulary.
    var fileDrift: ServerStatusDrift { ServerStatusDrift(reportValue: drift) }

    /// The state the file reads as,  in the grid's own vocabulary.
    ///
    /// A file that is gone reads as a failure,  a file that changed or is new reads
    /// as drift,  and a drift this app cannot read reads as unproven rather than as
    /// an unchanged file.
    var state: CellState {
        switch fileDrift {
        case .unchanged, .info: return .passed
        case .changed, .new: return .drift
        case .missing: return .missing
        case .unknown: return .unproven
        }
    }

    /// The size of the file,  when the collector could read it.
    var sizeText: String? { bytes.map { ServerStatusFormat.bytes($0) } }

    /// The file's name,  for a row that shows a path.
    var fileName: String {
        let parts = path.split(separator: "/")
        if let last = parts.last { return String(last) }
        return path
    }
}

// MARK: - The host

/// The host's memory,  as the collector read it from `/proc/meminfo`.
struct HostMemory: Decodable, Equatable {
    let totalBytes: Int?
    let availableBytes: Int?

    enum CodingKeys: String, CodingKey {
        case totalBytes, availableBytes
    }

    init(totalBytes: Int?, availableBytes: Int?) {
        self.totalBytes = totalBytes
        self.availableBytes = availableBytes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        totalBytes = try? container.decodeIfPresent(Int.self, forKey: .totalBytes)
        availableBytes = try? container.decodeIfPresent(Int.self, forKey: .availableBytes)
    }

    /// The share of memory in use, from 0 to 1, when both numbers were read.
    ///
    /// Impossible numbers (negative,  or more available than there is) are not a
    /// measurement,  so they read as unmeasured rather than clamping to a pass.
    var usedFraction: Double? {
        guard let total = totalBytes, let available = availableBytes,
            total > 0, available >= 0, available <= total
        else { return nil }
        return Double(total - available) / Double(total)
    }
}

/// The host's swap,  as the collector read it from `/proc/meminfo`.
struct HostSwap: Decodable, Equatable {
    let totalBytes: Int?
    let freeBytes: Int?

    enum CodingKeys: String, CodingKey {
        case totalBytes, freeBytes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        totalBytes = try? container.decodeIfPresent(Int.self, forKey: .totalBytes)
        freeBytes = try? container.decodeIfPresent(Int.self, forKey: .freeBytes)
    }

    /// The share of swap in use, from 0 to 1.
    ///
    /// A host with no swap has no share to show,  so it is nil rather than a zero.
    var usedFraction: Double? {
        guard let total = totalBytes, let free = freeBytes,
            total > 0, free >= 0, free <= total
        else { return nil }
        return Double(total - free) / Double(total)
    }
}

/// One mounted file system,  as the collector read it with `statvfs`.
struct HostDisk: Decodable, Identifiable, Equatable {
    let mount: String
    let totalBytes: Int?
    let usedBytes: Int?
    let freeBytes: Int?
    let inodesTotal: Int?
    let inodesFree: Int?

    enum CodingKeys: String, CodingKey {
        case mount, totalBytes, usedBytes, freeBytes, inodesTotal, inodesFree
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        mount = (try? container.decodeIfPresent(String.self, forKey: .mount)) ?? ""
        totalBytes = try? container.decodeIfPresent(Int.self, forKey: .totalBytes)
        usedBytes = try? container.decodeIfPresent(Int.self, forKey: .usedBytes)
        freeBytes = try? container.decodeIfPresent(Int.self, forKey: .freeBytes)
        inodesTotal = try? container.decodeIfPresent(Int.self, forKey: .inodesTotal)
        inodesFree = try? container.decodeIfPresent(Int.self, forKey: .inodesFree)
    }

    var id: String { mount }

    /// The share of the disk in use, from 0 to 1.
    ///
    /// Used over used plus free,  the way `df` counts it,  so the space kept back for
    /// root does not read as free space the fleet can use.
    var usedFraction: Double? {
        if let used = usedBytes, let free = freeBytes, used >= 0, free >= 0, used + free > 0 {
            return Double(used) / Double(used + free)
        }
        if let used = usedBytes, let total = totalBytes, used >= 0, total > 0, used <= total {
            return Double(used) / Double(total)
        }
        return nil
    }

    /// The state the disk reads as:  amber from 85 percent full,  red from 95.
    var state: CellState {
        guard let fraction = usedFraction else { return .unproven }
        if fraction >= 0.95 { return .missing }
        if fraction >= 0.85 { return .drift }
        return .passed
    }
}

/// The host's own numbers:  load,  memory,  swap,  uptime and disks.
///
/// Every field is optional,  because a number the collector could not read is left out
/// of the report rather than written as a zero.
struct HostMetrics: Decodable, Equatable {
    let loadAverage: [Double]
    let cpuCount: Int?
    let memory: HostMemory?
    let swap: HostSwap?
    let uptimeSeconds: Int?
    let disks: [HostDisk]
    /// When the host booted,  from the kernel.
    let bootedAt: String?
    /// True only when the host's flag file says a reboot is needed.  Nil claims nothing.
    let rebootRequired: Bool?
    /// Whole-machine CPU use,  0 to 100.  Only a collector with a tick baseline writes it
    /// (Hog Hunter's Mac report does;  the server's does not).
    let cpuPercent: Double?

    enum CodingKeys: String, CodingKey {
        case loadAverage, cpuCount, memory, swap, uptimeSeconds, disks, bootedAt, rebootRequired, cpuPercent
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        loadAverage = (try? container.decodeIfPresent([Double].self, forKey: .loadAverage)) ?? []
        cpuCount = try? container.decodeIfPresent(Int.self, forKey: .cpuCount)
        memory = try? container.decodeIfPresent(HostMemory.self, forKey: .memory)
        swap = try? container.decodeIfPresent(HostSwap.self, forKey: .swap)
        uptimeSeconds = try? container.decodeIfPresent(Int.self, forKey: .uptimeSeconds)
        disks = (try? container.decodeIfPresent([HostDisk].self, forKey: .disks)) ?? []
        bootedAt = try? container.decodeIfPresent(String.self, forKey: .bootedAt)
        rebootRequired = try? container.decodeIfPresent(Bool.self, forKey: .rebootRequired)
        cpuPercent = try? container.decodeIfPresent(Double.self, forKey: .cpuPercent)
    }

    /// The machine's CPU use,  when the collector measured it.
    var cpuText: String? {
        guard let cpuPercent, cpuPercent >= 0, cpuPercent.isFinite else { return nil }
        return String(format: "%.0f%%", cpuPercent)
    }

    /// A restart in the last hour explains a lot,  so it is said in words.
    var restartNote: String? {
        guard let uptimeSeconds, uptimeSeconds < 3600,
              let booted = bootedAt.flatMap(FleetTime.parse) else { return nil }
        return "The server restarted at \(FleetTime.clock(booted))."
    }

    /// The host's own flag,  in words.  Nil claims nothing either way.
    var rebootNote: String? {
        rebootRequired == true ? "The server says it needs a reboot." : nil
    }

    /// The one-minute load over the cpu count, from 0 up.
    ///
    /// A load of one per cpu is a full machine,  so the bar is drawn against that and
    /// can run past it.
    var loadFraction: Double? {
        guard let load = loadAverage.first, load >= 0, let cpus = cpuCount, cpus > 0 else { return nil }
        return load / Double(cpus)
    }

    /// The three load numbers,  as `uptime` prints them.
    var loadText: String? {
        guard !loadAverage.isEmpty else { return nil }
        return loadAverage.prefix(3).map { String(format: "%.2f", $0) }.joined(separator: " ")
    }

    /// How long the host has been up,  in the app's own words.
    var uptimeText: String? {
        guard let seconds = uptimeSeconds, seconds >= 0 else { return nil }
        return FleetTime.age(seconds)
    }

    /// The state the host reads as:  the worst of its load,  memory and disks.
    var state: CellState {
        var states: [CellState] = disks.map(\.state)
        if let load = loadFraction {
            states.append(load >= 2 ? .missing : (load >= 1 ? .drift : .passed))
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

// MARK: - The services

/// One systemd unit the fleet runs on the server,  as `systemctl show` described it.
///
/// The collector reads the units the allowlist names (`server.units` in
/// `scripts/fleet-ops-control.json`),  and `controllable` says whether the allowlist lets
/// Muster start,  stop or restart it.  It also reads the units `server.watch` names,  and
/// those are `watched` and have no actions.  A timer says when its job last ran,  how that
/// ended and when it runs next.
struct ServerService: Decodable, Identifiable, Equatable {
    let unit: String
    let scope: String?
    let description: String?
    let activeState: String?
    let subState: String?
    let since: String?
    /// The unit file state (`enabled`,  `disabled`,  `static` and so on).
    let enabled: String?
    let controllable: Bool
    /// What the helper would really do for this unit,  when the collector asked it.
    let actions: [String]?
    /// True for a unit that is read and granted nothing.
    let watched: Bool
    /// True on the collector's own timer while the collector was running.
    let selfObserved: Bool
    /// The service this timer starts.
    let job: String?
    /// True when the timer's job is running at the moment the report was written.
    let jobRunning: Bool
    let lastRunAt: String?
    /// How the job last ended,  in systemd's word.  Nil says the result is not known,  because
    /// it has not run since boot or is running now.
    let lastResult: String?
    let lastExitCode: Int?
    let lastFinishedAt: String?
    let nextRunAt: String?
    /// `reporting` or `updater`,  when the allowlist says what depends on this unit.
    let role: String?
    /// What stopping this unit costs,  in owner copy.
    let stopConsequence: String?

    enum CodingKeys: String, CodingKey {
        case unit, scope, description, activeState, subState, since, enabled, controllable
        case actions, watched, selfObserved, job, jobRunning
        case lastRunAt, lastResult, lastExitCode, lastFinishedAt, nextRunAt
        case role, stopConsequence
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        unit = (try? container.decodeIfPresent(String.self, forKey: .unit)) ?? ""
        scope = try? container.decodeIfPresent(String.self, forKey: .scope)
        description = try? container.decodeIfPresent(String.self, forKey: .description)
        activeState = try? container.decodeIfPresent(String.self, forKey: .activeState)
        subState = try? container.decodeIfPresent(String.self, forKey: .subState)
        since = try? container.decodeIfPresent(String.self, forKey: .since)
        // The unit file state is a word in systemd's vocabulary,  and a collector that
        // writes it as a yes or a no is read the same way.
        if let flag = try? container.decodeIfPresent(Bool.self, forKey: .enabled) {
            enabled = flag ? "enabled" : "disabled"
        } else {
            enabled = try? container.decodeIfPresent(String.self, forKey: .enabled)
        }
        // A unit is controllable only when the report says so in as many words:  a field
        // that is missing or unreadable is not a permission.
        controllable = (try? container.decodeIfPresent(Bool.self, forKey: .controllable)) == true
        actions = try? container.decodeIfPresent([String].self, forKey: .actions)
        watched = (try? container.decodeIfPresent(Bool.self, forKey: .watched)) == true
        selfObserved = (try? container.decodeIfPresent(Bool.self, forKey: .selfObserved)) == true
        job = try? container.decodeIfPresent(String.self, forKey: .job)
        jobRunning = (try? container.decodeIfPresent(Bool.self, forKey: .jobRunning)) == true
        lastRunAt = try? container.decodeIfPresent(String.self, forKey: .lastRunAt)
        lastResult = try? container.decodeIfPresent(String.self, forKey: .lastResult)
        lastExitCode = try? container.decodeIfPresent(Int.self, forKey: .lastExitCode)
        lastFinishedAt = try? container.decodeIfPresent(String.self, forKey: .lastFinishedAt)
        nextRunAt = try? container.decodeIfPresent(String.self, forKey: .nextRunAt)
        role = try? container.decodeIfPresent(String.self, forKey: .role)
        stopConsequence = try? container.decodeIfPresent(String.self, forKey: .stopConsequence)
    }

    /// Two scopes can hold units of one name,  so the scope is part of the identity.
    var id: String { "\(scope ?? "system")/\(unit)" }

    /// True when the timer's job last ended badly.
    ///
    /// A job its own condition skipped did not fail,  and a result nobody could read is not
    /// a failure either.
    var lastRunFailed: Bool {
        guard let result = lastResult else { return false }
        return !ServerService.resultsThatAreNotFailures.contains(result)
    }

    /// systemd's words for a job that worked,  or that a condition skipped and so never ran.
    private static let resultsThatAreNotFailures: Set<String> = [
        "success", "exec-condition", "skipped", "condition",
        "skip-pre-start", "skip-pre-stop", "skip-post-stop",
    ]

    /// The verbs the collector says the helper would run,  or nil when the report does not say.
    var listedVerbs: [OpsVerb]? {
        actions.map { listed in OpsVerb.allCases.filter { listed.contains($0.rawValue) } }
    }

    /// True unless the report lists this unit's actions and Logs is not among them.
    var offersLogs: Bool { actions?.contains("logs") ?? true }

    /// A timer's job,  in words:  how it last went and when it runs next.  Nil for a unit
    /// that says neither.
    func runText(now: Date = Date()) -> String? {
        var sentences: [String] = []
        let finished = (lastFinishedAt ?? lastRunAt).flatMap(FleetTime.parse)
        let when: String = finished.map { " " + ServerStatusFormat.moment($0, now: now) } ?? ""
        if jobRunning {
            sentences.append("Its job is running now.")
        } else if lastRunFailed {
            let code: String = lastExitCode.map { " with exit \($0)" } ?? ""
            sentences.append("Last run failed\(when)\(code).")
        } else if lastResult == "success" {
            sentences.append("Last run succeeded\(when).")
        } else if lastResult != nil {
            sentences.append("Last run was skipped\(when).")
        } else if let started = lastRunAt.flatMap(FleetTime.parse) {
            sentences.append("Last started \(ServerStatusFormat.moment(started, now: now)).")
        }
        if let next = nextRunAt.flatMap(FleetTime.parse) {
            sentences.append("Next run \(ServerStatusFormat.moment(next, now: now)).")
        }
        return sentences.isEmpty ? nil : sentences.joined(separator: ServerStatusCopy.sentenceGap)
    }

    /// The state the unit reads as.
    ///
    /// An active unit is a pass unless the job it starts last failed,  because a timer stays
    /// active while its job fails.  A failed one is a failure,  one that is stopped is
    /// drift,  and one that is between states or that nobody could read is unproven.
    var state: CellState {
        switch activeState ?? "" {
        case "active": return lastRunFailed ? .missing : .passed
        case "failed": return .missing
        case "inactive": return .drift
        default: return .unproven
        }
    }

    /// The state,  as systemd words it.
    var stateText: String {
        let active = activeState ?? "unknown"
        guard let sub = subState, !sub.isEmpty else { return active }
        return "\(active) (\(sub))"
    }

    /// How long the unit has been in its state,  when the collector wrote a time this
    /// app can read.
    var sinceText: String? {
        guard let since = since, let date = FleetTime.parse(since) else { return nil }
        return FleetTime.ageText(since: date)
    }
}

/// One unit `systemctl --failed` named,  whether or not anything else names it.
struct ServerFailedUnit: Decodable, Identifiable, Equatable {
    let unit: String
    let scope: String?
    let description: String?
    let since: String?
    let result: String?
    let exitCode: Int?

    enum CodingKeys: String, CodingKey {
        case unit, scope, description, since, result, exitCode
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        unit = (try? container.decodeIfPresent(String.self, forKey: .unit)) ?? ""
        scope = try? container.decodeIfPresent(String.self, forKey: .scope)
        description = try? container.decodeIfPresent(String.self, forKey: .description)
        since = try? container.decodeIfPresent(String.self, forKey: .since)
        result = try? container.decodeIfPresent(String.self, forKey: .result)
        exitCode = try? container.decodeIfPresent(Int.self, forKey: .exitCode)
    }

    var id: String { "\(scope ?? "system")/\(unit)" }

    /// The whole name and what the unit is,  for a row that clips the name.
    var helpText: String {
        guard let description, !description.isEmpty else { return unit }
        return "\(unit): \(description)"
    }

    /// How it failed,  in words.
    func detailText(now: Date = Date()) -> String {
        var parts = ["failed"]
        if let date = since.flatMap(FleetTime.parse) {
            parts[0] = "failed for \(FleetTime.ageText(since: date, now: now))"
        }
        if let result, !result.isEmpty { parts.append(result) }
        if let exitCode { parts.append("exit \(exitCode)") }
        return parts.joined(separator: ", ")
    }
}

/// One checkout removed from the server in the last seven days.  This is information:  it
/// is not a failure of the files it took with it,  and it does not count in any state.
struct ServerRemovedCheckout: Decodable, Identifiable, Equatable {
    let repo: String
    let files: Int?
    let noticedAt: String?

    enum CodingKeys: String, CodingKey {
        case repo, files, noticedAt
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        repo = (try? container.decodeIfPresent(String.self, forKey: .repo)) ?? ""
        files = try? container.decodeIfPresent(Int.self, forKey: .files)
        noticedAt = try? container.decodeIfPresent(String.self, forKey: .noticedAt)
    }

    var id: String { repo }

    /// The line the panel shows.
    var text: String {
        guard let files else { return "\(repo) was removed from the server." }
        return "\(repo) was removed from the server, with \(files) watched \(files == 1 ? "file" : "files")."
    }
}

/// One live Coolify application and what a redeploy of it restarts.
struct ServerApplication: Decodable, Identifiable, Equatable {
    let uuid: String
    let name: String
    let containers: Int?
    let services: [String]

    enum CodingKeys: String, CodingKey {
        case uuid, name, containers, services
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        uuid = (try? container.decodeIfPresent(String.self, forKey: .uuid)) ?? ""
        name = (try? container.decodeIfPresent(String.self, forKey: .name)) ?? ""
        containers = try? container.decodeIfPresent(Int.self, forKey: .containers)
        services = (try? container.decodeIfPresent([String].self, forKey: .services)) ?? []
    }

    var id: String { uuid }
}

/// How one half of the report went,  in the collector's fixed codes.
struct ServerCollectionEntry: Decodable, Equatable {
    /// False when the half could not be read at all.  Nil when the collector did not say.
    let ok: Bool?
    /// True when the half was read and something in it is missing.
    let partial: Bool
    let errorCode: String?

    enum CodingKeys: String, CodingKey {
        case ok, partial, errorCode
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = try? container.decodeIfPresent(Bool.self, forKey: .ok)
        partial = (try? container.decodeIfPresent(Bool.self, forKey: .partial)) == true
        errorCode = try? container.decodeIfPresent(String.self, forKey: .errorCode)
    }

    /// True when the collector says this half was not read whole,  so it may not read as a pass.
    var isIncomplete: Bool { ok == false || partial || errorCode != nil }

    /// Why,  in owner copy.  The collector writes fixed codes,  and this is where they get words.
    var problemText: String? {
        guard isIncomplete else { return nil }
        switch errorCode ?? "" {
        case "docker_unreadable": return "The collector could not read Docker, so the containers are not known."
        case "docker_inspect_failed": return "Docker listed containers but would not describe them, so they are not known."
        case "docker_inspect_partial": return "Docker would not describe every container, so this list may be short."
        case "docker_stats_unreadable": return "Docker did not give cpu and memory, so those are not shown."
        case "allowlist_unreadable": return "The collector could not read the allowlist, so these units are not known."
        case "allowlist_refused": return "The control helper refused the allowlist, so no unit can be controlled."
        case "systemctl_unreadable": return "The collector could not ask systemd what has failed, so that is not known."
        case "systemctl_scope_unreadable": return "One part of systemd did not answer, so this list may be short."
        case "proc_unreadable": return "The collector could not read the process list."
        case "code_root_unreadable": return "The collector could not read the code folder, so the files are not known."
        case "files_truncated": return "The collector stopped at its file limit, so some files were not checked."
        default: return "The collector could not read all of this."
        }
    }
}

/// What the collector could and could not read this run.
struct ServerCollection: Decodable, Equatable {
    let coolify: ServerCollectionEntry?
    let services: ServerCollectionEntry?
    let failedUnits: ServerCollectionEntry?
    let processes: ServerCollectionEntry?
    let files: ServerCollectionEntry?

    enum CodingKeys: String, CodingKey {
        case coolify, services, failedUnits, processes, files
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        coolify = try? container.decodeIfPresent(ServerCollectionEntry.self, forKey: .coolify)
        services = try? container.decodeIfPresent(ServerCollectionEntry.self, forKey: .services)
        failedUnits = try? container.decodeIfPresent(ServerCollectionEntry.self, forKey: .failedUnits)
        processes = try? container.decodeIfPresent(ServerCollectionEntry.self, forKey: .processes)
        files = try? container.decodeIfPresent(ServerCollectionEntry.self, forKey: .files)
    }
}

/// How many watched files the collector found,  and whether it stopped at its limit.
struct ServerFilesSummary: Decodable, Equatable {
    let found: Int?
    let limit: Int?
    let truncated: Bool
    /// The checkouts the limit cut short.
    let cutCheckouts: [String]

    enum CodingKeys: String, CodingKey {
        case found, limit, truncated, cutCheckouts
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        found = try? container.decodeIfPresent(Int.self, forKey: .found)
        limit = try? container.decodeIfPresent(Int.self, forKey: .limit)
        truncated = (try? container.decodeIfPresent(Bool.self, forKey: .truncated)) == true
        cutCheckouts = (try? container.decodeIfPresent([String].self, forKey: .cutCheckouts)) ?? []
    }
}

// MARK: - The report

/// The report,  as the collector writes it.
struct ServerStatusReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let host: String?
    /// The role the collector says it ran in (`mac` or `server`),  when it says.  A
    /// collector that predates the field leaves it out,  which is not a refusal.
    let hostRole: String?
    let coolify: [CoolifyResource]
    let processes: [ServerProcess]
    let files: [WatchedFile]
    /// The host's own numbers,  when the collector wrote them.
    let hostMetrics: HostMetrics?
    /// The fleet's systemd units,  when the collector wrote them.
    let services: [ServerService]
    /// How many files the collector found,  when it said.
    let filesSummary: ServerFilesSummary?
    /// The redacted resource receipt is optional; its absence never proves limits applied.
    let resourcePolicy: ResourcePolicySnapshot?
    /// What `systemctl --failed` said.  Nil says no scope answered,  which is not the same as
    /// nothing failed.
    let failedUnits: [ServerFailedUnit]?
    /// The checkouts removed in the last seven days.
    let removedCheckouts: [ServerRemovedCheckout]
    /// The live applications and what a redeploy of each restarts.
    let applications: [ServerApplication]
    /// What the collector could and could not read,  when it said.
    let collection: ServerCollection?
    /// How long the cpu of the processes was sampled over,  in seconds.
    let processSampleSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, host, hostRole, coolify, processes, files
        case hostMetrics, services, filesSummary, resourcePolicy
        case failedUnits, removedCheckouts, applications, collection, processSampleSeconds
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate:  a report that does not carry them is
        // not a report this app can judge.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        host = try? container.decodeIfPresent(String.self, forKey: .host)
        hostRole = try? container.decodeIfPresent(String.self, forKey: .hostRole)
        coolify = (try? container.decodeIfPresent([CoolifyResource].self, forKey: .coolify)) ?? []
        processes = (try? container.decodeIfPresent([ServerProcess].self, forKey: .processes)) ?? []
        files = (try? container.decodeIfPresent([WatchedFile].self, forKey: .files)) ?? []
        // The three newer halves are optional:  a report from a collector that predates
        // them still decodes,  and a half that is malformed costs that half alone.
        hostMetrics = try? container.decodeIfPresent(HostMetrics.self, forKey: .hostMetrics)
        services = (try? container.decodeIfPresent([ServerService].self, forKey: .services)) ?? []
        filesSummary = try? container.decodeIfPresent(ServerFilesSummary.self, forKey: .filesSummary)
        resourcePolicy = try? container.decodeIfPresent(ResourcePolicySnapshot.self, forKey: .resourcePolicy)
        failedUnits = try? container.decodeIfPresent([ServerFailedUnit].self, forKey: .failedUnits)
        removedCheckouts = (try? container.decodeIfPresent([ServerRemovedCheckout].self, forKey: .removedCheckouts)) ?? []
        applications = (try? container.decodeIfPresent([ServerApplication].self, forKey: .applications)) ?? []
        collection = try? container.decodeIfPresent(ServerCollection.self, forKey: .collection)
        processSampleSeconds = try? container.decodeIfPresent(Double.self, forKey: .processSampleSeconds)
    }
}

// MARK: - The reading

/// Everything the panel shows,  from one read of the report.
struct ServerStatusReading {
    /// Where the report is.
    var path: String = ""
    /// When the report says it was written.
    var generatedAt: Date?
    /// How old the report is,  in the app's own words.
    var age: String?
    /// Whether the report is inside the window the collector runs in.
    var isCurrent: Bool = false
    /// Why the report could not be read at all,  when it could not be.
    var problem: String?
    /// When the Mac copied the report,  from the file's own modification date.
    var copiedAt: Date?
    /// True when the file is not there at all.
    var fileMissing: Bool = false
    /// True when the report is dated ahead of the Mac's clock.
    var futureDated: Bool = false
    /// What kind of refusal `problem` is,  when the file was there and was refused.
    var refusal: ReportRefusal?
    /// The host the collector ran on.
    var host: String = ""
    /// The Coolify resources,  in the report's own order.
    var coolify: [CoolifyResource] = []
    /// The busiest processes,  in the report's own order.
    var processes: [ServerProcess] = []
    /// The watched files,  in the report's own order.
    var files: [WatchedFile] = []
    /// The host's own numbers,  when the report carries them.
    var hostMetrics: HostMetrics?
    /// The fleet's systemd units,  in the report's own order.
    var services: [ServerService] = []
    /// How many files the collector found,  when the report says.
    var filesSummary: ServerFilesSummary?
    var resourcePolicy: ResourcePolicySnapshot?
    /// What `systemctl --failed` said.  Nil says no scope answered.
    var failedUnits: [ServerFailedUnit]?
    /// The checkouts removed in the last seven days,  which count in no state.
    var removedCheckouts: [ServerRemovedCheckout] = []
    /// The live applications and what a redeploy of each restarts.
    var applications: [ServerApplication] = []
    /// What the collector could and could not read,  when the report says.
    var collection: ServerCollection?
    /// How long the cpu of the processes was sampled over,  when the report says.
    var processSampleSeconds: Double?
    /// The verdict for the whole report.
    var state: CellState = .unknown

    /// True when the panel should offer Redeploy for `resource`.
    ///
    /// The server redeploys the first row of its report whose uuid matches,  so the row
    /// must be redeployable itself and so must the first row that shares its uuid.  A row that
    /// is gone is never offered,  and a row that lists its actions is offered only what it
    /// lists,  because the collector asked the helper.
    func offersRedeploy(_ resource: CoolifyResource) -> Bool {
        guard resource.isRedeployable, resource.isOnHost else { return false }
        if let actions = resource.actions, !actions.contains("redeploy") { return false }
        guard let first = coolify.first(where: { $0.uuid == resource.uuid }) else { return false }
        return first.isRedeployable && first.isOnHost
    }

    /// The application a row belongs to,  when the report lists it.
    private func application(of resource: CoolifyResource) -> ServerApplication? {
        applications.first { $0.uuid == resource.uuid }
    }

    /// The name a redeploy of this row acts on,  which is the application and not one service.
    func redeployName(of resource: CoolifyResource) -> String {
        let name = application(of: resource)?.name ?? resource.name
        return name.isEmpty ? "this resource" : name
    }

    /// What a redeploy of this row restarts,  in words.
    func redeployHelp(of resource: CoolifyResource) -> String {
        let plan = "Plan a redeploy of this application on the server."
        guard let count = application(of: resource)?.containers, count > 1 else { return plan }
        return "\(plan)\(ServerStatusCopy.sentenceGap)It restarts all \(count) of its containers."
    }

    /// The rows with the worst first,  and otherwise in the report's own order.
    var coolifyProblemsFirst: [CoolifyResource] {
        coolify.enumerated().sorted { left, right in
            let leftRank = GridBuilder.severityIndex(of: left.element.state)
            let rightRank = GridBuilder.severityIndex(of: right.element.state)
            return leftRank == rightRank ? left.offset < right.offset : leftRank < rightRank
        }.map { $0.element }
    }

    /// True when the report has something to say about units:  rows,  a failed unit,  or the
    /// collector saying it could not read them.  A collector that predates all three does not.
    var hasServicesHalf: Bool {
        !services.isEmpty
            || failedUnits?.isEmpty == false
            || collection?.services?.isIncomplete == true
            || collection?.failedUnits?.isIncomplete == true
    }

    /// A half the collector did not read whole may not read as a pass,  however good its rows
    /// look.  A worse state stands,  and a report with no `collection` says nothing here.
    private func provenState(_ state: CellState, _ entries: ServerCollectionEntry?...) -> CellState {
        guard state == .passed, entries.contains(where: { $0?.isIncomplete == true }) else {
            return state
        }
        return .unproven
    }

    /// The files whose drift is neither `unchanged` nor `info`:  the ones worth looking at.
    var driftedFiles: [WatchedFile] {
        files.filter { $0.fileDrift != .unchanged && $0.fileDrift != .info }
    }

    /// The files that changed in a way the collector reports for information only.
    var noticedFiles: [WatchedFile] {
        files.filter { $0.fileDrift == .info }
    }

    /// True when the collector stopped at its limit,  so the file list is a partial one.
    var filesTruncated: Bool { filesSummary?.truncated == true }

    /// The services half's own state.
    ///
    /// A failed unit counts whether or not anything else in the report names it.
    var servicesState: CellState {
        guard isCurrent else { return .unproven }
        var states = services.map(\.state)
        if failedUnits?.isEmpty == false { states.append(.missing) }
        return provenState(worst(of: states, empty: .unproven), collection?.services, collection?.failedUnits)
    }

    /// The host half's own state.
    var hostState: CellState {
        guard isCurrent, let metrics = hostMetrics else { return .unproven }
        return metrics.state
    }

    /// The Coolify half's own state.
    ///
    /// An empty list proves nothing:  the collector could not read docker,  or the
    /// host has no containers,  and neither of those is a pass.  A report out of its
    /// window proves nothing either,  so the half reads as unproven and not as a pass.
    var coolifyState: CellState {
        guard isCurrent else { return .unproven }
        return provenState(worst(of: coolify.map(\.state), empty: .unproven), collection?.coolify)
    }

    /// The processes half's own state.
    var processState: CellState {
        guard isCurrent else { return .unproven }
        return provenState(processes.isEmpty ? .unproven : .passed, collection?.processes)
    }

    /// The config half's own state.
    var configState: CellState {
        guard isCurrent else { return .unproven }
        return provenState(worst(of: files.map(\.state), empty: .unproven), collection?.files)
    }

    /// The worst of a set of states.
    private func worst(of states: [CellState], empty: CellState) -> CellState {
        guard !states.isEmpty else { return empty }
        var result = CellState.passed
        for state in states {
            if GridBuilder.severityIndex(of: state) < GridBuilder.severityIndex(of: result) {
                result = state
            }
        }
        return result
    }
}

// MARK: - The reader

/// Reads the collector's report and says what it means.
enum ServerStatusReportReader {
    /// Where the collector writes the report.
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("server-status.json", isDirectory: false)
    }

    /// Read the report at a path and say what it means.
    ///
    /// Every way of not knowing ends the same way:  the reading carries a problem and
    /// no rows,  so nothing on screen can be read as a pass.
    static func read(at url: URL, now: Date = Date()) -> ServerStatusReading {
        var reading = ServerStatusReading()
        reading.path = url.path

        let data: Data
        if FileManager.default.fileExists(atPath: url.path) {
            reading.copiedAt = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date
            do {
                data = try Data(contentsOf: url)
            } catch {
                reading.problem =
                    "The report could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
                reading.refusal = .unreadable
                return reading
            }
        } else {
            reading.problem =
                "The report could not be read:\(ServerStatusCopy.sentenceGap)the file is not there"
            reading.fileMissing = true
            return reading
        }

        let report: ServerStatusReport
        do {
            report = try JSONDecoder().decode(ServerStatusReport.self, from: data)
        } catch {
            reading.problem =
                "The report could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
            reading.refusal = .unreadable
            return reading
        }

        guard report.schemaVersion == ServerStatusContract.supportedSchemaVersion else {
            reading.problem =
                "The report is version \(report.schemaVersion);\(ServerStatusCopy.sentenceGap)this app reads version \(ServerStatusContract.supportedSchemaVersion)."
            reading.refusal = ReportRefusal(reportVersion: report.schemaVersion,
                                            supported: ServerStatusContract.supportedSchemaVersion)
            return reading
        }
        guard report.tool == ServerStatusContract.expectedTool else {
            reading.problem =
                "The report came from \"\(report.tool)\";\(ServerStatusCopy.sentenceGap)this app reads \"\(ServerStatusContract.expectedTool)\"."
            reading.refusal = .appOutOfDate
            return reading
        }
        // A collector that names its role and does not name the server wrote this report
        // somewhere else,  so nothing in it describes the server.
        if let role = report.hostRole, role != "server" {
            reading.problem = "This report did not come from the server."
            reading.refusal = .notFromServer
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem =
                "The report carries no time this app can read,\(ServerStatusCopy.sentenceGap)so nothing in it is current."
            reading.refusal = .unreadable
            return reading
        }

        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem =
                "The report is dated in the future,\(ServerStatusCopy.sentenceGap)so nothing in it is current."
            reading.futureDated = true
            return reading
        }
        reading.isCurrent = seconds <= ServerStatusContract.maxAgeSeconds
        reading.host = report.host ?? ""
        reading.coolify = report.coolify
        reading.processes = report.processes
        reading.files = report.files
        reading.hostMetrics = report.hostMetrics
        reading.services = report.services
        reading.filesSummary = report.filesSummary
        reading.resourcePolicy = report.resourcePolicy
        reading.failedUnits = report.failedUnits
        reading.removedCheckouts = report.removedCheckouts
        reading.applications = report.applications
        reading.collection = report.collection
        reading.processSampleSeconds = report.processSampleSeconds
        // A report out of its window is not a reading:  the whole report reads unknown,
        // even where every row in it looks fine.
        reading.state = overall(of: reading)
        return reading
    }

    /// Read the report the collector writes for this user.
    static func readDefault(now: Date = Date()) -> ServerStatusReading {
        read(at: reportURL(), now: now)
    }

    /// The report's own state.
    ///
    /// A report that proved nothing at all reads as unknown,  and a report out of its
    /// window reads as unknown too.
    private static func overall(of reading: ServerStatusReading) -> CellState {
        guard reading.isCurrent else { return .unknown }
        if reading.coolify.isEmpty && reading.processes.isEmpty && reading.files.isEmpty {
            return .unknown
        }
        var halves = [reading.coolifyState, reading.processState, reading.configState]
        // The services half counts only when the report carries one:  a collector that
        // predates it is not a server whose services are unproven.
        if reading.hasServicesHalf {
            halves.append(reading.servicesState)
        }
        var worst = CellState.passed
        for state in halves {
            if GridBuilder.severityIndex(of: state) < GridBuilder.severityIndex(of: worst) {
                worst = state
            }
        }
        return worst
    }
}

// MARK: - The format

/// The sizes and times the panel shows.
enum ServerStatusFormat {
    /// A size in bytes,  in the units the fleet reads.
    static func bytes(_ count: Int) -> String {
        let units = ["B", "KB", "MB", "GB", "TB"]
        var value = Double(count)
        var index = 0
        while value >= 1024, index < units.count - 1 {
            value /= 1024
            index += 1
        }
        if index == 0 { return "\(count) B" }
        return String(format: "%.1f %@", value, units[index])
    }

    /// When something happened or will happen,  to follow a verb:  `at 4:52am` today,  and
    /// `Nov 3 at 4:52am` on any other day,  so a run from days ago never reads as today's.
    static func moment(_ date: Date, now: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let clock = FleetTime.clock(date)
        if calendar.isDate(date, inSameDayAs: now) { return "at \(clock)" }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.locale = Locale(identifier: "en_US_POSIX")
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        formatter.dateFormat = sameYear ? "MMM d" : "MMM d, yyyy"
        return "\(formatter.string(from: date)) at \(clock)"
    }
}
