import Foundation

// The seat wake settings,  as the panel shows them.
//
// The panel has two sources and never mixes them:  the configuration file
// (`~/.simple-tools/listener.toml`) holds what the operator wrote,  and the running eavesdropper
// holds what is actually in effect.  A setting where the two disagree is DRIFT,  and it is
// never shown as a quiet green.
//
// MARK: - Wakes or Captures Only

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
            return "Held by the server eavesdropper.\u{00A0} Its settings live on the server listener, not in the Mac's file.\u{00A0} Whether it is on and how it is woken are changed under Server Eavesdropper Settings, through Show Plan and Touch ID;  its caps, timings and routine figures are knobs, so they are read only there."
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

    /// The agent seats the partition gives to the Mac, sorted, or nil when it could not be read.
    /// The Operator stays listed for the Mac as a service identity, not as an agent seat, so it is
    /// not one of them.
    var macAgentSeats: [String]? {
        guard let seats else { return nil }
        return seats
            .filter { $0.value == Self.thisInstance && $0.key != OperatorOptions.seatTag }
            .map(\.key)
            .sorted()
    }

    /// Whether the partition was read and gives the Mac no agent seat.  Since the one-listener
    /// cutover of Nov 10 the server listener holds every seat, and the Mac's eavesdropper is
    /// retired.  A partition that could not be read says nothing either way, so it is never this.
    var givesTheMacNoAgentSeat: Bool {
        macAgentSeats?.isEmpty == true
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
