import Foundation

// The seat wake settings,  as the panel shows them.
//
// The panel has two sources and never mixes them:  the configuration file
// (`~/.simple-tools/listener.toml`) holds what the operator wrote,  and the running eavesdropper
// holds what is actually in effect.  A setting where the two disagree is DRIFT,  and it is
// never shown as a quiet green.
//
// MARK: - The wake types

/// The wake types the eavesdropper knows.
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

    /// The runtime `daemon test-wake --runtime` names for this wake, or nil when the eavesdropper
    /// runs nothing on this Mac to test:  `cmd_test_wake` refuses any seat whose wake is not
    /// claude, muse or openrouter.
    var testRuntime: String? {
        switch self {
        case .claude, .muse, .openrouter: return rawValue
        case .inbox, .http: return nil
        }
    }

    /// The pin file a wake of this type is proved by, by the reader's runtime key, or nil when
    /// the wake needs no pin.
    var pinKey: String? { testRuntime }

    /// Why a wake of this type needs no pin and has no test wake.
    var noPinReason: String? {
        switch self {
        case .inbox: return "An inbox wake captures the message and runs no worker, so there is nothing to test or pin."
        case .http: return "An HTTP wake calls a remote routine and runs no worker on this Mac, so there is nothing to test or pin."
        case .claude, .muse, .openrouter: return nil
        }
    }

    /// The pin file a fallback runtime is proved by.  The OpenRouter fallback has a pin of its own,
    /// apart from an OpenRouter wake's (`wake-pin-openrouter-fallback.json`).
    static func fallbackPinKey(_ fallback: String) -> String {
        fallback == "openrouter" ? "openrouter-fallback" : fallback
    }

    /// The runtimes the reader reads a pin file for.
    static let pinKeys = ["claude", "muse", "mcode", "openrouter", "openrouter-fallback"]
}

// MARK: - Which eavesdropper holds a seat

/// Who holds a seat, according to the seat partition.
///
/// The partition (`docs/protocols/simple-tools-partition.toml`) gives each seat to one eavesdropper.  A
/// eavesdropper refuses to run with a `[seat.X]` section the partition does not give to it, enabled or
/// not, and it fails closed:  a seat it does not list is refused, and a file it cannot read
/// refuses every seat (`simple_tools/config.py` `partition_errors`).  Muster runs on the Mac and
/// reaches only the Mac's eavesdropper, so only `.mac` seats can be changed here.
enum ListenerSeatHost: Equatable {
    case mac
    case server
    /// The partition marks the seat `none`.
    case noListener
    /// The partition does not list the seat.
    case unlisted
    /// The partition gives the seat to the Mac, but the seat's own section in the Mac's file says
    /// `instance = "server"`.  The eavesdropper tests that key first and refuses the section.
    case markedForTheServer
    /// Muster cannot say:  the partition could not be read, or it disagrees with the running
    /// eavesdropper.  The text says which.
    case unknown(String)

    /// Whether the Mac's eavesdropper holds this seat.
    var isTheMac: Bool { self == .mac }

    /// Whether the partition was read and the Mac's eavesdropper will not run this seat.
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
            return "Held by the server eavesdropper.\u{00A0} Change it there."
        case .noListener:
            return "No eavesdropper holds this seat."
        case .unlisted:
            return "No eavesdropper holds this seat.\u{00A0} The seat partition does not list it."
        case .markedForTheServer:
            return "The seat's section in the Mac's file says instance = \"server\", so the Mac's eavesdropper refuses it."
        case .unknown(let why):
            return "Muster cannot say who holds this seat, so it will not change it.\u{00A0} \(why)"
        }
    }

    /// The place, in words.
    var badge: String {
        switch self {
        case .mac: return "On The Mac"
        case .server: return "On The Server"
        case .noListener, .unlisted: return "No Eavesdropper"
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

/// The seat partition, read the way the eavesdropper reads it.
struct ListenerPartition: Equatable {
    /// Where the file sits in a checkout.  The Mac's eavesdropper reads its own checkout's copy, and
    /// says which one in its status (`partition_file`), so that copy decides once it has said.
    static let relativePath = "docs/protocols/simple-tools-partition.toml"
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

    /// The copy in the checkout Muster runs the eavesdropper's command line from.
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

    /// Who holds a seat.  The eavesdropper upper-cases a section's name before it looks it up.
    ///
    /// A name that is not a seat name once upper-cased, like the `"MM"` of a quoted `[seat."MM"]`
    /// header, may be a seat the eavesdropper holds under the plain name.  TOML reads that header as
    /// seat MM, and Muster keeps the quotes, so it cannot call this one unlisted.
    func host(of seat: String) -> ListenerSeatHost {
        guard let seats else {
            return .unknown("The seat partition could not be read.\u{00A0} \(problem ?? "")")
        }
        guard Self.isSeatName(seat.uppercased()) else {
            return .unknown("The file names this seat in a way Muster cannot read.")
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
    /// partition gives to the Mac can still be one the Mac's eavesdropper will not run.  Its `INSTANCES`
    /// are "mac" and "server", and any other value is a seat problem, not a refusal.
    func host(of seat: String, in config: ListenerConfigDocument) -> ListenerSeatHost {
        let host = self.host(of: seat)
        guard host.isTheMac,
              case .text("server")? = config.value(section: "seat.\(seat)", key: "instance") else {
            return host
        }
        return .markedForTheServer
    }

    /// Whether a tag is one the eavesdropper accepts:  `_SEAT_RE` in `simple_tools/config.py`.
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

    /// The settings that are live caps.
    var isLiveCap: Bool {
        switch self {
        case .livePerHour, .livePerDay, .livePerTopicMinutes, .liveOwnerPerDay, .liveLoopTurns:
            return true
        default:
            return false
        }
    }

    /// Whether the setting means anything for a seat with this wake and fallback, by the rules
    /// `simple_tools/config.py` applies:  the muse binary only for a muse wake, the mcode binary only
    /// for a claude or muse wake, a fallback only for a wake that runs a turn, the container rule
    /// only for the mcode fallback, and no cap for an inbox seat, which never runs a wake.  A nil
    /// wake is the file's own default, inbox.
    func applies(toWake wake: String?, fallback: String?) -> Bool {
        let wake = wake ?? WakeType.inbox.rawValue
        switch self {
        case .wakeType:
            return true
        case .model:
            return ["claude", "muse", "openrouter"].contains(wake)
        case .musePath:
            return wake == "muse"
        case .mcodePath:
            return ["claude", "muse"].contains(wake)
        case .fallback:
            // `WORKER_WAKES` in `simple_tools/config.py`:  every wake that runs a turn, HTTP included.
            return ["claude", "http", "muse", "openrouter"].contains(wake)
        case .fallbackUncontained:
            return fallback == "mcode"
        case .hostLoadGate:
            return true
        default:
            return wake != WakeType.inbox.rawValue
        }
    }

    /// Whether the eavesdropper's own configuration enforces this setting.
    ///
    /// The dollar cap is read but never acted on:  the eavesdropper counts wakes and not
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
            return "The eavesdropper counts wakes and not dollars,  so this cap is never enforced and Muster will not write it."
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
    /// The eavesdropper is using the value the configuration file holds.
    case match
    /// The eavesdropper is using something else.
    case drift
    /// The setting is configured,  but nobody ever proved it works.
    case unproven
    /// The eavesdropper does not report this setting,  so the panel can only show the file.
    case notOwned
    /// The engine owns this setting,  but the eavesdropper does not publish it:  Muster can
    /// write it,  and cannot say whether it is in effect.
    case unpublished
    /// The file does not hold this setting,  so the live value is the default.
    case unset
    /// The setting is deliberately off,  by the owner's ruling.
    case off
    /// Something could not be read.
    case unknown
    /// The Mac's eavesdropper does not hold this seat,  so there is nothing to compare here.
    case heldElsewhere
    /// The eavesdropper says the file turns this seat off,  so it is not using this setting.
    case seatOff
    /// The eavesdropper's status is old, or it came from an eavesdropper that has stopped, so what
    /// it says it is using may no longer be true.
    case stale

    var label: String {
        switch self {
        // Not "PASS":  that word means every check a rule asks for was proven, and this
        // state means one thing only, that the eavesdropper is using the value the file holds.
        case .match: return "MATCHES"
        case .drift: return "DRIFT"
        case .unproven: return "UNPROVEN"
        case .notOwned: return "NOT ENFORCED"
        case .unpublished: return "NOT REPORTED"
        case .unset: return "DEFAULT"
        case .off: return "OFF"
        case .unknown: return "UNKNOWN"
        case .heldElsewhere: return "NOT ON MAC"
        case .seatOff: return "SEAT OFF"
        case .stale: return "NOT CURRENT"
        }
    }

    /// Whether the state is a problem the owner should see first.
    var isProblem: Bool {
        switch self {
        case .drift, .unproven, .unknown, .stale: return true
        case .match, .notOwned, .unpublished, .unset, .off, .heldElsewhere, .seatOff: return false
        }
    }
}

// MARK: - The eavesdropper itself

/// Whether the Mac's eavesdropper is running, as far as Muster can say.
///
/// `daemon status --json` answers with `running: false` when the eavesdropper is down, so a
/// failed read is not that answer:  it is a third state, and it is never shown as stopped.
enum ListenerRunState: Equatable {
    case running
    case stopped
    /// The status could not be read.  The text says why.
    case unknown(String)

    /// The line the panel shows.
    var title: String {
        switch self {
        case .running: return "The Mac's eavesdropper is running"
        case .stopped: return "The Mac's eavesdropper is not running"
        case .unknown: return "Muster could not check the Mac's eavesdropper"
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
    /// What the running eavesdropper is using.
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
    /// The runtime the pin is for:  "claude", "muse", "mcode", "openrouter" or
    /// "openrouter-fallback".
    let runtime: String
    /// The binary the pin proves.  An OpenRouter pin proves a model and a request, not a binary.
    let binary: String?
    /// The model the pin proved.
    let model: String?
    /// When the pin was made.
    let verifiedAt: Date?
    /// Whether the pin holds:  the eavesdropper's own verdict when it published one, and
    /// otherwise only that the file exists.
    let pinned: Bool
    /// The eavesdropper's own reason when the pin does not hold, such as `pin_stale`.
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

    /// The same evidence, with the eavesdropper's verdict on it.
    func judged(pinned: Bool, why: String?) -> PinEvidence {
        PinEvidence(runtime: runtime, binary: binary, model: model, verifiedAt: verifiedAt, pinned: pinned, why: why)
    }

    /// The runtime, in words.
    static func runtimeTitle(_ runtime: String) -> String {
        switch runtime {
        case "claude": return "Claude"
        case "muse": return "Muse"
        case "mcode": return "Mcode fallback"
        case "openrouter": return "OpenRouter"
        case "openrouter-fallback": return "OpenRouter fallback"
        default: return runtime
        }
    }

    /// The eavesdropper's reason a pin does not hold, in words.
    static func words(forWhy why: String) -> String {
        switch why {
        case "not_pinned": return "No passing test wake has been pinned yet."
        case "pin_stale": return "The pin is stale:  the binary, model or request moved since it was pinned.\u{00A0} Run a test wake, read it, then pin again."
        case "pin_unverified": return "The pin is from before the eavesdropper recorded what it proves, so it vouches for nothing about the current command.\u{00A0} Run a test wake, then pin again."
        case "pin_unreadable": return "The wake contract or schema could not be read, so no pin can be checked.\u{00A0} Pinning again will not fix this."
        case "no_claude": return "No claude binary was found to pin."
        case "no_muse": return "No muse binary was found to pin."
        default: return "The eavesdropper says the pin does not hold:  \(why.replacingOccurrences(of: "_", with: " "))."
        }
    }
}

/// What the panel can say about the pin a seat's own wake needs.
enum SeatPinState: Equatable {
    /// The wake runs nothing that a pin proves.  The text says why.
    case notNeeded(String)
    /// The pin holds.
    case pinned(PinEvidence)
    /// A pin file is there, and the eavesdropper says it does not hold.
    case notHolding(PinEvidence, why: String)
    /// A pin file is there, and no current word from the eavesdropper says whether it holds:  its
    /// status could not be read, is old, or carries no pin verdict for the seat.  A file that is
    /// there is never shown as a pin that holds.  The text says why nothing is known, and
    /// `needsLook` is false only when a current status reported the seat with no verdict.
    case fileOnly(PinEvidence, why: String, needsLook: Bool)
    /// No pin file, with the eavesdropper's reason when it gave one.
    case missing(runtime: String, why: String?)
    /// The pin file is there and could not be read.
    case unreadable(runtime: String, problem: String)
    /// The seat's wake is not known, so neither is the pin it needs.
    case unknownWake

    /// Whether the owner should look at this.
    var isProblem: Bool {
        switch self {
        case .notNeeded, .pinned: return false
        // A wake this app does not know leaves the pin it needs unknown, which the panel draws as
        // a problem, so the summary and the Problems Only filter count it as one too.
        case .notHolding, .missing, .unreadable, .unknownWake: return true
        case .fileOnly(_, _, let needsLook): return needsLook
        }
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
    /// True when the counts are a floor:  a row inside the window could not be read, or the read
    /// kept only the end of a ledger too large to read whole and that end does not reach back a day.
    var atLeast: Bool = false

    /// Total deferred / suppressed wakes in the last day.
    var deferredTotalThisDay: Int {
        heldBackThisDay + droppedThisDay + skippedThisDay + expiredThisDay
    }
}

/// What reading the kill switch file (`~/.simple-tools/listener/pause.json`) found.
enum WakePauseFile: Equatable {
    /// Muster did not read it.
    case notRead
    /// The file is not there.
    case missing
    /// The file's entries, by scope:  a seat tag, or `*` for every seat.
    case entries([String: ListenerStatus.PauseEntry])
    /// The file is there and could not be read.  The text says why.
    case unreadable(String)
}

/// Where the panel read the pause a seat is under.
enum WakePauseSource: Equatable {
    /// A current status from the running eavesdropper.
    case currentStatus
    /// The pause file itself, because the status is not current or could not be read.
    case pauseFile
    /// The last status, which is not current, because the pause file was not read.
    case lastStatus
    /// Nothing says whether a pause is in force.  The text says why.  The panel never shows this
    /// as no pause.
    case notKnown(String)

    /// Why the pause is not known, when it is not.
    var notKnownReason: String? {
        if case .notKnown(let why) = self { return why }
        return nil
    }
}

/// The pause a seat is under, and where the panel read it.
struct WakePauseFacts: Equatable {
    var paused: Bool = false
    var wakesPaused: Bool = false
    var scope: String?
    var by: String?
    var at: Date?
    var everySeat: Bool = false
    var source: WakePauseSource = .currentStatus
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
    /// Whether a pause on every seat is in force, whatever else names this seat.
    var everySeatPaused: Bool = false
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
    /// Why the eavesdropper would not run this seat,  when it would not.
    var skippedReasons: [String] = []
    /// Whether the file turns this seat off.
    var disabledInFile: Bool = false
    /// The wake word the eavesdropper published, as it wrote it, so a wake this app does not know
    /// is shown by its own name and never as no wake at all.
    var liveWakeText: String? = nil
    /// The pin the seat's own wake needs, and what is known about it.
    var pinState: SeatPinState = .unknownWake
    /// The fallback the file names, if any.
    var fallbackRuntime: String? = nil
    /// What is known about the fallback's own pin, or nil when the file names no fallback.  A pin
    /// file that is there and could not be read is unreadable, never missing.
    var fallbackPinState: SeatPinState? = nil
    /// When the last passing test wake left a pin candidate, by pin key.  The eavesdropper pins
    /// only a candidate under a day old.
    var candidates: [String: Date] = [:]
    /// Pin candidates that are there and could not be read, by pin key, with why.  Such a
    /// candidate is not known, and never the same as none.
    var unreadableCandidates: [String: String] = [:]
    /// Why the ledger could not be read, when it could not.  The usage is then not known, and is
    /// never shown as zero.
    var usageProblem: String? = nil
    /// Ledger rows inside the last day that could not be read.
    var tornRecentRows: Int = 0
    /// The claude binary the eavesdropper resolved for this seat.
    var claudeBinary: String? = nil
    /// The provider the seat talks to.
    var provider: String? = nil
    /// Whether an http seat's routine can be reached, and why not.
    var routine: ListenerStatus.Routine? = nil
    /// Why the quota is what it is, as the eavesdropper says.
    var quotaReason: String? = nil
    /// When the quota resets.
    var quotaResetAt: Date? = nil
    /// The held-back wakes by reason.
    var heldBackReasons: [String: Int] = [:]
    /// How old the oldest held-back wake is, in seconds.
    var heldBackOldestAge: Double? = nil
    /// Who set the pause that covers this seat, and when.
    var pauseBy: String? = nil
    var pauseAt: Date? = nil
    /// Where the pause facts came from.  Under a status that is not current they come from the
    /// pause file itself, and a file that cannot be read leaves them not known, never unpaused.
    var pauseSource: WakePauseSource = .currentStatus
    /// Whether this seat's live facts (held-back wakes, red warnings, chat nudges, quota and the
    /// fallback) come from a current status.  When they do not, they are the eavesdropper's last
    /// word as of `liveAsOf`, and the panel says so and never calls them "now".
    var liveIsCurrent: Bool = true
    /// When the status those live facts come from was published.
    var liveAsOf: Date? = nil
    /// Held-back wakes the eavesdropper lost past both its caps and still owes.
    var heldBackEvicted: Int = 0
    /// Held-back wakes now kept in the compact overflow.  They are still delivered by id.
    var heldBackOverflow: Int = 0
    /// Held-back wakes that expired, since the eavesdropper started counting.
    var heldBackExpiredTotal: Int = 0
    /// Held-back wakes lost past both caps, since the eavesdropper started counting.
    var heldBackEvictedTotal: Int = 0
    /// Whether the OpenRouter fallback is answering in the seat's place, because its own wake
    /// cannot run.
    var fallbackStandsIn: Bool = false
    /// Why the seat's own wake cannot run, as the eavesdropper says, while the fallback answers.
    var fallbackPrimaryHold: String? = nil

    var id: String { seat }

    /// Whether the fallback is answering for this seat, as a current status says.
    var fallbackIsAnswering: Bool {
        fallbackStandsIn && liveIsCurrent
    }

    /// The settings that are not a plain pass.
    var problems: [WakeSetting] {
        settings.filter { $0.state.isProblem }
    }

    /// The pin key of a test wake of the seat's own wake, or nil when there is nothing to test.
    /// It goes by the file, because `daemon test-wake` reads the seat's wake from the file.
    var testRuntime: String? {
        configuredWake?.testRuntime
    }

    /// The value a cap is held at:  the eavesdropper's when it published one, else the file's.
    func cap(_ key: WakeSettingKey) -> String? {
        guard let setting = settings.first(where: { $0.key == key }) else { return nil }
        return setting.live?.displayText ?? setting.configured?.displayText
    }

    /// Whether a passing test wake of this runtime left a candidate young enough to pin.
    func hasPinnableCandidate(_ pinKey: String, now: Date = Date()) -> Bool {
        guard let at = candidates[pinKey] else { return false }
        let age = now.timeIntervalSince(at)
        return age >= 0 && age <= 86_400
    }

    /// Whether the Mac's file holds a section the eavesdropper refuses to run with.
    var hasRefusedSection: Bool {
        host.isElsewhere && hasSection
    }

    /// What the panel says about such a section.
    var refusedSectionProblem: String? {
        guard hasRefusedSection else { return nil }
        let why = host == .markedForTheServer
            ? "The section says instance = \"server\""
            : "The seat partition does not give \(seat) to the Mac"
        return "The Mac's configuration file holds a [seat.\(seat)] section.\u{00A0} \(why), so the eavesdropper refuses every reload and will refuse its next start until the section is gone."
    }

    /// Whether the seat's settings are shown.  The owner's own choice holds once made, so an edit
    /// being typed, or the seat starting or stopping needing attention, never folds them away.
    /// Before any choice they are open when the seat needs attention or holds an edit.
    func settingsOpen(choice: Bool?, hasEdits: Bool) -> Bool {
        choice ?? (needsAttention || hasEdits)
    }

    /// Whether the seat belongs under the Problems Only filter.
    ///
    /// An exhausted quota is not one while a current status says the fallback answers in the
    /// seat's place:  its wakes are still answered.  Wakes lost past the caps and still owed are.
    var needsAttention: Bool {
        // In parts, so the type checker never meets one long chain.
        let settingsOrWarnings = !problems.isEmpty || !redWarnings.isEmpty
        let quota = quotaExhausted && !fallbackIsAnswering
        let section = hasRefusedSection || !skippedReasons.isEmpty
        let pinOrUse = pinState.isProblem || usageProblem != nil || tornRecentRows > 0
        let lost: Bool = heldBackEvicted > 0
        let pauseNotKnown: Bool = pauseSource.notKnownReason != nil
        let disconnected: Bool = connected == false
        return settingsOrWarnings || quota || section || pinOrUse || lost || pauseNotKnown || disconnected
    }
}

/// One fact the panel shows about a seat, and whether it is a problem.
struct WakeFact: Equatable {
    let text: String
    let emphasised: Bool
}

extension WakeSeat {
    /// What a live fact carries when the status it comes from is not current, and nothing when
    /// it is.  Such a fact is the eavesdropper's last word, so it never says "now".
    func notCurrentSuffix(now: Date = Date()) -> String {
        guard !liveIsCurrent else { return "" }
        if let at = liveAsOf {
            return ", as of \(FleetTime.when(at, now: now)), not current"
        }
        return ", not current"
    }

    /// The held-back parts of the seat's one-line summary:  the wakes held for later, and those
    /// lost past the caps and still owed.  Each is marked when the status is not current.
    var heldBackSummary: [String] {
        let mark = liveIsCurrent ? "" : " (not current)"
        var parts: [String] = []
        if heldBackQueueCount > 0 {
            parts.append("\(heldBackQueueCount) held for later" + mark)
        }
        if heldBackEvicted > 0 {
            let wakes = heldBackEvicted == 1 ? "1 held wake" : "\(heldBackEvicted) held wakes"
            parts.append("\(wakes) lost and still owed" + mark)
        }
        return parts
    }

    /// The pause the seat is under, in words:  what it stops, which entry, who set it and when,
    /// and where the panel read it.  Nil when no pause is in force and that is known.
    func pauseText(now: Date = Date()) -> String? {
        if let why = pauseSource.notKnownReason {
            return "Pause not known:  \(why)"
        }
        guard paused || wakesPaused else { return nil }
        let what = paused ? "Paused" : "Wakes Paused"
        let scope = pauseScope == "*" ? " by the pause on every seat" : ""
        let by = pauseBy.map { " by \($0)" } ?? ""
        let at = pauseAt.map { " at \(FleetTime.when($0, now: now))" } ?? ""
        let source: String
        switch pauseSource {
        case .currentStatus, .notKnown: source = ""
        case .pauseFile: source = ", from the pause file"
        case .lastStatus:
            source = liveAsOf.map { ", as of \(FleetTime.when($0, now: now)), not current" } ?? ", not current"
        }
        return "\(what)\(scope)\(by)\(at)\(source)"
    }

    /// The held-back wakes, the chat nudges, the quota and the fallback, as the eavesdropper
    /// published them.  Under a status that is not current each one says so, and none says "now".
    func liveFacts(now: Date = Date()) -> [WakeFact] {
        var facts: [WakeFact] = []
        let suffix = notCurrentSuffix(now: now)
        let held = liveIsCurrent ? "held for later now" : "held for later"
        if heldBackQueueCount > 0 || heldBackTotalCount > 0 || heldBackEvicted > 0 {
            let reasons = heldBackReasons.sorted { $0.key < $1.key }
                .map { "\($0.value) \(WakeLedger.words(forReason: $0.key))" }
                .joined(separator: ", ")
            let oldest = heldBackOldestAge.map { ", oldest \(FleetTime.age(Int($0)))" } ?? ""
            let why = reasons.isEmpty ? "" : " (\(reasons))"
            let overflow = heldBackOverflow > 0 ? ", \(heldBackOverflow) of them in the overflow and still owed by id" : ""
            facts.append(WakeFact(
                text: "\(held):  \(heldBackQueueCount)\(why)\(oldest)\(overflow)\(suffix)",
                emphasised: heldBackQueueCount > 0
            ))
            var lifetime = "held since start:  \(heldBackTotalCount), \(heldBackSurfacedCount) shown to the seat"
            if heldBackExpiredTotal > 0 { lifetime += ", \(heldBackExpiredTotal) expired" }
            if heldBackEvictedTotal > 0 { lifetime += ", \(heldBackEvictedTotal) lost past the caps" }
            facts.append(WakeFact(text: lifetime + suffix, emphasised: false))
        }
        if heldBackEvicted > 0 {
            let wakes = heldBackEvicted == 1 ? "1 held wake" : "\(heldBackEvicted) held wakes"
            facts.append(WakeFact(
                text: "\(wakes) lost past the caps and still owed\(suffix)",
                emphasised: true
            ))
        }
        if chatNudgesPending > 0 || chatNudgesBlocked > 0 || chatNudgesAccepted > 0 {
            facts.append(WakeFact(
                text: "chat nudges:  \(chatNudgesPending) pending, \(chatNudgesBlocked) blocked, \(chatNudgesAccepted) accepted\(suffix)",
                emphasised: chatNudgesBlocked > 0
            ))
        }
        if quotaRemaining != nil || quotaExhausted {
            var text = quotaExhausted ? "quota exhausted" : "quota \(Int(quotaRemaining ?? 0))% left"
            if quotaExhausted, let remaining = quotaRemaining {
                text += " (\(Int(remaining))% reported)"
            }
            if let source = quotaSource { text += ", from \(source)" }
            if let reason = quotaReason, !reason.isEmpty { text += ":  \(reason)" }
            if let reset = quotaResetAt { text += ", resets \(FleetTime.when(reset, now: now))" }
            facts.append(WakeFact(text: text + suffix, emphasised: quotaExhausted && !fallbackIsAnswering))
        }
        if fallbackStandsIn {
            let verb = liveIsCurrent ? "is answering" : "was answering"
            var text = "the OpenRouter fallback \(verb) in this seat's place"
            if let hold = fallbackPrimaryHold, !hold.isEmpty { text += ":  \(hold)" }
            facts.append(WakeFact(text: text + suffix, emphasised: false))
        }
        return facts
    }
}

/// Everything the panel shows.
struct WakeSettingsModel: Equatable {
    /// How long a published status stays current.  The same rule as Seat Activity's, so the two
    /// panels never disagree about one file:  an eavesdropper publishes every few seconds.
    static let maximumStatusAge: TimeInterval = SeatActivityReader.defaultMaximumListenerAge

    /// Whether the eavesdropper is running.
    var running: Bool = false
    /// Whether it is running, stopped, or could not be checked.
    var runState: ListenerRunState = .unknown("Muster has not read the eavesdropper yet.")
    /// The pid of the running eavesdropper.
    var pid: Int?
    /// When the eavesdropper last published its state.
    var updated: Date?
    /// When Muster read it.
    var readAt: Date?
    /// Whether what the eavesdropper published is current:  it is running and published within
    /// `maximumStatusAge` of the read.
    var statusIsCurrent: Bool = false
    /// The Mac seats that report a connection, and how many of them are connected.
    var connectedSeats: Int = 0
    var reportingSeats: Int = 0
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
    /// What the Mac's eavesdropper itself reports is wrong with its configuration.
    var listenerErrors: [String] = []
    /// Whether the Operator can post (`status.operator_voice`),  from the same status as the
    /// rest of the panel and judged by the same currency.
    var operatorVoice: OperatorVoiceReading = .notRead("Muster has not read the eavesdropper yet.")
    /// Whether resume-wake is verified on this Mac, from the eavesdropper's status.  Nil unless
    /// that status is current:  a stopped eavesdropper, or an old status, cannot say what resumes
    /// a session now, as Seat Activity rules too.
    var rewake: RewakeVerification?
    /// The instance the status names, when it is not the Mac's.
    var foreignInstance: String?

    /// Why the header calls a running eavesdropper's status not current, and nil when it is
    /// current or the eavesdropper is not running.  A status another instance wrote is named as
    /// that, since it can be fresh and still not be the Mac's.
    var notCurrentReason: String? {
        guard runState == .running, !statusIsCurrent else { return nil }
        if let instance = foreignInstance {
            return "Not current:  the eavesdropper on this machine says it is the \(instance) instance, not the Mac's, so its values here are not what the Mac's eavesdropper uses."
        }
        return "Not current:  a running eavesdropper publishes every few seconds, so its values here may not be what it uses now."
    }

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
    /// Where that partition was read from:  the eavesdropper's own copy once it has named one.
    var partitionURL: URL?
    /// The eavesdropper's own published state,  when it could be read.
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
    /// When the read was made.  Nil leaves the status's age unjudged.
    var readAt: Date?
    /// Why the eavesdropper's status could not be read, when it could not.
    var statusProblem: String?
    /// Why the configuration file was not read, when it was not.
    var configProblem: String?
    /// When each seat's pin candidates were left:  seat -> pin key -> time.
    var candidates: [String: [String: Date]] = [:]
    /// Pin candidates that are there and could not be read:  seat -> pin key -> why.
    var unreadableCandidates: [String: [String: String]] = [:]
    /// Pin files that are there and could not be read:  seat -> pin key -> why.
    var unreadablePins: [String: [String: String]] = [:]
    /// Ledgers that are there and could not be read:  seat -> why.
    var usageProblems: [String: String] = [:]
    /// Ledger rows inside the last day that could not be read, by seat.
    var tornRows: [String: Int] = [:]
    /// The kill switch file, read so the pause is known when the status is not current.
    var pauseFile: WakePauseFile = .notRead
}

// MARK: - The builder

/// Turns what was read into what the panel shows.
enum WakeSettingsBuilder {
    /// Whether what a status says is current:  a running eavesdropper that published within the
    /// limit of the read.  A stopped one's file is its last word, not what anything is using.
    /// A status another instance wrote is not the Mac's at all, as Seat Activity rules too.
    /// Without a read time the age is not judged.
    static func statusIsCurrent(_ sources: WakeSettingsSources) -> Bool {
        guard let status = sources.status, status.running == true else { return false }
        if let instance = status.status?.instance, instance != ListenerPartition.thisInstance {
            return false
        }
        guard let readAt = sources.readAt else { return true }
        guard let updated = status.updated else { return false }
        return abs(readAt.timeIntervalSince(updated)) <= WakeSettingsModel.maximumStatusAge
    }

    static func build(_ sources: WakeSettingsSources) -> WakeSettingsModel {
        var model = WakeSettingsModel()
        model.running = sources.status?.running ?? false
        if let status = sources.status {
            // The status is read field by field, so `running` can be missing or of another type.
            // Then Muster does not know, and never says the eavesdropper has stopped.
            switch status.running {
            case true?: model.runState = .running
            case false?: model.runState = .stopped
            case nil: model.runState = .unknown("The eavesdropper's status did not say whether it is running.")
            }
        } else {
            model.runState = .unknown(
                sources.statusProblem ?? "The eavesdropper's status could not be read."
            )
        }
        model.pid = sources.status?.pid
        model.updated = sources.status?.updated
        model.readAt = sources.readAt
        model.statusIsCurrent = statusIsCurrent(sources)
        model.configPath = sources.configPath
        model.operatorOptions = sources.operatorOptions
        model.problems = sources.problems + sources.config.problems
        model.listenerErrors = sources.status?.status?.config_errors ?? []
        model.seats = sources.seats.map { seat(for: $0, sources: sources) }
        if let why = sources.partition.problem {
            model.problems.append("The seat partition could not be read, so every seat is read only.\u{00A0} \(why)")
        }
        model.problems.append(contentsOf: model.seats.compactMap(\.refusedSectionProblem))
        var foreignInstance: String?
        if let instance = sources.status?.status?.instance, instance != ListenerPartition.thisInstance {
            foreignInstance = instance
            model.problems.append("The eavesdropper on this machine says it is the \(instance) instance, not the Mac's, so nothing it says is shown as current.")
        }
        // The pause file decides the pause when the status is not current, so a file that could
        // not be read is named once, with why.
        if !model.statusIsCurrent, case .unreadable(let why) = sources.pauseFile {
            model.problems.append("Whether a pause is in force is not known, because the eavesdropper's status is not current and the pause file could not be read.\u{00A0} \(why)")
        }
        let reporting = model.seats.filter { !$0.host.isElsewhere && $0.connected != nil }
        model.reportingSeats = reporting.count
        model.connectedSeats = reporting.filter { $0.connected == true }.count
        model.daemonSettings = daemonSettings(sources)
        model.operatorVoice = OperatorVoiceReading.current(
            sources.status, runState: model.runState, statusIsCurrent: model.statusIsCurrent)
        model.foreignInstance = foreignInstance
        // Resume-wake is a claim about what the Mac's eavesdropper does now, so only a current
        // status makes it.  `statusIsCurrent` already rules out another instance's status.
        if model.statusIsCurrent {
            model.rewake = sources.status?.status?.rewake
        }
        return model
    }

    /// One seat's rows.
    private static func seat(for tag: String, sources: WakeSettingsSources) -> WakeSeat {
        let host = host(of: tag, sources: sources)
        // The eavesdropper upper-cases a section's name, and publishes every seat under that name, so
        // a `[seat.mm]` section is looked up as MM.
        let published = tag.uppercased()
        // The Mac's eavesdropper and the Mac's ledgers say nothing about a seat another eavesdropper holds,
        // so nothing from them is shown for it:  no zero, no default and no pause.
        let live = host.isElsewhere ? nil : sources.status?.seat(published)
        let pins: [String: PinEvidence] = host.isElsewhere ? [:] : (sources.pins[tag] ?? [:])
        // A seat the file turns off is reported under `disabled` and not under `seats`, so a running
        // eavesdropper has told Muster about it.  An old status from a stopped one has not, and nor
        // has one another instance wrote:  its list is about its own file, not the Mac's.
        let statusIsTheMacs = sources.status?.status?.instance.map { $0 == ListenerPartition.thisInstance } ?? true
        let seatOff = sources.status?.running == true
            && statusIsTheMacs
            && sources.status?.status?.disabled?.contains(published) == true
        let section = "seat.\(tag)"
        let current = statusIsCurrent(sources)
        let staleText = Self.staleNote(sources)
        let configuredWakeText = sources.config.value(section: section, key: "wake")?.scalarText
        let hasSection = !sources.config.sections(ofSeat: tag).isEmpty
        // The wake the seat runs:  the eavesdropper's word when it published one, else the file's.
        let wake = live?.wake ?? configuredWakeText
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
            // A Mac seat the file has no section for is one the Mac's eavesdropper does not run, so
            // no default is in effect for it either.
            if !hasSection, live == nil, sources.configProblem == nil {
                return WakeSetting(
                    seat: tag, key: key, configured: nil, live: nil, state: .seatOff,
                    note: "The file has no section for this seat, so the Mac's eavesdropper does not run it."
                )
            }
            let row = setting(
                for: key, seat: tag, section: section, wake: wake, live: live,
                seatOff: seatOff, pins: pins, config: sources.config
            )
            // A file that was not read says nothing about what it holds, so no row is DEFAULT,
            // PASS or DRIFT for want of a read:  it is not known, beside whatever the eavesdropper
            // reports.  The problem box above the seats says why the file was not read.
            if sources.configProblem != nil {
                return WakeSetting(
                    seat: tag, key: key, configured: nil, live: row.live, state: .unknown,
                    note: "The configuration file was not read, so what it holds for this setting is not known."
                )
            }
            // A pass or a drift is a claim about what a running eavesdropper uses now.  An old
            // status, or one a stopped eavesdropper left, cannot make it.  A row whose value Muster
            // read from a pin file itself is not the eavesdropper's word, so the status's age says
            // nothing about it.
            guard !current, sources.status != nil, row.state == .match || row.state == .drift,
                  Self.liveComesFromStatus(
                    key, wake: wake,
                    fallback: sources.config.value(section: section, key: "fallback")?.scalarText
                  ) else {
                return row
            }
            return WakeSetting(
                seat: row.seat, key: row.key, configured: row.configured, live: row.live,
                state: .stale, note: staleText
            )
        }
        let fallback = sources.config.value(section: section, key: "fallback")?.scalarText
        let seatPin: SeatPinState
        if host.isElsewhere {
            seatPin = .notNeeded(host.readOnlyReason ?? "")
        } else {
            seatPin = Self.pinState(
                wake: wake, live: live, pins: pins,
                unreadable: sources.unreadablePins[tag] ?? [:],
                statusIsCurrent: current,
                statusRead: sources.status != nil
            )
        }
        let ownPin: PinEvidence?
        switch seatPin {
        case .pinned(let evidence), .notHolding(let evidence, _), .fileOnly(let evidence, _, _): ownPin = evidence
        default: ownPin = nil
        }
        let fallbackPinState = host.isElsewhere
            ? nil
            : Self.fallbackPinState(
                fallback: fallback, live: live, pins: pins,
                unreadable: sources.unreadablePins[tag] ?? [:],
                statusIsCurrent: current,
                statusRead: sources.status != nil
            )
        let pause = host.isElsewhere
            ? WakePauseFacts()
            : pauseFacts(seat: published, live: live, sources: sources, current: current)
        let heldBack = live?.heldBack
        // The fallback answers in the seat's place only while its own wake cannot run, and the
        // eavesdropper then leaves the quota out of the seat's red warnings on purpose.
        let fallbackStandsIn = live?.fallback?.stands_in == true
        return WakeSeat(
            seat: tag,
            host: host,
            hasSection: hasSection,
            bot: live?.bot ?? sources.config.value(section: section, key: "bot")?.scalarText,
            configuredWake: configuredWakeText.flatMap(WakeType.init(rawValue:)),
            liveWake: live?.wake.flatMap(WakeType.init(rawValue:)),
            settings: settings,
            usage: host.isElsewhere ? WakeUsage() : (sources.usage[tag] ?? WakeUsage()),
            recentWakes: host.isElsewhere ? [] : (sources.recentWakes[tag] ?? []),
            pin: ownPin,
            fallbackPin: fallbackPin(fallback: fallback, live: live, pins: pins),
            paused: pause.paused,
            wakesPaused: pause.wakesPaused,
            pauseScope: pause.scope,
            // A last-known value is not a current success:  an old status, or one a stopped
            // eavesdropper left, does not say the seat is connected now.
            connected: current ? live?.connected : nil,
            everySeatPaused: pause.everySeat,
            redWarnings: live?.red ?? [],
            heldBackQueueCount: heldBack?.count ?? 0,
            heldBackTotalCount: heldBack?.totals?.held ?? 0,
            heldBackSurfacedCount: heldBack?.totals?.surfaced ?? 0,
            chatNudgesPending: live?.chatNudges?.pending ?? 0,
            chatNudgesBlocked: live?.chatNudges?.blocked ?? 0,
            chatNudgesAccepted: live?.chatNudges?.accepted ?? 0,
            quotaRemaining: (live?.quota?.known == true) ? live?.quota?.remaining : nil,
            quotaExhausted: live?.quota?.exhausted ?? false,
            quotaSource: live?.quota?.source,
            skippedReasons: host.isElsewhere ? [] : (sources.status?.status?.skipped_seats?[published] ?? []),
            // As for the settings rows' SEAT OFF:  only a running eavesdropper has told Muster the
            // file turns the seat off.  A stopped one's old list is not that word.
            disabledInFile: seatOff && !host.isElsewhere,
            liveWakeText: live?.wake,
            pinState: seatPin,
            fallbackRuntime: fallback,
            fallbackPinState: fallbackPinState,
            candidates: host.isElsewhere ? [:] : (sources.candidates[tag] ?? [:]),
            unreadableCandidates: host.isElsewhere ? [:] : (sources.unreadableCandidates[tag] ?? [:]),
            usageProblem: host.isElsewhere ? nil : sources.usageProblems[tag],
            tornRecentRows: host.isElsewhere ? 0 : (sources.tornRows[tag] ?? 0),
            claudeBinary: live?.claude,
            provider: live?.provider,
            routine: live?.routine,
            quotaReason: live?.quota?.reason,
            quotaResetAt: live?.quota?.reset_at.flatMap(FleetTime.parse),
            heldBackReasons: heldBack?.reasons ?? [:],
            heldBackOldestAge: heldBack?.oldest_age,
            pauseBy: pause.by,
            pauseAt: pause.at,
            pauseSource: pause.source,
            liveIsCurrent: live == nil || current,
            liveAsOf: live == nil ? nil : sources.status?.updated,
            heldBackEvicted: heldBack?.evicted ?? 0,
            heldBackOverflow: heldBack?.overflow ?? 0,
            heldBackExpiredTotal: heldBack?.totals?.expired ?? 0,
            heldBackEvictedTotal: heldBack?.totals?.evicted ?? 0,
            fallbackStandsIn: fallbackStandsIn,
            fallbackPrimaryHold: fallbackStandsIn ? live?.fallback?.primary_hold : nil
        )
    }

    /// The pause a seat is under, and where it was read.
    ///
    /// A current status says it.  Otherwise the pause file itself does, because the status is old,
    /// a stopped eavesdropper's last word, or not read at all.  A pause file that is not there is
    /// no pause, as the eavesdropper reads it.  Only when the file was not read is the last
    /// status all there is, and the panel then says it is not current.  A pause file that cannot
    /// be read leaves the pause not known:  never no pause.  Whichever source is used,
    /// the entry named is the one that has the effect, by the rule Seat Activity uses too.
    static func pauseFacts(
        seat published: String,
        live: ListenerStatus.Seat?,
        sources: WakeSettingsSources,
        current: Bool
    ) -> WakePauseFacts {
        func fromStatus(_ status: ListenerStatus, source: WakePauseSource) -> WakePauseFacts {
            let covering = SeatActivityBuilder.coveringPause(
                for: published, in: status.pause, wakesOnly: { $0.wakes_only }
            )
            return WakePauseFacts(
                paused: live?.paused ?? false,
                wakesPaused: live?.wakesPaused ?? false,
                scope: covering?.scope,
                by: covering?.entry.by,
                at: covering?.entry.ts.map { Date(timeIntervalSince1970: $0) },
                everySeat: status.pause["*"] != nil,
                source: source
            )
        }
        if current, let status = sources.status {
            return fromStatus(status, source: .currentStatus)
        }
        switch sources.pauseFile {
        case .entries(let entries):
            let covering = SeatActivityBuilder.coveringPause(
                for: published, in: entries, wakesOnly: { $0.wakes_only }
            )
            return WakePauseFacts(
                paused: covering?.kind == .full,
                // The eavesdropper's own `wakes_paused` is true under either kind of pause.
                wakesPaused: covering != nil,
                scope: covering?.scope,
                by: covering?.entry.by,
                at: covering?.entry.ts.map { Date(timeIntervalSince1970: $0) },
                everySeat: entries["*"] != nil,
                source: .pauseFile
            )
        case .unreadable(let why):
            return WakePauseFacts(source: .notKnown(why))
        case .missing:
            // No pause file is no pause, as the eavesdropper reads it, whatever an old status
            // still lists:  the file was read, and it decides.
            return WakePauseFacts(source: .pauseFile)
        case .notRead:
            if let status = sources.status {
                return fromStatus(status, source: .lastStatus)
            }
            return WakePauseFacts(source: .notKnown("Neither the eavesdropper's status nor the pause file was read."))
        }
    }

    /// What a seat's status says when it is not current.
    private static func staleNote(_ sources: WakeSettingsSources) -> String {
        switch sources.status?.running {
        case true?:
            break
        case false?:
            return "The eavesdropper is not running, so this is the last value it published, not one it uses now."
        case nil:
            return "The eavesdropper's status does not say whether it is running, so this may not be what it uses now."
        }
        if let instance = sources.status?.status?.instance, instance != ListenerPartition.thisInstance {
            return "The eavesdropper on this machine says it is the \(instance) instance, not the Mac's, so this is not what the Mac's eavesdropper uses."
        }
        guard let updated = sources.status?.updated, let readAt = sources.readAt else {
            return "The eavesdropper's status does not say when it was written, so this may not be current."
        }
        let age = SeatActivityReader.ageText(readAt.timeIntervalSince(updated))
        return "The eavesdropper last published \(age), so this may not be what it uses now."
    }

    /// Whether a row's live value is the eavesdropper's own word, rather than one Muster read from
    /// a pin file:  the muse model, the mcode binary and a non-OpenRouter fallback come from pin
    /// files.  An OpenRouter fallback's verdict is the eavesdropper's published one, so it is not
    /// a pin-file row.
    static func liveComesFromStatus(_ key: WakeSettingKey, wake: String?, fallback: String? = nil) -> Bool {
        switch key {
        case .mcodePath: return false
        case .fallback: return fallback == "openrouter"
        case .model: return wake != "muse"
        default: return true
        }
    }

    /// The pin a seat's own wake needs, judged by the eavesdropper's own current verdict.
    ///
    /// The pin is chosen by the seat's wake and nothing else:  a leftover muse pin on a Claude
    /// seat proves nothing about it, and the mcode fallback's pin is not the seat's own.  A pin
    /// file with no current verdict on it is only a file:  it is never called a pin that holds.
    static func pinState(
        wake: String?,
        live: ListenerStatus.Seat?,
        pins: [String: PinEvidence],
        unreadable: [String: String],
        statusIsCurrent: Bool = true,
        statusRead: Bool = true
    ) -> SeatPinState {
        let type = WakeType(rawValue: wake ?? WakeType.inbox.rawValue)
        guard let type else { return .unknownWake }
        guard let key = type.pinKey else { return .notNeeded(type.noPinReason ?? "") }
        if let problem = unreadable[key] {
            return .unreadable(runtime: key, problem: problem)
        }
        guard let evidence = pins[key] else {
            return .missing(runtime: key, why: live?.pin?.why)
        }
        guard statusRead else {
            return .fileOnly(evidence, why: "Muster could not read the eavesdropper's status, so it cannot say whether the pin holds.", needsLook: true)
        }
        guard statusIsCurrent else {
            return .fileOnly(evidence, why: "The eavesdropper's status is not current, so it cannot say whether the pin holds now.", needsLook: true)
        }
        guard let live else {
            return .fileOnly(evidence, why: "The eavesdropper did not report this seat, so it did not say whether the pin holds.", needsLook: true)
        }
        guard let verdict = live.pin, verdict.pinned != nil || verdict.why != nil else {
            return .fileOnly(evidence, why: "The eavesdropper did not say whether it holds.", needsLook: false)
        }
        if verdict.pinned == true {
            return .pinned(evidence.judged(pinned: true, why: nil))
        }
        let why = verdict.why ?? "not_pinned"
        return .notHolding(evidence.judged(pinned: false, why: why), why: why)
    }

    /// Who holds a seat, as far as Muster can say.
    ///
    /// An eavesdropper from before `partition_file` does not say which partition it reads, so the copy
    /// Muster read may not be its copy.  If that eavesdropper is running a seat this copy gives away,
    /// the two differ, and calling the seat elsewhere would offer to delete a live seat's section.
    /// A seat it skipped or that the file turns off counts too:  its partition check took those.
    ///
    /// With no status at all, Muster does not know whether an eavesdropper is running or which copy
    /// it reads, so its own copy cannot call a seat elsewhere either.  `daemon status --json` answers
    /// with an object when the eavesdropper is stopped, so no status only means the command failed.
    static func host(of tag: String, sources: WakeSettingsSources) -> ListenerSeatHost {
        let host = sources.partition.host(of: tag, in: sources.config)
        if host.isElsewhere, sources.status == nil {
            return .unknown("Muster could not read the eavesdropper's status, so it cannot say which partition the eavesdropper reads.")
        }
        let published = tag.uppercased()
        guard host.isElsewhere,
              sources.status?.running == true,
              let state = sources.status?.status,
              state.partition_file == nil,
              state.seats?[published] != nil
                || state.disabled?.contains(published) == true
                || state.skipped_seats?[published] != nil else {
            return host
        }
        return .unknown("The running eavesdropper holds this seat, and the copy Muster read gives it elsewhere.")
    }

    /// One row,  with its state decided.
    private static func setting(
        for key: WakeSettingKey,
        seat: String,
        section: String,
        wake: String?,
        live: ListenerStatus.Seat?,
        seatOff: Bool,
        pins: [String: PinEvidence],
        config: ListenerConfigDocument
    ) -> WakeSetting {
        let location = key.configLocation
        let configured = config.value(section: section, key: location.key, subPath: location.subPath)
        let fallback = fallbackRuntime(section: section, config: config)
        let liveValue = liveValue(for: key, wake: wake, fallback: fallback, live: live, pins: pins)
        let state = state(
            for: key,
            wake: wake,
            fallback: fallback,
            configured: configured,
            live: liveValue,
            seatReported: live != nil,
            seatOff: seatOff,
            publishedFallback: live?.fallback,
            publishedPin: live?.pin,
            pins: pins
        )
        return WakeSetting(
            seat: seat,
            key: key,
            configured: configured,
            live: liveValue,
            state: state,
            note: note(
                for: key, state: state, wake: wake, fallback: fallback,
                configured: configured, live: liveValue,
                publishedFallback: live?.fallback, publishedPin: live?.pin, pins: pins
            )
        )
    }

    /// The value the running eavesdropper is using.
    private static func liveValue(
        for key: WakeSettingKey,
        wake: String?,
        fallback: String?,
        live: ListenerStatus.Seat?,
        pins: [String: PinEvidence]
    ) -> ListenerConfigValue? {
        switch key {
        case .wakeType:
            return live?.wake.map { .text($0) }
        case .model:
            // Only the seat's own runtime can say which model it runs.  An OpenRouter wake's model
            // is published by the eavesdropper;  a muse wake's is the one its pin proved;  nothing
            // reports a Claude wake's model.  The mcode fallback's model is the fallback's, never
            // the seat's.
            switch wake {
            case "muse": return pins["muse"]?.model.map { .text($0) }
            case "openrouter": return live?.model.map { .text($0) }
            default: return nil
            }
        case .musePath:
            return live?.muse.map { .text($0) }
        case .mcodePath:
            return pins["mcode"]?.binary.map { .text($0) }
        case .fallback:
            // Only an OpenRouter fallback has a verdict the eavesdropper publishes.  Any other
            // fallback's pin file is one Muster reads itself, so it is in the note, not in the
            // eavesdropper's column.
            guard fallback == "openrouter", let pinned = live?.fallback?.pinned else { return nil }
            return pinned ? .text("pinned") : nil
        case .fallbackUncontained:
            // The eavesdropper does not publish this,  so the panel has nothing live to compare.
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
    ///   * the eavesdropper says the file turns this seat off:  it is using nothing,  and that is the
    ///     file's own choice,  not a problem
    ///   * the eavesdropper reported nothing for this seat:  what it is using is unknown,  and that is
    ///     a problem,  because its status could not be read or it is not running the seat
    ///   * the eavesdropper reported the seat and not this setting:  it does not publish it
    ///   * the two agree:  a pass
    ///   * the two differ:  DRIFT
    ///
    /// A configured fallback with no pin is never a pass:  unless the seat is off,  it is
    /// unproven,  because nobody ever showed that it works.  A model is compared only with what
    /// the seat's own runtime reports, and a runtime that reports none leaves it not reported.
    private static func state(
        for key: WakeSettingKey,
        wake: String?,
        fallback: String?,
        configured: ListenerConfigValue?,
        live: ListenerConfigValue?,
        seatReported: Bool,
        seatOff: Bool,
        publishedFallback: ListenerStatus.Fallback?,
        publishedPin: ListenerStatus.Pin? = nil,
        pins: [String: PinEvidence]
    ) -> WakeSettingState {
        // A setting the engine does not enforce is never a pass,  whatever the two values
        // say:  the number in the file would not bind anything.
        if !key.isEnforced {
            return .notOwned
        }
        if seatOff, configured != nil, live == nil {
            return .seatOff
        }
        switch key {
        case .model:
            guard configured != nil else { return .unset }
            switch wake {
            case "muse":
                guard let pin = pins["muse"] else { return .unproven }
                // The eavesdropper says the muse pin no longer holds, so the model it recorded
                // proves nothing now, whatever it says.
                if publishedPin?.pinned == false { return .unproven }
                guard pin.model != nil else { return .unpublished }
            case "openrouter":
                guard live != nil else { return seatReported ? .unpublished : .unknown }
            default:
                return seatReported || wake != nil ? .unpublished : .unknown
            }
        case .fallback:
            guard let fallback else { return configured == nil ? .unset : .unproven }
            if fallback == "openrouter", let pinned = publishedFallback?.pinned {
                return pinned ? .match : .unproven
            }
            // A pin file the eavesdropper gives no verdict on proves only that it is there:  the
            // eavesdropper warns about a stale one elsewhere, so it is never a pass here.
            return pins[WakeType.fallbackPinKey(fallback)] == nil ? .unproven : .unpublished
        case .mcodePath:
            guard configured != nil else { return .unset }
            guard live != nil else { return .unproven }
        case .fallbackUncontained:
            // The engine reads this one,  but the eavesdropper does not publish it.
            return configured == nil ? .unset : .unpublished
        default:
            break
        }
        // The file first:  a setting nobody holds is the eavesdropper's default,  and a
        // setting only the file holds is one the eavesdropper does not publish.
        guard let configured else { return .unset }
        guard let live else {
            return seatReported ? .unpublished : .unknown
        }
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
        wake: String?,
        fallback: String?,
        configured: ListenerConfigValue?,
        live: ListenerConfigValue?,
        publishedFallback: ListenerStatus.Fallback?,
        publishedPin: ListenerStatus.Pin? = nil,
        pins: [String: PinEvidence]
    ) -> String {
        let file = configured?.displayText ?? "nothing"
        switch state {
        case .drift:
            if key == .model, wake == "muse" {
                return "The muse pin proved \(live?.displayText ?? "another model"), and the file holds \(file).\u{00A0} Run a test wake and pin it to prove this model."
            }
            return "The eavesdropper is using \(live?.displayText ?? "something else"),  and the file holds \(file)."
        case .unproven:
            if key == .model {
                if wake == "muse", pins["muse"] != nil, publishedPin?.pinned == false {
                    return "The eavesdropper says the muse pin does not hold, so it proves no model now.\u{00A0} \(PinEvidence.words(forWhy: publishedPin?.why ?? "not_pinned"))"
                }
                return "No muse pin proves this model yet."
            }
            if key == .fallback {
                if fallback == "openrouter", let why = publishedFallback?.pin?.why {
                    return PinEvidence.words(forWhy: why)
                }
                return "This fallback is configured, and no pin has proved it works.\u{00A0} Test the fallback, read the result, then pin it."
            }
            return "This is configured,  but no pin has ever proved it works."
        case .notOwned:
            if let reason = key.refusalReason { return reason }
            return "The eavesdropper does not enforce this setting."
        case .unpublished:
            if key == .model {
                let runtime = WakeType(rawValue: wake ?? "")?.title ?? "this"
                return "Neither the eavesdropper nor the \(runtime) pin reports the model, so only the file's value can be shown."
            }
            if key == .fallbackUncontained {
                return "The eavesdropper reads this one,  but does not publish it,  so only the file can be shown."
            }
            if key == .fallback {
                let title = fallback.map { PinEvidence.runtimeTitle(WakeType.fallbackPinKey($0)) } ?? "fallback"
                let age = fallback.flatMap { pins[WakeType.fallbackPinKey($0)]?.age }.map { ", \($0)" } ?? ""
                return "The \(title) pin file is there\(age).\u{00A0} The eavesdropper does not publish whether it still holds, so a stale one shows only in its warnings."
            }
            return "The eavesdropper reports this seat and does not publish this setting, so only the file's value can be shown."
        case .unset:
            return "The file holds nothing,  so the eavesdropper's default is in effect."
        case .off:
            return "Off:  the eavesdropper never holds a wake for load."
        case .unknown:
            return "The eavesdropper did not report this seat, so what it is using is not known."
        case .heldElsewhere:
            return "The Mac's eavesdropper does not hold this seat."
        case .seatOff:
            return "The file turns this seat off, so the eavesdropper is not using this setting."
        case .stale:
            return "The eavesdropper's status is not current, so this may not be what it uses now."
        case .match:
            if key == .model, wake == "muse", let age = pins["muse"]?.age {
                return "Proved by the muse pin,  \(age)."
            }
            if key == .fallback {
                return "The eavesdropper says the OpenRouter fallback's pin holds."
            }
            return ""
        }
    }

    /// The runtime the file names as the seat's fallback.
    private static func fallbackRuntime(section: String, config: ListenerConfigDocument) -> String? {
        config.value(section: section, key: "fallback")?.scalarText
    }

    /// The pin for the fallback the file names, or nil when the file names none.
    private static func fallbackPin(
        fallback: String?,
        live: ListenerStatus.Seat?,
        pins: [String: PinEvidence]
    ) -> PinEvidence? {
        guard let fallback, let evidence = pins[WakeType.fallbackPinKey(fallback)] else { return nil }
        if fallback == "openrouter", let pinned = live?.fallback?.pinned {
            return evidence.judged(pinned: pinned, why: pinned ? nil : live?.fallback?.pin?.why)
        }
        return evidence
    }

    /// What is known about the fallback's own pin.
    ///
    /// Only an OpenRouter fallback has a verdict the eavesdropper publishes.  Any other fallback's
    /// pin file proves only that it is there:  the eavesdropper warns about a stale one elsewhere,
    /// so it is never shown as a pin that holds.
    static func fallbackPinState(
        fallback: String?,
        live: ListenerStatus.Seat?,
        pins: [String: PinEvidence],
        unreadable: [String: String],
        statusIsCurrent: Bool = true,
        statusRead: Bool = true
    ) -> SeatPinState? {
        guard let fallback else { return nil }
        let key = WakeType.fallbackPinKey(fallback)
        if let problem = unreadable[key] {
            return .unreadable(runtime: key, problem: problem)
        }
        let isOpenRouter = fallback == "openrouter"
        let verdict = isOpenRouter ? live?.fallback : nil
        guard let evidence = pins[key] else {
            return .missing(runtime: key, why: verdict?.pin?.why)
        }
        guard isOpenRouter else {
            return .fileOnly(
                evidence,
                why: "The eavesdropper does not publish whether this pin still holds, so a stale one shows only in its warnings.",
                needsLook: false
            )
        }
        guard statusRead else {
            return .fileOnly(evidence, why: "Muster could not read the eavesdropper's status, so it cannot say whether the pin holds.", needsLook: true)
        }
        guard statusIsCurrent else {
            return .fileOnly(evidence, why: "The eavesdropper's status is not current, so it cannot say whether the pin holds now.", needsLook: true)
        }
        guard let pinned = verdict?.pinned else {
            return .fileOnly(evidence, why: "The eavesdropper did not say whether it holds.", needsLook: live == nil)
        }
        if pinned {
            return .pinned(evidence.judged(pinned: true, why: nil))
        }
        let why = verdict?.pin?.why ?? "not_pinned"
        return .notHolding(evidence.judged(pinned: false, why: why), why: why)
    }

    /// The daemon-wide settings.
    ///
    /// The host load gate stays off,  by the owner's ruling:  `0` means the eavesdropper never
    /// holds a wake for load.  It is shown as off,  and never as a setting nobody looked at.
    private static func daemonSettings(_ sources: WakeSettingsSources) -> [WakeSetting] {
        // A file that was not read says nothing about the gate, so the gate is not known:  it is
        // never shown as off for want of a read.
        if let problem = sources.configProblem {
            return [
                WakeSetting(
                    seat: "daemon",
                    key: .hostLoadGate,
                    configured: nil,
                    live: nil,
                    state: .unknown,
                    note: "The configuration file was not read, so this gate's setting is not known.\u{00A0} \(problem)"
                ),
            ]
        }
        let configured = sources.config.value(section: "daemon", key: "host_wake_load_per_core")
        let isOff = (configured?.displayText ?? "0") == "0"
        // A key the file does not hold is shown as nothing in the file's column, never as a 0 the
        // file does not hold:  the default is the eavesdropper's, and the note says so.
        let note: String
        if configured == nil {
            note = "The file holds nothing, so the eavesdropper's default is in effect:  0, which is off, so it never holds a wake for load."
        } else if isOff {
            note = "Off:  the eavesdropper never holds a wake for load."
        } else {
            note = "The owner ruled this gate stays off."
        }
        return [
            WakeSetting(
                seat: "daemon",
                key: .hostLoadGate,
                configured: configured,
                live: nil,
                state: isOff ? .off : .drift,
                note: note
            ),
        ]
    }
}
