import Foundation

public protocol CommandRunning {
    func run(_ executable: String, _ arguments: [String]) throws -> String
    func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> String
    /// Runs a command with an explicit environment.  A menu bar app inherits
    /// no shell PATH, so a command that needs one has to be given it.
    func run(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) throws -> String
    /// Runs a command and keeps what it printed, whatever its exit status.
    ///
    /// A command that warns and still answers is not a command that failed.
    /// `lsof` exits with 1 when it could not examine one of the processes it
    /// was asked about, and the answer for the ones it could examine is in
    /// its output all the same.  Throwing that away would leave the caller
    /// with nothing, and the caller cannot tell an empty answer from a lost
    /// one.
    func runKeepingOutput(_ executable: String, _ arguments: [String]) -> String
}

public struct CommandExitError: LocalizedError {
    /// What the command printed on standard output.
    public let output: String
    /// The exit status, when the runner knows it.  Callers that must tell a
    /// refused command from an absent one (fleet-secrets exit 3) read this;
    /// a fake that only knows the command failed leaves it 0.
    public let status: Int32
    /// What the command printed on standard error,  as it printed it.  The runner keeps it
    /// apart from `output`,  because callers read `output` as the command's answer.
    public let errorOutput: String
    public init(output: String, status: Int32 = 0, errorOutput: String = "") {
        self.output = output
        self.status = status
        self.errorOutput = errorOutput
    }

    /// Why the command failed,  in a few clean words:  what it said on standard error,  or
    /// on standard output when it said nothing there.  Empty when it said nothing at all.
    public var reason: String { CommandText.reason(errorOutput: errorOutput, output: output) }

    public var errorDescription: String? {
        let why = reason
        return why.isEmpty ? "The system command failed." : "The system command failed:  \(why)"
    }
}

/// A command that did not finish in its time.
///
/// It names the command,  so a slow `launchctl` is never reported as a problem with some
/// other service.
public struct CommandTimedOutError: LocalizedError, Equatable {
    public let executable: String
    public let arguments: [String]

    public init(executable: String, arguments: [String]) {
        self.executable = executable
        self.arguments = arguments
    }

    /// The command's name:  `launchctl`,  or `pm2` for a script a node or python runs.
    public var commandName: String {
        let base = (executable as NSString).lastPathComponent
        guard ["node", "python3", "python", "bash", "sh"].contains(base),
              let script = arguments.first(where: { !$0.hasPrefix("-") }) else { return base }
        return (script as NSString).lastPathComponent
    }

    public var errorDescription: String? { "\(commandName) did not finish in time." }
}

/// Makes what a command printed fit for a menu row or a panel.
public enum CommandText {
    /// The most characters a reason keeps.
    public static let reasonLimit = 300

    /// A reason from what a command printed,  preferring standard error.
    public static func reason(errorOutput: String, output: String) -> String {
        let fromError = clean(errorOutput)
        return fromError.isEmpty ? clean(output) : fromError
    }

    /// Cleans what a command printed.
    ///
    /// Colour codes and other control characters go,  and so does a table pm2 draws with
    /// box characters.  The last few lines are kept,  because the reason comes last,  and
    /// the whole is cut to `reasonLimit` characters.
    public static func clean(_ text: String) -> String {
        let withoutColour = text.replacingOccurrences(of: "\u{1B}\\[[0-9;?]*[ -/]*[@-~]",
                                                      with: "",
                                                      options: .regularExpression)
        let box = CharacterSet(charactersIn: "│┌┐└┘├┤┬┴┼─═║╔╗╚╝╠╣╦╩╬")
        let lines = withoutColour.split(whereSeparator: \.isNewline)
            .map { line -> String in
                var printable = String.UnicodeScalarView()
                printable.append(contentsOf: line.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) })
                return String(printable).trimmingCharacters(in: .whitespaces)
            }
            .filter { line in
                !line.isEmpty && line.unicodeScalars.allSatisfy { !box.contains($0) }
            }
        let tail = lines.suffix(4).joined(separator: " ")
        guard tail.count > reasonLimit else { return tail }
        return "\u{2026}" + String(tail.suffix(reasonLimit))
    }
}

public extension CommandRunning {
    func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> String {
        try run(executable, arguments)
    }

    func run(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) throws -> String {
        try run(executable, arguments, timeout: timeout)
    }

    /// The default keeps what the command printed, whatever it exited with.
    ///
    /// A command that warns and still answers is not a command that failed,
    /// and the answer it printed is in the failure itself.  A conformer that
    /// does not override this has to keep that answer too, or the same probe
    /// would come back empty from one conformer and full from another.
    func runKeepingOutput(_ executable: String, _ arguments: [String]) -> String {
        do { return try run(executable, arguments) }
        catch let failure as CommandExitError { return failure.output }
        catch { return "" }
    }
}

public struct ProcessCommandRunner: CommandRunning {
    public init() {}
    public func run(_ executable: String, _ arguments: [String]) throws -> String {
        try run(executable, arguments, timeout: 15)
    }
    public func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> String {
        try run(executable, arguments, environment: ProcessInfo.processInfo.environment, timeout: timeout)
    }
    public func run(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) throws -> String {
        let (result, errors, status) = try execute(executable, arguments, environment: environment, timeout: timeout)
        guard status == 0 else { throw CommandExitError(output: result, status: status, errorOutput: errors) }
        return result
    }
    public func runKeepingOutput(_ executable: String, _ arguments: [String]) -> String {
        runKeepingOutput(executable, arguments, environment: ProcessInfo.processInfo.environment, timeout: 15)
    }
    public func runKeepingOutput(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) -> String {
        (try? execute(executable, arguments, environment: environment, timeout: timeout)).map { $0.0 } ?? ""
    }
    /// The most standard error a command is kept for.  A command that writes more than this
    /// has its last part kept,  which is where the reason is.
    private static let errorLimit = 16 * 1024

    /// Runs a command and reports what it printed on each stream and how it ended.
    ///
    /// Standard error is read on a pipe of its own,  so a failure carries its reason and
    /// standard output stays the command's answer.  A command that does not finish in its
    /// time is reported as the one that timed out.
    private func execute(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) throws -> (String, String, Int32) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = environment
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        let readerDone = DispatchSemaphore(value: 0)
        let errorReaderDone = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        var data = Data()
        var errorData = Data()
        process.terminationHandler = { _ in finished.signal() }
        try process.run()
        DispatchQueue.global(qos: .utility).async {
            data = output.fileHandleForReading.readDataToEndOfFile()
            readerDone.signal()
        }
        DispatchQueue.global(qos: .utility).async {
            // Read it all so the command never blocks on a full pipe,  and keep the end,
            // where the reason is.
            let all = errors.fileHandleForReading.readDataToEndOfFile()
            errorData = all.suffix(Self.errorLimit)
            errorReaderDone.signal()
        }
        if finished.wait(timeout: .now() + timeout) == .timedOut {
            if process.isRunning { process.terminate() }
            if finished.wait(timeout: .now() + 2) == .timedOut && process.isRunning {
                Darwin.kill(process.processIdentifier, SIGKILL)
            }
            throw CommandTimedOutError(executable: executable, arguments: arguments)
        }
        guard readerDone.wait(timeout: .now() + 2) == .success,
              errorReaderDone.wait(timeout: .now() + 2) == .success else {
            throw CommandTimedOutError(executable: executable, arguments: arguments)
        }
        let result = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let errorText = String(decoding: errorData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return (result, errorText, process.terminationStatus)
    }
}

public enum StartupPolicyError: LocalizedError {
    case updateRequired
    case bootIdentityUnavailable
    case launchAgentMissing
    case commandFailed
    case refused(String)

    public var errorDescription: String? {
        switch self {
        case .updateRequired: return "Update BotFleet before changing server startup settings."
        case .bootIdentityUnavailable: return "Could not identify the current Mac boot session."
        case .launchAgentMissing: return "The BotFleet Server LaunchAgent is not installed.  Reinstall BotFleet."
        // It names no service:  the caller that knows which LaunchAgent it was changing
        // says so (`StartupPolicy.launchctl`),  and a timeout is `CommandTimedOutError`.
        case .commandFailed: return "A system command did not work."
        case .refused(let message): return message
        }
    }
}

public enum ServerLoginPolicy: Equatable {
    case legacy
    case enabled
    case disabled
}

public enum InstalledServerState: Equatable {
    case notInstalled
    case stopped
    case running
    case unknown

    /// The state as the fleet services list shows it.
    public var managedState: ManagedServiceState {
        switch self {
        case .running: return .running
        case .stopped: return .stopped
        case .notInstalled: return .notInstalled
        case .unknown: return .unknown
        }
    }
}

public final class StartupPolicy {
    private let home: URL
    private let files: FileManager
    private let commands: CommandRunning
    private let uid: UInt32
    private let environment: [String: String]

    public init(home: URL = FileManager.default.homeDirectoryForCurrentUser,
                files: FileManager = .default,
                commands: CommandRunning = ProcessCommandRunner(),
                uid: UInt32 = getuid(),
                environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.home = home
        self.files = files
        self.commands = commands
        self.uid = uid
        self.environment = environment
    }

    private var dataDirectory: URL {
        if let value = environment["OMB_DATA_DIR"], !value.isEmpty, value.hasPrefix("/") {
            return URL(fileURLWithPath: value, isDirectory: true)
        }
        return home.appendingPathComponent(".botfleet", isDirectory: true)
    }
    private var wrapper: URL { home.appendingPathComponent("apps/botfleet-server-start.sh") }
    private var agent: URL { home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist") }
    private var disabled: URL { dataDirectory.appendingPathComponent("startup-disabled.marker") }
    private var enabled: URL { dataDirectory.appendingPathComponent("startup-enabled.marker") }
    private var session: URL { dataDirectory.appendingPathComponent("startup-session") }
    private var stopped: URL { dataDirectory.appendingPathComponent("stopped.marker") }

    /// The label of the BotFleet Server LaunchAgent.
    public static let serverAgentLabel = "app.botfleet.server"

    /// The LaunchAgent plist that installs a label.
    public func launchAgentPlist(label: String) -> URL {
        home.appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    public var supportsPolicy: Bool {
        guard let data = try? Data(contentsOf: wrapper),
              let source = String(data: data, encoding: .utf8) else { return false }
        return source.contains("BF_CONTROL_STARTUP_POLICY_V1")
    }

    public var loginPolicy: ServerLoginPolicy {
        if files.fileExists(atPath: disabled.path) { return .disabled }
        if files.fileExists(atPath: enabled.path) { return .enabled }
        return .legacy
    }

    public var startsAtLogin: Bool { loginPolicy == .enabled }

    public var installedServerState: InstalledServerState {
        state(ofAgent: StartupPolicy.serverAgentLabel)
    }

    /// The state of a launchd agent, by its label.
    ///
    /// An agent with a live process behind it is running.  An agent that is
    /// loaded without one is stopped, and so is one that is not loaded at
    /// all.  An agent whose plist is not installed is not installed, and a
    /// launchctl that could not be asked is unknown.
    public func state(ofAgent label: String) -> InstalledServerState {
        guard files.fileExists(atPath: launchAgentPlist(label: label).path) else { return .notInstalled }
        let listing: String
        do { listing = try commands.run("/bin/launchctl", ["list"]) }
        catch { return .unknown }
        let row = listing.split(separator: "\n").first { line in
            line.split(whereSeparator: \.isWhitespace).last == Substring(label)
        }
        guard let row else { return .stopped }
        let pid = row.split(whereSeparator: \.isWhitespace).first ?? "-"
        return UInt32(pid) == nil ? .stopped : .running
    }

    public func setStartsAtLogin(_ enabled: Bool) throws {
        guard supportsPolicy else { throw StartupPolicyError.updateRequired }
        let boot = try bootID()
        try files.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        try Data((boot + "\n").utf8).write(to: session, options: .atomic)
        if enabled {
            try Data().write(to: self.enabled, options: .atomic)
            if files.fileExists(atPath: disabled.path) { try files.removeItem(at: disabled) }
        } else {
            try Data().write(to: disabled, options: .atomic)
            if files.fileExists(atPath: self.enabled.path) { try files.removeItem(at: self.enabled) }
        }
    }

    public func startServer() throws {
        guard supportsPolicy else { throw StartupPolicyError.updateRequired }
        guard files.fileExists(atPath: agent.path) else { throw StartupPolicyError.launchAgentMissing }
        let boot = try bootID()
        try files.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        try Data((boot + "\n").utf8).write(to: session, options: .atomic)
        let wasStopped = files.fileExists(atPath: stopped.path)
        if wasStopped { try files.removeItem(at: stopped) }
        do {
            try startAgent(label: StartupPolicy.serverAgentLabel)
        } catch {
            if wasStopped { try? Data().write(to: stopped, options: .atomic) }
            throw error
        }
    }

    /// Restarts the BotFleet Server.
    ///
    /// A restart asks for a fresh copy of the server, so the deliberate-stop
    /// marker is cleared first: the installed wrapper stays down while that
    /// marker exists, and a restart that left it in place would report a
    /// server that never came back.  The marker is put back when the restart
    /// fails, so a refused restart does not lift a deliberate stop.
    ///
    /// The returned handle puts the marker back as well, for a restart that
    /// does not throw but does not replace the server either.
    @discardableResult
    public func restartServer() throws -> DeliberateStop {
        guard supportsPolicy else { throw StartupPolicyError.updateRequired }
        guard files.fileExists(atPath: agent.path) else { throw StartupPolicyError.launchAgentMissing }
        let boot = try bootID()
        try files.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        try Data((boot + "\n").utf8).write(to: session, options: .atomic)
        let wasStopped = files.fileExists(atPath: stopped.path)
        if wasStopped { try files.removeItem(at: stopped) }
        do {
            try restartAgent(label: StartupPolicy.serverAgentLabel)
        } catch {
            if wasStopped { try? Data().write(to: stopped, options: .atomic) }
            throw error
        }
        return DeliberateStop(marker: stopped, files: files, wasStopped: wasStopped)
    }

    /// What a restart of the BotFleet Server did to the deliberate-stop
    /// marker.
    ///
    /// A restart clears the marker so the installed wrapper can start the
    /// server again.  When the restart does not replace the server, the
    /// marker goes back, because a refused restart must not lift a
    /// deliberate stop.
    public struct DeliberateStop {
        private let marker: URL
        private let files: FileManager
        private let wasStopped: Bool

        fileprivate init(marker: URL, files: FileManager, wasStopped: Bool) {
            self.marker = marker
            self.files = files
            self.wasStopped = wasStopped
        }

        /// True when the marker was there before the restart.
        public var wasStoppedByOwner: Bool { wasStopped }

        /// Puts the deliberate-stop marker back, when it was there before.
        public func restore() {
            guard wasStopped else { return }
            try? files.createDirectory(at: marker.deletingLastPathComponent(),
                                       withIntermediateDirectories: true)
            try? Data().write(to: marker, options: .atomic)
        }
    }

    /// Starts a launchd agent: it is enabled, loaded if it is not loaded
    /// yet, and kickstarted.
    ///
    /// The kickstart does not terminate a running instance, so starting an
    /// agent that is already up leaves it alone.
    public func startAgent(label: String) throws {
        let plist = launchAgentPlist(label: label)
        guard files.fileExists(atPath: plist.path) else {
            throw StartupPolicyError.refused("The \(label) LaunchAgent is not installed on the Mac.")
        }
        let domain = "gui/\(uid)"
        let full = "\(domain)/\(label)"
        try launchctl(["enable", full], doing: "start", label: label)
        if (try? commands.run("/bin/launchctl", ["print", full])) == nil {
            try launchctl(["bootstrap", domain, plist.path], doing: "start", label: label)
        }
        try launchctl(["kickstart", full], doing: "start", label: label)
    }

    /// Stops a launchd agent and leaves it stopped.
    ///
    /// The agent is booted out of the login domain, so launchd does not
    /// bring it back on its own afterwards.
    public func stopAgent(label: String) throws {
        let full = "gui/\(uid)/\(label)"
        try launchctl(["bootout", full], doing: "stop", label: label)
    }

    /// Restarts a launchd agent.
    ///
    /// An agent that is not loaded is started instead, because there is
    /// nothing to replace.  A loaded one is kickstarted with the running
    /// instance replaced, so the agent comes back fresh.
    public func restartAgent(label: String) throws {
        let full = "gui/\(uid)/\(label)"
        if (try? commands.run("/bin/launchctl", ["print", full])) == nil {
            try startAgent(label: label)
            return
        }
        try launchctl(["kickstart", "-k", full], doing: "restart", label: label)
    }

    /// Runs one launchctl command for one agent,  and says which agent and what it was
    /// doing when the command did not work,  with the reason launchctl gave.
    @discardableResult
    private func launchctl(_ arguments: [String], doing verb: String, label: String) throws -> String {
        let subject = "the \(label) LaunchAgent on the Mac"
        do {
            return try commands.run("/bin/launchctl", arguments)
        } catch let error as CommandExitError {
            let why = error.reason
            throw StartupPolicyError.refused("macOS could not \(verb) \(subject)" + (why.isEmpty ? "." : ":  \(why)"))
        } catch let error as CommandTimedOutError {
            throw StartupPolicyError.refused("\(error.commandName) did not finish in time, so macOS did not \(verb) \(subject).")
        } catch {
            throw StartupPolicyError.refused("macOS could not \(verb) \(subject).")
        }
    }

    private func bootID() throws -> String {
        let value = (try? commands.run("/usr/sbin/sysctl", ["-n", "kern.bootsessionuuid"])) ?? ""
        guard UUID(uuidString: value) != nil else { throw StartupPolicyError.bootIdentityUnavailable }
        return value
    }
}
