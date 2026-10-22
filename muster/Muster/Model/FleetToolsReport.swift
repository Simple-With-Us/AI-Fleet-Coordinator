import Foundation

// The fleet tools doctor's report,  as Muster reads it.
//
// The file is `~/.local/state/fleet-tools/report.json`:  the doctor writes it every
// fifteen minutes,  it covers both hosts,  and the protocol names it as "the report
// Muster reads" (`docs/protocols/fleet-tool-parity.md`,  section "The report Muster
// reads").
//
// The rule this file follows is that protocol's rule:  a report that cannot be trusted
// reads as unknown,  never as a pass.  A missing file,  a malformed file,  a version
// this app does not read and a report older than forty-five minutes all read as
// unknown,  and a host the doctor could not reach reads as unknown too.
//
// Nothing here reaches the network and nothing here reads a credential:  the report is
// one local file the doctor already wrote.

// MARK: - The contract

/// The pinned contract for the doctor's report.
///
/// The version is an exact integer,  never a range:  a change to the report's shape
/// raises it deliberately,  and this app refuses a report it does not know instead of
/// reading a part of it and calling the rest a pass.
enum FleetToolsContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-tools-doctor"

    /// The doctor's timer runs every fifteen minutes,  so three of its runs are the
    /// window a report is current in.
    static let maxAgeSeconds = 45 * 60
}

// MARK: - The rows

/// One row's state,  from the doctor's own vocabulary.
///
/// A state this app does not know is `unknown`,  never a healthy one:  an unreadable
/// state is not a passing one.
enum FleetToolsRowState: String {
    case current
    case stale
    case missing
    case unknown
    case absent

    init(reportValue: String?) {
        guard
            let value = reportValue,
            let state = FleetToolsRowState(rawValue: value)
        else {
            self = .unknown
            return
        }
        self = state
    }
}

/// One row's own status:  what the doctor said about the check.
///
/// The status is the doctor's own severity and the state is what it observed:  a row
/// behind is a WARN inside the grace window and a FAIL past it.
enum FleetToolsRowStatus: String {
    case ok = "OK"
    case warn = "WARN"
    case fail = "FAIL"
}

/// What one row contributes to a host's verdict.
enum FleetToolsRowOutcome {
    /// The check is current.
    case passing
    /// The check is behind,  inside the grace the doctor allows.
    case stale
    /// The doctor could not tell.
    case unknown
    /// The check failed.
    case failing
    /// An optional thing that is not installed,  which is information and not a fault.
    case absent
}

/// One row of a host's half of the report.
struct FleetToolsRow: Decodable, Identifiable, Equatable {
    let check: String
    let status: String?
    let state: String?
    let category: String?
    let tool: String?
    let detail: String?
    let reason: String?
    let installed: String?
    let expectedRev: String?
    let behindCommits: Int?

    /// The categories a reader leaves out of its verdict.
    ///
    /// The protocol says so:  `harness`,  `human-tree` and `presence` rows are not
    /// tool-currency rows,  so they are not the fleet's health.  Every other category
    /// counts,  including one this app does not know:  a new category is never
    /// silently dropped.
    static let excludedCategories: Set<String> = ["harness", "human-tree", "presence"]

    enum CodingKeys: String, CodingKey {
        case check, status, state, category, tool, detail, reason
        case installed, expectedRev, behindCommits
    }

    /// A row decodes field by field,  so one field of a type this app did not expect
    /// costs that field and not the whole report.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        check = (try? container.decodeIfPresent(String.self, forKey: .check)) ?? ""
        status = try? container.decodeIfPresent(String.self, forKey: .status)
        state = try? container.decodeIfPresent(String.self, forKey: .state)
        category = try? container.decodeIfPresent(String.self, forKey: .category)
        tool = try? container.decodeIfPresent(String.self, forKey: .tool)
        detail = try? container.decodeIfPresent(String.self, forKey: .detail)
        reason = try? container.decodeIfPresent(String.self, forKey: .reason)
        installed = try? container.decodeIfPresent(String.self, forKey: .installed)
        expectedRev = try? container.decodeIfPresent(String.self, forKey: .expectedRev)
        behindCommits = try? container.decodeIfPresent(Int.self, forKey: .behindCommits)
    }

    var id: String { check }

    /// The row's state,  as the doctor's vocabulary.
    var rowState: FleetToolsRowState { FleetToolsRowState(reportValue: state) }

    /// The row's status,  when this app knows it.
    var rowStatus: FleetToolsRowStatus? { status.flatMap(FleetToolsRowStatus.init(rawValue:)) }

    /// Whether this row counts towards the host's verdict.
    var countsInVerdict: Bool {
        guard let category, !category.isEmpty else { return true }
        return !FleetToolsRow.excludedCategories.contains(category)
    }

    /// What the row contributes to the host's verdict.
    ///
    /// The rule,  in order:
    ///
    ///   * a FAIL is a failure,  whatever the state beside it says
    ///   * a required thing that is absent is a failure
    ///   * a row behind is stale
    ///   * a row the doctor could not tell is unknown
    ///   * a status this app does not know is unknown,  never a pass
    ///   * an optional thing that is not installed is information
    var outcome: FleetToolsRowOutcome {
        if rowStatus == .fail { return .failing }
        if rowState == .missing { return .failing }
        if rowState == .stale { return .stale }
        if rowState == .unknown { return .unknown }
        if rowStatus == nil { return .unknown }
        if rowState == .absent { return .absent }
        return .passing
    }

    /// The row as the panel shows it,  in one line.
    var summary: String {
        if let detail, !detail.isEmpty { return detail }
        if let reason, !reason.isEmpty { return reason }
        return check
    }
}

// MARK: - The hosts

/// The tip the doctor compared against,  and how it read it.
struct FleetToolsExpected: Decodable, Equatable {
    let repo: String?
    let ref: String?
    let source: String?
    let rev: String?
    let readAt: String?

    enum CodingKeys: String, CodingKey {
        case repo, ref, source, rev, readAt
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        repo = try? container.decodeIfPresent(String.self, forKey: .repo)
        ref = try? container.decodeIfPresent(String.self, forKey: .ref)
        source = try? container.decodeIfPresent(String.self, forKey: .source)
        rev = try? container.decodeIfPresent(String.self, forKey: .rev)
        readAt = try? container.decodeIfPresent(String.self, forKey: .readAt)
    }
}

/// One host's half of the report.
struct FleetToolsHostDocument: Decodable, Equatable {
    let host: String
    let reachable: Bool
    let error: String?
    let schemaVersion: Int?
    let generatedAt: String?
    let doctorCommit: String?
    let expected: FleetToolsExpected?
    let checks: [FleetToolsRow]

    enum CodingKeys: String, CodingKey {
        case host, reachable, error, schemaVersion, generatedAt, doctorCommit
        case expected, checks
    }

    /// A host document decodes field by field,  for the same reason a row does:  one
    /// field of an unexpected type costs that field and not the host.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        host = (try? container.decodeIfPresent(String.self, forKey: .host)) ?? ""
        reachable = (try? container.decodeIfPresent(Bool.self, forKey: .reachable)) ?? false
        error = try? container.decodeIfPresent(String.self, forKey: .error)
        schemaVersion = try? container.decodeIfPresent(Int.self, forKey: .schemaVersion)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        doctorCommit = try? container.decodeIfPresent(String.self, forKey: .doctorCommit)
        expected = try? container.decodeIfPresent(FleetToolsExpected.self, forKey: .expected)
        checks = (try? container.decodeIfPresent([FleetToolsRow].self, forKey: .checks)) ?? []
    }
}

/// The report,  as the doctor writes it.
struct FleetToolsReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let hosts: [FleetToolsHostDocument]

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, hosts
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate:  a report that does not carry them is
        // not a report this app can judge.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        hosts = (try? container.decodeIfPresent([FleetToolsHostDocument].self, forKey: .hosts)) ?? []
    }
}

// MARK: - The verdicts

/// What Muster can say about one host,  from the report.
///
/// The state is the app's own vocabulary (`CellState`),  so a host reads like every
/// other cell in the app:  a failure is red,  a host that is behind is amber,  and a
/// host nobody could prove reads as unproven rather than as a red alarm.
struct FleetToolsHostVerdict: Identifiable, Equatable {
    let host: String
    /// Whether the doctor could read the host at all.
    let reachable: Bool
    /// The state the host reads as.
    let state: CellState
    /// The rows that failed.
    let failing: Int
    /// The rows the doctor could not tell.
    let unknown: Int
    /// The rows that are behind.
    let stale: Int
    /// The rows that are current.
    let current: Int
    /// The optional things that are not installed,  which are information.
    let absent: Int
    /// The rows worth looking at,  worst first.
    let notableRows: [FleetToolsRow]
    /// Why the host could not be read,  when it could not be.
    let reason: String?
    /// When this host's own half of the report was written.
    let generatedAt: Date?
    /// How old that half is,  in the app's own words.
    let age: String?
    /// The tip the doctor compared against,  and how it read it.
    let expected: FleetToolsExpected?
    /// The doctor's own commit.
    let doctorCommit: String?

    var id: String { host }

    /// The tip,  short.
    var tip: String? {
        guard let rev = expected?.rev, !rev.isEmpty else { return nil }
        return String(rev.prefix(7))
    }

    /// How the tip was read.
    var tipSource: String? {
        guard let source = expected?.source, !source.isEmpty else { return nil }
        return source
    }

    /// The doctor's own commit,  short.
    var doctor: String? {
        guard let commit = doctorCommit, !commit.isEmpty else { return nil }
        return String(commit.prefix(7))
    }
}

/// Everything the panel shows,  from one read of the report.
struct FleetToolsReading {
    /// Where the report is.
    var path: String = ""
    /// When the report says it was written.
    var generatedAt: Date?
    /// How old the report is,  in the app's own words.
    var age: String?
    /// Whether the report is inside the window the doctor runs in.
    var isCurrent: Bool = false
    /// Why the report could not be read at all,  when it could not be.
    var problem: String?
    /// The hosts,  in the report's own order.
    var hosts: [FleetToolsHostVerdict] = []
    /// The verdict for the whole report.
    var verdict: CellState = .unknown
}

// MARK: - The reader

/// Reads the doctor's report and says what it means.
enum FleetToolsReportReader {
    /// Where the doctor's timer writes the report.
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("report.json", isDirectory: false)
    }

    /// Read the report at a path and say what it means.
    ///
    /// Every way of not knowing ends the same way:  the reading carries a problem and
    /// no host rows,  so nothing on screen can be read as a pass.
    static func read(at url: URL, now: Date = Date()) -> FleetToolsReading {
        var reading = FleetToolsReading()
        reading.path = url.path

        let data: Data
        if FileManager.default.fileExists(atPath: url.path) {
            do {
                data = try Data(contentsOf: url)
            } catch {
                reading.problem = "The report could not be read:  \(error.localizedDescription)"
                return reading
            }
        } else {
            reading.problem = "The report could not be read:  the file is not there"
            return reading
        }

        let report: FleetToolsReport
        do {
            report = try JSONDecoder().decode(FleetToolsReport.self, from: data)
        } catch {
            reading.problem = "The report could not be read:  \(error.localizedDescription)"
            return reading
        }

        guard report.schemaVersion == FleetToolsContract.supportedSchemaVersion else {
            reading.problem = "The report is version \(report.schemaVersion);  this app reads version \(FleetToolsContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == FleetToolsContract.expectedTool else {
            reading.problem = "The report came from \"\(report.tool)\";  this app reads \"\(FleetToolsContract.expectedTool)\"."
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem = "The report carries no time this app can read,  so nothing in it is current."
            return reading
        }

        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem = "The report is dated in the future,  so nothing in it is current."
            return reading
        }
        reading.isCurrent = seconds <= FleetToolsContract.maxAgeSeconds
        reading.hosts = report.hosts.map { host in
            verdict(for: host, reportIsCurrent: reading.isCurrent, now: now)
        }
        // A report out of its window is not a reading:  the whole report reads unknown,
        // even where every row in it looks fine.
        reading.verdict = reading.isCurrent ? overall(of: reading.hosts) : .unknown
        return reading
    }

    /// Read the report the doctor writes for this user.
    static func readDefault(now: Date = Date()) -> FleetToolsReading {
        read(at: reportURL(), now: now)
    }

    /// What one host's half of the report says.
    ///
    /// A host the doctor could not reach is unknown with the doctor's own reason:  the
    /// rows in an unreachable host are the placeholders the doctor writes for every
    /// check it would have made,  and counting them would report observations that
    /// were never made.
    ///
    /// The host's own age is information and not a verdict:  the server's doctor runs
    /// on an hourly timer,  so its half is routinely older than the Mac's report and
    /// that is the server working as it should.
    private static func verdict(
        for host: FleetToolsHostDocument,
        reportIsCurrent: Bool,
        now: Date
    ) -> FleetToolsHostVerdict {
        let rows = host.reachable ? host.checks.filter { $0.countsInVerdict } : []
        var failing = 0
        var unknown = 0
        var stale = 0
        var current = 0
        var absent = 0
        for row in rows {
            switch row.outcome {
            case .failing: failing += 1
            case .unknown: unknown += 1
            case .stale: stale += 1
            case .passing: current += 1
            case .absent: absent += 1
            }
        }
        let notable = rows
            .filter { $0.outcome != .passing && $0.outcome != .absent }
            .sorted(by: notableFirst)

        let state: CellState
        if !reportIsCurrent {
            // A report that is out of its window proves nothing about either host.
            state = .unproven
        } else if !host.reachable {
            state = .unproven
        } else if failing > 0 {
            state = .missing
        } else if stale > 0 {
            state = .drift
        } else if unknown > 0 {
            state = .unproven
        } else {
            state = .passed
        }

        let hostGenerated = host.generatedAt.flatMap(FleetTime.parse)
        return FleetToolsHostVerdict(
            host: host.host,
            reachable: host.reachable,
            state: state,
            failing: failing,
            unknown: unknown,
            stale: stale,
            current: current,
            absent: absent,
            notableRows: notable,
            reason: host.reachable ? nil : (host.error ?? "the doctor could not reach this host"),
            generatedAt: hostGenerated,
            age: hostGenerated == nil ? nil : FleetTime.ageText(since: hostGenerated, now: now),
            expected: host.expected,
            doctorCommit: host.doctorCommit
        )
    }

    /// The worst host's state,  which is the report's own state.
    private static func overall(of hosts: [FleetToolsHostVerdict]) -> CellState {
        guard !hosts.isEmpty else { return .unknown }
        var worst = CellState.passed
        for host in hosts {
            if GridBuilder.severityIndex(of: host.state) < GridBuilder.severityIndex(of: worst) {
                worst = host.state
            }
        }
        return worst
    }

    /// The rows worst first:  the failures,  then the unknown,  then the stale.
    private static func notableFirst(_ left: FleetToolsRow, _ right: FleetToolsRow) -> Bool {
        let rank = outcomeRank(left.outcome) - outcomeRank(right.outcome)
        if rank != 0 { return rank < 0 }
        return left.check < right.check
    }

    private static func outcomeRank(_ outcome: FleetToolsRowOutcome) -> Int {
        switch outcome {
        case .failing: return 0
        case .unknown: return 1
        case .stale: return 2
        case .absent: return 3
        case .passing: return 4
        }
    }
}
