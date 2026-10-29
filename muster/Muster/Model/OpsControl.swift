import Foundation
import Combine

// Starting,  stopping,  restarting and updating the fleet's processes,  and writing a knob.
//
// Muster holds no credential and touches no process itself.  It runs two helpers and reads
// what they answer:
//
//   * `scripts/fleet-ops-control.py`,  for the Mac's LaunchAgents (run here) and the
//     server's units,  updates and Coolify redeploys (run on the server,  over ssh to the
//     `fleet-ops-ctl` alias,  whose key's forced command is `fleet-ops-control.py --from-ssh`)
//   * `python3 -m fleet_knobs set`,  for a non-secret knob
//
// Both helpers check `scripts/fleet-ops-control.json` or the knob registry themselves,  so
// nothing outside the allowlist can be touched however Muster is driven.
//
// Every action runs `--plan` first,  which changes nothing,  and Muster shows the plan.  The
// view asks for Touch ID,  then Muster plans again,  refuses when the plan changed,  and only
// then runs `--yes`.  One action runs at a time,  app-wide.

// MARK: - The route

enum OpsControlRoute {
    /// The ssh alias the server verbs go over.
    ///
    /// It is set up the same way as the doctor's read-only `fleet-tools-ro` alias
    /// (`docs/protocols/fleet-tool-parity.md`):  a `Host fleet-ops-ctl` entry in
    /// `~/.ssh/config` that names the server and the `fleet-ops-ctl` key,  whose forced
    /// command on the server is `fleet-ops-control.py --from-ssh`.  The alias is a name and
    /// not an address,  so no host is written in this source.
    static let serverAlias = "fleet-ops-ctl"
    static let ssh = "/usr/bin/ssh"
    /// The doctor's own ssh options:  never prompt,  give up quickly,  and never accept a
    /// host key that is not already known.
    static let sshOptions = [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=yes",
    ]
    /// The pinned interpreter,  the one the rest of the app runs.
    static let interpreter = "/usr/bin/python3"
    /// The helper,  relative to the repository root.
    static let scriptPath = "scripts/fleet-ops-control.py"
    /// The knob engine's entry point,  relative to the repository root.
    static let knobsEntryPoint = "scripts/fleet_knobs/__main__.py"
    /// The allowlist,  relative to the repository root.
    static let allowlistPath = "scripts/fleet-ops-control.json"

    /// A plan only reads,  so it is short.
    static let planTimeout: TimeInterval = 90
    /// An update can take minutes:  `systemctl start` waits for a oneshot unit to finish.
    static let runTimeout: TimeInterval = 900
    /// How long a timed-out helper has to exit after SIGTERM before it gets SIGKILL.
    static let killGrace: TimeInterval = 5
}

// MARK: - The actions

/// What a LaunchAgent or a unit can be asked to do.
enum OpsVerb: String, CaseIterable, Identifiable {
    case start
    case stop
    case restart

    var id: String { rawValue }

    var title: String {
        switch self {
        case .start: return "Start"
        case .stop: return "Stop"
        case .restart: return "Restart"
        }
    }
}

/// One action the owner can ask for.
enum OpsAction: Equatable {
    /// `mac start|stop|restart LABEL`,  run on this Mac.
    case macAgent(verb: OpsVerb, label: String)
    /// `server start|stop|restart UNIT`,  run on the server.
    case serverUnit(verb: OpsVerb, unit: String)
    /// `server update NAME`,  run on the server.
    case serverUpdate(name: String)
    /// `server redeploy UUID`,  run on the server.  The name is for the screen only.
    case coolifyRedeploy(uuid: String, name: String)
    /// `fleet_knobs set KEY VALUE`,  run on this Mac.
    case knobSet(key: String, value: String)

    /// True when the action runs on the server,  over ssh.
    var runsOnServer: Bool {
        switch self {
        case .serverUnit, .serverUpdate, .coolifyRedeploy: return true
        case .macAgent, .knobSet: return false
        }
    }

    /// The action,  in a few words,  for a sheet's title.
    var title: String {
        switch self {
        case .macAgent(let verb, let label): return "\(verb.title) \(label) on this Mac"
        case .serverUnit(let verb, let unit): return "\(verb.title) \(unit) on the server"
        case .serverUpdate(let name): return "Run the \(name) update on the server"
        case .coolifyRedeploy(_, let name): return "Redeploy \(name) on Coolify"
        case .knobSet(let key, let value): return "Set \(key) to \"\(value)\""
        }
    }

    /// The verb,  as the audit log names it.
    var auditVerb: String {
        switch self {
        case .macAgent(let verb, _): return "mac \(verb.rawValue)"
        case .serverUnit(let verb, _): return "server \(verb.rawValue)"
        case .serverUpdate: return "server update"
        case .coolifyRedeploy: return "server redeploy"
        case .knobSet: return "knob set"
        }
    }

    /// The target,  as the audit log names it.
    ///
    /// A redeploy is named by its resource name and not its uuid,  and a knob by its key
    /// alone:  the knob engine keeps its own audit of the values.
    var auditTarget: String {
        switch self {
        case .macAgent(_, let label): return label
        case .serverUnit(_, let unit): return unit
        case .serverUpdate(let name): return name
        case .coolifyRedeploy(_, let name): return name
        case .knobSet(let key, _): return key
        }
    }

    /// The words after the helper's name and before the mode flag.
    var verbArguments: [String] {
        switch self {
        case .macAgent(let verb, let label): return ["mac", verb.rawValue, label]
        case .serverUnit(let verb, let unit): return ["server", verb.rawValue, unit]
        case .serverUpdate(let name): return ["server", "update", name]
        case .coolifyRedeploy(let uuid, _): return ["server", "redeploy", uuid]
        case .knobSet(let key, let value): return ["set", key, value]
        }
    }
}

/// Whether a run only plans or does the thing.
enum OpsMode: String {
    case plan
    case yes

    var flag: String { "--\(rawValue)" }
}

// MARK: - The invocation

/// One process to run:  an executable,  an argv array and an explicit environment.
///
/// The arguments are never a shell string.
struct OpsInvocation: Equatable {
    var executable: String
    var arguments: [String]
    var workingDirectory: String
    var environment: [String: String]
    var timeout: TimeInterval
}

enum OpsControlError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    case helperNotFound(String)
    case launchFailed(String)
    case outputTooLarge
    case busy

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let label):
            return "The \(label) is not a value Muster will pass on,\(ServerStatusCopy.sentenceGap)so nothing was run."
        case .helperNotFound(let path):
            return ApplyCopy.join(
                "The helper was not found at \(path).",
                "Set the fleetConfigRepoRoot default or the MUSTER_REPO_ROOT variable to the repository root."
            )
        case .launchFailed(let detail):
            return "The helper could not be started: \(detail)"
        case .outputTooLarge:
            return "The helper produced more output than Muster will read."
        case .busy:
            return "Another action is still running."
        }
    }
}

/// Builds the one invocation for an action.
struct OpsCommandBuilder {
    var repoRoot: URL
    var home: URL = FileManager.default.homeDirectoryForCurrentUser
    /// The ssh agent's socket,  when this app was started with one,  so a key held in the
    /// agent can be used.
    var sshAuthSocket: String? = ProcessInfo.processInfo.environment["SSH_AUTH_SOCK"]

    /// The longest knob value Muster passes on.
    static let maximumValueLength = 1024

    /// A label,  unit,  update name or uuid is a plain token:  it starts with a letter or a
    /// digit and holds only letters,  digits,  `.`,  `_`,  `-` and `@`.  A token that could
    /// be read as a flag,  or that carries whitespace,  a path separator or a shell
    /// character,  is refused before any process exists.
    static func isSafeToken(_ value: String) -> Bool {
        guard value.count <= 200,
              let first = value.unicodeScalars.first,
              CharacterSet.alphanumerics.contains(first),
              first.isASCII else { return false }
        let allowed = CharacterSet(charactersIn: "._-@")
        return value.unicodeScalars.allSatisfy {
            $0.isASCII && (CharacterSet.alphanumerics.contains($0) || allowed.contains($0))
        }
    }

    /// A knob key is the registry's own shape:  upper case letters,  digits and `_`.
    static func isKnobKey(_ value: String) -> Bool {
        guard value.count <= 128, let first = value.unicodeScalars.first,
              isUpperCaseLetter(first) else { return false }
        return value.unicodeScalars.allSatisfy {
            isUpperCaseLetter($0) || isDigit($0) || $0 == "_"
        }
    }

    private static func isUpperCaseLetter(_ scalar: Unicode.Scalar) -> Bool {
        (65...90).contains(scalar.value)
    }

    private static func isDigit(_ scalar: Unicode.Scalar) -> Bool {
        (48...57).contains(scalar.value)
    }

    /// A knob value is passed as one argument,  so any text is safe to pass except a
    /// control character,  a value too long to be a knob,  and a value the helper's
    /// parser would read as a flag (a leading `-` that is not a number).
    static func isSafeKnobValue(_ value: String) -> Bool {
        guard value.count <= maximumValueLength else { return false }
        guard !value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            return false
        }
        if value.hasPrefix("-") {
            // Only a plain negative number:  `-inf` parses as a number and still reads as
            // a flag to the helper's parser.
            let rest = value.unicodeScalars.dropFirst()
            return !rest.isEmpty
                && rest.allSatisfy { isDigit($0) || $0 == "." }
                && Double(value) != nil
        }
        return true
    }

    /// Check an action's values before anything runs.
    static func validate(_ action: OpsAction) throws {
        switch action {
        case .macAgent(_, let label):
            guard isSafeToken(label) else { throw OpsControlError.invalidArgument("agent label") }
        case .serverUnit(_, let unit):
            guard isSafeToken(unit) else { throw OpsControlError.invalidArgument("unit name") }
        case .serverUpdate(let name):
            guard isSafeToken(name) else { throw OpsControlError.invalidArgument("update name") }
        case .coolifyRedeploy(let uuid, _):
            guard isSafeToken(uuid), !uuid.contains("@") else {
                throw OpsControlError.invalidArgument("resource id")
            }
        case .knobSet(let key, let value):
            guard isKnobKey(key) else { throw OpsControlError.invalidArgument("knob name") }
            guard isSafeKnobValue(value) else { throw OpsControlError.invalidArgument("knob value") }
        }
    }

    var helperURL: URL { repoRoot.appendingPathComponent(OpsControlRoute.scriptPath) }
    var scriptsDirectory: URL { repoRoot.appendingPathComponent("scripts", isDirectory: true) }

    /// The file a local action runs,  which must be there before anything is started.
    ///
    /// A server action runs the server's own copy,  so nothing local is needed.
    func requiredFile(for action: OpsAction) -> URL? {
        switch action {
        case .macAgent: return helperURL
        case .knobSet: return repoRoot.appendingPathComponent(OpsControlRoute.knobsEntryPoint)
        case .serverUnit, .serverUpdate, .coolifyRedeploy: return nil
        }
    }

    func checkInstalled(for action: OpsAction, fileManager: FileManager = .default) throws {
        guard let file = requiredFile(for: action) else { return }
        guard fileManager.fileExists(atPath: file.path) else {
            throw OpsControlError.helperNotFound(file.path)
        }
    }

    /// The environment a local helper sees.
    ///
    /// It is the engine's own child environment,  so the knob engine is importable the same
    /// way,  with Homebrew's directories added:  the knob engine writes through the
    /// Infisical CLI,  which Homebrew installs.  Nothing from this app's own environment is
    /// passed on,  so no variable can point a helper at a stand-in.
    var localEnvironment: [String: String] {
        let location = EngineLocation(
            root: repoRoot,
            interpreter: URL(fileURLWithPath: OpsControlRoute.interpreter)
        )
        var environment = EngineRunner.childEnvironment(location: location, home: home)
        let path = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PATH"] = path + ":/opt/homebrew/bin:/usr/local/bin"
        return environment
    }

    /// The environment ssh sees:  a plain path,  the home directory for its config,  and the
    /// agent's socket when there is one.
    var sshEnvironment: [String: String] {
        var environment: [String: String] = [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": home.path,
            "LANG": "en_US.UTF-8",
        ]
        if let socket = sshAuthSocket, !socket.isEmpty {
            environment["SSH_AUTH_SOCK"] = socket
        }
        return environment
    }

    /// The one invocation for an action and a mode.
    func invocation(for action: OpsAction, mode: OpsMode) throws -> OpsInvocation {
        try Self.validate(action)
        let timeout = mode == .plan ? OpsControlRoute.planTimeout : OpsControlRoute.runTimeout
        var tail = action.verbArguments + [mode.flag, "--json"]
        if case .knobSet(let key, let value) = action {
            // The flags go first and `--` ends them,  so the value is never read as a flag
            // even when it starts with `-`.
            tail = ["set", mode.flag, "--json", "--", key, value]
        }
        switch action {
        case .macAgent:
            return OpsInvocation(
                executable: OpsControlRoute.interpreter,
                arguments: ["-s", helperURL.path] + tail,
                workingDirectory: "/",
                environment: localEnvironment,
                timeout: timeout
            )
        case .knobSet:
            return OpsInvocation(
                executable: OpsControlRoute.interpreter,
                arguments: ["-s", "-m", "fleet_knobs"] + tail,
                workingDirectory: scriptsDirectory.path,
                environment: localEnvironment,
                timeout: timeout
            )
        case .serverUnit, .serverUpdate, .coolifyRedeploy:
            // `--` ends ssh's own options,  so nothing after the alias is read as one.  The
            // server's forced command reads the rest from SSH_ORIGINAL_COMMAND and checks it
            // against its own allowlist again.
            return OpsInvocation(
                executable: OpsControlRoute.ssh,
                arguments: OpsControlRoute.sshOptions + [OpsControlRoute.serverAlias, "--"] + tail,
                workingDirectory: "/",
                environment: sshEnvironment,
                timeout: timeout
            )
        }
    }
}

// MARK: - The run

/// What one process produced.
struct OpsProcessResult: Equatable {
    var exitCode: Int32
    var stdout: String
    var stderr: String
    var timedOut: Bool = false

    /// The last few non-empty stderr lines,  bounded.
    var stderrTail: String {
        let lines = stderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(4).joined(separator: "\n")
        return tail.count > 600 ? String(tail.suffix(600)) : tail
    }
}

protocol OpsRunning: Sendable {
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult
}

/// Runs one invocation as a child process,  with its output in private files.
struct OpsProcessRunner: OpsRunning {
    static let maximumOutputBytes = 4 * 1024 * 1024

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try Self.runBlocking(invocation))
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

    private static func runBlocking(_ invocation: OpsInvocation) throws -> OpsProcessResult {
        let fileManager = FileManager.default
        // Output goes to files and not pipes,  the way the engine wrapper does it:  nothing
        // can deadlock on a full pipe.
        let scratch = fileManager.temporaryDirectory
            .appendingPathComponent("muster-ops-\(UUID().uuidString)", isDirectory: true)
        // Every failure before the process starts is a launch failure:  nothing ran,  and
        // the controller relies on that to leave no audit row for it.
        do {
            try fileManager.createDirectory(
                at: scratch, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        } catch {
            throw OpsControlError.launchFailed(error.localizedDescription)
        }
        defer { try? fileManager.removeItem(at: scratch) }
        let stdoutURL = scratch.appendingPathComponent("stdout.txt")
        let stderrURL = scratch.appendingPathComponent("stderr.txt")
        fileManager.createFile(atPath: stdoutURL.path, contents: nil)
        fileManager.createFile(atPath: stderrURL.path, contents: nil)
        let stdoutHandle: FileHandle
        let stderrHandle: FileHandle
        do {
            stdoutHandle = try FileHandle(forWritingTo: stdoutURL)
            stderrHandle = try FileHandle(forWritingTo: stderrURL)
        } catch {
            throw OpsControlError.launchFailed(error.localizedDescription)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: invocation.executable)
        process.arguments = invocation.arguments
        process.currentDirectoryURL = URL(fileURLWithPath: invocation.workingDirectory, isDirectory: true)
        process.environment = invocation.environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            try? stdoutHandle.close()
            try? stderrHandle.close()
            throw OpsControlError.launchFailed(error.localizedDescription)
        }

        let flag = TimeoutFlag()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + invocation.timeout)
        timer.setEventHandler {
            flag.set()
            guard process.isRunning else { return }
            process.terminate()
            // A child that ignores SIGTERM gets SIGKILL a few seconds later,  so the wait
            // below always ends.
            let pid = process.processIdentifier
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + OpsControlRoute.killGrace) {
                if process.isRunning { _ = kill(pid, SIGKILL) }
            }
        }
        timer.resume()
        process.waitUntilExit()
        timer.cancel()
        try? stdoutHandle.close()
        try? stderrHandle.close()

        return OpsProcessResult(
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
        if data.count > maximumOutputBytes { throw OpsControlError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
    }
}

// MARK: - The answer

/// The helper's `--json` answer:
/// `{ "ok", "verb", "target", "commands": [[argv...]], "result", "detail" }`.
///
/// `fleet_knobs set` also answers `old`,  `old_source` and `new`,  and `lkg` after a write.
struct OpsResult: Decodable, Equatable {
    let ok: Bool
    let verb: String?
    let target: String?
    let commands: [[String]]
    let result: String?
    let detail: String?
    /// A knob's value before the write,  as the knob engine renders it.
    let old: String?
    /// Where that value came from:  `infisical`,  `lkg`,  `default` or `unknown`.
    let oldSource: String?
    /// The value the write sets.
    let new: String?
    /// How the last-known-good copy was brought up to date after a write.
    let lkg: String?

    enum CodingKeys: String, CodingKey {
        case ok, verb, target, commands, result, detail, old, new, lkg
        case oldSource = "old_source"
    }

    /// The answer decodes field by field.  A missing or unreadable `ok` is false:  an
    /// answer that does not say it worked did not work.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? container.decodeIfPresent(Bool.self, forKey: .ok)) == true
        verb = try? container.decodeIfPresent(String.self, forKey: .verb)
        target = try? container.decodeIfPresent(String.self, forKey: .target)
        commands = (try? container.decodeIfPresent([[String]].self, forKey: .commands)) ?? []
        result = try? container.decodeIfPresent(String.self, forKey: .result)
        detail = try? container.decodeIfPresent(String.self, forKey: .detail)
        old = try? container.decodeIfPresent(String.self, forKey: .old)
        oldSource = try? container.decodeIfPresent(String.self, forKey: .oldSource)
        new = try? container.decodeIfPresent(String.self, forKey: .new)
        lkg = try? container.decodeIfPresent(String.self, forKey: .lkg)
    }
}

/// What a knob write changes,  from the knob engine's answer.
struct OpsKnobChange: Equatable {
    /// The value before the write,  or nil when the knob has none or it could not be read.
    var old: String?
    /// Where `old` came from:  `infisical`,  `lkg`,  `default` or `unknown`.
    var oldSource: String?
    /// The value the write sets.
    var new: String?
    /// After a write:  `refreshed`,  `patched` or `not-refreshed:<note>`.
    var lkg: String?

    /// The old value for the plan sheet.
    var oldText: String {
        if oldSource == nil || oldSource == "unknown" { return "unknown" }
        return old.map { "\"\($0)\"" } ?? "unset"
    }

    /// The new value for the plan sheet.
    var newText: String { new.map { "\"\($0)\"" } ?? "unknown" }

    /// Where the old value came from,  in a sentence.
    var sourceText: String {
        let gap = ServerStatusCopy.sentenceGap
        switch oldSource {
        case "infisical": return "The current value was read from Infisical."
        case "lkg":
            return "The current value is from the last-known-good copy,\(gap)so it may be behind Infisical."
        case "default":
            return "The knob is not set in Infisical,\(gap)so the current value is the registry default."
        default:
            return "The current value could not be read,\(gap)so this write may replace a value not shown here."
        }
    }

    /// The note when the last-known-good copy was not brought up to date,  or nil.
    var lkgWarning: String? {
        guard let lkg = lkg, lkg.hasPrefix("not-refreshed") else { return nil }
        let note = lkg.dropFirst("not-refreshed".count).drop(while: { $0 == ":" })
        let reason = note.isEmpty ? "" : " (\(note))"
        return "The Mac's last-known-good copy was not refreshed\(reason),\(ServerStatusCopy.sentenceGap)so tools may read the old value until the next fetch."
    }
}

/// The helper's result word.
enum OpsResultKind: String {
    case planned
    case done
    case refused
    case failed
    case unknown
}

/// What one run of a helper came to,  in the app's terms.
struct OpsOutcome: Equatable {
    var kind: OpsResultKind
    /// The helper's own `ok`.
    var ok: Bool
    /// What the helper ran,  or would run,  one argv per command.
    var commands: [[String]]
    /// The helper's own words,  or the app's when the helper gave none.
    var detail: String
    var exitCode: Int32
    /// What a knob write changes,  for a knob action whose answer said;  nil otherwise.
    var knobChange: OpsKnobChange? = nil

    /// True when the run did what its mode asks for:  a plan that planned,  or a run that
    /// was done.  The helper's `ok`,  its result word and its exit code must all agree.
    func isSuccess(for mode: OpsMode) -> Bool {
        guard ok, exitCode == 0 else { return false }
        switch mode {
        case .plan: return kind == .planned
        case .yes: return kind == .done
        }
    }

    /// The commands,  one line each,  for the plan sheet.
    var commandLines: [String] {
        commands.map { $0.joined(separator: " ") }
    }
}

/// Reads a helper's answer.
enum OpsResultParser {
    /// The answer,  from the whole of stdout or,  when the helper printed more than one
    /// line,  from the last line that is a JSON object.
    static func decode(_ stdout: String) -> OpsResult? {
        let decoder = JSONDecoder()
        let trimmed = stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        if let whole = try? decoder.decode(OpsResult.self, from: Data(trimmed.utf8)) {
            return whole
        }
        let lines = trimmed.split(whereSeparator: \.isNewline).reversed()
        for line in lines {
            let text = line.trimmingCharacters(in: .whitespaces)
            guard text.hasPrefix("{") else { continue }
            if let answer = try? decoder.decode(OpsResult.self, from: Data(text.utf8)) {
                return answer
            }
        }
        return nil
    }

    /// What a run came to.
    ///
    /// Every way of not knowing ends as a failure,  never as a success:  a run that timed
    /// out,  an answer that is not JSON,  and a plan that answers "done" all read as failed.
    static func parse(
        _ result: OpsProcessResult,
        mode: OpsMode,
        action: OpsAction,
        timeout: TimeInterval
    ) -> OpsOutcome {
        if result.timedOut {
            return OpsOutcome(
                kind: .failed, ok: false, commands: [],
                detail: "The helper did not finish within \(Int(timeout)) seconds.",
                exitCode: result.exitCode
            )
        }
        guard let answer = decode(result.stdout) else {
            return OpsOutcome(
                kind: .failed, ok: false, commands: [],
                detail: unreadableDetail(result, action: action),
                exitCode: result.exitCode
            )
        }
        var kind = OpsResultKind(rawValue: answer.result ?? "") ?? .unknown
        var detail = ApplyCopy.normalize(answer.detail ?? "")
        // A plan changes nothing.  A plan that answers "done" is a helper that did something
        // it was not asked to,  and the app says so rather than calling it a plan.
        if mode == .plan && kind == .done {
            kind = .failed
            let warning = "The helper answered \"done\" to a plan."
            detail = detail.isEmpty ? warning : ApplyCopy.join(warning, detail)
        }
        if detail.isEmpty {
            detail = defaultDetail(kind: kind, exitCode: result.exitCode)
        }
        var knobChange: OpsKnobChange?
        // Only a knob answer that got as far as naming a value carries a change.
        if case .knobSet = action, answer.new != nil || answer.oldSource != nil {
            knobChange = OpsKnobChange(
                old: answer.old, oldSource: answer.oldSource, new: answer.new, lkg: answer.lkg
            )
        }
        return OpsOutcome(
            kind: kind,
            ok: answer.ok,
            commands: answer.commands,
            detail: detail,
            exitCode: result.exitCode,
            knobChange: knobChange
        )
    }

    private static func defaultDetail(kind: OpsResultKind, exitCode: Int32) -> String {
        switch kind {
        case .planned: return "Nothing has run yet."
        case .done: return "Done."
        case .refused: return "The helper refused the action."
        case .failed: return "The helper said the action failed (exit \(exitCode))."
        case .unknown: return "The helper's answer has no result this app knows (exit \(exitCode))."
        }
    }

    /// Why a run produced no answer.
    ///
    /// ssh's own errors are put in fixed words,  because its messages can carry the
    /// server's address and that does not belong on the screen.
    private static func unreadableDetail(_ result: OpsProcessResult, action: OpsAction) -> String {
        if action.runsOnServer && result.exitCode == 255 {
            let errors = result.stderr.lowercased()
            let alias = OpsControlRoute.serverAlias
            if errors.contains("could not resolve hostname") {
                return "ssh could not find the \(alias) alias.\(ServerStatusCopy.sentenceGap)Add it to ~/.ssh/config."
            }
            if errors.contains("permission denied") {
                return "The server refused the \(alias) key.\(ServerStatusCopy.sentenceGap)Install it on the server."
            }
            if errors.contains("host key verification failed") {
                return "The server's host key is not known to ssh,\(ServerStatusCopy.sentenceGap)so nothing was sent."
            }
            if errors.contains("timed out") || errors.contains("connection refused") {
                return "The server did not answer over ssh."
            }
            return "ssh could not reach the server through the \(alias) alias."
        }
        let tail = ApplyCopy.normalize(result.stderrTail)
        if tail.isEmpty {
            return "The helper printed no answer (exit \(result.exitCode))."
        }
        return ApplyCopy.join("The helper printed no answer (exit \(result.exitCode)).", tail)
    }
}

// MARK: - The allowlist

/// The allowlist,  as this Mac's checkout carries it.
///
/// The helpers check their own copy,  so this read only decides which buttons the app
/// shows:  a verb the allowlist does not name gets no button.
struct OpsAllowlist: Equatable {
    struct Entry: Equatable {
        let name: String
        let actions: [String]
    }

    struct Update: Equatable, Identifiable {
        let name: String
        let unit: String
        let scope: String

        var id: String { name }
    }

    var macAgents: [Entry] = []
    var serverUnits: [Entry] = []
    var updates: [Update] = []
    var coolifyRedeploy: Bool = false

    /// The verbs the allowlist names for a LaunchAgent,  in the app's order.
    func verbs(forMacAgent label: String) -> [OpsVerb] {
        verbs(in: macAgents, named: label)
    }

    /// The verbs the allowlist names for a unit,  in the app's order.
    func verbs(forServerUnit unit: String) -> [OpsVerb] {
        verbs(in: serverUnits, named: unit)
    }

    private func verbs(in entries: [Entry], named name: String) -> [OpsVerb] {
        guard let entry = entries.first(where: { $0.name == name }) else { return [] }
        return OpsVerb.allCases.filter { entry.actions.contains($0.rawValue) }
    }

    /// Where the allowlist is in a checkout.
    static func url(repoRoot: URL) -> URL {
        repoRoot.appendingPathComponent(OpsControlRoute.allowlistPath)
    }

    /// Read the allowlist,  or nil when it is not there or cannot be read.
    static func read(at url: URL) -> OpsAllowlist? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return parse(data)
    }

    /// Parse the allowlist.  A version this app does not know is not read:  the buttons
    /// it would draw could name verbs that mean something else.
    static func parse(_ data: Data) -> OpsAllowlist? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            (object["schemaVersion"] as? NSNumber)?.intValue == 1
        else { return nil }
        var list = OpsAllowlist()
        let mac = object["mac"] as? [String: Any]
        let server = object["server"] as? [String: Any]
        for item in (mac?["agents"] as? [[String: Any]]) ?? [] {
            guard let label = item["label"] as? String else { continue }
            list.macAgents.append(Entry(name: label, actions: (item["actions"] as? [String]) ?? []))
        }
        for item in (server?["units"] as? [[String: Any]]) ?? [] {
            guard let unit = item["unit"] as? String else { continue }
            list.serverUnits.append(Entry(name: unit, actions: (item["actions"] as? [String]) ?? []))
        }
        for item in (server?["updates"] as? [[String: Any]]) ?? [] {
            guard let name = item["name"] as? String else { continue }
            list.updates.append(Update(
                name: name,
                unit: (item["unit"] as? String) ?? "",
                scope: (item["scope"] as? String) ?? "system"
            ))
        }
        let coolify = server?["coolify"] as? [String: Any]
        list.coolifyRedeploy = (coolify?["redeploy"] as? Bool) == true
        return list
    }
}

// MARK: - The controller

/// The app-lifetime owner of the one action in flight.
///
/// Views render what is published here,  so closing a sheet never cancels a run.  The
/// Touch ID question belongs to the view:  `confirm()` is called only after the owner
/// answered it.
@MainActor
final class OpsController: ObservableObject {
    static let shared = OpsController()

    enum Phase: Equatable {
        case idle
        case planning(OpsAction)
        case review(OpsAction, OpsOutcome)
        case running(OpsAction, OpsOutcome)
        case finished(OpsAction, OpsOutcome)
        case failed(OpsAction, String)

        var action: OpsAction? {
            switch self {
            case .idle: return nil
            case .planning(let action): return action
            case .review(let action, _): return action
            case .running(let action, _): return action
            case .finished(let action, _): return action
            case .failed(let action, _): return action
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    /// A sentence about the last refusal,  shown in the sheet.
    @Published private(set) var notice: String?
    /// Counts every `--yes` run that ended,  so a view can read its data again.
    @Published private(set) var completedRuns = 0

    private let runner: OpsRunning
    private let makeBuilder: () -> OpsCommandBuilder
    private let auditURL: URL?
    private let checksInstall: Bool
    private var task: Task<Void, Never>?

    init(
        runner: OpsRunning = OpsProcessRunner(),
        makeBuilder: @escaping () -> OpsCommandBuilder = {
            OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
        },
        auditURL: URL? = OpsAuditLog.logURL(),
        checksInstall: Bool = true
    ) {
        self.runner = runner
        self.makeBuilder = makeBuilder
        self.auditURL = auditURL
        self.checksInstall = checksInstall
    }

    /// True while a helper is running.
    var isBusy: Bool {
        switch phase {
        case .planning, .running: return true
        default: return false
        }
    }

    /// True while there is anything to show in the sheet.
    var isPresenting: Bool { phase != .idle }

    /// Plan an action and show the plan.
    ///
    /// One action at a time:  a plan asked for while another action is planned,  waiting
    /// for review or running is refused,  and the one already there is left alone.
    @discardableResult
    func plan(_ action: OpsAction) -> Bool {
        switch phase {
        case .idle, .finished, .failed:
            break
        case .planning, .review, .running:
            notice = OpsControlError.busy.errorDescription
            return false
        }
        guard task == nil else { return false }
        notice = nil
        phase = .planning(action)
        task = Task { [weak self] in
            await self?.runPlan(action)
        }
        return true
    }

    /// Run the action the owner reviewed.  The view calls this after Touch ID.
    func confirm() {
        guard case .review(let action, let reviewed) = phase, task == nil else { return }
        notice = nil
        phase = .running(action, reviewed)
        task = Task { [weak self] in
            await self?.runConfirmed(action, reviewed: reviewed)
        }
    }

    /// The owner said no,  or Touch ID could not ask:  nothing runs.
    func decline(_ reason: String) {
        guard case .review(let action, _) = phase else { return }
        phase = .failed(action, reason)
    }

    /// Close the sheet.  A run in flight is never cut short,  so this does nothing while a
    /// helper is running.
    func dismiss() {
        guard !isBusy else { return }
        phase = .idle
        notice = nil
    }

    private func runPlan(_ action: OpsAction) async {
        defer { task = nil }
        do {
            let outcome = try await execute(action, mode: .plan)
            phase = outcome.isSuccess(for: .plan) ? .review(action, outcome) : .finished(action, outcome)
        } catch {
            phase = .failed(action, error.localizedDescription)
        }
    }

    private func runConfirmed(_ action: OpsAction, reviewed: OpsOutcome) async {
        defer { task = nil }
        // True once the `--yes` helper may have started:  only then can anything have run,
        // so only then does a server action get an audit row.
        var started = false
        do {
            // The plan is read again after the owner said yes.  A second plan that was
            // refused or failed is shown with its own words,  and a plan that changed in
            // between is shown again instead of being run.
            let fresh: OpsOutcome
            do {
                fresh = try await execute(action, mode: .plan)
            } catch {
                phase = .failed(action, error.localizedDescription)
                notice = "Nothing ran:\(ServerStatusCopy.sentenceGap)the plan could not be read again after Touch ID."
                return
            }
            guard fresh.isSuccess(for: .plan) else {
                phase = .finished(action, fresh)
                notice = "Nothing ran:\(ServerStatusCopy.sentenceGap)the plan read again after Touch ID did not succeed."
                return
            }
            guard
                fresh.commands == reviewed.commands,
                fresh.knobChange?.old == reviewed.knobChange?.old,
                fresh.knobChange?.oldSource == reviewed.knobChange?.oldSource
            else {
                phase = .review(action, fresh)
                notice = ApplyCopy.join(
                    "The plan changed after you reviewed it, so nothing ran.",
                    "Review the new plan before running it."
                )
                return
            }
            let invocation = try prepare(action, mode: .yes)
            started = true
            let outcome = try await launch(invocation, action: action, mode: .yes)
            phase = .finished(action, outcome)
            record(action, result: outcome.kind.rawValue)
        } catch {
            phase = .failed(action, error.localizedDescription)
            // A helper that could not be started ran nothing.
            if let control = error as? OpsControlError, case .launchFailed = control { started = false }
            if started { record(action, result: OpsResultKind.failed.rawValue) }
        }
        if started { completedRuns += 1 }
    }

    private func execute(_ action: OpsAction, mode: OpsMode) async throws -> OpsOutcome {
        let invocation = try prepare(action, mode: mode)
        return try await launch(invocation, action: action, mode: mode)
    }

    /// Check the helper is there and build its invocation.  Nothing is started.
    private func prepare(_ action: OpsAction, mode: OpsMode) throws -> OpsInvocation {
        let builder = makeBuilder()
        if checksInstall {
            try builder.checkInstalled(for: action)
        }
        return try builder.invocation(for: action, mode: mode)
    }

    /// Start the helper and read its answer.
    private func launch(_ invocation: OpsInvocation, action: OpsAction, mode: OpsMode) async throws -> OpsOutcome {
        let result = try await runner.run(invocation)
        return OpsResultParser.parse(result, mode: mode, action: action, timeout: invocation.timeout)
    }

    /// Add a row to the Mac's audit log for a server action.
    ///
    /// A local action's helper writes its own row,  so only a server action gets one here.
    /// A row that cannot be written does not change the result:  the server's own log has
    /// the action.
    private func record(_ action: OpsAction, result: String) {
        guard action.runsOnServer, let url = auditURL else { return }
        try? OpsAuditLog.append(OpsAuditLog.entry(for: action, result: result), to: url)
    }
}
