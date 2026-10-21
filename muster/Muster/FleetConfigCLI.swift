import Foundation

// A general wrapper around the `fleet-config` engine (`python3 -m fleet_config`, run from the
// repository's scripts directory).  Nothing here is specific to apply:  any view can run any
// verb through `FleetConfigRunning`, and tests substitute a fake runner.
//
// Safety properties, each pinned by a test:
//   * arguments are an argv array, never a shell string, and seat and kind tokens are validated
//   * the interpreter is the absolute /usr/bin/python3
//   * the engine's global flags (--manifest, --registry, --home) are never passed
//   * the child environment drops FLEET_CONFIG_* and PYTHON* variables, so nothing in the app's
//     environment can point the engine at a stand-in helper or a different module path
//   * the Touch ID gate lives inside the engine;  this file never reaches it

// MARK: - Invocation

struct FleetConfigInvocation: Equatable {
    var arguments: [String]
    var timeout: TimeInterval

    static let defaultTimeout: TimeInterval = 180
    static let applyTimeout: TimeInterval = 900

    init(_ arguments: [String], timeout: TimeInterval = FleetConfigInvocation.defaultTimeout) {
        self.arguments = arguments
        self.timeout = timeout
    }

    /// A seat tag or config kind is a plain token.  A value that could be read as a flag, or that
    /// carries whitespace or a path separator, is refused before any process exists.
    static func isSafeToken(_ value: String) -> Bool {
        guard let first = value.unicodeScalars.first,
              CharacterSet.alphanumerics.contains(first) else { return false }
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        return value.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private static func checked(_ value: String, as label: String) throws -> String {
        guard isSafeToken(value) else { throw FleetConfigCLIError.invalidArgument(label) }
        return value
    }

    /// `status --json [--seat S] [--kind K] [--platform P] [--no-doctor]`
    static func status(
        seat: String? = nil,
        kind: String? = nil,
        platform: String? = nil,
        includeDoctors: Bool = true
    ) throws -> FleetConfigInvocation {
        var arguments = ["status", "--json"]
        if let seat { arguments += ["--seat", try checked(seat, as: "seat")] }
        if let kind { arguments += ["--kind", try checked(kind, as: "kind")] }
        if let platform { arguments += ["--platform", try checked(platform, as: "platform")] }
        if !includeDoctors { arguments.append("--no-doctor") }
        return FleetConfigInvocation(arguments)
    }

    /// `apply SEAT [KIND] --plan`:  read-only, prints the plan and changes nothing.
    static func planApply(seat: String, kind: String? = nil) throws -> FleetConfigInvocation {
        var arguments = ["apply", try checked(seat, as: "seat")]
        if let kind { arguments.append(try checked(kind, as: "kind")) }
        arguments.append("--plan")
        return FleetConfigInvocation(arguments)
    }

    /// `apply SEAT [KIND] --yes`:  writes.  `--yes` only skips the engine's stdin prompt, because
    /// the app has already asked once.  The engine still gates persistence-class writes behind
    /// Touch ID, and no flag here can skip that.
    static func apply(seat: String, kind: String? = nil) throws -> FleetConfigInvocation {
        var arguments = ["apply", try checked(seat, as: "seat")]
        if let kind { arguments.append(try checked(kind, as: "kind")) }
        arguments.append("--yes")
        return FleetConfigInvocation(arguments, timeout: applyTimeout)
    }
}

// MARK: - Result and errors

struct FleetConfigCLIResult: Equatable {
    var exitCode: Int32
    var stdout: String
    var stderr: String
    var timedOut: Bool = false

    /// The last few non-empty stderr lines, bounded, for showing a refusal or an error.
    var stderrTail: String {
        let lines = stderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(6).joined(separator: "\n")
        return tail.count > 800 ? String(tail.suffix(800)) : tail
    }
}

enum FleetConfigCLIError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    case engineNotFound(String)
    case launchFailed(String)
    case timedOut(Int)
    case outputTooLarge
    case engineRefused(exitCode: Int32, message: String)
    case undecodable(String)
    case unsupportedSchema(Int)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let label):
            return "The \(label) value is not a plain token, so nothing was run."
        case .engineNotFound(let path):
            return ApplyCopy.join(
                "The fleet-config engine was not found at \(path).",
                "Set the fleetConfigRepoRoot default or the MUSTER_REPO_ROOT variable to the repository root."
            )
        case .launchFailed(let detail):
            return "The fleet-config engine could not be started: \(detail)"
        case .timedOut(let seconds):
            return "The fleet-config engine did not finish within \(seconds) seconds."
        case .outputTooLarge:
            return "The fleet-config engine produced more output than Muster will read."
        case .engineRefused(let code, let message):
            let detail = message.isEmpty ? "No reason was given." : ApplyCopy.normalize(message)
            return ApplyCopy.join("The fleet-config engine refused the run (exit \(code)).", detail)
        case .undecodable(let detail):
            return "The fleet-config output could not be read: \(detail)"
        case .unsupportedSchema(let version):
            return "The fleet-config output uses schema version \(version), which this Muster does not understand."
        }
    }
}

// MARK: - Location

/// Where the engine lives.  The repository root is a setting:  the `fleetConfigRepoRoot` user
/// default, then the `MUSTER_REPO_ROOT` environment variable, then `~/Code/AI-Fleet-Coordinator`.
struct FleetConfigLocation: Equatable {
    static let defaultsKey = "fleetConfigRepoRoot"
    static let environmentKey = "MUSTER_REPO_ROOT"

    var repoRoot: URL

    var scriptsDirectory: URL { repoRoot.appendingPathComponent("scripts", isDirectory: true) }
    var engineEntryPoint: URL {
        scriptsDirectory.appendingPathComponent("fleet_config/__main__.py")
    }

    static func resolve(
        defaults: UserDefaults = .standard,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> FleetConfigLocation {
        func expanded(_ raw: String) -> URL {
            URL(fileURLWithPath: (raw as NSString).expandingTildeInPath, isDirectory: true)
        }
        if let saved = defaults.string(forKey: defaultsKey), !saved.isEmpty {
            return FleetConfigLocation(repoRoot: expanded(saved))
        }
        if let fromEnvironment = environment[environmentKey], !fromEnvironment.isEmpty {
            return FleetConfigLocation(repoRoot: expanded(fromEnvironment))
        }
        return FleetConfigLocation(
            repoRoot: home.appendingPathComponent("Code/AI-Fleet-Coordinator", isDirectory: true)
        )
    }

    func validate(fileManager: FileManager = .default) throws {
        guard fileManager.fileExists(atPath: engineEntryPoint.path) else {
            throw FleetConfigCLIError.engineNotFound(engineEntryPoint.path)
        }
    }
}

// MARK: - Runner

protocol FleetConfigRunning: Sendable {
    func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult
}

struct FleetConfigCLI: FleetConfigRunning {
    static let interpreter = "/usr/bin/python3"
    static let maximumOutputBytes = 8 * 1024 * 1024

    var location: @Sendable () -> FleetConfigLocation

    init(location: @escaping @Sendable () -> FleetConfigLocation = { FleetConfigLocation.resolve() }) {
        self.location = location
    }

    /// The child environment.  Variables that could redirect the engine's helper or its module
    /// search path are dropped, and bytecode is not written into the repository.
    static func sanitizedEnvironment(_ environment: [String: String]) -> [String: String] {
        var result = environment.filter { key, _ in
            !key.hasPrefix("FLEET_CONFIG") && !key.hasPrefix("PYTHON")
        }
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        return result
    }

    static func processArguments(for invocation: FleetConfigInvocation) -> [String] {
        ["-m", "fleet_config"] + invocation.arguments
    }

    func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult {
        let resolved = location()
        try resolved.validate()
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try Self.runBlocking(invocation, at: resolved))
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
        _ invocation: FleetConfigInvocation,
        at location: FleetConfigLocation
    ) throws -> FleetConfigCLIResult {
        let fileManager = FileManager.default
        // Output goes to files, not pipes:  nothing can deadlock on a full pipe, and a child
        // that outlives the app never takes a SIGPIPE when it prints its last line.
        let scratch = fileManager.temporaryDirectory
            .appendingPathComponent("muster-cli-\(UUID().uuidString)", isDirectory: true)
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
        process.executableURL = URL(fileURLWithPath: interpreter)
        process.arguments = processArguments(for: invocation)
        process.currentDirectoryURL = location.scriptsDirectory
        process.environment = sanitizedEnvironment(ProcessInfo.processInfo.environment)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            try? stdoutHandle.close()
            try? stderrHandle.close()
            throw FleetConfigCLIError.launchFailed(error.localizedDescription)
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

        let stdout = try read(stdoutURL)
        let stderr = try read(stderrURL)
        return FleetConfigCLIResult(
            exitCode: process.terminationStatus,
            stdout: stdout,
            stderr: stderr,
            timedOut: flag.isSet
        )
    }

    private static func read(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: maximumOutputBytes + 1) ?? Data()
        if data.count > maximumOutputBytes { throw FleetConfigCLIError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
    }
}
