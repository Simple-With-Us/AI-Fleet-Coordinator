import Foundation

/// The health the BotFleet MCP server reports about itself.
public struct McpServerHealth: Decodable, Equatable {
    public let status: String
    public let app: String
    public let tools: Int?
}

/// Why the Control app could not manage the BotFleet MCP server.
public enum McpServerError: LocalizedError, Equatable {
    case managerMissing
    case refused(String)

    public var errorDescription: String? {
        switch self {
        case .managerMissing:
            return "The BotFleet MCP Server manager is not installed on this Mac."
        case .refused(let message):
            return message
        }
    }
}

/// One pm2 application, as `pm2 jlist` reports it.
struct PM2Application: Decodable {
    let name: String
    /// The state pm2 has for the application.
    let status: String?
    /// The process pm2 has behind the application, zero when it has none.
    let pid: Int

    private enum CodingKeys: String, CodingKey {
        case name
        case pid
        case environment = "pm2_env"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        let environment = try? container.decode(PM2Environment.self, forKey: .environment)
        status = environment?.status
        // pm2 carries the process id at the entry level of its listing, and the
        // nested environment may keep the id of a process that is long gone,
        // so the entry decides whenever it has the key:  an explicit zero there
        // means no process.  A missing entry key falls back to the nested
        // value, so a shape that moves the field cannot make every running
        // application read as failed.
        if let entry = try? container.decode(Int.self, forKey: .pid) {
            pid = entry
        } else {
            pid = environment?.pid ?? 0
        }
    }
}

/// The part of a pm2 application the Control app reads.
private struct PM2Environment: Decodable {
    let status: String?
    let pid: Int?

    private enum CodingKeys: String, CodingKey {
        case status
        case pid
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try? container.decode(String.self, forKey: .status)
        pid = try? container.decode(Int.self, forKey: .pid)
    }
}

/// One look at pm2:  the state of every application it knows about, and the
/// applications whose record is stale.
///
/// Both come from one listing, so a caller that needs both pays for one
/// process rather than two.
public struct PM2Snapshot {
    /// The state of every application pm2 knows about, by name.
    public let states: [String: ManagedServiceState]
    /// The applications whose record says online with no process behind them.
    ///
    /// These are the stale records:  pm2 still remembers the application and
    /// still calls it online, but there is no pid, so nothing is running.
    public let stale: Set<String>

    public init(states: [String: ManagedServiceState], stale: Set<String>) {
        self.states = states
        self.stale = stale
    }
}

/// Runs the pm2 client that owns the BotFleet MCP server.
///
/// The MCP server is a long-running pm2 job, so pm2 is the only thing that may
/// start or stop it.  The Control app never spawns a second copy.
public struct PM2Client {
    /// The pm2 application name for the BotFleet MCP server.
    public static let mcpAppName = "botfleet-mcp"
    /// The pm2 ecosystem file that defines it.
    public static let ecosystemPath = "apps/pm2-ecosystem.config.cjs"

    /// How long a pm2 change is waited for, and how often it is looked at.
    ///
    /// Every look at a pm2 application spawns a node client, so the wait is
    /// made of a few wide looks rather than many narrow ones.  The window is
    /// the same twenty seconds the narrow looks gave, with a quarter of the
    /// processes.
    public static let settleAttempts = 8
    public static let settleInterval: TimeInterval = 2.5

    /// How long the MCP server's own health is waited for.
    ///
    /// The MCP server is observed through an HTTP health endpoint, so a look
    /// at it costs no process, and its window stays the twenty seconds it
    /// always was.  The window is a wall-clock one, because a look that
    /// hangs costs its whole request timeout: a count of looks would let
    /// the menu bar app sit busy for far longer than twenty seconds.
    public static let mcpSettleWindow: TimeInterval = 20

    /// How long the MCP server is given to answer after a start, before the
    /// start is treated as having done nothing.
    ///
    /// The server is a local process that answers its own health endpoint
    /// within a second or two, so a start that has not brought it up by now
    /// did not bring it up at all.  The window is short so a start on a stale
    /// record, which starts nothing, is repaired without a long wait.
    public static let mcpStartProbeWindow: TimeInterval = 5

    private let commands: CommandRunning
    private let environment: [String: String]
    private let nodePath: String?
    private let pm2Path: String?
    private let home: URL

    public init(commands: CommandRunning = ProcessCommandRunner(),
                environment: [String: String] = ProcessInfo.processInfo.environment,
                nodePath: String? = nil,
                pm2Path: String? = nil,
                home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.commands = commands
        self.environment = environment
        self.home = home
        // A nil path means "find it".  An empty path means it is not there.
        self.nodePath = nodePath ?? PM2Client.discoverNode()
        self.pm2Path = pm2Path ?? PM2Client.discoverPm2()
    }

    public var isAvailable: Bool {
        guard let nodePath, let pm2Path else { return false }
        return !nodePath.isEmpty && !pm2Path.isEmpty
    }

    /// Runs pm2 with an argument list.
    public func run(_ arguments: [String], timeout: TimeInterval = 60) throws -> String {
        guard let nodePath, let pm2Path, !nodePath.isEmpty, !pm2Path.isEmpty else {
            throw McpServerError.managerMissing
        }
        return try commands.run(nodePath, [pm2Path] + arguments,
                                 environment: PM2Client.pm2Environment(environment, home: home),
                                 timeout: timeout)
    }

    /// The environment pm2 needs to find its own daemon and its home.
    static func pm2Environment(_ environment: [String: String], home: URL) -> [String: String] {
        var out = environment
        out["HOME"] = home.path
        out["PM2_HOME"] = environment["PM2_HOME"] ?? home.appendingPathComponent(".pm2").path
        if out["PATH"] == nil || out["PATH"]?.isEmpty == true {
            out["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        }
        return out
    }

    /// The node that runs pm2.  A menu bar app inherits no shell PATH, so the
    /// path has to be absolute.
    public static func discoverNode() -> String? {
        ["/opt/homebrew/bin/node", "/usr/local/bin/node"].first { isExecutable($0) }
    }

    /// The pm2 client script.
    public static func discoverPm2() -> String? {
        ["/opt/homebrew/lib/node_modules/pm2/bin/pm2",
         "/usr/local/lib/node_modules/pm2/bin/pm2"].first { isExecutable($0) }
    }

    /// The state of every pm2 application pm2 knows about, by name.
    ///
    /// `nil` means pm2 could not be asked at all, so nothing is known about
    /// any application.  That is not the same as an empty answer, which
    /// means pm2 knows about no application at all.
    public func appStates() -> [String: ManagedServiceState]? {
        snapshot()?.states
    }

    /// The state of one pm2 application.
    ///
    /// An application pm2 has no record of is not running: it was never
    /// started, and nothing asked it to stop.  A pm2 that cannot be reached
    /// knows nothing, so the state is unknown.
    public func state(ofApp name: String) -> ManagedServiceState {
        guard let snapshot = snapshot() else { return .unknown }
        return snapshot.states[name] ?? .stopped
    }

    /// One look at pm2:  the state of every application it knows about, and
    /// the applications whose record is stale.
    ///
    /// Both come from one listing, so a caller that needs both pays for one
    /// process rather than two.  `nil` means pm2 could not be asked at all.
    public func snapshot() -> PM2Snapshot? {
        guard isAvailable else { return nil }
        guard let listing = try? run(["jlist"]) else { return nil }
        return PM2Client.snapshot(from: listing)
    }

    /// Reads the states out of what `pm2 jlist` printed.
    ///
    /// pm2 reports every application it knows about as JSON, with the state
    /// of each one under its own environment.
    ///
    /// A listing that cannot be decoded is `nil`, not an empty registry:  an
    /// empty registry reads every application as stopped, and the reconciler
    /// would act on that.  Only a listing that decodes to an empty array means
    /// pm2 knows about no application.
    public static func parseAppStates(_ listing: String) -> [String: ManagedServiceState]? {
        snapshot(from: listing)?.states
    }

    /// The one look at pm2, from what `pm2 jlist` printed.
    public static func snapshot(from listing: String) -> PM2Snapshot? {
        guard let data = listing.data(using: .utf8),
              let applications = try? JSONDecoder().decode([PM2Application].self, from: data) else { return nil }
        var states: [String: ManagedServiceState] = [:]
        var stale: Set<String> = []
        for application in applications {
            states[application.name] = state(forStatus: application.status, pid: application.pid)
            if application.status == "online", application.pid <= 0 {
                stale.insert(application.name)
            }
        }
        return PM2Snapshot(states: states, stale: stale)
    }

    /// The state pm2's own status means.
    ///
    /// An application that errored is not running and nothing asked it to
    /// stop, so it failed.  A status the Control app does not know is
    /// unknown, and an unknown service is never acted on.
    ///
    /// An application pm2 calls "online" with no process behind it is not
    /// running either.  pm2 keeps the record of every application it started
    /// and the record outlives the process:  the BotFleet MCP adapter read
    /// as "online" for days while nothing served its port and its launcher
    /// pointed at a checkout that had been removed.  A status of "online"
    /// with no pid behind it is a stale record, and a stale record is not a
    /// running service:  it reads as failed, which is what it is.
    public static func state(forStatus status: String?, pid: Int? = nil) -> ManagedServiceState {
        switch status {
        case "online":
            if (pid ?? 0) > 0 { return ManagedServiceState.running }
            return ManagedServiceState.failed
        case "stopped": return .stopped
        case "errored": return .failed
        default: return .unknown
        }
    }

    /// Starts a pm2 application.
    ///
    /// `pm2 start <name>` only works while pm2 still remembers the job.  When
    /// it does not, the ecosystem file is the way back, so it is tried second.
    ///
    /// A stale record is neither of those.  pm2 answers `pm2 start` on a
    /// record it believes is online with "already started" and starts
    /// nothing, so a stale application would never come back and the start
    /// would report a success it did not achieve.  A stale record is
    /// restarted instead, which runs the application's script again, and the
    /// restart carries the same ecosystem-file fallback as any other.
    ///
    /// `stale` is the set the caller already read, so a start does not pay for
    /// a listing of its own.  `nil` means the caller has no set:  the start is
    /// then a plain start, because a start that does not bring the application
    /// up is the caller's own signal that the record was stale.
    public func start(appName: String, stale: Set<String>? = nil) throws {
        if let stale, stale.contains(appName) {
            try restart(appName: appName)
            return
        }
        do {
            _ = try run(["start", appName])
        } catch {
            let ecosystem = home.appendingPathComponent(PM2Client.ecosystemPath)
            guard FileManager.default.fileExists(atPath: ecosystem.path) else { throw error }
            _ = try run(["start", ecosystem.path, "--only", appName])
        }
    }

    /// Stops a pm2 application.
    public func stop(appName: String) throws {
        _ = try run(["stop", appName])
    }

    /// Restarts a pm2 application.
    ///
    /// The same fallback as a start: when pm2 no longer remembers the
    /// application, the ecosystem file is the way back, so it is tried second.
    public func restart(appName: String) throws {
        do {
            _ = try run(["restart", appName])
        } catch {
            let ecosystem = home.appendingPathComponent(PM2Client.ecosystemPath)
            guard FileManager.default.fileExists(atPath: ecosystem.path) else { throw error }
            _ = try run(["start", ecosystem.path, "--only", appName])
        }
    }

    /// Brings one pm2 application to the wanted state and reports what was
    /// seen.
    ///
    /// The observed state after the change is what is reported, so a manager
    /// that refused the change is never reported as a success.  An unknown
    /// observed state is never acted on: the Control app does not stop a
    /// service it cannot see, and it does not start one it cannot prove is
    /// missing.
    @discardableResult
    public func setRunning(_ running: Bool,
                           appName: String,
                           attempts: Int = PM2Client.settleAttempts,
                           interval: TimeInterval = PM2Client.settleInterval) async throws -> ManagedServiceState {
        let wanted: ManagedServiceState = running ? .running : .stopped
        let snapshot = self.snapshot()
        // An application pm2 has no record of is stopped,  the same reading
        // `state(ofApp:)` gives,  and a pm2 that could not be asked at all is
        // unknown.  One listing answers both the state and the stale set.
        guard let snapshot else { return .unknown }
        let current = snapshot.states[appName] ?? .stopped
        if current == wanted { return current }
        if current == .unknown { return current }
        if running {
            try start(appName: appName, stale: snapshot.stale)
        } else {
            try stop(appName: appName)
        }
        return await ManagedServices.settle(to: wanted,
                                            attempts: attempts,
                                            interval: interval) {
            self.state(ofApp: appName)
        }
    }
}

private extension PM2Client {
    static func isExecutable(_ path: String) -> Bool {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) else { return false }
        return !isDirectory.boolValue
    }
}

/// The BotFleet MCP server as the Control app sees it: the loopback HTTP and
/// SSE adapter that proxies every tool call to the BotFleet Server.
public final class McpServerControl {
    /// The default loopback port of the MCP server.
    public static let defaultPort = 8794
    /// The app identity the MCP server reports.
    public static let appIdentity = "botfleet-admin-mcp"

    private let session: URLSession
    private let baseURL: URL
    private let pm2: PM2Client

    /// Prepares the MCP server control.
    ///
    /// The endpoint is optional so that a missing one is reported rather than
    /// forced open: a crash over a URL would take the whole Control app down
    /// for a problem the owner can read about in the menu.
    public init(session: URLSession = .shared,
                baseURL: URL? = URL(string: "http://127.0.0.1:\(McpServerControl.defaultPort)"),
                pm2: PM2Client = PM2Client()) throws {
        guard let baseURL else {
            throw McpServerError.refused("Could not determine the BotFleet MCP Server endpoint.")
        }
        self.session = session
        self.baseURL = baseURL
        self.pm2 = pm2
    }

    /// The observed state of the MCP server.
    ///
    /// Nothing is assumed: the server is running only when it answers its own
    /// health endpoint as the BotFleet MCP server.  A port that nothing
    /// answers is stopped; a port that answers as something else is unknown.
    public func state() async -> ManagedServiceState {
        guard let url = URL(string: "/health", relativeTo: baseURL)?.absoluteURL else { return .unknown }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 3
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch let error as URLError {
            switch error.code {
            case .cannotConnectToHost, .networkConnectionLost, .cannotFindHost:
                return .stopped
            default:
                return .unknown
            }
        }
        catch { return .unknown }
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.url?.host == "127.0.0.1" else { return .unknown }
        guard let health = try? JSONDecoder().decode(McpServerHealth.self, from: data),
              health.app == McpServerControl.appIdentity, health.status == "ok" else { return .unknown }
        return .running
    }

    /// Brings the MCP server to the wanted state.
    ///
    /// The observed state after the change is what is reported, so a manager
    /// that refused the change is never reported as a success.  An unknown
    /// observed state is never acted on: the Control app does not stop a
    /// service it cannot see, and it does not start one it cannot prove is
    /// missing.
    ///
    /// A start is one pm2 command, and the listing that would tell a stale
    /// record from a running one is a second command that every start would
    /// pay for.  The start is not paid for that listing instead:  pm2 answers
    /// a start on a record it believes is online with "already started" and
    /// starts nothing, so a start that leaves the server down has proved the
    /// record was stale, and the record is then restarted.
    @discardableResult
    public func setRunning(_ running: Bool, within: TimeInterval = PM2Client.mcpSettleWindow) async throws -> ManagedServiceState {
        let wanted: ManagedServiceState = running ? .running : .stopped
        let current = await state()
        if current == wanted { return current }
        if current == .unknown { return current }
        if running {
            try runPM2Change { try pm2.start(appName: PM2Client.mcpAppName) }
        } else {
            try runPM2Change { try pm2.stop(appName: PM2Client.mcpAppName) }
        }
        let settled = await ManagedServices.settle(to: wanted,
                                                   within: min(within, PM2Client.mcpStartProbeWindow)) {
            await self.state()
        }
        guard running, settled != wanted else { return settled }
        // The start did not bring the server up, so the record was stale:  it
        // is restarted, which runs the application's script again, and the
        // restart carries the same ecosystem-file fallback as any other.
        try runPM2Change { try pm2.restart(appName: PM2Client.mcpAppName) }
        return await ManagedServices.settle(to: wanted, within: within) { await self.state() }
    }

    /// Runs a pm2 change and says what went wrong, in the Control app's words.
    private func runPM2Change(_ change: () throws -> Void) throws {
        do {
            try change()
        } catch let error as McpServerError {
            throw error
        } catch let error as CommandExitError {
            throw McpServerError.refused(error.output.isEmpty
                ? "pm2 could not change the BotFleet MCP Server."
                : error.output)
        } catch StartupPolicyError.commandFailed {
            // The command runner reports a timeout as a generic command
            // failure.  Say which command it was, so a slow pm2 is never
            // reported as a problem with the BotFleet Server LaunchAgent.
            throw McpServerError.refused("pm2 timed out before it could change the BotFleet MCP Server.")
        } catch {
            throw McpServerError.refused("pm2 could not change the BotFleet MCP Server.")
        }
    }

    /// Restarts the MCP server and reports what was seen.
    ///
    /// The MCP server keeps its own health endpoint as the only authority on
    /// whether it is running, so the state that is reported is the one that
    /// endpoint answers with.
    @discardableResult
    public func restart(within: TimeInterval = PM2Client.mcpSettleWindow) async throws -> ManagedServiceState {
        do {
            try pm2.restart(appName: PM2Client.mcpAppName)
        } catch let error as McpServerError {
            throw error
        } catch let error as CommandExitError {
            throw McpServerError.refused(error.output.isEmpty
                ? "pm2 could not restart the BotFleet MCP Server."
                : error.output)
        } catch StartupPolicyError.commandFailed {
            throw McpServerError.refused("pm2 timed out before it could restart the BotFleet MCP Server.")
        } catch {
            throw McpServerError.refused("pm2 could not restart the BotFleet MCP Server.")
        }
        return await ManagedServices.settle(to: .running, within: within) { await self.state() }
    }
}
