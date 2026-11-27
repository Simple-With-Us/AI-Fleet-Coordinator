import Darwin
import Foundation

// Socratic Trade and Congress.Trade operational health,  as Muster reads it (AFC-398).
//
// The file is `~/.local/state/fleet-tools/api-health.json`:  the `api` collector,
// `scripts/fleet-api-health.py`, polls the two apps' own health routes on the Mac and
// writes what the pinned mapping contract made of the answers.  The store holds
// states, fixed reason codes and numbers only:  no credential and no upstream text.
//
// The rule this file follows is the doctor's rule:  a report that cannot be trusted
// reads as unknown,  never as a pass.  A missing file,  a malformed file,  a version
// this app does not read and a report older than forty-five minutes all read as
// unknown,  and an app row the collector could not observe keeps its fixed error
// code and reads as unproven rather than as healthy.
//
// Muster only displays this file.  It reaches no network here, reads no credential,
// and decides no letters:  the Operator decides letters.

// MARK: - The contract

/// The pinned contract for the collector's file.
enum ApiHealthContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-api-health"
    static let expectedCollector = "api"
    static let maxBytes = 256 * 1024

    /// The collector runs every fifteen minutes,  so three of its runs are the window a
    /// file is current in:  the same window the doctor's report has.
    static let maxAgeSeconds = 45 * 60
}

// MARK: - The vocabulary

/// One app's mapped state,  in the collector's own vocabulary.
///
/// A state this app does not know is `unknown`,  never `ok`.
enum ApiHealthState: String {
    case ok
    case warn
    case fail
    case unknown

    init(reportValue: String?) {
        guard
            let value = reportValue,
            let state = ApiHealthState(rawValue: value)
        else {
            self = .unknown
            return
        }
        self = state
    }

    /// The collector's ranking,  worst highest:  ok,  warn,  unknown,  fail.
    var rank: Int {
        switch self {
        case .ok: return 0
        case .warn: return 1
        case .unknown: return 2
        case .fail: return 3
        }
    }

    /// The state in the grid's own vocabulary.
    var cellState: CellState {
        switch self {
        case .ok: return .passed
        case .warn: return .drift
        case .fail: return .missing
        case .unknown: return .unproven
        }
    }
}

/// One CT component,  as the collector mapped it.
struct ApiHealthComponent: Decodable, Equatable {
    let name: String
    let state: String?
    let reported: Bool?

    var healthState: ApiHealthState { ApiHealthState(reportValue: state) }
    var cellState: CellState { healthState.cellState }
}

/// Where a row's answer came from.
struct ApiHealthProvenance: Decodable, Equatable {
    let source: String?
    let fetchedAt: String?
    let httpStatus: Int?
}

/// One app's row:  Socratic Trade or Congress.Trade.
struct ApiHealthApp: Decodable, Identifiable, Equatable {
    let app: String
    let displayName: String?
    let state: String?
    let reason: String?
    let error: String?
    /// ST only:  whether Mango was reachable,  when the route reported it.
    let mangoReachable: Bool?
    /// ST only:  the age of the last observation in seconds,  when proved.
    let observationAgeSeconds: Int?
    /// CT only:  the upstream aggregate word,  verbatim,  when reported.
    let upstreamStatus: String?
    /// CT only:  whether ingestion was observed alive.
    let ingestionAlive: Bool?
    let components: [ApiHealthComponent]
    let provenance: ApiHealthProvenance?

    enum CodingKeys: String, CodingKey {
        case app, displayName, state, reason, error, mangoReachable
        case observationAgeSeconds, upstreamStatus, ingestionAlive, components, provenance
    }

    /// A row of the wrong shape throws,  and the whole file then reads as unknown:
    /// nothing downstream ever sees a field the collector did not write as written.
    /// A field the collector leaves out or writes as null is nil;  ST rows carry no
    /// components.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        app = try container.decode(String.self, forKey: .app)
        displayName = try container.decodeIfPresent(String.self, forKey: .displayName)
        state = try container.decodeIfPresent(String.self, forKey: .state)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        error = try container.decodeIfPresent(String.self, forKey: .error)
        mangoReachable = try container.decodeIfPresent(Bool.self, forKey: .mangoReachable)
        observationAgeSeconds = try container.decodeIfPresent(Int.self, forKey: .observationAgeSeconds)
        upstreamStatus = try container.decodeIfPresent(String.self, forKey: .upstreamStatus)
        ingestionAlive = try container.decodeIfPresent(Bool.self, forKey: .ingestionAlive)
        components = try container.decodeIfPresent([ApiHealthComponent].self, forKey: .components) ?? []
        provenance = try container.decodeIfPresent(ApiHealthProvenance.self, forKey: .provenance)
    }

    var id: String { app }

    /// The app's state,  in the collector's vocabulary.
    var healthState: ApiHealthState { ApiHealthState(reportValue: state) }

    /// The app's state,  in the grid's vocabulary.
    var cellState: CellState { healthState.cellState }
}

// MARK: - The file

/// The file,  as the collector writes it.
struct ApiHealthReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let collector: String?
    let generatedAt: String?
    let worst: String?
    let apps: [ApiHealthApp]

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        collector = try container.decodeIfPresent(String.self, forKey: .collector)
        generatedAt = try container.decodeIfPresent(String.self, forKey: .generatedAt)
        worst = try container.decodeIfPresent(String.self, forKey: .worst)
        apps = try container.decode([ApiHealthApp].self, forKey: .apps)
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, collector, generatedAt, worst, apps
    }
}

// MARK: - The reading

/// Everything a panel shows,  from one read of the file.
struct ApiHealthReading {
    /// Where the file is.
    var path: String = ""
    /// When the file says it was written.
    var generatedAt: Date?
    /// How old the file is,  in the app's own words.
    var age: String?
    /// Whether the file is inside the window the collector runs in.
    var isCurrent: Bool = false
    /// Why the file could not be read at all,  when it could not be.
    var problem: String?
    /// The collector's worst app state.
    var worst: ApiHealthState = .unknown
    /// The apps,  in the file's own order.
    var apps: [ApiHealthApp] = []
    /// The verdict for the whole file.
    var state: CellState = .unknown
}

// MARK: - The reader

/// Reads the collector's file and says what it means.
enum ApiHealthReportReader {
    /// Where the collector writes the file.
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("api-health.json", isDirectory: false)
    }

    /// Read the file at a path and say what it means.
    static func read(at url: URL, now: Date = Date()) -> ApiHealthReading {
        var reading = ApiHealthReading()
        reading.path = url.path
        let gap = ServerStatusCopy.sentenceGap

        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else {
            reading.problem = errno == ENOENT
                ? "The file could not be read:\(gap)the file is not there"
                : "The API health report could not be opened as a regular file."
            return reading
        }
        defer { close(fd) }

        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o022 == 0,
              info.st_size >= 0, info.st_size <= off_t(ApiHealthContract.maxBytes) else {
            reading.problem = "The API health report is too large or is not a trusted file for this user."
            return reading
        }
        let reader = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
        let data: Data
        do {
            data = try reader.read(upToCount: ApiHealthContract.maxBytes + 1) ?? Data()
        } catch {
            reading.problem = "The API health report could not be read."
            return reading
        }
        guard data.count <= ApiHealthContract.maxBytes else {
            reading.problem = "The API health report is too large."
            return reading
        }

        let report: ApiHealthReport
        do {
            report = try JSONDecoder().decode(ApiHealthReport.self, from: data)
        } catch {
            reading.problem = "The file could not be read:\(gap)\(error.localizedDescription)"
            return reading
        }

        guard report.schemaVersion == ApiHealthContract.supportedSchemaVersion else {
            reading.problem =
                "The file is version \(report.schemaVersion);\(gap)this app reads version \(ApiHealthContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == ApiHealthContract.expectedTool else {
            reading.problem =
                "The file came from \"\(report.tool)\";\(gap)this app reads \"\(ApiHealthContract.expectedTool)\"."
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem =
                "The file carries no time this app can read,\(gap)so nothing in it is current."
            return reading
        }

        let mappedApps = report.apps.filter { ["ST", "CT"].contains($0.app) }
        guard Set(mappedApps.map(\.app)).count == mappedApps.count else {
            reading.problem = "The API health report contains duplicate app rows."
            return reading
        }
        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem = "The file is dated in the future,\(gap)so nothing in it is current."
            return reading
        }
        reading.isCurrent = seconds <= ApiHealthContract.maxAgeSeconds
        if reading.isCurrent {
            reading.worst = ApiHealthState(reportValue: report.worst)
            reading.apps = mappedApps
        }
        reading.state = overall(of: reading)
        return reading
    }

    /// Read the file the collector writes for this user.
    static func readDefault(now: Date = Date()) -> ApiHealthReading {
        read(at: reportURL(), now: now)
    }

    /// The file's own state.
    ///
    /// A file out of its window is unknown;  inside it,  the worst app's state rules,
    /// and an app the collector could not observe reads as unproven,  never as a pass.
    ///
    /// The apps are ranked the way the collector ranks them (`_STATE_RANK` in
    /// `scripts/fleet-api-health.py`),  so an unobserved app outranks a warning one
    /// and the reader never shows the file better than the collector wrote it.
    private static func overall(of reading: ApiHealthReading) -> CellState {
        guard reading.isCurrent, !reading.apps.isEmpty else { return .unknown }
        var worst = reading.worst
        for app in reading.apps where app.healthState.rank > worst.rank {
            worst = app.healthState
        }
        return worst.cellState
    }
}
