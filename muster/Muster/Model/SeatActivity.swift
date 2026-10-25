import Foundation

// The Seat Activity panel (AFC-395):  what the app can say about each seat right now.
//
// Every column has its own source,  and a source that could not be read leaves its
// column unknown.  Nothing here guesses.  "No sessions" and "offline" are answers,  and
// they are only ever given when the source said so;  a failed read is never dressed up
// as a healthy answer.
//
// The sources are:
//
//   * the listener's own published state,  `~/.agent-sync/listener/status.json`,  which
//     holds the open sessions (the leases it holds alive)  and the listener's state for
//     each seat
//   * the fleet chat CLI (`simple-chat`),  which holds the seat's Zulip presence and the
//     seat's last Zulip post
//
// The panel is read only.  It runs `users`,  `presence` and `search`,  and nothing else:
// the runner in `SeatActivityReader` refuses any other verb.

// MARK: - One fact

/// One measured fact about a seat:  what the source said,  or why it could not say.
///
/// This is the whole fail-closed rule of the panel in one type.  A fact is either an
/// answer the source gave,  or a reason it could not answer.  There is no third state,
/// and in particular there is no "empty" state that a reader could mistake for a
/// healthy answer.
enum SeatFact<Value: Equatable & Sendable>: Equatable, Sendable {
    /// The source answered.
    case known(Value)
    /// The source could not answer,  and this is why.
    case unknown(String)

    /// Whether the source answered.
    var isKnown: Bool {
        if case .known = self { return true }
        return false
    }

    /// The value,  when the source answered.
    var value: Value? {
        if case .known(let value) = self { return value }
        return nil
    }

    /// Why the source could not answer,  when it could not.
    var reason: String? {
        if case .unknown(let reason) = self { return reason }
        return nil
    }
}

// MARK: - The listener's own state

/// The listener's published state,  as `~/.agent-sync/listener/status.json` holds it.
///
/// The file is the listener's own account of itself,  rewritten every two seconds while
/// it runs.  It is not the CLI's envelope:  the CLI wraps this document in
/// `{"running":…, "status":…}`,  and the file is the inner document on its own.
struct ListenerStateDocument: Decodable, Equatable, Sendable {
    /// One seat's half of the state.
    struct Seat: Decodable, Equatable, Sendable {
        /// The seat's bot name,  as the listener knows it.
        var bot: String?
        /// Whether the listener is connected for this seat.
        var connected: Bool?
        /// Whether the kill switch stops this seat.
        var paused: Bool?
        /// The wake jobs the listener is running for this seat.
        var jobs: Int?
        /// The session leases the listener holds for this seat.
        var leases: [Lease]?

        enum CodingKeys: String, CodingKey {
            case bot, connected, paused, jobs, leases
        }

        /// A seat decodes field by field,  so one field of a type this app did not expect
        /// costs that field and not the whole seat.
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            bot = try? container.decodeIfPresent(String.self, forKey: .bot)
            connected = try? container.decodeIfPresent(Bool.self, forKey: .connected)
            paused = try? container.decodeIfPresent(Bool.self, forKey: .paused)
            jobs = try? container.decodeIfPresent(Int.self, forKey: .jobs)
            leases = try? container.decodeIfPresent([Lease].self, forKey: .leases)
        }
    }

    /// One session lease the listener holds.
    struct Lease: Decodable, Equatable, Sendable {
        /// The lease's own id.
        var leaseID: String?
        /// The platform the lease belongs to.
        var platform: String?
        /// Whether the listener judges the session behind the lease to be alive.
        ///
        /// This is the listener's own judgement,  and it is the one the panel shows:  a
        /// lease whose process is gone,  whose pid was reused or  whose heartbeat stopped
        /// is not an open session.
        var alive: Bool?
        /// Why the lease is alive or not,  in the listener's words.
        var why: String?
        /// How many topics the lease holds.
        var topics: Int?
        /// Whether a rewake watcher holds the lease.
        var watcher: Bool?
        /// How many items the lease's live inbox holds.
        var pending: Int?

        enum CodingKeys: String, CodingKey {
            case leaseID = "lease_id"
            case platform, alive, why, topics, watcher, pending
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            leaseID = try? container.decodeIfPresent(String.self, forKey: .leaseID)
            platform = try? container.decodeIfPresent(String.self, forKey: .platform)
            alive = try? container.decodeIfPresent(Bool.self, forKey: .alive)
            why = try? container.decodeIfPresent(String.self, forKey: .why)
            topics = try? container.decodeIfPresent(Int.self, forKey: .topics)
            watcher = try? container.decodeIfPresent(Bool.self, forKey: .watcher)
            pending = try? container.decodeIfPresent(Int.self, forKey: .pending)
        }
    }

    /// The kill switch,  by scope.
    struct PauseEntry: Decodable, Equatable, Sendable {
        /// Whether the pause stops wakes only.
        var wakesOnly: Bool?
        /// Who set it.
        var by: String?
        /// When it was set.
        var ts: Double?

        enum CodingKeys: String, CodingKey {
            case wakesOnly = "wakes_only"
            case by, ts
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            wakesOnly = try? container.decodeIfPresent(Bool.self, forKey: .wakesOnly)
            by = try? container.decodeIfPresent(String.self, forKey: .by)
            ts = try? container.decodeIfPresent(Double.self, forKey: .ts)
        }
    }

    /// The listener's pid.
    var pid: Int?
    /// The listener's version.
    var version: String?
    /// When the listener last published.
    var updated: Double?
    /// The seats the listener's configuration turned off.
    var disabled: [String]?
    /// The seats the listener could not start,  and why.
    var skippedSeats: [String: [String]]?
    /// The kill switch,  by scope.
    var pause: [String: PauseEntry]?
    /// The seats the listener runs,  by tag.
    var seats: [String: Seat]?

    enum CodingKeys: String, CodingKey {
        case pid, version, updated, disabled, pause, seats
        case skippedSeats = "skipped_seats"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pid = try? container.decodeIfPresent(Int.self, forKey: .pid)
        version = try? container.decodeIfPresent(String.self, forKey: .version)
        updated = try? container.decodeIfPresent(Double.self, forKey: .updated)
        disabled = try? container.decodeIfPresent([String].self, forKey: .disabled)
        skippedSeats = try? container.decodeIfPresent([String: [String]].self, forKey: .skippedSeats)
        pause = try? container.decodeIfPresent([String: PauseEntry].self, forKey: .pause)
        seats = try? container.decodeIfPresent([String: Seat].self, forKey: .seats)
    }

    /// When the listener last published,  as a date.
    var publishedAt: Date? {
        updated.map { Date(timeIntervalSince1970: $0) }
    }
}

// MARK: - What a seat reads as

/// The listener's state for one seat.
enum SeatListenerState: Equatable, Sendable {
    /// The listener is connected for this seat.
    case connected(jobs: Int)
    /// The listener runs this seat but is not connected to it.
    case disconnected
    /// The kill switch stops this seat.
    case paused
    /// The kill switch stops this seat's wakes only.  The listener still runs the seat,
    /// so it still says whether it is connected.
    case wakesPaused(connected: Bool)
    /// The listener's configuration turned this seat off.
    case off
    /// The listener could not start this seat,  and says why.
    case skipped(String)

    /// The label the column shows.
    var label: String {
        switch self {
        case .connected(let jobs):
            if jobs == 1 { return "Connected, 1 job" }
            return "Connected, \(jobs) jobs"
        case .disconnected: return "Disconnected"
        case .paused: return "Paused"
        case .wakesPaused(let connected):
            return connected ? "Connected, wakes paused" : "Disconnected, wakes paused"
        case .off: return "Off"
        case .skipped: return "Skipped"
        }
    }

    /// Whether this state is a fact the source stated,  rather than a guess.
    ///
    /// Every case is a statement the listener made,  so every case is known.  The
    /// unknown case lives in `SeatFact`,  not here.
    var isHealthy: Bool {
        switch self {
        case .connected: return true
        case .disconnected, .paused, .wakesPaused, .off, .skipped: return false
        }
    }
}

/// The seat's Zulip presence.
enum SeatPresence: Equatable, Sendable {
    /// Zulip says the seat is active.
    case active
    /// Zulip says the seat is idle.
    case idle
    /// Zulip says the seat is offline.
    case offline

    /// The label the column shows.
    var label: String {
        switch self {
        case .active: return "Active"
        case .idle: return "Idle"
        case .offline: return "Offline"
        }
    }
}

/// The seat's last Zulip post.
enum SeatLastPost: Equatable, Sendable {
    /// A post the search found.
    ///
    /// The label is where the post went,  never its body:  a message body is untrusted
    /// text,  and it is not rendered as if it were the app's own.
    case post(at: Date?, channel: String, label: String, isAtQuota: Bool)
    /// The seat's bot has no post the search could find.
    case none

    /// Whether the post reports the seat at quota.
    var isAtQuota: Bool {
        if case .post(_, _, _, let quota) = self { return quota }
        return false
    }

    /// When the post was sent.
    var at: Date? {
        if case .post(let date, _, _, _) = self { return date }
        return nil
    }

    /// The short label of the post.
    var label: String {
        switch self {
        case .post(_, _, let label, _): return label
        case .none: return "No post"
        }
    }

    /// The channel the post went to.
    var channel: String {
        switch self {
        case .post(_, let channel, _, _): return channel
        case .none: return ""
        }
    }
}

// MARK: - The row

/// One seat's row.
struct SeatActivityRow: Identifiable, Equatable, Sendable {
    /// The seat tag.
    let seat: String
    /// The bot's Zulip display name,  when the realm knows one for this seat.
    let botName: String?
    /// The number of open sessions the listener holds for this seat.
    let openSessions: SeatFact<Int>
    /// The listener's state for this seat.
    let listener: SeatFact<SeatListenerState>
    /// The seat's Zulip presence.
    let presence: SeatFact<SeatPresence>
    /// The seat's last Zulip post.
    let lastPost: SeatFact<SeatLastPost>

    var id: String { seat }

    /// Whether the seat's last post reports it at quota.
    ///
    /// The flag is read out of the last post,  because that is where a seat reports its
    /// quota in Zulip.  A post that cannot be read leaves the flag unknown:  a seat
    /// whose last post could not be fetched has not been shown to be out of quota.
    var atQuota: SeatFact<Bool> {
        switch lastPost {
        case .unknown(let reason):
            return .unknown("The seat's last post could not be read:  \(reason)")
        case .known(.none):
            return .known(false)
        case .known(.post(_, _, _, let quota)):
            return .known(quota)
        }
    }

    /// The parts of this row that could not be read,  as column names and reasons.
    var unknowns: [(column: String, reason: String)] {
        var found: [(String, String)] = []
        if let reason = openSessions.reason { found.append(("open sessions", reason)) }
        if let reason = listener.reason { found.append(("listener", reason)) }
        if let reason = presence.reason { found.append(("presence", reason)) }
        if let reason = lastPost.reason { found.append(("last post", reason)) }
        return found
    }

    /// Whether any column of this row is unknown.
    var hasUnknown: Bool {
        !unknowns.isEmpty
    }
}

// MARK: - The model

/// Everything the panel shows.
struct SeatActivityModel: Equatable, Sendable {
    /// The rows,  in roster order.
    var rows: [SeatActivityRow] = []
    /// When the panel read its sources.
    var readAt: Date?
    /// The seat the panel read Zulip as.
    var readingSeat: String = ""
    /// How many CLI calls the last refresh made.
    var requestsMade: Int = 0
    /// The ceiling the last refresh was held to.
    var requestCeiling: Int = 0
    /// What could not be read,  across every seat.
    var problems: [String] = []
    /// Where the listener's state came from.
    var listenerPath: String = ""

    /// The seats with at least one open session.
    ///
    /// This is the question the panel exists for:  which seats are actually working.
    /// A seat the listener knows but that holds no live lease is not on this list.
    var seatsWithSessions: [SeatActivityRow] {
        rows.filter { row in
            if case .known(let sessions) = row.openSessions, sessions > 0 { return true }
            return false
        }
    }
}

// MARK: - The builder

/// Turns what the reader read into what the panel shows.
enum SeatActivityBuilder {
    /// The sources the builder reads.
    struct Sources {
        /// The roster,  in the order the app shows it.
        var roster: [String] = []
        /// The listener's published state,  when it could be read.
        var listener: ListenerStateDocument?
        /// Why the listener's state could not be read.
        var listenerProblem: String?
        /// The bots the realm knows,  by seat tag.
        var bots: [String: ChatBot] = [:]
        /// Why the user list could not be read.
        var userProblem: String?
        /// Each seat's presence answer.
        var presence: [String: SeatFact<SeatPresence>] = [:]
        /// Each seat's last post answer.
        var lastPosts: [String: SeatFact<SeatLastPost>] = [:]
        /// The seat the panel read Zulip as.
        var readingSeat: String = ""
        /// How many CLI calls were made.
        var requestsMade: Int = 0
        /// The ceiling the calls were held to.
        var requestCeiling: Int = 0
        /// When the read happened.
        var readAt: Date = Date()
        /// Where the listener's state lives.
        var listenerPath: String = ""
    }

    /// Build the panel's model.
    static func build(_ sources: Sources) -> SeatActivityModel {
        var model = SeatActivityModel()
        model.readAt = sources.readAt
        model.readingSeat = sources.readingSeat
        model.requestsMade = sources.requestsMade
        model.requestCeiling = sources.requestCeiling
        model.listenerPath = sources.listenerPath

        var problems: [String] = []
        if let problem = sources.listenerProblem { problems.append(problem) }
        if let problem = sources.userProblem { problems.append(problem) }
        model.problems = problems

        model.rows = sources.roster.map { seat in
            row(for: seat, sources: sources)
        }
        return model
    }

    /// One seat's row.
    private static func row(for seat: String, sources: Sources) -> SeatActivityRow {
        let bot = sources.bots[seat]
        return SeatActivityRow(
            seat: seat,
            botName: bot?.fullName,
            openSessions: openSessions(for: seat, sources: sources),
            listener: listenerState(for: seat, sources: sources),
            presence: sources.presence[seat] ?? .unknown(SeatActivityBuilder.noPresenceReason),
            lastPost: sources.lastPosts[seat] ?? .unknown(SeatActivityBuilder.noPostReason)
        )
    }

    /// The reason a seat has no presence answer,  when the panel never asked.
    static let noPresenceReason = "The seat's presence was not read."
    /// The reason a seat has no last post,  when the panel never asked.
    static let noPostReason = "The seat's last post was not read."

    /// The number of open sessions the listener holds for a seat.
    ///
    /// The listener's own lease list is the source:  a lease is a session that attached
    /// to the fleet chat,  and the listener judges it alive or not.  A seat the listener
    /// publishes no state for has no lease list to read,  so its count is unknown and
    /// never zero:  zero would claim the seat has nothing running,  which is a claim
    /// about a seat the listener never said anything about.
    private static func openSessions(for seat: String, sources: Sources) -> SeatFact<Int> {
        guard let listener = sources.listener else {
            return .unknown(sources.listenerProblem ?? "The listener's state could not be read.")
        }
        guard let entry = listener.seats?[seat] else {
            return .unknown("The listener publishes no state for this seat,  so its open sessions are unknown.")
        }
        guard let leases = entry.leases else {
            return .unknown("The listener's state for this seat names no leases,  so its open sessions are unknown.")
        }
        let alive = leases.filter { $0.alive == true }.count
        return .known(alive)
    }

    /// The listener's state for a seat.
    private static func listenerState(for seat: String, sources: Sources) -> SeatFact<SeatListenerState> {
        guard let listener = sources.listener else {
            return .unknown(sources.listenerProblem ?? "The listener's state could not be read.")
        }
        // The kill switch comes first:  a paused seat is paused whatever else it is.  A
        // wakes-only pause leaves the seat running and connected,  so it does not hide the
        // connection state;  it only changes the label.
        let pause = pauseKind(for: seat, listener: listener)
        if pause == .full {
            return .known(.paused)
        }
        if listener.disabled?.contains(seat) == true {
            return .known(.off)
        }
        if let entry = listener.seats?[seat] {
            if entry.paused == true {
                return .known(.paused)
            }
            guard let connected = entry.connected else {
                return .unknown("The listener's state for this seat does not say whether it is connected.")
            }
            if pause == .wakesOnly {
                return .known(.wakesPaused(connected: connected))
            }
            return .known(connected ? .connected(jobs: entry.jobs ?? 0) : .disconnected)
        }
        if let problems = listener.skippedSeats?[seat], !problems.isEmpty {
            return .known(.skipped(problems.joined(separator: "; ")))
        }
        return .unknown("The listener publishes no state for this seat.")
    }

    /// How far the kill switch covers a seat.
    enum PauseKind: Equatable {
        /// The seat is stopped.
        case full
        /// Only the seat's wakes are stopped.
        case wakesOnly
    }

    /// The kill switch that covers a seat,  when one does.
    ///
    /// This is the listener's own rule (`live.paused`):  a scope covers the seat when it
    /// names the seat or is `*`,  and an entry without `wakes_only` is a full pause.  A
    /// full pause in either scope wins over a wakes-only one.
    static func pauseKind(for seat: String, listener: ListenerStateDocument) -> PauseKind? {
        guard let pause = listener.pause else { return nil }
        var found: PauseKind?
        for scope in [seat, "*"] {
            guard let entry = pause[scope] else { continue }
            if entry.wakesOnly == true {
                found = found ?? .wakesOnly
            } else {
                return .full
            }
        }
        return found
    }
}

// MARK: - The text

/// The text of a seat's last post,  kept short and safe to show.
///
/// A topic name is untrusted text like any other message text,  so it is stripped of
/// control characters and cut to a length the column can hold.  The body of a message
/// is never rendered at all.
enum SeatActivityText {
    /// The longest label the panel shows.
    static let maximumLabelLength = 48

    /// The gap between two sentences on screen:  a real U+00A0,  then a normal space.
    static let sentenceGap = "\u{00A0} "

    /// Text written with the code's two-space gaps,  made right for the screen.
    ///
    /// A run of two or more spaces after a sentence end becomes the sentence gap,  and
    /// any other run of spaces,  after a comma,  a colon or a word,  becomes one space.
    /// Reasons and problems are written in the code's house style and some come from
    /// the fleet chat tool's own messages,  so they pass through here before they are
    /// shown.
    static func display(_ text: String) -> String {
        var result = ""
        var pendingSpaces = 0
        var previous: Character?
        for character in text {
            if character == " " {
                pendingSpaces += 1
                continue
            }
            if pendingSpaces > 0 {
                if pendingSpaces >= 2, let previous, ".!?".contains(previous) {
                    result += sentenceGap
                } else {
                    result += " "
                }
                pendingSpaces = 0
            }
            result.append(character)
            previous = character
        }
        return result
    }

    /// A label, safe to show:  one line,  no control characters,  cut short.
    static func label(_ text: String) -> String {
        let flattened = text
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\t", with: " ")
            .unicodeScalars
            .filter { scalar in
                !CharacterSet.controlCharacters.contains(scalar)
            }
            .map(String.init)
            .joined()
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard flattened.count > maximumLabelLength else { return flattened }
        let cut = flattened.prefix(maximumLabelLength)
        return String(cut) + "…"
    }
}

// MARK: - The at-quota question

/// How a seat's last post is read for a quota report.
///
/// This is a heuristic,  and it is a narrow one:  a seat says it is out of quota in a
/// handful of ways,  and only those count.  A seat that reports its quota another way
/// reads as no report,  which is not a claim that the seat has quota:  the panel says
/// what it read and stops there.
enum SeatActivityQuota {
    /// The phrases that count as a quota report.
    static let markers = [
        "at quota",
        "at-quota",
        "at my quota",
        "hit quota",
        "hit the quota",
        "quota exhausted",
        "quota is exhausted",
        "out of quota",
        "no quota left",
        "out of credits",
        "usage limit reached",
    ]

    /// The words that turn a marker around when they come shortly before it,  so "not
    /// at quota",  "not yet at quota" and "no longer out of credits" do not count.
    static let negations: Set<String> = [
        "not", "never", "isn't", "aren't", "wasn't", "weren't", "no", "longer",
    ]

    /// How many words before a marker are read for a negation.
    static let negationWindow = 3

    /// The markers as whole-word patterns:  "that quota" does not hold "at quota".
    private static let patterns: [NSRegularExpression] = markers.compactMap { marker in
        let pattern = "\\b\(NSRegularExpression.escapedPattern(for: marker))\\b"
        return try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
    }

    /// Whether a message body reports the seat at quota.
    ///
    /// A marker counts unless one of the few words before it,  inside the same
    /// sentence,  is a negation.
    static func reportsQuota(_ body: String) -> Bool {
        let range = NSRange(body.startIndex..., in: body)
        for pattern in patterns {
            for match in pattern.matches(in: body, options: [], range: range) {
                guard let markerRange = Range(match.range, in: body) else { continue }
                if !isNegated(before: body[..<markerRange.lowerBound]) { return true }
            }
        }
        return false
    }

    /// Whether the words just before a marker negate it.
    static func isNegated(before prefix: Substring) -> Bool {
        // Only the current sentence or clause counts.
        let clause = prefix.split(omittingEmptySubsequences: false, whereSeparator: { ".!?;\n".contains($0) })
            .last ?? ""
        let words = clause.lowercased()
            .replacingOccurrences(of: "\u{2019}", with: "'")
            .split(whereSeparator: { !$0.isLetter && $0 != "'" })
            .suffix(negationWindow)
        return words.contains { negations.contains(String($0)) }
    }
}
