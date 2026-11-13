import Combine
import Foundation

// The Operator panel's model:  what the server's Operator says about itself, read on the Mac.
//
// The Operator is one service on the server:  the Eavesdropper reads Zulip, one wake router
// sends the wakes, and one letter registry holds the chats.  Muster asks the server's control
// helper for three reduced documents over the existing restricted, read-only route
// (`fleet-ops-ctl`):
//
//   * `server eavesdropper-status`   the Eavesdropper's status (`ServerEavesdropperStatus.swift`)
//   * `server operator-sessions`     the letter registry:  every live chat, newest first
//   * `server operator-wakes`        the wake record of the last 24 hours, by seat
//
// Every read is `--plan` only and changes nothing, so none needs a sheet or Touch ID.  Every
// field of every document is read as optional and a field that is missing, null or of another
// type reads as not reported, never as zero, healthy or live.  A key Muster does not know is
// ignored.  A read that fails says so in plain words in its own section and costs no other.

// MARK: - Words

enum OperatorPanelText {
    static let gap = ServerStatusCopy.sentenceGap

    /// The dash a missing value shows.
    static let none = "\u{2014}"

    /// `held_back` as `Held back`:  a server word made readable, never given a meaning of its own.
    static func humanized(_ word: String) -> String {
        let spaced = word.replacingOccurrences(of: "_", with: " ").trimmingCharacters(in: .whitespaces)
        guard let first = spaced.first else { return none }
        return first.uppercased() + spaced.dropFirst()
    }

    /// A time as the panel's lists show it:  `3m ago`.  Nothing is said for a time that is not
    /// known, and a time a little ahead of this Mac's clock reads as now.
    static func relative(_ date: Date?, now: Date) -> String {
        guard let date else { return none }
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        return "\(FleetTime.age(seconds)) ago"
    }

    /// `1 wake` or `3 wakes`.
    static func counted(_ number: Int, _ singular: String, _ plural: String) -> String {
        "\(number) \(number == 1 ? singular : plural)"
    }
}

/// The colour a state carries.  The words always carry the meaning, and the tone only adds to it.
enum OperatorTone: Equatable {
    case good
    case warning
    case bad
    case neutral
}

// MARK: - What a read came to

/// One server read:  the document, or why there is none.
enum OperatorReading<Value: Equatable>: Equatable {
    case read(Value)
    /// The route to the server failed:  ssh could not connect, the read timed out or could not
    /// start.  Said once at the top of the panel when every read fails this way.
    case unreachable(String)
    /// The server answered, and could not read its Operator's store, refused the read, or
    /// answered in a form this version of Muster does not read.
    case unavailable(String)

    var value: Value? {
        if case .read(let value) = self { return value }
        return nil
    }

    /// Why there is no document, in words, or nil when there is one.
    var failure: String? {
        switch self {
        case .read: return nil
        case .unreachable(let why), .unavailable(let why): return why
        }
    }

    var isUnreachable: Bool {
        if case .unreachable = self { return true }
        return false
    }
}

// MARK: - The helper's envelope

/// The envelope the helper puts around every answer:  `ok`, the verb, the target, a result word,
/// a detail sentence and, for a read it could not make, an error code.  Every field is optional.
struct OperatorEnvelopeHead: Decodable, Equatable {
    let ok: Bool?
    let verb: String?
    let result: String?
    let detail: String?
    let errorCode: String?

    enum CodingKeys: String, CodingKey { case ok, verb, result, detail, errorCode }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = container.optionalField(Bool.self, .ok)
        verb = container.optionalText(.verb, limit: 48)
        result = container.optionalText(.result, limit: 24)
        detail = container.optionalText(.detail, limit: 300)
        errorCode = container.optionalText(.errorCode, limit: 48)
    }
}

/// What the helper's error codes mean, in plain words.  The Operator reads share the status read's
/// codes for the container and add the store's own.
enum OperatorUnavailable {
    static let gap = OperatorPanelText.gap

    static func cause(errorCode: String?, detail: String?, what: String) -> String {
        switch errorCode {
        case "docker_unavailable"?:
            return "The server could not run docker, so it could not read the Operator's \(what)."
        case "timeout"?:
            return "Reading the Operator's \(what) on the server took too long, so the server gave up."
        case "container_not_running"?:
            return "The Eavesdropper is not running on the server."
        case "read_failed"?, "reader_failed"?:
            return "The server could not read the Operator's \(what)."
        case "too_large"?:
            return "The Operator's \(what) is larger than the server will read."
        case "not_json"?:
            return "The Operator's \(what) is not in a form the server can read."
        case "store_missing"?:
            return "The Operator's \(what) is not in the server's container."
        case "store_busy"?:
            return "The Operator's \(what) was busy, so it was not read.\(gap)Refresh in a moment."
        case "store_recovering"?:
            return "The Operator's \(what) is waiting for the Operator to recover it, so it was not read."
        default:
            break
        }
        let said = (detail ?? "").lowercased()
        if said.contains("unknown server verb") || said.contains("not in the allowlist") {
            return "The server's control helper does not have this read yet, so the Operator's \(what) is not read."
        }
        if let detail, !detail.isEmpty { return detail }
        return "The server could not read the Operator's \(what), and did not say why."
    }
}

/// Reads one answer of the helper into an `OperatorReading`.  The same checks apply to every
/// Operator verb, so they are written once.
enum OperatorAnswerReader {
    /// - Parameters:
    ///   - verb: the verb the answer must name when it names one (`server operator-sessions`).
    ///   - what: the store, in words, for the error sentences.
    ///   - decode: the envelope's head and its document, or nil when the output is not JSON.
    ///   - check: why the document is not this app's to show (version, instance), or nil.
    static func reading<Document: Equatable>(
        _ result: OpsProcessResult,
        verb: String,
        what: String,
        decode: (Data) -> (head: OperatorEnvelopeHead, document: Document?)?,
        check: (Document) -> String?
    ) -> OperatorReading<Document> {
        if result.timedOut {
            return .unreachable("The server did not answer in time.")
        }
        guard let decoded = decode(Data(result.stdout.utf8)) else {
            // Only exit 255 means ssh itself could not connect.  Any other failure without an
            // answer means the server was reached and its helper failed, so the route is not blamed.
            if result.exitCode == 255 {
                return .unreachable("The Mac could not reach the server over ssh (the fleet-ops-ctl route).")
            }
            if result.exitCode != 0 {
                return .unavailable("The server's helper failed without an answer (exit \(result.exitCode)).")
            }
            return .unavailable("The server's answer could not be read.")
        }
        let head = decoded.head
        guard head.ok == true else {
            return .unavailable(OperatorUnavailable.cause(errorCode: head.errorCode, detail: head.detail, what: what))
        }
        if let named = head.verb, named != verb {
            return .unavailable("The answer is for another read (\(named)), so it is not shown.")
        }
        guard let document = decoded.document else {
            return .unavailable("The server answered without the Operator's \(what).")
        }
        if let why = check(document) { return .unavailable(why) }
        return .read(document)
    }

    /// Why a document is not shown when it is not the version, or not the instance, this app reads.
    static func mismatch(schema: Int?, expected: Int, instance: String?, what: String) -> String? {
        guard schema == expected else {
            let said = schema.map { "version \($0)" } ?? "no version"
            return "The Operator's \(what) is in a form this version of Muster does not read (\(said)).\(OperatorPanelText.gap)Update Muster to read it."
        }
        guard instance == "server" else {
            return "The answer does not say it is from the server's Operator, so it is not shown."
        }
        return nil
    }
}

// MARK: - The letter registry

/// The `operator-sessions` document:
///
///     {"schema": 1, "generated_at", "instance": "server", "counts": {"active", "closed"},
///      "truncated", "dropped", "sessions": [{"letter", "generation", "seat", "platform",
///        "host", "host_code", "status", "wake", "registered_at", "last_seen_at", "issues"}]}
struct OperatorSessionsDocument: Decodable, Equatable {
    static let schemaVersion = 1
    static let verb = "server operator-sessions"

    let schema: Int?
    let generatedAtText: String?
    let instance: String?
    /// Live chats, as the registry counts them (every one, not only the ones listed).
    let activeCount: Int?
    /// Chats that have gone.  They are counted and not listed.
    let closedCount: Int?
    let truncated: Bool?
    let dropped: Int?
    let sessions: [OperatorSession]

    enum CodingKeys: String, CodingKey {
        case schema, instance, counts, truncated, dropped, sessions
        case generatedAtText = "generated_at"
    }

    private struct Counts: Decodable {
        let active: Int?
        let closed: Int?
        enum CodingKeys: String, CodingKey { case active, closed }
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            active = container.optionalCount(.active)
            closed = container.optionalCount(.closed)
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = container.optionalField(Int.self, .schema)
        generatedAtText = container.optionalText(.generatedAtText, limit: 40)
        instance = container.optionalText(.instance, limit: 24)
        let counts = container.optionalField(Counts.self, .counts)
        activeCount = counts?.active
        closedCount = counts?.closed
        truncated = container.optionalField(Bool.self, .truncated)
        dropped = container.optionalCount(.dropped)
        sessions = container.lenientList(OperatorSession.self, .sessions)
    }

    init(
        schema: Int? = 1, generatedAtText: String? = nil, instance: String? = "server",
        activeCount: Int? = nil, closedCount: Int? = nil, truncated: Bool? = nil, dropped: Int? = nil,
        sessions: [OperatorSession] = []
    ) {
        self.schema = schema
        self.generatedAtText = generatedAtText
        self.instance = instance
        self.activeCount = activeCount
        self.closedCount = closedCount
        self.truncated = truncated
        self.dropped = dropped
        self.sessions = sessions
    }
}

/// One chat the registry holds.  The registry sends no title, topic, link or session id, and
/// nothing here asks for one.
struct OperatorSession: Decodable, Equatable {
    /// The session letter, `A` to `ZZ`.
    let letter: String?
    /// Which time this letter was given out, when the registry says.
    let generation: Int?
    let seat: String
    let platform: String?
    /// `mac`, `server` or `cloud`.
    let host: String?
    /// `M`, `S` or `C`.
    let hostCode: String?
    /// `active` or `closed`.
    let status: String?
    /// `code-supported`, `none` or `unknown`.
    let wake: String?
    let registeredAtText: String?
    let lastSeenAtText: String?
    let issues: [String]

    enum CodingKeys: String, CodingKey {
        case letter, generation, seat, platform, host, status, wake, issues
        case hostCode = "host_code"
        case registeredAtText = "registered_at"
        case lastSeenAtText = "last_seen_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let name = container.optionalText(.seat, limit: 32) else {
            throw DecodingError.dataCorruptedError(
                forKey: .seat, in: container, debugDescription: "A chat with no seat is left out.")
        }
        seat = name
        letter = container.optionalText(.letter, limit: 2)
        generation = container.optionalCount(.generation)
        platform = container.optionalText(.platform, limit: 32)
        host = container.optionalText(.host, limit: 12)
        hostCode = container.optionalText(.hostCode, limit: 2)
        status = container.optionalText(.status, limit: 12)
        wake = container.optionalText(.wake, limit: 16)
        registeredAtText = container.optionalText(.registeredAtText, limit: 40)
        lastSeenAtText = container.optionalText(.lastSeenAtText, limit: 40)
        issues = container.lenientList(String.self, .issues)
            .compactMap { EavesdropperStatusText.plain($0, limit: 16) }
    }

    init(
        seat: String, letter: String? = nil, generation: Int? = nil, platform: String? = nil,
        host: String? = nil, hostCode: String? = nil, status: String? = nil, wake: String? = nil,
        registeredAt: String? = nil, lastSeenAt: String? = nil, issues: [String] = []
    ) {
        self.seat = seat
        self.letter = letter
        self.generation = generation
        self.platform = platform
        self.host = host
        self.hostCode = hostCode
        self.status = status
        self.wake = wake
        self.registeredAtText = registeredAt
        self.lastSeenAtText = lastSeenAt
        self.issues = issues
    }

    var lastSeen: Date? { lastSeenAtText.flatMap(FleetTime.parse) }
    var registeredAt: Date? { registeredAtText.flatMap(FleetTime.parse) }
}

private struct OperatorSessionsEnvelope: Decodable {
    let head: OperatorEnvelopeHead
    let sessions: OperatorSessionsDocument?

    enum CodingKeys: String, CodingKey { case sessions }

    init(from decoder: Decoder) throws {
        head = try OperatorEnvelopeHead(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessions = container.optionalField(OperatorSessionsDocument.self, .sessions)
    }
}

enum OperatorSessionsDecoder {
    static func reading(_ result: OpsProcessResult) -> OperatorReading<OperatorSessionsDocument> {
        OperatorAnswerReader.reading(
            result, verb: OperatorSessionsDocument.verb, what: "letter registry",
            decode: { data in
                guard let envelope = try? JSONDecoder().decode(OperatorSessionsEnvelope.self, from: data) else { return nil }
                return (envelope.head, envelope.sessions)
            },
            check: { OperatorAnswerReader.mismatch(schema: $0.schema, expected: OperatorSessionsDocument.schemaVersion, instance: $0.instance, what: "letter registry") }
        )
    }
}

// MARK: - The wake record

/// The `operator-wakes` document, kept to what the panel shows:  the totals, and each seat's
/// recent wakes and counts.
///
///     {"schema": 1, "generated_at", "instance": "server", "window_hours": 24,
///      "totals": {"recent", "held_back", "held_back_lost", "owner_queue"}, "truncated",
///      "seats": [{"seat", "recent": {"total", "by_state", "rows": [...]},
///                 "held_back": {"count", "lost", "reasons", "rows"},
///                 "owner_queue": {"pending", "rows"}}], "unreadable": [name]}
struct OperatorWakesDocument: Decodable, Equatable {
    static let schemaVersion = 1
    static let verb = "server operator-wakes"

    let schema: Int?
    let generatedAtText: String?
    let instance: String?
    let windowHours: Int?
    let totals: OperatorWakeTotals?
    let truncated: Bool?
    let seats: [OperatorWakeSeat]
    /// Seats whose wake record could not be read.
    let unreadable: [String]

    enum CodingKeys: String, CodingKey {
        case schema, instance, totals, truncated, seats, unreadable
        case generatedAtText = "generated_at"
        case windowHours = "window_hours"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = container.optionalField(Int.self, .schema)
        generatedAtText = container.optionalText(.generatedAtText, limit: 40)
        instance = container.optionalText(.instance, limit: 24)
        windowHours = container.optionalCount(.windowHours)
        totals = container.optionalField(OperatorWakeTotals.self, .totals)
        truncated = container.optionalField(Bool.self, .truncated)
        seats = container.lenientList(OperatorWakeSeat.self, .seats)
        unreadable = container.lenientList(String.self, .unreadable)
            .compactMap { EavesdropperStatusText.plain($0, limit: 32) }
    }

    init(
        schema: Int? = 1, instance: String? = "server", windowHours: Int? = 24,
        totals: OperatorWakeTotals? = nil, truncated: Bool? = nil,
        seats: [OperatorWakeSeat] = [], unreadable: [String] = []
    ) {
        self.schema = schema
        self.generatedAtText = nil
        self.instance = instance
        self.windowHours = windowHours
        self.totals = totals
        self.truncated = truncated
        self.seats = seats
        self.unreadable = unreadable
    }
}

struct OperatorWakeTotals: Decodable, Equatable {
    let recent: Int?
    let heldBack: Int?
    let heldBackLost: Int?
    let ownerQueue: Int?

    enum CodingKeys: String, CodingKey {
        case recent
        case heldBack = "held_back"
        case heldBackLost = "held_back_lost"
        case ownerQueue = "owner_queue"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        recent = container.optionalCount(.recent)
        heldBack = container.optionalCount(.heldBack)
        heldBackLost = container.optionalCount(.heldBackLost)
        ownerQueue = container.optionalCount(.ownerQueue)
    }

    init(recent: Int? = nil, heldBack: Int? = nil, heldBackLost: Int? = nil, ownerQueue: Int? = nil) {
        self.recent = recent
        self.heldBack = heldBack
        self.heldBackLost = heldBackLost
        self.ownerQueue = ownerQueue
    }
}

/// One seat's wake record.  The server lists at most ten recent wakes for a seat and counts all
/// of them in `recentTotal`.
struct OperatorWakeSeat: Decodable, Equatable {
    let seat: String
    let recentTotal: Int?
    let recent: [OperatorWake]

    enum CodingKeys: String, CodingKey { case seat, recent }

    private struct Recent: Decodable {
        let total: Int?
        let rows: [OperatorWake]
        enum CodingKeys: String, CodingKey { case total, rows }
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            total = container.optionalCount(.total)
            rows = container.lenientList(OperatorWake.self, .rows)
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let name = container.optionalText(.seat, limit: 32) else {
            throw DecodingError.dataCorruptedError(
                forKey: .seat, in: container, debugDescription: "A seat with no name is left out.")
        }
        seat = name
        let recent = container.optionalField(Recent.self, .recent)
        recentTotal = recent?.total
        self.recent = recent?.rows ?? []
    }

    init(seat: String, recentTotal: Int? = nil, recent: [OperatorWake] = []) {
        self.seat = seat
        self.recentTotal = recentTotal
        self.recent = recent
    }
}

/// One wake of the last day:  its state word, when it happened and, for a wake that stopped
/// before it ran, the server's reason word.  The record has no body, topic or sender.
struct OperatorWake: Decodable, Equatable {
    let wakeID: String?
    let state: String?
    let atText: String?
    let reason: String?
    let dm: Bool?
    let owner: Bool?
    let catchUp: Bool?

    enum CodingKeys: String, CodingKey {
        case state, reason, dm, owner
        case wakeID = "wake_id"
        case atText = "at"
        case catchUp = "catch_up"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        wakeID = container.optionalText(.wakeID, limit: 80)
        state = container.optionalText(.state, limit: 40)
        atText = container.optionalText(.atText, limit: 40)
        reason = container.optionalText(.reason, limit: 40)
        dm = container.optionalField(Bool.self, .dm)
        owner = container.optionalField(Bool.self, .owner)
        catchUp = container.optionalField(Bool.self, .catchUp)
    }

    init(wakeID: String? = nil, state: String? = nil, at: String? = nil, reason: String? = nil,
         dm: Bool? = nil, owner: Bool? = nil, catchUp: Bool? = nil) {
        self.wakeID = wakeID
        self.state = state
        self.atText = at
        self.reason = reason
        self.dm = dm
        self.owner = owner
        self.catchUp = catchUp
    }

    var at: Date? { atText.flatMap(FleetTime.parse) }
}

private struct OperatorWakesEnvelope: Decodable {
    let head: OperatorEnvelopeHead
    let wakes: OperatorWakesDocument?

    enum CodingKeys: String, CodingKey { case wakes }

    init(from decoder: Decoder) throws {
        head = try OperatorEnvelopeHead(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        wakes = container.optionalField(OperatorWakesDocument.self, .wakes)
    }
}

enum OperatorWakesDecoder {
    static func reading(_ result: OpsProcessResult) -> OperatorReading<OperatorWakesDocument> {
        OperatorAnswerReader.reading(
            result, verb: OperatorWakesDocument.verb, what: "wake record",
            decode: { data in
                guard let envelope = try? JSONDecoder().decode(OperatorWakesEnvelope.self, from: data) else { return nil }
                return (envelope.head, envelope.wakes)
            },
            check: { OperatorAnswerReader.mismatch(schema: $0.schema, expected: OperatorWakesDocument.schemaVersion, instance: $0.instance, what: "wake record") }
        )
    }
}

// MARK: - The readers

/// Reads the letter registry once over the restricted, read-only route.
struct OperatorSessionsReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read() async -> OperatorReading<OperatorSessionsDocument> {
        let invocation = makeBuilder().operatorSessionsInvocation()
        do {
            return OperatorSessionsDecoder.reading(try await runner.run(invocation))
        } catch {
            return .unreachable("The Mac could not start the ssh route to the server.")
        }
    }
}

/// Reads the wake record once over the restricted, read-only route.
struct OperatorWakesReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read() async -> OperatorReading<OperatorWakesDocument> {
        let invocation = makeBuilder().operatorWakesInvocation()
        do {
            return OperatorWakesDecoder.reading(try await runner.run(invocation))
        } catch {
            return .unreachable("The Mac could not start the ssh route to the server.")
        }
    }
}

// MARK: - The store

/// What the Operator panel holds:  the three server reads, each kept on its own, so a read that
/// failed never blanks another.  At most one refresh runs at a time, and a second request while
/// one runs waits for it and starts nothing.
@MainActor
final class OperatorPanelStore: ObservableObject {
    /// The one store the app uses.
    static let shared = OperatorPanelStore()

    /// How the Eavesdropper's status is read.  The default goes through the Seats panel's own
    /// store, so the two panels never run the same read twice at once.
    typealias StatusRead = @MainActor () async -> ServerEavesdropperReading
    typealias SessionsRead = @Sendable () async -> OperatorReading<OperatorSessionsDocument>
    typealias WakesRead = @Sendable () async -> OperatorReading<OperatorWakesDocument>

    @Published private(set) var status: ServerEavesdropperReading?
    @Published private(set) var sessions: OperatorReading<OperatorSessionsDocument>?
    @Published private(set) var wakes: OperatorReading<OperatorWakesDocument>?
    /// When the last refresh that read at least one answer finished, on this Mac's clock.  Nil when
    /// the last refresh read nothing.
    @Published private(set) var readAt: Date?
    /// When the last refresh finished, whatever it read, on this Mac's clock.
    @Published private(set) var triedAt: Date?
    @Published private(set) var loading = false
    /// The refreshes this store started.
    private(set) var refreshesStarted = 0

    private var inFlight: Task<Void, Never>?
    private let statusRead: StatusRead
    private let sessionsRead: SessionsRead
    private let wakesRead: WakesRead
    private let now: () -> Date

    init(
        statusRead: @escaping StatusRead = {
            let shared = ServerEavesdropperStatusStore.shared
            await shared.refresh()
            return shared.reading ?? .notRead("The server's eavesdropper has not been read yet.")
        },
        sessionsRead: @escaping SessionsRead = { await OperatorSessionsReader().read() },
        wakesRead: @escaping WakesRead = { await OperatorWakesReader().read() },
        now: @escaping () -> Date = { Date() }
    ) {
        self.statusRead = statusRead
        self.sessionsRead = sessionsRead
        self.wakesRead = wakesRead
        self.now = now
    }

    private enum Piece {
        case status(ServerEavesdropperReading)
        case sessions(OperatorReading<OperatorSessionsDocument>)
        case wakes(OperatorReading<OperatorWakesDocument>)
    }

    /// Reads the three answers together, each stored as it arrives.  Called while a refresh runs,
    /// it waits for that one.  Either way it returns once every read has ended.
    func refresh() async {
        if let running = inFlight {
            await running.value
            return
        }
        loading = true
        refreshesStarted += 1
        let statusRead = self.statusRead
        let sessionsRead = self.sessionsRead
        let wakesRead = self.wakesRead
        // `inFlight` is set below before this body can run:  both are on the main actor and
        // nothing between them suspends.
        let task = Task { @MainActor [weak self] in
            await withTaskGroup(of: Piece.self) { group in
                group.addTask { @MainActor in .status(await statusRead()) }
                group.addTask { .sessions(await sessionsRead()) }
                group.addTask { .wakes(await wakesRead()) }
                for await piece in group {
                    guard let self else { continue }
                    switch piece {
                    case .status(let reading): self.status = reading
                    case .sessions(let reading): self.sessions = reading
                    case .wakes(let reading): self.wakes = reading
                    }
                }
            }
            guard let self else { return }
            let finished = self.now()
            var statusRead = false
            if case .read? = self.status { statusRead = true }
            let anyRead = statusRead || self.sessions?.value != nil || self.wakes?.value != nil
            self.triedAt = finished
            self.readAt = anyRead ? finished : nil
            self.inFlight = nil
            self.loading = false
        }
        inFlight = task
        await task.value
    }
}

// MARK: - What the panel says

/// One of the three cards at the top.
struct OperatorStatusCard: Equatable {
    let title: String
    /// The state in words:  `Listening`, `Paused`, `Not reporting`.
    let state: String
    let tone: OperatorTone
    /// Lines under the state, each a plain sentence or fact.
    let lines: [String]
}

/// A cell that says one thing and, in its tooltip, why.
struct OperatorCell: Equatable {
    let text: String
    let tone: OperatorTone
    let help: String
}

/// How Seats And Chats groups its rows.
enum OperatorSeatGrouping: String, CaseIterable, Identifiable {
    case byPlatform
    case bySeat
    case flat

    var id: String { rawValue }

    var label: String {
        switch self {
        case .byPlatform: return "By platform"
        case .bySeat: return "By seat"
        case .flat: return "All rows"
        }
    }
}

/// Which chats Seats And Chats shows.
enum OperatorSeatFilter: String, CaseIterable, Identifiable {
    case all
    case live
    case problems

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "All"
        case .live: return "Live"
        case .problems: return "Attention"
        }
    }
}

/// One row of Seats And Chats.
struct OperatorSeatRow: Equatable, Identifiable {
    let id: String
    let seat: String
    let letter: String
    let host: String
    let state: OperatorCell
    let wake: OperatorCell
    let router: OperatorCell
    let lastSeen: String
    var platform: String = "Other"
    var issues: [String] = []

    var isLive: Bool {
        state.text == "Live"
    }

    var hasProblem: Bool {
        state.tone == .warning || wake.tone == .warning || router.tone == .warning || !issues.isEmpty
    }
}

/// One collapsible group of chats in Seats And Chats.
struct OperatorSeatGroup: Equatable, Identifiable {
    let id: String
    let title: String
    let liveCount: Int
    let goneCount: Int
    let problemCount: Int
    let seatsSummary: String?
    let rows: [OperatorSeatRow]
}

struct OperatorSeatsTable: Equatable {
    let rows: [OperatorSeatRow]
    /// Live and gone chats the table does not list, past its row limit.
    let moreCount: Int
    let note: String?
    var totalMatchingCount: Int = 0
    var groups: [OperatorSeatGroup] = []
}

/// How Recent Wakes groups its rows.
enum OperatorWakeGrouping: String, CaseIterable, Identifiable {
    case newestFirst
    case bySeat

    var id: String { rawValue }

    var label: String {
        switch self {
        case .newestFirst: return "Newest first"
        case .bySeat: return "By seat"
        }
    }
}

/// Which wakes Recent Wakes shows.
enum OperatorWakeFilter: String, CaseIterable, Identifiable {
    case all
    case problems
    case ownerOrDM

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "All"
        case .problems: return "Stopped"
        case .ownerOrDM: return "Owner / DM"
        }
    }
}

/// One row of Recent Wakes.
struct OperatorWakeLine: Equatable, Identifiable {
    let id: String
    let time: String
    let seat: String
    let state: OperatorCell
    let reason: String
    var isOwnerOrDM: Bool = false

    var isProblem: Bool {
        state.tone == .warning
    }
}

/// One collapsible per-seat group of wakes.
struct OperatorWakeSeatGroup: Equatable, Identifiable {
    let id: String
    let seat: String
    let problemCount: Int
    let rows: [OperatorWakeLine]
}

struct OperatorWakesTable: Equatable {
    let rows: [OperatorWakeLine]
    let moreCount: Int
    /// Held back and waiting for the owner, shown only when either is not zero.
    let summary: String?
    let notes: [String]
    var totalLoadedCount: Int = 0
    var loadedMoreCount: Int = 0
    var unloadedServerCount: Int = 0
    var seatGroups: [OperatorWakeSeatGroup] = []
}

/// Everything the Operator panel says, computed from the readings and a clock, so each card,
/// row and word is a plain function with a test.
enum OperatorPanelPresentation {
    static let gap = OperatorPanelText.gap

    /// How many chats the table lists.
    static let seatRowLimit = 30
    /// How many wakes the list shows.
    static let wakeRowLimit = 15
    /// How long after its last pass the registry's stale sweep is called late.  The sweep runs
    /// every fifteen minutes by default, and the document does not carry the interval, so a pass
    /// more than an hour old (four default intervals) is late.
    static let sweepLate: TimeInterval = 3600

    /// Why a read has no document, in one line:  the cause the read itself gave, so a section never
    /// hides what failed behind the banner (which says what to do about it).
    static func shortFailure<Value: Equatable>(_ reading: OperatorReading<Value>, fallback: String) -> String {
        reading.failure ?? fallback
    }

    // MARK: The Eavesdropper

    static func eavesdropper(_ reading: ServerEavesdropperReading?, now: Date) -> OperatorStatusCard {
        let title = "Eavesdropper"
        guard let reading else {
            return OperatorStatusCard(title: title, state: "Reading\u{2026}", tone: .neutral, lines: [])
        }
        switch reading {
        case .notRead(let why):
            return OperatorStatusCard(title: title, state: "Not reporting", tone: .bad, lines: [why])
        case .unavailable(let reason):
            return OperatorStatusCard(title: title, state: "Not reporting", tone: .bad, lines: [reason.cause])
        case .notThisAppsVersion(let why):
            return OperatorStatusCard(title: title, state: "Not reporting", tone: .bad, lines: [why])
        case .read(let status):
            if let why = status.notCurrentReason(now: now) {
                return OperatorStatusCard(title: title, state: "Not reporting", tone: .bad, lines: [why])
            }
            var lines: [String] = []
            var tone: OperatorTone = .good
            let state: String
            if status.pause?.paused == true {
                state = "Paused"
                tone = .warning
                if status.pause?.wakesOnly == true {
                    lines.append("Wakes are paused.\(gap)Mentions are still read.")
                } else {
                    lines.append("It reads nothing and wakes nothing while paused.")
                }
                if let since = status.pause?.since {
                    lines.append("Paused since \(EavesdropperStatusText.time(since, now: now)).")
                }
            } else {
                state = "Listening"
                if status.pause?.paused == nil {
                    // The colour must not claim a state the server did not report.
                    tone = .neutral
                    lines.append("Whether it is paused is not reported.")
                }
            }
            if status.pauseFileError != nil {
                tone = .warning
                lines.append("Its pause setting could not be read.")
            }
            if let newest = status.seats.compactMap(\.lastEvent).max() {
                lines.append("Last Zulip event \(OperatorPanelText.relative(newest, now: now)).")
            } else {
                lines.append("No Zulip event reported.")
            }
            if !status.seats.isEmpty, state == "Listening" {
                let connected = status.seats.filter { $0.connected == true }.count
                if connected == 0 {
                    tone = .warning
                    lines.append("No seat is connected.")
                }
            }
            switch status.operatorVoiceReading {
            case .cannotPost(let code, _):
                tone = tone == .good ? .warning : tone
                let cause = OperatorVoiceReading.cause(code ?? "")
                lines.append("It cannot post to Zulip\(cause.map { ": \($0)" } ?? ".")")
            case .canPost, .notReported:
                break
            }
            return OperatorStatusCard(title: title, state: state, tone: tone, lines: lines)
        }
    }

    // MARK: The wake router

    /// Whether the seats that wake through the router are ready, from the status's per-seat
    /// `routine` readiness, and how many wakes the last day held.  The router's own run state is
    /// not in these reads, so the card never says Running:  it says Ready, Some not ready, or
    /// Not read here.
    static func router(
        wakes: OperatorReading<OperatorWakesDocument>?,
        status: ServerEavesdropperReading?,
        now: Date
    ) -> OperatorStatusCard {
        let title = "Wake router"
        guard let wakes else {
            return OperatorStatusCard(title: title, state: "Reading\u{2026}", tone: .neutral, lines: [])
        }
        guard let document = wakes.value else {
            return OperatorStatusCard(title: title, state: "Not read", tone: .warning,
                                      lines: ["The wake record was not read.",
                                              shortFailure(wakes, fallback: "The server did not report its wake record.")])
        }
        var lines: [String] = []
        var state = "Not read here"
        var tone: OperatorTone = .neutral
        if let current = status?.currentStatus(now: now) {
            let onRouter = current.seats.filter { seat in
                switch seat.router {
                case .ready, .notReady, .readinessNotReported: return true
                case .captureOnly, .notSaid: return false
                }
            }
            if !onRouter.isEmpty {
                let notReady = onRouter.filter { $0.router.isProblem }
                let unknown = onRouter.filter { $0.router == .readinessNotReported }
                if !notReady.isEmpty {
                    state = "Some not ready"
                    tone = .warning
                    lines.append("\(notReady.count) of \(onRouter.count) seats that wake through it \(notReady.count == 1 ? "is" : "are") not ready.")
                } else if unknown.isEmpty {
                    state = "Ready"
                    tone = .good
                    lines.append("All \(onRouter.count) seats that wake through it are ready.")
                } else {
                    lines.append("Whether \(unknown.count) of its \(onRouter.count) seats are ready is not reported.")
                }
            }
        }
        if state == "Not read here" {
            lines.append("The router's own state is not in what the server reports.")
        }
        let total = document.totals?.recent
        let isFloor = !document.unreadable.isEmpty || document.truncated == true
        if let total {
            let words = OperatorPanelText.counted(total, "wake", "wakes")
            lines.append(isFloor ? "At least \(words) in the last 24 hours." : "\(words) in the last 24 hours.")
        } else {
            lines.append("The number of wakes in the last 24 hours is not reported.")
        }
        return OperatorStatusCard(title: title, state: state, tone: tone, lines: lines)
    }

    // MARK: The letter registry

    /// What needs a look in the registry:  a sweep that did not finish cleanly, is late, or held
    /// chats back, and live chats that cannot be woken.  Nothing is said from a status that is
    /// not current, because a sweep's last word is history by then.
    static func registryWarnings(
        sessions: OperatorReading<OperatorSessionsDocument>?,
        status: ServerEavesdropperReading?,
        now: Date
    ) -> [String] {
        var warnings: [String] = []
        if let sweep = status?.currentStatus(now: now)?.registrySweep, sweep.isReported {
            if sweep.isProblem {
                warnings.append("The registry's last stale chat sweep did not finish cleanly (\(sweep.label)).")
            }
            if let ranAt = sweep.ranAt {
                if now.timeIntervalSince(ranAt) > sweepLate {
                    warnings.append("The registry's stale chat sweep last ran at \(EavesdropperStatusText.time(ranAt, now: now)), more than an hour ago.")
                }
            } else {
                warnings.append("The registry does not say when its stale chat sweep last ran.")
            }
            if (sweep.refused ?? 0) > 0 || !sweep.refusedPlatforms.isEmpty {
                let who = sweep.refusedPlatforms.isEmpty ? "" : " (\(sweep.refusedPlatforms.joined(separator: ", ")))"
                warnings.append("The sweep's share guard held back some chats\(who) and left them open.")
            }
        }
        if let document = sessions?.value {
            let unwakeable = document.sessions.filter { $0.status == "active" && ($0.wake == "none" || $0.wake == "unknown") }.count
            if unwakeable > 0 {
                let words = unwakeable == 1 ? "1 live chat has" : "\(unwakeable) live chats have"
                warnings.append("\(words) no known wake route.")
            }
        }
        return warnings
    }

    static func registry(
        sessions: OperatorReading<OperatorSessionsDocument>?,
        status: ServerEavesdropperReading?,
        now: Date
    ) -> OperatorStatusCard {
        let title = "Letter registry"
        guard let sessions else {
            return OperatorStatusCard(title: title, state: "Reading\u{2026}", tone: .neutral, lines: [])
        }
        guard let document = sessions.value else {
            return OperatorStatusCard(title: title, state: "Not reporting", tone: .warning,
                                      lines: [shortFailure(sessions, fallback: "The server did not report its registry.")])
        }
        let warnings = registryWarnings(sessions: sessions, status: status, now: now)
        var lines: [String] = []
        let state = document.activeCount.map { "\($0) live" } ?? "Not reported"
        lines.append(document.closedCount.map { "\($0) gone" } ?? "Gone chats not reported")
        if warnings.isEmpty {
            lines.append("No warnings")
        } else {
            lines.append(OperatorPanelText.counted(warnings.count, "warning", "warnings"))
        }
        let tone: OperatorTone = document.activeCount == nil ? .neutral : (warnings.isEmpty ? .good : .warning)
        return OperatorStatusCard(title: title, state: state, tone: tone, lines: lines)
    }

    // MARK: Seats and chats

    static func hostLabel(code: String?, host: String?) -> String {
        switch code ?? host?.uppercased().prefix(1).description {
        case "M"?: return "Mac"
        case "S"?: return "Server"
        case "C"?: return "Cloud"
        default: return "Not known"
        }
    }

    static func platformLabel(_ raw: String?) -> String {
        guard let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
            return "Other"
        }
        switch trimmed.lowercased() {
        case "claude", "claude-code", "claude_code": return "Claude"
        case "codex", "openai-codex": return "Codex"
        case "cursor": return "Cursor"
        case "antigravity", "agy": return "Antigravity"
        case "gemini", "gemini-cli": return "Gemini"
        case "grok", "grok-build", "gb": return "Grok"
        case "minimax", "mm": return "MiniMax"
        case "opencode": return "OpenCode"
        case "botfleet", "bf": return "BotFleet"
        case "chat": return "Chat"
        default: return OperatorPanelText.humanized(trimmed)
        }
    }

    static func stateCell(_ status: String?) -> OperatorCell {
        switch status {
        case "active"?: return OperatorCell(text: "Live", tone: .good, help: "The registry holds this chat as live.")
        case "closed"?: return OperatorCell(text: "Gone", tone: .neutral, help: "The registry has closed this chat.")
        default: return OperatorCell(text: "Not known", tone: .neutral, help: "The registry did not say whether this chat is live.")
        }
    }

    static func wakeCell(_ wake: String?) -> OperatorCell {
        switch wake {
        case "code-supported"?:
            return OperatorCell(text: "Can be woken", tone: .good, help: "The chat's platform supports a wake by its letter.")
        case "none"?:
            return OperatorCell(text: "No wake route", tone: .warning, help: "Nothing can wake this chat when a message arrives.")
        default:
            return OperatorCell(text: "Not known", tone: .neutral, help: "The registry did not say whether this chat can be woken.")
        }
    }

    /// Where a seat's wakes go, from the status's per-seat `routine`:  on the router, or why not.
    static func routerCell(seat: String, status: ServerEavesdropperReading?, now: Date) -> OperatorCell {
        guard let status else {
            return OperatorCell(text: "Not read yet", tone: .neutral, help: "The Eavesdropper's status has not been read yet.")
        }
        switch status.seat(seat, now: now) {
        case .read(let facts):
            return routerCell(facts)
        case .notCurrent(_, let why):
            return OperatorCell(text: "Not current", tone: .neutral, help: why)
        case .notHeld(let why):
            return OperatorCell(text: "Not listed", tone: .neutral, help: why)
        case .notRead(let why), .notThisAppsVersion(let why):
            return OperatorCell(text: "Not read", tone: .neutral, help: why)
        case .unavailable(let reason):
            return OperatorCell(text: "Not read", tone: .neutral, help: reason.cause)
        }
    }

    static func routerCell(_ facts: ServerSeatFacts) -> OperatorCell {
        switch facts.router {
        case .ready:
            return OperatorCell(text: "On the router", tone: .good, help: "Its wakes go to the router, and the router is ready.")
        case .notReady(let why):
            let text: String
            switch facts.routine?.why {
            case "routine_not_configured"?: text = "Not configured"
            case "routine_key_refused"?: text = "Key refused"
            case "routine_url_refused"?: text = "Address refused"
            default: text = "Not ready"
            }
            return OperatorCell(text: text, tone: .warning, help: "Its wakes go to the router, but the router is not ready: \(why).")
        case .readinessNotReported:
            return OperatorCell(text: "Not reported", tone: .neutral, help: "Its wakes go to the router, and the server did not say whether it is ready.")
        case .captureOnly:
            return OperatorCell(text: "Inbox only", tone: .neutral, help: "Its mentions are kept in its inbox, and nothing wakes it.")
        case .notSaid:
            return OperatorCell(text: "Not reported", tone: .neutral, help: "The server did not say how this seat is woken.")
        }
    }

    /// `II.2` when the registry gives a generation, `II` when it gives only the letter.
    static func letterText(letter: String?, generation: Int?) -> String {
        guard let letter else { return OperatorPanelText.none }
        guard let generation else { return letter }
        return "\(letter).\(generation)"
    }

    static func seatsTable(
        _ document: OperatorSessionsDocument,
        status: ServerEavesdropperReading?,
        now: Date,
        limit: Int = seatRowLimit,
        filter: OperatorSeatFilter = .all,
        search: String = "",
        grouping: OperatorSeatGrouping = .byPlatform
    ) -> OperatorSeatsTable {
        func rank(_ session: OperatorSession) -> Int {
            switch session.status {
            case "active"?: return 0
            case "closed"?: return 2
            default: return 1
            }
        }
        let ordered = document.sessions.enumerated().sorted { a, b in
            let (x, y) = (a.element, b.element)
            if rank(x) != rank(y) { return rank(x) < rank(y) }
            let (xs, ys) = (x.lastSeen ?? x.registeredAt, y.lastSeen ?? y.registeredAt)
            switch (xs, ys) {
            case let (l?, r?) where l != r: return l > r
            case (_?, nil): return true
            case (nil, _?): return false
            default: return a.offset < b.offset
            }
        }
        var routerCache: [String: OperatorCell] = [:]
        func cachedRouter(for seat: String) -> OperatorCell {
            if let cached = routerCache[seat] { return cached }
            let computed = routerCell(seat: seat, status: status, now: now)
            routerCache[seat] = computed
            return computed
        }
        func cleanedIssues(for session: OperatorSession) -> [String] {
            session.issues
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
                .map { $0.contains(" ") ? $0 : OperatorPanelText.humanized($0) }
        }

        let needle = search.trimmingCharacters(in: .whitespacesAndNewlines)
        let matchingSessions: [(offset: Int, element: OperatorSession)]
        if filter == .all && needle.isEmpty {
            matchingSessions = ordered
        } else {
            matchingSessions = ordered.filter { _, session in
                let state = stateCell(session.status)
                let wake = wakeCell(session.wake)
                let router = cachedRouter(for: session.seat)
                let issues = session.issues.contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
                let hasProblem = (state.tone == .warning) || (wake.tone == .warning) || (router.tone == .warning) || issues
                switch filter {
                case .all: break
                case .live: guard state.text == "Live" else { return false }
                case .problems: guard hasProblem else { return false }
                }
                guard !needle.isEmpty else { return true }
                let fields = [
                    session.seat,
                    letterText(letter: session.letter, generation: session.generation),
                    platformLabel(session.platform),
                    hostLabel(code: session.hostCode, host: session.host),
                    state.text,
                    wake.text,
                    router.text,
                ] + cleanedIssues(for: session)
                return fields.contains { $0.localizedCaseInsensitiveContains(needle) }
            }
        }
        let visibleSessions = limit < Int.max ? Array(matchingSessions.prefix(max(0, limit))) : matchingSessions
        let rows = visibleSessions.map { index, session in
            OperatorSeatRow(
                id: "\(index)|\(session.seat)|\(session.letter ?? "-")|\(session.generation ?? 0)",
                seat: session.seat,
                letter: letterText(letter: session.letter, generation: session.generation),
                host: hostLabel(code: session.hostCode, host: session.host),
                state: stateCell(session.status),
                wake: wakeCell(session.wake),
                router: cachedRouter(for: session.seat),
                lastSeen: OperatorPanelText.relative(session.lastSeen, now: now),
                platform: platformLabel(session.platform),
                issues: cleanedIssues(for: session)
            )
        }
        let groups = buildSeatGroups(rows: rows, grouping: grouping)

        var notes: [String] = []
        if document.truncated == true {
            let in_all = document.activeCount.map { " of \($0) live" } ?? ""
            notes.append("The server lists only the newest \(document.sessions.count)\(in_all) chats, so older live chats are not here.")
        }
        return OperatorSeatsTable(
            rows: rows,
            moreCount: max(0, matchingSessions.count - rows.count),
            note: notes.isEmpty ? nil : notes.joined(separator: gap),
            totalMatchingCount: matchingSessions.count,
            groups: groups
        )
    }

    private static func buildSeatGroups(rows: [OperatorSeatRow], grouping: OperatorSeatGrouping) -> [OperatorSeatGroup] {
        guard grouping != .flat, !rows.isEmpty else { return [] }
        var buckets: [String: [OperatorSeatRow]] = [:]
        for row in rows {
            let key = grouping == .byPlatform ? row.platform : row.seat
            buckets[key, default: []].append(row)
        }
        let groups = buckets.map { key, groupRows -> OperatorSeatGroup in
            let liveCount = groupRows.filter(\.isLive).count
            let goneCount = groupRows.filter { $0.state.text == "Gone" }.count
            let problemCount = groupRows.filter(\.hasProblem).count
            let detailTokens: [String] = groupRows.reduce(into: []) { acc, row in
                let token = grouping == .byPlatform ? row.seat : row.platform
                if !acc.contains(token) { acc.append(token) }
            }
            let summary: String?
            if detailTokens.isEmpty {
                summary = nil
            } else if detailTokens.count <= 5 {
                summary = detailTokens.joined(separator: ", ")
            } else {
                summary = detailTokens.prefix(5).joined(separator: ", ") + " +\(detailTokens.count - 5)"
            }
            return OperatorSeatGroup(
                id: "\(grouping.rawValue):\(key)",
                title: key,
                liveCount: liveCount,
                goneCount: goneCount,
                problemCount: problemCount,
                seatsSummary: summary,
                rows: groupRows
            )
        }
        return groups.sorted { lhs, rhs in
            if (lhs.liveCount > 0) != (rhs.liveCount > 0) {
                return lhs.liveCount > 0
            }
            if grouping == .byPlatform, (lhs.title == "Other") != (rhs.title == "Other") {
                return lhs.title != "Other"
            }
            if lhs.liveCount != rhs.liveCount {
                return lhs.liveCount > rhs.liveCount
            }
            return lhs.title.localizedCaseInsensitiveCompare(rhs.title) == .orderedAscending
        }
    }

    // MARK: Recent wakes

    static func wakeStateCell(_ state: String?) -> OperatorCell {
        guard let state else {
            return OperatorCell(text: OperatorPanelText.none, tone: .neutral, help: "The record did not say what became of this wake.")
        }
        // The words a wake that stopped before it ran can end in (`reduce_operator_wakes`).
        let stopped: Set<String> = ["skipped", "dropped", "held_back", "surfaced", "expired", "evicted"]
        let tone: OperatorTone = stopped.contains(state) ? .warning : .neutral
        return OperatorCell(text: OperatorPanelText.humanized(state), tone: tone, help: "The wake record's own word is \(state).")
    }

    private static func wakeReasonText(_ wake: OperatorWake) -> String {
        var parts: [String] = []
        if let reason = wake.reason { parts.append(OperatorPanelText.humanized(reason)) }
        if wake.owner == true { parts.append("Owner") }
        if wake.dm == true { parts.append("DM") }
        if wake.catchUp == true { parts.append("Catch-up") }
        return parts.isEmpty ? OperatorPanelText.none : parts.joined(separator: ", ")
    }

    static func wakesTable(
        _ document: OperatorWakesDocument,
        now: Date,
        limit: Int = wakeRowLimit,
        filter: OperatorWakeFilter = .all,
        search: String = ""
    ) -> OperatorWakesTable {
        struct Item { let seat: String; let wake: OperatorWake; let order: Int }
        var items: [Item] = []
        for seat in document.seats {
            for wake in seat.recent {
                items.append(Item(seat: seat.seat, wake: wake, order: items.count))
            }
        }
        let ordered = items.sorted { x, y in
            switch (x.wake.at, y.wake.at) {
            case let (l?, r?) where l != r: return l > r
            case (_?, nil): return true
            case (nil, _?): return false
            default: return x.order < y.order
            }
        }
        let needle = search.trimmingCharacters(in: .whitespacesAndNewlines)
        let matchingItems: [Item]
        if filter == .all && needle.isEmpty {
            matchingItems = ordered
        } else {
            matchingItems = ordered.filter { item in
                let stateCell = wakeStateCell(item.wake.state)
                let isOwnerOrDM = (item.wake.owner == true) || (item.wake.dm == true)
                switch filter {
                case .all: break
                case .problems: guard stateCell.tone == .warning else { return false }
                case .ownerOrDM: guard isOwnerOrDM else { return false }
                }
                guard !needle.isEmpty else { return true }
                let reason = wakeReasonText(item.wake)
                if [item.seat, stateCell.text, reason].contains(where: { $0.localizedCaseInsensitiveContains(needle) }) {
                    return true
                }
                let time = item.wake.at.map { FleetTime.when($0, now: now) } ?? OperatorPanelText.none
                return time.localizedCaseInsensitiveContains(needle)
            }
        }
        let visibleItems = limit < Int.max ? Array(matchingItems.prefix(max(0, limit))) : matchingItems
        let rows = visibleItems.map { item in
            let isOwnerOrDM = (item.wake.owner == true) || (item.wake.dm == true)
            return OperatorWakeLine(
                id: "\(item.order)|\(item.seat)|\(item.wake.wakeID ?? "-")",
                time: item.wake.at.map { FleetTime.when($0, now: now) } ?? OperatorPanelText.none,
                seat: item.seat,
                state: wakeStateCell(item.wake.state),
                reason: wakeReasonText(item.wake),
                isOwnerOrDM: isOwnerOrDM
            )
        }

        var seatOrder: [String] = []
        var seatBuckets: [String: [OperatorWakeLine]] = [:]
        for row in rows {
            if seatBuckets[row.seat] == nil { seatOrder.append(row.seat) }
            seatBuckets[row.seat, default: []].append(row)
        }
        let seatGroups = seatOrder.compactMap { seat -> OperatorWakeSeatGroup? in
            guard let groupRows = seatBuckets[seat] else { return nil }
            return OperatorWakeSeatGroup(
                id: seat,
                seat: seat,
                problemCount: groupRows.filter(\.isProblem).count,
                rows: groupRows
            )
        }

        // The server counts every wake and lists at most a few per seat, so the count is the
        // truth and the list is the newest of it.
        let counted = document.totals?.recent ?? ordered.count
        let loadedMore = max(0, matchingItems.count - rows.count)
        let unloaded = max(0, counted - ordered.count)
        let isUnfiltered = (filter == .all) && needle.isEmpty
        let more = isUnfiltered ? max(0, max(counted, ordered.count) - rows.count) : loadedMore

        var summaryParts: [String] = []
        if let held = document.totals?.heldBack, held > 0 {
            let lost = document.totals?.heldBackLost ?? 0
            summaryParts.append("\(held) held back" + (lost > 0 ? " (\(lost) lost)" : ""))
        }
        if let waiting = document.totals?.ownerQueue, waiting > 0 {
            summaryParts.append("\(waiting) waiting for the owner")
        }
        var notes: [String] = []
        if !document.unreadable.isEmpty {
            notes.append("The wake record of \(document.unreadable.joined(separator: ", ")) could not be read, so the counts here are a floor.")
        }
        if document.truncated == true {
            notes.append("The server lists only some seats, so the counts here are a floor.")
        }
        return OperatorWakesTable(
            rows: rows,
            moreCount: more,
            summary: summaryParts.isEmpty ? nil : summaryParts.joined(separator: ", ") + ".",
            notes: notes,
            totalLoadedCount: matching.count,
            loadedMoreCount: loadedMore,
            unloadedServerCount: unloaded,
            seatGroups: seatGroups
        )
    }

    // MARK: When the server cannot be reached

    /// The reason for the one banner at the top, or nil.  It shows only when every read failed on
    /// the route itself:  one read that failed alone says so in its own section.
    static func unreachableReason(
        status: ServerEavesdropperReading?,
        sessions: OperatorReading<OperatorSessionsDocument>?,
        wakes: OperatorReading<OperatorWakesDocument>?
    ) -> String? {
        guard let sessions, let wakes, let status,
              sessions.isUnreachable, wakes.isUnreachable,
              case .notRead(let why) = status else { return nil }
        // The status read's own words say which ssh failure it was and what to do about it.
        return why.isEmpty ? sessions.failure : why
    }

    static let unreachableTitle = "The server could not be reached, so nothing here is current.\(gap)Check that this Mac can ssh to the server (the fleet-ops-ctl route), then press Refresh."
}

// MARK: - Fixed words

enum OperatorPanelCopy {
    static let gap = OperatorPanelText.gap
    static let heading = "Operator"
    static let seatsHeading = "Seats And Chats"
    static let wakesHeading = "Recent Wakes"
    static let retiredListener = "The Mac's own listener is retired and no longer has a panel."
    static let controlsPointer = "Operator knobs are in Controls"
}
