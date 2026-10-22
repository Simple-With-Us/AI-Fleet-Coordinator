import Foundation

// The listener's own published state,  and the one command line that may change a wake.
//
// The panel never edits the listener's state directly.  It reads the listener's own account
// of itself —  `agent-sync daemon status --json` —  and every change it makes goes through
// the listener's own command line.

// MARK: - The envelope

/// The listener's published state.
///
/// This is the envelope `agent-sync daemon status --json` prints:  the running listener's own
/// account of what it is doing.  A setting the listener does not publish is a setting the
/// panel cannot claim anything live about.
struct ListenerStatus: Decodable, Equatable {
    /// One seat's state.
    struct Seat: Decodable, Equatable {
        /// The seat's name,  as the listener knows it.
        var bot: String?
        /// The wake type the listener is running.
        var wake: String?
        /// The muse binary the listener uses.
        var muse: String?
        /// The claude binary the listener uses.
        var claude: String?
        /// The provider the seat talks to.
        var provider: String?
        /// Where the seat's credentials come from.
        var creds: String?
        /// Whether the listener is connected for this seat.
        var connected: Bool?
        /// Whether the kill switch stops this seat.
        var paused: Bool?
        /// Whether the kill switch stops this seat's wakes only.
        var wakesPaused: Bool?
        /// The wakes this seat ran in the last day.
        var wakes24h: Int?
        /// The budget the listener is holding.
        var budget: [String: JSONValue]?
        /// The live caps the listener is holding.
        var live: [String: JSONValue]?
        /// The pin the listener holds for this seat.
        var pin: Pin?

        enum CodingKeys: String, CodingKey {
            case bot, wake, muse, claude, provider, creds, connected, paused
            case wakesPaused = "wakes_paused"
            case wakes24h = "wakes_24h"
            case budget, live, pin
        }
    }

    /// The pin that proves a seat's binary works.
    struct Pin: Decodable, Equatable {
        /// Whether the listener holds the pin.
        var pinned: Bool?
        /// The pinned muse binary.
        var pinned_muse: String?
        /// The pinned claude binary.
        var pinned_claude: String?
        /// The listener's own reason,  when the pin is not what it should be.
        var why: String?
    }

    /// One entry of the kill switch.
    struct PauseEntry: Decodable, Equatable {
        /// Whether the pause stops wakes only.
        var wakes_only: Bool?
        /// Who set it.
        var by: String?
        /// When it was set.
        var ts: Double?
    }

    /// The listener's own state.
    struct State: Decodable, Equatable {
        /// The seats,  by tag.
        var seats: [String: Seat]?
        /// The kill switch,  by scope.
        var pause: [String: PauseEntry]?
        /// When the listener last published.
        var updated: Double?
        /// When the listener started.
        var started: Double?
        /// The listener's version.
        var version: String?
    }

    /// Whether the listener is running.
    var running: Bool?
    /// Its pid.
    var pid: Int?
    /// The state itself.
    var status: State?
    /// The plugin the listener runs,  when it runs one.
    var plugin: JSONValue?
    /// The launch agent that keeps the listener alive.
    var launch_agent: JSONValue?

    /// The seat's state,  when the listener has one.
    func seat(_ tag: String) -> Seat? {
        status?.seats?[tag]
    }

    /// The kill switch,  by scope.
    var pause: [String: PauseEntry] {
        status?.pause ?? [:]
    }

    /// When the listener last published its state.
    var updated: Date? {
        status?.updated.map { Date(timeIntervalSince1970: $0) }
    }

    /// The seats the listener knows.
    var seatTags: [String] {
        (status?.seats ?? [:]).keys.sorted()
    }
}

// MARK: - The command line

/// A run of the fleet's `agent-sync` command line.
struct AgentSyncInvocation: Equatable {
    var arguments: [String]
    var timeout: TimeInterval

    static let defaultTimeout: TimeInterval = 60
    /// A test wake runs a real agent,  so it gets the engine's own long timeout.
    static let testWakeTimeout: TimeInterval = 900

    init(_ arguments: [String], timeout: TimeInterval = AgentSyncInvocation.defaultTimeout) {
        self.arguments = arguments
        self.timeout = timeout
    }

    /// `daemon status --json`:  the listener's own published state.
    static func status() -> AgentSyncInvocation {
        AgentSyncInvocation(["daemon", "status", "--json"])
    }

    /// `daemon pause --seat S --wakes-only`:  the kill switch,  for wakes only.
    static func pause(seat: String) throws -> AgentSyncInvocation {
        AgentSyncInvocation([
            "daemon", "pause", "--seat", try checked(seat), "--wakes-only",
        ])
    }

    /// `daemon resume --seat S`:  lift the kill switch.
    static func resume(seat: String) throws -> AgentSyncInvocation {
        AgentSyncInvocation(["daemon", "resume", "--seat", try checked(seat)])
    }

    /// `daemon reload`:  re-read the configuration file.
    ///
    /// A reload does not re-read a seat's budget,  so a changed budget needs a listener
    /// restart.  The panel says so instead of pretending the reload was enough.
    static func reload() -> AgentSyncInvocation {
        AgentSyncInvocation(["daemon", "reload"])
    }

    /// `daemon test-wake --seat S --runtime R --run`:  run the seat's wake for real.
    static func testWakeRun(seat: String, runtime: String) throws -> AgentSyncInvocation {
        AgentSyncInvocation([
            "daemon", "test-wake", "--seat", try checked(seat), "--runtime", try checked(runtime), "--run",
        ], timeout: testWakeTimeout)
    }

    /// `daemon test-wake --seat S --runtime R --pin`:  pin the binary that passed.
    static func testWakePin(seat: String, runtime: String) throws -> AgentSyncInvocation {
        AgentSyncInvocation([
            "daemon", "test-wake", "--seat", try checked(seat), "--runtime", try checked(runtime), "--pin",
        ], timeout: testWakeTimeout)
    }

    /// The command as a person would type it.
    var commandLine: String {
        (["agent-sync"] + arguments).joined(separator: " ")
    }

    /// A seat tag and a runtime name are plain tokens.  A value that could be read as a
    /// flag,  or that carries whitespace or a path separator,  is refused before any
    /// process exists.
    private static func checked(_ value: String) throws -> String {
        guard FleetConfigInvocation.isSafeToken(value) else {
            throw AgentSyncError.invalidArgument(value)
        }
        return value
    }
}

// MARK: - Results and errors

struct AgentSyncCLIResult: Equatable {
    var exitCode: Int32
    var stdout: String
    var stderr: String
    var timedOut: Bool = false

    /// The last few non-empty stderr lines,  bounded,  for showing a refusal.
    var stderrTail: String {
        let lines = stderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(6).joined(separator: "\n")
        return tail.count > 800 ? String(tail.suffix(800)) : tail
    }
}

enum AgentSyncError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    case commandNotFound(String)
    case launchFailed(String)
    case timedOut(Int)
    case outputTooLarge
    case refused(exitCode: Int32, message: String)
    case undecodable(String)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let value):
            return "The \(value) value is not a plain token,  so nothing was run."
        case .commandNotFound(let path):
            return "The agent-sync command line was not found at \(path)."
        case .launchFailed(let detail):
            return "The agent-sync command line could not be started:  \(detail)"
        case .timedOut(let seconds):
            return "The agent-sync command line did not finish within \(seconds) seconds."
        case .outputTooLarge:
            return "The agent-sync command line produced more output than Muster will read."
        case .refused(let code, let message):
            let detail = message.isEmpty ? "No reason was given." : message
            return "The agent-sync command line refused the run (exit \(code)).  \(detail)"
        case .undecodable(let detail):
            return "The agent-sync output could not be read:  \(detail)"
        }
    }
}

// MARK: - The runner

protocol AgentSyncRunning: Sendable {
    func run(_ invocation: AgentSyncInvocation) async throws -> AgentSyncCLIResult
}

/// Runs the fleet's `agent-sync` command line from the repository checkout.
///
/// The command line is the listener's own,  at `scripts/agent-sync`,  so the app asks the
/// same tool a person would ask.  The child environment keeps HOME and PATH,  and drops the
/// variables that could point the command line at a stand-in.
struct AgentSyncCLI: AgentSyncRunning {
    static let maximumOutputBytes = 8 * 1024 * 1024

    /// Where the checkout is.
    var location: @Sendable () -> EngineLocation?

    init(location: @escaping @Sendable () -> EngineLocation? = { EngineLocator.locate() }) {
        self.location = location
    }

    /// The command line's own path.
    static func commandURL(at location: EngineLocation) -> URL {
        location.packageDirectory.appendingPathComponent("agent-sync")
    }

    /// The child environment.
    ///
    /// HOME stays,  because the listener's state lives under it.  PYTHON* and FLEET_* go,
    /// so nothing in the app's environment can point the command line at another checkout.
    static func sanitizedEnvironment(_ environment: [String: String]) -> [String: String] {
        var result = environment.filter { key, _ in
            !key.hasPrefix("PYTHON") && !key.hasPrefix("FLEET_")
        }
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        return result
    }

    static func processArguments(at location: EngineLocation, for invocation: AgentSyncInvocation) -> [String] {
        [commandURL(at: location).path] + invocation.arguments
    }

    func run(_ invocation: AgentSyncInvocation) async throws -> AgentSyncCLIResult {
        guard let resolved = location() else {
            throw AgentSyncError.commandNotFound("the repository checkout")
        }
        let command = Self.commandURL(at: resolved)
        guard FileManager.default.isExecutableFile(atPath: command.path) else {
            throw AgentSyncError.commandNotFound(command.path)
        }
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let result = try Self.runBlocking(
                        invocation,
                        at: resolved,
                        command: command
                    )
                    continuation.resume(returning: result)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private final class TimeoutFlag: @unchecked Sendable {
        private let lock = NSLock()
        private var value = false
        func set() { lock.lock(); value = true; lock.unlock() }
        var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
    }

    private static func runBlocking(
        _ invocation: AgentSyncInvocation,
        at location: EngineLocation,
        command: URL
    ) throws -> AgentSyncCLIResult {
        let fileManager = FileManager.default
        // Output goes to files,  not pipes:  nothing can deadlock on a full pipe,  and a
        // child that outlives the app never takes a SIGPIPE when it prints its last line.
        let scratch = fileManager.temporaryDirectory
            .appendingPathComponent("muster-agent-sync-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(
            at: scratch, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        defer { try? fileManager.removeItem(at: scratch) }
        let stdoutURL = scratch.appendingPathComponent("stdout.txt")
        let stderrURL = scratch.appendingPathComponent("stderr.txt")
        fileManager.createFile(atPath: stdoutURL.path, contents: nil)
        fileManager.createFile(atPath: stderrURL.path, contents: nil)
        let stdoutHandle = try FileHandle(forWritingTo: stdoutURL)
        let stderrHandle = try FileHandle(forWritingTo: stderrURL)

        let process = Process()
        process.executableURL = command
        process.arguments = invocation.arguments
        process.currentDirectoryURL = location.packageDirectory
        process.environment = sanitizedEnvironment(ProcessInfo.processInfo.environment)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            try? stdoutHandle.close()
            try? stderrHandle.close()
            throw AgentSyncError.launchFailed(error.localizedDescription)
        }

        let flag = TimeoutFlag()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + invocation.timeout)
        timer.setEventHandler {
            flag.set()
            if process.isRunning { process.terminate() }
        }
        timer.resume()
        process.waitUntilExit()
        timer.cancel()
        try? stdoutHandle.close()
        try? stderrHandle.close()

        return AgentSyncCLIResult(
            exitCode: process.terminationStatus,
            stdout: try read(stdoutURL),
            stderr: try read(stderrURL),
            timedOut: flag.isSet
        )
    }

    private static func read(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: maximumOutputBytes + 1) ?? Data()
        if data.count > maximumOutputBytes { throw AgentSyncError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
    }
}

// MARK: - The JSON bridge

extension JSONValue {
    /// The value as the configuration file holds it.
    var configValue: ListenerConfigValue {
        switch self {
        case .string(let text): return .text(text)
        case .number(let number): return .number(number)
        case .bool(let flag): return .bool(flag)
        case .array(let items): return .list(items.map(\.configValue))
        case .object(let fields):
            var result: [String: ListenerConfigValue] = [:]
            for (key, value) in fields {
                result[key] = value.configValue
            }
            return .table(result)
        case .null: return .text("")
        }
    }
}
