import Combine
import Foundation

// The server eavesdropper's status, read on the Mac.
//
// The server's listener (the eavesdropper) runs in a container on the server, and Muster has no
// route into it.  The server's control helper reads the listener's own status file and cuts it
// down to names, counts, tokens and times (`scripts/agent_sync/reduced_status.py`, the
// reduced_status v1 document).  Muster asks for that document over the existing restricted
// route with the read-only verb `server eavesdropper-status server --plan --json`
// (`OpsAction.eavesdropperStatus`).  It changes nothing, so it needs no sheet and no Touch ID.
//
// Every scalar in the document can be null:  the reducer drops a value it cannot vouch for to
// null rather than repair it, and a seat it cannot reduce is left out.  So every field is read on
// its own and as optional, and a missing or null field reads as not reported, never as zero,
// healthy or connected.  The v1 document carries no lease ids, session ids, paths or user ids,
// and nothing here asks for one.

// MARK: - Reading one field at a time

private extension KeyedDecodingContainer {
    /// A field, or nil when it is missing, null or not of this type.
    func optionalField<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        guard let value = try? decodeIfPresent(type, forKey: key) else { return nil }
        return value
    }

    /// A string with its control characters removed and its length bounded, or nil.
    func optionalText(_ key: Key, limit: Int = 96) -> String? {
        guard let raw = optionalField(String.self, key) else { return nil }
        return EavesdropperStatusText.plain(raw, limit: limit)
    }

    /// A count:  a whole number, zero or more, or nil.
    func optionalCount(_ key: Key) -> Int? {
        guard let value = optionalField(Int.self, key), value >= 0 else { return nil }
        return value
    }

    /// A list read one entry at a time:  an entry that cannot be read is left out and costs no
    /// other.  A list that is missing, null or not a list is empty.
    func lenientList<T: Decodable>(_ type: T.Type, _ key: Key) -> [T] {
        guard let entries = optionalField([LenientEntry<T>].self, key) else { return [] }
        return entries.compactMap { $0.value }
    }
}

/// One list entry, or nil when it cannot be read.
private struct LenientEntry<T: Decodable>: Decodable {
    let value: T?

    init(from decoder: Decoder) throws {
        value = try? T(from: decoder)
    }
}

/// The words this file shows, and how a string from the document is made safe to show.
enum EavesdropperStatusText {
    /// What a missing or null field reads as.
    static let notReported = "Not reported"
    static let gap = ServerStatusCopy.sentenceGap

    /// `text` without control characters, trimmed and cut to `limit` characters, or nil when
    /// nothing is left.
    static func plain(_ text: String, limit: Int) -> String? {
        var scalars = String.UnicodeScalarView()
        for scalar in text.unicodeScalars where !CharacterSet.controlCharacters.contains(scalar) {
            scalars.append(scalar)
        }
        let trimmed = String(scalars).trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        return String(trimmed.prefix(limit))
    }

    /// A count, or Not reported.
    static func count(_ value: Int?) -> String {
        value.map { String($0) } ?? notReported
    }

    /// A time on Jay's clock, or Not reported.
    static func time(_ date: Date?, now: Date) -> String {
        date.map { OperatorVoiceReading.whenText($0, now: now) } ?? notReported
    }
}

// MARK: - The document

/// The reduced_status v1 document, exactly as `reduce_status` writes it:
///
///     {"schema": 1, "generated_at", "instance", "status_updated",
///      "operator_voice": {"available", "code", "since"},
///      "pause": {"paused", "since", "wakes_only", "set_by"}, "pause_file_error",
///      "seats": [{"seat", "bot", "connected", "running", "wake", "paused", "wakes_24h",
///                 "held_back", "jobs", "last_event", "quota": {...}, "leases": [...],
///                 "letters": {"known", "why"}}],
///      "skipped": [{"seat", "reason"}], "disabled": [name]}
///
/// The document has no schema name of its own:  `schema` is its version, and the helper's answer
/// around it names the verb (`server eavesdropper-status`), which the answer parser checks.
struct ReducedEavesdropperStatus: Decodable, Equatable {
    /// The one version this app reads.
    static let schemaVersion = 1

    /// How long a reading stays current.  The reducer calls the listener's own status stale
    /// after five minutes (`STALE_AFTER_SECONDS = 300`:  the listener rewrites it every two
    /// seconds, so five minutes is a stopped or wedged listener and not a slow one), and the
    /// rollout note says the same.  Muster uses the same window for the age of the server's
    /// answer and for the age of the listener's own status.
    static let freshnessWindow: TimeInterval = 300
    /// How far ahead of this Mac's clock a time may be and still count:  the reducer's own rule
    /// (session BB, PR 1479:  "5 s ahead to 300 s old").  Both machines keep NTP time, so more
    /// than this is a clock problem, not a fresh reading.
    static let aheadTolerance: TimeInterval = 5

    /// The version the document says it is, or nil when it does not say.
    let schema: Int?
    /// When the server's helper reduced the document, as written.
    let generatedAtText: String?
    /// `server` or `mac`, or nil.
    let instance: String?
    /// When the listener last wrote its own status, as written.
    let statusUpdatedText: String?
    let operatorVoice: ServerOperatorVoiceFields?
    /// The global pause (the kill switch).  A pause on one seat shows on that seat.
    let pause: ServerEavesdropperPause?
    /// `invalid` when the listener's pause value had the wrong shape, else nil.
    let pauseFileError: String?
    let seats: [ServerSeatFacts]
    /// Seats the listener did not load, each with a reason word.
    let skipped: [ServerSkippedSeat]
    /// Seats the listener's settings turn off.
    let disabled: [String]

    enum CodingKeys: String, CodingKey {
        case schema, instance, pause, seats, skipped, disabled
        case generatedAtText = "generated_at"
        case statusUpdatedText = "status_updated"
        case operatorVoice = "operator_voice"
        case pauseFileError = "pause_file_error"
    }
}

extension ReducedEavesdropperStatus {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = container.optionalField(Int.self, .schema)
        generatedAtText = container.optionalText(.generatedAtText, limit: 40)
        instance = container.optionalText(.instance, limit: 24)
        statusUpdatedText = container.optionalText(.statusUpdatedText, limit: 40)
        operatorVoice = container.optionalField(ServerOperatorVoiceFields.self, .operatorVoice)
        pause = container.optionalField(ServerEavesdropperPause.self, .pause)
        pauseFileError = container.optionalText(.pauseFileError, limit: 24)
        seats = container.lenientList(ServerSeatFacts.self, .seats)
        skipped = container.lenientList(ServerSkippedSeat.self, .skipped)
        disabled = container.lenientList(String.self, .disabled)
            .compactMap { EavesdropperStatusText.plain($0, limit: 32) }
    }

    /// True when this is the version this app reads.
    var isThisAppsVersion: Bool { schema == Self.schemaVersion }

    /// When the server's helper reduced the document, or nil when it does not say or the time
    /// cannot be read.
    var generatedAt: Date? { generatedAtText.flatMap(FleetTime.parse) }

    /// When the listener last wrote its own status, or nil.
    var statusUpdated: Date? { statusUpdatedText.flatMap(FleetTime.parse) }

    /// How old the server's answer is, or nil when it does not say when it was read.
    func age(now: Date) -> TimeInterval? {
        generatedAt.map { now.timeIntervalSince($0) }
    }

    /// The age as a list shows it (`3m`), or a dash.
    func ageText(now: Date) -> String {
        FleetTime.ageText(since: generatedAt, now: now)
    }

    /// Why the reading is not current, or nil when it is.  The server's answer and the
    /// listener's own status must each be at most `freshnessWindow` old and at most
    /// `aheadTolerance` ahead of `now`, the rule the reducer applies to the listener's status.
    func notCurrentReason(now: Date) -> String? {
        let window = Self.freshnessWindow
        let minutes = Int(window / 60)
        guard let generatedAt else {
            return "The server's answer does not say when it was read, so it is not current."
        }
        let answerAge = now.timeIntervalSince(generatedAt)
        if answerAge > window {
            return "The server read this at \(EavesdropperStatusText.time(generatedAt, now: now)), more than \(minutes) minutes ago, so it is not current."
        }
        if answerAge < -Self.aheadTolerance {
            return "The server's answer is dated \(EavesdropperStatusText.time(generatedAt, now: now)), ahead of this Mac's clock, so it is not current."
        }
        guard let statusUpdated else {
            return "The server's eavesdropper does not say when it last wrote its status, so it is not current."
        }
        let statusAge = now.timeIntervalSince(statusUpdated)
        if statusAge > window {
            return "The server's eavesdropper last wrote its status at \(EavesdropperStatusText.time(statusUpdated, now: now)), more than \(minutes) minutes ago, so it is not current."
        }
        if statusAge < -Self.aheadTolerance {
            return "The server's eavesdropper dated its status \(EavesdropperStatusText.time(statusUpdated, now: now)), ahead of this Mac's clock, so it is not current."
        }
        return nil
    }

    /// True when the reading is current at `now`.
    func isCurrent(now: Date) -> Bool { notCurrentReason(now: now) == nil }

    /// The seat named `name`, matched exactly first and then ignoring case.
    func seat(named name: String) -> ServerSeatFacts? {
        if let exact = seats.first(where: { $0.seat == name }) { return exact }
        let upper = name.uppercased()
        return seats.first { $0.seat.uppercased() == upper }
    }

    /// The skipped entry for `name`, matched the same way.
    func skippedSeat(named name: String) -> ServerSkippedSeat? {
        let upper = name.uppercased()
        return skipped.first { $0.seat == name || $0.seat.uppercased() == upper }
    }

    /// True when the listener's settings turn `name` off.
    func isDisabled(_ name: String) -> Bool {
        let upper = name.uppercased()
        return disabled.contains { $0 == name || $0.uppercased() == upper }
    }

    /// Whether the Operator can post, as the server's eavesdropper says.
    var operatorVoiceReading: ServerOperatorVoice {
        operatorVoice?.reading ?? .notReported
    }
}

// MARK: - The Operator's voice and the global pause

/// The document's `operator_voice` block, field by field.
struct ServerOperatorVoiceFields: Decodable, Equatable {
    let available: Bool?
    let code: String?
    let sinceText: String?

    enum CodingKeys: String, CodingKey {
        case available, code
        case sinceText = "since"
    }
}

extension ServerOperatorVoiceFields {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        available = container.optionalField(Bool.self, .available)
        code = container.optionalText(.code, limit: 48)
        sinceText = container.optionalText(.sinceText, limit: 40)
    }

    var reading: ServerOperatorVoice {
        let since = sinceText.flatMap(FleetTime.parse)
        switch available {
        case true?: return .canPost(since: since)
        case false?: return .cannotPost(code: code, since: since)
        case nil: return .notReported
        }
    }
}

/// Whether the Operator can post, as the server's eavesdropper says.  A null `available` is
/// not reported:  the reduced document cannot tell a listener that has not tried yet from one
/// that does not publish the block, so Muster claims neither.
enum ServerOperatorVoice: Equatable {
    case notReported
    case canPost(since: Date?)
    case cannotPost(code: String?, since: Date?)

    var label: String {
        switch self {
        case .notReported: return EavesdropperStatusText.notReported
        case .canPost: return "Can post"
        case .cannotPost: return "Cannot post"
        }
    }

    /// The cause in plain words for the listener's code, when Muster knows the code.
    var cause: String? {
        guard case .cannotPost(let code?, _) = self else { return nil }
        return OperatorVoiceReading.cause(code)
    }
}

/// The document's `pause` block:  the global pause only.
struct ServerEavesdropperPause: Decodable, Equatable {
    let paused: Bool?
    let sinceText: String?
    /// True when the pause stops wakes only.
    let wakesOnly: Bool?
    /// Who set it, as a short word (`cli`, `owner-dm`).
    let setBy: String?

    enum CodingKeys: String, CodingKey {
        case paused
        case sinceText = "since"
        case wakesOnly = "wakes_only"
        case setBy = "set_by"
    }
}

extension ServerEavesdropperPause {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        paused = container.optionalField(Bool.self, .paused)
        sinceText = container.optionalText(.sinceText, limit: 40)
        wakesOnly = container.optionalField(Bool.self, .wakesOnly)
        setBy = container.optionalText(.setBy, limit: 24)
    }

    var since: Date? { sinceText.flatMap(FleetTime.parse) }
}

// MARK: - One seat

/// One seat of the server's eavesdropper, field by field, with each fact as a reading that
/// says Not reported for a missing or null field.
struct ServerSeatFacts: Decodable, Equatable {
    /// The seat's name, as the listener's settings spell it.  The one field a seat must have.
    let seat: String
    /// The seat's bot, as a short word.
    let bot: String?
    let connected: Bool?
    /// `running`, `stopped` or `unknown`, as the reducer derived it.
    let running: String?
    /// The wake kind word.
    let wake: String?
    let paused: Bool?
    let wakesLastDay: Int?
    let heldBack: Int?
    let jobs: Int?
    let lastEventText: String?
    let quota: ServerSeatQuotaFields?
    /// The seat's sessions (the listener's leases), with no lease or session id.
    let sessions: [ServerSeatSession]
    let letters: ServerSeatLetterFields?

    enum CodingKeys: String, CodingKey {
        case seat, bot, connected, running, wake, paused, jobs, quota, letters
        case sessions = "leases"
        case wakesLastDay = "wakes_24h"
        case heldBack = "held_back"
        case lastEventText = "last_event"
    }
}

extension ServerSeatFacts {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let name = container.optionalText(.seat, limit: 32) else {
            throw DecodingError.dataCorruptedError(
                forKey: .seat, in: container, debugDescription: "A seat with no name is left out."
            )
        }
        seat = name
        bot = container.optionalText(.bot, limit: 24)
        connected = container.optionalField(Bool.self, .connected)
        running = container.optionalText(.running, limit: 24)
        wake = container.optionalText(.wake, limit: 24)
        paused = container.optionalField(Bool.self, .paused)
        wakesLastDay = container.optionalCount(.wakesLastDay)
        heldBack = container.optionalCount(.heldBack)
        jobs = container.optionalCount(.jobs)
        lastEventText = container.optionalText(.lastEventText, limit: 40)
        quota = container.optionalField(ServerSeatQuotaFields.self, .quota)
        sessions = container.lenientList(ServerSeatSession.self, .sessions)
        letters = container.optionalField(ServerSeatLetterFields.self, .letters)
    }

    var connection: ServerSeatConnection { ServerSeatConnection(connected) }
    var runState: ServerSeatRunState { ServerSeatRunState(token: running) }
    var wakeKind: ServerSeatWake { ServerSeatWake(token: wake) }
    var pauseState: ServerSeatPause { ServerSeatPause(paused) }
    var lastEvent: Date? { lastEventText.flatMap(FleetTime.parse) }
    var quotaReading: ServerSeatQuota { quota?.reading ?? .notReported }
    var letterState: ServerSeatLetters { letters?.reading ?? .notReported }

    /// Wakes in the last day, or Not reported.
    var wakesLastDayText: String { EavesdropperStatusText.count(wakesLastDay) }
    /// Messages held back, or Not reported.
    var heldBackText: String { EavesdropperStatusText.count(heldBack) }
    /// Jobs, or Not reported.
    var jobsText: String { EavesdropperStatusText.count(jobs) }
    /// The bot, or Not reported.
    var botText: String { bot ?? EavesdropperStatusText.notReported }

    /// The last event on Jay's clock, or Not reported.
    func lastEventLabel(now: Date) -> String {
        EavesdropperStatusText.time(lastEvent, now: now)
    }
}

/// Whether a seat's queue is connected.
enum ServerSeatConnection: Equatable {
    case connected
    case notConnected
    case notReported

    init(_ value: Bool?) {
        switch value {
        case true?: self = .connected
        case false?: self = .notConnected
        case nil: self = .notReported
        }
    }

    var label: String {
        switch self {
        case .connected: return "Connected"
        case .notConnected: return "Not connected"
        case .notReported: return EavesdropperStatusText.notReported
        }
    }
}

/// Whether a seat's listener runs, as the reducer derived it:  running when connected, stopped
/// when not, and not known when the listener's status was stale or did not say.
enum ServerSeatRunState: Equatable {
    case running
    case stopped
    case notKnown
    case notReported

    init(token: String?) {
        switch token {
        case "running"?: self = .running
        case "stopped"?: self = .stopped
        case "unknown"?: self = .notKnown
        default: self = .notReported
        }
    }

    var label: String {
        switch self {
        case .running: return "Running"
        case .stopped: return "Stopped"
        case .notKnown: return "Not known"
        case .notReported: return EavesdropperStatusText.notReported
        }
    }
}

/// What wakes a seat.
enum ServerSeatWake: Equatable {
    case inbox
    case muse
    case http
    case claude
    case openRouter
    case miniMaxCode
    /// The fallback is standing in for the seat's own wake.
    case fallback
    /// A wake kind the reducer does not name.
    case other
    case notReported

    init(token: String?) {
        switch token {
        case "inbox"?: self = .inbox
        case "muse"?: self = .muse
        case "http"?: self = .http
        case "claude"?: self = .claude
        case "openrouter"?: self = .openRouter
        case "mcode"?: self = .miniMaxCode
        case "fallback"?: self = .fallback
        case "other"?: self = .other
        default: self = .notReported
        }
    }

    var label: String {
        switch self {
        case .inbox: return "Inbox"
        case .muse: return "Muse"
        case .http: return "HTTP"
        case .claude: return "Claude"
        case .openRouter: return "OpenRouter"
        case .miniMaxCode: return "MiniMax Code"
        case .fallback: return "Fallback standing in"
        case .other: return "Other"
        case .notReported: return EavesdropperStatusText.notReported
        }
    }
}

/// Whether a seat is paused (a full pause or a pause of its wakes).
enum ServerSeatPause: Equatable {
    case paused
    case notPaused
    case notReported

    init(_ value: Bool?) {
        switch value {
        case true?: self = .paused
        case false?: self = .notPaused
        case nil: self = .notReported
        }
    }

    var label: String {
        switch self {
        case .paused: return "Paused"
        case .notPaused: return "Not paused"
        case .notReported: return EavesdropperStatusText.notReported
        }
    }
}

/// A seat's `quota` block:  the provider quota the wake gate reads.
struct ServerSeatQuotaFields: Decodable, Equatable {
    let known: Bool?
    let exhausted: Bool?
    /// A percent, 0 to 100.
    let remaining: Int?
    let resetAtText: String?
    /// Never produced by v1 (the source has no such count);  read in case a later reducer does.
    let used: Int?
    let capDay: Int?
    let capHour: Int?
    let source: String?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case known, exhausted, remaining, used, source, reason
        case resetAtText = "reset_at"
        case capDay = "cap_day"
        case capHour = "cap_hour"
    }
}

extension ServerSeatQuotaFields {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        known = container.optionalField(Bool.self, .known)
        exhausted = container.optionalField(Bool.self, .exhausted)
        remaining = container.optionalCount(.remaining).flatMap { $0 <= 100 ? $0 : nil }
        resetAtText = container.optionalText(.resetAtText, limit: 40)
        used = container.optionalCount(.used)
        capDay = container.optionalCount(.capDay)
        capHour = container.optionalCount(.capHour)
        source = container.optionalText(.source, limit: 24)
        reason = container.optionalText(.reason, limit: 24)
    }

    var reading: ServerSeatQuota {
        switch known {
        case nil: return .notReported
        case false?: return .notKnown
        case true?:
            return .known(
                exhausted: exhausted,
                remainingPercent: remaining,
                resetsAt: resetAtText.flatMap(FleetTime.parse)
            )
        }
    }
}

/// A seat's provider quota.  An absent reading is never an exhausted one.
enum ServerSeatQuota: Equatable {
    case notReported
    /// The listener has no quota reading for this seat (or its wake names no provider).
    case notKnown
    /// Each part can still be nil, which reads as not reported.
    case known(exhausted: Bool?, remainingPercent: Int?, resetsAt: Date?)

    var label: String {
        switch self {
        case .notReported: return EavesdropperStatusText.notReported
        case .notKnown: return "Not known"
        case .known(let exhausted, let remaining, _):
            if exhausted == true { return "Used up" }
            if let remaining { return "\(remaining)% left" }
            if exhausted == false { return "Not used up" }
            return EavesdropperStatusText.notReported
        }
    }
}

/// Whether the session letters of a seat's sessions are known.
struct ServerSeatLetterFields: Decodable, Equatable {
    let known: Bool?
    /// `partial` when some sessions have a letter and some do not, else nil.
    let why: String?

    enum CodingKeys: String, CodingKey { case known, why }
}

extension ServerSeatLetterFields {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        known = container.optionalField(Bool.self, .known)
        why = container.optionalText(.why, limit: 24)
    }

    var reading: ServerSeatLetters {
        switch known {
        case true?: return .allKnown
        case false?: return why == "partial" ? .partial : .notKnown
        case nil: return .notReported
        }
    }
}

enum ServerSeatLetters: Equatable {
    /// Every session has a letter, or the seat has no session.
    case allKnown
    /// Some sessions have a letter and some do not.
    case partial
    /// No session has a letter.
    case notKnown
    case notReported

    var label: String {
        switch self {
        case .allKnown: return "Known"
        case .partial: return "Some known"
        case .notKnown: return "Not known"
        case .notReported: return EavesdropperStatusText.notReported
        }
    }
}

/// One session the server's eavesdropper holds for a seat (a lease row, without its ids).
///
/// It has no title, on purpose:  a server lease's title never leaves the server (session BB's
/// reducer stops emitting it).  An older reducer that still sends `title` is read without it,
/// because the key is not decoded, so no title is ever stored or shown for a server seat.
struct ServerSeatSession: Decodable, Equatable {
    /// The session letter, `A` to `ZZ`.
    let letter: String?
    /// The platform, as a short word (`claude-code`).
    let platform: String?
    let alive: Bool?
    let pending: Int?
    /// `mac`, `server` or `cloud`.
    let verifiedHost: String?
    /// The liveness reason word.
    let why: String?
    let topics: Int?
    let watcher: Bool?

    enum CodingKeys: String, CodingKey {
        case letter, platform, alive, pending, why, topics, watcher
        case verifiedHost = "verified_host"
    }
}

extension ServerSeatSession {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        letter = container.optionalText(.letter, limit: 2)
        platform = container.optionalText(.platform, limit: 24)
        alive = container.optionalField(Bool.self, .alive)
        pending = container.optionalCount(.pending)
        verifiedHost = container.optionalText(.verifiedHost, limit: 24)
        why = container.optionalText(.why, limit: 24)
        topics = container.optionalCount(.topics)
        watcher = container.optionalField(Bool.self, .watcher)
    }

    /// Whether the session is alive, in words.
    var aliveLabel: String {
        switch alive {
        case true?: return "Alive"
        case false?: return "Not alive"
        case nil: return EavesdropperStatusText.notReported
        }
    }

    /// The liveness reason in plain words, or nil for a word Muster does not know.
    var whyText: String? {
        switch why {
        case "ok"?: return "Its session is alive."
        case "wait_grace"?: return "It started a moment ago and is not confirmed yet."
        case "pid_gone"?: return "Its process has ended."
        case "pid_reused"?: return "Its process number now belongs to another process."
        case "ended"?: return "It ended."
        case "no_lease"?: return "It holds no lease."
        case "no_heartbeat"?: return "It has stopped checking in."
        case "not_interactive"?: return "It is not an interactive session."
        default: return nil
        }
    }

    /// The machine it was verified on, in words, or Not reported.
    var verifiedHostLabel: String {
        switch verifiedHost {
        case "mac"?: return "The Mac"
        case "server"?: return "The server"
        case "cloud"?: return "The cloud"
        default: return EavesdropperStatusText.notReported
        }
    }
}

/// A seat the server's eavesdropper did not load, with its reason word.
struct ServerSkippedSeat: Decodable, Equatable {
    let seat: String
    let reason: String?

    enum CodingKeys: String, CodingKey { case seat, reason }
}

extension ServerSkippedSeat {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let name = container.optionalText(.seat, limit: 32) else {
            throw DecodingError.dataCorruptedError(
                forKey: .seat, in: container, debugDescription: "A skipped seat with no name is left out."
            )
        }
        seat = name
        reason = container.optionalText(.reason, limit: 24)
    }

    /// Why it was skipped, in plain words.
    var cause: String {
        switch reason {
        case "invalid_section"?: return "Its section of the listener's settings is not valid."
        case "invalid_instance"?: return "Its machine setting is not valid."
        case "invalid_enabled"?: return "Its on or off setting is not valid."
        case "invalid_credentials"?: return "Its credential setting is not valid."
        case "invalid_bot"?: return "Its bot setting is not valid."
        case "shared_environment"?: return "It shares a credential setting with another seat."
        case "invalid_environment"?: return "Its credential setting names something that is not valid."
        case "config_problem"?: return "Its settings have a problem."
        default: return "Its settings have a problem the server did not name."
        }
    }
}

// MARK: - Why there is no reading

/// Why the server's helper could not read the eavesdropper's status.  Each word the helper can
/// answer has its cause in plain words;  the word itself stays in the tooltip.
enum ServerEavesdropperUnavailable: Equatable {
    case dockerUnavailable
    case timeout
    case readFailed
    case tooLarge
    case notJSON
    case moduleUnavailable
    case containerNotRunning
    /// A word this app does not know, or none.
    case other(String?)

    /// Every word the helper answers today, in the order the rollout note lists them.
    static let knownCodes = [
        "docker_unavailable", "timeout", "container_not_running", "read_failed",
        "too_large", "not_json", "module_unavailable",
    ]

    init(errorCode: String?) {
        switch errorCode {
        case "docker_unavailable"?: self = .dockerUnavailable
        case "timeout"?: self = .timeout
        case "read_failed"?: self = .readFailed
        case "too_large"?: self = .tooLarge
        case "not_json"?: self = .notJSON
        case "module_unavailable"?: self = .moduleUnavailable
        case "container_not_running"?: self = .containerNotRunning
        default: self = .other(errorCode)
        }
    }

    /// The helper's own word.
    var errorCode: String? {
        switch self {
        case .dockerUnavailable: return "docker_unavailable"
        case .timeout: return "timeout"
        case .readFailed: return "read_failed"
        case .tooLarge: return "too_large"
        case .notJSON: return "not_json"
        case .moduleUnavailable: return "module_unavailable"
        case .containerNotRunning: return "container_not_running"
        case .other(let code): return code
        }
    }

    /// The cause in plain words.
    var cause: String {
        let gap = EavesdropperStatusText.gap
        switch self {
        case .dockerUnavailable:
            return "The server could not run docker, so it could not read its eavesdropper's status."
        case .timeout:
            return "Reading the eavesdropper's status on the server took too long, so the server gave up."
        case .readFailed:
            return "The server could not read its eavesdropper's status."
        case .tooLarge:
            return "The eavesdropper's status on the server is larger than the server will read."
        case .notJSON:
            return "The eavesdropper's status on the server is not in a form the server can read."
        case .moduleUnavailable:
            return "The server's control helper is missing the part that reads the eavesdropper's status.\(gap)It has to be installed beside the helper on the server."
        case .containerNotRunning:
            return "The eavesdropper is not running on the server."
        case .other:
            return "The server could not read its eavesdropper's status, for a reason this app does not know."
        }
    }

    /// The tooltip:  the cause and the helper's own word.
    var help: String {
        "\(cause)\(EavesdropperStatusText.gap)The helper's reason word is \(errorCode ?? "missing")."
    }
}

// MARK: - The reading

/// What the server's eavesdropper status read came to.
enum ServerEavesdropperReading: Equatable {
    /// The document, in the version this app reads.  Whether it is current is judged against
    /// the clock when it is shown (`ReducedEavesdropperStatus.isCurrent(now:)`).
    case read(ReducedEavesdropperStatus)
    /// No document came back, and why, in words:  the route failed, the helper refused (an
    /// older helper does not have the verb) or the answer could not be read.
    case notRead(String)
    /// The server's helper answered that it could not read the status, and why.
    case unavailable(ServerEavesdropperUnavailable)
    /// A document in a version this app does not read, with what it said.
    case notThisAppsVersion(String)

    /// The reading from one run of `server eavesdropper-status`.
    init(outcome: OpsOutcome) {
        if outcome.kind == .unavailable {
            self = .unavailable(ServerEavesdropperUnavailable(errorCode: outcome.errorCode))
            return
        }
        guard outcome.isSuccess(for: .plan) else {
            if outcome.kind == .refused, outcome.detail.lowercased().contains("unknown server verb") {
                self = .notRead("The server's control helper does not have the eavesdropper status read yet, so the server's eavesdropper is not read.")
                return
            }
            let said = outcome.detail.trimmingCharacters(in: .whitespacesAndNewlines)
            self = .notRead(said.isEmpty ? "The server's eavesdropper status could not be read." : said)
            return
        }
        guard let status = outcome.eavesdropperStatus else {
            self = .notRead("The server answered without its eavesdropper's status.")
            return
        }
        self = Self.reading(of: status)
    }

    /// A document checked:  its version, and that it is the server's.
    static func reading(of status: ReducedEavesdropperStatus) -> ServerEavesdropperReading {
        guard status.isThisAppsVersion else {
            let said = status.schema.map { "version \($0)" } ?? "no version"
            return .notThisAppsVersion(
                "The server's eavesdropper status is in a form this version of Muster does not read (\(said)).\(EavesdropperStatusText.gap)Update Muster to read it."
            )
        }
        // The helper always reduces with the instance `server` (`reduce_status(document,
        // ctx.now(), "server")`), even for an empty document, so an answer that does not say
        // `server` does not vouch for being the server's and is not shown as it.
        guard status.instance == "server" else {
            switch status.instance {
            case "mac"?:
                return .notRead("The answer says it is from the Mac's eavesdropper, not the server's, so it is not shown.")
            case nil:
                return .notRead("The answer does not say it is from the server's eavesdropper, so it is not shown.")
            default:
                return .notRead("The answer says it is from another eavesdropper, not the server's, so it is not shown.")
            }
        }
        return .read(status)
    }

    /// The document, when there is one, current or not.  Its document-level facts (the
    /// Operator's voice, the global pause) say nothing about whether they still hold:  show them
    /// as the present state only through `currentStatus(now:)`.
    var status: ReducedEavesdropperStatus? {
        if case .read(let status) = self { return status }
        return nil
    }

    /// The document only when it is current at `now`, else nil.
    func currentStatus(now: Date) -> ReducedEavesdropperStatus? {
        guard let status = status, status.isCurrent(now: now) else { return nil }
        return status
    }

    /// What the reading says about one seat at `now`.  A seat is called not listed, skipped or
    /// turned off only from a current document;  a document that is not current says only that.
    func seat(_ name: String, now: Date) -> ServerSeatReading {
        switch self {
        case .notRead(let why): return .notRead(why)
        case .unavailable(let reason): return .unavailable(reason)
        case .notThisAppsVersion(let why): return .notThisAppsVersion(why)
        case .read(let status):
            let staleWhy = status.notCurrentReason(now: now)
            if let facts = status.seat(named: name) {
                if let why = staleWhy {
                    return .notCurrent(facts, why: why)
                }
                return .read(facts)
            }
            if let why = staleWhy {
                return .notCurrent(nil, why: why)
            }
            if let skipped = status.skippedSeat(named: name) {
                return .notHeld("The server's eavesdropper did not load this seat.\(EavesdropperStatusText.gap)\(skipped.cause)")
            }
            if status.isDisabled(name) {
                return .notHeld("The server's eavesdropper has this seat turned off in its settings.")
            }
            return .notHeld("The server's eavesdropper does not list this seat.")
        }
    }
}

/// What the server's eavesdropper says about one seat.
enum ServerSeatReading: Equatable {
    /// A current reading with the seat's facts.
    case read(ServerSeatFacts)
    /// A reading that is not current, and why it is not, with the seat's last known facts when
    /// that reading held the seat (nil when it did not:  a stale document never says a seat is
    /// not listed).
    case notCurrent(ServerSeatFacts?, why: String)
    /// A current document was read and does not hold this seat:  not listed, skipped or
    /// turned off.
    case notHeld(String)
    case notRead(String)
    case unavailable(ServerEavesdropperUnavailable)
    case notThisAppsVersion(String)

    /// The seat's facts from a current reading only, or nil.  These are the facts a view may
    /// show as the present state.
    var facts: ServerSeatFacts? {
        if case .read(let facts) = self { return facts }
        return nil
    }

    /// The seat's facts from a reading that is not current, or nil.  Their labels (Connected,
    /// Running) say what the last reading said, not what holds now:  a view must never show
    /// them as the present state, only as the last known one beside `explanation`.
    var lastKnownFacts: ServerSeatFacts? {
        if case .notCurrent(let facts, _) = self { return facts }
        return nil
    }

    /// True only for a current reading.
    var isCurrent: Bool {
        if case .read = self { return true }
        return false
    }

    /// The short answer for a cell.
    var label: String {
        switch self {
        case .read: return "Read"
        case .notCurrent: return "Not current"
        case .notHeld: return "Not listed"
        case .notRead: return "Not read"
        case .unavailable: return "Not available"
        case .notThisAppsVersion: return "Not readable"
        }
    }

    /// Why there is no current reading, in words, or nil for a current one.
    var explanation: String? {
        switch self {
        case .read: return nil
        case .notCurrent(_, let why): return why
        case .notHeld(let why), .notRead(let why), .notThisAppsVersion(let why): return why
        case .unavailable(let reason): return reason.cause
        }
    }
}

// MARK: - Reading it

/// Reads the server eavesdropper's status once over the restricted, read-only route.
///
/// Like `OpsResourcePolicyReader`, it bypasses the confirmation sheet, because
/// `eavesdropper-status --plan` cannot change anything and the helper refuses `--yes` for it.
struct ServerEavesdropperStatusReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read() async -> ServerEavesdropperReading {
        let action = OpsAction.eavesdropperStatus
        do {
            let invocation = try makeBuilder().invocation(for: action, mode: .plan)
            let process = try await runner.run(invocation)
            let outcome = OpsResultParser.parse(process, mode: .plan, action: action, timeout: invocation.timeout)
            return ServerEavesdropperReading(outcome: outcome)
        } catch {
            return .notRead(error.localizedDescription)
        }
    }
}

/// The server eavesdropper's last reading, held for the whole app, with at most one read in
/// flight.  A second request while a read runs waits for that read and starts nothing.  The
/// Seats panel asks it for one seat at a time (`seat(_:)`).
@MainActor
final class ServerEavesdropperStatusStore: ObservableObject {
    /// The one store the app uses.
    static let shared = ServerEavesdropperStatusStore()

    /// How a read is made.  The default asks the server.
    typealias Read = @Sendable () async -> ServerEavesdropperReading

    /// The last reading, or nil before the first one.
    @Published private(set) var reading: ServerEavesdropperReading?
    /// When the last reading came back, on this Mac's clock.
    @Published private(set) var readAt: Date?
    /// Whether a read is running now.
    @Published private(set) var loading = false
    /// The reads this store started.
    private(set) var readsStarted = 0

    /// The read in flight.  It stores its own reading before it ends, so every caller waiting on
    /// it resumes with the reading already in place.
    private var inFlight: Task<Void, Never>?
    private let reader: Read
    private let now: () -> Date

    init(
        reader: @escaping Read = { await ServerEavesdropperStatusReader().read() },
        now: @escaping () -> Date = { Date() }
    ) {
        self.reader = reader
        self.now = now
    }

    /// Read now, unless a read is already running, in which case wait for that one.  Either way
    /// it returns only once the reading is stored.
    func refresh() async {
        if let running = inFlight {
            await running.value
            return
        }
        loading = true
        readsStarted += 1
        let reader = self.reader
        // The shared work is the read and its storing, on the main actor;  the read itself runs
        // off it.  `inFlight` is set below before this body can run, because both are on the
        // main actor and nothing between them suspends.
        let task = Task { @MainActor [weak self] in
            let result = await Task.detached(priority: .utility) {
                await reader()
            }.value
            guard let self else { return }
            self.reading = result
            self.readAt = self.now()
            self.inFlight = nil
            self.loading = false
        }
        inFlight = task
        await task.value
    }

    /// The document only when the last reading is current now, else nil.  Use it, not
    /// `reading?.status`, for the document-level facts (the Operator's voice, the global pause).
    func currentStatus() -> ReducedEavesdropperStatus? {
        reading?.currentStatus(now: now())
    }

    /// Read unless the reading on hand came back less than `maximumAge` ago.
    func refreshIfOlder(than maximumAge: TimeInterval) async {
        if reading != nil, let readAt, now().timeIntervalSince(readAt) < maximumAge {
            return
        }
        await refresh()
    }

    /// What the last reading says about one seat, judged against the clock now.
    func seat(_ name: String) -> ServerSeatReading {
        guard let reading else {
            return .notRead("The server's eavesdropper has not been read yet.")
        }
        return reading.seat(name, now: now())
    }
}
