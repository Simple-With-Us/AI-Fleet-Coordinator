import Foundation

// The seat wake settings,  as the panel shows them.
//
// The panel has two sources and never mixes them:  the configuration file
// (`~/.simple-tools/listener.toml`) holds what the operator wrote,  and the running eavesdropper
// holds what is actually in effect.  A setting where the two disagree is DRIFT,  and it is
// never shown as a quiet green.
//
// MARK: - Wakes or Captures Only

/// What a seat is, by its own section of the configuration file.
///
/// The owner's ruling of Nov 10, 2026:  a seat has one wake path, a signed wake the eavesdropper
/// sends to the seat's routine, which wakes the right chat and delivers the message to it.  On a
/// seat with several chats the routine picks one by letter, topic words or recency;  on a
/// single-threaded seat it wakes the one thread.  A seat with no routine has no wake at all:  its
/// mentions are kept in its inbox.  The one mechanism is the `routine` table.  Muster never
/// writes a `wake` line.
///
/// Until the listener change lands, today's listener (`simple_tools/config.py`) still decides by
/// the old `wake` line:  it defaults to inbox, reads a routine only with `wake = "http"`, still
/// runs `claude`, `muse` and `openrouter`, and drops every other mismatch to inbox, which only
/// captures.  So a seat Wakes with a routine and `wake = "http"`, or with a routine and no wake
/// line, which is how the one-wake-path schema reads every seat once the line is gone.  It
/// Captures Only with no routine and no wake line or `wake = "inbox"`.  Every other mix is an Old
/// Wake Setting, and the panel says what today's listener does with it rather than call it either.
enum WakeSeatKind: Equatable {
    /// The seat's section holds a routine table, with `wake = "http"` or no wake line, so each
    /// mention wakes the right chat.
    case wakes
    /// The seat's section holds no routine and no wake line, or `wake = "inbox"`, so mentions are
    /// kept in its inbox and nothing is woken.
    case capturesOnly
    /// The seat's section mixes a routine and an old wake line in a way the one wake path does not.
    case oldWakeSetting(WakeOldSetting)
    /// Muster cannot say.  The text says why.
    case notKnown(String)

    /// The kind, as the panel labels it.
    var title: String {
        switch self {
        case .wakes: return "Wakes"
        case .capturesOnly: return "Captures Only"
        case .oldWakeSetting: return "Old Wake Setting"
        case .notKnown: return "Not Known"
        }
    }

    /// Whether the seat Wakes through its routine, so its routine applies.
    var hasRoutine: Bool { self == .wakes }

    /// Whether today's listener wakes the seat at all, by its routine or by an old wake it still
    /// runs, so its caps apply.  A seat the listener drops to inbox is not woken.
    var isWoken: Bool {
        switch self {
        case .wakes: return true
        case .oldWakeSetting(let old): return old.isWoken
        case .capturesOnly, .notKnown: return false
        }
    }

    /// What the label means, for its help text.
    var help: String {
        switch self {
        case .wakes:
            return "Each mention goes to this seat's routine, which wakes the right chat:  it picks among the seat's chats by letter, topic words or recency, or wakes the seat's only thread."
        case .capturesOnly:
            return "Mentions are kept in the seat's inbox, and nothing is woken."
        case .oldWakeSetting(let old):
            return old.note
        case .notKnown(let why):
            return why
        }
    }

    /// Why a seat that Captures Only is not offered a routine here.
    static let routineWaitsOnTheListener = "Giving this seat a routine waits on the listener change that drops the old wake line, so Muster does not offer it yet."

    /// Why Test Wake is not offered yet.
    static let testWakeWaits = "Test Wake waits on the listener's new dry run, which shows the chat a wake would reach and why, without waking it."
}

/// What today's listener does with a seat whose routine and old wake line do not make one wake
/// path, by `simple_tools/config.py`'s own rules.
enum WakeOldSetting: Equatable {
    /// No routine, and an old wake the listener still runs:  claude, muse or openrouter.
    case stillWakes(String)
    /// No routine, and an old wake line the listener refuses:  `http` without a routine, or a word
    /// it does not know.  It drops the seat to inbox.
    case refused(String)
    /// A routine beside a wake line other than `http`.  Today's listener reads a routine only with
    /// `wake = "http"`, so it drops the seat to inbox.  A routine with no wake line is not this
    /// case:  it Wakes, as the one-wake-path schema reads it.
    case routineNeedsHTTP(wake: String?)

    /// The old wakes today's listener still runs without a routine.
    static let stillRun: Set<String> = ["claude", "muse", "openrouter"]

    /// Whether today's listener wakes the seat.
    var isWoken: Bool {
        if case .stillWakes = self { return true }
        return false
    }

    /// What the panel says about it.
    var note: String {
        switch self {
        case .stillWakes(let wake):
            return "The listener still wakes this seat through its old wake = \"\(wake)\" setting until the listener change lands.\u{00A0} Muster no longer edits it."
        case .refused(let wake):
            return "The listener refuses this old wake = \"\(wake)\" line, so this seat only captures until it is removed."
        case .routineNeedsHTTP(let wake):
            let line = wake.map { "has wake = \"\($0)\"" } ?? "has no wake line"
            return "This seat has a routine and \(line).\u{00A0} Today's listener uses a routine only with wake = \"http\", so this seat only captures until the listener change lands."
        }
    }
}
/// How a seat's section holds its routine table.
enum WakeRoutineForm: Equatable {
    /// No routine table:  the seat Captures Only.
    case absent
    /// `routine = { … }` in the seat's own section.
    case inline
    /// A `[seat.X.routine]` header of its own.
    case subTable
    /// A routine Muster cannot model:  a `routine` line that is not a table, or both forms at
    /// once.  The text says which.
    case unreadable(String)

    /// Whether the seat holds a routine table Muster can read.
    var hasTable: Bool { self == .inline || self == .subTable }
}

extension ListenerConfigDocument {
    /// The spellings this file gives a seat in its headers, `[seat.X]` and any table inside it
    /// such as `[seat.X.routine]`, matched as the listener matches them:  it upper-cases a seat's
    /// name (`simple_tools/config.py`, `seat_name = str(name).upper()`), so `[seat.mm]` is the MM
    /// seat.  Two spellings are two headers the listener folds into one seat.
    func spellings(ofSeat tag: String) -> [String] {
        let wanted = tag.uppercased()
        return seatTags.filter { $0.uppercased() == wanted }
    }

    /// The spelling the file already gives a seat, so a read finds `[seat.mm]` for MM and a write
    /// keeps that header as the file has it.  With no header it is the tag itself;  with two, the
    /// first, and the seat is Not Known and is not written.
    func spelling(ofSeat tag: String) -> String {
        spellings(ofSeat: tag).first ?? tag
    }

    /// The name of a seat's own section in this file:  `seat.` and the file's spelling.
    func seatSection(_ tag: String) -> String {
        "seat.\(spelling(ofSeat: tag))"
    }

    /// Every section of a seat, under every spelling the listener folds to it.
    func sections(matchingSeat tag: String) -> [Section] {
        spellings(ofSeat: tag).flatMap { sections(ofSeat: $0) }
    }

    /// How a seat's section holds its routine table.
    func routineForm(seat tag: String) -> WakeRoutineForm {
        let section = seatSection(tag)
        let header = sections.contains { $0.name == "\(section).routine" }
        guard let line = assignment(section: section, key: "routine") else {
            return header ? .subTable : .absent
        }
        if header {
            return .unreadable("The [\(section)] section holds a routine line and a [\(section).routine] header, which TOML refuses, so Muster cannot read the routine.")
        }
        guard line.node.tableEntries != nil else {
            return .unreadable("The routine line in [\(section)] is not a table, so Muster cannot read it.")
        }
        return .inline
    }

    /// The routine table a seat's section holds, or nil when it holds none Muster can read.
    func routineTable(seat tag: String) -> [String: ListenerConfigValue]? {
        routineTable(seat: tag, form: routineForm(seat: tag))
    }

    /// The routine table a seat's section holds, for a form already looked up.
    func routineTable(seat tag: String, form: WakeRoutineForm) -> [String: ListenerConfigValue]? {
        let section = seatSection(tag)
        switch form {
        case .inline:
            if case .table(let entries)? = value(section: section, key: "routine") { return entries }
            return nil
        case .subTable:
            guard let table = sections.first(where: { $0.name == "\(section).routine" }) else { return nil }
            var entries: [String: ListenerConfigValue] = [:]
            for line in table.assignments {
                entries[line.key] = line.node.value
            }
            return entries
        case .absent, .unreadable:
            return nil
        }
    }

    /// The `wake` line a seat's section still holds from the old wake types, as the file holds it,
    /// with nothing trimmed:  the listener compares it exactly, so `" http"` is not `http`.  It is
    /// shown, and read only to say what today's listener does with the seat.
    func leftoverWake(seat tag: String) -> String? {
        value(section: seatSection(tag), key: "wake")?.displayText
    }
}

// MARK: - The routine rules

/// The eavesdropper's own rules for a routine table, as `_routine` in `simple_tools/config.py`
/// applies them, so Muster refuses what `daemon check-config` would refuse.  The names of the
/// environment variables are names, never values:  Muster never reads a secret.
enum WakeRoutineRules {
    /// `ROUTINE_METHODS`.
    static let methods = ["POST", "PUT", "PATCH"]
    /// `ROUTINE_AUTH`.
    static let auths = ["header", "bearer", "hmac-sha256"]
    /// `ROUTINE_DEFAULT_HEADER`.
    static let defaultHeaders: [String: String] = [
        "header": "X-Routine-Key",
        "bearer": "Authorization",
        "hmac-sha256": "X-Simple-Tools-Signature",
    ]
    /// `ROUTINE_RESERVED_HEADERS`.
    static let reservedHeaders: Set<String> = [
        "host", "content-length", "content-type", "transfer-encoding",
        "connection", "user-agent", "idempotency-key", "cookie",
    ]
    /// `GENERIC_ENV`:  the CLI's single-seat variables, which two seats would alias.
    static let genericEnv: Set<String> = ["ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_RC"]
    /// The keys `_routine` knows.
    static let knownKeys: Set<String> = [
        "url_env", "key_env", "method", "header", "auth", "timeout_seconds", "cost_usd", "signature_prefix",
    ]
    /// `ROUTINE_TIMEOUT_DEFAULT`, and the most `_routine` allows.
    static let defaultTimeout: Double = 15
    static let minimumTimeout: Double = 1
    static let maximumTimeout: Double = 60

    /// The router contract on PR #1702:  every router's routine line is `routine = { url_env,
    /// key_env, method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    /// so each takes a POST, signed with a bearer key in the Authorization header.  Muster shows these and never offers
    /// them as choices.
    static let contract: [String: String] = [
        "method": "POST",
        "auth": "bearer",
        "header": "Authorization",
    ]

    /// `env_code`:  GB-COMPILER is GB_COMPILER.
    static func envCode(_ seat: String) -> String {
        var code = ""
        for scalar in seat.uppercased().unicodeScalars {
            if isUpper(scalar) || isDigit(scalar) {
                code.unicodeScalars.append(scalar)
            } else {
                code += "_"
            }
        }
        return code
    }

    /// The variable `_routine` reads a seat's router address from when the table names none.
    static func defaultURLEnv(seat: String) -> String { envCode(seat) + "_ROUTINE_URL" }
    /// The variable `_routine` reads a seat's sender key from when the table names none.
    static func defaultKeyEnv(seat: String) -> String { envCode(seat) + "_ROUTINE_KEY" }

    private static func isUpper(_ scalar: Unicode.Scalar) -> Bool { (65...90).contains(scalar.value) }
    private static func isLower(_ scalar: Unicode.Scalar) -> Bool { (97...122).contains(scalar.value) }
    private static func isDigit(_ scalar: Unicode.Scalar) -> Bool { (48...57).contains(scalar.value) }

    /// `_ENV_NAME_RE`:  `^[A-Z_][A-Z0-9_]{0,127}$`.
    static func isEnvName(_ value: String) -> Bool {
        let scalars = Array(value.unicodeScalars)
        guard (1...128).contains(scalars.count), let first = scalars.first,
              isUpper(first) || first == "_" else { return false }
        return scalars.allSatisfy { isUpper($0) || isDigit($0) || $0 == "_" }
    }

    /// `_HEADER_RE`:  `^[A-Za-z0-9][A-Za-z0-9-]{0,63}$`.
    static func isHeaderName(_ value: String) -> Bool {
        let scalars = Array(value.unicodeScalars)
        guard (1...64).contains(scalars.count), let first = scalars.first,
              isUpper(first) || isLower(first) || isDigit(first) else { return false }
        return scalars.allSatisfy { isUpper($0) || isLower($0) || isDigit($0) || $0 == "-" }
    }

    /// `_PREFIX_RE`:  `^[A-Za-z0-9=:_.-]{0,32}$`.
    static func isSignaturePrefix(_ value: String) -> Bool {
        let scalars = Array(value.unicodeScalars)
        guard scalars.count <= 32 else { return false }
        return scalars.allSatisfy {
            isUpper($0) || isLower($0) || isDigit($0) || "=:_.-".unicodeScalars.contains($0)
        }
    }

    /// `_env_name`:  the problem with a variable name, or nil when there is none.
    static func envNameProblem(_ value: ListenerConfigValue, name: String) -> String? {
        guard case .text(let text) = value, isEnvName(text) else {
            return "\(name) must be an environment variable name (upper case letters, digits and _)"
        }
        if genericEnv.contains(text) {
            return "\(name) may not be \(text): that is the CLI's single-seat variable, and two seats reading it would alias one bot"
        }
        return nil
    }

    /// `_number`:  a number of at least `minimum`, and never a yes or a no.
    private static func number(_ value: ListenerConfigValue) -> Double? {
        if case .number(let number) = value { return number }
        return nil
    }

    /// Python's `str()` of a value, for the method, which `_routine` reads as text whatever it is.
    private static func pythonText(_ value: ListenerConfigValue) -> String {
        switch value {
        case .text(let text): return text
        case .bool(let flag): return flag ? "True" : "False"
        default: return value.displayText
        }
    }

    /// Everything `_routine` would say is wrong with a seat's routine table, in its own words.
    static func problems(seat: String, routine given: [String: ListenerConfigValue]) -> [String] {
        var problems: [String] = []
        // The listener names a seat by its upper-cased name, so its messages do too.
        let prefix = "seat.\(seat.uppercased()).routine"
        for key in given.keys.sorted() where !knownKeys.contains(key) {
            problems.append("\(prefix).\(key) is not a known routine setting")
        }
        let urlValue = given["url_env"] ?? .text(defaultURLEnv(seat: seat))
        let keyValue = given["key_env"] ?? .text(defaultKeyEnv(seat: seat))
        let urlProblem = envNameProblem(urlValue, name: "\(prefix).url_env")
        let keyProblem = envNameProblem(keyValue, name: "\(prefix).key_env")
        if let urlProblem { problems.append(urlProblem) }
        if let keyProblem { problems.append(keyProblem) }
        let method = pythonText(given["method"] ?? .text("POST")).uppercased()
        if !methods.contains(method) {
            problems.append("\(prefix).method must be one of \(methods.joined(separator: ", "))")
        }
        var auth = "bearer"
        if let value = given["auth"] {
            if case .text(let text) = value, auths.contains(text) {
                auth = text
            } else {
                problems.append("\(prefix).auth must be one of \(auths.joined(separator: ", "))")
            }
        }
        let header = given["header"] ?? .text(defaultHeaders[auth] ?? "Authorization")
        var headerIsGood = false
        if case .text(let text) = header, isHeaderName(text), !reservedHeaders.contains(text.lowercased()) {
            headerIsGood = true
        }
        if !headerIsGood {
            problems.append("\(prefix).header must be an HTTP header name (letters, digits and -) other than \(reservedHeaders.sorted().joined(separator: ", "))")
        }
        let signature = given["signature_prefix"] ?? .text("")
        if case .text(let text) = signature, isSignaturePrefix(text) {
            if !text.isEmpty, auth != "hmac-sha256" {
                problems.append("\(prefix).signature_prefix applies only to auth = \"hmac-sha256\"")
            }
        } else {
            problems.append("\(prefix).signature_prefix must be up to 32 letters, digits or =:_.-")
        }
        let timeout = number(given["timeout_seconds"] ?? .number(defaultTimeout))
        if let timeout, timeout >= minimumTimeout {
            if timeout > maximumTimeout {
                problems.append("\(prefix).timeout_seconds must be 60 or less")
            }
        } else {
            problems.append("\(prefix).timeout_seconds must be a number of at least 1")
        }
        let cost = number(given["cost_usd"] ?? .number(0))
        if cost == nil || (cost ?? 0) < 0 {
            problems.append("\(prefix).cost_usd must be a number of at least 0")
        }
        if urlProblem == nil, keyProblem == nil, urlValue == keyValue {
            problems.append("\(prefix).url_env and key_env must be different variables")
        }
        return problems
    }

    /// What a contract key's value is in effect for a routine table:  the file's, or the
    /// eavesdropper's default when the file holds none.
    static func effective(_ key: String, in routine: [String: ListenerConfigValue]) -> String {
        if let value = routine[key] { return value.displayText }
        switch key {
        case "method": return "POST"
        case "auth": return "bearer"
        case "header":
            let auth = routine["auth"]?.displayText ?? "bearer"
            return defaultHeaders[auth] ?? "Authorization"
        default: return ""
        }
    }

    /// How a contract key differs from the router contract, in words, or nil when it does not.
    static func contractDifference(_ key: String, in routine: [String: ListenerConfigValue]) -> String? {
        guard let wanted = contract[key] else { return nil }
        let value = effective(key, in: routine)
        let same = key == "method" ? value.uppercased() == wanted : value == wanted
        guard !same else { return nil }
        return "Differs from the router contract:  \(key) is \(value), and the contract says \(wanted).\u{00A0} Muster offers no control for it."
    }
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
              case .text("server")? = config.value(section: config.seatSection(seat), key: "instance") else {
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
    /// The routine table's keys.  The first three are a waking seat's own;  the method, the
    /// signing and the header are fixed by the router contract, so they are shown and never
    /// offered as choices.
    case routineURLEnv
    case routineKeyEnv
    case routineTimeout
    case routineMethod
    case routineAuth
    case routineHeader
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
        case .routineURLEnv: return "Routine address variable"
        case .routineKeyEnv: return "Routine key variable"
        case .routineTimeout: return "Routine timeout seconds"
        case .routineMethod: return "Routine method"
        case .routineAuth: return "Routine signing"
        case .routineHeader: return "Routine key header"
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

    /// The key inside the routine table, for a routine setting.
    var routineKey: String? {
        switch self {
        case .routineURLEnv: return "url_env"
        case .routineKeyEnv: return "key_env"
        case .routineTimeout: return "timeout_seconds"
        case .routineMethod: return "method"
        case .routineAuth: return "auth"
        case .routineHeader: return "header"
        default: return nil
        }
    }

    /// Whether the setting is one of the routine table's.
    var isRoutine: Bool { routineKey != nil }

    /// Whether the router contract fixes this setting, so Muster shows it and offers no control.
    var isFixedByContract: Bool {
        routineKey.map { WakeRoutineRules.contract[$0] != nil } ?? false
    }

    /// Whether a change to this setting needs the owner's Touch ID:  the variables name where a
    /// seat's wakes go and the key that signs them.
    var needsOwnerCheck: Bool {
        self == .routineURLEnv || self == .routineKeyEnv
    }

    /// Where the setting lives in the seat's section of the configuration file, with the routine
    /// table written inline.  `location(seat:in:)` says where it is in a given file.
    var configLocation: (key: String, subPath: [String]) {
        if let routineKey { return ("routine", [routineKey]) }
        switch self {
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
        case .routineURLEnv, .routineKeyEnv, .routineTimeout, .routineMethod, .routineAuth, .routineHeader:
            // Answered above, by the routine key.
            return ("routine", [])
        }
    }

    /// Where the setting lives for one seat in this file.  A table the file keeps under its own
    /// header (`[seat.X.routine]`, `[seat.X.budget]` or `[seat.X.live]`) is read and written there,
    /// in place, and every other setting lives in the seat's own section, so a write keeps the form
    /// the file already has.  A table written both ways at once is left to the seat's own section,
    /// where the writer refuses it.
    ///
    /// The seat's section is found under the spelling the file already gives it, as the listener
    /// finds it.  A caller that has looked that spelling up passes it, so it is not looked up again
    /// for every setting.
    func location(
        seat tag: String,
        in config: ListenerConfigDocument,
        spelling: String? = nil
    ) -> (section: String, key: String, subPath: [String]) {
        let section = "seat.\(spelling ?? config.spelling(ofSeat: tag))"
        let location = configLocation
        let table = "\(section).\(location.key)"
        if location.subPath.count == 1, let inner = location.subPath.first,
           config.sections.contains(where: { $0.name == table }),
           config.assignment(section: section, key: location.key) == nil {
            return (table, inner, [])
        }
        return (section, location.key, location.subPath)
    }

    /// The five wake count caps in a seat's `budget` (`config.py` `WAKE_CAP_KEYS`).  Since #1735
    /// they have no default (owner, Nov 10, 2026:  "remove budget limits for wakes for now but have
    /// it remain as option in Muster"):  a cap the seat's `budget` does not hold is No Limit, `0` is
    /// Paused, because `wakes.py` `at_cap` is reached at once, and a positive number is a cap.
    var isWakeCap: Bool {
        switch self {
        case .wakesPerHour, .wakesPerDay, .perTopicPerHour, .ownerPerDay, .ownerPerTopicPerHour:
            return true
        default:
            return false
        }
    }

    /// What a wake cap with no value reads.
    static let noLimitText = "No Limit"
    /// What a wake cap of 0 reads.
    static let pausedText = "Paused"

    /// How a wake cap's value reads:  No Limit when the seat holds none, Paused at 0, and the
    /// number otherwise.
    static func wakeCapText(_ value: ListenerConfigValue?) -> String {
        WakeCapLimit(value).text
    }

    /// What a wake cap's field says about its three kinds of value.
    static let wakeCapHelp = "Clear the field for No Limit, which is the plain way to choose it.\u{00A0} 0 is Paused:  no wake of this kind is allowed while it is 0.\u{00A0} A positive number is a cap."

    /// Whether a value of 0 stops every wake:  `live.py` refuses a live turn once its count is at
    /// or over these caps, and keeps its defaults, so a 0 there refuses every wake, never "no
    /// limit".  The live loop turns treat 0 as off, and live minutes per topic as no spacing, so 0
    /// is a real setting for those two.  The five wake caps take 0 as Paused (`isWakeCap`), and the
    /// dollar cap is refused outright.
    var zeroStopsEveryWake: Bool {
        switch self {
        case .livePerHour, .livePerDay, .liveOwnerPerDay:
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

    /// Whether the setting means anything for a seat of this kind:  the routine table for a seat
    /// that Wakes, the caps for any seat the listener wakes today (by its routine or by an old wake
    /// line), and none of them for one that Captures Only, which never wakes.
    func applies(to kind: WakeSeatKind) -> Bool {
        switch self {
        case .hostLoadGate:
            return true
        default:
            return isRoutine ? kind.hasRoutine : kind.isWoken
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
        case .routineMethod, .routineAuth, .routineHeader:
            return "The router contract fixes this (POST, signed with a bearer key in the Authorization header), so Muster shows it and offers no control for it."
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
        case .drift, .unknown, .stale: return true
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

/// The limit a wake cap holds a seat to.  The grid's two columns, the drift note and the usage
/// line all read it from here, so they never say different things about one cap.
enum WakeCapLimit: Equatable {
    /// No cap:  the seat's budget holds none, so any number of wakes of this kind is allowed.
    case noLimit
    /// 0:  no wake of this kind is allowed.
    case paused
    /// A cap.
    case cap(ListenerConfigValue)
    /// Nothing read says what the limit is, such as a file that was not read beside a status with
    /// no budget.  It is never shown as No Limit.
    case notKnown

    /// The limit a wake cap's value sets:  none is No Limit, 0 is Paused, and any other value is a
    /// cap.
    init(_ value: ListenerConfigValue?) {
        guard let value else {
            self = .noLimit
            return
        }
        if case .number(let number) = value, number == 0 {
            self = .paused
            return
        }
        self = .cap(value)
    }

    /// The limit in the panel's words.
    var text: String {
        switch self {
        case .noLimit: return WakeSettingKey.noLimitText
        case .paused: return WakeSettingKey.pausedText
        case .cap(let value): return value.displayText
        case .notKnown: return "not known"
        }
    }

    /// What follows a count of wakes on the usage line:  " of 6", " of 0 (Paused)", " (No Limit)",
    /// or " (limit not known)".
    var usageSuffix: String {
        switch self {
        case .noLimit: return " (\(WakeSettingKey.noLimitText))"
        case .paused: return " of 0 (\(WakeSettingKey.pausedText))"
        case .cap(let value): return " of \(value.displayText)"
        case .notKnown: return " (limit not known)"
        }
    }

    /// Whether a count has reached the limit.  Paused is always reached, and No Limit, or a limit
    /// that is not known, never is.
    func isReached(by count: Int) -> Bool {
        switch self {
        case .paused:
            return true
        case .cap(let value):
            guard case .number(let number) = value else { return false }
            return Double(count) >= number
        case .noLimit, .notKnown:
            return false
        }
    }
}

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
    /// How the setting differs from the router contract, for one the contract fixes, or nil when
    /// it does not differ.
    var contractDifference: String? = nil

    var id: String { "\(seat).\(key.rawValue)" }
    var title: String { key.title }
    var configuredText: String { configured?.displayText ?? WakeSetting.unsetText }
    var liveText: String { live?.displayText ?? WakeSetting.unsetText }
    /// Whether the eavesdropper reported this seat's budget without this wake cap, which since #1735
    /// is No Limit in effect.
    var liveNoLimit: Bool = false

    /// The limit the file sets for a wake cap.  A file that was read and holds none is No Limit,
    /// whether the eavesdropper agrees (`unset`), publishes a cap (`drift`), or did so in a status
    /// that is not current (`stale`).  A file that was not read (`unknown`), a seat the Mac's
    /// eavesdropper does not run (`seatOff`) and one held elsewhere make no claim.
    var fileLimit: WakeCapLimit {
        if let configured { return WakeCapLimit(configured) }
        switch state {
        case .unset, .drift, .stale:
            return .noLimit
        case .match, .notOwned, .unpublished, .off, .unknown, .heldElsewhere, .seatOff:
            return .notKnown
        }
    }

    /// The limit the eavesdropper published for a wake cap:  the cap its budget holds, No Limit
    /// when its budget leaves the cap out, and not known when it published no budget.
    var liveLimit: WakeCapLimit {
        if let live { return WakeCapLimit(live) }
        return liveNoLimit ? .noLimit : .notKnown
    }

    /// The limit a wake cap holds the seat to, the way the grid reads it:  what the eavesdropper
    /// published when it reported a budget, and otherwise what a file that was read holds.  A file
    /// that was not read beside a status with no budget leaves it not known, never No Limit.
    var effectiveLimit: WakeCapLimit {
        let published = liveLimit
        return published == .notKnown ? fileLimit : published
    }

    /// What the file's column shows when it is not a field:  a wake cap reads No Limit when the
    /// file was read and holds none, and Paused at 0.  A file that was not read is never No Limit.
    var fileWords: String {
        if key.isWakeCap, fileLimit != .notKnown {
            return fileLimit.text
        }
        return configured?.displayText ?? "not set"
    }

    /// What the eavesdropper's column shows.
    var liveWords: String {
        if key.isWakeCap, liveLimit != .notKnown {
            return liveLimit.text
        }
        return live?.displayText ?? "not reported"
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
    /// Wakes or Captures Only, from whether the seat's section holds a routine table.
    let kind: WakeSeatKind
    /// The settings,  in the order the panel shows them.
    let settings: [WakeSetting]
    /// How much of the budget the seat has used.
    let usage: WakeUsage
    /// The seat's last few wakes,  newest first.
    let recentWakes: [WakeLedger.Outcome]
    /// Whether the kill switch covers this seat.
    let paused: Bool
    /// Whether the kill switch stops this seat's wakes,  and leaves the rest alone.
    let wakesPaused: Bool
    /// The scope the pause names.
    let pauseScope: String?
    /// Whether the Operator is connected for this seat.
    let connected: Bool?
    /// How the seat's section holds its routine table.
    var routineForm: WakeRoutineForm = .absent
    /// The `wake` line the seat's section still holds from the old wake types, shown and never
    /// read for meaning.
    var leftoverWake: String? = nil
    /// Whether the Mac's eavesdropper reported this seat in its status.
    var reported: Bool = false
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
    /// Why the ledger could not be read, when it could not.  The usage is then not known, and is
    /// never shown as zero.
    var usageProblem: String? = nil
    /// Ledger rows inside the last day that could not be read.
    var tornRecentRows: Int = 0
    /// The provider the seat talks to.
    var provider: String? = nil
    /// Whether the seat's routine can be reached, and why not, as the eavesdropper says.
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
    /// routine's readiness) come from a current status.  When they do not, they are the
    /// eavesdropper's last word as of `liveAsOf`, and the panel says so and never calls them "now".
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

    var id: String { seat }

    /// The settings that are not a plain pass.
    var problems: [WakeSetting] {
        settings.filter { $0.state.isProblem }
    }

    /// The settings the router contract fixes and the file holds otherwise.
    var contractDifferences: [WakeSetting] {
        settings.filter { $0.contractDifference != nil }
    }

    /// The limit a wake cap holds the seat to, read the way its row reads it
    /// (`WakeSetting.effectiveLimit`), or not known for a seat with no such row.
    func capLimit(_ key: WakeSettingKey) -> WakeCapLimit {
        guard key.isWakeCap, let setting = settings.first(where: { $0.key == key }) else { return .notKnown }
        return setting.effectiveLimit
    }

    /// The wake counts beside the limits their caps hold the seat to, and the failed wakes.  A
    /// count that has reached its limit is emphasised, and one under No Limit, or under a limit
    /// that is not known, never is.  The windows are rolling:  the last hour and the last 24 hours,
    /// counted the way the runtime counts them.
    var usageFacts: [WakeFact] {
        let floor = usage.atLeast ? "at least " : ""
        func of(_ count: Int, _ key: WakeSettingKey, _ what: String) -> WakeFact {
            let held = capLimit(key)
            return WakeFact(text: "\(floor)\(count)\(held.usageSuffix) \(what)", emphasised: held.isReached(by: count))
        }
        var facts = [
            of(usage.wakesThisHour, .wakesPerHour, "wakes in the last hour"),
            of(usage.wakesThisDay, .wakesPerDay, "wakes in the last 24 hours"),
            of(usage.ownerThisDay, .ownerPerDay, "owner wakes in the last 24 hours"),
            of(usage.worstTopicThisHour, .perTopicPerHour, "on the busiest topic, last hour"),
            of(usage.worstOwnerTopicThisHour, .ownerPerTopicPerHour, "on the busiest owner topic, last hour"),
        ]
        if usage.failedThisDay > 0 {
            facts.append(WakeFact(text: "\(floor)\(usage.failedThisDay) failed in the last 24 hours", emphasised: true))
        }
        return facts
    }

    /// Whether a current status says the seat's routine cannot be reached.
    var routineNotReady: Bool {
        kind.hasRoutine && liveIsCurrent && routine?.ready == false
    }

    /// What the panel says about the routine's readiness, or nil for a seat with no routine.  A
    /// seat with a routine the eavesdropper reports with none is said to be so, since the
    /// eavesdropper then has no routine for it.
    var routineReadiness: WakeFact? {
        guard kind.hasRoutine else { return nil }
        let suffix = liveIsCurrent ? "" : " (not current)"
        guard let routine else {
            guard reported else { return nil }
            return WakeFact(
                text: "The eavesdropper reports no routine for this seat, so it may still be keeping its mentions in the inbox only." + suffix,
                emphasised: liveIsCurrent
            )
        }
        if routine.ready == true {
            return WakeFact(text: "Routine ready\(routine.host.map { " at \($0)" } ?? "")" + suffix, emphasised: false)
        }
        if routine.ready == false {
            let why = routine.detail ?? routine.why ?? "no reason given"
            return WakeFact(text: "Routine not ready:  \(why)" + suffix, emphasised: liveIsCurrent)
        }
        return WakeFact(text: "The eavesdropper does not say whether the routine is ready." + suffix, emphasised: false)
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
    /// Wakes lost past the caps and still owed are, and so are a routine a current status says
    /// cannot be reached, a routine Muster cannot read, and a setting that differs from the
    /// router contract.
    var needsAttention: Bool {
        // In parts, so the type checker never meets one long chain.
        let settingsOrWarnings = !problems.isEmpty || !redWarnings.isEmpty
        let quota = quotaExhausted
        let section = hasRefusedSection || !skippedReasons.isEmpty
        let use = usageProblem != nil || tornRecentRows > 0
        let lost: Bool = heldBackEvicted > 0
        let pauseNotKnown: Bool = pauseSource.notKnownReason != nil
        let disconnected: Bool = connected == false
        let routineLooks: Bool = routineNotReady || !contractDifferences.isEmpty
        let unreadable: Bool
        if case .unreadable = routineForm { unreadable = true } else { unreadable = false }
        // A seat whose section is there and whose kind Muster cannot say, such as one holding a
        // line Muster cannot read, needs a look, and never reads "No problem found".
        let kindNotKnown: Bool
        if case .notKnown = kind, hasSection { kindNotKnown = true } else { kindNotKnown = false }
        return settingsOrWarnings || quota || section || use || lost || pauseNotKnown || disconnected
            || routineLooks || unreadable || kindNotKnown
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

    /// The held-back wakes, the chat nudges and the quota, as the eavesdropper published them.  Under a status that is not current each one says so, and none says "now".
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
            facts.append(WakeFact(text: text + suffix, emphasised: quotaExhausted))
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

    /// What a seat is, by its own section of the Mac's file, as today's listener reads it.
    ///
    /// A routine table with `wake = "http"` makes it a seat that Wakes, and no routine with no
    /// wake line or `wake = "inbox"` one that Captures Only.  Every other mix is an Old Wake
    /// Setting:  an old wake the listener still runs (claude, muse, openrouter), one it refuses
    /// (`http` with no routine, or a word it does not know), or a routine it ignores because the
    /// wake is not `http`.  The wake line is read for that purpose and never written.  Muster cannot say what a seat is
    /// when the file was not read, when another eavesdropper holds the seat (the Mac's file does not
    /// describe it), when the Mac's file has no section for it, or when its section holds a line
    /// Muster cannot read, which may be the routine itself.
    static func kind(
        form: WakeRoutineForm,
        configProblem: String?,
        host: ListenerSeatHost = .mac,
        hasSection: Bool = true,
        lineProblems: [String] = [],
        leftoverWake: String? = nil,
        foldedHeaders: [String] = []
    ) -> WakeSeatKind {
        if configProblem != nil {
            return .notKnown("The configuration file was not read, so whether this seat has a routine table is not known.")
        }
        if host.isElsewhere {
            return .notKnown("Another eavesdropper holds this seat, and Muster does not read its file, so whether it Wakes is not known here.")
        }
        if !hasSection {
            return .notKnown("The Mac's file has no section for this seat, so the Mac's eavesdropper does not run it and whether it Wakes is not known here.")
        }
        if !foldedHeaders.isEmpty {
            let named = foldedHeaders.map { "[\($0)]" }.joined(separator: " and ")
            return .notKnown("The file holds \(named), which spell this seat differently, and the listener folds them into one seat and merges them unpredictably, so whether it Wakes is not known.\u{00A0} Keep one spelling.")
        }
        if !lineProblems.isEmpty {
            return .notKnown("This seat's section holds a line Muster can't read, which may be its routine, so whether it Wakes is not known.\u{00A0} " + lineProblems.joined(separator: " "))
        }
        // `section.get("wake", "inbox")`:  no wake line is an inbox wake.  The listener compares
        // the value exactly, with no trimming and no case folding, so neither does Muster:
        // `" http"`, `"HTTP"` and `"Claude"` are words it does not know.
        let wake = leftoverWake
        switch form {
        case .unreadable(let why):
            return .notKnown(why)
        case .inline, .subTable:
            // A routine with no wake line Wakes:  in the one-wake-path schema `wake` is gone and a
            // routine alone means the seat wakes, so the panel must not turn every working seat
            // into "only captures" the day the live files drop the line.  Only a wake line that
            // names something other than `http` beside a routine is today's dropped-to-inbox case.
            if wake == nil || wake == "http" {
                return .wakes
            }
            return .oldWakeSetting(.routineNeedsHTTP(wake: wake))
        case .absent:
            guard let line = wake, line != "inbox" else {
                return .capturesOnly
            }
            if WakeOldSetting.stillRun.contains(line) {
                return .oldWakeSetting(.stillWakes(line))
            }
            return .oldWakeSetting(.refused(line))
        }
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
        // A seat the file turns off is reported under `disabled` and not under `seats`, so a running
        // eavesdropper has told Muster about it.  An old status from a stopped one has not, and nor
        // has one another instance wrote:  its list is about its own file, not the Mac's.
        let statusIsTheMacs = sources.status?.status?.instance.map { $0 == ListenerPartition.thisInstance } ?? true
        let seatOff = sources.status?.running == true
            && statusIsTheMacs
            && sources.status?.status?.disabled?.contains(published) == true
        let current = statusIsCurrent(sources)
        let staleText = Self.staleNote(sources)
        // The seat's sections are found as the listener finds them, by the upper-cased name, and
        // the spelling, the form and the routine are looked up once for the seat.
        let seatSections = sources.config.sections(matchingSeat: tag)
        let spellings = sources.config.spellings(ofSeat: tag)
        let spelling = spellings.first ?? tag
        let hasSection = !seatSections.isEmpty
        let form = sources.config.routineForm(seat: tag)
        let leftoverWake = sources.config.leftoverWake(seat: tag)
        let kind = Self.kind(
            form: form,
            configProblem: sources.configProblem,
            host: host,
            hasSection: hasSection,
            lineProblems: seatSections.flatMap(\.problems),
            leftoverWake: leftoverWake,
            // Only the seat headers are spellings:  a `routine`, `budget` or `live` sub-table is not.
            foldedHeaders: spellings.count > 1 ? spellings.map { "seat.\($0)" } : []
        )
        let routine = sources.config.routineTable(seat: tag, form: form) ?? [:]
        let settings = WakeSettingKey.seatKeys.map { key -> WakeSetting in
            if host.isElsewhere {
                let location = key.location(seat: tag, in: sources.config, spelling: spelling)
                return WakeSetting(
                    seat: tag,
                    key: key,
                    configured: sources.config.value(section: location.section, key: location.key, subPath: location.subPath),
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
                for: key, seat: tag, live: live, seatOff: seatOff, config: sources.config,
                spelling: spelling, form: form, routine: routine
            )
            // A file that was not read says nothing about what it holds, so no row is DEFAULT,
            // PASS or DRIFT for want of a read:  it is not known, beside whatever the eavesdropper
            // reports.  The problem box above the seats says why the file was not read.
            if sources.configProblem != nil {
                return WakeSetting(
                    seat: tag, key: key, configured: nil, live: row.live, state: .unknown,
                    note: "The configuration file was not read, so what it holds for this setting is not known.",
                    liveNoLimit: row.liveNoLimit
                )
            }
            // A pass or a drift is a claim about what a running eavesdropper uses now.  An old
            // status, or one a stopped eavesdropper left, cannot make it.
            guard !current, sources.status != nil, row.state == .match || row.state == .drift else {
                return row
            }
            return WakeSetting(
                seat: row.seat, key: row.key, configured: row.configured, live: row.live,
                state: .stale, note: staleText, contractDifference: row.contractDifference,
                liveNoLimit: row.liveNoLimit
            )
        }
        let pause = host.isElsewhere
            ? WakePauseFacts()
            : pauseFacts(seat: published, live: live, sources: sources, current: current)
        let heldBack = live?.heldBack
        return WakeSeat(
            seat: tag,
            host: host,
            hasSection: hasSection,
            bot: live?.bot ?? sources.config.value(section: "seat.\(spelling)", key: "bot")?.scalarText,
            kind: kind,
            settings: settings,
            usage: host.isElsewhere ? WakeUsage() : (sources.usage[tag] ?? WakeUsage()),
            recentWakes: host.isElsewhere ? [] : (sources.recentWakes[tag] ?? []),
            paused: pause.paused,
            wakesPaused: pause.wakesPaused,
            pauseScope: pause.scope,
            // A last-known value is not a current success:  an old status, or one a stopped
            // eavesdropper left, does not say the seat is connected now.
            connected: current ? live?.connected : nil,
            routineForm: form,
            leftoverWake: leftoverWake,
            reported: live != nil,
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
            usageProblem: host.isElsewhere ? nil : sources.usageProblems[tag],
            tornRecentRows: host.isElsewhere ? 0 : (sources.tornRows[tag] ?? 0),
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
            heldBackEvictedTotal: heldBack?.totals?.evicted ?? 0
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
        live: ListenerStatus.Seat?,
        seatOff: Bool,
        config: ListenerConfigDocument,
        spelling: String,
        form: WakeRoutineForm,
        routine: [String: ListenerConfigValue]
    ) -> WakeSetting {
        // The seat's spelling, routine form and routine table were looked up once for the seat.
        let location = key.location(seat: seat, in: config, spelling: spelling)
        let configured = config.value(section: location.section, key: location.key, subPath: location.subPath)
        let liveValue = liveValue(for: key, live: live)
        // Since #1735 the eavesdropper publishes only the wake caps a seat holds, so a budget it
        // published without one says that cap is No Limit.  A status with no budget says nothing.
        let liveNoLimit = key.isWakeCap && liveValue == nil && live?.budget != nil
        let state = state(
            for: key,
            configured: configured,
            live: liveValue,
            liveNoLimit: liveNoLimit,
            seatReported: live != nil,
            seatOff: seatOff
        )
        // The contract is checked only for a seat with a routine table:  a seat without one has
        // no router to reach.
        let difference: String?
        if key.isFixedByContract, let name = key.routineKey, form.hasTable {
            difference = WakeRoutineRules.contractDifference(name, in: routine)
        } else {
            difference = nil
        }
        return WakeSetting(
            seat: seat,
            key: key,
            configured: configured,
            live: liveValue,
            state: state,
            note: note(
                for: key, state: state, configured: configured, live: liveValue,
                liveNoLimit: liveNoLimit,
                defaultText: defaultText(for: key, seat: seat, routine: routine)
            ),
            contractDifference: difference,
            liveNoLimit: liveNoLimit
        )
    }

    /// What the eavesdropper uses for a routine setting the file does not hold.
    static func defaultText(
        for key: WakeSettingKey,
        seat: String,
        routine: [String: ListenerConfigValue]
    ) -> String? {
        switch key {
        case .routineURLEnv: return WakeRoutineRules.defaultURLEnv(seat: seat.uppercased())
        case .routineKeyEnv: return WakeRoutineRules.defaultKeyEnv(seat: seat.uppercased())
        case .routineTimeout: return ListenerConfigValue.format(number: WakeRoutineRules.defaultTimeout)
        case .routineMethod: return "POST"
        case .routineAuth: return "bearer"
        case .routineHeader: return WakeRoutineRules.effective("header", in: routine)
        default: return nil
        }
    }

    /// The value the running eavesdropper is using.  It publishes the caps it holds, and none of
    /// the routine's settings, only whether the routine can be reached.
    private static func liveValue(
        for key: WakeSettingKey,
        live: ListenerStatus.Seat?
    ) -> ListenerConfigValue? {
        switch key {
        case .routineURLEnv, .routineKeyEnv, .routineTimeout, .routineMethod, .routineAuth, .routineHeader:
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
    ///   * a setting the engine does not enforce:  never a pass
    ///   * the eavesdropper says the file turns this seat off:  it is using nothing,  and that is the
    ///     file's own choice,  not a problem
    ///   * the file holds nothing:  the live value is the default, except for a wake cap, which is
    ///     then No Limit, so a cap the eavesdropper published differs from it:  DRIFT
    ///   * the eavesdropper reported nothing for this seat:  what it is using is unknown,  and that is
    ///     a problem,  because its status could not be read or it is not running the seat
    ///   * the eavesdropper reported the seat and not this setting:  it does not publish it
    ///   * the two agree:  a pass
    ///   * the two differ:  DRIFT
    private static func state(
        for key: WakeSettingKey,
        configured: ListenerConfigValue?,
        live: ListenerConfigValue?,
        liveNoLimit: Bool = false,
        seatReported: Bool,
        seatOff: Bool
    ) -> WakeSettingState {
        // A setting the engine does not enforce is never a pass,  whatever the two values
        // say:  the number in the file would not bind anything.
        if !key.isEnforced {
            return .notOwned
        }
        if seatOff, configured != nil, live == nil {
            return .seatOff
        }
        // The file first:  a setting nobody holds is the eavesdropper's default,  and a
        // setting only the file holds is one the eavesdropper does not publish.
        guard let configured else {
            // Since #1735 a wake cap the file holds none of is No Limit, so a cap the eavesdropper
            // published is not what the file chooses:  the two differ.  Every other setting the
            // file leaves out is the eavesdropper's default.
            if key.isWakeCap, live != nil { return .drift }
            return .unset
        }
        guard let live else {
            // A wake cap the eavesdropper's budget leaves out is No Limit in effect, so a cap the
            // file holds is not what it uses.
            if liveNoLimit { return .drift }
            return seatReported ? .unpublished : .unknown
        }
        return configured.matches(live) ? .match : .drift
    }

    /// A line about the setting,  in words.
    private static func note(
        for key: WakeSettingKey,
        state: WakeSettingState,
        configured: ListenerConfigValue?,
        live: ListenerConfigValue?,
        liveNoLimit: Bool = false,
        defaultText: String?
    ) -> String {
        let file = key.isWakeCap ? WakeSettingKey.wakeCapText(configured) : (configured?.displayText ?? "nothing")
        switch state {
        case .drift:
            let using: String
            if key.isWakeCap, live != nil || liveNoLimit {
                using = WakeSettingKey.wakeCapText(live)
            } else {
                using = live?.displayText ?? "something else"
            }
            // A wake cap the file leaves out is No Limit, and the note says so in those words.
            if key.isWakeCap, configured == nil {
                return "The eavesdropper is using \(using),  and the file holds no cap, so it says No Limit."
            }
            return "The eavesdropper is using \(using),  and the file holds \(file)."
        case .notOwned:
            if let reason = key.refusalReason { return reason }
            return "The eavesdropper does not enforce this setting."
        case .unpublished:
            if key.isRoutine {
                return "The eavesdropper reads this routine setting and does not publish it, so only the file's value can be shown."
            }
            return "The eavesdropper reports this seat and does not publish this setting, so only the file's value can be shown."
        case .unset:
            if key.isWakeCap {
                return "No Limit:  the file holds no cap, and the eavesdropper sets none of its own.\u{00A0} Clearing the field is how to choose No Limit."
            }
            if let defaultText {
                return "The file holds nothing,  so the eavesdropper's default is in effect:  \(defaultText)."
            }
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
            return ""
        }
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
