import Foundation

// The Operator's own published state,  and the one command line that may change a wake.
//
// The panel never edits the Operator's state directly.  It reads the Operator's own account
// of itself —  `simple-chat daemon status --json` —  and every change it makes goes through
// the Operator's own command line.

// MARK: - The envelope

/// The Operator's published state.
///
/// This is the envelope `simple-chat daemon status --json` prints:  the running Operator's own
/// account of what it is doing.  A setting the Operator does not publish is a setting the
/// panel cannot claim anything live about.
struct ListenerStatus: Decodable, Equatable {
    /// Held-back / deferred wake counters published by the Operator for a seat.
    struct HeldBackTotals: Decodable, Equatable {
        var held: Int?
        var surfaced: Int?
        var expired: Int?
        var evicted: Int?
        var overflowed: Int?
    }

    /// The seat's held-back wake queue and lifetime totals.
    struct HeldBack: Decodable, Equatable {
        var count: Int?
        var evicted: Int?
        var overflow: Int?
        var reasons: [String: Int]?
        var totals: HeldBackTotals?
        /// How old the oldest held wake is, in seconds.
        var oldest_age: Double?
    }

    /// The fallback an OpenRouter-backed seat publishes:  its model, its own pin, and whether it
    /// would answer right now.  An eavesdropper publishes this only for `fallback = "openrouter"`.
    struct Fallback: Decodable, Equatable {
        var runtime: String?
        var model: String?
        var pinned: Bool?
        var pin: Pin?
        var ready: Bool?
        var stands_in: Bool?
        var primary_hold: String?
    }

    /// Whether an http seat's routine can be reached.
    struct Routine: Decodable, Equatable {
        var ready: Bool?
        var why: String?
        var detail: String?
        var host: String?
    }

    /// Chat nudge queue counters published by the Operator for a seat.
    struct ChatNudges: Decodable, Equatable {
        var accepted: Int?
        var bindings: Int?
        var blocked: Int?
        var cancelled: Int?
        var dispatching: Int?
        var pending: Int?
        var unknown: Int?
    }

    /// Provider quota status published by the Operator when CodeCaps or a provider reports it.
    struct Quota: Decodable, Equatable {
        var exhausted: Bool?
        var known: Bool?
        var reason: String?
        var remaining: Double?
        var reset_at: String?
        var source: String?
    }

    /// One seat's state.
    struct Seat: Decodable, Equatable {
        /// The seat's name,  as the Operator knows it.
        var bot: String?
        /// The wake type the Operator is running.
        var wake: String?
        /// The muse binary the Operator uses.
        var muse: String?
        /// The claude binary the Operator uses.
        var claude: String?
        /// The provider the seat talks to.
        var provider: String?
        /// Where the seat's credentials come from.
        var creds: String?
        /// Whether the Operator is connected for this seat.
        var connected: Bool?
        /// Whether the kill switch stops this seat.
        var paused: Bool?
        /// Whether the kill switch stops this seat's wakes only.
        var wakesPaused: Bool?
        /// The wakes this seat ran in the last day.
        var wakes24h: Int?
        /// The budget the Operator is holding.
        var budget: [String: JSONValue]?
        /// The live caps the Operator is holding.
        var live: [String: JSONValue]?
        /// The pin the Operator holds for this seat.
        var pin: Pin?
        /// Active red warnings the Operator reports for this seat.
        var red: [String]?
        /// Held-back / deferred wakes for this seat.
        var heldBack: HeldBack?
        /// Chat nudge queue counters for this seat.
        var chatNudges: ChatNudges?
        /// Quota reading for this seat.
        var quota: Quota?
        /// The model an OpenRouter wake runs.  An eavesdropper publishes it for that wake only.
        var model: String?
        /// The OpenRouter fallback, when the seat has one.
        var fallback: Fallback?
        /// Whether an http seat's routine can be reached.
        var routine: Routine?
        /// Whether the seat may route a wake to another seat.
        var routeEnabled: Bool?

        enum CodingKeys: String, CodingKey {
            case bot, wake, muse, claude, provider, creds, connected, paused
            case wakesPaused = "wakes_paused"
            case wakes24h = "wakes_24h"
            case budget, live, pin, red
            case heldBack = "held_back"
            case chatNudges = "chat_nudges"
            case quota, model, fallback, routine
            case routeEnabled = "route_enabled"
        }
    }

    /// The pin that proves a seat's binary works, as the eavesdropper judges it on every publish:
    /// the pin file against the binary (or the model and request) the seat runs now.
    struct Pin: Decodable, Equatable {
        /// Whether the eavesdropper holds the pin.
        var pinned: Bool?
        /// The pinned muse binary.
        var pinned_muse: String?
        /// The pinned claude binary.
        var pinned_claude: String?
        /// The pinned OpenRouter model.
        var pinned_model: String?
        /// The claude binary on disk now.
        var claude: String?
        /// The muse binary on disk now.
        var muse: String?
        /// The OpenRouter model the seat runs now.
        var model: String?
        /// The OpenRouter role the pin is for.
        var role: String?
        /// The eavesdropper's own reason,  when the pin is not what it should be:  `not_pinned`,
        /// `pin_stale`, `pin_unverified`, `pin_unreadable`, `no_claude` or `no_muse`.
        var why: String?
    }

    /// One entry of the kill switch.
    struct PauseEntry: Decodable, Equatable {
        /// Whether the pause stops wakes only.
        var wakes_only: Bool?
        /// Who set it.
        var by: String?
        /// When it was set.
        var ts: Double?
    }

    /// Whether the Operator can post to Zulip,  as the eavesdropper last found it
    /// (`status.operator_voice`).  An eavesdropper from before this block does not publish it.
    struct OperatorVoice: Decodable, Equatable {
        /// Whether the Operator can post.  Nil when the block holds no usable answer.
        var available: Bool?
        /// The eavesdropper's own code for why it cannot,  when it gave one.
        var code: String?
        /// When a failure first began,  as the eavesdropper wrote it (ISO-8601).  The
        /// eavesdropper retries every two minutes and keeps this as the first failure's time.
        var since: String?
        /// True when `available` is present and null:  the eavesdropper has not finished its
        /// first Operator build since it started,  so it has not tried yet.  A missing key or a
        /// value of the wrong type is not this.
        var availableIsNull = false

        enum CodingKeys: String, CodingKey {
            case available, code, since
        }
    }

    /// The eavesdropper's own state.
    struct State: Decodable, Equatable {
        /// The seats,  by tag.
        var seats: [String: Seat]?
        /// The kill switch,  by scope.
        var pause: [String: PauseEntry]?
        /// When the eavesdropper last published.
        var updated: Double?
        /// When the eavesdropper started.
        var started: Double?
        /// The eavesdropper's version.
        var version: String?
        /// Which instance this eavesdropper says it is:  "mac" or "server".
        var instance: String?
        /// The seats the file turns off.
        var disabled: [String]?
        /// The seats the eavesdropper would not run, and why.
        var skipped_seats: [String: [String]]?
        /// Everything wrong with the configuration the eavesdropper read, a refused reload included.
        var config_errors: [String]?
        /// Why the eavesdropper refused its last reload, or empty when it did not.  An eavesdropper from
        /// before this field publishes the same text in `config_errors` only.
        var reload_refused: [String]?
        /// When the eavesdropper last ran a reload, accepted or refused.
        var reload_at: Double?
        /// The seat partition file this eavesdropper reads.  An eavesdropper from before this field does
        /// not say.
        var partition_file: String?
        /// Whether the Operator can post.  Nil when the eavesdropper does not publish the block;
        /// a block it publishes in a shape Muster cannot read is kept with no answer in it,  so
        /// it is never mistaken for an eavesdropper too old to say.
        var operator_voice: OperatorVoice?

        enum CodingKeys: String, CodingKey {
            case seats, pause, updated, started, version, instance, disabled, skipped_seats
            case config_errors, reload_refused, reload_at, partition_file
            case operator_voice
        }
    }

    /// Whether the eavesdropper is running.
    var running: Bool?
    /// Its pid.
    var pid: Int?
    /// The state itself.
    var status: State?
    /// The plugin the eavesdropper runs,  when it runs one.
    var plugin: JSONValue?
    /// The launch agent that keeps the eavesdropper alive.
    var launch_agent: JSONValue?

    enum CodingKeys: String, CodingKey {
        case running, pid, status, plugin, launch_agent
    }

    /// The seat's state,  when the eavesdropper has one.
    func seat(_ tag: String) -> Seat? {
        status?.seats?[tag]
    }

    /// The kill switch,  by scope.
    var pause: [String: PauseEntry] {
        status?.pause ?? [:]
    }

    /// When the eavesdropper last published its state.
    var updated: Date? {
        status?.updated.map { Date(timeIntervalSince1970: $0) }
    }

    /// The seats the eavesdropper knows.
    var seatTags: [String] {
        (status?.seats ?? [:]).keys.sorted()
    }
}

// MARK: - Reading the status field by field

// The status is read one field at a time, the way Seat Activity reads its own:  one field an
// eavesdropper writes in a shape Muster does not expect costs that field, and never the whole
// status.  A strict read made one mistyped number blank every seat and say the eavesdropper was
// not running.  The decoders live in extensions so the memberwise initializers stay.

private extension KeyedDecodingContainer {
    /// A field, or nil when it is missing, null or not of this type.
    func lenient<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        guard let value = try? decodeIfPresent(type, forKey: key) else { return nil }
        return value
    }
}

extension ListenerStatus {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        running = container.lenient(Bool.self, .running)
        pid = container.lenient(Int.self, .pid)
        status = container.lenient(State.self, .status)
        plugin = container.lenient(JSONValue.self, .plugin)
        launch_agent = container.lenient(JSONValue.self, .launch_agent)
    }
}

extension ListenerStatus.State {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        seats = container.lenient([String: ListenerStatus.Seat].self, .seats)
        pause = container.lenient([String: ListenerStatus.PauseEntry].self, .pause)
        updated = container.lenient(Double.self, .updated)
        started = container.lenient(Double.self, .started)
        version = container.lenient(String.self, .version)
        instance = container.lenient(String.self, .instance)
        disabled = container.lenient([String].self, .disabled)
        skipped_seats = container.lenient([String: [String]].self, .skipped_seats)
        config_errors = container.lenient([String].self, .config_errors)
        reload_refused = container.lenient([String].self, .reload_refused)
        reload_at = container.lenient(Double.self, .reload_at)
        partition_file = container.lenient(String.self, .partition_file)
        // A missing or null block is an eavesdropper that does not say.  A block of the wrong
        // shape costs only this block:  it is kept with no answer in it,  never dropped.
        if container.contains(.operator_voice), (try? container.decodeNil(forKey: .operator_voice)) != true {
            operator_voice = container.lenient(ListenerStatus.OperatorVoice.self, .operator_voice)
                ?? ListenerStatus.OperatorVoice(available: nil, code: nil, since: nil)
        } else {
            operator_voice = nil
        }
    }
}

extension ListenerStatus.OperatorVoice {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        available = container.lenient(Bool.self, .available)
        code = container.lenient(String.self, .code)
        since = container.lenient(String.self, .since)
        availableIsNull = container.contains(.available)
            && (try? container.decodeNil(forKey: .available)) == true
    }
}

// MARK: - Whether the Operator can post

/// What the eavesdropper says about the Operator's voice:  whether the Operator can post to
/// Zulip right now.  Muster never fills a gap with a guess:  an eavesdropper that does not
/// report it reads as not reported,  never as available.
enum OperatorVoiceReading: Equatable {
    /// The status could not be read,  and this is why.
    case notRead(String)
    /// The eavesdropper does not report it.  An older eavesdropper does not publish the block.
    case notReported
    /// The eavesdropper reports it in a shape Muster cannot read.
    case unreadable
    /// The eavesdropper has not tried yet:  `available` is null until its first Operator build
    /// since it started has finished.
    case notTriedYet
    /// The Operator can post.
    case available(since: Date?, sinceText: String?)
    /// The Operator cannot post.  `code` is the eavesdropper's own code,  safe to show.
    case unavailable(code: String?, since: Date?, sinceText: String?)
    /// The status is not current or the eavesdropper is not running,  so whether the Operator
    /// can post now is not known.  `lastAvailable` is what the last status said,  and `code` is
    /// the eavesdropper's own code when that status said the Operator could not post.
    case notCurrent(why: String, lastAvailable: Bool, since: Date?, sinceText: String?, code: String? = nil)

    /// The reading from a status.
    init(_ state: ListenerStatus.State?) {
        guard let state else {
            self = .notRead("The eavesdropper's status was not read, so whether the Operator can post is not known.")
            return
        }
        guard let voice = state.operator_voice else {
            self = .notReported
            return
        }
        let sinceText = voice.since.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 }
        let since = sinceText.flatMap { FleetTime.parse($0) }
        switch voice.available {
        case true?:
            self = .available(since: since, sinceText: sinceText)
        case false?:
            let code = voice.code.map(SeatActivityText.label).flatMap { $0.isEmpty ? nil : $0 }
            self = .unavailable(code: code, since: since, sinceText: sinceText)
        case nil:
            self = voice.availableIsNull ? .notTriedYet : .unreadable
        }
    }

    /// The reading the Wake & Listener panel shows,  from the same status the rest of the panel
    /// reads (`simple-chat daemon status --json`) and judged by the same currency.  A status
    /// that is not current,  or an eavesdropper that is not running,  never reads as Can post or
    /// Cannot post:  it reads as not known,  with what the last status said.
    ///
    /// - Parameters:
    ///   - status:  The envelope as read,  or nil when it could not be read.
    ///   - runState:  Whether the eavesdropper is running,  as the panel decided it.
    ///   - statusIsCurrent:  Whether the status is current,  as the panel decided it.
    static func current(
        _ status: ListenerStatus?,
        runState: ListenerRunState,
        statusIsCurrent: Bool
    ) -> OperatorVoiceReading {
        guard let status else {
            return .notRead("The eavesdropper's status could not be read, so whether the Operator can post is not known.")
        }
        if let instance = status.status?.instance, instance != ListenerPartition.thisInstance {
            return .notRead("The eavesdropper on this machine says it is the \(instance) instance, not the Mac's, so whether the Operator can post is not known.")
        }
        let reading = OperatorVoiceReading(status.status)
        guard !statusIsCurrent else { return reading }
        let why: String
        switch runState {
        case .running: why = "The eavesdropper's status is not current"
        case .stopped: why = "The eavesdropper is not running"
        case .unknown: why = "Whether the eavesdropper is running is not known"
        }
        switch reading {
        case .available(let since, let sinceText):
            return .notCurrent(why: why, lastAvailable: true, since: since, sinceText: sinceText)
        case .unavailable(let code, let since, let sinceText):
            return .notCurrent(why: why, lastAvailable: false, since: since, sinceText: sinceText, code: code)
        case .notRead, .notReported, .unreadable, .notTriedYet, .notCurrent:
            return reading
        }
    }

    /// Whether the Operator cannot post,  as a current status says.
    var isUnavailable: Bool {
        if case .unavailable = self { return true }
        return false
    }

    /// Whether the panel shows this as something to look at.
    var isProblem: Bool {
        switch self {
        case .unavailable, .unreadable: return true
        case .notCurrent(_, let lastAvailable, _, _, _): return !lastAvailable
        case .notRead, .notReported, .notTriedYet, .available: return false
        }
    }

    /// The short answer the Operator section shows.
    var label: String {
        switch self {
        case .available: return "Can post"
        case .unavailable: return "Cannot post"
        case .notReported: return "Not reported"
        case .notTriedYet: return "Not tried yet"
        case .unreadable, .notRead, .notCurrent: return "Not known"
        }
    }

    /// The symbol beside the answer,  never instead of it.
    var systemImage: String {
        switch self {
        case .available: return "checkmark.circle.fill"
        case .unavailable: return "exclamationmark.triangle.fill"
        case .notReported: return "info.circle"
        case .notTriedYet: return "clock"
        case .unreadable, .notRead, .notCurrent: return "questionmark.circle"
        }
    }

    /// The answer in a sentence,  with the time on Jay's clock.
    func sentence(now: Date = Date()) -> String {
        switch self {
        case .available(let since, _):
            guard let since else { return "The Operator can post to Zulip." }
            return "The Operator can post to Zulip, and has been able to since \(Self.whenText(since, now: now))."
        case .unavailable(let code, let since, _):
            let cause = code.flatMap(Self.cause).map { "\u{00A0} " + $0 } ?? ""
            guard let since else { return "The Operator cannot post to Zulip." + cause }
            return "The Operator cannot post to Zulip, and has not been able to since \(Self.whenText(since, now: now))." + cause
        case .notTriedYet:
            return "The Mac's eavesdropper has not tried the Operator yet.\u{00A0} It tries once soon after it starts."
        case .notCurrent(let why, let lastAvailable, let since, _, let code):
            let said = lastAvailable ? "could post" : "could not post"
            let from = since.map { " from \(Self.whenText($0, now: now))" } ?? ""
            let cause = lastAvailable ? "" : (code.flatMap(Self.cause).map { "\u{00A0} " + $0 } ?? "")
            return "\(why), so whether the Operator can post now is not known.\u{00A0} Its last status said the Operator \(said)\(from)." + cause
        case .notReported:
            return "The Mac's eavesdropper does not report whether the Operator can post."
        case .unreadable:
            return "The Mac's eavesdropper reports whether the Operator can post in a shape Muster cannot read, so it is not known."
        case .notRead(let why):
            return why
        }
    }

    /// The tooltip:  where the answer came from,  with the eavesdropper's own code,  and its
    /// time on Jay's clock.  A time Muster could not read is quoted and said to be so.
    func tooltip(now: Date = Date()) -> String {
        switch self {
        case .available(let since, let sinceText):
            return "From status.operator_voice:  available is true" + Self.sinceClause(since, sinceText, now: now) + "."
        case .unavailable(let code, let since, let sinceText):
            return "From status.operator_voice:  available is false, code \(code ?? "not given")"
                + Self.sinceClause(since, sinceText, now: now) + "."
        case .notCurrent(_, let lastAvailable, let since, let sinceText, let code):
            let codeClause = lastAvailable ? "" : ", code \(code ?? "not given")"
            return "From a status.operator_voice that is not current:  available was \(lastAvailable)" + codeClause
                + Self.sinceClause(since, sinceText, now: now) + "."
        case .notTriedYet:
            return "From status.operator_voice:  available is null, which the eavesdropper writes until its first Operator build since it started has finished."
        case .notReported:
            return "The eavesdropper's status has no operator_voice block.\u{00A0} An older eavesdropper does not report it, so this is not a sign that the Operator can post."
        case .unreadable:
            return "The eavesdropper's status has an operator_voice block Muster cannot read, so whether the Operator can post is not known."
        case .notRead(let why):
            return why
        }
    }

    /// The cause in plain words for a reason code the eavesdropper writes,  or nil for a code
    /// Muster does not know (that code stays in the tooltip,  never guessed at).
    static func cause(_ code: String) -> String? {
        switch code {
        case "operator_credential_rejected":
            return "Zulip refused the Operator's credential."
        case "operator_owner_not_pinned":
            return "The owner is not pinned in the listener's settings, so the Operator does not post."
        case "operator_unavailable":
            // The catch-all for any failed build of the Operator client:  an unreadable
            // credential, not the pinned bot, or an identity it could not confirm.
            return "The listener could not get the Operator ready to post."
        default:
            return nil
        }
    }

    /// `, since 2:14 PM`,  or the raw text when it is not a time Muster can read.
    private static func sinceClause(_ since: Date?, _ sinceText: String?, now: Date) -> String {
        if let since { return ", since \(whenText(since, now: now))" }
        if let sinceText { return ", since a time Muster could not read:  \(sinceText)" }
        return ""
    }

    /// The lines for the panel's problem box,  or none when the Operator is not known to be
    /// unable to post.  Dropped wakes are tied to it only when the ledger says so:  a wake the
    /// panel shows as dropped with the eavesdropper's own reason `operator_unavailable`.  A
    /// status that is not current,  whose last word was that the Operator could not post,  says
    /// so and that it is not current.
    func problemLines(seats: [WakeSeat], now: Date = Date()) -> [String] {
        switch self {
        case .unavailable: break
        case .notCurrent(_, let lastAvailable, _, _, _) where !lastAvailable: break
        default: return []
        }
        var text = sentence(now: now)
        let dropped = Self.droppedForVoice(seats)
        if dropped.count > 0 {
            let verb = dropped.count == 1 ? "was" : "were"
            text += "\u{00A0} \(dropped.count) of the last wakes listed for \(dropped.seats.joined(separator: ", ")) \(verb) dropped because the Operator could not post."
        }
        return [text]
    }

    /// The ledger's reason for a wake dropped because the Operator could not post.
    static let droppedReason = "operator_unavailable"

    /// The recent wakes the panel shows as dropped because the Operator could not post,  and
    /// the seats they belong to,  in the panel's order.  Only seats the Mac's eavesdropper
    /// holds have recent wakes here.
    static func droppedForVoice(_ seats: [WakeSeat]) -> (count: Int, seats: [String]) {
        var count = 0
        var named: [String] = []
        for seat in seats {
            let dropped = seat.recentWakes.filter { $0.state == "dropped" && $0.reason == Self.droppedReason }.count
            guard dropped > 0 else { continue }
            count += dropped
            named.append(seat.seat)
        }
        return (count, named)
    }

    /// A time on Jay's clock:  the time alone on the same day as `now`,  else the month and
    /// day with it.
    static func whenText(_ date: Date, now: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Chicago") ?? .current
        if calendar.isDate(date, inSameDayAs: now) {
            return FleetTime.clock(date)
        }
        let parts = calendar.dateComponents([.month, .day], from: date)
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let month = parts.month.map { months[max(0, min(11, $0 - 1))] } ?? ""
        return "\(month) \(parts.day ?? 0), \(FleetTime.clock(date))"
    }
}

extension ListenerStatus.Seat {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        bot = container.lenient(String.self, .bot)
        wake = container.lenient(String.self, .wake)
        muse = container.lenient(String.self, .muse)
        claude = container.lenient(String.self, .claude)
        provider = container.lenient(String.self, .provider)
        creds = container.lenient(String.self, .creds)
        connected = container.lenient(Bool.self, .connected)
        paused = container.lenient(Bool.self, .paused)
        wakesPaused = container.lenient(Bool.self, .wakesPaused)
        wakes24h = container.lenient(Int.self, .wakes24h)
        budget = container.lenient([String: JSONValue].self, .budget)
        live = container.lenient([String: JSONValue].self, .live)
        pin = container.lenient(ListenerStatus.Pin.self, .pin)
        red = container.lenient([String].self, .red)
        heldBack = container.lenient(ListenerStatus.HeldBack.self, .heldBack)
        chatNudges = container.lenient(ListenerStatus.ChatNudges.self, .chatNudges)
        quota = container.lenient(ListenerStatus.Quota.self, .quota)
        model = container.lenient(String.self, .model)
        fallback = container.lenient(ListenerStatus.Fallback.self, .fallback)
        routine = container.lenient(ListenerStatus.Routine.self, .routine)
        routeEnabled = container.lenient(Bool.self, .routeEnabled)
    }
}

// MARK: - What a reload did

/// What the eavesdropper did when it was asked to re-read the file.
///
/// `daemon reload` only sends a signal and exits 0, so its exit code says nothing about whether
/// the eavesdropper took the file.  The answer is in the status the eavesdropper publishes afterwards.
enum ListenerReloadOutcome: Equatable {
    /// The eavesdropper read the file and is running on it.  `otherProblems` are soft errors it
    /// reports, which do not stop the reload.
    case accepted(otherProblems: [String])
    /// The eavesdropper refused the file and kept its old settings.
    case refused([String])
    /// An eavesdropper too old to say which:  it re-read the file and reports these errors, which may
    /// include a refusal, so Muster cannot call it accepted.
    case reportsProblems([String])
    /// An eavesdropper too old to say which, reporting nothing wrong.  It publishes on every tick, so a
    /// status newer than the signal does not show it has read the file, or that it took it.
    case cannotTell
    /// The eavesdropper published nothing newer in time.
    case noAnswer(seconds: Int)
    /// The signal could not be sent.
    case notAsked(String)

    /// Whether the panel shows this as a failure.
    var isFailure: Bool {
        if case .accepted = self { return false }
        return true
    }

    /// What the panel says.
    var report: String {
        switch self {
        case .accepted(let others):
            guard !others.isEmpty else { return "The eavesdropper accepted it." }
            return "The eavesdropper accepted it, and reports these problems:\n" + others.joined(separator: "\n")
        case .refused(let reasons):
            return "The eavesdropper refused it and kept running on its old settings.\u{00A0} It will refuse to start until this is fixed:\n"
                + reasons.joined(separator: "\n")
        case .reportsProblems(let problems):
            return "The eavesdropper re-read it and reports these problems, so Muster cannot say it accepted the change:\n"
                + problems.joined(separator: "\n")
        case .cannotTell:
            return "This eavesdropper is too old to say whether it took the change, so Muster cannot say it is in effect.\u{00A0} An eavesdropper restart loads the current settings and gives Muster an answer next time."
        case .noAnswer(let seconds):
            return "No answer from the eavesdropper after \(seconds) seconds, so Muster cannot say the change is in effect."
        case .notAsked(let reason):
            let detail = reason.isEmpty ? "" : "\u{00A0} \(reason)"
            return "The eavesdropper could not be asked to re-read it, so the change is not in effect yet.\(detail)"
        }
    }

    /// The answer a published status gives to a reload sent at a time, or nil when that status
    /// could have been written before the eavesdropper acted on it.
    static func answer(toReloadSentAt sentAt: Double, in status: ListenerStatus) -> ListenerReloadOutcome? {
        guard let state = status.status else { return nil }
        if let refused = state.reload_refused {
            guard let reloadAt = state.reload_at, reloadAt >= sentAt else { return nil }
            if !refused.isEmpty { return .refused(refused) }
            return .accepted(otherProblems: state.config_errors ?? [])
        }
        guard let updated = state.updated, updated >= sentAt else { return nil }
        let problems = state.config_errors ?? []
        return problems.isEmpty ? .cannotTell : .reportsProblems(problems)
    }
}

// MARK: - What the eavesdropper thinks of a candidate file

/// The answer of `daemon check-config`:  the eavesdropper's own rules run on a candidate file.
struct ListenerConfigCheck: Decodable, Equatable {
    var ok: Bool
    /// Why the candidate is not TOML the eavesdropper can read.
    var parse_error: String?
    /// Errors the candidate has and the current file does not.
    var new_errors: [String]?
    /// What the seat partition refuses in the candidate.
    var refused: [String]?

    /// What stops an ordinary edit:  anything new, and any refusal at all, because the eavesdropper
    /// would refuse every reload of a file that holds one.
    var problems: [String] {
        var found: [String] = parse_error.map { [$0] } ?? []
        for problem in (refused ?? []) + (new_errors ?? []) where !found.contains(problem) {
            found.append(problem)
        }
        return found
    }

    /// What stops a repair:  only something new, because a repair of one refused section may leave
    /// another one the file already had.
    var problemsForARepair: [String] {
        (parse_error.map { [$0] } ?? []) + (new_errors ?? [])
    }
}

// MARK: - The command line

/// A run of the fleet's `simple-chat` command line.
struct AgentSyncInvocation: Equatable {
    var arguments: [String]
    var timeout: TimeInterval

    static let defaultTimeout: TimeInterval = 60
    /// A test wake runs a real agent,  so it gets the engine's own long timeout.
    static let testWakeTimeout: TimeInterval = 900

    init(_ arguments: [String], timeout: TimeInterval = AgentSyncInvocation.defaultTimeout) {
        self.arguments = arguments
        self.timeout = timeout
    }

    /// `daemon status --json`:  the eavesdropper's own published state.
    static func status() -> AgentSyncInvocation {
        AgentSyncInvocation(["daemon", "status", "--json"])
    }

    /// `daemon pause --seat S --wakes-only`:  the kill switch,  for wakes only.
    static func pause(seat: String) throws -> AgentSyncInvocation {
        AgentSyncInvocation([
            "daemon", "pause", "--seat", try checked(seat), "--wakes-only",
        ])
    }

    /// `daemon resume --seat S`:  lift the kill switch.
    static func resume(seat: String) throws -> AgentSyncInvocation {
        AgentSyncInvocation(["daemon", "resume", "--seat", try checked(seat)])
    }

    /// `daemon reload`:  re-read the configuration file.
    static func reload() -> AgentSyncInvocation {
        AgentSyncInvocation(["daemon", "reload"])
    }

    /// `daemon check-config --file F [--partition P] --json`:  would the eavesdropper take this
    /// candidate file?
    ///
    /// The command line runs from Muster's checkout and the eavesdropper can run from another, so
    /// `partition` names the copy to judge by.  A checkout too old for the flag exits 2 with no
    /// answer, and that stops the write.
    static func checkConfig(file: String, partition: String?) -> AgentSyncInvocation {
        let copy = partition.map { ["--partition", $0] } ?? []
        return AgentSyncInvocation(["daemon", "check-config", "--file", file] + copy + ["--json"])
    }

    /// `daemon test-wake --seat S --runtime R --run`:  run the seat's wake for real.
    ///
    /// `runtime` is nil when the seat's wake is not one of the runtimes the CLI names
    /// (`claude`, `muse`, `mcode`, `fallback`).  Passing a wake type the CLI does not know
    /// -- `inbox` or `http` -- makes argparse exit 2 with "invalid choice",  so the flag is
    /// left off and the CLI's own default (`auto`) decides instead.
    static func testWakeRun(seat: String, runtime: String?) throws -> AgentSyncInvocation {
        AgentSyncInvocation([
            "daemon", "test-wake", "--seat", try checked(seat),
        ] + runtimeArgument(runtime) + ["--run"], timeout: testWakeTimeout)
    }

    /// `daemon test-wake --seat S --runtime R --pin`:  pin the binary that passed.
    static func testWakePin(seat: String, runtime: String?) throws -> AgentSyncInvocation {
        AgentSyncInvocation([
            "daemon", "test-wake", "--seat", try checked(seat),
        ] + runtimeArgument(runtime) + ["--pin"], timeout: testWakeTimeout)
    }

    /// `--runtime R`,  or nothing when there is no runtime to name.
    private static func runtimeArgument(_ runtime: String?) -> [String] {
        guard let runtime else { return [] }
        return ["--runtime", (try? checked(runtime)) ?? runtime]
    }

    /// The command as a person would type it.
    var commandLine: String {
        (["simple-chat"] + arguments).joined(separator: " ")
    }

    /// A seat tag and a runtime name are plain tokens.  A value that could be read as a
    /// flag,  or that carries whitespace or a path separator,  is refused before any
    /// process exists.
    private static func checked(_ value: String) throws -> String {
        guard FleetConfigInvocation.isSafeToken(value) else {
            throw AgentSyncError.invalidArgument(value)
        }
        return value
    }
}

// MARK: - Results and errors

struct AgentSyncCLIResult: Equatable {
    var exitCode: Int32
    var stdout: String
    var stderr: String
    var timedOut: Bool = false

    /// The last few non-empty stderr lines,  bounded,  for showing a refusal.
    var stderrTail: String {
        Self.tail(of: stderr)
    }

    /// The reason to show for a run that failed.  The eavesdropper's own command line prints its
    /// refusals (`FAIL: …`, `NOT PINNED: …`) on stdout and leaves stderr empty, so an empty stderr
    /// falls back to the last lines of stdout rather than to nothing.
    var failureTail: String {
        let errors = stderrTail
        return errors.isEmpty ? Self.tail(of: stdout) : errors
    }

    /// The last few non-empty lines of a text,  bounded.
    static func tail(of text: String) -> String {
        let lines = text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(6).joined(separator: "\n")
        return tail.count > 800 ? String(tail.suffix(800)) : tail
    }
}

enum AgentSyncError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    case commandNotFound(String)
    case launchFailed(String)
    case timedOut(Int)
    case outputTooLarge
    case refused(exitCode: Int32, message: String)
    case undecodable(String)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let value):
            return "The \(value) value is not a plain token,  so nothing was run."
        case .commandNotFound(let path):
            return "The simple-chat command line was not found at \(path)."
        case .launchFailed(let detail):
            return "The simple-chat command line could not be started:  \(detail)"
        case .timedOut(let seconds):
            return "The simple-chat command line did not finish within \(seconds) seconds."
        case .outputTooLarge:
            return "The simple-chat command line produced more output than Muster will read."
        case .refused(let code, let message):
            let detail = message.isEmpty ? "No reason was given." : message
            return "The simple-chat command line refused the run (exit \(code)).  \(detail)"
        case .undecodable(let detail):
            return "The simple-chat output could not be read:  \(detail)"
        }
    }
}

// MARK: - The runner

protocol AgentSyncRunning: Sendable {
    func run(_ invocation: AgentSyncInvocation) async throws -> AgentSyncCLIResult
}

/// Runs the fleet's `simple-chat` command line from the repository checkout.
///
/// The command line is the Operator's own,  at `scripts/simple-chat`,  so the app asks the
/// same tool a person would ask.  The child environment keeps HOME and PATH,  and drops the
/// variables that could point the command line at a stand-in.
struct AgentSyncCLI: AgentSyncRunning {
    static let maximumOutputBytes = 8 * 1024 * 1024

    /// Where the checkout is.
    var location: @Sendable () -> EngineLocation?

    init(location: @escaping @Sendable () -> EngineLocation? = { EngineLocator.locate() }) {
        self.location = location
    }

    /// The command line's own path.
    static func commandURL(at location: EngineLocation) -> URL {
        location.packageDirectory.appendingPathComponent("simple-chat")
    }

    /// The child environment.
    ///
    /// HOME stays,  because the eavesdropper's state lives under it.  PYTHON* and FLEET_* go,
    /// so nothing in the app's environment can point the command line at another checkout.
    static func sanitizedEnvironment(_ environment: [String: String]) -> [String: String] {
        var result = environment.filter { key, _ in
            !key.hasPrefix("PYTHON") && !key.hasPrefix("FLEET_")
        }
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        return result
    }

    static func processArguments(at location: EngineLocation, for invocation: AgentSyncInvocation) -> [String] {
        [commandURL(at: location).path] + invocation.arguments
    }

    func run(_ invocation: AgentSyncInvocation) async throws -> AgentSyncCLIResult {
        guard let resolved = location() else {
            throw AgentSyncError.commandNotFound("the repository checkout")
        }
        let command = Self.commandURL(at: resolved)
        guard FileManager.default.isExecutableFile(atPath: command.path) else {
            throw AgentSyncError.commandNotFound(command.path)
        }
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let result = try Self.runBlocking(
                        invocation,
                        at: resolved,
                        command: command
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
        _ invocation: AgentSyncInvocation,
        at location: EngineLocation,
        command: URL
    ) throws -> AgentSyncCLIResult {
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
        process.arguments = invocation.arguments
        process.currentDirectoryURL = location.packageDirectory
        process.environment = sanitizedEnvironment(ProcessInfo.processInfo.environment)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            try? stdoutHandle.close()
            try? stderrHandle.close()
            throw AgentSyncError.launchFailed(error.localizedDescription)
        }

        let flag = TimeoutFlag()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + invocation.timeout)
        timer.setEventHandler {
            flag.set()
            if process.isRunning { process.terminate() }
        }
        timer.resume()
        process.waitUntilExit()
        timer.cancel()
        try? stdoutHandle.close()
        try? stderrHandle.close()

        return AgentSyncCLIResult(
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
        if data.count > maximumOutputBytes { throw AgentSyncError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
    }
}

// MARK: - The JSON bridge

extension JSONValue {
    /// The value as the configuration file holds it.
    var configValue: ListenerConfigValue {
        switch self {
        case .string(let text): return .text(text)
        case .number(let number): return .number(number)
        case .bool(let flag): return .bool(flag)
        case .array(let items): return .list(items.map(\.configValue))
        case .object(let fields):
            var result: [String: ListenerConfigValue] = [:]
            for (key, value) in fields {
                result[key] = value.configValue
            }
            return .table(result)
        case .null: return .text("")
        }
    }
}
