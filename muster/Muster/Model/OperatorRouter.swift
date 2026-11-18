import Combine
import Foundation

// The Operator panel's wake router, held-back wakes, owner queue and Test Wake (AFC-825).
//
// Everything here comes from the server's control helper over the restricted route
// (`fleet-ops-ctl`), like the rest of the panel:
//
//   * `server router-health server --plan`   the one wake router's own `/health`, reduced to
//     booleans, counts and reason words per seat:  is the router running, and is each seat's
//     delivery on or off
//   * `server operator-wakes server --plan`  (already read by the panel) also carries each seat's
//     held-back wakes and the notes waiting for the owner, as rows of ids, words and times
//   * `server test-wake SEAT.LETTER.GEN.HOST` sends one dry-run wake addressed to one exact chat
//     of the letter registry.  It goes Show Plan, then Touch ID, through the shared action sheet,
//     and only from a button the owner presses:  nothing here sends one by itself.  The server
//     confirms the chat is live and is one chat before anything is sent, and a dry run delivers
//     nothing, so nothing here ever calls one delivered.
//
// Every field is read as optional, and a field that is missing, null or of another type reads as
// not reported, never as on, healthy or zero.

// MARK: - One exact chat

/// One chat of the letter registry, as a test wake addresses it:  `CLAUDE.BB.2.S` is CLAUDE's chat
/// with letter BB, generation 2, on the server.
struct OperatorWakeTarget: Equatable, Hashable {
    let seat: String
    let letter: String
    let generation: Int
    /// `M`, `S` or `C`.
    let hostCode: String

    init(seat: String, letter: String, generation: Int, hostCode: String) {
        self.seat = seat
        self.letter = letter
        self.generation = generation
        self.hostCode = hostCode
    }

    /// The target for a registry row, or nil when the row does not name one chat exactly.
    static func make(seat: String, letter: String?, generation: Int?, hostCode: String?) -> OperatorWakeTarget? {
        guard let letter, let generation, let hostCode else { return nil }
        let target = OperatorWakeTarget(seat: seat, letter: letter, generation: generation, hostCode: hostCode)
        return target.isValid ? target : nil
    }

    /// The target the helper's word names, or nil when it is not exactly one.
    init?(word: String) {
        let parts = word.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 4, let generation = Int(parts[2]) else { return nil }
        self.init(seat: parts[0], letter: parts[1], generation: generation, hostCode: parts[3])
        guard isValid, self.word == word else { return nil }
    }

    /// Each part in the helper's own shape (`TEST_WAKE_TARGET_RE`), so a target the helper would
    /// refuse is refused here, before any process exists.
    var isValid: Bool {
        OpsCommandBuilder.isSeatName(seat)
            && (1...2).contains(letter.unicodeScalars.count)
            && letter.unicodeScalars.allSatisfy { (65...90).contains($0.value) }
            && (1...999_999).contains(generation)
            && ["M", "S", "C"].contains(hostCode)
    }

    /// The one target word the helper takes:  `CLAUDE.BB.2.S`.
    var word: String { "\(seat).\(letter).\(generation).\(hostCode)" }

    var hostWord: String {
        switch hostCode {
        case "M": return "Mac"
        case "S": return "server"
        case "C": return "cloud"
        default: return hostCode
        }
    }

    /// `CLAUDE BB.2 (server)`.
    var name: String { "\(seat) \(letter).\(generation) (\(hostWord))" }

    /// The host code for the helper's host word (`mac`, `server`, `cloud`).
    static func hostCode(forWord word: String?) -> String? {
        switch word {
        case "mac"?: return "M"
        case "server"?: return "S"
        case "cloud"?: return "C"
        default: return nil
        }
    }
}

// MARK: - What a test wake came to

/// The `testWake` object of a `server test-wake` run:  the chat it addressed, whether the router
/// accepted the dry run, what the router would have picked, and the router's own delivery switch
/// for the seat.  A dry run delivers nothing, so there is no "delivered" here to show.
struct OperatorTestWake: Decodable, Equatable {
    let target: OperatorWakeTarget?
    let passed: Bool?
    /// The router took the dry run as its own (an HTTP answer it gave as a dry run).
    let accepted: Bool?
    let wakeID: String?
    let httpStatus: Int?
    let error: String?
    let routerHealthy: Bool?
    let routerDelivery: Bool?
    let routerReason: String?
    let pick: String?
    let pickedLetter: String?
    let pickedGeneration: Int?
    let pickedHost: String?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case target, passed, accepted, error, router, answer, httpStatus
        case wakeID = "wakeId"
    }

    private struct Target: Decodable {
        let seat: String?
        let letter: String?
        let generation: Int?
        let host: String?
        enum CodingKeys: String, CodingKey { case seat, letter, generation, host }
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            seat = container.optionalText(.seat, limit: 32)
            letter = container.optionalText(.letter, limit: 2)
            generation = container.optionalCount(.generation)
            host = container.optionalText(.host, limit: 12)
        }
    }

    private struct Router: Decodable {
        let healthy: Bool?
        let delivery: Bool?
        let reason: String?
        enum CodingKeys: String, CodingKey { case healthy, delivery, reason }
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            healthy = container.optionalField(Bool.self, .healthy)
            delivery = container.optionalField(Bool.self, .delivery)
            reason = container.optionalText(.reason, limit: 48)
        }
    }

    private struct Answer: Decodable {
        let pick: String?
        let pickedLetter: String?
        let pickedGeneration: Int?
        let host: String?
        let reason: String?
        enum CodingKeys: String, CodingKey { case pick, pickedLetter, pickedGeneration, host, reason }
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            pick = container.optionalText(.pick, limit: 48)
            pickedLetter = container.optionalText(.pickedLetter, limit: 2)
            pickedGeneration = container.optionalCount(.pickedGeneration)
            host = container.optionalText(.host, limit: 12)
            reason = container.optionalText(.reason, limit: 48)
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let target = container.optionalField(Target.self, .target)
        if let target, let seat = target.seat {
            self.target = OperatorWakeTarget.make(
                seat: seat, letter: target.letter, generation: target.generation,
                hostCode: OperatorWakeTarget.hostCode(forWord: target.host)
            )
        } else {
            self.target = nil
        }
        passed = container.optionalField(Bool.self, .passed)
        accepted = container.optionalField(Bool.self, .accepted)
        wakeID = container.optionalText(.wakeID, limit: 80)
        httpStatus = container.optionalCount(.httpStatus)
        error = container.optionalText(.error, limit: 48)
        let router = container.optionalField(Router.self, .router)
        routerHealthy = router?.healthy
        routerDelivery = router?.delivery
        routerReason = router?.reason
        let answer = container.optionalField(Answer.self, .answer)
        pick = answer?.pick
        pickedLetter = answer?.pickedLetter
        pickedGeneration = answer?.pickedGeneration
        pickedHost = answer?.host
        reason = answer?.reason
    }

    init(
        target: OperatorWakeTarget? = nil, passed: Bool? = nil, accepted: Bool? = nil, wakeID: String? = nil,
        httpStatus: Int? = nil, error: String? = nil, routerHealthy: Bool? = nil, routerDelivery: Bool? = nil,
        routerReason: String? = nil, pick: String? = nil, pickedLetter: String? = nil,
        pickedGeneration: Int? = nil, pickedHost: String? = nil, reason: String? = nil
    ) {
        self.target = target
        self.passed = passed
        self.accepted = accepted
        self.wakeID = wakeID
        self.httpStatus = httpStatus
        self.error = error
        self.routerHealthy = routerHealthy
        self.routerDelivery = routerDelivery
        self.routerReason = routerReason
        self.pick = pick
        self.pickedLetter = pickedLetter
        self.pickedGeneration = pickedGeneration
        self.pickedHost = pickedHost
        self.reason = reason
    }

    private static func word(_ value: String) -> String {
        OperatorPanelText.humanized(value).lowercased()
    }

    /// The result, one fact a line, for the action sheet.  Accepted and delivered are never the
    /// same line:  a dry run is accepted or not, and it always delivers nothing.
    var lines: [String] {
        var lines: [String] = []
        if let target { lines.append("Addressed: \(target.name).") }
        let status = httpStatus.map { " (HTTP \($0))" } ?? ""
        switch accepted {
        case true?: lines.append("Router: accepted the dry run\(status).")
        case false?: lines.append("Router: did not accept the dry run\(status).")
        case nil: lines.append("Router: its answer is not reported.")
        }
        var how: [String] = []
        if let pick { how.append("by \(Self.word(pick))") }
        if let reason { how.append("reason \(Self.word(reason))") }
        let howText = how.isEmpty ? "" : ", " + how.joined(separator: ", ")
        if let pickedLetter {
            var picked = pickedLetter
            if let pickedGeneration { picked += ".\(pickedGeneration)" }
            if let pickedHost { picked += " (\(pickedHost))" }
            lines.append("Picked: \(picked)\(howText).")
        } else if accepted == true {
            lines.append("Picked: no chat\(howText).")
        }
        lines.append("Delivered: nothing, because a test wake is a dry run.")
        let seat = target?.seat ?? "this seat"
        switch routerDelivery {
        case true?: lines.append("Router delivery for \(seat): on.")
        case false?: lines.append("Router delivery for \(seat): off, so a real wake would be held.")
        case nil: lines.append("Router delivery for \(seat): not reported.")
        }
        if passed != true, let error {
            lines.append("Why it did not pass: \(Self.word(error)).")
        }
        if let wakeID { lines.append("Wake id: \(wakeID)") }
        return lines
    }
}

// MARK: - The router's health

/// The `routerHealth` object of `server router-health server --plan`:
///
///     {"schema": 1, "generated_at", "instance": "server", "healthy", "counts", "truncated",
///      "dropped", "seats": [{"seat", "installed", "healthy", "delivery", "registry_configured",
///                            "reason"}]}
struct OperatorRouterHealthDocument: Decodable, Equatable {
    static let schemaVersion = 1
    static let verb = "server router-health"

    let schema: Int?
    let instance: String?
    let healthy: Bool?
    let truncated: Bool?
    let seats: [OperatorRouterSeat]

    enum CodingKeys: String, CodingKey { case schema, instance, healthy, truncated, seats }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = container.optionalField(Int.self, .schema)
        instance = container.optionalText(.instance, limit: 24)
        healthy = container.optionalField(Bool.self, .healthy)
        truncated = container.optionalField(Bool.self, .truncated)
        seats = container.lenientList(OperatorRouterSeat.self, .seats)
    }

    init(schema: Int? = 1, instance: String? = "server", healthy: Bool? = nil, truncated: Bool? = nil,
         seats: [OperatorRouterSeat] = []) {
        self.schema = schema
        self.instance = instance
        self.healthy = healthy
        self.truncated = truncated
        self.seats = seats
    }
}

/// One seat the router serves, as its `/health` says.
struct OperatorRouterSeat: Decodable, Equatable {
    let seat: String
    let installed: Bool?
    let healthy: Bool?
    let delivery: Bool?
    let registryConfigured: Bool?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case seat, installed, healthy, delivery, reason
        case registryConfigured = "registry_configured"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let name = container.optionalText(.seat, limit: 32) else {
            throw DecodingError.dataCorruptedError(
                forKey: .seat, in: container, debugDescription: "A seat with no name is left out.")
        }
        seat = name
        installed = container.optionalField(Bool.self, .installed)
        healthy = container.optionalField(Bool.self, .healthy)
        delivery = container.optionalField(Bool.self, .delivery)
        registryConfigured = container.optionalField(Bool.self, .registryConfigured)
        reason = container.optionalText(.reason, limit: 48)
    }

    init(seat: String, installed: Bool? = nil, healthy: Bool? = nil, delivery: Bool? = nil,
         registryConfigured: Bool? = nil, reason: String? = nil) {
        self.seat = seat
        self.installed = installed
        self.healthy = healthy
        self.delivery = delivery
        self.registryConfigured = registryConfigured
        self.reason = reason
    }
}

private struct OperatorRouterHealthEnvelope: Decodable {
    let head: OperatorEnvelopeHead
    let routerHealth: OperatorRouterHealthDocument?

    enum CodingKeys: String, CodingKey { case routerHealth }

    init(from decoder: Decoder) throws {
        head = try OperatorEnvelopeHead(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        routerHealth = container.optionalField(OperatorRouterHealthDocument.self, .routerHealth)
    }
}

enum OperatorRouterHealthDecoder {
    /// What the panel says when nothing answers on the router's port:  the router is not running.
    static let notRunning = "Nothing answers on the wake router's port on the server, so it is not running."

    static func reading(_ result: OpsProcessResult) -> OperatorReading<OperatorRouterHealthDocument> {
        let decode: (Data) -> (head: OperatorEnvelopeHead, document: OperatorRouterHealthDocument?)? = { data in
            guard let envelope = try? JSONDecoder().decode(OperatorRouterHealthEnvelope.self, from: data) else { return nil }
            return (envelope.head, envelope.routerHealth)
        }
        // The helper's word for a router that does not answer at all is its own state, not a failed read.
        if !result.timedOut, let decoded = decode(Data(result.stdout.utf8)),
           decoded.head.ok != true, decoded.head.errorCode == "router_not_running" {
            return .unavailable(notRunning)
        }
        return OperatorAnswerReader.reading(
            result, verb: OperatorRouterHealthDocument.verb, what: "wake router's health",
            decode: decode,
            check: {
                OperatorAnswerReader.mismatch(schema: $0.schema, expected: OperatorRouterHealthDocument.schemaVersion,
                                              instance: $0.instance, what: "wake router's health")
            }
        )
    }
}

/// Reads the router's health once over the restricted, read-only route.
struct OperatorRouterHealthReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read() async -> OperatorReading<OperatorRouterHealthDocument> {
        let invocation = makeBuilder().routerHealthInvocation()
        do {
            return OperatorRouterHealthDecoder.reading(try await runner.run(invocation))
        } catch {
            return .unreachable("The Mac could not start the ssh route to the server.")
        }
    }
}

/// The router's health, read alongside the panel's other reads.  At most one read runs at a time,
/// and a second request while one runs waits for it.
@MainActor
final class OperatorRouterHealthStore: ObservableObject {
    static let shared = OperatorRouterHealthStore()

    typealias Read = @Sendable () async -> OperatorReading<OperatorRouterHealthDocument>

    @Published private(set) var reading: OperatorReading<OperatorRouterHealthDocument>?

    private var inFlight: Task<Void, Never>?
    private let read: Read

    init(read: @escaping Read = { await OperatorRouterHealthReader().read() }) {
        self.read = read
    }

    /// True when the router says this seat's routine is installed:  the seats a test wake can reach.
    func isRouted(_ seat: String) -> Bool {
        reading?.value?.seats.contains { $0.seat == seat && $0.installed == true } == true
    }

    func refresh() async {
        if let running = inFlight {
            await running.value
            return
        }
        let read = self.read
        let task = Task { @MainActor [weak self] in
            let value = await read()
            guard let self else { return }
            self.reading = value
            self.inFlight = nil
        }
        inFlight = task
        await task.value
    }
}

// MARK: - Held back, and waiting for the owner

/// A seat's held-back wakes, as `server operator-wakes` sends them:  how many, how many were lost,
/// how many for each reason, and up to ten rows.
struct OperatorHeldBack: Decodable, Equatable {
    let count: Int?
    let lost: Int?
    let reasons: [String: Int]
    let rows: [OperatorHeldRow]

    enum CodingKeys: String, CodingKey { case count, lost, reasons, rows }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        count = container.optionalCount(.count)
        lost = container.optionalCount(.lost)
        let raw = container.optionalField([String: Int].self, .reasons) ?? [:]
        var kept: [String: Int] = [:]
        for (key, value) in raw where value >= 0 {
            if let word = EavesdropperStatusText.plain(key, limit: 40) { kept[word] = value }
        }
        reasons = kept
        rows = container.lenientList(OperatorHeldRow.self, .rows)
    }

    init(count: Int? = nil, lost: Int? = nil, reasons: [String: Int] = [:], rows: [OperatorHeldRow] = []) {
        self.count = count
        self.lost = lost
        self.reasons = reasons
        self.rows = rows
    }
}

/// One held-back wake:  the Zulip message id, when, and the reason word.  No body, topic or sender.
struct OperatorHeldRow: Decodable, Equatable {
    let messageID: Int?
    let atText: String?
    let reason: String?
    let dm: Bool?

    enum CodingKeys: String, CodingKey {
        case reason, dm
        case messageID = "message_id"
        case atText = "at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        messageID = container.optionalCount(.messageID)
        atText = container.optionalText(.atText, limit: 40)
        reason = container.optionalText(.reason, limit: 40)
        dm = container.optionalField(Bool.self, .dm)
    }

    init(messageID: Int? = nil, at: String? = nil, reason: String? = nil, dm: Bool? = nil) {
        self.messageID = messageID
        self.atText = at
        self.reason = reason
        self.dm = dm
    }

    var at: Date? { atText.flatMap(FleetTime.parse) }
}

/// A seat's notes waiting for the owner:  how many, and up to ten rows.
struct OperatorOwnerQueue: Decodable, Equatable {
    let pending: Int?
    let rows: [OperatorOwnerRow]

    enum CodingKeys: String, CodingKey { case pending, rows }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pending = container.optionalCount(.pending)
        rows = container.lenientList(OperatorOwnerRow.self, .rows)
    }

    init(pending: Int? = nil, rows: [OperatorOwnerRow] = []) {
        self.pending = pending
        self.rows = rows
    }
}

/// One note waiting for the owner:  its number, when, its kind and its risk word.
struct OperatorOwnerRow: Decodable, Equatable {
    let seq: Int?
    let atText: String?
    let kind: String?
    let risk: String?

    enum CodingKeys: String, CodingKey {
        case seq, kind, risk
        case atText = "at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        seq = container.optionalCount(.seq)
        atText = container.optionalText(.atText, limit: 40)
        kind = container.optionalText(.kind, limit: 40)
        risk = container.optionalText(.risk, limit: 40)
    }

    init(seq: Int? = nil, at: String? = nil, kind: String? = nil, risk: String? = nil) {
        self.seq = seq
        self.atText = at
        self.kind = kind
        self.risk = risk
    }

    var at: Date? { atText.flatMap(FleetTime.parse) }
}

// MARK: - What the panel says

/// One seat the router serves, as the Wake Router section lists it.
struct OperatorRouterSeatLine: Equatable, Identifiable {
    var id: String { seat }
    let seat: String
    let installed: OperatorCell
    let delivery: OperatorCell
    let health: OperatorCell
    let reason: String
}

/// One seat's held-back wakes and owner queue, as the Held Back section lists it.
struct OperatorQueueLine: Equatable, Identifiable {
    var id: String { seat }
    let seat: String
    let heldBack: String
    let reasons: String
    let oldestHeld: String
    let ownerQueue: String
    let ownerRows: String
    let tone: OperatorTone
}

extension OperatorPanelPresentation {
    /// The line the router card shows when it has nothing of the router's own to say.
    static let routerStateNotReported = "The router's own state is not in what the server reports."

    /// The Wake Router card:  the router's own health when it was read, and the wake counts of
    /// `router(wakes:status:now:)` under it.  With no health reading it is that card unchanged.
    static func routerCard(
        health: OperatorReading<OperatorRouterHealthDocument>?,
        wakes: OperatorReading<OperatorWakesDocument>?,
        status: ServerEavesdropperReading?,
        now: Date
    ) -> OperatorStatusCard {
        let base = router(wakes: wakes, status: status, now: now)
        guard let health else { return base }
        let kept = base.lines.filter { $0 != routerStateNotReported }
        switch health {
        case .read(let document):
            let installed = document.seats.filter { $0.installed == true }
            let deliveryOn = installed.filter { $0.delivery == true }
            let deliveryOff = installed.filter { $0.delivery == false }
            let unhealthy = installed.filter { $0.healthy == false }
            var lines = ["It answered its health check."]
            lines.append("\(OperatorPanelText.counted(installed.count, "seat", "seats")) on the router, \(deliveryOn.count) with delivery on.")
            if !deliveryOff.isEmpty {
                lines.append("Delivery off: \(deliveryOff.map(\.seat).joined(separator: ", ")).")
            }
            if !unhealthy.isEmpty {
                lines.append("Not healthy: \(unhealthy.map(\.seat).joined(separator: ", ")).")
            }
            let tone: OperatorTone
            if document.healthy == false || !unhealthy.isEmpty || !deliveryOff.isEmpty {
                tone = .warning
            } else if document.healthy == true {
                tone = .good
            } else {
                // The colour must not claim a health the router did not report.
                tone = .neutral
            }
            return OperatorStatusCard(title: base.title, state: "Running", tone: tone, lines: lines + kept)
        case .unavailable(let why) where why == OperatorRouterHealthDecoder.notRunning:
            return OperatorStatusCard(title: base.title, state: "Not running", tone: .bad, lines: [why] + kept)
        case .unavailable(let why), .unreachable(let why):
            return OperatorStatusCard(title: base.title, state: base.state, tone: base.tone,
                                      lines: base.lines + ["Its health was not read: \(why)"])
        }
    }

    /// One line per seat the router serves, in the router's order.
    static func routerSeatLines(_ document: OperatorRouterHealthDocument) -> [OperatorRouterSeatLine] {
        document.seats.map { seat in
            let installed: OperatorCell
            switch seat.installed {
            case true?: installed = OperatorCell(text: "On the router", tone: .neutral, help: "The router serves this seat.")
            case false?: installed = OperatorCell(text: "Not installed", tone: .warning, help: "The router has no routine for this seat.")
            case nil: installed = OperatorCell(text: "Not reported", tone: .neutral, help: "The router did not say.")
            }
            let delivery: OperatorCell
            switch seat.delivery {
            case true?: delivery = OperatorCell(text: "On", tone: .good, help: "A real wake for this seat is delivered.")
            case false?: delivery = OperatorCell(text: "Off", tone: .warning, help: "A real wake for this seat is held, not delivered.")
            case nil: delivery = OperatorCell(text: "Not reported", tone: .neutral, help: "The router did not say.")
            }
            let health: OperatorCell
            switch seat.healthy {
            case true?: health = OperatorCell(text: "Healthy", tone: .good, help: "The router says this seat is healthy.")
            case false?: health = OperatorCell(text: "Not healthy", tone: .warning, help: "The router says this seat is not healthy.")
            case nil: health = OperatorCell(text: "Not reported", tone: .neutral, help: "The router did not say.")
            }
            return OperatorRouterSeatLine(
                seat: seat.seat, installed: installed, delivery: delivery, health: health,
                reason: seat.reason.map(OperatorPanelText.humanized) ?? OperatorPanelText.none
            )
        }
    }

    /// One line per seat that has wakes held back or notes waiting for the owner, the most held
    /// first.  Read only:  nothing here releases, replays or dismisses anything.
    static func queueLines(_ document: OperatorWakesDocument, now: Date) -> [OperatorQueueLine] {
        let lines = document.seats.compactMap { seat -> (held: Int, owner: Int, line: OperatorQueueLine)? in
            let held = seat.heldBack?.count ?? 0
            let owner = seat.ownerQueue?.pending ?? 0
            guard held > 0 || owner > 0 else { return nil }
            var heldText = OperatorPanelText.none
            if let count = seat.heldBack?.count {
                heldText = "\(count)"
                if let lost = seat.heldBack?.lost, lost > 0 { heldText += " (\(lost) lost)" }
            }
            let reasons = (seat.heldBack?.reasons ?? [:])
                .sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }
                .map { "\(OperatorPanelText.humanized($0.key)) \($0.value)" }
                .joined(separator: " · ")
            let oldest = seat.heldBack?.rows.compactMap(\.at).min()
            let ownerRows = (seat.ownerQueue?.rows ?? []).prefix(3).map { row -> String in
                let words = [row.kind, row.risk.map { "\($0) risk" }].compactMap { $0 }
                let what = words.isEmpty ? "note" : words.map(OperatorPanelText.humanized).joined(separator: ", ")
                return "\(what) \(OperatorPanelText.relative(row.at, now: now))"
            }.joined(separator: " · ")
            let line = OperatorQueueLine(
                seat: seat.seat,
                heldBack: heldText,
                reasons: reasons.isEmpty ? OperatorPanelText.none : reasons,
                oldestHeld: oldest.map { OperatorPanelText.relative($0, now: now) } ?? OperatorPanelText.none,
                ownerQueue: seat.ownerQueue?.pending.map { "\($0)" } ?? OperatorPanelText.none,
                ownerRows: ownerRows.isEmpty ? OperatorPanelText.none : ownerRows,
                tone: held > 0 ? .warning : .neutral
            )
            return (held: held, owner: owner, line: line)
        }
        return lines.sorted { a, b in
            if a.held != b.held { return a.held > b.held }
            if a.owner != b.owner { return a.owner > b.owner }
            return a.line.seat < b.line.seat
        }.map(\.line)
    }
}
