import Foundation

// The Worker's Linear token health,  as Muster reads it.
//
// The file is `~/.local/state/fleet-tools/worker-health.json`:  the Mac's
// `com.jay.fleet-worker-health` agent runs `scripts/fleet-worker-health.py` every fifteen
// minutes,  which asks the Worker's signed health route and writes what it answered.  The
// answer holds ages,  counts and fixed codes only:  no token and no upstream text.
//
// The rule this file follows is the doctor's rule:  a report that cannot be trusted reads
// as unknown,  never as a pass.  A missing file,  a malformed file,  a version this app does
// not read and a report older than forty-five minutes all read as unknown,  and a Worker
// the collector could not reach reads as unknown with the collector's own code.
//
// Nothing here reaches the network and nothing here reads a credential.

// MARK: - The contract

/// The pinned contract for the collector's file.
enum WorkerHealthContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-worker-health"

    /// The collector runs every fifteen minutes,  so three of its runs are the window a
    /// file is current in:  the same window the doctor's report has.
    static let maxAgeSeconds = 45 * 60
}

// MARK: - The vocabulary

/// A seat's state,  or the Worker's worst one,  in the Worker's own vocabulary.
///
/// A state this app does not know is `unknown`,  never `ok`.
enum WorkerHealthState: String {
    case ok
    case warn
    case fail
    case unknown

    init(reportValue: String?) {
        guard
            let value = reportValue,
            let state = WorkerHealthState(rawValue: value)
        else {
            self = .unknown
            return
        }
        self = state
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

// MARK: - The rows

/// One seat's Linear token,  as the Worker described it.
struct WorkerHealthSeat: Decodable, Identifiable, Equatable {
    let seat: String
    let state: String?
    let minted: Bool?
    let mintedAgeDays: Double?
    let expiresInDays: Double?
    let remintDueInDays: Double?
    let mintCount: Int?
    let lastMintOkAt: String?
    let lastMintFailAt: String?
    let lastMintFailKind: String?
    let note: String?

    /// The Worker writes its fields in snake case,  and the collector keeps them as the
    /// Worker wrote them.
    enum CodingKeys: String, CodingKey {
        case seat, state, minted, note
        case mintedAgeDays = "minted_age_days"
        case expiresInDays = "expires_in_days"
        case remintDueInDays = "remint_due_in_days"
        case mintCount = "mint_count"
        case lastMintOkAt = "last_mint_ok_at"
        case lastMintFailAt = "last_mint_fail_at"
        case lastMintFailKind = "last_mint_fail_kind"
    }

    /// A seat decodes field by field,  so one field of a type this app did not expect
    /// costs that field and not the whole file.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        seat = (try? container.decodeIfPresent(String.self, forKey: .seat)) ?? ""
        state = try? container.decodeIfPresent(String.self, forKey: .state)
        minted = try? container.decodeIfPresent(Bool.self, forKey: .minted)
        mintedAgeDays = try? container.decodeIfPresent(Double.self, forKey: .mintedAgeDays)
        expiresInDays = try? container.decodeIfPresent(Double.self, forKey: .expiresInDays)
        remintDueInDays = try? container.decodeIfPresent(Double.self, forKey: .remintDueInDays)
        mintCount = try? container.decodeIfPresent(Int.self, forKey: .mintCount)
        lastMintOkAt = try? container.decodeIfPresent(String.self, forKey: .lastMintOkAt)
        lastMintFailAt = try? container.decodeIfPresent(String.self, forKey: .lastMintFailAt)
        lastMintFailKind = try? container.decodeIfPresent(String.self, forKey: .lastMintFailKind)
        note = try? container.decodeIfPresent(String.self, forKey: .note)
    }

    var id: String { seat }

    /// The seat's state,  in the Worker's vocabulary.
    var healthState: WorkerHealthState { WorkerHealthState(reportValue: state) }

    /// The seat's state,  in the grid's vocabulary.
    var cellState: CellState { healthState.cellState }

    /// The token's age and expiry,  in one line.
    var tokenText: String {
        var parts: [String] = []
        if let age = mintedAgeDays { parts.append(String(format: "minted %.1fd ago", age)) }
        if let left = expiresInDays { parts.append(String(format: "expires in %.1fd", left)) }
        if minted == false { parts.append("never minted") }
        return parts.joined(separator: ", ")
    }
}

// MARK: - The file

/// The file,  as the collector writes it.
struct WorkerHealthReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let reachable: Bool
    let worst: String?
    let bundle: String?
    let error: String?
    let seats: [WorkerHealthSeat]

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, reachable, worst, bundle, error, seats
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        reachable = (try? container.decodeIfPresent(Bool.self, forKey: .reachable)) == true
        worst = try? container.decodeIfPresent(String.self, forKey: .worst)
        bundle = try? container.decodeIfPresent(String.self, forKey: .bundle)
        error = try? container.decodeIfPresent(String.self, forKey: .error)
        seats = (try? container.decodeIfPresent([WorkerHealthSeat].self, forKey: .seats)) ?? []
    }
}

// MARK: - The reading

/// Everything the card shows,  from one read of the file.
struct WorkerHealthReading {
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
    /// Whether the collector reached the Worker.
    var reachable: Bool = false
    /// Why the collector could not reach the Worker,  in the app's own words.
    var unreachableReason: String?
    /// The Worker's worst seat state.
    var worst: WorkerHealthState = .unknown
    /// Whether the Worker's credential bundle is valid,  when it said.
    var bundle: String?
    /// The seats,  in the file's own order.
    var seats: [WorkerHealthSeat] = []
    /// The verdict for the whole file.
    var state: CellState = .unknown
}

// MARK: - The reader

/// Reads the collector's file and says what it means.
enum WorkerHealthReportReader {
    /// Where the collector writes the file.
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("worker-health.json", isDirectory: false)
    }

    /// The collector's fixed codes,  in words.
    ///
    /// A code this app does not know is shown as it is,  because it is a fixed code and
    /// never upstream text.
    static func describe(errorCode: String?) -> String {
        guard let code = errorCode, !code.isEmpty else { return "the collector did not say why" }
        switch code {
        case "no_key": return "the signing key could not be read on this Mac"
        case "no_answer": return "the Worker did not answer"
        case "not_configured_or_refused": return "the Worker refused the request or has no health route"
        default:
            if code.hasPrefix("http_") {
                return "the Worker answered HTTP \(code.dropFirst(5))"
            }
            return code
        }
    }

    /// Read the file at a path and say what it means.
    static func read(at url: URL, now: Date = Date()) -> WorkerHealthReading {
        var reading = WorkerHealthReading()
        reading.path = url.path
        let gap = ServerStatusCopy.sentenceGap

        let data: Data
        if FileManager.default.fileExists(atPath: url.path) {
            do {
                data = try Data(contentsOf: url)
            } catch {
                reading.problem = "The file could not be read:\(gap)\(error.localizedDescription)"
                return reading
            }
        } else {
            reading.problem = "The file could not be read:\(gap)the file is not there"
            return reading
        }

        let report: WorkerHealthReport
        do {
            report = try JSONDecoder().decode(WorkerHealthReport.self, from: data)
        } catch {
            reading.problem = "The file could not be read:\(gap)\(error.localizedDescription)"
            return reading
        }

        guard report.schemaVersion == WorkerHealthContract.supportedSchemaVersion else {
            reading.problem =
                "The file is version \(report.schemaVersion);\(gap)this app reads version \(WorkerHealthContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == WorkerHealthContract.expectedTool else {
            reading.problem =
                "The file came from \"\(report.tool)\";\(gap)this app reads \"\(WorkerHealthContract.expectedTool)\"."
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem =
                "The file carries no time this app can read,\(gap)so nothing in it is current."
            return reading
        }

        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem = "The file is dated in the future,\(gap)so nothing in it is current."
            return reading
        }
        reading.isCurrent = seconds <= WorkerHealthContract.maxAgeSeconds
        reading.reachable = report.reachable
        reading.bundle = report.bundle
        if report.reachable {
            reading.worst = WorkerHealthState(reportValue: report.worst)
            reading.seats = report.seats
        } else {
            // An unreachable Worker's seats are not a reading:  the collector writes none,
            // and any it did write would be observations that were never made.
            reading.worst = .unknown
            reading.unreachableReason = describe(errorCode: report.error)
        }
        reading.state = overall(of: reading)
        return reading
    }

    /// Read the file the collector writes for this user.
    static func readDefault(now: Date = Date()) -> WorkerHealthReading {
        read(at: reportURL(), now: now)
    }

    /// The file's own state.
    ///
    /// A file out of its window is unknown,  a Worker that could not be reached is
    /// unproven,  and a bundle the Worker calls invalid is a failure whatever the seats say.
    private static func overall(of reading: WorkerHealthReading) -> CellState {
        guard reading.isCurrent else { return .unknown }
        guard reading.reachable else { return .unproven }
        if reading.bundle == "invalid" { return .missing }
        var worst = reading.worst.cellState
        for seat in reading.seats
        where GridBuilder.severityIndex(of: seat.cellState) < GridBuilder.severityIndex(of: worst) {
            worst = seat.cellState
        }
        return worst
    }
}
