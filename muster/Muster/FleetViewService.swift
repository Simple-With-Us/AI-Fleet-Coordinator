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
    enum ServiceError: Error, LocalizedError {
        case scriptsNotFound
        case cliFailed(String)
        case unparseable
        case unknownSchema(Int)
        case timedOut
        case outputTooLarge
        case launchFailed(String)

        /// What went wrong and what to do next, in the owner's words.  The banner shows
        /// this, so every case has to say something the owner can act on or pass on.
        var errorDescription: String? {
            switch self {
            case .scriptsNotFound:
                return "Muster could not find the fleet scripts on the Mac.\u{00A0} Check that ~/Code/AI-Fleet-Coordinator is there, or choose the folder under Scripts Directory in Settings."
            case .cliFailed:
                return "The fleet-view command stopped with an error on the Mac, so no lanes, pull requests or claims were read.\u{00A0} Copy the details and send them to an agent."
            case .unparseable:
                return "The fleet-view command finished, but Muster could not read its answer.\u{00A0} Ask an agent to run fleet-view status on the Mac and check what it prints."
            case .unknownSchema(let version):
                return "The fleet-view command answered in format \(version), and this Muster reads format 1.\u{00A0} Update Muster and the checkout so the two match."
            case .timedOut:
                return "The fleet-view command did not finish within \(Int(FleetViewService.defaultTimeout)) seconds while reading lanes, pull requests and claims.\u{00A0} Refresh to try again, and if it keeps happening, ask an agent to check gh and simple-board on the Mac."
            case .outputTooLarge:
                return "The fleet-view command wrote more than \(FleetViewService.maximumOutputBytes / (1024 * 1024)) MB, which is more than Muster will read, so its answer was dropped.\u{00A0} Ask an agent to check what it is printing."
            case .launchFailed:
                return "Muster could not start \(FleetViewService.interpreterPath) on the Mac, so the fleet-view command never ran.\u{00A0} Ask an agent to check that it works."
            }
        }

        /// What the command itself said, unscrubbed.  `FleetViewFailure` scrubs it before
        /// anything reaches the screen or the clipboard.
        fileprivate var commandOutput: String? {
            switch self {
            case .cliFailed(let text), .launchFailed(let text):
                return text
            case .scriptsNotFound, .unparseable, .unknownSchema, .timedOut, .outputTooLarge:
                return nil
            }
        }
    }

    /// The interpreter the app runs the collector under.  It is absolute because GUI apps do
    /// not inherit a shell's `PATH`.  The package has to import on it:  the repo records it as
    /// Python 3.9 on the Mac (ruff.toml), and CI runs the package on a real 3.9.
    static let interpreterPath = "/usr/bin/python3"

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
                executable: URL(fileURLWithPath: interpreterPath),
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

/// What the Apps & Work banner shows about a failed refresh (FLEETVIEW-5, COPY-24).
///
/// The reason is written for the owner;  the detail is the last line the command printed,
/// which is the line a Python traceback or a CLI refusal ends on.  The text is scrubbed here,
/// before it can reach the screen or the clipboard, because a child's stderr can quote a URL
/// or a header with a credential in it, and Muster never shows one.
struct FleetViewFailure: Equatable {
    let reason: String
    /// The command's last line, scrubbed.  Nil when the command said nothing.
    let detail: String?
    /// The end of what the command printed, scrubbed, for the Copy button.
    let fullDetail: String?

    /// A failure line longer than this is cut, so a child cannot flood the banner.
    static let maximumLineCharacters = 2000
    /// How many of the command's last lines the Copy button carries.
    static let copiedLineCount = 40

    init(error: Error) {
        let service = error as? FleetViewService.ServiceError
        reason = service?.errorDescription
            ?? "The refresh failed for a reason Muster does not recognize.\u{00A0} Copy the details and send them to an agent."
        let raw = service == nil ? error.localizedDescription : (service?.commandOutput ?? "")
        let lines = raw
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .suffix(Self.copiedLineCount)
            .map { Self.bounded(Self.scrub($0)) }
        detail = lines.last
        fullDetail = lines.isEmpty ? nil : lines.joined(separator: "\n")
    }

    /// The reason and the command's own words, ready to paste to an agent.
    var copyText: String {
        guard let fullDetail else { return reason }
        return reason + "\n\n" + fullDetail
    }

    private static func bounded(_ line: String) -> String {
        guard line.count > maximumLineCharacters else { return line }
        return String(line.prefix(maximumLineCharacters)) + "\u{2026}"
    }

    // MARK: Scrubbing

    /// Prefixes of the credential formats the fleet's tools can print.
    private static let secretPrefixes = [
        "ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_", "glpat-",
        "sk-", "xoxb-", "xoxp-", "lin_api_", "st.", "AKIA", "eyJ",
    ]

    /// One line of command output with anything credential shaped replaced.
    ///
    /// It works word by word:  a URL's userinfo and secret query values, a `key=value` or
    /// `key: value` whose key names a secret, the word after `Bearer` or `Basic`, and any
    /// word or quoted piece of a word that has a credential's shape.  It over-redacts on
    /// purpose, because a hidden word costs less than a leaked token.
    static func scrub(_ line: String) -> String {
        let mark = ConfigFileReader.redactionMark
        var redactNext = false
        var out: [String] = []
        for word in line.split(separator: " ", omittingEmptySubsequences: false).map(String.init) {
            if word.isEmpty {
                out.append(word)
                continue
            }
            let lowered = word.lowercased()
            // Checked before redactNext, so `Authorization: Bearer abc` hides `abc` and
            // keeps the scheme word, which says what kind of credential it was.
            if lowered == "bearer" || lowered == "basic" {
                out.append(word)
                redactNext = true
                continue
            }
            if redactNext {
                out.append(mark)
                redactNext = false
                continue
            }
            let (scrubbed, nextIsSecret) = scrubWord(word)
            out.append(scrubbed)
            redactNext = nextIsSecret
        }
        return out.joined(separator: " ")
    }

    private static func scrubWord(_ word: String) -> (text: String, nextIsSecret: Bool) {
        let mark = ConfigFileReader.redactionMark
        if word.contains("://") {
            return (scrubURL(word), false)
        }
        let punctuation = CharacterSet(charactersIn: "\"'`,;()[]{}<>")
        // `token=abc` and `"token":"abc"`:  the value is in this word.
        if let separator = word.firstIndex(where: { $0 == "=" || $0 == ":" }) {
            let name = word[..<separator]
                .trimmingCharacters(in: punctuation.union(CharacterSet(charactersIn: "-")))
            let value = word[word.index(after: separator)...]
            if !value.isEmpty, ConfigFileReader.isSecretKey(name) {
                return (String(word[...separator]) + mark, false)
            }
        }
        // `--token abc` and `password: abc`:  the value is the next word.
        let bare = word.trimmingCharacters(in: punctuation)
        if !bare.contains("="), bare.hasPrefix("--") || bare.hasSuffix(":") {
            let name = bare.trimmingCharacters(in: CharacterSet(charactersIn: "-:"))
            if ConfigFileReader.isSecretKey(name) {
                return (word, true)
            }
        }
        var text = word
        let pieces = word.split(whereSeparator: { "=:\"'`,;()[]{}<>".contains($0) }).map(String.init)
        for piece in pieces where isSecretShaped(piece) {
            text = text.replacingOccurrences(of: piece, with: mark)
        }
        return (text, false)
    }

    private static func scrubURL(_ word: String) -> String {
        let mark = ConfigFileReader.redactionMark
        guard let scheme = word.range(of: "://") else { return word }
        let start = String(word[..<scheme.upperBound])
        var rest = String(word[scheme.upperBound...])
        let authority = rest.prefix { $0 != "/" && $0 != "?" && $0 != "#" }
        if let at = authority.lastIndex(of: "@") {
            rest = mark + String(rest[at...])
        }
        if let query = rest.firstIndex(of: "?") {
            let pairs = rest[rest.index(after: query)...].split(separator: "&")
            let hasSecret = pairs.contains { pair in
                let name = pair.prefix { $0 != "=" }
                return ConfigFileReader.isSecretKey(String(name))
            }
            if hasSecret {
                rest = String(rest[...query]) + mark
            }
        }
        return start + rest
    }

    private static func isSecretShaped(_ piece: String) -> Bool {
        guard piece.count >= 12, !piece.contains("/") else { return false }
        if secretPrefixes.contains(where: { piece.hasPrefix($0) }) { return true }
        // A long opaque run.  A dotted Python name such as subprocess.CalledProcessError is
        // long too, but a token has digits in it unless it is very long.
        guard ConfigFileReader.looksLikeSecretValue(piece) else { return false }
        return piece.count >= 40 || piece.contains(where: \.isNumber)
    }
}

/// The header's times, kept apart (FLEETVIEW-16).
///
/// When the data was collected and when a refresh last failed are different facts.  A failed
/// refresh must never read as fresh data, and a success shows the time the collector stamped
/// on its report, not the time the app happened to finish reading it.
struct FleetViewRefreshClock: Equatable {
    private(set) var lastAttemptAt: Date?
    private(set) var lastAttemptFailed = false
    private(set) var hasCollected = false
    /// The collector's own stamp from the last success.  Nil when it could not be read.
    private(set) var collectedAt: Date?

    mutating func recordSuccess(generatedAt: String, at now: Date = Date()) {
        lastAttemptAt = now
        lastAttemptFailed = false
        hasCollected = true
        collectedAt = FleetTime.parse(generatedAt)
    }

    mutating func recordFailure(at now: Date = Date()) {
        lastAttemptAt = now
        lastAttemptFailed = true
    }

    /// The header line, or nil before the first attempt ends.
    var headerText: String? {
        guard let lastAttemptAt else { return nil }
        guard lastAttemptFailed else { return collectedText }
        let attempt = "Last attempt \(FleetTime.clock(lastAttemptAt)) failed"
        guard let collectedText else { return attempt }
        return "\(attempt) · \(collectedText)"
    }

    /// Nil until a run has been collected.
    private var collectedText: String? {
        guard hasCollected else { return nil }
        guard let collectedAt else { return "Collection time unknown" }
        return "Collected \(FleetTime.clock(collectedAt))"
    }
}
