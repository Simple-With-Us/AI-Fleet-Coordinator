import Foundation

// A general wrapper around the `fleet-config` engine.  Nothing here is specific to apply:  any
// view can run any verb through `FleetConfigRunning`, and tests substitute a fake runner.
//
// What it does to limit what can influence the engine (and what it does not):
//   * the interpreter is the absolute /usr/bin/python3, started isolated (`-I -B`), with the
//     validated repository's scripts directory put on sys.path by a fixed bootstrap;  the root
//     reaches the bootstrap as an argv element, never inside the code string
//   * arguments are an argv array, never a shell string, and seat and kind tokens are validated
//   * the engine's global flags (--manifest, --registry, --home) are never passed
//   * the child gets an allow-listed environment (HOME, USER, LOGNAME, TMPDIR, LANG, LC_ALL and a
//     fixed PATH), so ambient variables cannot redirect the engine
//   * the repository root is validated before every run (FleetConfigRepoValidation.swift)
// This is hygiene, not a sandbox:  the engine is code from the validated checkout, and it can do
// whatever the user can.  The Touch ID gate lives inside the engine, which this file never reaches.

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
    /// True when the process ended on an uncaught signal;  `exitCode` is then the signal number.
    var endedBySignal: Bool = false

    /// A timeout or a signal means the exit code says nothing about the engine's verdict.
    func requireCompleted(timeout: TimeInterval) throws {
        if timedOut { throw FleetConfigCLIError.timedOut(Int(timeout)) }
        if endedBySignal { throw FleetConfigCLIError.endedBySignal(exitCode) }
    }

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
    case launchFailed(String)
    case timedOut(Int)
    case endedBySignal(Int32)
    case outputTooLarge
    case engineRefused(exitCode: Int32, message: String)
    case undecodable(String)
    case unsupportedSchema(Int)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let label):
            return "The \(label) value is not a plain token, so nothing was run."
        case .launchFailed(let detail):
            return "The fleet-config engine could not be started: \(detail)"
        case .timedOut(let seconds):
            return ApplyCopy.join(
                "The fleet-config engine did not finish within \(seconds) seconds and was stopped.",
                "Installers it started may still be running.",
                "A cell is re-proved only after the engine exits, so its state may not reflect them yet."
            )
        case .endedBySignal(let signal):
            return ApplyCopy.join(
                "The fleet-config engine was ended by signal \(signal) before it finished.",
                "Installers it started may still be running."
            )
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

/// Where the engine lives.  The root is a setting read from Muster's persistent preferences only
/// (`defaults write services.jays.Muster fleetConfigRepoRoot PATH`), never from the environment or
/// the launch arguments, and then falls back to `~/Code/AI-Fleet-Coordinator`.  Wherever it points,
/// `FleetConfigRootValidator` decides whether it may run.
struct FleetConfigLocation: Equatable {
    static let defaultsKey = "fleetConfigRepoRoot"

    var repoRoot: URL

    var scriptsDirectory: URL { repoRoot.appendingPathComponent("scripts", isDirectory: true) }

    /// The persistent app domain.  `UserDefaults.standard.string(forKey:)` would also see the
    /// argument domain, which any process can fill with `-fleetConfigRepoRoot X`.
    static func appPersistentSettings() -> [String: Any]? {
        guard let identifier = Bundle.main.bundleIdentifier else { return nil }
        return UserDefaults.standard.persistentDomain(forName: identifier)
    }

    static func resolve(
        persistentSettings: [String: Any]? = FleetConfigLocation.appPersistentSettings(),
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> FleetConfigLocation {
        if let saved = persistentSettings?[defaultsKey] as? String, !saved.isEmpty {
            let expanded = (saved as NSString).expandingTildeInPath
            return FleetConfigLocation(repoRoot: URL(fileURLWithPath: expanded, isDirectory: true))
        }
        return FleetConfigLocation(
            repoRoot: home.appendingPathComponent("Code/AI-Fleet-Coordinator", isDirectory: true)
        )
    }
}

// MARK: - Process

enum FleetConfigProcess {
    static let maximumOutputBytes = 8 * 1024 * 1024

    private final class Flag: @unchecked Sendable {
        private let lock = NSLock()
        private var value = false
        func set() { lock.lock(); value = true; lock.unlock() }
        var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
    }

    /// Runs one child to completion.  Output goes to files, not pipes:  nothing can deadlock on a
    /// full pipe, and a child that outlives the app never takes a SIGPIPE.  On timeout the child
    /// gets SIGTERM, then SIGKILL after a short grace.  Blocking:  call from a background queue.
    static func run(
        executable: String,
        arguments: [String],
        currentDirectory: URL,
        environment: [String: String],
        timeout: TimeInterval,
        grace: TimeInterval = 3
    ) throws -> FleetConfigCLIResult {
        let fileManager = FileManager.default
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
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.currentDirectoryURL = currentDirectory
        process.environment = environment
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

        let timedOut = Flag()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + timeout)
        timer.setEventHandler {
            // Only a process that is still running was timed out;  one that just exited was not.
            guard process.isRunning else { return }
            timedOut.set()
            process.terminate()
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + grace) {
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
        }
        timer.resume()
        process.waitUntilExit()
        timer.cancel()
        try? stdoutHandle.close()
        try? stderrHandle.close()

        return FleetConfigCLIResult(
            exitCode: process.terminationStatus,
            stdout: try read(stdoutURL),
            stderr: try read(stderrURL),
            timedOut: timedOut.isSet,
            endedBySignal: process.terminationReason == .uncaughtSignal
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

// MARK: - Runner

protocol FleetConfigRunning: Sendable {
    func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult
    /// Validates the repository root and reports the commit the engine would run from.
    func resolveRoot() async throws -> FleetConfigRootStatus
}

struct FleetConfigCLI: FleetConfigRunning {
    static let interpreter = "/usr/bin/python3"

    /// Runs the engine package from the validated root.  The root is `sys.argv[1]`;  it is removed
    /// before the engine starts, so the engine sees exactly the arguments the caller passed.
    static let bootstrap = """
    import runpy, sys
    root = sys.argv.pop(1)
    sys.path.insert(0, root + "/scripts")
    runpy.run_module("fleet_config", run_name="__main__", alter_sys=True)
    """

    var location: @Sendable () -> FleetConfigLocation
    var rootCheck: @Sendable (FleetConfigLocation) throws -> FleetConfigRootStatus

    init(
        location: @escaping @Sendable () -> FleetConfigLocation = { FleetConfigLocation.resolve() },
        rootCheck: @escaping @Sendable (FleetConfigLocation) throws -> FleetConfigRootStatus = {
            try FleetConfigRootValidator.validate($0)
        }
    ) {
        self.location = location
        self.rootCheck = rootCheck
    }

    /// The child environment is an allow-list, not a filtered copy:  nothing else is passed.
    static func childEnvironment(from environment: [String: String]) -> [String: String] {
        var result = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
        for key in ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"] {
            if let value = environment[key], !value.isEmpty { result[key] = value }
        }
        return result
    }

    static func processArguments(for invocation: FleetConfigInvocation, rootPath: String) -> [String] {
        ["-I", "-B", "-c", bootstrap, rootPath] + invocation.arguments
    }

    func resolveRoot() async throws -> FleetConfigRootStatus {
        let resolved = location()
        let check = rootCheck
        return try await Self.background { try check(resolved) }
    }

    func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult {
        let resolved = location()
        let check = rootCheck
        return try await Self.background {
            // Validated again at every start:  a reset between plan and apply is caught here.
            let root = try check(resolved)
            return try FleetConfigProcess.run(
                executable: interpreter,
                arguments: processArguments(for: invocation, rootPath: root.path),
                currentDirectory: URL(fileURLWithPath: root.path).appendingPathComponent("scripts"),
                environment: childEnvironment(from: ProcessInfo.processInfo.environment),
                timeout: invocation.timeout
            )
        }
    }

    private static func background<T: Sendable>(
        _ work: @escaping @Sendable () throws -> T
    ) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do { continuation.resume(returning: try work()) } catch { continuation.resume(throwing: error) }
            }
        }
    }
}
