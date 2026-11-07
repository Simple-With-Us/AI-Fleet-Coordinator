import Foundation

/// Where the window can be.  The sidebar, the overview cards and the saved place all
/// name a destination, so the list of places is written once here rather than once
/// per surface.
///
/// The raw value is the saved spelling under `MusterSettings.sidebarSelectionDefaultsKey`.
/// A saved value this app no longer names reads as the overview, never as a guess at
/// what the owner meant, except the one panel that was split:  see `restored(from:)`.
///
/// The cases are written in the order the sidebar reads, because each section of the
/// sidebar is the cases whose `place` names it, in this order.
enum MusterDestination: String, CaseIterable, Identifiable {
    case overview
    // The Mac
    case services
    case macAgents
    case complianceGrid
    case driftHistory
    case wakeAndListener
    case seatDetail
    // The server
    case serverStatus
    case serverResourceLimits
    case serverUpdates
    // Across the fleet
    case appsAndWork
    case seats
    case controls
    case hosts
    case virtualMachines
    case actionHistory

    var id: String { rawValue }

    /// The panel's one name:  the sidebar row, the window title and the panel's own
    /// heading all read it, so the same place never has two names.
    var title: String {
        switch self {
        case .overview: return "Overview"
        case .services: return "Services"
        case .macAgents: return "Background Agents"
        case .complianceGrid: return "Compliance Grid"
        case .driftHistory: return "Drift History"
        case .wakeAndListener: return "Wake & Listener"
        case .seatDetail: return "Seat Setup"
        case .serverStatus: return "Server Status"
        case .serverResourceLimits: return "Resource Limits"
        case .serverUpdates: return "Server Updates"
        case .appsAndWork: return "Apps & Work"
        case .seats: return "Seats"
        case .controls: return "Controls"
        case .hosts: return "Tools And Worker Health"
        case .virtualMachines: return "Virtual Machines"
        case .actionHistory: return "Action History"
        }
    }

    var systemImage: String {
        switch self {
        case .overview: return "house"
        case .services: return "square.stack.3d.up"
        case .macAgents: return "gearshape.2"
        case .complianceGrid: return "square.grid.3x3"
        case .driftHistory: return "exclamationmark.triangle"
        case .wakeAndListener: return "bell.badge"
        case .seatDetail: return "checklist"
        case .serverStatus: return "cpu"
        case .serverResourceLimits: return "dial.low"
        case .serverUpdates: return "arrow.down.circle"
        case .appsAndWork: return "list.bullet.rectangle"
        case .seats: return "person.2.badge.gearshape"
        case .controls: return "slider.horizontal.3"
        case .hosts: return "rectangle.split.2x1"
        case .virtualMachines: return "cube"
        case .actionHistory: return "list.bullet.clipboard"
        }
    }

    /// Where the panel's data and actions live, or nil for the overview, which shows both
    /// machines.
    ///
    /// There is no default:  a new panel cannot compile until it names its place, and the
    /// sidebar files it from this and from nothing else.  Compliance Grid and Drift History
    /// sit on the Mac because every rule is collected there;  they move when the server
    /// gets a column of its own.
    var place: FleetPlace? {
        switch self {
        case .overview:
            return nil
        case .services, .macAgents, .complianceGrid, .driftHistory, .wakeAndListener, .seatDetail:
            return .machine(.mac)
        case .serverStatus, .serverResourceLimits, .serverUpdates:
            return .machine(.server)
        case .appsAndWork, .seats, .controls, .hosts, .virtualMachines, .actionHistory:
            return .acrossFleet
        }
    }

    /// The line under the window title:  the place, in the same words as its badge.
    var subtitle: String {
        place?.title ?? "The Mac And The Server"
    }

    /// The sidebar's sections, in the order they read.
    static let sidebarPlaces: [FleetPlace] = [.machine(.mac), .machine(.server), .acrossFleet]

    /// The panels under one section of the sidebar, in the order they read.
    static func listed(under place: FleetPlace) -> [MusterDestination] {
        allCases.filter { $0.place == place }
    }

    /// The saved spellings of panels that were split, and where each one went.
    ///
    /// Operations held the Mac's agents, the server's updates and the action log on one
    /// page under the Server heading.  Its saved spelling opens Server Updates, the part
    /// that was the server's.
    static let retiredSpellings: [String: MusterDestination] = ["operations": .serverUpdates]

    /// The destination a saved spelling names, or the overview when it names nothing.
    static func restored(from saved: String?) -> MusterDestination {
        guard let saved else { return .overview }
        if let destination = MusterDestination(rawValue: saved) { return destination }
        return retiredSpellings[saved] ?? .overview
    }
}

extension FleetPlace {
    /// The place as a section of the sidebar names it.
    var sectionHeading: String {
        switch self {
        case .machine(let host): return host.heading
        case .acrossFleet: return "Across The Fleet"
        }
    }
}

// MARK: - Where each panel's data comes from

/// What the window knows about its sources, taken once, for the line under each badge.
struct PlaceSourceInputs {
    /// When the Mac's services were last read.
    var servicesReadAt: Date?
    /// The server's report as the Mac holds it, or nil before the first read.
    var serverStatus: ServerStatusReading?
    /// When the engine last checked the seats on the Mac.
    var seatCheckAt: Date?
}

extension MusterDestination {
    /// Where the panel's data comes from, in one or two sentences, or nil for the overview.
    ///
    /// A time is said only when the window holds it.  A source not read yet says so and
    /// never reads as current.
    func sourceLine(_ inputs: PlaceSourceInputs) -> String? {
        let gap = ServerStatusCopy.sentenceGap
        switch self {
        case .overview:
            return nil
        case .services:
            return inputs.servicesReadAt.map { "Read live from the Mac at \(FleetTime.clock($0))." }
                ?? "Not read from the Mac yet."
        case .macAgents:
            return "Read from the reports the Mac writes about its own agents and Hog Hunter."
        case .complianceGrid:
            return inputs.seatCheckAt.map { "Seat setup checked on the Mac at \(FleetTime.clock($0))." }
                ?? "Seat setup has not been checked on the Mac yet."
        case .driftHistory:
            return "Kept on the Mac, from the Mac's own checks."
        case .wakeAndListener:
            return "The wake settings file on the Mac, beside what the listener reports."
        case .seatDetail:
            return "One seat's setup on the Mac, for any platform:  the engine's answers and the files behind them."
        case .serverStatus:
            return Self.serverSourceLine(inputs.serverStatus)
        case .serverResourceLimits:
            return "Read from the server when this panel opens."
        case .serverUpdates:
            return "The updates the allowlist names.\(gap)Each one runs on the server."
        case .appsAndWork:
            return "The fleet's app list, with each seat's lanes, pull requests and claims."
        case .seats:
            // Zulip refuses presence for bot users,  so the panel no longer reads it.
            return "Sessions and quota come from the Mac's listener, and the last post from Zulip.\(gap)Sessions shown are on the Mac only."
        case .controls:
            return "Shared values in the fleet's secret store, as the Mac's copy shows them."
        case .hosts:
            return "The doctor's report on the Mac, which covers the Mac and the server.\(gap)Worker Health is the Worker's own file, read on the Mac."
        case .virtualMachines:
            return "Each machine's own report, copied to the Mac."
        case .actionHistory:
            return "Actions started from the Mac, on the Mac or the server."
        }
    }

    /// The server report's two times:  when the server wrote it and when the Mac copied it.
    static func serverSourceLine(_ reading: ServerStatusReading?) -> String {
        guard let reading else { return "Reading the server's report on the Mac\u{2026}" }
        if reading.fileMissing { return "No server report on the Mac yet." }
        guard let written = reading.generatedAt else {
            return "The server's report on the Mac could not be read."
        }
        let wrote = "Report written by the server at \(FleetTime.clock(written))."
        guard let copied = reading.copiedAt else { return wrote }
        return "\(wrote)\(ServerStatusCopy.sentenceGap)The Mac copied it at \(FleetTime.clock(copied))."
    }
}

// MARK: - The two places on the Overview

/// One machine as the Overview's card shows it.
///
/// It is built from the menu's own summary of that place, so the card and the menu say
/// the same thing in the same words and cannot disagree.
struct PlaceCard: Equatable {
    /// How many lines a card shows.  The rest are in the panel.
    static let lineLimit = 6

    let host: FleetHost
    let health: MachineHealth
    /// The place's lines, as the menu writes them.
    let lines: [String]

    init(host: FleetHost, health: MachineHealth, items: [MenuItem]) {
        self.host = host
        self.health = health
        self.lines = Array(items.compactMap(Self.words).prefix(Self.lineLimit))
    }

    /// The state in words, the same words the menu bar item speaks.
    var stateWords: String { PlaceHealthWords.sentence(health) }

    /// The panel the card opens.
    var destination: MusterDestination {
        switch host {
        case .mac: return .services
        case .server: return .serverStatus
        }
    }

    /// The words of one menu item, or nil for a click that only makes sense in the menu.
    private static func words(_ item: MenuItem) -> String? {
        switch item {
        case .line(let text): return text
        case .row(let row): return row.title
        case .group(let title, _): return title
        case .command, .action: return nil
        }
    }
}

extension MachineMenu {
    /// The Mac, as the Overview's card shows it.
    var macCard: PlaceCard { PlaceCard(host: .mac, health: mac.health, items: mac.items) }
    /// The server, as the Overview's card shows it.
    var serverCard: PlaceCard { PlaceCard(host: .server, health: server.health, items: server.items) }
}

/// A place's health in words, for the Overview's cards and Server Status.
enum PlaceHealthWords {
    /// The menu bar item's spoken words, starting with a capital.
    static func sentence(_ health: MachineHealth) -> String {
        let spoken = MachineMenuCopy.spoken(health)
        return "\(spoken.prefix(1).uppercased())\(spoken.dropFirst())"
    }

    /// The badge on a sidebar row:  nil when there is nothing to say, a count when something
    /// needs attention, with a plus when some part was not read, and `?` when not known.
    static func badge(_ health: MachineHealth) -> String? {
        switch health {
        case .ok: return nil
        case .attention(let count, let someUnread): return someUnread ? "\(count)+" : "\(count)"
        case .notKnown: return "?"
        }
    }

    /// The symbol that goes with the words, never instead of them.
    static func systemImage(_ health: MachineHealth) -> String {
        switch health {
        case .ok: return "checkmark.circle"
        case .attention: return "exclamationmark.triangle"
        case .notKnown: return "questionmark.circle"
        }
    }
}

// MARK: - The sidebar's badges

enum SidebarBadges {
    /// The Services row's badge:  how many services on the Mac need attention, by the menu's
    /// rule:  failed, could not be read, or started and given up on.
    ///
    /// Before the first read, or once the read is older than the menu's own window, it is
    /// `?`:  an old or absent read is never a clean list.
    ///
    /// It counts by the menu's own rule.  When pm2 did not answer, its rows are not known
    /// and are not counted as problems:  the badge is then `?` with nothing else wrong, or
    /// the count with a plus beside a real problem, as the Mac's card and the menu bar say.
    static func services(_ entries: [FleetServiceEntry], pm2Answered: Bool, readAt: Date?, now: Date) -> String? {
        guard readIsCurrent(readAt: readAt, now: now) else { return "?" }
        let problems = entries.filter { entry in
            MachineMenuCopy.isProblem(entry) && !(MachineMenuCopy.isPM2Row(entry) && !pm2Answered)
        }.count
        if !pm2Answered {
            return problems == 0 ? "?" : "\(problems)+"
        }
        return problems == 0 ? nil : "\(problems)"
    }

    /// True when the Services read is recent enough to count, by the menu's own window.
    static func readIsCurrent(readAt: Date?, now: Date) -> Bool {
        guard let readAt else { return false }
        return now.timeIntervalSince(readAt) <= MachineMenuCopy.macReadWindow
    }

    /// The Services badge in words, for the row's help and for VoiceOver, so the number is
    /// never left to be guessed:  what it counts, and what `?` and a plus mean.  A `?` has two
    /// causes, an old or absent read and a recent read that pm2 did not answer, so it is said
    /// by which one it is.
    static func servicesWords(_ badge: String, readIsCurrent: Bool) -> String {
        if badge == "?" {
            return readIsCurrent
                ? "Not known: pm2 on the Mac did not answer, so its services could not be read."
                : "Not known: the Mac's services have not been read recently."
        }
        let unread = badge.hasSuffix("+")
        let count = Int(badge.dropLast(unread ? 1 : 0)) ?? 0
        let things = count == 1 ? "1 service needs attention" : "\(count) services need attention"
        let ending = unread ? ", and some could not be read." : "."
        return "\(things) (failed, could not be read, or started and given up on)\(ending)"
    }
}

// MARK: - Server Status when the report is not current

/// What Server Status offers when the report it holds is out of its window.
///
/// The rows are the last report's, said to be not current.  The repairs come from the
/// allowlist and never from the report, so an old report cannot take them away:  restarting
/// the report timer is the repair when the server is reached and its report stopped.  A
/// plan cannot change anything, and Touch ID comes before any run.
enum ServerLastKnown {
    /// One unit as the list shows it.  A unit can be named in two scopes, so a row is the
    /// scope and the unit together, as `ServerService.id` is.
    struct Row: Equatable, Identifiable {
        let id: String
        let unit: String
        /// The scope the last report gave, or nil for a unit it did not name or named with no
        /// scope.  A missing scope is never shown as a scope, and never treated as `system`.
        let scope: String?
        /// The unit's last state, in words.
        let lastState: String
    }

    /// The rows to list:  the last report's, one per scope and unit, then the report timer
    /// when the report named it in no scope and its repair is offered.  With no repairs the
    /// unlisted timer row would say nothing the report said, so it is left out.
    static func rows(_ services: [ServerService], offersRepairs: Bool) -> [Row] {
        var rows = services.map { service in
            Row(id: service.id, unit: service.unit, scope: service.scope,
                lastState: "last known \(service.activeState ?? "not known")")
        }
        if offersRepairs, !services.contains(where: { $0.unit == OpsNames.reportTimerUnit }) {
            rows.append(Row(id: "unlisted/\(OpsNames.reportTimerUnit)", unit: OpsNames.reportTimerUnit,
                            scope: nil, lastState: "not in the last report"))
        }
        return rows
    }

    /// The actions one row gets, which are the menu's own repair and nothing wider:  only
    /// the report timer has any, Restart when the allowlist names it and Logs when the
    /// allowlist names the unit at all.  Every other unit gets none.  An old report is no
    /// reason to restart a unit it names, such as a production service, and no reason for a
    /// Start or a Stop.
    ///
    /// An action names the unit and not its scope, and the server acts on the unit in the
    /// scope its allowlist gives.  So a row from the report gets actions only when its scope
    /// is that one:  a same-named unit in the other scope gets none, because the button
    /// would act on a unit the row does not show.  A row the report did not name has no
    /// scope of its own, and its actions run in the allowlist's.  The same holds for a
    /// report row that gave no scope:  it shows the bare unit name, and the allowlist's scope
    /// applies.
    ///
    /// The repairs are offered only when the menu offers its own:  when the server was
    /// reached and its report stopped.  When the Mac's fetch stopped or could not reach the
    /// server, the menu offers a fetch instead, and so the panel offers nothing.
    static func actions(unit: String, scope: String?, allowlist: OpsAllowlist?, offersRepairs: Bool) -> [OpsAction] {
        guard offersRepairs, unit == OpsNames.reportTimerUnit, let allowlist else { return [] }
        if let scope, allowlist.scope(forServerUnit: unit) != scope { return [] }
        let verbs = allowlist.verbs(forServerUnit: unit)
        guard !verbs.isEmpty else { return [] }
        var actions: [OpsAction] = []
        if verbs.contains(.restart) { actions.append(.serverUnit(verb: .restart, unit: unit)) }
        actions.append(.logs(unit: unit))
        return actions
    }
}

/// What the Server Status panel shows under its state line.
///
/// It is chosen from the menu's own reach, the same value the state line's words come
/// from, so the body can never show a verdict the line above it does not.  Only a current
/// reach shows the report's sections and their buttons.
enum ServerPanelMode: Equatable {
    /// Nothing has been read yet:  the state line says so, and nothing else is shown.
    case reading
    /// The report on the Mac could not be read, and here is why.
    case problem(String)
    /// The last report's units, said to be not current.  The allowlist's repairs are
    /// offered only when the server was reached and its report stopped.
    case lastKnown(offersRepairs: Bool)
    /// The report is inside its window and the server was reached.
    case current

    static func make(reach: ServerReach, reading: ServerStatusReading?) -> ServerPanelMode {
        switch reach {
        case .current:
            return .current
        case .notReadYet:
            return .reading
        case .reportStopped:
            if let problem = reading?.problem { return .problem(problem) }
            return .lastKnown(offersRepairs: true)
        case .fetchStopped, .unreachable, .futureDated, .noReport, .cannotRead:
            if let problem = reading?.problem { return .problem(problem) }
            return .lastKnown(offersRepairs: false)
        }
    }

    /// The note above the last-known rows.
    var lastKnownNote: String? {
        guard case .lastKnown(let offersRepairs) = self else { return nil }
        let gap = ServerStatusCopy.sentenceGap
        let opening = "This report is not current, so its rows are not current health."
        if offersRepairs {
            return "\(opening)\(gap)The last known units are below, with the repairs the allowlist names."
        }
        return "\(opening)\(gap)The last known units are below, with no repairs, because the Mac has no current word from the server."
    }
}

// MARK: - The action history

enum ActionHistoryWords {
    /// The machine an audit row names, in the app's own words.
    ///
    /// The two machine words become their place names, a row with none reads as a dash,
    /// and any other word is shown as it was written, because it is all there is.
    static func host(_ raw: String?, via: String?) -> String {
        let machine: String
        if let raw, let host = FleetHost(reportValue: raw) {
            machine = host.heading
        } else {
            machine = raw ?? "\u{2014}"
        }
        return machine + (via.map { " via \($0)" } ?? "")
    }
}

// MARK: - The run's verdict

/// How the run's verdict reads.  The words carry it, and the tone only adds colour.
enum RunVerdictTone: Equatable {
    case neutral
    case pass
    case fail
    case notKnown
}

/// The engine run's verdict, in the owner's words, for the Overview and the grid's footer.
///
/// It is written once here, so the two places that show it cannot drift apart.
struct RunVerdict: Equatable {
    let words: String
    let tone: RunVerdictTone

    static func make(phase: FleetStore.Phase, verdict: String?) -> RunVerdict {
        switch phase {
        case .idle: return RunVerdict(words: "Not Checked Yet", tone: .neutral)
        case .running: return RunVerdict(words: "Checking\u{2026}", tone: .neutral)
        case .failed: return RunVerdict(words: "Not Known", tone: .notKnown)
        case .loaded:
            switch verdict {
            case "PASS": return RunVerdict(words: "All Passing", tone: .pass)
            case "FAIL": return RunVerdict(words: "Problems Found", tone: .fail)
            default: return RunVerdict(words: "Not Known", tone: .notKnown)
            }
        }
    }

    /// What the verdict means,  in `PassMeaning`'s words,  so "All Passing" here is the
    /// grid's PASS.  The Overview shows it under the verdict and the grid's footer as its
    /// tooltip.
    ///
    /// Only a verdict the engine returned has a meaning to state:  an idle, running or
    /// failed run has none.
    static func meaning(phase: FleetStore.Phase, verdict: String?) -> String? {
        guard phase == .loaded, let verdict else { return nil }
        return "\(PassMeaning.verdict(verdict))\u{00A0} \(PassMeaning.everywhere)"
    }

    /// The failing count with its noun:  the engine counts checks, not cells.
    static func failingText(_ summary: EnvelopeSummary?) -> String? {
        guard let summary else { return nil }
        let noun = summary.total == 1 ? "check" : "checks"
        return "\(summary.failing) of \(summary.total) \(noun) failing"
    }
}

// MARK: - The grid's filter

/// Which seats the grid shows.  The Overview's cards open the grid on the state they count,
/// so a click on a count lands on those seats and not on the whole grid.
enum GridStateFilter: String, CaseIterable, Identifiable {
    case all = "All Seats"
    case attention = "Needing Attention"
    case red = "Red"
    case drift = "Drifted"
    case unproven = "Unproven"
    /// Results checked more than a minute before or after the rest of the check, by the
    /// grid's own two-sided rule, so the name says another time and not earlier.
    case checkedAtAnotherTime = "Checked At Another Time"

    var id: String { rawValue }

    /// What the grid says when the filter cannot narrow anything, or nil when it can.
    func cannotNarrowNote(commonCheckedAt: Date?) -> String? {
        guard self == .checkedAtAnotherTime, commonCheckedAt == nil else { return nil }
        return "This check has no shared time, so which results were checked at another time is not known.\(ServerStatusCopy.sentenceGap)Every seat is shown."
    }

    /// True when a seat with these cells belongs under the filter.
    ///
    /// Checked At Another Time uses the Overview's own rule, so the card's count and the
    /// seats it opens on are the same cells.  With no shared check time which results were
    /// checked at another time is not known, as the card's count is then unknown, so every
    /// seat stays in view:  an empty grid would be a zero the Overview does not claim.
    func includes(_ cells: [GridCell], commonCheckedAt: Date?) -> Bool {
        let states = cells.map(\.state)
        switch self {
        case .all: return true
        case .attention:
            return states.contains { $0 == .drift || $0 == .missing || $0 == .unproven || $0 == .unknown }
        case .red: return states.contains(where: \.isRed)
        case .drift: return states.contains(.drift)
        case .unproven: return states.contains(.unproven)
        case .checkedAtAnotherTime:
            guard let commonCheckedAt else { return true }
            return cells.contains { OverviewCounts.isCheckedAtAnotherTime($0, commonCheckedAt: commonCheckedAt) }
        }
    }
}

/// The overview's counts, read off the last run's cells.
///
/// Every count is nil when the run could not be trusted, and nil is what keeps the
/// overview honest:  a run that failed says nothing about the seats, so a count of
/// zero would be a claim the app cannot make.  The view renders nil as unknown.
struct OverviewCounts: Equatable {
    var red: Int?
    var unknown: Int?
    var missing: Int?
    var drift: Int?
    var unproven: Int?
    var stale: Int?

    /// The counts the cells give, when the run could be trusted.
    ///
    /// A cell is stale when its oldest observation differs from the run's shared check
    /// time by more than the grid's own sixty-second tolerance, which is a rule
    /// served from the cache and checked apart from the rest.  With no shared check
    /// time there is nothing to be stale against, so the count stays unknown rather
    /// than reading as none.
    static func make(cells: [GridCell], trusted: Bool, commonCheckedAt: Date?) -> OverviewCounts {
        var counts = OverviewCounts()
        guard trusted else { return counts }
        counts.red = cells.filter(\.state.isRed).count
        counts.unknown = cells.filter { $0.state == .unknown }.count
        counts.missing = cells.filter { $0.state == .missing }.count
        counts.drift = cells.filter { $0.state == .drift }.count
        counts.unproven = cells.filter { $0.state == .unproven }.count
        if let commonCheckedAt {
            counts.stale = cells.filter { isCheckedAtAnotherTime($0, commonCheckedAt: commonCheckedAt) }.count
        }
        return counts
    }

    /// True when a cell was checked at another time than the run's shared check time,
    /// beyond the grid's own sixty-second tolerance.  The count and the grid's filter both
    /// use this, so they cannot disagree.  Like the grid's own rule it is two-sided:  a cell
    /// checked well after the shared time counts as well as one checked well before it.
    static func isCheckedAtAnotherTime(_ cell: GridCell, commonCheckedAt: Date) -> Bool {
        guard let checkedAt = cell.checkedAt else { return false }
        return abs(commonCheckedAt.timeIntervalSince(checkedAt)) > 60
    }

    /// What the red count is made of, one line per part, or nil when it is not known.
    ///
    /// Red is the cells that could not be checked plus the missing ones, so the parts are
    /// shown under the one count and never beside it as more problems.
    var redBreakdown: [String]? {
        guard let unknown, let missing else { return nil }
        return [
            unknown == 1 ? "1 could not be checked" : "\(unknown) could not be checked",
            missing == 1 ? "1 is missing" : "\(missing) are missing",
        ]
    }
}
