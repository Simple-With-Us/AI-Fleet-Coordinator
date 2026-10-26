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

    enum CodingKeys: String, CodingKey {
        case uuid, name, type, status, cpuPercent, memoryBytes, image, updatedAt
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
        case .unchanged: return .passed
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

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, host, coolify, processes, files
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
    /// The verdict for the whole report.
    var state: CellState = .unknown

    /// The files whose drift is not `unchanged`:  the ones worth looking at.
    var driftedFiles: [WatchedFile] {
        files.filter { $0.fileDrift != .unchanged }
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
        var worst = CellState.passed
        for state in [reading.coolifyState, reading.processState, reading.configState] {
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
