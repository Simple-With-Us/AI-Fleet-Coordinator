import Foundation

/// Runs `fleet-view status --json` as a subprocess and decodes the envelope.
///
/// GUI apps do not inherit the shell's `PATH`, so the interpreter and the
/// scripts directory are absolute and the environment is explicit (the
/// AFC-389 rule).  The child runs off the main thread.  Any failure - a
/// missing CLI, a non-zero exit, unparseable output, a schema the app does
/// not know - throws, and the view shows UNKNOWN.  It never serves the last
/// green report as if it were fresh.
enum FleetViewService {
    enum ServiceError: Error {
        case scriptsNotFound
        case cliFailed(String)
        case unparseable
        case unknownSchema(Int)
        case timedOut
        case outputTooLarge
        case launchFailed(String)
    }

    /// Deadline for one CLI run, mirroring the terminate-then-SIGKILL shape
    /// of `ProcessCommandRunner` in StartupPolicy.swift.
    static let defaultTimeout: TimeInterval = 60
    static let maximumOutputBytes = 4 * 1024 * 1024
    static let killGrace: TimeInterval = 2

    /// Candidate scripts directories, first hit wins.  `MUSTER_SCRIPTS_DIR`
    /// overrides for a lane;  the integration tree is the merged default.
    static func scriptsDirectory() -> URL? {
        let fileManager = FileManager.default
        var candidates: [String] = []
        if let override = ProcessInfo.processInfo.environment["MUSTER_SCRIPTS_DIR"] {
            candidates.append(override)
        }
        if let saved = UserDefaults.standard.string(forKey: MusterSettings.scriptsDirectoryDefaultsKey), !saved.isEmpty {
            candidates.append((saved as NSString).expandingTildeInPath)
        }
        let home = fileManager.homeDirectoryForCurrentUser.path
        candidates.append("\(home)/Code/AI-Fleet-Coordinator/scripts")
        for candidate in candidates {
            let marker = "\(candidate)/fleet_view/__main__.py"
            if fileManager.fileExists(atPath: marker) {
                return URL(fileURLWithPath: candidate)
            }
        }
        return nil
    }

    static func load() async throws -> FleetViewEnvelope {
        guard let scripts = scriptsDirectory() else {
            throw ServiceError.scriptsNotFound
        }
        return try await Task.detached(priority: .userInitiated) {
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            return try fetch(
                executable: URL(fileURLWithPath: "/usr/bin/python3"),
                arguments: ["-m", "fleet_view", "status", "--json"],
                currentDirectory: scripts,
                environment: [
                    "HOME": home,
                    "PATH": "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                ],
                timeout: defaultTimeout
            )
        }.value
    }

    /// Run the CLI once and decode its envelope. Synchronous; callers keep it
    /// off the main thread. Both pipes are drained concurrently in capped
    /// buffers, so a child cannot deadlock on a full stderr pipe, and a child
    /// that outlives the deadline is terminated, then SIGKILLed.
    static func fetch(
        executable: URL,
        arguments: [String],
        currentDirectory: URL?,
        environment: [String: String],
        timeout: TimeInterval
    ) throws -> FleetViewEnvelope {
        let outcome = try runProcess(
            executable: executable,
            arguments: arguments,
            currentDirectory: currentDirectory,
            environment: environment,
            timeout: timeout
        )
        if outcome.timedOut { throw ServiceError.timedOut }
        if outcome.stdoutExceeded { throw ServiceError.outputTooLarge }
        let stderrText = String(decoding: outcome.stderr, as: UTF8.self)
        guard outcome.status == 0 || outcome.status == 3 else {
            throw ServiceError.cliFailed(
                stderrText.isEmpty ? "exit \(outcome.status)" : stderrText
            )
        }
        guard let envelope = try? JSONDecoder().decode(FleetViewEnvelope.self, from: outcome.stdout) else {
            if outcome.status != 0, !stderrText.isEmpty {
                throw ServiceError.cliFailed(stderrText)
            }
            throw ServiceError.unparseable
        }
        guard envelope.schemaVersion == 1 else {
            throw ServiceError.unknownSchema(envelope.schemaVersion)
        }
        return envelope
    }

    struct ProcessOutcome {
        let status: Int32
        let stdout: Data
        let stderr: Data
        let timedOut: Bool
        let stdoutExceeded: Bool
    }

    /// Drain both pipes concurrently, cap each, enforce the deadline.
    /// Stdout keeps its head (the envelope); stderr keeps its tail (the
    /// failure line is last).
    static func runProcess(
        executable: URL,
        arguments: [String],
        currentDirectory: URL?,
        environment: [String: String],
        timeout: TimeInterval
    ) throws -> ProcessOutcome {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = currentDirectory
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        let stdoutBuffer = CappedBuffer(mode: .head, cap: maximumOutputBytes)
        let stderrBuffer = CappedBuffer(mode: .tail, cap: maximumOutputBytes)
        let readers = DispatchGroup()
        func drain(_ handle: FileHandle, into buffer: CappedBuffer) {
            readers.enter()
            DispatchQueue.global(qos: .utility).async {
                while true {
                    let chunk = handle.readData(ofLength: 64 * 1024)
                    if chunk.isEmpty { break }
                    buffer.append(chunk)
                }
                readers.leave()
            }
        }
        drain(stdoutPipe.fileHandleForReading, into: stdoutBuffer)
        drain(stderrPipe.fileHandleForReading, into: stderrBuffer)
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        do {
            try process.run()
        } catch {
            stdoutPipe.fileHandleForWriting.closeFile()
            stderrPipe.fileHandleForWriting.closeFile()
            readers.wait()
            throw ServiceError.launchFailed(error.localizedDescription)
        }
        var timedOut = false
        if finished.wait(timeout: .now() + timeout) == .timedOut {
            timedOut = true
            if process.isRunning { process.terminate() }
            if finished.wait(timeout: .now() + killGrace) == .timedOut, process.isRunning {
                Darwin.kill(process.processIdentifier, SIGKILL)
                _ = finished.wait(timeout: .now() + killGrace)
            }
        }
        // Bound the drain: grandchildren can inherit the pipes and hold EOF
        // open long past the deadline, so after a grace period take whatever
        // arrived and leave the drain threads to finish against the buffers.
        _ = readers.wait(timeout: .now() + killGrace)
        return ProcessOutcome(
            status: process.terminationStatus,
            stdout: stdoutBuffer.data,
            stderr: stderrBuffer.data,
            timedOut: timedOut,
            stdoutExceeded: stdoutBuffer.exceeded
        )
    }

    /// Bytes from one pipe, capped. Head mode keeps the first bytes; tail
    /// mode keeps the last bytes.
    private final class CappedBuffer: @unchecked Sendable {
        enum Mode { case head, tail }
        private let lock = NSLock()
        private let mode: Mode
        private let cap: Int
        private var storage = Data()
        private(set) var exceeded = false

        init(mode: Mode, cap: Int) {
            self.mode = mode
            self.cap = cap
        }

        func append(_ chunk: Data) {
            lock.lock()
            defer { lock.unlock() }
            if storage.count + chunk.count > cap { exceeded = true }
            switch mode {
            case .head:
                let room = cap - storage.count
                if room > 0 { storage.append(chunk.prefix(room)) }
            case .tail:
                storage.append(chunk)
                if storage.count > cap { storage = storage.suffix(cap) }
            }
        }

        var data: Data {
            lock.lock()
            defer { lock.unlock() }
            return storage
        }
    }
}
