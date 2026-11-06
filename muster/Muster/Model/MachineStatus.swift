import Foundation

// What the menu bar says about the two places.
//
// Location comes before status:  the Mac and the server are separate named places,  and
// everything sits under the place it runs on.  The menu's meaning is carried only by words in
// plain rows,  so it survives dropped icons,  colour and header styling.  This file builds
// those words,  and the views only draw them,  so a unit test can read the whole menu.
//
// Nothing here reaches the network.  A place reads OK only when it was read inside its window:
// an unknown is never counted as zero,  and never shown as OK.

// MARK: - The health of one place

/// What the bar says about one place.
enum MachineHealth: Equatable {
    case ok
    /// So many things need attention,  and perhaps something else could not be read.
    case attention(Int, someUnread: Bool)
    case notKnown
}

/// How far the Mac's knowledge of the server goes.  The first match wins,  in this order.
enum ServerReach: Equatable {
    /// Nothing has been read yet.
    case notReadYet
    /// The Mac's own fetch is missing,  unreadable or old,  so the fault is on the Mac.
    case fetchStopped(lastFetchAt: Date?, lastReportAt: Date?)
    /// The fetch is current and it could not reach the server.
    case unreachable(checkedAt: Date?, lastReportAt: Date?)
    /// The server was reached,  and its report stopped.
    case reportStopped(reachedAt: Date?, reportAt: Date)
    /// No server report is on the Mac.
    case noReport
    /// The report is there and this app does not read it.
    case cannotRead(ReportRefusal)
    /// The report is dated ahead of the Mac's clock.
    case futureDated
    /// The report is inside its window.
    case current(reportAt: Date, copiedAt: Date?)

    /// Reads the two local files.  Opening the menu never contacts the server.
    static func make(status: ServerStatusReading?, tools: FleetToolsReading?) -> ServerReach {
        guard let status, let tools else { return .notReadYet }
        if let refusal = tools.refusal { return .cannotRead(refusal) }
        if tools.problem != nil || !tools.isCurrent {
            return .fetchStopped(lastFetchAt: tools.generatedAt ?? status.copiedAt,
                                 lastReportAt: status.generatedAt)
        }
        let serverHalf = tools.hosts.first { FleetHost(reportValue: $0.host) == .server }
        if let serverHalf, !serverHalf.reachable {
            return .unreachable(checkedAt: tools.generatedAt, lastReportAt: status.generatedAt)
        }
        if status.fileMissing { return .noReport }
        if let refusal = status.refusal { return .cannotRead(refusal) }
        if status.futureDated { return .futureDated }
        guard status.problem == nil, let reportAt = status.generatedAt else {
            return .cannotRead(.unreadable)
        }
        if !status.isCurrent {
            return .reportStopped(reachedAt: serverHalf == nil ? nil : tools.generatedAt, reportAt: reportAt)
        }
        return .current(reportAt: reportAt, copiedAt: status.copiedAt)
    }
}

/// What came of the last Ask The Server Now.
struct ServerAskResult: Equatable {
    let answered: Bool
    let at: Date
}

// MARK: - The menu's shape

/// A click that does not start an action on a machine.
enum MenuCommand: Equatable {
    case checkMac
    case openServerStatus
    case openServices
    case openComplianceGrid
    case openMuster
    case askServer
    /// Fetch From The Server Again:  it runs on the Mac and asks the server for its report.
    case fetchFromServer
}

/// One row with a state and,  when it has any,  its actions behind it.
struct MenuRow: Equatable {
    let title: String
    let actions: [MenuAction]
}

/// One item in a section of the menu.
indirect enum MenuItem: Equatable {
    /// Words,  which are a dimmed row.
    case line(String)
    /// A row with a state,  which opens to its actions or is plain words when it has none.
    case row(MenuRow)
    /// A submenu.
    case group(title: String, items: [MenuItem])
    /// A click on something that is not an action on a machine.
    case command(title: String, MenuCommand)
    /// An action on a machine,  directly in the section.
    case action(MenuAction)
}

/// One place,  as the menu shows it.
struct MacPlaceSummary: Equatable {
    let health: MachineHealth
    let items: [MenuItem]
}

/// The server,  as the menu shows it.
struct ServerPlaceSummary: Equatable {
    let reach: ServerReach
    let health: MachineHealth
    let items: [MenuItem]

    /// True when the fault is on the Mac,  which counts it.
    var fetchStopped: Bool {
        if case .fetchStopped = reach { return true }
        return false
    }
}

/// Everything the menu reads,  taken once.
struct MachineMenuInputs {
    var services: FleetServices?
    var lastReadAt: Date?
    /// The red cells,  when the last run could be trusted.
    var redCells: [DriftRedCell]?
    var driftCells: [DriftRedCell] = []
    /// Whether the last run could be trusted,  or nil before any run has landed.
    var lastRunWasTrusted: Bool?
    var transitions: [DriftTransition] = []
    var status: ServerStatusReading?
    var tools: FleetToolsReading?
    var asked: ServerAskResult?
    var isAsking = false
    var allowlist: OpsAllowlist?
    var actionState: ServiceActionState?
    var now: Date
}

/// The whole menu,  built once from what it reads.
struct MachineMenu: Equatable {
    let mac: MacPlaceSummary
    let server: ServerPlaceSummary

    init(_ inputs: MachineMenuInputs) {
        let server = MachineMenuCopy.server(inputs)
        self.server = server
        self.mac = MachineMenuCopy.mac(inputs, fetchStopped: server.fetchStopped, reach: server.reach)
    }

    func barTitle(short: Bool) -> String {
        MachineMenuCopy.barTitle(mac: mac.health, server: server.health, short: short)
    }

    var barSymbol: String {
        mac.health == .ok && server.health == .ok ? "shield" : "exclamationmark.shield"
    }

    var accessibilityLabel: String {
        "Muster:  the Mac \(MachineMenuCopy.spoken(mac.health)), the server \(MachineMenuCopy.spoken(server.health))"
    }

    /// The whole menu as strings,  for a test to read.
    var plainText: String { MachineMenuCopy.plainText(self) }
}

// MARK: - The words

enum MachineMenuCopy {
    /// How long a read of the Mac stays current.
    static let macReadWindow: TimeInterval = 5 * 60
    /// How many red cells the menu names before it points at the grid.
    static let cellRowLimit = 8
    /// How long a reason can be in a menu row.  The rest is in the Services panel.
    static let reasonLimit = 120

    private static let gap = ServerStatusCopy.sentenceGap

    // MARK: The bar

    static func barTitle(mac: MachineHealth, server: MachineHealth, short: Bool) -> String {
        let macWord = short ? "M" : "Mac"
        let serverWord = short ? "S" : "Server"
        return "\(macWord) \(barWord(mac)) \u{00B7} \(serverWord) \(barWord(server))"
    }

    private static func barWord(_ health: MachineHealth) -> String {
        switch health {
        case .ok: return "OK"
        case .attention(let count, let someUnread): return someUnread ? "\(count)+" : "\(count)"
        case .notKnown: return "?"
        }
    }

    /// The same state,  in words for VoiceOver.
    static func spoken(_ health: MachineHealth) -> String {
        switch health {
        case .ok: return "OK"
        case .attention(let count, let someUnread):
            let things = count == 1 ? "1 thing needs attention" : "\(count) things need attention"
            return someUnread ? "\(things), and some could not be read" : things
        case .notKnown: return "not known"
        }
    }

    // MARK: The Mac

    static func mac(_ inputs: MachineMenuInputs, fetchStopped: Bool, reach: ServerReach) -> MacPlaceSummary {
        var items: [MenuItem] = []
        let entries = inputs.services?.entries ?? []
        let pm2Answered = inputs.services?.pm2Answered ?? true
        let macIsCurrent: Bool
        if let readAt = inputs.lastReadAt {
            macIsCurrent = inputs.now.timeIntervalSince(readAt) <= macReadWindow
            items.append(.line(macIsCurrent
                ? "Checked at \(FleetTime.clock(readAt))"
                : "Last checked at \(FleetTime.clock(readAt)), so this is not current."))
        } else {
            macIsCurrent = false
            items.append(.line("Reading the Mac\u{2026}"))
        }
        if fetchStopped {
            items.append(.line(fetchStoppedLine(reach)))
        }
        if let line = actionItem(inputs.actionState, host: .mac) {
            items.append(line)
        }
        let unreadPM2 = pm2Answered ? [] : entries.filter(isPM2Row)
        if !unreadPM2.isEmpty {
            items.append(.line("pm2 did not answer, so \(unreadPM2.count) services are not known."))
        }

        let rows = entries.map { (entry: $0, row: serviceRow($0, allowlist: inputs.allowlist, pm2Answered: pm2Answered)) }
        let problems = rows.filter { pair in
            isProblem(pair.entry) && !(isPM2Row(pair.entry) && !pm2Answered)
        }
        if problems.isEmpty, !entries.isEmpty {
            items.append(.line("Services:  \(serviceCounts(entries))"))
        }
        items.append(contentsOf: problems.map { MenuItem.row($0.row) })
        items.append(seatItem(inputs))
        if let services = inputs.services {
            let sorted = rows.filter { isProblem($0.entry) } + rows.filter { !isProblem($0.entry) }
            items.append(.group(title: services.menuTitle, items: sorted.map { MenuItem.row($0.row) }))
        }
        items.append(.command(title: "Check The Mac Again", .checkMac))

        let health = macHealth(inputs, problems: problems.count, fetchStopped: fetchStopped,
                               macIsCurrent: macIsCurrent, pm2Answered: pm2Answered)
        return MacPlaceSummary(health: health, items: items)
    }

    private static func macHealth(_ inputs: MachineMenuInputs, problems: Int, fetchStopped: Bool,
                                  macIsCurrent: Bool, pm2Answered: Bool) -> MachineHealth {
        guard macIsCurrent, let reds = inputs.redCells else { return .notKnown }
        let count = reds.count + inputs.driftCells.count + problems + (fetchStopped ? 1 : 0)
        if !pm2Answered {
            return count == 0 ? .notKnown : .attention(count, someUnread: true)
        }
        return count == 0 ? .ok : .attention(count, someUnread: false)
    }

    private static func isPM2Row(_ entry: FleetServiceEntry) -> Bool {
        if case .pm2 = entry.kind { return entry.policy == .alwaysOn }
        return false
    }

    /// A failure,  a state nobody can read,  or a service Muster tried to start and gave up on.
    private static func isProblem(_ entry: FleetServiceEntry) -> Bool {
        entry.needsAttention
    }

    private static func serviceCounts(_ entries: [FleetServiceEntry]) -> String {
        let order: [(ManagedServiceState, String)] = [
            (.running, "running"), (.failed, "failed"), (.stopped, "stopped"),
            (.notInstalled, "not installed"), (.notInPM2, "not in this Mac's pm2"),
            (.unknown, "not known"),
        ]
        let parts = order.compactMap { state, word -> String? in
            let count = entries.filter { $0.state == state }.count
            // Running and stopped are always said,  so a list of none is not a blank.
            if count == 0, state != .running, state != .stopped { return nil }
            return "\(count) \(word)"
        }
        return parts.joined(separator: ", ")
    }

    /// One service's row:  its title,  its state and the actions it offers.
    static func serviceRow(_ entry: FleetServiceEntry, allowlist: OpsAllowlist?, pm2Answered: Bool) -> MenuRow {
        var title = "\(entry.menuRowTitle):  \(entry.summary)"
        // The owner's stop,  or what Muster did about it,  so the menu says who stopped it.
        if let note = entry.menuNote { title += " (\(note))" }
        guard pm2Answered || !isPM2Row(entry) else {
            return MenuRow(title: title, actions: [])
        }
        let order: [ManagedServiceAction] = [.start, .restart, .stop]
        let offered = order.filter { entry.availableActions.contains($0) }
        let actions = offered.compactMap { MenuAction.forService($0, entry: entry, allowlist: allowlist) }
        if let why = entry.whyNoActions {
            title += ", \(why)"
        } else if actions.isEmpty, !offered.isEmpty {
            title += ", \(noRouteWords(allowlist: allowlist))"
        }
        return MenuRow(title: title, actions: actions)
    }

    /// Why a row with actions to offer has no route for them.
    static func noRouteWords(allowlist: OpsAllowlist?) -> String {
        allowlist == nil ? "the allowlist could not be read" : "the allowlist does not name it"
    }

    /// The same reason as a sentence,  for the Services panel,  which also has to say so when
    /// only some of a row's actions have a route.
    static func noRouteSentence(allowlist: OpsAllowlist?, someRouted: Bool) -> String {
        if allowlist != nil, someRouted { return "The allowlist does not name its other actions." }
        let words = noRouteWords(allowlist: allowlist)
        return "\(words.prefix(1).uppercased())\(words.dropFirst())."
    }

    private static func seatItem(_ inputs: MachineMenuInputs) -> MenuItem {
        guard let reds = inputs.redCells else {
            if inputs.lastRunWasTrusted == false {
                return .line("Seat setup:  the last check could not be trusted.")
            }
            return .line("Seat Setup:  checking\u{2026}")
        }
        let drifted = inputs.driftCells
        if reds.isEmpty, drifted.isEmpty {
            return .line("Seat Setup:  no red or drifted cells")
        }
        var parts: [String] = []
        if !reds.isEmpty { parts.append(reds.count == 1 ? "1 red cell" : "\(reds.count) red cells") }
        if !drifted.isEmpty { parts.append(drifted.count == 1 ? "1 drifted cell" : "\(drifted.count) drifted cells") }

        var rows: [MenuItem] = []
        for cell in (reds + drifted).prefix(cellRowLimit) {
            rows.append(.command(title: "\(cell.seat) / \(cell.kindTitle):  \(cell.ruleID ?? stateWords(cell.state))",
                                 .openComplianceGrid))
        }
        let more = reds.count + drifted.count - cellRowLimit
        if more > 0 {
            // No ellipsis:  this opens the grid,  and nothing runs.
            rows.append(.command(title: more == 1 ? "1 More Cell" : "\(more) More Cells",
                                 .openComplianceGrid))
        }
        if !inputs.transitions.isEmpty {
            rows.append(.line("Turned Red Most Recently"))
            for transition in inputs.transitions.prefix(3) {
                rows.append(.command(title: "\(transition.timeText)  \(transition.seat) / \(transition.kindTitle)",
                                     .openComplianceGrid))
            }
        }
        rows.append(.command(title: "Open Compliance Grid", .openComplianceGrid))
        return .group(title: "Seat Setup:  \(parts.joined(separator: ", "))", items: rows)
    }

    /// A cell's state in words,  for a cell with no rule to name.
    private static func stateWords(_ state: CellState) -> String {
        switch state {
        case .missing: return "missing"
        case .unknown: return "could not be read"
        case .drift: return "drifted"
        case .passed: return "passing"
        case .unproven: return "not proven"
        case .unsupported: return "not supported"
        case .covered: return "does not apply"
        }
    }

    // MARK: The server

    static func server(_ inputs: MachineMenuInputs) -> ServerPlaceSummary {
        let reach = ServerReach.make(status: inputs.status, tools: inputs.tools)
        var items: [MenuItem] = []
        var offersFetch = false
        let notKnownTimers = MenuItem.line("Timers \(FleetHost.server.onTitle):  not known")
        var health = MachineHealth.notKnown

        switch reach {
        case .notReadYet:
            items.append(.line("Reading the server's report on the Mac\u{2026}"))
        case .fetchStopped(_, let lastReportAt):
            items.append(.line("\(lastReportLine(lastReportAt))\(gap)Whether the server is up is not known."))
            items.append(notKnownTimers)
            offersFetch = true
        case .unreachable(let checkedAt, let lastReportAt):
            let reached = checkedAt.map { "The Mac could not reach the server at \(FleetTime.clock($0))." }
                ?? "The Mac could not reach the server."
            items.append(.line("\(reached)\(gap)\(lastReportLine(lastReportAt))"))
            items.append(notKnownTimers)
            offersFetch = true
        case .reportStopped(let reachedAt, let reportAt):
            let stopped = reachedAt.map {
                "Reached at \(FleetTime.clock($0)), but its report stopped at \(FleetTime.clock(reportAt))."
            } ?? "Its report stopped at \(FleetTime.clock(reportAt))."
            items.append(.line(stopped))
            items.append(contentsOf: reportTimerRepair(inputs.allowlist))
            health = .attention(1, someUnread: false)
        case .noReport:
            items.append(.line("No server report on the Mac yet."))
            offersFetch = true
        case .cannotRead(let refusal):
            switch refusal {
            case .appOutOfDate:
                items.append(.line("This Muster cannot read the server's report.\(gap)Update Muster."))
            case .reporterOutOfDate:
                items.append(.line("This Muster cannot read the server's report.\(gap)Update the server's reporter."))
            case .notFromServer:
                // The next fetch writes the server's own report over this file.
                items.append(.line("This report did not come from the server."))
                offersFetch = true
            case .unreadable:
                items.append(.line("The server's report on the Mac could not be read."))
                offersFetch = true
            }
        case .futureDated:
            items.append(.line("The server's report has a time ahead of the Mac's clock, so it is not current."))
        case .current(let reportAt, let copiedAt):
            let copied = copiedAt.map { ", copied at \(FleetTime.clock($0))" } ?? ""
            items.append(.line("Report from \(FleetTime.clock(reportAt))\(copied)"))
            if let status = inputs.status {
                items.append(contentsOf: currentItems(status, allowlist: inputs.allowlist))
                health = currentHealth(status)
            }
        }

        if let line = actionItem(inputs.actionState, host: .server) {
            items.append(line)
        }
        if let asked = inputs.asked {
            let time = FleetTime.clock(asked.at)
            items.append(.line(asked.answered
                ? "The server answered at \(time)."
                : "The server did not answer at \(time)."))
        }
        items.append(.command(title: "Open Server Status", .openServerStatus))
        if offersFetch {
            items.append(.command(title: "Fetch From The Server Again\u{2026}", .fetchFromServer))
        }
        items.append(inputs.isAsking
            ? .line("Asking the server\u{2026}")
            : .command(title: "Ask The Server Now", .askServer))
        return ServerPlaceSummary(reach: reach, health: health, items: items)
    }

    /// What the Mac section says when the fault is the Mac's own fetch.
    private static func fetchStoppedLine(_ reach: ServerReach) -> String {
        let opening = "The Mac stopped fetching the server's report."
        if case .fetchStopped(.some(let at), _) = reach {
            return "\(opening)\(gap)Last fetch at \(FleetTime.clock(at))."
        }
        return opening
    }

    private static func lastReportLine(_ at: Date?) -> String {
        guard let at else { return "The Mac holds no server report." }
        return "Last report from \(FleetTime.clock(at)), so it is not current."
    }

    /// The repair when the server was reached and its report stopped.
    ///
    /// A plan cannot change anything,  and the verbs come from the allowlist and not from
    /// the report,  so a stale report does not take this away.
    private static func reportTimerRepair(_ allowlist: OpsAllowlist?) -> [MenuItem] {
        let unit = OpsNames.reportTimerUnit
        guard let allowlist else { return [] }
        var items: [MenuItem] = []
        let verbs = allowlist.verbs(forServerUnit: unit)
        if verbs.contains(.restart) {
            items.append(.action(unitAction(.serverUnit(verb: .restart, unit: unit),
                                            word: "Restart \(OpsNames.unitTitle(unit))")))
        }
        if !verbs.isEmpty {
            items.append(.action(unitAction(.logs(unit: unit), word: "Read Its Logs")))
        }
        return items
    }

    private static func unitAction(_ action: OpsAction, word: String) -> MenuAction {
        MenuAction.plan(action, word: word)
    }

    private static func currentItems(_ status: ServerStatusReading, allowlist: OpsAllowlist?) -> [MenuItem] {
        var items: [MenuItem] = []
        let services = status.services
        let timersWord = services.allSatisfy { $0.unit.hasSuffix(".timer") } ? "Timers" : "Services"
        if services.isEmpty {
            items.append(.line("Timers \(FleetHost.server.onTitle):  not known"))
        } else {
            let active = services.filter { $0.activeState == "active" }.count
            items.append(.line("\(timersWord):  \(active) of \(services.count) active"))
        }
        items.append(.line(appsLine(status.coolify)))
        items.append(.line(fullnessLine(status.hostMetrics)))
        if let load = status.hostMetrics?.loadFraction {
            if load >= 2 { items.append(.line(String(format: "Load %.1f per cpu", load))) }
        } else {
            // Nothing else says so,  and it keeps the bar from reading OK.
            items.append(.line("Load not known"))
        }
        let rows = services.map { (service: $0, row: unitRow($0, allowlist: allowlist)) }
        items.append(contentsOf: rows.filter { countsAgainst($0.service) }.map { MenuItem.row($0.row) })
        if !rows.isEmpty {
            items.append(.group(title: "\(timersWord) \(FleetHost.server.onTitle) (\(rows.count))",
                                items: rows.map { MenuItem.row($0.row) }))
        }
        return items
    }

    private static func appsLine(_ apps: [CoolifyResource]) -> String {
        let seen = apps.filter { $0.resourceStatus != .absent }
        guard !seen.isEmpty else { return "Apps:  not known" }
        let running = seen.filter { $0.resourceStatus == .running }.count
        let failed = seen.filter { $0.resourceStatus == .exited || $0.resourceStatus == .unhealthy }.count
        let unknown = seen.count - running - failed
        let extra = unknown > 0 ? ", \(unknown) not known" : ""
        return "Apps:  \(running) running, \(failed) failed\(extra)"
    }

    private static func fullnessLine(_ metrics: HostMetrics?) -> String {
        let disk = metrics?.disks.compactMap(\.usedFraction).max()
        let memory = metrics?.memory?.usedFraction
        let unreadDisks = metrics?.disks.filter { $0.usedFraction == nil }.count ?? 0
        var diskWords = disk.map { "Fullest disk \(percent($0))" } ?? "Fullest disk not known"
        if disk != nil, unreadDisks > 0 { diskWords += ", \(unreadDisks) more not known" }
        let memoryWords = memory.map { "memory \(percent($0))" } ?? "memory not known"
        return "\(diskWords), \(memoryWords)"
    }

    private static func percent(_ fraction: Double) -> String {
        "\(Int((fraction * 100).rounded()))%"
    }

    /// True when a unit is a problem:  failed,  stopped,  or in no state that can be read.
    /// A unit on its way between two states is neither.
    private static func countsAgainst(_ service: ServerService) -> Bool {
        switch service.activeState ?? "" {
        case "active", "activating", "reloading", "deactivating": return false
        default: return true
        }
    }

    /// The count on the bar,  from a current report.
    ///
    /// It deliberately does not use the report's own overall state:  absent Coolify rows
    /// and missing files are not counted,  and the panel still shows them.
    ///
    /// Every part the count rests on has to have been read.  A part that was not read is not
    /// a zero,  so it can never leave the place reading OK:  with nothing wrong the place is
    /// not known,  and beside a real problem the count carries a plus.
    private static func currentHealth(_ status: ServerStatusReading) -> MachineHealth {
        let metrics = status.hostMetrics
        let apps = status.coolify.filter { $0.resourceStatus != .absent }
        let diskFractions = metrics?.disks.compactMap(\.usedFraction) ?? []
        let memory = metrics?.memory?.usedFraction
        let load = metrics?.loadFraction
        var count = status.services.filter(countsAgainst).count
        count += apps.filter { $0.resourceStatus == .exited || $0.resourceStatus == .unhealthy }.count
        count += diskFractions.filter { $0 >= 0.85 }.count
        if let memory, memory >= 0.85 { count += 1 }
        if let load, load >= 2 { count += 1 }
        let appsUnread = apps.isEmpty || apps.contains { $0.resourceStatus == .unknown }
        let disksUnread = diskFractions.isEmpty || diskFractions.count != metrics?.disks.count
        let unread = status.services.isEmpty || appsUnread || disksUnread || memory == nil || load == nil
        if count == 0 { return unread ? .notKnown : .ok }
        return .attention(count, someUnread: unread)
    }

    /// One unit's row.  Its verbs come only from the allowlist,  and Stop stays out of the
    /// menu:  stopping the report timer would blind this very section within 45 minutes,
    /// so Stop stays in the panel,  behind the plan and Touch ID.
    static func unitRow(_ service: ServerService, allowlist: OpsAllowlist?) -> MenuRow {
        let title = "\(OpsNames.unitTitle(service.unit)):  \(service.activeState ?? "not known")"
        guard service.controllable, let verbs = allowlist?.verbs(forServerUnit: service.unit), !verbs.isEmpty else {
            return MenuRow(title: title, actions: [])
        }
        var actions: [MenuAction] = []
        switch service.activeState ?? "" {
        case "active":
            if verbs.contains(.restart) {
                actions.append(unitAction(.serverUnit(verb: .restart, unit: service.unit), word: "Restart"))
            }
        case "failed", "inactive":
            if verbs.contains(.start) {
                actions.append(unitAction(.serverUnit(verb: .start, unit: service.unit), word: "Start"))
            }
        default:
            break
        }
        actions.append(unitAction(.logs(unit: service.unit), word: "Read Logs"))
        return MenuRow(title: title, actions: actions)
    }

    // MARK: An action's state

    /// The line for an action,  under the section of the machine it runs on.
    private static func actionItem(_ state: ServiceActionState?, host: FleetHost) -> MenuItem? {
        guard let state, state.subject.host == host else { return nil }
        let line = FleetActionWords.line(for: state)
        switch state {
        case .waiting:
            // The sheet needs a window,  so this row brings it forward.
            return .command(title: line, .openMuster)
        case .running, .done:
            return .line(line)
        case .failed, .notKnown:
            // The whole reason is in the Services panel,  selectable.
            return .command(title: oneRow(line), .openServices)
        }
    }

    /// A long or many-line reason,  cut to what a menu row can hold.
    static func oneRow(_ text: String) -> String {
        let first = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? text
        guard first.count > reasonLimit else { return first }
        return String(first.prefix(reasonLimit)) + "\u{2026}"
    }

    // MARK: The plain text

    /// The menu as strings,  with each submenu indented,  the way the design writes it.
    static func plainText(_ menu: MachineMenu) -> String {
        var lines: [String] = [FleetHost.mac.heading]
        lines.append(contentsOf: flatten(menu.mac.items, indent: ""))
        lines.append("----------")
        lines.append(FleetHost.server.heading)
        lines.append(contentsOf: flatten(menu.server.items, indent: ""))
        lines.append("----------")
        lines.append("Open Muster")
        lines.append("Launch Muster At Login")
        return lines.joined(separator: "\n")
    }

    private static func flatten(_ items: [MenuItem], indent: String) -> [String] {
        items.flatMap { item -> [String] in
            switch item {
            case .line(let text):
                return [indent + text]
            case .row(let row):
                return [indent + row.title] + row.actions.map { indent + "    " + $0.title }
            case .group(let title, let children):
                return [indent + title] + flatten(children, indent: indent + "    ")
            case .command(let title, _):
                return [indent + title]
            case .action(let action):
                return [indent + action.title]
            }
        }
    }
}
