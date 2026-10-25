import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

// Reading the Seat Activity panel's four sources.
//
// Two of them are read from the fleet chat CLI (`simple-chat`),  and two come from the
// listener's own published state.  The CLI is asked three things,  and this file is the
// only place that asks it anything:
//
//   * `users`       one call,  which resolves every seat's bot in one request
//   * `presence`    one call per seat
//   * `search`      one call per seat,  for that seat's last post
//
// Every one of those verbs reads.  `ChatCLI` refuses any other verb before a process
// exists,  so no call site can turn this panel into a writer,  and there is no path here
// that posts,  replies,  reacts  or sets a status.
//
// The app holds no credential of its own.  The CLI resolves the credential of the seat
// named in `--as`,  and the child environment this file builds carries only HOME and
// PATH:  no key,  no token,  no zuliprc,  and nothing the app could print.

// MARK: - The shapes the CLI prints

/// One user the realm knows,  as `users --json` prints it.
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

    /// The name to hand the CLI,  which is the address when there is one.
    ///
    /// The address is exact:  two bots can share a display name,  and the CLI refuses an
    /// ambiguous one,  so the address is the better name to ask for.
    var lookupName: String? {
        if let email, !email.isEmpty { return email }
        if let fullName, !fullName.isEmpty { return fullName }
        return nil
    }
}

/// One presence answer,  as `presence --json` prints it.
struct ChatPresenceAnswer: Decodable, Equatable, Sendable {
    /// Whose presence this is.
    var userID: Int?
    /// Zulip's own word for it:  `active`,  `idle` or `offline`.
    var status: String?
    /// When the newest client was seen,  when there ever was one.
    var timestamp: Int?

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case status, timestamp
    }
}

/// One message,  as `search --json` prints it.
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

/// One call to the fleet chat CLI.
///
/// The verb is the first argument,  and the runner refuses any verb that is not on the
/// read-only list.  Nothing here can post.
struct ChatInvocation: Equatable, Sendable {
    /// The arguments,  verb first.
    var arguments: [String]
    /// How long the call may take.
    var timeout: TimeInterval

    /// The calls this panel makes are short:  a lookup,  a presence check and a search.
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

    /// The only verbs this panel may run.
    ///
    /// Every one of them reads.  The runner refuses anything else,  so a new call site
    /// cannot make this panel write to Zulip by accident.
    static let readOnlyVerbs: Set<String> = ["users", "presence", "search"]

    /// `users --json --inactive --limit N`:  every user in the realm,  deactivated ones
    /// included,  which resolves every seat's bot in one request.
    ///
    /// `--inactive` is what lets a deactivated bot read as deactivated:  without it the
    /// CLI drops the account (`zulip_ops.find_users`),  and the seat would read as a seat
    /// the realm knows no bot for.
    static func users(limit: Int = 500) -> ChatInvocation {
        ChatInvocation(["users", "--json", "--inactive", "--limit", String(limit)])
    }

    /// `presence --json NAME`:  whether one user is active,  idle or offline.
    static func presence(name: String) throws -> ChatInvocation {
        ChatInvocation(["presence", "--json", try ChatInvocation.checked(name)])
    }

    /// `search --json --sender NAME --limit 1`:  the seat's last post.
    ///
    /// The CLI fetches the newest `limit` messages of a narrow (`anchor=newest`) and
    /// prints them oldest first,  so a limit of one is the last post and not the first.
    static func lastPost(sender: String) throws -> ChatInvocation {
        ChatInvocation([
            "search", "--json", "--sender", try ChatInvocation.checked(sender), "--limit", "1",
        ])
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

/// Why a call to the fleet chat CLI could not be made or could not be read.
enum ChatCLIError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    case commandNotFound(String)
    case launchFailed(String)
    case timedOut(Int)
    case outputTooLarge
    case writeVerbRefused(String)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let value):
            return "The \(value) value is not a plain token,  so nothing was run."
        case .commandNotFound(let path):
            return "The fleet chat command line was not found at \(path)."
        case .launchFailed(let detail):
            return "The fleet chat command line could not be started:  \(detail)"
        case .timedOut(let seconds):
            return "The fleet chat command line did not finish within \(seconds) seconds."
        case .outputTooLarge:
            return "The fleet chat command line produced more output than Muster will read."
        case .writeVerbRefused(let verb):
            return "The \(verb) verb is not one this panel may run,  so nothing was run."
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

    /// The seat the panel reads Zulip as,  when nothing says otherwise.
    static let defaultSeat = "CLAUDE"

    /// The seat the panel reads Zulip as.
    ///
    /// The panel never reads as Jay:  the CLI resolves the credential of the seat named
    /// here,  and Jay's own account is not a seat.  A launcher that started the app
    /// cannot change this,  because the child environment carries neither
    /// `AGENT_LAUNCHER` nor `AGENT_LAUNCH_SEAT`.
    static func readingSeat(environment: [String: String] = ProcessInfo.processInfo.environment) -> String {
        let override = (environment["MUSTER_ZULIP_SEAT"] ?? "").trimmingCharacters(in: .whitespaces)
        if !override.isEmpty { return override }
        return defaultSeat
    }

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
/// the CLI resolves the seat's own credential the way it does for a person at a
/// terminal.
struct ChatCLI: ChatRunning {
    /// The largest output this app will read.
    static let maximumOutputBytes = 8 * 1024 * 1024

    /// Where the CLI lives.
    var location: @Sendable () -> ChatLocation?
    /// The seat the calls read as.
    var seat: String
    /// The home directory the CLI resolves its credential under.
    var home: URL

    init(
        location: @escaping @Sendable () -> ChatLocation? = { ChatLocator.locate() },
        seat: String = ChatLocator.readingSeat(),
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) {
        self.location = location
        self.seat = seat
        self.home = home
    }

    /// The environment every call sees.
    ///
    /// HOME stays,  because the seat's credential lives under it.  Everything else the
    /// app happens to have in its own environment is dropped:  a variable that pointed
    /// the CLI at another seat,  another realm or another checkout would make the panel
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
            throw ChatCLIError.commandNotFound("the fleet chat command line")
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
                        seat: seat,
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
        seat: String,
        home: URL
    ) throws -> ChatCLIResult {
        // The seat is joined to its flag as one argument,  and it is checked like any
        // other value before anything exists:  a `MUSTER_ZULIP_SEAT` that starts with `-`
        // can never be read as a flag of its own.
        let seatArgument = "--as=" + (try ChatInvocation.checked(seat))
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
        process.arguments = [seatArgument] + invocation.arguments
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

        // Only a child the timer actually stopped is a timeout:  one that exited on its
        // own at the deadline keeps its output.
        if flag.isSet && process.terminationReason == .uncaughtSignal {
            throw ChatCLIError.timedOut(Int(invocation.timeout.rounded(.up)))
        }
        return ChatCLIResult(
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
        if data.count > maximumOutputBytes { throw ChatCLIError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
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
/// The reader makes at most one `users` call,  one `presence` call per seat  and one
/// `search` call per seat,  and it counts every call it makes:  the ceiling is written
/// down in `requestCeiling(forSeats:)`  and the panel shows both numbers.
struct SeatActivityReader {
    /// Where the listener's state lives.
    var location: ListenerStateLocation
    /// The fleet chat CLI.
    var runner: ChatRunning
    /// The clock.
    var now: () -> Date
    /// How many calls may run at once.
    var maximumConcurrentCalls: Int
    /// The oldest listener state the panel still believes.
    var maximumListenerAge: TimeInterval
    /// The seat the panel reads Zulip as,  which the panel shows.
    var readingSeat: String

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: ChatRunning? = nil,
        now: @escaping () -> Date = { Date() },
        maximumConcurrentCalls: Int = 8,
        maximumListenerAge: TimeInterval = SeatActivityReader.defaultMaximumListenerAge,
        readingSeat: String = ChatLocator.readingSeat()
    ) {
        self.location = location
        self.runner = runner ?? ChatCLI(seat: readingSeat)
        self.now = now
        self.maximumConcurrentCalls = maximumConcurrentCalls
        self.maximumListenerAge = maximumListenerAge
        self.readingSeat = readingSeat
    }

    /// The listener rewrites its state every few seconds while it runs
    /// (`daemon.write_status`),  so a file a minute old belongs to a listener that has
    /// stopped or hung,  and what it says about seats is no longer true.
    static let defaultMaximumListenerAge: TimeInterval = 60

    /// The ceiling for one refresh over a roster.
    ///
    /// The refresh asks the fleet chat CLI for at most:
    ///
    ///   * one `users` call,  which resolves every seat's bot in one request
    ///   * one `presence` call per seat
    ///   * one `search` call per seat,  and only for a seat the `users` call found
    ///
    /// so the ceiling is `1 + 2 * seats`.  Nothing else may run,  and the reader counts
    /// what it did run  so the panel can show the two numbers beside each other.
    static func requestCeiling(forSeats seats: Int) -> Int {
        1 + 2 * max(0, seats)
    }

    /// Read everything the panel shows.
    func read(roster: [String]) async -> SeatActivityModel {
        var sources = SeatActivityBuilder.Sources()
        sources.roster = roster
        sources.readAt = now()
        sources.readingSeat = readingSeat
        sources.listenerPath = location.statusURL.path
        sources.requestCeiling = SeatActivityReader.requestCeiling(forSeats: roster.count)

        // The listener's own state:  one local file,  no call.
        readListenerState(into: &sources)

        // The fleet chat CLI:  one `users` call,  then two calls per seat.
        var requests = 0
        var bots: [String: ChatBot] = [:]
        guard !roster.isEmpty else {
            // An empty roster needs no user list:  there is no seat to ask about.
            sources.requestsMade = requests
            return SeatActivityBuilder.build(sources)
        }
        do {
            let result = try await runner.run(ChatInvocation.users())
            requests += 1
            guard result.exitCode == 0 else {
                sources.userProblem = "The fleet chat command line refused the user list "
                    + "(exit \(result.exitCode)):  \(result.stderrTail)"
                sources.requestsMade = requests
                return SeatActivityBuilder.build(sources)
            }
            guard let decoded = SeatActivityReader.decodeBots(result.stdout) else {
                sources.userProblem = "The user list could not be read:  "
                    + "the fleet chat command line printed a line that is not a user record."
                sources.requestsMade = requests
                return SeatActivityBuilder.build(sources)
            }
            bots = decoded
        } catch {
            requests += 1
            sources.userProblem = "The user list could not be read:  \(error.localizedDescription)"
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
                sources.presence[seat] = read.presence
                sources.lastPosts[seat] = read.lastPost
            } else {
                // The seat was never asked about,  and the row says why.
                let reason = SeatActivityReader.notAskedReason(for: bots[seat])
                sources.presence[seat] = .unknown(reason)
                sources.lastPosts[seat] = .unknown(reason)
            }
        }
        sources.requestsMade = requests
        return SeatActivityBuilder.build(sources)
    }

    /// Why a seat was never asked about Zulip.
    static func notAskedReason(for bot: ChatBot?) -> String {
        guard let bot else {
            return "The realm knows no bot for this seat,  so its presence and its last post are unknown."
        }
        if bot.isActive == false {
            return "This seat's bot account is deactivated,  so its presence and its last post are unknown."
        }
        return "The realm's record of this seat's bot names no user to ask about,  "
            + "so its presence and its last post are unknown."
    }

    /// The listener's own published state,  read from one local file.
    private func readListenerState(into sources: inout SeatActivityBuilder.Sources) {
        let url = location.statusURL
        guard let data = try? Data(contentsOf: url) else {
            sources.listenerProblem = "The listener's state at \(url.path) could not be read,  "
                + "so the listener state and the open sessions of every seat are unknown."
            return
        }
        guard let document = try? JSONDecoder().decode(ListenerStateDocument.self, from: data) else {
            sources.listenerProblem = "The listener's state at \(url.path) could not be read,  "
                + "so the listener state and the open sessions of every seat are unknown."
            return
        }
        // A listener that stopped leaves its last file behind,  and every lease in it
        // still says alive.  The file is only believed while the listener is still
        // writing it.
        guard let published = document.publishedAt else {
            sources.listenerProblem = "The listener's state at \(url.path) does not say when it was written,  "
                + "so the listener state and the open sessions of every seat are unknown."
            return
        }
        let age = now().timeIntervalSince(published)
        guard abs(age) <= maximumListenerAge else {
            sources.listenerProblem = "The listener's state at \(url.path) was last written "
                + SeatActivityReader.ageText(age) + ",  so the listener may have stopped,  "
                + "and the listener state and the open sessions of every seat are unknown."
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

    /// One seat's two answers.
    private struct SeatFacts: Sendable {
        var presence: SeatFact<SeatPresence>
        var lastPost: SeatFact<SeatLastPost>
        /// How many calls this seat actually cost.
        var calls: Int
    }

    /// The two calls for one seat.
    private static func readFacts(
        for seat: String,
        bot: ChatBot,
        runner: ChatRunning
    ) async -> SeatFacts {
        var facts = SeatFacts(
            presence: .unknown("The seat's presence was not read."),
            lastPost: .unknown("The seat's last post was not read."),
            calls: 0
        )
        guard let name = bot.lookupName else {
            facts.presence = .unknown("The realm's record of this seat's bot names no user to ask about.")
            facts.lastPost = .unknown("The realm's record of this seat's bot names no user to ask about.")
            return facts
        }
        let presence = await readPresence(name: name, runner: runner)
        let lastPost = await readLastPost(name: name, runner: runner)
        facts.presence = presence.fact
        facts.lastPost = lastPost.fact
        facts.calls = presence.calls + lastPost.calls
        return facts
    }

    /// One seat's presence,  and what it cost.
    private static func readPresence(
        name: String,
        runner: ChatRunning
    ) async -> (fact: SeatFact<SeatPresence>, calls: Int) {
        let invocation: ChatInvocation
        do {
            invocation = try ChatInvocation.presence(name: name)
        } catch {
            return (.unknown("The presence call could not be prepared:  \(error.localizedDescription)"), 0)
        }
        do {
            let result = try await runner.run(invocation)
            guard result.exitCode == 0 else {
                return (.unknown(
                    "The presence call failed (exit \(result.exitCode)):  \(result.stderrTail)"
                ), 1)
            }
            return (decodePresence(result.stdout), 1)
        } catch {
            return (.unknown("The presence call could not be run:  \(error.localizedDescription)"), 1)
        }
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
                    "The last post call failed (exit \(result.exitCode)):  \(result.stderrTail)"
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

    /// One presence answer.
    ///
    /// A presence record that carries no timestamp is not an answer:  Zulip holds no
    /// record of this user being anywhere,  and that is unknown,  not offline.  Only a
    /// record with a time behind it can say active,  idle or offline.
    static func decodePresence(_ text: String) -> SeatFact<SeatPresence> {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8),
              let answer = try? JSONDecoder().decode(ChatPresenceAnswer.self, from: data) else {
            return .unknown("The presence answer could not be read.")
        }
        guard answer.timestamp != nil else {
            return .unknown("Zulip holds no presence record for this seat.")
        }
        switch answer.status {
        case "active": return .known(.active)
        case "idle": return .known(.idle)
        case "offline": return .known(.offline)
        case .some(let other):
            return .unknown("Zulip reported a presence this app does not read:  \(other)")
        case nil:
            return .unknown("The presence answer carried no state.")
        }
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
        // The CLI prints oldest first (`zulip_ops.search`,  `fetch` sorts by id),  so the
        // last line is the newest post.
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
