import Foundation

// The fleet apps' processes on the Mac and on the server,  as Muster reads them (AFC-576).
//
// The file is `~/.local/state/fleet-tools/app-processes.json`.  The fleet tools doctor
// writes it beside its own report every fifteen minutes:  it scans the Mac itself,  and it
// carries the server's scan over the read-only `fleet-tools-ro` route it already uses
// (`scripts/fleet_app_processes.py` says how a process is matched to an app).
//
// The rule this file follows is the doctor's rule:  a report that cannot be trusted reads
// as not known,  never as running.  A missing file,  a malformed file,  a version this app
// does not read and a report older than forty-five minutes all read as not known.  A host
// the doctor could not reach reads as not known with the doctor's own reason,  and an app
// on a host whose read was only partly made reads as not known rather than as having no
// process,  because the part that was not read could have held its processes.
//
// A row carries the app,  pid,  executable,  launchd label,  pm2 name or systemd unit,  state
// and start time,  and nothing else:  the doctor never reads a process's argv or environment.
//
// Nothing here reaches the network,  runs anything or reads a credential.  There is no
// SwiftUI here,  so the test bundle compiles it.

// MARK: - The contract

/// The pinned contract for the app-process report.
enum AppProcessesContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-app-processes"

    /// The doctor runs every fifteen minutes,  so three of its runs are the window a
    /// report is current in,  the same window as the doctor's own report.
    static let maxAgeSeconds = 45 * 60

    /// The largest file the reader will load.  The doctor keeps each host's half under
    /// 128 KiB,  so a bigger file did not come from it.
    static let maxReportBytes = 1024 * 1024

    static let fileName = "app-processes.json"
}

// MARK: - The rows

/// One process the doctor matched to a fleet app.
struct AppProcessRow: Decodable, Equatable {
    let app: String
    let pid: Int?
    let executable: String?
    let launchdLabel: String?
    let pm2Name: String?
    let systemdUnit: String?
    let state: String?
    let startedAt: String?
    let matchedBy: String?

    enum CodingKeys: String, CodingKey {
        case app, pid, executable, launchdLabel, pm2Name, systemdUnit, state, startedAt, matchedBy
    }

    /// A row decodes field by field,  so one field of a type this app did not expect costs
    /// that field and not the whole report.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        app = (try? container.decodeIfPresent(String.self, forKey: .app)) ?? ""
        pid = try? container.decodeIfPresent(Int.self, forKey: .pid)
        executable = try? container.decodeIfPresent(String.self, forKey: .executable)
        launchdLabel = try? container.decodeIfPresent(String.self, forKey: .launchdLabel)
        pm2Name = try? container.decodeIfPresent(String.self, forKey: .pm2Name)
        systemdUnit = try? container.decodeIfPresent(String.self, forKey: .systemdUnit)
        state = try? container.decodeIfPresent(String.self, forKey: .state)
        startedAt = try? container.decodeIfPresent(String.self, forKey: .startedAt)
        matchedBy = try? container.decodeIfPresent(String.self, forKey: .matchedBy)
    }

    /// The name the list shows:  the service's own name when it has one,  else the
    /// executable's file name.
    var name: String {
        if let pm2Name, !pm2Name.isEmpty { return pm2Name }
        if let launchdLabel, !launchdLabel.isEmpty { return launchdLabel }
        if let systemdUnit, !systemdUnit.isEmpty { return systemdUnit }
        if let executable, !executable.isEmpty {
            return executable.split(separator: "/").last.map(String.init) ?? executable
        }
        return "unnamed process"
    }

    /// The state in words.  A state this app does not know reads as not known.
    var stateWords: String {
        switch state {
        case "running": return "Running"
        case "sleeping": return "Sleeping"
        case "idle": return "Idle"
        case "waiting": return "Waiting"
        case "stopped": return "Stopped"
        case "zombie": return "Exited, Not Reaped"
        case "dead": return "Dead"
        default: return "State Not Known"
        }
    }

    /// True when the state says the process is not doing anything any more.
    var isStoppedOrGone: Bool {
        state == "stopped" || state == "zombie" || state == "dead"
    }

    /// How long the process has been up,  when its start time could be read.
    func uptimeText(now: Date = Date()) -> String? {
        guard let startedAt, let date = FleetTime.parse(startedAt) else { return nil }
        let seconds = Int(now.timeIntervalSince(date))
        guard seconds >= 0 else { return nil }
        return FleetTime.age(seconds)
    }
}

/// One fleet app as the report names it.
struct AppProcessesApp: Decodable, Equatable {
    let acronym: String
    let displayName: String?

    enum CodingKeys: String, CodingKey {
        case acronym, displayName
    }

    init(acronym: String, displayName: String?) {
        self.acronym = acronym
        self.displayName = displayName
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        acronym = (try? container.decodeIfPresent(String.self, forKey: .acronym)) ?? ""
        displayName = try? container.decodeIfPresent(String.self, forKey: .displayName)
    }
}

/// One host's half of the report.
struct AppProcessesHostDocument: Decodable, Equatable {
    let host: String
    let reachable: Bool
    let error: String?
    let generatedAt: String?
    let complete: Bool
    let truncated: Bool
    let notes: [String]
    let processes: [AppProcessRow]

    enum CodingKeys: String, CodingKey {
        case host, reachable, error, generatedAt, complete, truncated, notes, processes
    }

    /// A host decodes field by field.  A field that is missing or of another type reads
    /// as the cautious answer:  not reachable,  not complete.
    ///
    /// The process list decodes entry by entry (Kody, PR 1463):  one entry this app cannot
    /// read costs that entry,  and the host is then not complete,  so its apps with no row
    /// read Not Known rather than No Process Seen.  A list that is missing or is not a list
    /// makes the host not complete for the same reason.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        host = (try? container.decodeIfPresent(String.self, forKey: .host)) ?? ""
        reachable = (try? container.decodeIfPresent(Bool.self, forKey: .reachable)) ?? false
        error = try? container.decodeIfPresent(String.self, forKey: .error)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        let saysComplete = (try? container.decodeIfPresent(Bool.self, forKey: .complete)) ?? false
        let cut = try? container.decodeIfPresent(Bool.self, forKey: .truncated)
        truncated = cut == true
        notes = Self.decodeEach(String.self, in: container, forKey: .notes).values
        let rows = Self.decodeEach(AppProcessRow.self, in: container, forKey: .processes)
        let kept = rows.values.filter { !$0.app.isEmpty }
        processes = kept
        // Only a readable `false` says the list was not cut.
        complete = saysComplete && cut == false && rows.wholeList && kept.count == rows.values.count
    }

    /// Every entry of a list that decodes,  and whether the list was there and every entry
    /// in it decoded.  A non-list or an absent key gives no values and `wholeList` false.
    static func decodeEach<Value: Decodable>(
        _ type: Value.Type,
        in container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys
    ) -> (values: [Value], wholeList: Bool) {
        guard var list = try? container.nestedUnkeyedContainer(forKey: key) else {
            return ([], false)
        }
        var values: [Value] = []
        var dropped = false
        while !list.isAtEnd {
            // `superDecoder()` consumes the entry whether or not it decodes,  so a bad entry
            // never stops the loop.
            guard let entry = try? list.superDecoder() else {
                dropped = true
                break
            }
            if let value = try? Value(from: entry) {
                values.append(value)
            } else {
                dropped = true
            }
        }
        return (values, !dropped)
    }
}

/// The report,  as the doctor writes it.
struct AppProcessesReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let apps: [AppProcessesApp]
    let hosts: [AppProcessesHostDocument]

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, apps, hosts
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The version and the tool are the gate:  a report without them is not one this
        // app can judge.
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        apps = (try? container.decodeIfPresent([AppProcessesApp].self, forKey: .apps)) ?? []
        hosts = (try? container.decodeIfPresent([AppProcessesHostDocument].self, forKey: .hosts)) ?? []
    }
}

// MARK: - What the panel says

/// What Muster can say about one app on one host.
enum AppProcessesAppState: Equatable {
    /// The doctor saw this many of the app's processes.
    case seen(Int)
    /// The host was read in full and none of the app's processes were there.  This is
    /// information,  not a fault:  many apps run no process on a given machine.
    case noProcessSeen
    /// The report cannot say:  it is old,  the host was not reached,  or part of the
    /// read failed.
    case notKnown

    /// The state in words,  which come first;  colour only repeats them.
    var words: String {
        switch self {
        case .seen(let count): return count == 1 ? "1 Process Seen" : "\(count) Processes Seen"
        case .noProcessSeen: return "No Process Seen"
        case .notKnown: return "Not Known"
        }
    }

    /// True when at least one process was seen.
    var isSeen: Bool {
        if case .seen = self { return true }
        return false
    }
}

/// What Muster can say about one host.
enum AppProcessesHostState: Equatable {
    /// The host was read in full,  inside the report's window.
    case read
    /// The host was reached,  but part of the read failed or the list was cut.
    case partlyRead
    /// The doctor could not read the host,  for this reason.
    case notReached(String)
    /// The report is out of its window,  so nothing in it is current.
    case reportOld

    var words: String {
        switch self {
        case .read: return "Read"
        case .partlyRead: return "Partly Read"
        case .notReached: return "Not Known:  Not Reached"
        case .reportOld: return "Not Known:  Report Is Old"
        }
    }

    /// True when the host's absences can be believed:  only a full, current read.
    var absencesAreKnown: Bool { self == .read }

    /// True when the host's rows can be shown at all.
    var showsRows: Bool { self == .read || self == .partlyRead }
}

/// One app on one host,  as the panel shows it.
struct AppProcessesAppReading: Identifiable, Equatable {
    let acronym: String
    let displayName: String
    let state: AppProcessesAppState
    /// The processes seen,  in pid order.  Empty unless the host's rows can be shown.
    let processes: [AppProcessRow]

    var id: String { acronym }

    /// The name with its acronym,  as the app list elsewhere in Muster writes it.
    var title: String {
        displayName == acronym ? acronym : "\(displayName) (\(acronym))"
    }
}

/// One host,  as the panel shows it.
struct AppProcessesHostReading: Identifiable, Equatable {
    /// The host's word in the report.
    let hostWord: String
    let state: AppProcessesHostState
    /// When the host's half was written,  when it says.
    let generatedAt: Date?
    /// The doctor's own notes on what it could not read.
    let notes: [String]
    let apps: [AppProcessesAppReading]

    var id: String { hostWord }

    /// The host's heading:  The Mac,  The Server,  or the report's own word.
    var heading: String { FleetHost(reportValue: hostWord)?.heading ?? hostWord }

    /// How many of the fleet's apps had a process seen here.
    var appsSeen: Int {
        apps.filter { $0.state.isSeen }.count
    }

    /// The notes,  each identified by its place in the list (Kody, PR 1463):  two notes with
    /// the same words are still two rows,  so the list's identities never collide.
    var numberedNotes: [AppProcessesNote] {
        notes.enumerated().map { AppProcessesNote(id: $0.offset, text: $0.element) }
    }
}

/// One of the doctor's notes about a host,  identified by its position.
struct AppProcessesNote: Identifiable, Equatable {
    let id: Int
    let text: String
}

/// Everything the panel shows,  from one read of the report.
struct AppProcessesReading: Equatable {
    var path: String = ""
    var generatedAt: Date?
    var age: String?
    var isCurrent: Bool = false
    /// Why the report could not be read at all,  when it could not be.
    var problem: String?
    var hosts: [AppProcessesHostReading] = []
}

// MARK: - The reader

enum AppProcessesReportReader {
    /// Where the doctor writes the report:  beside its own `report.json`.
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent(AppProcessesContract.fileName, isDirectory: false)
    }

    /// Read the report the doctor writes for this user.
    static func readDefault(now: Date = Date()) -> AppProcessesReading {
        read(at: reportURL(), now: now)
    }

    /// Read the report at a path and say what it means.
    ///
    /// The size is checked twice (Kody, PR 1463):  once from the file's attributes,  and
    /// again on the bytes actually loaded,  so a file that grew or was replaced between the
    /// two is still refused.  The default loader reads at most one byte past the cap,  so an
    /// oversized file is never loaded whole.
    static func read(
        at url: URL,
        now: Date = Date(),
        load: (URL) throws -> Data = AppProcessesReportReader.boundedData(at:)
    ) -> AppProcessesReading {
        let gap = ServerStatusCopy.sentenceGap
        var reading = AppProcessesReading()
        reading.path = url.path
        guard FileManager.default.fileExists(atPath: url.path) else {
            reading.problem = "The report is not on the Mac yet.\(gap)The fleet tools doctor writes it beside its own report on its next run."
            return reading
        }
        if let size = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? NSNumber,
            let problem = sizeProblem(size.intValue)
        {
            reading.problem = problem
            return reading
        }
        let data: Data
        do {
            data = try load(url)
        } catch {
            reading.problem = "The report could not be read:  \(error.localizedDescription)"
            return reading
        }
        if let problem = sizeProblem(data.count) {
            reading.problem = problem
            return reading
        }
        return parse(data, path: url.path, now: now)
    }

    /// Why a report of this many bytes is refused,  or nil when it is within the cap.
    static func sizeProblem(_ bytes: Int) -> String? {
        guard bytes > AppProcessesContract.maxReportBytes else { return nil }
        return "The report is larger than the \(AppProcessesContract.maxReportBytes) bytes this app reads, so it did not come from the doctor."
    }

    /// The file's bytes,  at most one past the cap:  enough to know it is too big.
    static func boundedData(at url: URL) throws -> Data {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        return try handle.read(upToCount: AppProcessesContract.maxReportBytes + 1) ?? Data()
    }

    /// What the report's bytes say.
    ///
    /// Every way of not knowing ends the same way:  the reading carries a problem and no
    /// hosts,  so nothing on screen can be read as running.
    static func parse(_ data: Data, path: String = "", now: Date = Date()) -> AppProcessesReading {
        let gap = ServerStatusCopy.sentenceGap
        var reading = AppProcessesReading()
        reading.path = path
        let report: AppProcessesReport
        do {
            report = try JSONDecoder().decode(AppProcessesReport.self, from: data)
        } catch {
            reading.problem = "The report could not be read:  \(error.localizedDescription)"
            return reading
        }
        guard report.schemaVersion == AppProcessesContract.supportedSchemaVersion else {
            reading.problem = "The report is version \(report.schemaVersion);  this app reads version \(AppProcessesContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == AppProcessesContract.expectedTool else {
            reading.problem = "The report came from \"\(report.tool)\";  this app reads \"\(AppProcessesContract.expectedTool)\"."
            return reading
        }
        guard let generated = report.generatedAt.flatMap(FleetTime.parse) else {
            reading.problem = "The report carries no time this app can read,\(gap)so nothing in it is current."
            return reading
        }
        reading.generatedAt = generated
        reading.age = FleetTime.ageText(since: generated, now: now)
        let seconds = Int(now.timeIntervalSince(generated))
        if seconds < -60 {
            reading.problem = "The report is dated in the future,\(gap)so nothing in it is current."
            return reading
        }
        reading.isCurrent = seconds <= AppProcessesContract.maxAgeSeconds
        let apps = appList(report)
        reading.hosts = report.hosts.map { host($0, apps: apps, reportIsCurrent: reading.isCurrent) }
        return reading
    }

    /// The apps to list:  the report's own list from `fleet-apps.json`,  then any app a row
    /// names that the list does not,  so a process is never dropped for want of a name.
    static func appList(_ report: AppProcessesReport) -> [AppProcessesApp] {
        var apps = report.apps.filter { !$0.acronym.isEmpty }
        var known = Set(apps.map(\.acronym))
        for host in report.hosts {
            for row in host.processes where !row.app.isEmpty && !known.contains(row.app) {
                apps.append(AppProcessesApp(acronym: row.app, displayName: nil))
                known.insert(row.app)
            }
        }
        return apps
    }

    /// What one host's half says,  app by app.
    static func host(
        _ document: AppProcessesHostDocument,
        apps: [AppProcessesApp],
        reportIsCurrent: Bool
    ) -> AppProcessesHostReading {
        let state: AppProcessesHostState
        if !reportIsCurrent {
            state = .reportOld
        } else if !document.reachable {
            state = .notReached(document.error ?? "the doctor could not read this host")
        } else if !document.complete || document.truncated {
            state = .partlyRead
        } else {
            state = .read
        }
        let readings = apps.map { app -> AppProcessesAppReading in
            var rows: [AppProcessRow] = []
            if state.showsRows {
                rows = document.processes
                    .filter { $0.app == app.acronym }
                    .sorted { ($0.pid ?? 0) < ($1.pid ?? 0) }
            }
            let appState: AppProcessesAppState
            if !rows.isEmpty {
                appState = .seen(rows.count)
            } else if state.absencesAreKnown {
                appState = .noProcessSeen
            } else {
                appState = .notKnown
            }
            let name = app.displayName.flatMap { $0.isEmpty ? nil : $0 } ?? app.acronym
            return AppProcessesAppReading(acronym: app.acronym, displayName: name, state: appState, processes: rows)
        }
        return AppProcessesHostReading(
            hostWord: document.host,
            state: state,
            generatedAt: document.generatedAt.flatMap(FleetTime.parse),
            notes: document.notes,
            apps: readings
        )
    }
}
