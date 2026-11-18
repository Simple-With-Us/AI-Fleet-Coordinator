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

    /// Whether a seat's routine can be reached, as the eavesdropper last found it.
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
        /// Active red warnings the Operator reports for this seat.
        var red: [String]?
        /// Held-back / deferred wakes for this seat.
        var heldBack: HeldBack?
        /// Chat nudge queue counters for this seat.
        var chatNudges: ChatNudges?
        /// Quota reading for this seat.
        var quota: Quota?
        /// Whether the seat's routine can be reached.
        var routine: Routine?
        /// Whether the seat may route a wake to another seat.
        var routeEnabled: Bool?
        /// Whether the seat wakes on being named in a leading bracket intro's recipient list.
        /// An eavesdropper from before the switch does not publish it.
        var bracketRecipientWakes: Bool?

        enum CodingKeys: String, CodingKey {
            case bot, provider, creds, connected, paused
            case wakesPaused = "wakes_paused"
            case wakes24h = "wakes_24h"
            case budget, live, red
            case heldBack = "held_back"
            case chatNudges = "chat_nudges"
            case quota, routine
            case routeEnabled = "route_enabled"
            case bracketRecipientWakes = "bracket_recipient_wakes"
        }
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
        /// Whether resume-wake is verified for terminal sessions on this machine.
        var rewake_verified_cli: Bool?
        /// Whether resume-wake is verified for Desktop sessions on this machine.
        var rewake_verified_desktop: Bool?
        /// The one flag an eavesdropper from before the two above published for both.
        var rewake_verified: Bool?

        enum CodingKeys: String, CodingKey {
            case seats, pause, updated, started, version, instance, disabled, skipped_seats
            case config_errors, reload_refused, reload_at, partition_file
            case operator_voice
            case rewake_verified_cli, rewake_verified_desktop, rewake_verified
        }

        /// Whether resume-wake is verified on this machine, as the eavesdropper says.
        var rewake: RewakeVerification {
            RewakeVerification(
                terminal: rewake_verified_cli ?? rewake_verified,
                desktop: rewake_verified_desktop ?? rewake_verified
            )
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
        rewake_verified_cli = container.lenient(Bool.self, .rewake_verified_cli)
        rewake_verified_desktop = container.lenient(Bool.self, .rewake_verified_desktop)
        rewake_verified = container.lenient(Bool.self, .rewake_verified)
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
        provider = container.lenient(String.self, .provider)
        creds = container.lenient(String.self, .creds)
        connected = container.lenient(Bool.self, .connected)
        paused = container.lenient(Bool.self, .paused)
        wakesPaused = container.lenient(Bool.self, .wakesPaused)
        wakes24h = container.lenient(Int.self, .wakes24h)
        budget = container.lenient([String: JSONValue].self, .budget)
        live = container.lenient([String: JSONValue].self, .live)
        red = container.lenient([String].self, .red)
        heldBack = container.lenient(ListenerStatus.HeldBack.self, .heldBack)
        chatNudges = container.lenient(ListenerStatus.ChatNudges.self, .chatNudges)
        quota = container.lenient(ListenerStatus.Quota.self, .quota)
        routine = container.lenient(ListenerStatus.Routine.self, .routine)
        routeEnabled = container.lenient(Bool.self, .routeEnabled)
        bracketRecipientWakes = container.lenient(Bool.self, .bracketRecipientWakes)
    }
}

// MARK: - What a reload did

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
