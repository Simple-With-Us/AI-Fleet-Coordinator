import Foundation

// The Mac's LaunchAgents,  as Muster reads them.
//
// The file is `~/.local/state/fleet-tools/mac-services.json`:  the Mac's
// `com.jay.fleet-mac-services` agent runs `scripts/fleet-mac-services.py` every five
// minutes,  which lists every agent the fleet expects (each `scripts/launchd/*.plist`
// label and each `mac.agents` label in `scripts/fleet-ops-control.json`) and what
// `launchctl print` says about it.
//
// The rule is the doctor's rule:  a file that cannot be trusted reads as unknown,  never
// as a pass.  Nothing here runs `launchctl` itself:  the file is one local file the
// collector already wrote.

// MARK: - The contract

/// The pinned contract for the collector's file.
enum MacServicesContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-mac-services"

    /// The collector runs every five minutes,  so three of its runs are the window a file
    /// is current in.
    static let maxAgeSeconds = 15 * 60
}

// MARK: - The rows

/// One LaunchAgent,  as the collector saw it.
struct MacAgent: Decodable, Identifiable, Equatable {
    let label: String
    /// Whether the repository carries a plist for it.
    let plistInRepo: Bool
    /// Whether `~/Library/LaunchAgents/<label>.plist` is there.
    let installed: Bool
    /// Whether launchd has it loaded.
    let loaded: Bool
    let pid: Int?
    let lastExitStatus: Int?
    /// Whether the allowlist lets Muster start,  stop or restart it.
    let controllable: Bool
    /// Why the collector could not read this agent (`timeout` when its `launchctl print`
    /// timed out),  or nil when it read it.  A value that is not text reads as
    /// `unreadable`:  the key's presence alone says the agent was not observed.
    let error: String?

    enum CodingKeys: String, CodingKey {
        case label, plistInRepo, installed, loaded, pid, lastExitStatus, controllable, error
    }

    /// An agent decodes field by field.  A flag that is missing or unreadable is false:
    /// an agent the collector could not prove loaded is not a loaded one,  and a missing
    /// `controllable` is not a permission.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        label = (try? container.decodeIfPresent(String.self, forKey: .label)) ?? ""
        plistInRepo = (try? container.decodeIfPresent(Bool.self, forKey: .plistInRepo)) == true
        installed = (try? container.decodeIfPresent(Bool.self, forKey: .installed)) == true
        loaded = (try? container.decodeIfPresent(Bool.self, forKey: .loaded)) == true
        pid = try? container.decodeIfPresent(Int.self, forKey: .pid)
        lastExitStatus = try? container.decodeIfPresent(Int.self, forKey: .lastExitStatus)
        controllable = (try? container.decodeIfPresent(Bool.self, forKey: .controllable)) == true
        error = MacServicesReport.decodeError(container, forKey: .error)
    }

    /// True when the collector read this agent's launchd state,  so `loaded` is an
    /// observation and not the collector's fallback guess.
    var isObserved: Bool { error == nil }

    var id: String { label }

    /// The state the agent reads as.
    ///
    /// A loaded agent whose last run exited cleanly is a pass,  a loaded agent whose last
    /// run failed is drift,  an installed agent that is not loaded is drift,  and an
    /// expected agent that is not installed is unproven:  some agents are installed by
    /// hand and only on the Mac that needs them.  An agent the collector could not read
    /// is unproven,  whatever its flags say:  its `loaded` is a guess.
    var state: CellState {
        guard isObserved else { return .unproven }
        if loaded {
            if let status = lastExitStatus, status != 0 { return .drift }
            return .passed
        }
        if installed { return .drift }
        return .unproven
    }

    /// What the agent is doing,  in a few words.
    var statusText: String {
        if let error = error {
            return error == "timeout" ? "launchctl timed out" : "launchctl error: \(error)"
        }
        if loaded {
            if let pid = pid { return "running, pid \(pid)" }
            if let status = lastExitStatus { return "loaded, last exit \(status)" }
            return "loaded"
        }
        return installed ? "installed, not loaded" : "not installed"
    }
}

/// The file,  as the collector writes it.
struct MacServicesReport: Decodable {
    let schemaVersion: Int
    let tool: String
    let generatedAt: String?
    let host: String?
    let agents: [MacAgent]
    /// Why the collector could not read launchd at all (`launchctl_unavailable`),  or nil.
    /// When it is set,  every agent's `loaded` is a guess and not an observation.
    let error: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, generatedAt, host, agents, error
    }

    /// The `error` key of a report or an agent:  its text,  `unreadable` when it is there
    /// and not text,  or nil when it is missing or null.
    static func decodeError<Key: CodingKey>(_ container: KeyedDecodingContainer<Key>, forKey key: Key) -> String? {
        guard container.contains(key), (try? container.decodeNil(forKey: key)) == false else { return nil }
        return (try? container.decode(String.self, forKey: key)) ?? "unreadable"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        tool = try container.decode(String.self, forKey: .tool)
        generatedAt = try? container.decodeIfPresent(String.self, forKey: .generatedAt)
        host = try? container.decodeIfPresent(String.self, forKey: .host)
        agents = (try? container.decodeIfPresent([MacAgent].self, forKey: .agents)) ?? []
        error = MacServicesReport.decodeError(container, forKey: .error)
    }
}

// MARK: - The reading

/// Everything the Operations tab shows about the Mac's agents,  from one read.
struct MacServicesReading {
    var path: String = ""
    var generatedAt: Date?
    var age: String?
    var isCurrent: Bool = false
    var problem: String?
    /// The agents,  in the file's own order.
    var agents: [MacAgent] = []
    /// The verdict for the whole file.
    var state: CellState = .unknown

    /// How many expected agents are loaded,  counting only agents the collector read.
    var loadedCount: Int { agents.filter { $0.isObserved && $0.loaded }.count }

    /// How many agents the collector could not read.
    var unreadCount: Int { agents.filter { !$0.isObserved }.count }
}

// MARK: - The reader

enum MacServicesReportReader {
    /// Where the collector writes the file.
    static func reportURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("mac-services.json", isDirectory: false)
    }

    /// Read the file at a path and say what it means.
    static func read(at url: URL, now: Date = Date()) -> MacServicesReading {
        var reading = MacServicesReading()
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

        let report: MacServicesReport
        do {
            report = try JSONDecoder().decode(MacServicesReport.self, from: data)
        } catch {
            reading.problem = "The file could not be read:\(gap)\(error.localizedDescription)"
            return reading
        }

        guard report.schemaVersion == MacServicesContract.supportedSchemaVersion else {
            reading.problem =
                "The file is version \(report.schemaVersion);\(gap)this app reads version \(MacServicesContract.supportedSchemaVersion)."
            return reading
        }
        guard report.tool == MacServicesContract.expectedTool else {
            reading.problem =
                "The file came from \"\(report.tool)\";\(gap)this app reads \"\(MacServicesContract.expectedTool)\"."
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
        reading.isCurrent = seconds <= MacServicesContract.maxAgeSeconds
        if let error = report.error {
            // launchctl could not be run,  so no agent's state was observed:  the rows are
            // the collector's guesses,  and a guess is not shown as a reading.
            let cause = error == "launchctl_unavailable" ? "launchctl could not be run" : "the collector said \"\(error)\""
            reading.problem =
                "The collector could not read launchd (\(cause)),\(gap)so no agent's state was observed."
            return reading
        }
        reading.agents = report.agents
        reading.state = overall(of: reading)
        return reading
    }

    /// Read the file the collector writes for this user.
    static func readDefault(now: Date = Date()) -> MacServicesReading {
        read(at: reportURL(), now: now)
    }

    /// The worst agent's state,  or unknown when the file is out of its window or empty.
    private static func overall(of reading: MacServicesReading) -> CellState {
        guard reading.isCurrent, !reading.agents.isEmpty else { return .unknown }
        var worst = CellState.passed
        for agent in reading.agents
        where GridBuilder.severityIndex(of: agent.state) < GridBuilder.severityIndex(of: worst) {
            worst = agent.state
        }
        return worst
    }
}
