import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

// Reading the Seat Activity panel's sources.
//
// Three are local files on the Mac:  the Mac eavesdropper's own published state,  its
// configuration file and the seat partition.  The last post comes from the fleet chat CLI's
// read-only Operator service (`simple-chat muster-service`),  which is asked two things,  and
// this file is the only place that asks it anything:
//
//   * `users`       one call,  which resolves every seat's bot in one request
//   * `last-post`   one call per seat,  for that seat's last public post
//
// The panel no longer asks for Zulip presence.  Zulip refuses presence for bot users
// (`Presence is not supported for bot users`,  HTTP 400),  and every seat is a bot,  so the
// call could only ever answer Unknown.
//
// Both subcommands read.  `ChatCLI` refuses any other before a process exists,  so no call
// site can turn this panel into a writer,  and there is no path here that posts,  replies,
// reacts  or sets a status.
//
// The panel reads Zulip only as the Operator service bot (owner ruling,  AFC-654),  never as
// an agent seat and never as Jay.  There is no seat to choose and no fallback:  when the
// Operator read is unavailable,  the panel says so and reads nothing else.  The app holds no
// credential of its own.  The service reads the Operator's credential itself,  and the child
// environment this file builds carries only HOME and PATH:  no key,  no token,  no zuliprc,
// and nothing the app could print.

// MARK: - The shapes the CLI prints

/// One user the realm knows,  as `muster-service users` prints it (the `users --json` shape).
struct ChatBot: Decodable, Equatable, Sendable {
    /// The user's id.
    var userID: Int?
    /// The user's display name.
    var fullName: String?
    /// The user's address.
    var email: String?
    /// Whether the user is a bot.
    var isBot: Bool?
    /// Whether the account is active.
    var isActive: Bool?
    /// The seat tag this user belongs to,  when the fleet registry gives it one.
    var seat: String?

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case fullName = "full_name"
        case email, seat
        case isBot = "is_bot"
        case isActive = "is_active"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        userID = try? container.decodeIfPresent(Int.self, forKey: .userID)
        fullName = try? container.decodeIfPresent(String.self, forKey: .fullName)
        email = try? container.decodeIfPresent(String.self, forKey: .email)
        isBot = try? container.decodeIfPresent(Bool.self, forKey: .isBot)
        isActive = try? container.decodeIfPresent(Bool.self, forKey: .isActive)
        seat = try? container.decodeIfPresent(String.self, forKey: .seat)
    }

    /// The name to hand the Operator service:  the address,  and only the address.
    ///
    /// The address is exact,  where two bots can share a display name,  and the service's
    /// `last-post` takes nothing else.
    var lookupName: String? {
        if let email, email.contains("@") { return email }
        return nil
    }
}

/// One message,  as `muster-service last-post` prints it (the `search --json` shape).
struct ChatMessage: Decodable, Equatable, Sendable {
    /// The message's id.
    var id: Int?
    /// The channel the message went to.
    var channel: String?
    /// The topic the message went to.
    var topic: String?
    /// When it was sent.
    var timestamp: Double?
    /// The body,  which is untrusted text and is never rendered.
    var content: String?
    /// Who sent it.
    var senderFullName: String?
    /// The sender's address.
    var senderEmail: String?

    enum CodingKeys: String, CodingKey {
        case id, channel, topic, timestamp, content
        case senderFullName = "sender_full_name"
        case senderEmail = "sender_email"
    }
}

// MARK: - One call

/// One call to the fleet chat CLI's Operator service.
///
/// The verb is the service's subcommand and the first argument,  and the runner refuses any
/// verb that is not on the read-only list.  The runner puts `muster-service` in front of
/// it,  so a call can never reach an ordinary `simple-chat` verb.  Nothing here can post.
struct ChatInvocation: Equatable, Sendable {
    /// The `simple-chat` mode every call runs under:  the read-only Operator service.
    static let serviceMode = "muster-service"

    /// The arguments after the mode,  subcommand first.
    var arguments: [String]
    /// How long the call may take.
    var timeout: TimeInterval

    /// The calls this panel makes are short:  a lookup and a search.
    static let defaultTimeout: TimeInterval = 30

    /// How long a child that was asked to stop may take before it is killed.
    static let terminateGrace: TimeInterval = 2

    init(_ arguments: [String], timeout: TimeInterval = ChatInvocation.defaultTimeout) {
        self.arguments = arguments
        self.timeout = timeout
    }

    /// The verb of the call.
    var verb: String {
        arguments.first ?? ""
    }

    /// The only subcommands this panel may run.
    ///
    /// Both read,  and the service itself has no other.  The runner refuses anything
    /// else,  so a new call site cannot make this panel write to Zulip by accident.
    static let readOnlyVerbs: Set<String> = ["users", "last-post"]

    /// `muster-service users --limit N`:  every user in the realm,  deactivated ones
    /// included,  which resolves every seat's bot in one request.
    ///
    /// The service always includes deactivated accounts,  so a deactivated bot reads as
    /// deactivated and not as a seat the realm knows no bot for.
    static func users(limit: Int = 500) -> ChatInvocation {
        ChatInvocation(["users", "--limit", String(limit)])
    }

    /// `muster-service last-post --sender ADDRESS`:  the seat's newest post in a public
    /// channel,  one record,  or nothing when it has none.
    static func lastPost(sender: String) throws -> ChatInvocation {
        ChatInvocation(["last-post", "--sender", try ChatInvocation.checked(sender)])
    }

    /// Everything the child process is given:  the service mode,  then the arguments.
    var processArguments: [String] {
        [ChatInvocation.serviceMode] + arguments
    }

    /// A value that could be read as a flag,  or that carries whitespace or a control
    /// character,  is refused before any process exists.
    ///
    /// The fleet chat CLI names a user by display name or by address,  so a token here
    /// is looser than the engine's own:  it allows the punctuation an address and a
    /// display name use,  and it refuses anything that could be read as an option.
    static func checked(_ value: String) throws -> String {
        guard !value.isEmpty,
              !value.hasPrefix("-"),
              value.unicodeScalars.allSatisfy({ !CharacterSet.whitespacesAndNewlines.contains($0) }),
              value.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) })
        else {
            throw ChatCLIError.invalidArgument(value)
        }
        return value
    }
}

// MARK: - Results and errors

/// What one call to the fleet chat CLI produced.
struct ChatCLIResult: Equatable, Sendable {
    /// The child's exit code.
    var exitCode: Int32
    /// What it printed.
    var stdout: String
    /// What it complained about.
    var stderr: String
    /// Whether the call was cut off.
    var timedOut: Bool = false

    /// The last few non-empty stderr lines,  bounded,  for showing a refusal.
    var stderrTail: String {
        let lines = stderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(4).joined(separator: " ")
        return tail.count > 400 ? String(tail.suffix(400)) : tail
    }
}

/// Why the Operator read was unavailable,  from the service's own fixed refusal line.
///
/// The service prints exactly `muster-service: refused (CODE)` and nothing else on a
/// failure,  never provider text or a credential,  so the code is all there is to explain.
enum OperatorReadRefusal {
    /// The refusal code on a failed call's stderr,  or nil when the output is not one.
    static func code(in stderr: String) -> String? {
        let prefix = "muster-service: refused ("
        for line in stderr.split(whereSeparator: \.isNewline).reversed() {
            let text = line.trimmingCharacters(in: .whitespaces)
            guard text.hasPrefix(prefix), text.hasSuffix(")") else { continue }
            let code = String(text.dropFirst(prefix.count).dropLast())
            guard !code.isEmpty,
                  code.unicodeScalars.allSatisfy({ CharacterSet.lowercaseLetters.contains($0) || $0 == "-" })
            else { return nil }
            return code
        }
        return nil
    }

    /// The plain reason for one refusal code.
    static func reason(for code: String) -> String {
        switch code {
        case "pins-missing":
            return "this Mac has not pinned the Operator.  The service needs the Operator bot's user id and "
                + "address and the owner's user id:  operator_user_id,  operator_email and owner_user_id under "
                + "[daemon] in ~/.agent-sync/listener.toml (or OPERATOR_ZULIP_USER_ID and OPERATOR_ZULIP_EMAIL)."
        case "pins-unreadable":
            return "the Operator pins in ~/.agent-sync/listener.toml could not be read or are not valid."
        case "credential-unreadable":
            return "the Operator's credential could not be read from Fleet-OPS /zulip_operator on this Mac."
        case "realm-mismatch", "realm-invalid":
            return "the Operator's stored realm is not the realm Muster reads."
        case "identity-mismatch":
            return "the credential did not authenticate as the pinned Operator bot owned by the pinned owner."
        case "credential-rejected":
            return "Zulip rejected the Operator's credential."
        case "zulip-unreachable":
            return "Zulip could not be reached in time."
        case "zulip-error", "malformed-response":
            return "Zulip's answer could not be used."
        case "launched-or-remote":
            return "the service refuses to run for a launched seat or in remote mode."
        case "bad-request":
            return "the service refused the request,  so the installed simple-chat may not match this Muster."
        default:
            return "the service refused the read."
        }
    }

    /// One failed call,  in words:  the service's reason when it gave one,  else the exit
    /// code and the bounded stderr tail (an older simple-chat with no Operator service).
    static func describe(_ result: ChatCLIResult) -> String {
        if let code = code(in: result.stderr) {
            return reason(for: code)
        }
        let tail = result.stderrTail
        return "simple-chat did not run its Operator service (exit \(result.exitCode)),  "
            + "so the installed simple-chat may predate it." + (tail.isEmpty ? "" : "  \(tail)")
    }
}

/// Why a call to the fleet chat CLI could not be made or could not be read.
enum ChatCLIError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    /// The command is not at the path that was found for it.
    case commandNotFound(String)
    /// No place Muster looks holds the command.
    case notInstalled
    case launchFailed(String)
    case timedOut(Int)
    case outputTooLarge
    case writeVerbRefused(String)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let value):
            return "The \(value) value is not a plain token,  so nothing was run."
        case .commandNotFound(let path):
            return "simple-chat was not found at \(path)."
        case .notInstalled:
            return "Muster could not find simple-chat on this Mac.  "
                + "It looks in ~/.local/bin,  the managed fleet runtime and the fleet checkout."
        case .launchFailed(let detail):
            return "The fleet chat command line could not be started:  \(detail)"
        case .timedOut(let seconds):
            return "The fleet chat command line did not finish within \(seconds) seconds."
        case .outputTooLarge:
            return "The fleet chat command line produced more output than Muster will read."
        case .writeVerbRefused(let verb):
            return "The \(verb) subcommand is not one this panel may run,  so nothing was run."
        }
    }
}

// MARK: - The runner

/// Runs the fleet chat CLI.
protocol ChatRunning: Sendable {
    func run(_ invocation: ChatInvocation) async throws -> ChatCLIResult
}

/// Where the fleet chat CLI lives on this Mac.
struct ChatLocation: Equatable, Sendable {
    /// The command to run.
    var command: URL
}

/// Finds the fleet chat CLI on this Mac.
enum ChatLocator {
    /// The override a lane or a developer build may set.
    static let overrideVariable = "MUSTER_CHAT_CLI"

    /// Who the panel reads Zulip as:  always the Operator service bot,  through
    /// `simple-chat muster-service`.  There is no setting for it and no fallback.  The panel
    /// never reads as an agent seat and never as Jay,  and a launcher that started the app
    /// cannot change this,  because the child environment carries neither `AGENT_LAUNCHER`
    /// nor `AGENT_LAUNCH_SEAT`.
    static let readingIdentity = "Operator"

    /// The CLI,  or nil when it is not on this Mac.
    ///
    /// The order is deliberate:  an explicit override wins,  then the symlink every seat
    /// has on its own `PATH`,  then the managed runtime,  and last the checkout beside
    /// the app.
    static func locate(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> ChatLocation? {
        var candidates: [URL] = []
        let override = (environment[overrideVariable] ?? "").trimmingCharacters(in: .whitespaces)
        if !override.isEmpty {
            candidates.append(URL(fileURLWithPath: (override as NSString).expandingTildeInPath))
        }
        candidates.append(home.appendingPathComponent(".local/bin/simple-chat"))
        candidates.append(
            home.appendingPathComponent(
                "apps/lanes/_managed/fleet/agent-sync-runtime/scripts/simple-chat"
            )
        )
        if let engine = EngineLocator.locate(environment: environment, home: home) {
            candidates.append(engine.packageDirectory.appendingPathComponent("simple-chat"))
        }
        for candidate in candidates {
            if FileManager.default.isExecutableFile(atPath: candidate.path) {
                return ChatLocation(command: candidate.standardizedFileURL)
            }
        }
        return nil
    }
}

/// Runs the fleet chat CLI from this Mac.
///
/// The child environment is explicit and small:  HOME and PATH,  and nothing else.  That
/// is deliberate.  The app never reads a Zulip key,  so there is nothing to leak,  and
/// the Operator service reads the Operator's own credential and the host's pins itself.
struct ChatCLI: ChatRunning {
    /// The largest output this app will read.
    static let maximumOutputBytes = 8 * 1024 * 1024

    /// Where the CLI lives.
    var location: @Sendable () -> ChatLocation?
    /// The home directory the service reads the host's pins and the vault reader under.
    var home: URL

    init(
        location: @escaping @Sendable () -> ChatLocation? = { ChatLocator.locate() },
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) {
        self.location = location
        self.home = home
    }

    /// The environment every call sees.
    ///
    /// HOME stays,  because the host's pins and the vault reader live under it.  Everything
    /// else the app happens to have in its own environment is dropped:  a variable that
    /// pointed the CLI at a seat,  another realm or another checkout would make the panel
    /// read something other than what it says it reads.
    static func childEnvironment(home: URL) -> [String: String] {
        [
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                + home.appendingPathComponent(".local/bin").path,
            "HOME": home.path,
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": NSTemporaryDirectory(),
        ]
    }

    /// Run one call,  off the main thread,  with a timeout.
    func run(_ invocation: ChatInvocation) async throws -> ChatCLIResult {
        // The write guard comes first:  a verb this panel may not run never reaches a
        // process,  whatever the rest of the call looks like.
        guard ChatInvocation.readOnlyVerbs.contains(invocation.verb) else {
            throw ChatCLIError.writeVerbRefused(invocation.verb)
        }
        guard let resolved = location() else {
            throw ChatCLIError.notInstalled
        }
        let command = resolved.command
        guard FileManager.default.isExecutableFile(atPath: command.path) else {
            throw ChatCLIError.commandNotFound(command.path)
        }
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let result = try Self.runBlocking(
                        invocation,
                        command: command,
                        home: home
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
        _ invocation: ChatInvocation,
        command: URL,
        home: URL
    ) throws -> ChatCLIResult {
        let fileManager = FileManager.default
        // Output goes to files,  not pipes:  nothing can deadlock on a full pipe,  and a
        // child that outlives the app never takes a SIGPIPE when it prints its last line.
        let scratch = fileManager.temporaryDirectory
            .appendingPathComponent("muster-simple-chat-\(UUID().uuidString)", isDirectory: true)
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
        // No `--as`:  the Operator service reads as the Operator and nothing else.
        process.arguments = invocation.processArguments
        process.currentDirectoryURL = scratch
        process.environment = childEnvironment(home: home)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            try? stdoutHandle.close()
            try? stderrHandle.close()
            throw ChatCLIError.launchFailed(error.localizedDescription)
        }

        // The deadline is enforced,  not requested:  SIGTERM first,  and a child that is
        // still running after the grace period is killed,  so `waitUntilExit` always
        // returns and a stuck child can never stop the panel from reading again.
        let flag = TimeoutFlag()
        let pid = process.processIdentifier
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + invocation.timeout)
        timer.setEventHandler {
            // A child that already finished is not cut off,  whatever the clock says.
            guard process.isRunning else { return }
            flag.set()
            process.terminate()
            DispatchQueue.global(qos: .utility).asyncAfter(
                deadline: .now() + ChatInvocation.terminateGrace
            ) {
                if process.isRunning { _ = kill(pid, SIGKILL) }
            }
        }
        timer.resume()
        process.waitUntilExit()
        timer.cancel()
        try? stdoutHandle.close()
        try? stderrHandle.close()

        // Only a child the timer actually stopped is a timeout:  the flag is set only
        // when the child was still running at the deadline,  and one that exited on its
        // own keeps its output.  The exit status cannot tell the two apart,  because the
        // fleet chat CLI catches SIGTERM and exits 130 rather than dying on the signal.
        if flag.isSet {
            throw ChatCLIError.timedOut(Int(invocation.timeout.rounded(.up)))
        }
        return ChatCLIResult(
            exitCode: process.terminationStatus,
            stdout: try read(stdoutURL),
            stderr: try read(stderrURL),
            // Same rule as the throw above:  only a child the timer stopped was cut off.
            timedOut: flag.isSet
        )
    }

    private static func read(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: maximumOutputBytes + 1) ?? Data()
        if data.count > maximumOutputBytes { throw ChatCLIError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
    }
}

// MARK: - The panel's own roster

/// The seats the Seats panel shows.
///
/// This list is the panel's own,  and it is wider than the compliance grid's on purpose:  the
/// grid shows the seats the engine checks on this Mac,  and this panel shows every seat the
/// fleet runs,  wherever its eavesdropper is.  The grid's roster is never changed by it.
///
/// The seats come from five places,  each seat once,  in this order:
///
///   * the grid's roster,  as the app already holds it,  so a Mac seat keeps today's row
///   * the registry's live seats (`fleet-apps.json` `seats`,  less retired names and aliases)
///   * the registry's extra seats (`seatApps.extraSeats`),  real seats with no row in `seats`
///   * the family seats a pattern names role by role (`BF-<ROLE>`,  `GB-<ROLE>`)
///   * every seat the seat partition gives to an eavesdropper (`mac` or `server`)
///
/// Beyond the grid's own seats,  a seat the partition marks `none` is never a row:  the
/// partition keeps only retired names there.  When the partition was read,  an extra seat or a
/// family seat it does not list is not a row either:  the partition never leaves a live seat
/// with no holder,  so such a name is another login of a listed seat (`JET-CHAT` is Jet's
/// second login,  one bot with two seats),  not a seat of its own.  The Operator (`OPERATOR`)
/// is a service,  not a seat with a queue:  its health is the Operator's voice in Wake &
/// Listener and the Operator Eavesdropper's row in Services,  so it is never a row here.  A tag
/// that names a group (`GROK-BOT`),  holds a `*`,  or is not a seat name the eavesdropper
/// accepts is never a row.
enum SeatActivityRoster {
    /// Tags that are never a Seats row:  the Operator,  which is a service and holds no queue.
    static let notSeatRows: Set<String> = [OperatorOptions.seatTag]

    /// The roster,  from what was read.
    static func seats(
        gridRoster: [String],
        registry: FleetRegistryDocument?,
        partition: ListenerPartition?
    ) -> [String] {
        var roster: [String] = []
        var seen: Set<String> = []
        let retired = registry?.retiredTags ?? []
        let held = partition?.seats

        // The grid's seats are added as they are,  so their rows read exactly as before.
        for tag in gridRoster {
            let key = tag.uppercased()
            guard !tag.isEmpty, !seen.contains(key) else { continue }
            seen.insert(key)
            roster.append(tag)
        }

        // Every other seat is added by its upper-cased tag,  the name the eavesdropper and the
        // partition use for it.  `mustBeHeld` asks that a partition that was read gives the
        // seat to an eavesdropper.
        func add(_ tag: String, mustBeHeld: Bool = false) {
            let key = tag.trimmingCharacters(in: .whitespaces).uppercased()
            guard !key.isEmpty,
                  !seen.contains(key),
                  !key.contains("*"),
                  !NotASeat.contains(key),
                  !notSeatRows.contains(key),
                  !retired.contains(key),
                  ListenerPartition.isSeatName(key),
                  held?[key] != "none"
            else { return }
            if mustBeHeld, let held, held[key] == nil { return }
            seen.insert(key)
            roster.append(key)
        }

        for seat in registry?.liveSeats ?? [] where seat.aliasOf == nil {
            add(seat.tag)
        }
        for tag in (registry?.seatApps?.extraSeats ?? [:]).keys.sorted() {
            add(tag, mustBeHeld: true)
        }
        for tag in registry?.familySeats ?? [] {
            add(tag, mustBeHeld: true)
        }
        if let held {
            for tag in held.keys.sorted() {
                add(tag)
            }
        }
        return roster
    }

    /// The roster from the files the fleet checkout holds:  the registry and the seat
    /// partition beside it.  A file that cannot be read adds nothing,  and the grid's seats
    /// are always there.
    static func current(
        gridRoster: [String],
        engine: EngineLocation? = EngineLocator.locate()
    ) -> [String] {
        var registry: FleetRegistryDocument?
        if let url = engine?.registryURL,
           let data = try? Data(contentsOf: url),
           data.count <= WakeSettingsReader.maximumFileBytes {
            registry = try? JSONDecoder().decode(FleetRegistryDocument.self, from: data)
        }
        let partitionURL = engine?.root.appendingPathComponent(ListenerPartition.relativePath)
        let partition = ListenerPartition.load(from: partitionURL)
        return seats(gridRoster: gridRoster, registry: registry, partition: partition)
    }
}

// MARK: - The gate

/// A counting gate:  a refresh never opens more child processes than it says it will.
actor SeatActivityGate {
    /// How many calls may run at once.
    private let limit: Int
    /// How many are running now.
    private var active = 0
    /// The callers waiting for a slot.
    private var waiting: [CheckedContinuation<Void, Never>] = []

    init(limit: Int) {
        self.limit = max(1, limit)
    }

    /// Take a slot,  waiting for one when every slot is taken.
    func acquire() async {
        if active < limit {
            active += 1
            return
        }
        await withCheckedContinuation { continuation in
            waiting.append(continuation)
        }
    }

    /// Give the slot back.
    func release() {
        if waiting.isEmpty {
            active = max(0, active - 1)
            return
        }
        // The slot goes straight to the next caller,  so the number of open calls never
        // rises above the limit.
        let next = waiting.removeFirst()
        next.resume()
    }
}

// MARK: - The reader

/// Reads the Seat Activity panel's sources.
///
/// The reader makes at most one `users` call and one `last-post` call per seat,  and it
/// counts every call it makes:  the ceiling is written down in `requestCeiling(forSeats:)`.
struct SeatActivityReader {
    /// Where the eavesdropper's state lives.
    var location: ListenerStateLocation
    /// The fleet chat CLI.
    var runner: ChatRunning
    /// The clock.
    var now: () -> Date
    /// How many calls may run at once.
    var maximumConcurrentCalls: Int
    /// The oldest eavesdropper state the panel still believes.
    var maximumListenerAge: TimeInterval
    /// The seat partition in Muster's own checkout,  used when the Mac's eavesdropper does
    /// not name the copy it goes by.
    var partitionURL: URL?

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: ChatRunning? = nil,
        now: @escaping () -> Date = { Date() },
        maximumConcurrentCalls: Int = 8,
        maximumListenerAge: TimeInterval = SeatActivityReader.defaultMaximumListenerAge,
        partitionURL: URL? = ListenerPartition.defaultURL()
    ) {
        self.location = location
        self.runner = runner ?? ChatCLI()
        self.now = now
        self.maximumConcurrentCalls = maximumConcurrentCalls
        self.maximumListenerAge = maximumListenerAge
        self.partitionURL = partitionURL
    }

    /// What the panel says when the Operator read is unavailable.  It names the reason and
    /// says plainly that no agent seat is read instead.
    static func operatorUnavailable(_ reason: String) -> String {
        "Muster reads Zulip only as the Operator,  and the Operator read is unavailable:  "
            + reason + "  No agent seat is read instead,  so every last post is unknown."
    }

    /// The eavesdropper rewrites its state every few seconds while it runs
    /// (`daemon.write_status`),  so a file a minute old belongs to an eavesdropper that has
    /// stopped or hung,  and what it says about seats is no longer true.
    static let defaultMaximumListenerAge: TimeInterval = 60

    /// The ceiling for one refresh over a roster.
    ///
    /// The refresh asks the fleet chat CLI for at most:
    ///
    ///   * one `users` call,  which resolves every seat's bot in one request
    ///   * one `last-post` call per seat,  and only for a seat the `users` call found
    ///
    /// so the ceiling is `1 + seats`,  and nothing at all for an empty roster.  Nothing
    /// else may run,  and the reader counts what it did run.
    static func requestCeiling(forSeats seats: Int) -> Int {
        seats > 0 ? 1 + seats : 0
    }

    /// Why the panel shows no seat,  when the app has no seat list.
    static let noRosterReason = "Muster has no seat list,  so no seat is shown.  "
        + "The list comes from the fleet checkout's seat registry and seat manifest,  and neither gave a seat."

    /// Read everything the panel shows.
    func read(roster: [String]) async -> SeatActivityModel {
        var sources = SeatActivityBuilder.Sources()
        sources.roster = roster
        sources.readAt = now()
        sources.readingSeat = ChatLocator.readingIdentity
        sources.listenerPath = location.statusURL.path
        sources.requestCeiling = SeatActivityReader.requestCeiling(forSeats: roster.count)

        // The eavesdropper's own state:  one local file,  no call.
        readListenerState(into: &sources)
        // Who holds each seat:  the partition the eavesdropper says it goes by,  else the copy
        // in Muster's own checkout,  and the seat sections of the Mac's own file.
        readPartition(into: &sources)

        // The fleet chat CLI:  one `users` call,  then one call per seat.
        var requests = 0
        var bots: [String: ChatBot] = [:]
        guard !roster.isEmpty else {
            // An empty roster needs no user list:  there is no seat to ask about.  It is
            // never shown as an idle fleet:  the panel says it has no seat list.
            sources.rosterProblem = SeatActivityReader.noRosterReason
            sources.requestsMade = requests
            return SeatActivityBuilder.build(sources)
        }
        do {
            let result = try await runner.run(ChatInvocation.users())
            requests += 1
            guard result.exitCode == 0 else {
                sources.userProblem = SeatActivityReader.operatorUnavailable(
                    OperatorReadRefusal.describe(result)
                )
                sources.requestsMade = requests
                return SeatActivityBuilder.build(sources)
            }
            guard let decoded = SeatActivityReader.decodeBots(result.stdout) else {
                sources.userProblem = "The user list could not be read:  "
                    + "the Operator service printed a line that is not a user record."
                sources.requestsMade = requests
                return SeatActivityBuilder.build(sources)
            }
            bots = decoded
        } catch {
            requests += 1
            sources.userProblem = SeatActivityReader.operatorUnavailable(error.localizedDescription)
            sources.requestsMade = requests
            return SeatActivityBuilder.build(sources)
        }
        sources.bots = bots

        // The seats the realm knows a bot for.
        var seatsWithBots: [(String, ChatBot)] = []
        for seat in roster {
            guard let bot = bots[seat] else { continue }
            guard bot.isActive != false else { continue }
            guard bot.lookupName != nil else { continue }
            seatsWithBots.append((seat, bot))
        }

        let gate = SeatActivityGate(limit: maximumConcurrentCalls)
        var facts: [String: SeatFacts] = [:]
        await withTaskGroup(of: (String, SeatFacts).self) { group in
            for (seat, bot) in seatsWithBots {
                group.addTask { [runner, gate] in
                    await gate.acquire()
                    // A read that was stopped starts no more processes:  a seat still
                    // waiting for a slot costs nothing and says why it was not read.
                    guard !Task.isCancelled else {
                        await gate.release()
                        return (seat, SeatFacts(
                            lastPost: .unknown("The read was stopped before this seat's last post was read."),
                            calls: 0
                        ))
                    }
                    let read = await SeatActivityReader.readFacts(for: seat, bot: bot, runner: runner)
                    await gate.release()
                    return (seat, read)
                }
            }
            for await (seat, read) in group {
                facts[seat] = read
            }
        }
        // The calls are counted,  not assumed:  a seat the realm knows no bot for costs
        // nothing,  and a call that failed still cost a call.
        for read in facts.values {
            requests += read.calls
        }

        for seat in roster {
            if let read = facts[seat] {
                sources.lastPosts[seat] = read.lastPost
            } else {
                // The seat was never asked about,  and the row says why.
                sources.lastPosts[seat] = .unknown(SeatActivityReader.notAskedReason(for: bots[seat]))
            }
        }
        sources.requestsMade = requests
        return SeatActivityBuilder.build(sources)
    }

    /// Why a seat was never asked about Zulip.
    static func notAskedReason(for bot: ChatBot?) -> String {
        guard let bot else {
            return "The realm knows no bot for this seat,  so its last post is unknown."
        }
        if bot.isActive == false {
            return "This seat's bot account is deactivated,  so its last post is unknown."
        }
        return "The realm's record of this seat's bot names no user to ask about,  "
            + "so its last post is unknown."
    }

    /// Who holds each seat.
    ///
    /// The Mac's eavesdropper names the partition it goes by,  and that copy is the one that
    /// decides what it runs,  so it wins over the copy in Muster's own checkout.  The Mac's
    /// own file is read too:  a seat section there that names the server is refused by the
    /// Mac's eavesdropper,  whatever the partition says.
    private func readPartition(into sources: inout SeatActivityBuilder.Sources) {
        var url = partitionURL
        if let named = sources.listener?.partitionFile, !named.isEmpty {
            url = URL(fileURLWithPath: named)
        }
        // A file named by another file is not read whole when it is far larger than any real one.
        if let url,
           let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
           let size = attributes[.size] as? Int,
           size > WakeSettingsReader.maximumFileBytes {
            sources.partition = ListenerPartition(unreadable: "The file at \(url.path) is larger than Muster reads.")
        } else {
            sources.partition = ListenerPartition.load(from: url)
        }
        sources.partitionPath = url?.path ?? ""
        let configPath = location.configURL.path
        if let data = try? Data(contentsOf: location.configURL),
           data.count <= WakeSettingsReader.maximumFileBytes {
            sources.config = ListenerConfigDocument(text: String(decoding: data, as: UTF8.self))
        } else if FileManager.default.fileExists(atPath: configPath) {
            // No file is normal:  the eavesdropper then has no seat sections to refuse.  A
            // file that is there and cannot be read could hide a seat the Mac refuses.
            sources.configProblem = "The Mac eavesdropper's configuration at \(configPath) could not be read,  "
                + "so a seat it refuses because its section names the server may show under the Mac."
        }
    }

    /// The eavesdropper's own published state,  read from one local file.
    private func readListenerState(into sources: inout SeatActivityBuilder.Sources) {
        let url = location.statusURL
        let consequence = "so the state,  sessions and quota of every seat the Mac holds are unknown."
        guard let data = try? Data(contentsOf: url) else {
            sources.listenerProblem = "The Mac eavesdropper's state at \(url.path) could not be read,  " + consequence
            return
        }
        guard let document = try? JSONDecoder().decode(ListenerStateDocument.self, from: data) else {
            sources.listenerProblem = "The Mac eavesdropper's state at \(url.path) could not be read,  " + consequence
            return
        }
        // An eavesdropper that stopped leaves its last file behind,  and every lease in it
        // still says alive.  The file is only believed while the eavesdropper is still
        // writing it.
        guard let published = document.publishedAt else {
            sources.listenerProblem = "The Mac eavesdropper's state at \(url.path) does not say when it was written,  "
                + consequence
            return
        }
        let age = now().timeIntervalSince(published)
        guard abs(age) <= maximumListenerAge else {
            sources.listenerProblem = "The Mac eavesdropper's state at \(url.path) was last written "
                + SeatActivityReader.ageText(age) + ",  so the eavesdropper may have stopped,  and "
                + String(consequence.dropFirst(3))
            return
        }
        // The file names the instance that wrote it.  Only the Mac's own eavesdropper is
        // read here,  so a file another instance wrote is not taken as the Mac's.
        if let instance = document.instance, instance != ListenerPartition.thisInstance {
            sources.listenerProblem = "The eavesdropper's state at \(url.path) says it was written by the "
                + "\(SeatActivityText.label(instance)) instance,  not the Mac's,  " + consequence
            return
        }
        sources.listener = document
    }

    /// How old a file is,  in words.
    static func ageText(_ age: TimeInterval) -> String {
        if age < 0 { return "in the future by this Mac's clock" }
        let seconds = Int(age)
        if seconds < 120 { return "\(seconds) seconds ago" }
        let minutes = seconds / 60
        if minutes < 120 { return "\(minutes) minutes ago" }
        let hours = minutes / 60
        if hours < 48 { return "\(hours) hours ago" }
        return "\(hours / 24) days ago"
    }

    /// One seat's answer.
    private struct SeatFacts: Sendable {
        var lastPost: SeatFact<SeatLastPost>
        /// How many calls this seat actually cost.
        var calls: Int
    }

    /// The one call for one seat.
    private static func readFacts(
        for seat: String,
        bot: ChatBot,
        runner: ChatRunning
    ) async -> SeatFacts {
        guard let name = bot.lookupName else {
            return SeatFacts(
                lastPost: .unknown("The realm's record of this seat's bot names no user to ask about."),
                calls: 0
            )
        }
        let lastPost = await readLastPost(name: name, runner: runner)
        return SeatFacts(lastPost: lastPost.fact, calls: lastPost.calls)
    }

    /// One seat's last post,  and what it cost.
    private static func readLastPost(
        name: String,
        runner: ChatRunning
    ) async -> (fact: SeatFact<SeatLastPost>, calls: Int) {
        let invocation: ChatInvocation
        do {
            invocation = try ChatInvocation.lastPost(sender: name)
        } catch {
            return (.unknown("The last post call could not be prepared:  \(error.localizedDescription)"), 0)
        }
        do {
            let result = try await runner.run(invocation)
            guard result.exitCode == 0 else {
                return (.unknown(
                    "The Operator could not read the last post:  " + OperatorReadRefusal.describe(result)
                ), 1)
            }
            return (decodeLastPost(result.stdout), 1)
        } catch {
            return (.unknown("The last post call could not be run:  \(error.localizedDescription)"), 1)
        }
    }

    /// The longest output line the panel decodes.
    ///
    /// One record is one line,  and a Zulip message is at most 10,000 characters,  so a
    /// line longer than this is not a record the CLI meant to print.
    static let maximumLineBytes = 256 * 1024

    /// The output's records,  one per line.
    ///
    /// The CLI prints one JSON object per line with `ensure_ascii=False`,  so a string
    /// inside a record may hold U+2028,  U+2029 or U+0085 unescaped.  Only a line feed
    /// ends a record;  splitting on every Unicode newline would cut a record in two.
    static func records(_ text: String) -> [Substring] {
        text.split(separator: "\n", omittingEmptySubsequences: true)
            .map { line in
                line.hasSuffix("\r") ? line.dropLast() : line
            }
            .filter { !$0.allSatisfy { $0 == " " || $0 == "\t" } }
    }

    /// One record,  decoded,  or nil when the line is not one.
    static func decodeRecord<T: Decodable>(_ type: T.Type, _ line: Substring) -> T? {
        let trimmed = line.trimmingCharacters(in: CharacterSet(charactersIn: " \t"))
        guard trimmed.hasPrefix("{"), trimmed.utf8.count <= maximumLineBytes,
              let data = trimmed.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    /// The users the realm knows,  by seat tag,  or nil when a line is not a user record.
    ///
    /// A line that cannot be read fails the whole list:  dropping it would make that
    /// seat read as a seat the realm knows no bot for,  which is a claim the panel did not
    /// see.  When a seat has two accounts,  the active one wins over a deactivated one.
    static func decodeBots(_ text: String) -> [String: ChatBot]? {
        var bots: [String: ChatBot] = [:]
        for line in records(text) {
            guard let bot = decodeRecord(ChatBot.self, line) else { return nil }
            guard let seat = bot.seat, !seat.isEmpty else { continue }
            if let existing = bots[seat], existing.isActive != false, bot.isActive == false {
                continue
            }
            bots[seat] = bot
        }
        return bots
    }

    /// One seat's last post.
    ///
    /// The body is read and never shown:  it is untrusted text,  and the panel shows
    /// where the post went  and when  and nothing else.  The body is read for one thing
    /// only,  which is whether it reports the seat at quota.
    static func decodeLastPost(_ text: String) -> SeatFact<SeatLastPost> {
        let lines = records(text)
        let trimmedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !lines.isEmpty, trimmedText != "[]" else {
            // The call succeeded and printed nothing:  the seat has posted nothing the
            // search could see.  That is an answer,  not a failure.
            return .known(.none)
        }
        // Every line must be a message.  Anything else is output the panel cannot read,
        // and that is unknown,  never "no post".
        var messages: [ChatMessage] = []
        for line in lines {
            guard let message = decodeRecord(ChatMessage.self, line) else {
                return .unknown("The last post could not be read.")
            }
            messages.append(message)
        }
        // The service prints one record,  the newest.  Should a line ever come back with
        // more,  the CLI's order is oldest first,  so the last line is the newest post.
        guard let message = messages.last else {
            return .unknown("The last post could not be read.")
        }
        let topic = SeatActivityText.label(message.topic ?? "")
        let channel = SeatActivityText.label(message.channel ?? "")
        let label = topic.isEmpty ? channel : topic
        return .known(.post(
            at: message.timestamp.map { Date(timeIntervalSince1970: $0) },
            channel: channel,
            label: label,
            isAtQuota: SeatActivityQuota.reportsQuota(message.content ?? "")
        ))
    }
}
