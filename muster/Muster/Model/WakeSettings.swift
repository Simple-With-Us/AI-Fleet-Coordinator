import Foundation

// The seat wake settings,  as the panel shows them.
//
// The panel has two sources and never mixes them:  the configuration file
// (`~/.agent-sync/listener.toml`) holds what the operator wrote,  and the running listener
// holds what is actually in effect.  A setting where the two disagree is DRIFT,  and it is
// never shown as a quiet green.
//
// MARK: - The wake types

/// The wake types the listener knows.
enum WakeType: String, CaseIterable {
    case inbox
    case claude
    case http
    case muse
    case openrouter

    var title: String {
        switch self {
        case .inbox: return "Inbox"
        case .claude: return "Claude"
        case .http: return "HTTP"
        case .muse: return "Muse"
        case .openrouter: return "OpenRouter"
        }
    }

    /// Only these start a worker.  `inbox` captures the message and runs nothing.
    var runsWorker: Bool { self != .inbox }
}

// MARK: - Which listener holds a seat

/// Who holds a seat, according to the seat partition.
///
/// The partition (`docs/protocols/agent-sync-partition.toml`) gives each seat to one listener.  A
/// listener refuses to run with a `[seat.X]` section the partition does not give to it, enabled or
/// not, and it fails closed:  a seat it does not list is refused, and a file it cannot read
/// refuses every seat (`agent_sync/config.py` `partition_errors`).  Muster runs on the Mac and
/// reaches only the Mac's listener, so only `.mac` seats can be changed here.
enum ListenerSeatHost: Equatable {
    case mac
    case server
    /// The partition marks the seat `none`.
    case noListener
    /// The partition does not list the seat.
    case unlisted
    /// The partition gives the seat to the Mac, but the seat's own section in the Mac's file says
    /// `instance = "server"`.  The listener tests that key first and refuses the section.
    case markedForTheServer
    /// Muster cannot say:  the partition could not be read, or it disagrees with the running
    /// listener.  The text says which.
    case unknown(String)

    /// Whether the Mac's listener holds this seat.
    var isTheMac: Bool { self == .mac }

    /// Whether the partition was read and the Mac's listener will not run this seat.
    var isElsewhere: Bool {
        switch self {
        case .server, .noListener, .unlisted, .markedForTheServer: return true
        case .mac, .unknown: return false
        }
    }

    /// Why the panel will not change this seat, or nil when it will.
    var readOnlyReason: String? {
        switch self {
        case .mac:
            return nil
        case .server:
            return "Held by the server listener.\u{00A0} Change it there."
        case .noListener:
            return "No listener holds this seat."
        case .unlisted:
            return "No listener holds this seat.\u{00A0} The seat partition does not list it."
        case .markedForTheServer:
            return "The seat's section in the Mac's file says instance = \"server\", so the Mac's listener refuses it."
        case .unknown(let why):
            return "Muster cannot say who holds this seat, so it will not change it.\u{00A0} \(why)"
        }
    }

    /// The place, in words.
    var badge: String {
        switch self {
        case .mac: return "On The Mac"
        case .server: return "On The Server"
        case .noListener, .unlisted: return "No Listener"
        case .markedForTheServer: return "Marked For The Server"
        case .unknown: return "Not Known"
        }
    }

    /// The symbol that goes with the place.
    var systemImage: String {
        switch self {
        case .mac: return "desktopcomputer"
        case .server: return "server.rack"
        case .noListener, .unlisted: return "minus.circle"
        case .markedForTheServer: return "exclamationmark.triangle"
        case .unknown: return "questionmark.circle"
        }
    }
}

/// The seat partition, read the way the listener reads it.
struct ListenerPartition: Equatable {
    /// Where the file sits in a checkout.  The Mac's listener reads its own checkout's copy, and
    /// says which one in its status (`partition_file`), so that copy decides once it has said.
    static let relativePath = "docs/protocols/agent-sync-partition.toml"
    /// The instance name Muster's own machine has in the partition.
    static let thisInstance = "mac"

    /// Seat to instance, or nil when the file could not be read.
    let seats: [String: String]?
    /// Why it could not be read.
    let problem: String?

    /// A partition that was read.
    init(seats: [String: String]) {
        self.seats = seats
        self.problem = nil
    }

    /// A partition that could not be read, which holds no seat.
    init(unreadable problem: String) {
        self.seats = nil
        self.problem = problem
    }

    /// The copy in the checkout Muster runs the listener's command line from.
    static func defaultURL() -> URL? {
        EngineLocator.locate()?.root.appendingPathComponent(relativePath)
    }

    /// Reads the file.  Anything short of a clean read holds no seat.
    static func load(from url: URL?) -> ListenerPartition {
        guard let url else {
            return ListenerPartition(unreadable: "The checkout that holds it was not found.")
        }
        guard let data = try? Data(contentsOf: url) else {
            return ListenerPartition(unreadable: "The file at \(url.path) could not be read.")
        }
        guard let text = String(data: data, encoding: .utf8) else {
            return ListenerPartition(unreadable: "The file at \(url.path) is not text.")
        }
        return parse(text, path: url.path)
    }

    /// Reads the text with the same rules as `load_partition`:  one bad entry fails the whole file.
    static func parse(_ text: String, path: String) -> ListenerPartition {
        let document = ListenerConfigDocument(text: text)
        guard document.problems.isEmpty else {
            return ListenerPartition(unreadable: "The file at \(path) holds a line Muster cannot read.")
        }
        let tables = document.sections.filter { $0.name == "seats" }
        guard tables.count == 1, let table = tables.first else {
            return ListenerPartition(unreadable: "The file at \(path) has no [seats] table.")
        }
        var found: [String: String] = [:]
        for assignment in table.assignments {
            let seat = assignment.key.uppercased()
            guard isSeatName(seat), found[seat] == nil,
                  case .scalar(.text(let instance)) = assignment.node.body,
                  ["mac", "server", "none"].contains(instance) else {
                return ListenerPartition(unreadable: "The file at \(path) has a bad entry for \(assignment.key).")
            }
            found[seat] = instance
        }
        return ListenerPartition(seats: found)
    }

    /// Who holds a seat.  The listener upper-cases a section's name before it looks it up.
    func host(of seat: String) -> ListenerSeatHost {
        guard let seats else {
            return .unknown("The seat partition could not be read.\u{00A0} \(problem ?? "")")
        }
        let instance = seats[seat.uppercased()] ?? ""
        switch instance {
        case "mac": return .mac
        case "server": return .server
        case "none": return .noListener
        default: return .unlisted
        }
    }

    /// Who holds a seat, given the Mac's file.  `partition_errors` tests a section's own `instance`
    /// key before the partition and refuses one that names the other instance, so a seat the
    /// partition gives to the Mac can still be one the Mac's listener will not run.  Its `INSTANCES`
    /// are "mac" and "server", and any other value is a seat problem, not a refusal.
    func host(of seat: String, in config: ListenerConfigDocument) -> ListenerSeatHost {
        let host = self.host(of: seat)
        guard host.isTheMac,
              case .text("server")? = config.value(section: "seat.\(seat)", key: "instance") else {
            return host
        }
        return .markedForTheServer
    }

    /// Whether a tag is one the listener accepts:  `_SEAT_RE` in `agent_sync/config.py`.
    static func isSeatName(_ value: String) -> Bool {
        let scalars = Array(value.unicodeScalars)
        guard (1...32).contains(scalars.count), let first = scalars.first else { return false }
        func isAlphanumeric(_ scalar: Unicode.Scalar) -> Bool {
            (65...90).contains(scalar.value) || (48...57).contains(scalar.value)
        }
        guard isAlphanumeric(first) else { return false }
        return scalars.allSatisfy { isAlphanumeric($0) || $0 == "_" || $0 == "-" }
    }
}

// MARK: - The settings

/// One setting of one seat.
enum WakeSettingKey: String, CaseIterable, Identifiable {
    case wakeType
    case model
    case musePath
    case mcodePath
    case fallback
    case fallbackUncontained
    case wakesPerHour
    case wakesPerDay
    case perTopicPerHour
    case ownerPerDay
    case ownerPerTopicPerHour
    case usdPerDay
    case livePerHour
    case livePerDay
    case livePerTopicMinutes
    case liveOwnerPerDay
    case liveLoopTurns
    case hostLoadGate

    var id: String { rawValue }

    /// The settings a seat owns.  The host load gate is the daemon's,  not a seat's.
    static let seatKeys: [WakeSettingKey] = allCases.filter { $0 != .hostLoadGate }

    var title: String {
        switch self {
        case .wakeType: return "Wake type"
        case .model: return "Model"
        case .musePath: return "Muse binary"
        case .mcodePath: return "Mcode binary"
        case .fallback: return "Fallback"
        case .fallbackUncontained: return "Fallback may answer outside its container"
        case .wakesPerHour: return "Wakes per hour"
        case .wakesPerDay: return "Wakes per day"
        case .perTopicPerHour: return "Wakes per topic per hour"
        case .ownerPerDay: return "Owner wakes per day"
        case .ownerPerTopicPerHour: return "Owner wakes per topic per hour"
        case .usdPerDay: return "US dollars per day"
        case .livePerHour: return "Live wakes per hour"
        case .livePerDay: return "Live wakes per day"
        case .livePerTopicMinutes: return "Live minutes per topic"
        case .liveOwnerPerDay: return "Live owner wakes per day"
        case .liveLoopTurns: return "Live loop turns"
        case .hostLoadGate: return "Host load gate"
        }
    }

    /// Where the setting lives in the seat's section of the configuration file.
    var configLocation: (key: String, subPath: [String]) {
        switch self {
        case .wakeType: return ("wake", [])
        case .model: return ("model", [])
        case .musePath: return ("muse", [])
        case .mcodePath: return ("mcode", [])
        case .fallback: return ("fallback", [])
        case .fallbackUncontained: return ("fallback_uncontained_ok", [])
        case .wakesPerHour: return ("budget", ["wakes_per_hour"])
        case .wakesPerDay: return ("budget", ["wakes_per_day"])
        case .perTopicPerHour: return ("budget", ["per_topic_per_hour"])
        case .ownerPerDay: return ("budget", ["owner_per_day"])
        case .ownerPerTopicPerHour: return ("budget", ["owner_per_topic_per_hour"])
        case .usdPerDay: return ("budget", ["usd_per_day"])
        case .livePerHour: return ("live", ["per_hour"])
        case .livePerDay: return ("live", ["per_day"])
        case .livePerTopicMinutes: return ("live", ["per_topic_minutes"])
        case .liveOwnerPerDay: return ("live", ["owner_per_day"])
        case .liveLoopTurns: return ("live", ["loop_turns"])
        case .hostLoadGate: return ("host_wake_load_per_core", [])
        }
    }

    /// The settings that are budgets.
    var isBudget: Bool {
        switch self {
        case .wakesPerHour, .wakesPerDay, .perTopicPerHour, .ownerPerDay,
             .ownerPerTopicPerHour, .usdPerDay:
            return true
        default:
            return false
        }
    }

    /// The settings that are live caps.
    var isLiveCap: Bool {
        switch self {
        case .livePerHour, .livePerDay, .livePerTopicMinutes, .liveOwnerPerDay, .liveLoopTurns:
            return true
        default:
            return false
        }
    }

    /// Whether the listener's own configuration enforces this setting.
    ///
    /// The dollar cap is read but never acted on:  the listener counts wakes and not
    /// dollars,  by the owner's ruling of Oct 13, 2026.  A setting the engine does not
    /// enforce is refused with a reason,  rather than written as a number that would never
    /// bind.
    var isEnforced: Bool {
        self != .usdPerDay
    }

    /// Why Muster will not write this setting.
    var refusalReason: String? {
        switch self {
        case .usdPerDay:
            return "The listener counts wakes and not dollars,  so this cap is never enforced and Muster will not write it."
        case .hostLoadGate:
            return "The owner ruled the host load gate stays off,  so Muster will not turn it on."
        default:
            return nil
        }
    }
}

// MARK: - The states

/// What the panel knows about one setting.
enum WakeSettingState: String, Equatable {
    /// The listener is using the value the configuration file holds.
    case match
    /// The listener is using something else.
    case drift
    /// The setting is configured,  but nobody ever proved it works.
    case unproven
    /// The listener does not report this setting,  so the panel can only show the file.
    case notOwned
    /// The engine owns this setting,  but the listener does not publish it:  Muster can
    /// write it,  and cannot say whether it is in effect.
    case unpublished
    /// The file does not hold this setting,  so the live value is the default.
    case unset
    /// The setting is deliberately off,  by the owner's ruling.
    case off
    /// Something could not be read.
    case unknown
    /// The Mac's listener does not hold this seat,  so there is nothing to compare here.
    case heldElsewhere

    var label: String {
        switch self {
        case .match: return "PASS"
        case .drift: return "DRIFT"
        case .unproven: return "UNPROVEN"
        case .notOwned: return "NOT OWNED"
        case .unpublished: return "NOT REPORTED"
        case .unset: return "DEFAULT"
        case .off: return "OFF"
        case .unknown: return "UNKNOWN"
        case .heldElsewhere: return "NOT ON MAC"
        }
    }

    /// Whether the state is a problem the owner should see first.
    var isProblem: Bool {
        switch self {
        case .drift, .unproven, .unknown: return true
        case .match, .notOwned, .unpublished, .unset, .off, .heldElsewhere: return false
        }
    }
}

// MARK: - The rows

/// One setting of one seat,  as the panel shows it.
struct WakeSetting: Identifiable, Equatable {
    /// What a field shows for a setting the file does not hold.
    static let unsetText = "—"

    let seat: String
    let key: WakeSettingKey
    /// What the configuration file holds.
    let configured: ListenerConfigValue?
    /// What the running listener is using.
    let live: ListenerConfigValue?
    let state: WakeSettingState
    /// A line about the setting,  in words.
    let note: String

    var id: String { "\(seat).\(key.rawValue)" }
    var title: String { key.title }
    var configuredText: String { configured?.displayText ?? WakeSetting.unsetText }
    var liveText: String { live?.displayText ?? WakeSetting.unsetText }
}

/// The pin that proves a binary works.
struct PinEvidence: Equatable {
    /// The runtime the pin is for:  "muse",  "claude" or "mcode".
    let runtime: String
    /// The binary the pin proves.
    let binary: String?
    /// The model the pin proved.
    let model: String?
    /// When the pin was made.
    let verifiedAt: Date?
    /// Whether the listener holds the pin.
    let pinned: Bool
    /// The listener's own reason,  when it says the pin is stale.
    let why: String?

    /// How old the pin is,  in words.
    var age: String? {
        guard let verifiedAt else { return nil }
        let seconds = Date().timeIntervalSince(verifiedAt)
        if seconds < 0 { return "in the future" }
        if seconds < 3600 { return "\(Int(seconds / 60))m old" }
        if seconds < 86400 { return "\(Int(seconds / 3600))h old" }
        return "\(Int(seconds / 86400))d old"
    }
}

/// How much of a seat's budget it has used,  plus deferred and failed wakes in the last day.
struct WakeUsage: Equatable {
    /// Wakes counted in the last hour,  not from the owner.
    var wakesThisHour: Int = 0
    /// Wakes counted in the last day,  not from the owner.
    var wakesThisDay: Int = 0
    /// Owner wakes counted in the last day.
    var ownerThisDay: Int = 0
    /// The busiest topic's wakes in the last hour.
    var worstTopicThisHour: Int = 0
    /// The busiest owner topic's wakes in the last hour.
    var worstOwnerTopicThisHour: Int = 0
    /// Wakes held back in the last day.
    var heldBackThisDay: Int = 0
    /// Wakes dropped in the last day.
    var droppedThisDay: Int = 0
    /// Wakes skipped in the last day.
    var skippedThisDay: Int = 0
    /// Wakes that failed in the last day.
    var failedThisDay: Int = 0
    /// Wakes that expired in the last day.
    var expiredThisDay: Int = 0

    /// Total deferred / suppressed wakes in the last day.
    var deferredTotalThisDay: Int {
        heldBackThisDay + droppedThisDay + skippedThisDay + expiredThisDay
    }
}

/// One seat's wake settings.
struct WakeSeat: Identifiable, Equatable {
    /// The seat tag,  like "MM".
    let seat: String
    /// Who holds the seat,  according to the seat partition.
    let host: ListenerSeatHost
    /// Whether the Mac's file has a section for this seat.
    let hasSection: Bool
    /// The seat's name as the Operator knows it.
    let bot: String?
    /// The wake type the configuration file holds.
    let configuredWake: WakeType?
    /// The wake type the Operator is running.
    let liveWake: WakeType?
    /// The settings,  in the order the panel shows them.
    let settings: [WakeSetting]
    /// How much of the budget the seat has used.
    let usage: WakeUsage
    /// The seat's last few wakes,  newest first.
    let recentWakes: [WakeLedger.Outcome]
    /// The pin that proves the seat's own binary works.
    let pin: PinEvidence?
    /// The pin for the configured fallback.
    let fallbackPin: PinEvidence?
    /// Whether the kill switch covers this seat.
    let paused: Bool
    /// Whether the kill switch stops this seat's wakes,  and leaves the rest alone.
    let wakesPaused: Bool
    /// The scope the pause names.
    let pauseScope: String?
    /// Whether the Operator is connected for this seat.
    let connected: Bool?
    /// Active red warnings the Operator reports for this seat.
    var redWarnings: [String] = []
    /// Currently queued held-back wakes in the Operator.
    var heldBackQueueCount: Int = 0
    /// Lifetime held-back wakes recorded by the Operator since start.
    var heldBackTotalCount: Int = 0
    /// Lifetime held-back wakes surfaced by the Operator since start.
    var heldBackSurfacedCount: Int = 0
    /// Pending chat nudges queued for this seat.
    var chatNudgesPending: Int = 0
    /// Blocked chat nudges for this seat.
    var chatNudgesBlocked: Int = 0
    /// Accepted chat nudges for this seat.
    var chatNudgesAccepted: Int = 0
    /// Quota remaining percentage when reported by CodeCaps or provider.
    var quotaRemaining: Double? = nil
    /// Whether the seat's quota is exhausted.
    var quotaExhausted: Bool = false
    /// Source of the quota reading (e.g. "codecaps").
    var quotaSource: String? = nil
    /// Why the listener would not run this seat,  when it would not.
    var skippedReasons: [String] = []
    /// Whether the file turns this seat off.
    var disabledInFile: Bool = false

    var id: String { seat }

    /// The settings that are not a plain pass.
    var problems: [WakeSetting] {
        settings.filter { $0.state.isProblem }
    }

    /// Whether the Mac's file holds a section the listener refuses to run with.
    var hasRefusedSection: Bool {
        host.isElsewhere && hasSection
    }

    /// What the panel says about such a section.
    var refusedSectionProblem: String? {
        guard hasRefusedSection else { return nil }
        let why = host == .markedForTheServer
            ? "The section says instance = \"server\""
            : "The seat partition does not give \(seat) to the Mac"
        return "The Mac's configuration file holds a [seat.\(seat)] section.\u{00A0} \(why), so the listener refuses every reload and will refuse its next start until the section is gone."
    }

    /// Whether the seat belongs under the Problems Only filter.
    var needsAttention: Bool {
        !problems.isEmpty || !redWarnings.isEmpty || quotaExhausted
            || hasRefusedSection || !skippedReasons.isEmpty
    }
}

/// Everything the panel shows.
struct WakeSettingsModel: Equatable {
    /// Whether the listener is running.
    var running: Bool = false
    /// The pid of the running listener.
    var pid: Int?
    /// When the listener last published its state.
    var updated: Date?
    /// The seats,  in roster order.
    var seats: [WakeSeat] = []
    /// The daemon-wide settings,  like the host load gate.
    var daemonSettings: [WakeSetting] = []
    /// The Operator's own options.
    var operatorOptions: OperatorOptions = OperatorOptions()
    /// What could not be read.
    var problems: [String] = []
    /// Where the configuration file is.
    var configPath: String = ""
    /// What the Mac's listener itself reports is wrong with its configuration.
    var listenerErrors: [String] = []

    /// Every seat that has something wrong.
    var seatsWithProblems: [WakeSeat] {
        seats.filter { !$0.problems.isEmpty }
    }
}

// MARK: - The sources

/// Everything the panel reads,  before it decides anything.
struct WakeSettingsSources {
    /// The configuration file.
    var config: ListenerConfigDocument = ListenerConfigDocument(text: "")
    /// Whether the configuration file could be read.
    var configReadable: Bool = false
    /// Where the configuration file is.
    var configPath: String = ""
    /// The seat partition.  Until it has been read,  it holds no seat.
    var partition = ListenerPartition(unreadable: "It has not been read.")
    /// Where that partition was read from:  the listener's own copy once it has named one.
    var partitionURL: URL?
    /// The listener's own published state,  when it could be read.
    var status: ListenerStatus?
    /// The pins that prove a seat's binaries work:  seat -> runtime -> evidence.
    var pins: [String: [String: PinEvidence]] = [:]
    /// How much of each seat's budget it has used.
    var usage: [String: WakeUsage] = [:]
    /// The last few wakes of each seat,  newest first.
    var recentWakes: [String: [WakeLedger.Outcome]] = [:]
    /// The roster of seats,  in order.
    var seats: [String] = []
    /// The Operator's options.
    var operatorOptions: OperatorOptions = OperatorOptions()
    /// What could not be read.
    var problems: [String] = []
}

// MARK: - The builder

/// Turns what was read into what the panel shows.
enum WakeSettingsBuilder {
    static func build(_ sources: WakeSettingsSources) -> WakeSettingsModel {
        var model = WakeSettingsModel()
        model.running = sources.status?.running ?? false
        model.pid = sources.status?.pid
        model.updated = sources.status?.updated
        model.configPath = sources.configPath
        model.operatorOptions = sources.operatorOptions
        model.problems = sources.problems + sources.config.problems
        model.listenerErrors = sources.status?.status?.config_errors ?? []
        model.seats = sources.seats.map { seat(for: $0, sources: sources) }
        if let why = sources.partition.problem {
            model.problems.append("The seat partition could not be read, so every seat is read only.\u{00A0} \(why)")
        }
        model.problems.append(contentsOf: model.seats.compactMap(\.refusedSectionProblem))
        if let instance = sources.status?.status?.instance, instance != ListenerPartition.thisInstance {
            model.problems.append("The listener on this machine says it is the \(instance) instance, not the Mac's.")
        }
        model.daemonSettings = daemonSettings(sources)
        return model
    }

    /// One seat's rows.
    private static func seat(for tag: String, sources: WakeSettingsSources) -> WakeSeat {
        let host = host(of: tag, sources: sources)
        // The listener upper-cases a section's name, and publishes every seat under that name, so
        // a `[seat.mm]` section is looked up as MM.
        let published = tag.uppercased()
        // The Mac's listener and the Mac's ledgers say nothing about a seat another listener holds,
        // so nothing from them is shown for it:  no zero, no default and no pause.
        let live = host.isElsewhere ? nil : sources.status?.seat(published)
        let pins: [String: PinEvidence] = host.isElsewhere ? [:] : (sources.pins[tag] ?? [:])
        let section = "seat.\(tag)"
        let settings = WakeSettingKey.seatKeys.map { key -> WakeSetting in
            if host.isElsewhere {
                let location = key.configLocation
                return WakeSetting(
                    seat: tag,
                    key: key,
                    configured: sources.config.value(section: section, key: location.key, subPath: location.subPath),
                    live: nil,
                    state: .heldElsewhere,
                    note: host.readOnlyReason ?? ""
                )
            }
            return setting(for: key, seat: tag, section: section, live: live, pins: pins, config: sources.config)
        }
        return WakeSeat(
            seat: tag,
            host: host,
            hasSection: !sources.config.sections(ofSeat: tag).isEmpty,
            bot: live?.bot ?? sources.config.value(section: section, key: "bot")?.scalarText,
            configuredWake: sources.config.value(section: section, key: "wake")?.scalarText
                .flatMap(WakeType.init(rawValue:)),
            liveWake: live?.wake.flatMap(WakeType.init(rawValue:)),
            settings: settings,
            usage: host.isElsewhere ? WakeUsage() : (sources.usage[tag] ?? WakeUsage()),
            recentWakes: host.isElsewhere ? [] : (sources.recentWakes[tag] ?? []),
            pin: pins["muse"] ?? pins["claude"] ?? pins["mcode"],
            fallbackPin: fallbackPin(for: tag, config: sources.config, pins: pins),
            paused: live?.paused ?? false,
            wakesPaused: live?.wakesPaused ?? false,
            pauseScope: host.isElsewhere ? nil : pauseScope(seat: published, status: sources.status),
            connected: live?.connected,
            redWarnings: live?.red ?? [],
            heldBackQueueCount: live?.heldBack?.count ?? 0,
            heldBackTotalCount: live?.heldBack?.totals?.held ?? 0,
            heldBackSurfacedCount: live?.heldBack?.totals?.surfaced ?? 0,
            chatNudgesPending: live?.chatNudges?.pending ?? 0,
            chatNudgesBlocked: live?.chatNudges?.blocked ?? 0,
            chatNudgesAccepted: live?.chatNudges?.accepted ?? 0,
            quotaRemaining: (live?.quota?.known == true) ? live?.quota?.remaining : nil,
            quotaExhausted: live?.quota?.exhausted ?? false,
            quotaSource: live?.quota?.source,
            skippedReasons: host.isElsewhere ? [] : (sources.status?.status?.skipped_seats?[published] ?? []),
            disabledInFile: !host.isElsewhere && (sources.status?.status?.disabled?.contains(published) ?? false)
        )
    }

    /// Who holds a seat, as far as Muster can say.
    ///
    /// A listener from before `partition_file` does not say which partition it reads, so the copy
    /// Muster read may not be its copy.  If that listener is running a seat this copy gives away,
    /// the two differ, and calling the seat elsewhere would offer to delete a live seat's section.
    private static func host(of tag: String, sources: WakeSettingsSources) -> ListenerSeatHost {
        let host = sources.partition.host(of: tag, in: sources.config)
        guard host.isElsewhere,
              sources.status?.running == true,
              sources.status?.status?.partition_file == nil,
              sources.status?.seat(tag.uppercased()) != nil else {
            return host
        }
        return .unknown("The running listener holds this seat, and the copy Muster read gives it elsewhere.")
    }

    /// One row,  with its state decided.
    private static func setting(
        for key: WakeSettingKey,
        seat: String,
        section: String,
        live: ListenerStatus.Seat?,
        pins: [String: PinEvidence],
        config: ListenerConfigDocument
    ) -> WakeSetting {
        let location = key.configLocation
        let configured = config.value(section: section, key: location.key, subPath: location.subPath)
        let liveValue = liveValue(for: key, section: section, live: live, pins: pins, config: config)
        let state = state(
            for: key,
            section: section,
            configured: configured,
            live: liveValue,
            seatReported: live != nil,
            pins: pins,
            config: config
        )
        return WakeSetting(
            seat: seat,
            key: key,
            configured: configured,
            live: liveValue,
            state: state,
            note: note(for: key, state: state, configured: configured, live: liveValue, pins: pins)
        )
    }

    /// The value the running listener is using.
    private static func liveValue(
        for key: WakeSettingKey,
        section: String,
        live: ListenerStatus.Seat?,
        pins: [String: PinEvidence],
        config: ListenerConfigDocument
    ) -> ListenerConfigValue? {
        switch key {
        case .wakeType:
            return live?.wake.map { .text($0) }
        case .model:
            // The model is proved by a pin,  and only a pin carries one.
            guard let pin = pins["muse"] ?? pins["mcode"] else { return nil }
            return pin.model.map { .text($0) }
        case .musePath:
            return live?.muse.map { .text($0) }
        case .mcodePath:
            return pins["mcode"]?.binary.map { .text($0) }
        case .fallback:
            // The fallback the file names is proved by that runtime's pin.
            guard let fallback = fallbackRuntime(section: section, config: config) else { return nil }
            return pins[fallback] == nil ? nil : .text("verified")
        case .fallbackUncontained:
            // The listener does not publish this,  so the panel has nothing live to compare.
            return nil
        case .wakesPerHour:
            return live?.budget?["wakes_per_hour"]?.configValue
        case .wakesPerDay:
            return live?.budget?["wakes_per_day"]?.configValue
        case .perTopicPerHour:
            return live?.budget?["per_topic_per_hour"]?.configValue
        case .ownerPerDay:
            return live?.budget?["owner_per_day"]?.configValue
        case .ownerPerTopicPerHour:
            return live?.budget?["owner_per_topic_per_hour"]?.configValue
        case .usdPerDay:
            return live?.budget?["usd_per_day"]?.configValue
        case .livePerHour:
            return live?.live?["per_hour"]?.configValue
        case .livePerDay:
            return live?.live?["per_day"]?.configValue
        case .livePerTopicMinutes:
            return live?.live?["per_topic_minutes"]?.configValue
        case .liveOwnerPerDay:
            return live?.live?["owner_per_day"]?.configValue
        case .liveLoopTurns:
            return live?.live?["loop_turns"]?.configValue
        case .hostLoadGate:
            return nil
        }
    }

    /// The state of one row.
    ///
    /// The rule,  in order:
    ///
    ///   * the file holds nothing:  the live value is the default
    ///   * the listener reported nothing for this seat:  what it is using is unknown,  and that is
    ///     a problem,  because its status could not be read or it is not running the seat
    ///   * the listener reported the seat and not this setting:  it does not publish it
    ///   * the two agree:  a pass
    ///   * the two differ:  DRIFT
    ///
    /// A configured fallback or model with no pin is never a pass:  it is unproven,  because
    /// nobody ever showed that it works.
    private static func state(
        for key: WakeSettingKey,
        section: String,
        configured: ListenerConfigValue?,
        live: ListenerConfigValue?,
        seatReported: Bool,
        pins: [String: PinEvidence],
        config: ListenerConfigDocument
    ) -> WakeSettingState {
        // A setting the engine does not enforce is never a pass,  whatever the two values
        // say:  the number in the file would not bind anything.
        if !key.isEnforced {
            return .notOwned
        }
        switch key {
        case .model:
            guard let pin = pins["muse"] ?? pins["mcode"] else {
                return configured == nil ? .unset : .unproven
            }
            guard pin.model != nil else { return .notOwned }
        case .fallback:
            guard configured != nil else { return .unset }
            guard live != nil else { return .unproven }
            return .match
        case .mcodePath:
            guard configured != nil else { return .unset }
            guard live != nil else { return .unproven }
        case .fallbackUncontained:
            // The engine reads this one,  but the listener does not publish it.
            return configured == nil ? .unset : .unpublished
        default:
            break
        }
        // The file first:  a setting nobody holds is the listener's default,  and a
        // setting only the file holds is one the listener does not publish.
        guard let configured else { return .unset }
        guard let live else { return seatReported ? .notOwned : .unknown }
        if configured.matches(live) { return .match }
        if key == .musePath || key == .mcodePath {
            if pathsMatch(configured: configured, live: live) { return .match }
        }
        return .drift
    }

    /// Whether a configured path and a live path are the same binary.
    ///
    /// The file may name a binary on the wake PATH instead of a full path,  so a name is
    /// compared with the last part of the live path.
    private static func pathsMatch(configured: ListenerConfigValue, live: ListenerConfigValue) -> Bool {
        guard let left = configured.scalarText, let right = live.scalarText else { return false }
        let expanded = (left as NSString).expandingTildeInPath
        if expanded.contains("/") {
            return expanded == (right as NSString).expandingTildeInPath
        }
        return (right as NSString).lastPathComponent == left
    }

    /// A line about the setting,  in words.
    private static func note(
        for key: WakeSettingKey,
        state: WakeSettingState,
        configured: ListenerConfigValue?,
        live: ListenerConfigValue?,
        pins: [String: PinEvidence]
    ) -> String {
        switch state {
        case .drift:
            let stale = pins["muse"]?.why ?? pins["mcode"]?.why
            let reason = "The listener is using \(live?.displayText ?? "something else"),  and the file holds \(configured?.displayText ?? "nothing")."
            if let stale { return reason + "\u{00A0} The pin says \(stale)." }
            return reason
        case .unproven:
            if key == .model {
                return "No pin proves this model yet."
            }
            return "This is configured,  but no pin has ever proved it works."
        case .notOwned:
            if let reason = key.refusalReason { return reason }
            return "The listener does not publish this setting."
        case .unpublished:
            return "The listener reads this one,  but does not publish it,  so only the file can be shown."
        case .unset:
            return "The file holds nothing,  so the listener's default is in effect."
        case .off:
            return "Off:  the listener never holds a wake for load."
        case .unknown:
            return "The listener did not report this seat, so what it is using is not known."
        case .heldElsewhere:
            return "The Mac's listener does not hold this seat."
        case .match:
            if key == .model, let pin = pins["muse"] ?? pins["mcode"], let age = pin.age {
                return "Proved by the pin,  \(age)."
            }
            return ""
        }
    }

    /// The runtime the file names as the seat's fallback.
    private static func fallbackRuntime(section: String, config: ListenerConfigDocument) -> String? {
        config.value(section: section, key: "fallback")?.scalarText
    }

    /// The pin for the fallback the file names.
    private static func fallbackPin(
        for tag: String,
        config: ListenerConfigDocument,
        pins: [String: PinEvidence]
    ) -> PinEvidence? {
        let fallback = config.value(section: "seat.\(tag)", key: "fallback")?.scalarText ?? "mcode"
        return pins[fallback]
    }

    /// The scope the pause names,  when it covers this seat.
    private static func pauseScope(seat: String, status: ListenerStatus?) -> String? {
        guard let pause = status?.pause else { return nil }
        for scope in [seat, "*"] {
            if pause[scope] != nil { return scope }
        }
        return nil
    }

    /// The daemon-wide settings.
    ///
    /// The host load gate stays off,  by the owner's ruling:  `0` means the listener never
    /// holds a wake for load.  It is shown as off,  and never as a setting nobody looked at.
    private static func daemonSettings(_ sources: WakeSettingsSources) -> [WakeSetting] {
        let configured = sources.config.value(section: "daemon", key: "host_wake_load_per_core")
        let isOff = (configured?.displayText ?? "0") == "0"
        return [
            WakeSetting(
                seat: "daemon",
                key: .hostLoadGate,
                configured: configured ?? .number(0),
                live: nil,
                state: isOff ? .off : .drift,
                note: isOff
                    ? "Off:  the listener never holds a wake for load."
                    : "The owner ruled this gate stays off."
            ),
        ]
    }
}
