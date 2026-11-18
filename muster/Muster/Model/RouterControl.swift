import Combine
import Foundation

// The wake router, as the Operator panel shows it and tests it.
//
// The wake router is one service on the server that every seat's wakes go through.  Muster asks
// the server's control helper two read-only questions about it over the existing restricted
// route (`fleet-ops-ctl`):
//
//   * `server router-health server`   what the router says about itself (its `/health`), and the
//                                     state of the systemd unit that runs it
//   * `server router-test-wake SEAT`  a dry run of one wake for one seat:  the router picks the
//                                     chat it would wake and delivers nothing
//
// Both are `--plan` only, so neither needs a sheet or Touch ID, and neither can start, stop or
// change anything.  A dry run does leave one test receipt in the router's own record, which is why
// it is a button and not part of the periodic read.  Every field is read as optional and a field
// that is missing, null or of another type reads as not reported, never as healthy, on or ready.

// MARK: - What the router says about itself

/// One seat's row of the router's health.
struct RouterSeatHealth: Decodable, Equatable {
    let seat: String
    /// Whether the seat's module is installed on the router.
    let installed: Bool?
    let healthy: Bool?
    /// Whether delivery is on for the seat.  Off means a wake is picked and held, not delivered.
    let delivery: Bool?
    let registryConfigured: Bool?
    /// The server's reason word for a seat that is not healthy.
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
        reason = container.optionalText(.reason, limit: 40)
    }

    init(
        seat: String, installed: Bool? = nil, healthy: Bool? = nil, delivery: Bool? = nil,
        registryConfigured: Bool? = nil, reason: String? = nil
    ) {
        self.seat = seat
        self.installed = installed
        self.healthy = healthy
        self.delivery = delivery
        self.registryConfigured = registryConfigured
        self.reason = reason
    }
}

/// The `routerHealth` document:
///
///     {"schema": 1, "generated_at", "instance": "server", "healthy", "truncated",
///      "counts": {"total", "installed", "not_installed", "healthy", "delivery_on", "registry_configured"},
///      "seats": [{"seat", "installed", "healthy", "delivery", "registry_configured", "reason"}]}
struct RouterHealthDocument: Decodable, Equatable {
    static let schemaVersion = 1

    let schema: Int?
    let generatedAtText: String?
    let instance: String?
    /// The router's own verdict:  every installed seat is healthy.
    let healthy: Bool?
    let truncated: Bool?
    let seats: [RouterSeatHealth]

    enum CodingKeys: String, CodingKey {
        case schema, instance, healthy, truncated, seats
        case generatedAtText = "generated_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = container.optionalField(Int.self, .schema)
        generatedAtText = container.optionalText(.generatedAtText, limit: 40)
        instance = container.optionalText(.instance, limit: 24)
        healthy = container.optionalField(Bool.self, .healthy)
        truncated = container.optionalField(Bool.self, .truncated)
        seats = container.lenientList(RouterSeatHealth.self, .seats)
    }

    init(
        schema: Int? = 1, instance: String? = "server", healthy: Bool? = nil,
        truncated: Bool? = nil, seats: [RouterSeatHealth] = []
    ) {
        self.schema = schema
        self.generatedAtText = nil
        self.instance = instance
        self.healthy = healthy
        self.truncated = truncated
        self.seats = seats
    }

    /// Seats whose module is installed on the router.  A seat that does not say is not counted.
    var installedSeats: [RouterSeatHealth] { seats.filter { $0.installed == true } }
}

/// The state of the systemd user unit that runs the router, from the same fixed `systemctl show`
/// the Services panel's unit reads use.
struct RouterUnitState: Decodable, Equatable {
    let unit: String?
    /// systemd's own state word:  `active`, `inactive`, `failed`, `activating`, `deactivating`.
    let activeState: String?
    let subState: String?
    let unitResult: String?

    enum CodingKeys: String, CodingKey { case unit, activeState, subState, unitResult }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        unit = container.optionalText(.unit, limit: 64)
        activeState = container.optionalText(.activeState, limit: 32)
        subState = container.optionalText(.subState, limit: 32)
        unitResult = container.optionalText(.unitResult, limit: 32)
    }

    init(unit: String? = "afc-wake-router.service", activeState: String?, subState: String? = nil, unitResult: String? = nil) {
        self.unit = unit
        self.activeState = activeState
        self.subState = subState
        self.unitResult = unitResult
    }

    /// The unit in the words a person says:  `running`, `stopped`, `failed`, `starting`.
    var words: String? {
        switch activeState {
        case "active"?: return "running"
        case "inactive"?: return "stopped"
        case "failed"?: return "failed"
        case "activating"?: return "starting"
        case "deactivating"?: return "stopping"
        case "reloading"?: return "reloading"
        case let other?: return "in the state \(other)"
        case nil: return nil
        }
    }

    /// Whether systemd says the unit is down:  stopped, or failed.
    var isDown: Bool { activeState == "inactive" || activeState == "failed" }
    var isUp: Bool { activeState == "active" }
}

/// What one read of `server router-health` came to.  The unit rides with every case the server
/// answered, because it is the one fact that can still be said when the router does not answer.
enum RouterHealthReading: Equatable {
    /// The router answered its health.
    case answering(RouterHealthDocument, unit: RouterUnitState?)
    /// The server answered and the router did not:  nothing listens on its port, or it answered
    /// something this version cannot use.  `cause` is a plain sentence.
    case notAnswering(cause: String, unit: RouterUnitState?)
    /// The route to the server failed.
    case unreachable(String)
    /// The server's helper refused, or answered in a form this version of Muster does not read.
    case unavailable(String)

    var unit: RouterUnitState? {
        switch self {
        case .answering(_, let unit), .notAnswering(_, let unit): return unit
        case .unreachable, .unavailable: return nil
        }
    }

    var document: RouterHealthDocument? {
        if case .answering(let document, _) = self { return document }
        return nil
    }
}

private struct RouterHealthEnvelope: Decodable {
    let head: OperatorEnvelopeHead
    let health: RouterHealthDocument?
    let unit: RouterUnitState?

    enum CodingKeys: String, CodingKey {
        case health = "routerHealth"
        case unit = "routerUnit"
    }

    init(from decoder: Decoder) throws {
        head = try OperatorEnvelopeHead(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        health = container.optionalField(RouterHealthDocument.self, .health)
        unit = container.optionalField(RouterUnitState.self, .unit)
    }
}

enum RouterHealthDecoder {
    static let verb = "server router-health"

    /// What the helper's error words for this read mean, in plain words.
    static func cause(errorCode: String?, detail: String?) -> String {
        switch errorCode {
        case "router_not_running"?:
            return "Nothing is listening on the wake router's port on the server."
        case "timeout"?:
            return "The wake router did not answer in time."
        case "bad_status"?:
            return "The wake router answered with something this read does not expect."
        case "too_large"?:
            return "The wake router's answer is larger than the server will read."
        case "not_json"?:
            return "The wake router's answer is not in a form the server can read."
        case "read_failed"?:
            return "The server could not read the wake router's answer."
        default:
            if let detail, !detail.isEmpty { return Self.sentence(detail) }
            return "The server could not read the wake router, and did not say why."
        }
    }

    /// The helper's own detail as a sentence:  it starts with a capital and ends with a full stop.
    static func sentence(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard let first = trimmed.first else { return trimmed }
        let capital = first.uppercased() + trimmed.dropFirst()
        return capital.hasSuffix(".") ? capital : capital + "."
    }

    static func reading(_ result: OpsProcessResult) -> RouterHealthReading {
        if result.timedOut { return .unreachable("The server did not answer in time.") }
        guard let envelope = try? JSONDecoder().decode(RouterHealthEnvelope.self, from: Data(result.stdout.utf8)) else {
            if result.exitCode == 255 {
                return .unreachable("The Mac could not reach the server over ssh (the fleet-ops-ctl route).")
            }
            if result.exitCode != 0 {
                return .unavailable("The server's helper failed without an answer (exit \(result.exitCode)).")
            }
            return .unavailable("The server's answer could not be read.")
        }
        let head = envelope.head
        if let named = head.verb, named != verb {
            return .unavailable("The answer is for another read (\(named)), so it is not shown.")
        }
        guard head.ok == true else {
            // `unavailable` is the helper saying the request was fine and the router could not be
            // read.  Anything else (a refusal, an older helper without this verb) is not that.
            if head.result == "unavailable" {
                return .notAnswering(cause: cause(errorCode: head.errorCode, detail: head.detail), unit: envelope.unit)
            }
            return .unavailable(OperatorUnavailable.cause(errorCode: head.errorCode, detail: head.detail, what: "wake router state"))
        }
        guard let document = envelope.health else {
            return .unavailable("The server answered without the wake router's health.")
        }
        if let why = OperatorAnswerReader.mismatch(
            schema: document.schema, expected: RouterHealthDocument.schemaVersion,
            instance: document.instance, what: "wake router state") {
            return .unavailable(why)
        }
        return .answering(document, unit: envelope.unit)
    }
}

/// Reads the router's health once over the restricted, read-only route.
struct RouterHealthReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read() async -> RouterHealthReading {
        let invocation = makeBuilder().routerHealthInvocation()
        do {
            return RouterHealthDecoder.reading(try await runner.run(invocation))
        } catch {
            return .unreachable("The Mac could not start the ssh route to the server.")
        }
    }
}

// MARK: - The dry run

/// The answer of `server router-test-wake SEAT`, kept to what the panel shows.
///
///     {"schema": 1, "seat", "passed", "dry_run", "picked", "addressed", "http_status", "error",
///      "routine": {"url_env", "key_env", "auth", "method", "host"},
///      "router_health": {"healthy", "delivery", "reason"},
///      "answer": {"test", "status", "wake_id", "receipt_id", "seat", "letter", "letter_live",
///                 "picked", "picked_letter", "picked_generation", "host", "reason", "delivered"}}
///
/// The server sends no session id, link or note, and nothing here asks for one.
struct RouterTestWakeReport: Decodable, Equatable {
    static let schemaVersion = 1

    struct Routine: Decodable, Equatable {
        let host: String?
        let auth: String?

        enum CodingKeys: String, CodingKey { case host, auth }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            host = container.optionalText(.host, limit: 80)
            auth = container.optionalText(.auth, limit: 24)
        }

        init(host: String? = nil, auth: String? = nil) {
            self.host = host
            self.auth = auth
        }
    }

    struct Health: Decodable, Equatable {
        let healthy: Bool?
        /// False means a real wake for the seat would be held.
        let delivery: Bool?
        let reason: String?

        enum CodingKeys: String, CodingKey { case healthy, delivery, reason }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            healthy = container.optionalField(Bool.self, .healthy)
            delivery = container.optionalField(Bool.self, .delivery)
            reason = container.optionalText(.reason, limit: 40)
        }

        init(healthy: Bool? = nil, delivery: Bool? = nil, reason: String? = nil) {
            self.healthy = healthy
            self.delivery = delivery
            self.reason = reason
        }
    }

    struct Answer: Decodable, Equatable {
        let test: Bool?
        let letter: String?
        let letterLive: Bool?
        let picked: Bool?
        let pickedLetter: String?
        let pickedGeneration: Int?
        let host: String?
        let reason: String?
        let delivered: Bool?

        enum CodingKeys: String, CodingKey {
            case test, letter, picked, host, reason, delivered
            case letterLive = "letter_live"
            case pickedLetter = "picked_letter"
            case pickedGeneration = "picked_generation"
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            test = container.optionalField(Bool.self, .test)
            letter = container.optionalText(.letter, limit: 2)
            letterLive = container.optionalField(Bool.self, .letterLive)
            // The router names who it picked with a word, and the helper sends whether it picked.
            picked = container.optionalField(Bool.self, .picked)
                ?? container.optionalField(String.self, .picked).map { !$0.isEmpty }
            pickedLetter = container.optionalText(.pickedLetter, limit: 2)
            pickedGeneration = container.optionalCount(.pickedGeneration)
            host = container.optionalText(.host, limit: 12)
            reason = container.optionalText(.reason, limit: 48)
            delivered = container.optionalField(Bool.self, .delivered)
        }

        init(
            test: Bool? = true, letter: String? = nil, letterLive: Bool? = nil, picked: Bool? = nil,
            pickedLetter: String? = nil, pickedGeneration: Int? = nil, host: String? = nil,
            reason: String? = nil, delivered: Bool? = false
        ) {
            self.test = test
            self.letter = letter
            self.letterLive = letterLive
            self.picked = picked
            self.pickedLetter = pickedLetter
            self.pickedGeneration = pickedGeneration
            self.host = host
            self.reason = reason
            self.delivered = delivered
        }
    }

    let schema: Int?
    let seat: String?
    let passed: Bool?
    let dryRun: Bool?
    let picked: Bool?
    let addressed: Bool?
    let httpStatus: Int?
    /// One of the dry run's own words:  `not_the_router`, `router_unreachable`,
    /// `routine_not_configured`, or `other`.
    let error: String?
    let routine: Routine?
    let routerHealth: Health?
    let answer: Answer?

    enum CodingKeys: String, CodingKey {
        case schema, seat, passed, picked, addressed, error, routine, answer
        case dryRun = "dry_run"
        case httpStatus = "http_status"
        case routerHealth = "router_health"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = container.optionalField(Int.self, .schema)
        seat = container.optionalText(.seat, limit: 32)
        passed = container.optionalField(Bool.self, .passed)
        dryRun = container.optionalField(Bool.self, .dryRun)
        picked = container.optionalField(Bool.self, .picked)
        addressed = container.optionalField(Bool.self, .addressed)
        httpStatus = container.optionalCount(.httpStatus)
        error = container.optionalText(.error, limit: 40)
        routine = container.optionalField(Routine.self, .routine)
        routerHealth = container.optionalField(Health.self, .routerHealth)
        answer = container.optionalField(Answer.self, .answer)
    }

    init(
        seat: String?, passed: Bool?, dryRun: Bool? = nil, picked: Bool? = nil, addressed: Bool? = nil,
        httpStatus: Int? = nil, error: String? = nil, routine: Routine? = nil,
        routerHealth: Health? = nil, answer: Answer? = nil
    ) {
        self.schema = 1
        self.seat = seat
        self.passed = passed
        self.dryRun = dryRun
        self.picked = picked
        self.addressed = addressed
        self.httpStatus = httpStatus
        self.error = error
        self.routine = routine
        self.routerHealth = routerHealth
        self.answer = answer
    }
}

enum RouterTestWakeReading: Equatable {
    case report(RouterTestWakeReport)
    case unreachable(String)
    case unavailable(String)
}

private struct RouterTestWakeEnvelope: Decodable {
    let head: OperatorEnvelopeHead
    let report: RouterTestWakeReport?

    enum CodingKeys: String, CodingKey { case report = "routerTestWake" }

    init(from decoder: Decoder) throws {
        head = try OperatorEnvelopeHead(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        report = container.optionalField(RouterTestWakeReport.self, .report)
    }
}

enum RouterTestWakeDecoder {
    static let verb = "server router-test-wake"

    static func cause(errorCode: String?, detail: String?) -> String {
        switch errorCode {
        case "docker_unavailable"?:
            return "The server could not run docker, so the dry run did not run."
        case "timeout"?:
            return "The dry run took too long, so the server gave up."
        case "container_not_running"?:
            return "The Eavesdropper is not running on the server, so the dry run did not run."
        case "seat_not_testable"?:
            return "The Eavesdropper holds no wake route to test for this seat, so there is nothing to test."
        case "too_large"?, "not_json"?:
            return "The dry run's answer is not in a form the server can read."
        case "read_failed"?:
            return "The server could not run the dry run."
        default:
            let said = (detail ?? "").lowercased()
            if said.contains("unknown server verb") || said.contains("not in the allowlist") {
                return "The server's control helper does not have the dry run yet."
            }
            if let detail, !detail.isEmpty { return RouterHealthDecoder.sentence(detail) }
            return "The server could not run the dry run, and did not say why."
        }
    }

    static func reading(_ result: OpsProcessResult, seat: String) -> RouterTestWakeReading {
        if result.timedOut { return .unreachable("The server did not answer in time.") }
        guard let envelope = try? JSONDecoder().decode(RouterTestWakeEnvelope.self, from: Data(result.stdout.utf8)) else {
            if result.exitCode == 255 {
                return .unreachable("The Mac could not reach the server over ssh (the fleet-ops-ctl route).")
            }
            if result.exitCode != 0 {
                return .unavailable("The server's helper failed without an answer (exit \(result.exitCode)).")
            }
            return .unavailable("The server's answer could not be read.")
        }
        let head = envelope.head
        if let named = head.verb, named != verb {
            return .unavailable("The answer is for another read (\(named)), so it is not shown.")
        }
        guard head.ok == true else {
            return .unavailable(cause(errorCode: head.errorCode, detail: head.detail))
        }
        guard let report = envelope.report else {
            return .unavailable("The server answered without the dry run's result.")
        }
        guard report.schema == RouterTestWakeReport.schemaVersion else {
            return .unavailable("The dry run's answer is in a form this version of Muster does not read.\(OperatorPanelText.gap)Update Muster to read it.")
        }
        guard report.seat == seat else {
            return .unavailable("The answer is for another seat, so it is not shown.")
        }
        return .report(report)
    }
}

/// Runs the dry run for one seat once over the restricted, read-only route.
struct RouterTestWakeReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func run(seat: String) async -> RouterTestWakeReading {
        let invocation: OpsInvocation
        do {
            invocation = try makeBuilder().routerTestWakeInvocation(seat: seat)
        } catch {
            return .unavailable("This seat's name cannot be tested.")
        }
        do {
            return RouterTestWakeDecoder.reading(try await runner.run(invocation), seat: seat)
        } catch {
            return .unreachable("The Mac could not start the ssh route to the server.")
        }
    }
}

/// The dry runs of this session, one per seat, each kept until it is run again.  A seat runs one
/// at a time:  a second request while one runs starts nothing.
@MainActor
final class RouterTestWakeStore: ObservableObject {
    static let shared = RouterTestWakeStore()

    enum State: Equatable {
        case running
        case finished(RouterTestWakeReading, at: Date)
    }

    typealias Run = @Sendable (String) async -> RouterTestWakeReading

    @Published private(set) var states: [String: State] = [:]
    /// The dry runs this store started.
    private(set) var runsStarted = 0

    private let run: Run
    private let now: () -> Date

    init(
        run: @escaping Run = { await RouterTestWakeReader().run(seat: $0) },
        now: @escaping () -> Date = { Date() }
    ) {
        self.run = run
        self.now = now
    }

    func isRunning(_ seat: String) -> Bool { states[seat] == .running }

    func test(seat: String) async {
        guard !isRunning(seat) else { return }
        states[seat] = .running
        runsStarted += 1
        let reading = await run(seat)
        states[seat] = .finished(reading, at: now())
    }
}

// MARK: - What the panel says about the router

/// One seat of the router, as a row of the Wake Router section.
struct RouterSeatRow: Equatable, Identifiable {
    var id: String { seat }
    let seat: String
    let state: OperatorCell
    let delivery: OperatorCell
    /// The newest wake of the seat in the wake record that has an outcome, in words, or why there is none.
    let lastWake: String
    /// Whether a dry run can be asked for:  the seat's module is installed on the router.
    let canTest: Bool
}

/// A dry run's result in plain words.
struct RouterTestWakeSummary: Equatable {
    let headline: String
    let tone: OperatorTone
    let lines: [String]
}

enum RouterPresentation {
    static let gap = OperatorPanelText.gap
    static let heading = "Wake Router"
    /// Said under a dry run that passed:  the router did pick a chat.
    static let dryRunNote = "This was a dry run, so the router picked a chat and delivered nothing."
    /// Said under a dry run that did not pass but reached the router:  it claims no pick.
    static let dryRunNothingDeliveredNote = "This was a dry run, so nothing was delivered."

    // MARK: The card

    /// The router's run state:  `Running` when it answers, `Stopped` when its unit is down and
    /// nothing answers, and `Not reporting` for every other case, with the cause.  The unit is
    /// said beside the answer, so a router that answers while its unit is not active, or a unit
    /// that is active while nothing answers, is never shown as plainly well.
    static func card(
        health: RouterHealthReading?,
        wakes: OperatorReading<OperatorWakesDocument>?,
        now: Date
    ) -> OperatorStatusCard {
        let title = "Wake router"
        guard let health else {
            return OperatorStatusCard(title: title, state: "Reading\u{2026}", tone: .neutral, lines: [])
        }
        var lines: [String] = []
        let state: String
        var tone: OperatorTone
        switch health {
        case .answering(let document, let unit):
            state = "Running"
            let installed = document.installedSeats
            switch document.healthy {
            case true?: tone = .good
            case false?: tone = .warning
            case nil: tone = .neutral
            }
            if let unit {
                if let words = unit.words {
                    if unit.isUp {
                        lines.append("Its systemd unit is running.")
                    } else {
                        tone = tone == .good ? .warning : tone
                        lines.append("It answers, but its systemd unit is \(words).")
                    }
                }
            } else {
                lines.append("Its systemd unit's state is not reported.")
            }
            if document.seats.isEmpty {
                lines.append("It reports no seats.")
            } else {
                let deliverOn = installed.filter { $0.delivery == true }.count
                let notReady = installed.filter { $0.healthy == false }.count
                lines.append("Serves \(OperatorPanelText.counted(installed.count, "seat", "seats")).\(gap)Delivery is on for \(deliverOn).")
                if notReady > 0 {
                    lines.append("\(notReady) \(notReady == 1 ? "seat is" : "seats are") not ready.")
                } else if document.healthy == true {
                    lines.append("Every seat is ready.")
                }
            }
        case .notAnswering(let cause, let unit):
            tone = .bad
            if let unit, unit.isDown, let words = unit.words {
                state = "Stopped"
                lines.append("Its systemd unit is \(words).")
            } else {
                state = "Not reporting"
                if let unit, let words = unit.words {
                    lines.append("Its systemd unit is \(words), and the router does not answer.")
                } else {
                    lines.append("Its systemd unit's state is not reported.")
                }
            }
            lines.append(cause)
        case .unreachable(let why), .unavailable(let why):
            state = "Not reporting"
            tone = .warning
            lines.append(why)
        }
        if let wakes {
            if let document = wakes.value, let total = document.totals?.recent {
                let words = OperatorPanelText.counted(total, "wake", "wakes")
                let isFloor = !document.unreadable.isEmpty || document.truncated == true
                lines.append(isFloor ? "At least \(words) in the last 24 hours." : "\(words) in the last 24 hours.")
            } else if wakes.value != nil {
                lines.append("The number of wakes in the last 24 hours is not reported.")
            }
        }
        return OperatorStatusCard(title: title, state: state, tone: tone, lines: lines)
    }

    // MARK: The seats

    static func stateCell(_ seat: RouterSeatHealth) -> OperatorCell {
        if seat.installed == false {
            return OperatorCell(text: "Not installed", tone: .neutral, help: "The router does not have this seat's module installed yet, so it serves nothing for it.")
        }
        switch seat.healthy {
        case true?:
            return OperatorCell(text: "Ready", tone: .good, help: "The router serves this seat and can take its wakes.")
        case false?:
            let text: String
            switch seat.reason {
            case "registry_not_configured"?: text = "Registry not set"
            case "routine_key_unavailable"?: text = "Key not available"
            case "seat_not_served"?: text = "Not served"
            default: text = "Not ready"
            }
            let why = seat.reason.map { OperatorPanelText.humanized($0).lowercased() } ?? "the router did not say why"
            return OperatorCell(text: text, tone: .warning, help: "The router cannot take this seat's wakes: \(why).")
        case nil:
            return OperatorCell(text: "Not reported", tone: .neutral, help: "The router did not say whether this seat is ready.")
        }
    }

    static func deliveryCell(_ seat: RouterSeatHealth) -> OperatorCell {
        switch seat.delivery {
        case true?:
            return OperatorCell(text: "On", tone: .good, help: "A wake for this seat is delivered to the chat the router picks.")
        case false?:
            return OperatorCell(text: "Off", tone: .neutral, help: "A wake for this seat is picked and held, not delivered.")
        case nil:
            return OperatorCell(text: "Not reported", tone: .neutral, help: "The router did not say whether delivery is on for this seat.")
        }
    }

    /// The newest wake of a seat in the wake record that has an outcome, in words.
    static func lastWake(seat: String, wakes: OperatorReading<OperatorWakesDocument>?, now: Date) -> String {
        guard let wakes else { return "Not read yet" }
        guard let document = wakes.value else { return "Not read" }
        guard let entry = document.seats.first(where: { $0.seat == seat }) else {
            return document.unreadable.contains(seat) ? "Not read" : "None in 24 hours"
        }
        // The newest wake that has an outcome:  a wake still on its way has none yet, so it is
        // shown only when the seat has nothing else in the record, and then as still in progress.
        func newer(_ left: OperatorWake, _ right: OperatorWake) -> Bool {
            (left.at ?? .distantPast) < (right.at ?? .distantPast)
        }
        let finished = entry.recent.filter { !inProgressStates.contains($0.state ?? "") }
        let newest = finished.max(by: newer) ?? entry.recent.max(by: newer)
        guard let newest else { return "None in 24 hours" }
        let inProgress = inProgressStates.contains(newest.state ?? "")
        let state = newest.state.map(OperatorPanelText.humanized) ?? "State not reported"
        let said = inProgress ? "\(state) (still in progress)" : state
        guard let at = newest.at else { return said }
        return "\(said), \(OperatorPanelText.relative(at, now: now))"
    }

    /// The wake record's states of a wake that has not reached an outcome yet.
    private static let inProgressStates: Set<String> = ["pending", "dispatching", "queued"]

    /// The seats the router reports, one row each, in the router's own order.  Empty while the
    /// router has not answered:  without its answer there is no seat to name.
    static func seatRows(
        health: RouterHealthReading?,
        wakes: OperatorReading<OperatorWakesDocument>?,
        now: Date
    ) -> [RouterSeatRow] {
        guard let document = health?.document else { return [] }
        return document.seats.map { seat in
            RouterSeatRow(
                seat: seat.seat,
                state: stateCell(seat),
                delivery: deliveryCell(seat),
                lastWake: lastWake(seat: seat.seat, wakes: wakes, now: now),
                canTest: seat.installed == true
            )
        }
    }

    // MARK: The dry run

    /// Why the router picked a chat, in words a person says.
    static func pickReason(_ word: String?) -> String? {
        guard let word else { return nil }
        switch word {
        case "letter": return "the sender addressed its letter"
        case "reply": return "the wake was linked by reply"
        case "session-id": return "the wake was linked by session id"
        case "linear-issue": return "the wake was linked by its Linear issue"
        case "linear-issue-tie": return "the wake was linked by its Linear issue, as the most recent of several chats"
        case "topic-match": return "its title matches the topic"
        case "topic-tie": return "its title matches the topic, as the most recent of several equal matches"
        case "consultant-pick": return "the router's decider chose it"
        case "sole-live-chat": return "it is the only live chat"
        case "most-recent": return "it is the most recent live chat"
        case "no-live-session": return "no live chat is registered for the seat"
        case "registry-unavailable": return "the letter registry could not be read"
        case "registry-not-configured": return "the router has no registry address"
        case "delivery-disabled": return "delivery is off for the seat"
        default: return OperatorPanelText.humanized(word).lowercased()
        }
    }

    static func chatWords(_ answer: RouterTestWakeReport.Answer?) -> String {
        let letter = OperatorPanelPresentation.letterText(letter: answer?.pickedLetter, generation: answer?.pickedGeneration)
        let host = answer?.host.map { OperatorPanelPresentation.hostLabel(code: nil, host: $0) }
        let named = letter == OperatorPanelText.none ? "a chat" : "chat \(letter)"
        if let host, host != "Not known" { return "\(named) on the \(host)" }
        return named
    }

    static func summary(_ reading: RouterTestWakeReading) -> RouterTestWakeSummary {
        switch reading {
        case .unreachable(let why), .unavailable(let why):
            return RouterTestWakeSummary(headline: "The dry run did not run", tone: .warning, lines: [why])
        case .report(let report):
            var lines: [String] = []
            if report.passed == true {
                lines.append("The router would wake \(chatWords(report.answer)).")
                if let why = pickReason(report.answer?.reason) { lines.append("Why: \(why).") }
                if report.routerHealth?.delivery == false {
                    lines.append("Delivery is off for this seat, so a real wake would be held.")
                }
                lines.append(dryRunNote)
                return RouterTestWakeSummary(headline: "The dry run passed", tone: .good, lines: lines)
            }
            // When nothing was sent the router never saw a wake, so no dry-run note is added.
            var reachedRouter = true
            switch report.error {
            case "not_the_router"?:
                reachedRouter = false
                lines.append("This seat's wake route does not point at the wake router, so nothing was sent.")
            case "router_unreachable"?:
                reachedRouter = false
                lines.append("The router did not answer, so nothing was sent.")
            case "routine_not_configured"?:
                reachedRouter = false
                lines.append("This seat has no wake route configured on the server, so nothing was sent.")
            case _? where report.dryRun != true:
                lines.append("The router did not accept the dry run.")
            default:
                if report.dryRun == true, report.picked != true {
                    var line = "The router answered and found no chat to wake."
                    if let why = pickReason(report.answer?.reason) { line += "\(gap)Why: \(why)." }
                    lines.append(line)
                } else if report.dryRun == true, report.addressed == false {
                    lines.append("The router picked \(chatWords(report.answer)), not the chat that was addressed.")
                } else {
                    lines.append("The router did not answer as the dry run expects.")
                }
            }
            if report.routerHealth?.delivery == false {
                lines.append("Delivery is off for this seat, so a real wake would be held.")
            }
            if reachedRouter, report.dryRun == true { lines.append(dryRunNothingDeliveredNote) }
            return RouterTestWakeSummary(headline: "The dry run did not pass", tone: .warning, lines: lines)
        }
    }
}
