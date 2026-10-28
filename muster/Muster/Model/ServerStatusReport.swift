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

    enum CodingKeys: String, CodingKey {
        case uuid, name, type, status, cpuPercent, memoryBytes, image, updatedAt, uuidSource
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
    }

    /// The row's identity.
    ///
    /// The image is in it because two services of one Coolify application share the
    /// application's uuid and the resource name,  and a row must still be one row.
    var id: String { "\(uuid)/\(name)/\(image ?? "")" }

    /// The kind of resource,  in this app's vocabulary.
    var resourceType: CoolifyResourceType { CoolifyResourceType(reportValue: type) }

    /// The state of the resource,  in this app's vocabulary.
    var resourceStatus: CoolifyResourceStatus { CoolifyResourceStatus(reportValue: status) }

    /// The state the resource reads as,  in the grid's own vocabulary.
    ///
    /// A resource that is running reads as a pass,  one that exited or is unhealthy
    /// reads as a failure,  one that is gone reads as drift,  and one nobody could
    /// tell reads as unproven rather than as a pass.
    var state: CellState {
        switch resourceStatus {
        case .running: return .passed
        case .exited, .unhealthy: return .missing
        case .absent: return .drift
        case .unknown: return .unproven
        }
    }

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
}

/// One process on the server,  as the collector saw it.
struct ServerProcess: Decodable, Identifiable, Equatable {
    let pid: Int
    let owner: String?
    let name: String?
    let cpuPercent: Double?
    let memoryBytes: Int?

    enum CodingKeys: String, CodingKey {
        case pid, owner, name, cpuPercent, memoryBytes
    }

    /// A process decodes field by field,  for the same reason a resource does.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pid = (try? container.decodeIfPresent(Int.self, forKey: .pid)) ?? 0
        owner = try? container.decodeIfPresent(String.self, forKey: .owner)
        name = try? container.decodeIfPresent(String.self, forKey: .name)
        cpuPercent = try? container.decodeIfPresent(Double.self, forKey: .cpuPercent)
        memoryBytes = try? container.decodeIfPresent(Int.self, forKey: .memoryBytes)
    }

    var id: Int { pid }

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

    enum CodingKeys: String, CodingKey {
        case path, bytes, modifiedAt, sha256, drift
    }

    /// A file decodes field by field,  for the same reason a resource does.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        path = (try? container.decodeIfPresent(String.self, forKey: .path)) ?? ""
        bytes = try? container.decodeIfPresent(Int.self, forKey: .bytes)
        modifiedAt = try? container.decodeIfPresent(String.self, forKey: .modifiedAt)
        sha256 = try? container.decodeIfPresent(String.self, forKey: .sha256)
        drift = try? container.decodeIfPresent(String.self, forKey: .drift)
    }

    var id: String { path }

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
    var usedFraction: Double? {
        guard let total = totalBytes, let available = availableBytes, total > 0 else { return nil }
        return min(max(Double(total - available) / Double(total), 0), 1)
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
        guard let total = totalBytes, let free = freeBytes, total > 0 else { return nil }
        return min(max(Double(total - free) / Double(total), 0), 1)
    }
}

/// One mounted file system,  as the collector read it with `statvfs`.
struct HostDisk: Decodable, Identifiable, Equatable {
    let mount: String
    let totalBytes: Int?
    let usedBytes: Int?
    let freeBytes: Int?

    enum CodingKeys: String, CodingKey {
        case mount, totalBytes, usedBytes, freeBytes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        mount = (try? container.decodeIfPresent(String.self, forKey: .mount)) ?? ""
        totalBytes = try? container.decodeIfPresent(Int.self, forKey: .totalBytes)
        usedBytes = try? container.decodeIfPresent(Int.self, forKey: .usedBytes)
        freeBytes = try? container.decodeIfPresent(Int.self, forKey: .freeBytes)
    }

    var id: String { mount }

    /// The share of the disk in use, from 0 to 1.
    ///
    /// Used over used plus free,  the way `df` counts it,  so the space kept back for
    /// root does not read as free space the fleet can use.
    var usedFraction: Double? {
        if let used = usedBytes, let free = freeBytes, used + free > 0 {
            return min(max(Double(used) / Double(used + free), 0), 1)
        }
        if let used = usedBytes, let total = totalBytes, total > 0 {
            return min(max(Double(used) / Double(total), 0), 1)
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

    enum CodingKeys: String, CodingKey {
        case loadAverage, cpuCount, memory, swap, uptimeSeconds, disks
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        loadAverage = (try? container.decodeIfPresent([Double].self, forKey: .loadAverage)) ?? []
        cpuCount = try? container.decodeIfPresent(Int.self, forKey: .cpuCount)
        memory = try? container.decodeIfPresent(HostMemory.self, forKey: .memory)
        swap = try? container.decodeIfPresent(HostSwap.self, forKey: .swap)
        uptimeSeconds = try? container.decodeIfPresent(Int.self, forKey: .uptimeSeconds)
        disks = (try? container.decodeIfPresent([HostDisk].self, forKey: .disks)) ?? []
    }

    /// The one-minute load over the cpu count, from 0 up.
    ///
    /// A load of one per cpu is a full machine,  so the bar is drawn against that and
    /// can run past it.
    var loadFraction: Double? {
        guard let load = loadAverage.first, let cpus = cpuCount, cpus > 0 else { return nil }
        return max(load / Double(cpus), 0)
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
/// The collector reads only the units the allowlist names (`server.units` in
/// `scripts/fleet-ops-control.json`),  and `controllable` says whether the allowlist lets
/// Muster start,  stop or restart it.
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

    enum CodingKeys: String, CodingKey {
        case unit, scope, description, activeState, subState, since, enabled, controllable
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
    }

    /// Two scopes can hold units of one name,  so the scope is part of the identity.
    var id: String { "\(scope ?? "system")/\(unit)" }

    /// The state the unit reads as.
    ///
    /// An active unit is a pass,  a failed one is a failure,  one that is stopped is
    /// drift,  and one that is between states or that nobody could read is unproven.
    var state: CellState {
        switch activeState ?? "" {
        case "active": return .passed
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

/// How many watched files the collector found,  and whether it stopped at its limit.
struct ServerFilesSummary: Decodable, Equatable {
    let found: Int?
    let limit: Int?
    let truncated: Bool

    enum CodingKeys: String, CodingKey {
        case found, limit, truncated
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        found = try? container.decodeIfPresent(Int.self, forKey: .found)
        limit = try? container.decodeIfPresent(Int.self, forKey: .limit)
        truncated = (try? container.decodeIfPresent(Bool.self, forKey: .truncated)) == true
    }
}

// MARK: - The report

/// The report,  as the collector writes it.
struct ServerStatusReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let host: String?
    let coolify: [CoolifyResource]
    let processes: [ServerProcess]
    let files: [WatchedFile]
    /// The host's own numbers,  when the collector wrote them.
    let hostMetrics: HostMetrics?
    /// The fleet's systemd units,  when the collector wrote them.
    let services: [ServerService]
    /// How many files the collector found,  when it said.
    let filesSummary: ServerFilesSummary?

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, host, coolify, processes, files
        case hostMetrics, services, filesSummary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate:  a report that does not carry them is
        // not a report this app can judge.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        host = try? container.decodeIfPresent(String.self, forKey: .host)
        coolify = (try? container.decodeIfPresent([CoolifyResource].self, forKey: .coolify)) ?? []
        processes = (try? container.decodeIfPresent([ServerProcess].self, forKey: .processes)) ?? []
        files = (try? container.decodeIfPresent([WatchedFile].self, forKey: .files)) ?? []
        // The three newer halves are optional:  a report from a collector that predates
        // them still decodes,  and a half that is malformed costs that half alone.
        hostMetrics = try? container.decodeIfPresent(HostMetrics.self, forKey: .hostMetrics)
        services = (try? container.decodeIfPresent([ServerService].self, forKey: .services)) ?? []
        filesSummary = try? container.decodeIfPresent(ServerFilesSummary.self, forKey: .filesSummary)
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
    /// The verdict for the whole report.
    var state: CellState = .unknown

    /// True when the panel should offer Redeploy for `resource`.
    ///
    /// The server redeploys the first row of its report whose uuid matches,  so the row
    /// must be redeployable itself and so must the first row that shares its uuid.
    func offersRedeploy(_ resource: CoolifyResource) -> Bool {
        guard resource.isRedeployable else { return false }
        return coolify.first(where: { $0.uuid == resource.uuid })?.isRedeployable == true
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
    var servicesState: CellState {
        guard isCurrent else { return .unproven }
        return worst(of: services.map(\.state), empty: .unproven)
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
        return worst(of: coolify.map(\.state), empty: .unproven)
    }

    /// The processes half's own state.
    var processState: CellState {
        guard isCurrent else { return .unproven }
        return processes.isEmpty ? .unproven : .passed
    }

    /// The config half's own state.
    var configState: CellState {
        guard isCurrent else { return .unproven }
        return worst(of: files.map(\.state), empty: .unproven)
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
            do {
                data = try Data(contentsOf: url)
            } catch {
                reading.problem =
                    "The report could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
                return reading
            }
        } else {
            reading.problem =
                "The report could not be read:\(ServerStatusCopy.sentenceGap)the file is not there"
            return reading
        }

        let report: ServerStatusReport
        do {
            report = try JSONDecoder().decode(ServerStatusReport.self, from: data)
        } catch {
            reading.problem =
                "The report could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
            return reading
        }

        guard report.schemaVersion == ServerStatusContract.supportedSchemaVersion else {
            reading.problem =
                "The report is version \(report.schemaVersion);\(ServerStatusCopy.sentenceGap)this app reads version \(ServerStatusContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == ServerStatusContract.expectedTool else {
            reading.problem =
                "The report came from \"\(report.tool)\";\(ServerStatusCopy.sentenceGap)this app reads \"\(ServerStatusContract.expectedTool)\"."
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
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
        reading.isCurrent = seconds <= ServerStatusContract.maxAgeSeconds
        reading.host = report.host ?? ""
        reading.coolify = report.coolify
        reading.processes = report.processes
        reading.files = report.files
        reading.hostMetrics = report.hostMetrics
        reading.services = report.services
        reading.filesSummary = report.filesSummary
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
        if !reading.services.isEmpty {
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

/// The sizes the panel shows.
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
}
