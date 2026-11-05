import Foundation

// The Seat Activity panel (AFC-395):  what the app can say about each seat right now.
//
// Every column has its own source,  and a source that could not be read leaves its
// column unknown.  Nothing here guesses.  "No sessions" is an answer,  and it is only
// ever given when the source said so;  a failed read is never dressed up as a healthy
// answer.
//
// The sources are:
//
//   * the Mac eavesdropper's own published state,  `~/.agent-sync/listener/status.json`,
//     which holds the open sessions (the leases it holds alive),  the state of each seat
//     it runs,  the wake it is running and the provider quota it reads
//   * the seat partition (`docs/protocols/agent-sync-partition.toml`),  which says which
//     machine's eavesdropper holds each seat
//   * the fleet chat CLI (`simple-chat`),  which holds the seat's last Zulip post
//
// Muster runs on the Mac and reads only the Mac's eavesdropper.  A seat the partition
// gives to the server is shown as held by the server,  and its eavesdropper columns say
// that they are not read here:  never Unknown in orange,  and never zero.  A session that
// runs on the server attaches to no eavesdropper this Mac can read,  so every session
// count here is a count of sessions on the Mac.
//
// The panel is read only.  It runs `users` and `search`,  and nothing else:  the runner
// in `SeatActivityReader` refuses any other verb.

// MARK: - One fact

/// One measured fact about a seat:  what the source said,  why it could not say,  or
/// that the answer lives where this Mac does not read.
///
/// This is the whole fail-closed rule of the panel in one type.  There is no "empty"
/// state that a reader could mistake for a healthy answer:  `notHere` is a statement
/// that the answer is somewhere else,  and it is never shown as a zero or as OK.
enum SeatFact<Value: Equatable & Sendable>: Equatable, Sendable {
    /// The source answered.
    case known(Value)
    /// The source could not answer,  and this is why.
    case unknown(String)
    /// The answer is held by another machine's eavesdropper,  or by none,  so the Mac's
    /// own source cannot give it.  The text says where.
    case notHere(String)

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

    /// Why the source could not answer,  when a read failed.
    var reason: String? {
        if case .unknown(let reason) = self { return reason }
        return nil
    }

    /// Where the answer is instead,  when it is not on this Mac.
    var notHereReason: String? {
        if case .notHere(let reason) = self { return reason }
        return nil
    }
}

// MARK: - The eavesdropper's own state

/// The eavesdropper's published state,  as `~/.agent-sync/listener/status.json` holds it.
///
/// The file is the eavesdropper's own account of itself,  rewritten every two seconds while
/// it runs (`daemon.status`).  It is not the CLI's envelope:  the CLI wraps this document in
/// `{"running":…, "status":…}`,  and the file is the inner document on its own.
struct ListenerStateDocument: Decodable, Equatable, Sendable {
    /// The provider quota the eavesdropper reads for a seat (`adapters.provider_quota`).
    struct Quota: Decodable, Equatable, Sendable {
        /// Whether the quota snapshot can speak for this seat's provider at all.
        var known: Bool?
        /// The provider's own verdict that it will not serve.
        var exhausted: Bool?
        /// The lowest percentage left across the provider's windows.
        var remaining: Double?
        /// When the provider's window resets,  as the snapshot wrote it.
        var resetAt: String?
        /// The provider's reason,  when it gave one.
        var reason: String?

        enum CodingKeys: String, CodingKey {
            case known, exhausted, remaining, reason
            case resetAt = "reset_at"
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            known = try? container.decodeIfPresent(Bool.self, forKey: .known)
            exhausted = try? container.decodeIfPresent(Bool.self, forKey: .exhausted)
            remaining = try? container.decodeIfPresent(Double.self, forKey: .remaining)
            resetAt = try? container.decodeIfPresent(String.self, forKey: .resetAt)
            reason = try? container.decodeIfPresent(String.self, forKey: .reason)
        }

        init(known: Bool?, exhausted: Bool?, remaining: Double?, resetAt: String?, reason: String?) {
            self.known = known
            self.exhausted = exhausted
            self.remaining = remaining
            self.resetAt = resetAt
            self.reason = reason
        }
    }

    /// One seat's half of the state.
    struct Seat: Decodable, Equatable, Sendable {
        /// The seat's bot name,  as the eavesdropper knows it.
        var bot: String?
        /// Whether the eavesdropper is connected for this seat.
        var connected: Bool?
        /// Whether the kill switch stops this seat.
        var paused: Bool?
        /// The wake jobs the eavesdropper is running for this seat.
        var jobs: Int?
        /// The session leases the eavesdropper holds for this seat.
        var leases: [Lease]?
        /// The wake the eavesdropper is running for this seat right now,  by id.
        var running: String?
        /// Whether the eavesdropper said which wake it is running.  An eavesdropper from
        /// before the field says nothing,  and that is not a claim that no wake runs.
        var reportsRunning: Bool
        /// When the eavesdropper last had an event for this seat.
        var lastEvent: Double?
        /// The provider quota the eavesdropper reads for this seat.  Only a seat whose wake
        /// talks to a provider has one.
        var quota: Quota?

        enum CodingKeys: String, CodingKey {
            case bot, connected, paused, jobs, leases, running, quota
            case lastEvent = "last_event"
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
            lastEvent = try? container.decodeIfPresent(Double.self, forKey: .lastEvent)
            quota = try? container.decodeIfPresent(Quota.self, forKey: .quota)
            if container.contains(.running) {
                if (try? container.decodeNil(forKey: .running)) == true {
                    running = nil
                    reportsRunning = true
                } else if let id = try? container.decode(String.self, forKey: .running) {
                    running = id
                    reportsRunning = true
                } else {
                    running = nil
                    reportsRunning = false
                }
            } else {
                running = nil
                reportsRunning = false
            }
        }
    }

    /// One session lease the eavesdropper holds.
    struct Lease: Decodable, Equatable, Sendable {
        /// The lease's own id.
        var leaseID: String?
        /// The platform the lease belongs to.
        var platform: String?
        /// Whether the eavesdropper judges the session behind the lease to be alive.
        ///
        /// This is the eavesdropper's own judgement,  and it is the one the panel shows:  a
        /// lease whose process is gone,  whose pid was reused or  whose heartbeat stopped
        /// is not an open session.
        var alive: Bool?
        /// Why the lease is alive or not,  in the eavesdropper's words.
        var why: String?
        /// How many topics the lease holds.
        var topics: Int?
        /// Whether a rewake watcher holds the lease.
        var watcher: Bool?
        /// How many items the lease's live inbox holds.
        var pending: Int?
        /// The session's own id.
        var sessionID: String?
        /// The session's registered letter (AFC-597),  when the registry has one.
        var letter: String?
        /// The session's registered title,  already scrubbed by the eavesdropper.
        var title: String?
        /// The machine the registry verified the session on:  `mac` or `server`.
        var verifiedHost: String?

        enum CodingKeys: String, CodingKey {
            case leaseID = "lease_id"
            case sessionID = "session_id"
            case verifiedHost = "verified_host"
            case platform, alive, why, topics, watcher, pending, letter, title
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
            sessionID = try? container.decodeIfPresent(String.self, forKey: .sessionID)
            letter = try? container.decodeIfPresent(String.self, forKey: .letter)
            title = try? container.decodeIfPresent(String.self, forKey: .title)
            verifiedHost = try? container.decodeIfPresent(String.self, forKey: .verifiedHost)
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

    /// The eavesdropper's pid.
    var pid: Int?
    /// The eavesdropper's version.
    var version: String?
    /// When the eavesdropper last published.
    var updated: Double?
    /// Which instance the eavesdropper says it is:  `mac` or `server`.
    var instance: String?
    /// The seat partition file the eavesdropper goes by.
    var partitionFile: String?
    /// The seats the eavesdropper's configuration turned off.
    var disabled: [String]?
    /// The seats the eavesdropper could not start,  and why.
    var skippedSeats: [String: [String]]?
    /// The kill switch,  by scope.
    var pause: [String: PauseEntry]?
    /// The seats the eavesdropper runs,  by tag.
    var seats: [String: Seat]?

    enum CodingKeys: String, CodingKey {
        case pid, version, updated, instance, disabled, pause, seats
        case partitionFile = "partition_file"
        case skippedSeats = "skipped_seats"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pid = try? container.decodeIfPresent(Int.self, forKey: .pid)
        version = try? container.decodeIfPresent(String.self, forKey: .version)
        updated = try? container.decodeIfPresent(Double.self, forKey: .updated)
        instance = try? container.decodeIfPresent(String.self, forKey: .instance)
        partitionFile = try? container.decodeIfPresent(String.self, forKey: .partitionFile)
        disabled = try? container.decodeIfPresent([String].self, forKey: .disabled)
        skippedSeats = try? container.decodeIfPresent([String: [String]].self, forKey: .skippedSeats)
        pause = try? container.decodeIfPresent([String: PauseEntry].self, forKey: .pause)
        seats = try? container.decodeIfPresent([String: Seat].self, forKey: .seats)
    }

    /// When the eavesdropper last published,  as a date.
    var publishedAt: Date? {
        updated.map { Date(timeIntervalSince1970: $0) }
    }
}

// MARK: - Where a seat is held

/// Which machine's eavesdropper holds a seat,  from the seat partition.
///
/// This is where a seat's Zulip queue lives,  not where every session of the seat runs:  a
/// seat the Mac holds can still have a session on the server.
enum SeatPlace: Equatable, Sendable {
    /// The Mac's eavesdropper holds the seat.
    case mac
    /// The server's eavesdropper holds the seat.
    case server
    /// No eavesdropper holds the seat:  the partition says `none` or does not list it.
    case noEavesdropper
    /// The partition gives the seat to the Mac,  but the seat's own section in the Mac's
    /// file names the server,  so the Mac's eavesdropper refuses it.
    case markedForTheServer
    /// Muster cannot say,  and this is why.
    case notKnown(String)

    init(_ host: ListenerSeatHost) {
        switch host {
        case .mac: self = .mac
        case .server: self = .server
        case .noListener, .unlisted: self = .noEavesdropper
        case .markedForTheServer: self = .markedForTheServer
        case .unknown(let why): self = .notKnown(why)
        }
    }

    /// Whether the partition was read and the Mac's eavesdropper does not hold this seat.
    var isElsewhere: Bool {
        switch self {
        case .server, .noEavesdropper, .markedForTheServer: return true
        case .mac, .notKnown: return false
        }
    }

    /// The group the seat's row sits in.
    var group: SeatPlaceGroup {
        switch self {
        case .mac: return .mac
        case .server: return .server
        case .noEavesdropper: return .noEavesdropper
        case .markedForTheServer, .notKnown: return .notKnown
        }
    }

    /// Why the Mac's eavesdropper says nothing about this seat,  for a seat held elsewhere.
    var notHereReason: String {
        switch self {
        case .server:
            return "The server's eavesdropper holds this seat,  and Muster reads only the Mac's,  so this is not read here."
        case .noEavesdropper:
            return "No eavesdropper holds this seat,  so no eavesdropper reports on it."
        case .markedForTheServer:
            return "The seat's section in the Mac's file says instance = \"server\",  so the Mac's eavesdropper refuses it."
        case .mac, .notKnown:
            return "The Mac's eavesdropper holds this seat."
        }
    }
}

/// The groups the panel's rows sit in,  in the order the panel shows them.
enum SeatPlaceGroup: String, CaseIterable, Identifiable, Sendable {
    case mac
    case server
    case noEavesdropper
    case notKnown

    var id: String { rawValue }

    /// The group's heading.
    var title: String {
        switch self {
        case .mac: return "Held By The Mac"
        case .server: return "Held By The Server"
        case .noEavesdropper: return "Held By No Eavesdropper"
        case .notKnown: return "Muster Cannot Say Who Holds These"
        }
    }

    /// The symbol beside the heading,  never instead of its words.
    var systemImage: String {
        switch self {
        case .mac: return "desktopcomputer"
        case .server: return "server.rack"
        case .noEavesdropper: return "minus.circle"
        case .notKnown: return "questionmark.circle"
        }
    }

    /// One line under the heading that says what the group's columns can show.
    var explanation: String {
        switch self {
        case .mac:
            return "The Mac's eavesdropper holds these seats,  and their sessions,  state and quota come from it."
        case .server:
            return "The server's eavesdropper holds these seats.  Muster cannot read it yet,  so only their last post is shown."
        case .noEavesdropper:
            return "No eavesdropper holds these seats,  so only their last post is shown."
        case .notKnown:
            return "The seat partition could not settle where these seats are held."
        }
    }
}

// MARK: - What a seat reads as

/// The eavesdropper's state for one seat.
enum SeatListenerState: Equatable, Sendable {
    /// The eavesdropper is connected for this seat.
    case connected(jobs: Int)
    /// The eavesdropper runs this seat but is not connected to it.
    case disconnected
    /// The kill switch stops this seat.
    case paused
    /// The kill switch stops this seat's wakes only.  The eavesdropper still runs the seat,
    /// so it still says whether it is connected.
    case wakesPaused(connected: Bool)
    /// The eavesdropper's configuration turned this seat off.
    case off
    /// The eavesdropper could not start this seat,  and says why.
    case skipped(String)

    /// The label the column shows.
    var label: String {
        switch self {
        case .connected(let jobs):
            if jobs <= 0 { return "Connected" }
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

    /// Whether the eavesdropper is connected for this seat.
    var isConnected: Bool {
        switch self {
        case .connected: return true
        case .wakesPaused(let connected): return connected
        case .disconnected, .paused, .off, .skipped: return false
        }
    }

    /// Whether this state is the healthy one.
    var isHealthy: Bool {
        switch self {
        case .connected: return true
        case .disconnected, .paused, .wakesPaused, .off, .skipped: return false
        }
    }
}

/// The provider quota the eavesdropper reads for one seat.
enum SeatQuotaReading: Equatable, Sendable {
    /// The provider said it will not serve.
    case exhausted(resetAt: Date?)
    /// The provider serves.  The percentage left is the lowest across its windows,  when the
    /// snapshot gave one.
    case available(remaining: Double?, resetAt: Date?)
    /// The eavesdropper has no quota reading for this seat:  its wake talks to no provider,
    /// or the quota snapshot cannot speak for the provider right now.
    case notReported

    /// The label the column shows.
    var label: String {
        switch self {
        case .exhausted: return "Exhausted"
        case .available(let remaining, _):
            // A zero that is not the provider's own verdict is a passed reset or an unknown
            // remainder,  not an empty quota,  so it is not shown as 0% left.
            guard let remaining, remaining > 0 else { return "Not exhausted" }
            return "\(Int(remaining.rounded(.down)))% left"
        case .notReported: return "Not reported"
        }
    }

    /// When the provider's window resets,  when the snapshot said.
    var resetAt: Date? {
        switch self {
        case .exhausted(let reset): return reset
        case .available(_, let reset): return reset
        case .notReported: return nil
        }
    }

    /// Whether the quota stops the seat.
    var isExhausted: Bool {
        if case .exhausted = self { return true }
        return false
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

    /// Whether the post's own words say the seat is at quota.
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

/// One session the Mac's eavesdropper holds a lease for.
struct SeatSession: Identifiable, Equatable, Sendable {
    /// The lease's id,  or its place in the list when the lease names none.
    var id: String
    /// The session's registered letter.
    var letter: String?
    /// The session's registered title,  safe to show.
    var title: String?
    /// The platform the session runs on.
    var platform: String?
    /// The machine the registry verified the session on.
    var verifiedHost: FleetHost?
    /// Whether the eavesdropper judges the session alive.  Nil when it did not say.
    var alive: Bool?
    /// Why the session is alive or not,  in the eavesdropper's words,  safe to show.
    var why: String?
    /// How many topics the session holds.
    var topics: Int?
    /// How many items wait in the session's live inbox.
    var pending: Int?

    init(lease: ListenerStateDocument.Lease, index: Int) {
        id = lease.leaseID ?? "lease-\(index)"
        letter = lease.letter.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 }
        title = lease.title.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 }
        platform = lease.platform.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 }
        verifiedHost = FleetHost(reportValue: lease.verifiedHost)
        alive = lease.alive
        why = lease.why.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 }
        topics = lease.topics
        pending = lease.pending
    }

    /// The session in one line:  its letter and title,  where it runs,  and what it holds.
    var line: String {
        var parts: [String] = []
        let name = [letter, title].compactMap { $0 }.joined(separator: " ")
        parts.append(name.isEmpty ? "A session with no registered name" : name)
        if let platform { parts.append(platform) }
        if let verifiedHost { parts.append(verifiedHost.inProse) }
        if let topics { parts.append(topics == 1 ? "1 topic" : "\(topics) topics") }
        if let pending, pending > 0 { parts.append("\(pending) waiting") }
        switch alive {
        case true?:
            break
        case false?:
            parts.append("ended" + (why.map { ":  \($0)" } ?? ""))
        case nil:
            parts.append("alive or not is not said")
        }
        return parts.joined(separator: ",  ")
    }
}

// MARK: - The row

/// One line under a row:  a column that could not be read,  or what a column's label
/// leaves out.
struct SeatRowNote: Identifiable, Equatable, Sendable {
    /// The column the line is about.
    var column: String
    /// What it says.
    var text: String
    /// Whether it is a problem,  rather than a plain explanation.
    var isProblem: Bool

    var id: String { column + "\u{1F}" + text }
}

/// One seat's row.
struct SeatActivityRow: Identifiable, Equatable, Sendable {
    /// The seat tag.
    let seat: String
    /// The bot's Zulip display name,  when the realm knows one for this seat.
    let botName: String?
    /// Which machine's eavesdropper holds the seat.
    let place: SeatPlace
    /// The number of live sessions the Mac's eavesdropper holds for this seat.
    let openSessions: SeatFact<Int>
    /// Every session lease the Mac's eavesdropper holds for this seat,  live and ended.
    let sessions: [SeatSession]
    /// The eavesdropper's state for this seat.
    let listener: SeatFact<SeatListenerState>
    /// Whether the eavesdropper is running a wake for this seat right now.
    let runningWake: Bool
    /// When the eavesdropper last had an event for this seat.
    let lastEvent: Date?
    /// The provider quota the eavesdropper reads for this seat.
    let quota: SeatFact<SeatQuotaReading>
    /// The provider's reason for an exhausted quota,  safe to show.
    let quotaReason: String?
    /// The seat's last Zulip post.
    let lastPost: SeatFact<SeatLastPost>

    var id: String { seat }

    init(
        seat: String,
        botName: String? = nil,
        place: SeatPlace = .mac,
        openSessions: SeatFact<Int>,
        sessions: [SeatSession] = [],
        listener: SeatFact<SeatListenerState>,
        runningWake: Bool = false,
        lastEvent: Date? = nil,
        quota: SeatFact<SeatQuotaReading> = .known(.notReported),
        quotaReason: String? = nil,
        lastPost: SeatFact<SeatLastPost>
    ) {
        self.seat = seat
        self.botName = botName
        self.place = place
        self.openSessions = openSessions
        self.sessions = sessions
        self.listener = listener
        self.runningWake = runningWake
        self.lastEvent = lastEvent
        self.quota = quota
        self.quotaReason = quotaReason
        self.lastPost = lastPost
    }

    /// Whether the seat's last post says,  in its own words,  that the seat is at quota.
    ///
    /// This is what a seat said in Zulip,  and the panel labels it that way:  the quota
    /// column comes from the eavesdropper's reading,  not from this.
    var postSaysAtQuota: Bool {
        lastPost.value?.isAtQuota ?? false
    }

    /// Whether the seat is working:  a live session on the Mac,  or a wake running now.
    var isWorking: Bool {
        if runningWake { return true }
        if case .known(let count) = openSessions, count > 0 { return true }
        return false
    }

    /// The parts of this row that could not be read,  as column names and reasons.
    var unknowns: [(column: String, reason: String)] {
        var found: [(String, String)] = []
        if let reason = openSessions.reason { found.append(("sessions", reason)) }
        if let reason = listener.reason { found.append(("eavesdropper", reason)) }
        if let reason = quota.reason { found.append(("quota", reason)) }
        if let reason = lastPost.reason { found.append(("last post", reason)) }
        return found
    }

    /// Every line shown under the row:  each unknown column with its reason,  and what a
    /// short label leaves out,  such as why the eavesdropper skipped the seat.
    var notes: [SeatRowNote] {
        var found: [SeatRowNote] = []
        if case .markedForTheServer = place {
            found.append(SeatRowNote(column: "held by", text: place.notHereReason, isProblem: true))
        }
        if case .notKnown(let why) = place {
            found.append(SeatRowNote(column: "held by", text: why, isProblem: true))
        }
        if case .known(.skipped(let why)) = listener {
            found.append(SeatRowNote(
                column: "eavesdropper",
                text: "The eavesdropper did not start this seat:  \(why)",
                isProblem: true
            ))
        }
        if let quotaReason, quota.value?.isExhausted == true {
            found.append(SeatRowNote(column: "quota", text: quotaReason, isProblem: true))
        }
        for unknown in unknowns {
            found.append(SeatRowNote(column: unknown.column, text: unknown.reason, isProblem: true))
        }
        return found
    }

    /// Whether the row has anything the owner should look at.
    var needsAttention: Bool {
        if notes.contains(where: { $0.isProblem }) { return true }
        if let state = listener.value, !state.isHealthy { return true }
        if quota.value?.isExhausted == true { return true }
        return false
    }

    /// Whether any column of this row is unknown.
    var hasUnknown: Bool {
        !unknowns.isEmpty
    }
}

/// One group of rows,  under one heading.
struct SeatActivityGroup: Identifiable, Equatable, Sendable {
    let kind: SeatPlaceGroup
    let rows: [SeatActivityRow]

    var id: String { kind.id }
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
    /// Where the Mac eavesdropper's state came from.
    var listenerPath: String = ""
    /// When the Mac's eavesdropper wrote the state the panel used,  when it was used.
    var listenerWrittenAt: Date?
    /// Where the seat partition came from.
    var partitionPath: String = ""
    /// The seats the partition gives to the server that have no row,  because the app's
    /// roster does not hold them.
    var otherServerSeats: [String] = []
    /// Why there are no rows at all,  when the app has no seat list.
    var rosterProblem: String?

    /// The seats with at least one live session on the Mac.
    ///
    /// A session on the server attaches to no eavesdropper the Mac can read,  so this is
    /// a count of seats working on the Mac,  and the panel says so.
    var seatsWithSessions: [SeatActivityRow] {
        rows.filter { row in
            if case .known(let sessions) = row.openSessions, sessions > 0 { return true }
            return false
        }
    }

    /// The rows,  grouped by where each seat is held,  each group with the rows that need
    /// attention first and the roster's order after that.
    var groups: [SeatActivityGroup] {
        SeatPlaceGroup.allCases.compactMap { kind in
            let members = rows.filter { $0.place.group == kind }
            guard !members.isEmpty else { return nil }
            let first = members.filter { $0.needsAttention }
            let rest = members.filter { !$0.needsAttention }
            return SeatActivityGroup(kind: kind, rows: first + rest)
        }
    }

    /// The line about the server's seats that have no row,  or nil when there are none.
    var otherServerSeatsNote: String? {
        guard !otherServerSeats.isEmpty else { return nil }
        let count = otherServerSeats.count
        return "The seat partition also gives the server \(count == 1 ? "1 seat" : "\(count) seats") "
            + "that Muster's seat list does not hold,  so \(count == 1 ? "it has" : "they have") no row:  "
            + otherServerSeats.joined(separator: ", ") + "."
    }

    /// The answer to "is anything wrong",  in sentences,  with the code's two-space gaps.
    var headline: String {
        guard rosterProblem == nil, !rows.isEmpty else {
            return "No seat is shown,  because Muster has no seat list."
        }
        var sentences: [String] = []
        let mac = rows.filter { $0.place == .mac }
        if !mac.isEmpty {
            let read = mac.filter { $0.listener.isKnown }
            let connected = read.filter { $0.listener.value?.isConnected == true }.count
            let working = mac.filter { $0.isWorking }.count
            let seats = mac.count == 1 ? "1 seat" : "\(mac.count) seats"
            if read.isEmpty {
                sentences.append("Whether the \(seats) held by the Mac \(mac.count == 1 ? "is" : "are") connected is not known.")
            } else {
                sentences.append(
                    "\(connected) of \(seats) held by the Mac \(connected == 1 ? "is" : "are") connected,  "
                        + "and \(working) \(working == 1 ? "is" : "are") working on the Mac."
                )
                let unread = mac.count - read.count
                if unread > 0 {
                    sentences.append(unread == 1
                        ? "The state of 1 of them could not be read."
                        : "The state of \(unread) of them could not be read.")
                }
            }
            let attention = mac.filter { $0.needsAttention }.count
            if attention > 0 {
                sentences.append(attention == 1
                    ? "1 of them needs a look."
                    : "\(attention) of them need a look.")
            }
        }
        let server = rows.filter { $0.place == .server }.count + otherServerSeats.count
        if server > 0 {
            sentences.append(server == 1
                ? "1 seat is held by the server,  which Muster cannot read yet."
                : "\(server) seats are held by the server,  which Muster cannot read yet.")
        }
        let unknown = rows.filter { $0.place.group == .notKnown }.count
        if unknown > 0 {
            sentences.append(unknown == 1
                ? "Muster cannot say who holds 1 seat."
                : "Muster cannot say who holds \(unknown) seats.")
        }
        return sentences.joined(separator: "  ")
    }
}

// MARK: - The builder

/// Turns what the reader read into what the panel shows.
enum SeatActivityBuilder {
    /// The sources the builder reads.
    struct Sources {
        /// The roster,  in the order the app shows it.
        var roster: [String] = []
        /// The eavesdropper's published state,  when it could be read.
        var listener: ListenerStateDocument?
        /// Why the eavesdropper's state could not be read.
        var listenerProblem: String?
        /// The seat partition,  when it was read.
        var partition: ListenerPartition?
        /// The Mac eavesdropper's configuration file,  when it could be read.
        var config: ListenerConfigDocument?
        /// Where the partition came from.
        var partitionPath: String = ""
        /// The bots the realm knows,  by seat tag.
        var bots: [String: ChatBot] = [:]
        /// Why the user list could not be read.
        var userProblem: String?
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
        /// Where the eavesdropper's state lives.
        var listenerPath: String = ""
        /// Why there is no seat list,  when there is none.
        var rosterProblem: String?
    }

    /// Build the panel's model.
    static func build(_ sources: Sources) -> SeatActivityModel {
        var model = SeatActivityModel()
        model.readAt = sources.readAt
        model.readingSeat = sources.readingSeat
        model.requestsMade = sources.requestsMade
        model.requestCeiling = sources.requestCeiling
        model.listenerPath = sources.listenerPath
        model.listenerWrittenAt = sources.listener?.publishedAt
        model.partitionPath = sources.partitionPath
        model.rosterProblem = sources.rosterProblem

        var problems: [String] = []
        if let problem = sources.rosterProblem { problems.append(problem) }
        if let problem = sources.listenerProblem { problems.append(problem) }
        if let problem = sources.partition?.problem {
            problems.append("The seat partition could not be read,  so Muster cannot say which machine holds each seat.  \(problem)")
        }
        if let problem = sources.userProblem { problems.append(problem) }
        model.problems = problems

        model.rows = sources.roster.map { seat in
            row(for: seat, sources: sources)
        }
        model.otherServerSeats = otherServerSeats(sources)
        return model
    }

    /// The seats the partition gives to the server that the roster does not hold.
    private static func otherServerSeats(_ sources: Sources) -> [String] {
        guard let seats = sources.partition?.seats else { return [] }
        let roster = Set(sources.roster.map { $0.uppercased() })
        return seats
            .filter { $0.value == "server" && !roster.contains($0.key) }
            .map(\.key)
            .sorted()
    }

    /// Which machine's eavesdropper holds a seat.
    static func place(of seat: String, sources: Sources) -> SeatPlace {
        guard let partition = sources.partition else {
            return .notKnown("The seat partition was not read.")
        }
        if let config = sources.config {
            return SeatPlace(partition.host(of: seat, in: config))
        }
        return SeatPlace(partition.host(of: seat))
    }

    /// One seat's row.
    private static func row(for seat: String, sources: Sources) -> SeatActivityRow {
        let bot = sources.bots[seat]
        let place = place(of: seat, sources: sources)
        let entry = sources.listener?.seats?[seat]
        let quotaFact = quota(for: seat, place: place, sources: sources)
        return SeatActivityRow(
            seat: seat,
            botName: bot?.fullName,
            place: place,
            openSessions: openSessions(for: seat, place: place, sources: sources),
            sessions: (entry?.leases ?? []).enumerated().map { SeatSession(lease: $0.element, index: $0.offset) },
            listener: listenerState(for: seat, place: place, sources: sources),
            runningWake: entry?.running != nil,
            lastEvent: entry?.lastEvent.map { Date(timeIntervalSince1970: $0) },
            quota: quotaFact,
            quotaReason: entry?.quota?.reason.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 },
            lastPost: sources.lastPosts[seat] ?? .unknown(SeatActivityBuilder.noPostReason)
        )
    }

    /// The reason a seat has no last post,  when the panel never asked.
    static let noPostReason = "The seat's last post was not read."

    /// The number of live sessions the Mac's eavesdropper holds for a seat.
    ///
    /// The eavesdropper's own lease list is the source:  a lease is a session that attached
    /// to the fleet chat,  and the eavesdropper judges it alive or not.  A seat the Mac's
    /// eavesdropper publishes no state for has no lease list to read,  so its count is never
    /// zero:  it is held elsewhere,  or it is unknown.
    private static func openSessions(for seat: String, place: SeatPlace, sources: Sources) -> SeatFact<Int> {
        if let entry = sources.listener?.seats?[seat] {
            guard let leases = entry.leases else {
                return .unknown("The eavesdropper's state for this seat names no leases,  so its sessions on the Mac are unknown.")
            }
            return .known(leases.filter { $0.alive == true }.count)
        }
        if place.isElsewhere {
            return .notHere(place.notHereReason)
        }
        guard let listener = sources.listener else {
            return .unknown(sources.listenerProblem ?? "The eavesdropper's state could not be read.")
        }
        if listener.disabled?.contains(seat) == true {
            return .unknown("The eavesdropper's configuration turns this seat off,  so it holds no sessions for it.")
        }
        return .unknown("The Mac's eavesdropper publishes no state for this seat,  so its sessions on the Mac are unknown.")
    }

    /// The eavesdropper's state for a seat.
    private static func listenerState(
        for seat: String,
        place: SeatPlace,
        sources: Sources
    ) -> SeatFact<SeatListenerState> {
        // A seat held elsewhere is not the Mac's to report on,  and the Mac's kill switch
        // does not reach it.  Only a seat the Mac's eavesdropper still publishes reads as known.
        if place.isElsewhere, sources.listener?.seats?[seat] == nil {
            return .notHere(place.notHereReason)
        }
        guard let listener = sources.listener else {
            return .unknown(sources.listenerProblem ?? "The eavesdropper's state could not be read.")
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
                return .unknown("The eavesdropper's state for this seat does not say whether it is connected.")
            }
            if pause == .wakesOnly {
                return .known(.wakesPaused(connected: connected))
            }
            return .known(connected ? .connected(jobs: entry.jobs ?? 0) : .disconnected)
        }
        if let problems = listener.skippedSeats?[seat], !problems.isEmpty {
            return .known(.skipped(problems.joined(separator: "; ")))
        }
        return .unknown("The Mac's eavesdropper publishes no state for this seat.")
    }

    /// The provider quota the Mac's eavesdropper reads for a seat.
    ///
    /// This is the reading the wake gate itself uses,  so this column and the Wake panel
    /// cannot disagree.  A seat whose wake talks to no provider,  or whose provider the
    /// quota snapshot cannot speak for,  reads as not reported:  never as having quota.
    private static func quota(
        for seat: String,
        place: SeatPlace,
        sources: Sources
    ) -> SeatFact<SeatQuotaReading> {
        if let entry = sources.listener?.seats?[seat] {
            guard let quota = entry.quota, quota.known == true else {
                return .known(.notReported)
            }
            let reset = quota.resetAt.flatMap { FleetTime.parse($0) }
            if quota.exhausted == true {
                return .known(.exhausted(resetAt: reset))
            }
            return .known(.available(remaining: quota.remaining, resetAt: reset))
        }
        if place.isElsewhere {
            return .notHere(place.notHereReason)
        }
        guard sources.listener != nil else {
            return .unknown(sources.listenerProblem ?? "The eavesdropper's state could not be read.")
        }
        return .unknown("The Mac's eavesdropper publishes no state for this seat,  so its quota is unknown.")
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
    /// This is the eavesdropper's own rule (`live.paused`):  a scope covers the seat when it
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

    /// When a quota window resets,  in the fleet's own clock:  the time alone when it is
    /// the same day as `now`,  and the month and day with it when it is not.
    static func resetText(_ date: Date, now: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Chicago") ?? .current
        if calendar.isDate(date, inSameDayAs: now) {
            return "resets \(FleetTime.clock(date))"
        }
        let parts = calendar.dateComponents([.month, .day], from: date)
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let month = parts.month.map { months[max(0, min(11, $0 - 1))] } ?? ""
        return "resets \(month) \(parts.day ?? 0), \(FleetTime.clock(date))"
    }
}

// MARK: - The at-quota question

/// How a seat's last post is read for a quota report.
///
/// This is a heuristic,  and it is a narrow one:  a seat says it is out of quota in a
/// handful of ways,  and only those count.  It is shown only as what the post said,
/// beside the post,  and never as the seat's quota:  the quota column comes from the
/// eavesdropper's own reading.
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
        "not", "never", "isn't", "aren't", "wasn't", "weren't",
    ]

    /// The two-word negations,  matched only as a pair:  a bare "no" or "longer" is not
    /// a negation.
    static let negationPhrases: Set<String> = ["no longer"]

    /// How much text before a marker is read,  so each marker costs the same however
    /// long the post is.
    static let negationLookback = 120

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
        // A bounded tail,  then only the current clause:  a negation behind a comma,
        // a semicolon or a sentence end belongs to another clause.
        let tail = prefix.suffix(negationLookback)
        let clause = tail.split(omittingEmptySubsequences: false, whereSeparator: { ".!?;,\n".contains($0) })
            .last ?? ""
        let words = clause.lowercased()
            .replacingOccurrences(of: "\u{2019}", with: "'")
            .split(whereSeparator: { !$0.isLetter && $0 != "'" })
            .map(String.init)
        let window = Array(words.suffix(negationWindow + 1))
        let singles = window.suffix(negationWindow)
        if singles.contains(where: { negations.contains($0) }) { return true }
        for index in window.indices.dropLast() {
            if negationPhrases.contains(window[index] + " " + window[index + 1]) { return true }
        }
        return false
    }
}
