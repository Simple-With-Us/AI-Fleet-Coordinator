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
        case unparseable(String)
        case unknownSchema(Int)
        case timedOut
        case outputTooLarge
        case launchFailed(String)

        /// What went wrong and what to do next, in the owner's words.  The banner shows
        /// this, so every case has to say something the owner can act on or pass on.
        var errorDescription: String? {
            switch self {
            case .scriptsNotFound:
                return "Muster could not find the fleet scripts on the Mac.\u{00A0} Check that ~/Code/AI-Fleet-Coordinator/scripts is there, or in Settings, open Folders and choose the fleet scripts folder."
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
            case .cliFailed(let text), .launchFailed(let text), .unparseable(let text):
                return text
            case .scriptsNotFound, .unknownSchema, .timedOut, .outputTooLarge:
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

    /// The scripts folder, where it came from, and whether a folder chosen in Settings was passed over.
    struct ScriptsResolution: Equatable {
        var directory: URL?
        var source: SettingSource?
        /// The folder saved in Settings, expanded, when one is saved.
        var chosen: String?
        var chosenButUnused: Bool
        /// True when `MUSTER_SCRIPTS_DIR` was set but held no scripts, so it was passed over.
        var environmentSkipped: Bool
    }

    /// One spelling per folder, so a trailing slash, a `..` or a symlink cannot make one folder
    /// look like two.
    static func canonicalPath(_ path: String) -> String {
        URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath().path
    }

    /// Candidate scripts directories, first one holding the collector wins:  `MUSTER_SCRIPTS_DIR`
    /// overrides for a lane, then the folder saved in Settings, then the integration tree.
    static func resolveScripts(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        home: String = FileManager.default.homeDirectoryForCurrentUser.path,
        fileManager: FileManager = .default
    ) -> ScriptsResolution {
        var candidates: [(String, SettingSource)] = []
        var environmentSet = false
        // A blank or whitespace-only value counts as unset.
        if let override = environment[MusterSettings.scriptsDirectoryEnvironmentKey],
           !override.trimmingCharacters(in: .whitespaces).isEmpty {
            // Expanded like every other folder setting, so `~/...` works here too.
            candidates.append(((override as NSString).expandingTildeInPath, .environment))
            environmentSet = true
        }
        var chosen: String?
        if let saved = defaults.string(forKey: MusterSettings.scriptsDirectoryDefaultsKey), !saved.isEmpty {
            let expanded = (saved as NSString).expandingTildeInPath
            chosen = expanded
            candidates.append((expanded, .chosen))
        }
        candidates.append(("\(home)/Code/AI-Fleet-Coordinator/scripts", .standard))
        for (candidate, source) in candidates
        where fileManager.fileExists(atPath: "\(candidate)/fleet_view/__main__.py") {
            return ScriptsResolution(directory: URL(fileURLWithPath: candidate), source: source,
                                     chosen: chosen,
                                     chosenButUnused: chosen.map { canonicalPath($0) != canonicalPath(candidate) } ?? false,
                                     environmentSkipped: environmentSet && source != .environment)
        }
        return ScriptsResolution(directory: nil, source: nil, chosen: chosen, chosenButUnused: chosen != nil,
                                 environmentSkipped: environmentSet)
    }

    static func scriptsDirectory() -> URL? { resolveScripts().directory }

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
        let envelope: FleetViewEnvelope
        do {
            envelope = try JSONDecoder().decode(FleetViewEnvelope.self, from: outcome.stdout)
        } catch {
            if outcome.status != 0, !stderrText.isEmpty {
                throw ServiceError.cliFailed(stderrText)
            }
            // The decoder's words go last, because the banner shows the last line:  this is the
            // failure to expect when the app and the checkout have drifted apart.
            let decodeFailure = String(describing: error)
            throw ServiceError.unparseable(stderrText.isEmpty ? decodeFailure : stderrText + "\n" + decodeFailure)
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
struct FleetViewFailure: Equatable, Sendable {
    let reason: String
    /// The command's last line, scrubbed.  Nil when the command said nothing.
    let detail: String?
    /// The end of what the command printed, scrubbed, for the Copy button.  It says so when it
    /// starts part way through what the command printed.
    let fullDetail: String?

    /// A failure line longer than this is cut and marked, so a child cannot flood the banner.
    static let maximumLineCharacters = 2000
    /// How many of the command's last lines the Copy button carries.
    static let copiedLineCount = 40

    /// Callers keep this off the main thread, because stderr can be 4 MB in a single line.
    init(error: Error) {
        let service = error as? FleetViewService.ServiceError
        reason = service?.errorDescription
            ?? "The refresh failed for a reason Muster does not recognize.\u{00A0} Copy the details and send them to an agent."
        let raw = service == nil ? error.localizedDescription : (service?.commandOutput ?? "")
        // Bytes and not characters, because this walks everything the command printed.
        let lines = raw.utf8
            .split(whereSeparator: { $0 == 10 || $0 == 13 })
            .filter { line in line.contains { $0 != 32 && $0 != 9 } }
        let shown = lines
            .suffix(Self.copiedLineCount)
            .map { Self.scrubbedLine(String(decoding: $0, as: UTF8.self)) }
            .filter { !$0.isEmpty }
        detail = shown.last
        if shown.isEmpty {
            fullDetail = nil
        } else {
            let earlier = lines.count - Self.copiedLineCount
            let marker = earlier == 1 ? "\u{2026} 1 earlier line not shown" : "\u{2026} \(earlier) earlier lines not shown"
            fullDetail = (earlier > 0 ? [marker] + shown : shown).joined(separator: "\n")
        }
    }

    /// The reason and the command's own words, ready to paste to an agent.
    var copyText: String {
        guard let fullDetail else { return reason }
        return reason + "\n\n" + fullDetail
    }

    /// One line, scrubbed and bounded.  The cut comes before the scrub, so the scrub's cost has a
    /// ceiling however long the line is, and it drops the word it lands in, because half a token
    /// no longer has a token's shape.  A line with nowhere to cut is one word, so it is all hidden.
    private static func scrubbedLine(_ raw: String) -> String {
        let line = raw.trimmingCharacters(in: .whitespaces)
        let count = line.count
        guard count > maximumLineCharacters else { return scrub(line) }
        let kept = line.prefix(maximumLineCharacters)
        let whole = kept.lastIndex(where: \.isWhitespace).map { String(kept[..<$0]) } ?? ""
        let shown = whole.isEmpty ? ConfigFileReader.redactionMark : scrub(whole)
        return "\(shown) \u{2026} \(count - whole.count) more characters"
    }

    // MARK: Scrubbing

    /// Prefixes of the credential formats the fleet's tools can print.
    private static let secretPrefixes = [
        "ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_", "glpat-",
        "sk-", "xoxb-", "xoxp-", "lin_api_", "st.", "AKIA", "eyJ",
    ]

    /// Names that are secret in command output but not in a config file.  The config panel shares
    /// `ConfigFileReader.secretKeyNames`, so these stay here and do not widen it.
    private static let outputSecretNames: Set<String> = ["session", "sessionid", "session_id", "sid"]
    /// Query names that carry a credential or a signature in a URL, where a plain `key` or `code`
    /// is more likely to be one than it is elsewhere.
    private static let secretQueryNames: Set<String> = [
        "sig", "signature", "x-amz-signature", "x-amz-credential", "x-goog-signature",
        "code", "key", "auth",
    ]
    /// A cookie header's whole value is secret, however many pairs it has.
    private static let cookieHeaderNames: Set<String> = ["cookie", "cookies", "set-cookie"]
    /// A word that is only a separator, as in `password = abc` and `"token" : "abc"`.
    private static let separatorWords: Set<String> = ["=", ":", "=>", ":="]

    private static let punctuation = CharacterSet(charactersIn: "\"'`,;()[]{}<>\\")
    private static let dashes = CharacterSet(charactersIn: "-")
    /// What ends a name inside a word, so `{"user":"jay","token":"abc"}` and `a=1&token=abc` both
    /// find `token`.
    private static let nameBreaks = Set<Character>("\"'`,;()[]{}<>&?\\=:")
    /// What splits a word into the pieces whose shape is checked.
    private static let pieceBreaks = Set<Character>("/?#&=:\"'`,;()[]{}<>\\")
    /// The same, without the slash and the equals sign, which base64 uses inside a value.
    private static let blobBreaks = Set<Character>("?#&:\"'`,;()[]{}<>\\")

    /// One line of command output with what looks like a credential replaced.
    ///
    /// This is a best-effort filter over the shapes the table in `FleetViewCollectorTests` lists,
    /// and not a guarantee:  it works word by word on a URL's userinfo and secret query values,
    /// any `key=value` or `key: value` in a word whose key names a secret, the value after a
    /// spaced separator, the word after `Bearer` or `Basic`, the whole of a cookie header, and
    /// any word or piece of a word that has a credential's shape.  It over-redacts on purpose,
    /// because a hidden word costs less than a leaked token.
    static func scrub(_ line: String) -> String {
        let mark = ConfigFileReader.redactionMark
        var redactNext = false
        var previous = ""
        var out: [String] = []
        // Any whitespace separates words, or a credential after it would hide in a longer word.
        let spaced = String(line.map { (character: Character) -> Character in
            character.isWhitespace ? " " : character
        })
        for word in spaced.split(separator: " ", omittingEmptySubsequences: false).map(String.init) {
            if word.isEmpty {
                out.append(word)
                continue
            }
            defer { previous = word }
            let bare = word.trimmingCharacters(in: punctuation).lowercased()
            // Checked before redactNext, so `Authorization: Bearer abc` hides `abc` and keeps
            // the scheme word, which says what kind of credential it was.
            if bare == "bearer" || bare == "basic" {
                out.append(word)
                redactNext = true
                continue
            }
            if redactNext {
                // `Authorization: token abc`:  this word is the scheme, and the next is the value.
                if bare == "token" {
                    out.append(word)
                    continue
                }
                out.append(mark)
                redactNext = false
                continue
            }
            if let separator = firstSeparator(in: word, whereNameIs: isCookieHeader) {
                out.append(String(word[...separator]))
                out.append(mark)
                break
            }
            if separatorWords.contains(word) {
                let name = lastName(in: previous)
                if isCookieHeader(name) {
                    out.append(word)
                    out.append(mark)
                    break
                }
                if isSecretName(name) {
                    out.append(word)
                    redactNext = true
                    continue
                }
            }
            let (scrubbed, nextIsSecret) = scrubWord(word)
            out.append(scrubbed)
            redactNext = nextIsSecret
        }
        return out.joined(separator: " ")
    }

    private static func isSecretName(_ name: String) -> Bool {
        ConfigFileReader.isSecretKey(name) || outputSecretNames.contains(name.lowercased())
    }

    private static func isCookieHeader(_ name: String) -> Bool {
        cookieHeaderNames.contains(name.lowercased())
    }

    /// The last name in some text, without quotes, brackets, escapes or a flag's dashes.
    private static func lastName(in text: String) -> String {
        let last = text.split(whereSeparator: { nameBreaks.contains($0) }).last
        return last.map { $0.trimmingCharacters(in: dashes) } ?? ""
    }

    /// The first `=` or `:` in a word whose name matches, in one pass over the word.
    private static func firstSeparator(in word: String, whereNameIs matches: (String) -> Bool) -> String.Index? {
        var nameStart = word.startIndex
        var name = ""
        var index = word.startIndex
        while index < word.endIndex {
            let character = word[index]
            if nameBreaks.contains(character) {
                if nameStart < index {
                    name = word[nameStart..<index].trimmingCharacters(in: dashes)
                }
                if character == "=" || character == ":", matches(name) {
                    return index
                }
                nameStart = word.index(after: index)
            }
            index = word.index(after: index)
        }
        return nil
    }

    private static func scrubWord(_ word: String) -> (text: String, nextIsSecret: Bool) {
        let mark = ConfigFileReader.redactionMark
        if word.contains("://") {
            return (scrubURL(word), false)
        }
        // `token=abc` and `"token":"abc"`:  the value is in this word, and nothing after it is kept.
        // `token=`, `password:` and `"token":`:  the value is the next word.
        if let separator = firstSeparator(in: word, whereNameIs: isSecretName) {
            let value = word[word.index(after: separator)...]
            if value.trimmingCharacters(in: punctuation).isEmpty {
                return (word, true)
            }
            return (String(word[...separator]) + mark, false)
        }
        // `--token abc`:  the value is the next word.
        let bare = word.trimmingCharacters(in: punctuation)
        if bare.hasPrefix("--"), isSecretName(bare.trimmingCharacters(in: dashes)) {
            return (word, true)
        }
        var text = word
        for chunk in word.split(whereSeparator: { blobBreaks.contains($0) }).map(String.init) where isOpaqueBlob(chunk) {
            text = text.replacingOccurrences(of: chunk, with: mark)
        }
        // A slash splits pieces too, so a URL with no scheme gets each path segment checked.
        let pieces = text.split(whereSeparator: { pieceBreaks.contains($0) }).map(String.init)
        for piece in pieces where isSecretShaped(piece) {
            text = text.replacingOccurrences(of: piece, with: mark)
        }
        return (text, false)
    }

    /// Every URL in a word, and what comes before each.  A loop and not recursion, because a line
    /// can hold a URL for every few of its characters.
    private static func scrubURL(_ word: String) -> String {
        let mark = ConfigFileReader.redactionMark
        var out = ""
        var remaining = word
        while let scheme = remaining.range(of: "://") {
            let before = String(remaining[..<scheme.lowerBound])
            // A secret name before the URL opens a value that may run through it, so the URL goes too.
            if let separator = firstSeparator(in: before, whereNameIs: isSecretName) {
                return out + String(before[...separator]) + mark
            }
            out += scrubWord(before).text + "://"
            var rest = String(remaining[scheme.upperBound...])
            let authority = rest.prefix { $0 != "/" && $0 != "?" && $0 != "#" }
            if let at = authority.lastIndex(of: "@") {
                rest = mark + String(rest[at...])
            }
            if let query = rest.firstIndex(where: { $0 == "?" || $0 == "#" }) {
                let pairs = rest[rest.index(after: query)...].split(whereSeparator: { "&;?#".contains($0) })
                let hasSecret = pairs.contains { pair in
                    let name = String(pair.prefix { $0 != "=" })
                    return isSecretName(name) || secretQueryNames.contains(name.lowercased())
                }
                if hasSecret {
                    rest = String(rest[...query]) + mark
                }
            }
            if rest.contains("://") {
                remaining = rest
                continue
            }
            if let separator = firstSeparator(in: rest, whereNameIs: isSecretName),
               !rest[rest.index(after: separator)...].trimmingCharacters(in: punctuation).isEmpty {
                return out + String(rest[...separator]) + mark
            }
            // A token can also sit in the path, a query value or the fragment, as a webhook's does.
            let pieces = rest.split(whereSeparator: { pieceBreaks.contains($0) }).map(String.init)
            for piece in pieces where isSecretShaped(piece) {
                rest = rest.replacingOccurrences(of: piece, with: mark)
            }
            return out + rest
        }
        return out + remaining
    }

    /// A long run of base64 or the like, which can hold slashes:  a cloud secret key, or a line
    /// of a PEM body.  A path starts with a slash, a tilde or a dot, or has other characters in it.
    private static func isOpaqueBlob(_ text: String) -> Bool {
        guard text.count >= 40, let first = text.first, !"/~.".contains(first) else { return false }
        let inAlphabet = text.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || "+/=".contains($0)) }
        return inAlphabet && text.contains { $0.isNumber || $0 == "+" }
    }

    private static func isSecretShaped(_ piece: String) -> Bool {
        guard piece.count >= 12 else { return false }
        if secretPrefixes.contains(where: { piece.hasPrefix($0) }) { return true }
        // A short opaque run:  letters and digits together, and nothing else.  A timestamp or
        // a dashed name has other characters in it, and a plain word has no digit.
        let isAlphanumeric = piece.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber) }
        if isAlphanumeric, piece.contains(where: \.isLetter), piece.contains(where: \.isNumber) {
            return true
        }
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
