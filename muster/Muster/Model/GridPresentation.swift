import Foundation

/// The Compliance Grid's words and the small derivations behind them.
///
/// The grid proves one thing:  the seat config files on the Mac, plus the key names the
/// Permissions rules ask the secret store for.  Every sentence that says so lives here, in
/// plain code with no SwiftUI, so the tests can read them and the grid, its detail panel,
/// the Overview and Drift History cannot say it four different ways.
enum GridPresentation {
    /// The sentence gap the owner's prose uses:  a no-break space and a space.
    static let gap = "\u{00A0} "

    // MARK: Scope

    /// The heading above the grid.
    static let scopeHeading = "The Mac's Seat Config"

    /// What the grid checks and what it does not, under its heading.
    static let scopeExplanation = "Every cell is a config file or setting read on the Mac."
        + gap + "The Permissions cells also ask the secret store (Infisical) whether a key name is there."
        + gap + "The server's seat config is not checked here."

    /// The same scope, in one line, under the Overview's seat setup heading.
    static let overviewScope = "The Mac's seat config only."
        + gap + "The server's seat config is not checked here."

    // MARK: The check time

    /// The line beside the table that says when the run checked the rules.
    ///
    /// The view draws it inside a timeline, so the age moves while the grid is open.
    static func checkedLine(commonCheckedAt: Date?, now: Date) -> String? {
        guard let at = commonCheckedAt else { return nil }
        return "Checked on the Mac at \(FleetTime.clock(at)),  \(FleetTime.ageText(since: at, now: now)) ago"
    }

    /// The time a rule's result stops counting as current, when the engine gave one.
    static func expiry(of rule: CheckResult) -> Date? {
        guard let text = rule.expiresAt, !text.isEmpty else { return nil }
        return FleetTime.parse(text)
    }

    /// True when at least one rule behind the cell is past the time limit the engine set.
    ///
    /// After a long sleep every result is past it, and the grid dims the cell rather than
    /// let an old green read as current.
    static func isPastLimit(_ cell: GridCell, now: Date) -> Bool {
        cell.rules.contains { rule in
            guard let limit = expiry(of: rule) else { return false }
            return limit < now
        }
    }

    /// The banner over the grid when some results are past their time limit, or nil.
    static func pastLimitBanner(cells: [GridCell], now: Date) -> String? {
        let late = cells.filter { isPastLimit($0, now: now) }
        guard !late.isEmpty else { return nil }
        let first = late
            .flatMap { $0.rules.compactMap { expiry(of: $0) } }
            .filter { $0 < now }
            .min()
        let noun = late.count == 1 ? "1 cell is" : "\(late.count) cells are"
        let when = first.map { ", the first since \(FleetTime.clock($0))" } ?? ""
        return "\(noun) past the time limit the engine set for the results\(when)."
            + gap + "Read them as not current until the seat setup is checked again."
    }

    // MARK: Where a seat's config lives

    /// Whether a seat's config is on the Mac at all.
    ///
    /// The manifest carries no host field yet, so this is decided from what the manifest
    /// does carry:  a seat with no rule has nothing on the Mac to check, and its covers say
    /// where it runs instead.  The cover's own sentence is shown, never parsed.
    enum SeatLocation: Equatable {
        case onTheMac
        case notOnTheMac(reason: String?)
        case notKnown

        static func of(seat: String, manifest: SeatManifest?) -> SeatLocation {
            guard let manifest, let entry = manifest.seats[seat] else { return .notKnown }
            guard entry.rules.isEmpty else { return .onTheMac }
            let reason = ConfigKind.columnOrder.lazy
                .compactMap { manifest.coverReason(seat: seat, kind: $0) }
                .first
            return .notOnTheMac(reason: reason)
        }

        /// The chip under the seat's tag.
        var chip: String {
            switch self {
            case .onTheMac: return FleetHost.mac.onTitle
            case .notOnTheMac: return "Not \(FleetHost.mac.onTitle)"
            case .notKnown: return "Place Not Known"
            }
        }

        /// The line under a seat's row, when the row needs one to be read right.
        var note: String? {
            switch self {
            case .onTheMac:
                return nil
            case .notOnTheMac(let reason):
                let why = reason.map { GridPresentation.gap + $0 } ?? ""
                return "Not on the Mac, so nothing here is checked for it.\(why)"
            case .notKnown:
                return "The manifest does not name this seat, so where its config lives is not known."
            }
        }

        /// The chip's tooltip.
        var help: String {
            switch self {
            case .onTheMac:
                return "This seat's config files are read on the Mac."
            case .notOnTheMac:
                return "This seat does not run on the Mac, so the Mac holds none of its config."
            case .notKnown:
                return "The manifest does not name this seat."
            }
        }
    }

    /// Where one rule's evidence is read.
    enum RuleLocation: Hashable {
        case macFile
        case secretStore

        static func of(checkType: String) -> RuleLocation {
            checkType == "credential_key_present" ? .secretStore : .macFile
        }

        /// The label at the top of a rule block in the detail panel.
        var label: String {
            switch self {
            case .macFile: return "Read \(FleetHost.mac.onTitle)"
            case .secretStore: return "Read From The Secret Store (Infisical), By Key Name"
            }
        }

        /// What the path under a rule block is.
        var pathLabel: String {
            switch self {
            case .macFile: return "Path on the Mac"
            case .secretStore: return "Store folder"
            }
        }
    }

    /// The first line of a cell's tooltip:  where its rules read, or nil for a cell with none.
    static func locationLine(for cell: GridCell) -> String? {
        guard !cell.rules.isEmpty else { return nil }
        let places = Set(cell.rules.map { RuleLocation.of(checkType: $0.checkType) })
        if places == [.secretStore] {
            return "Read from the secret store (Infisical), asked from the Mac."
        }
        if places.contains(.secretStore) {
            return "Read on the Mac and from the secret store (Infisical)."
        }
        return "Read on the Mac."
    }

    /// The cell's tooltip:  where it reads, whether it is past its limit, then what its
    /// rules found.
    static func tooltip(for cell: GridCell, pastLimit: Bool = false) -> String {
        let late: String? = pastLimit
            ? "Past the time limit the engine set, so this is not current."
            : nil
        return [locationLine(for: cell), late, cell.tooltip].compactMap { $0 }.joined(separator: "\n")
    }

    // MARK: State words

    /// One entry of the state legend.
    struct StateLegendEntry: Identifiable, Equatable {
        /// The state the entry is for, or nil for NO GUARD, which wears no state of its own.
        let state: CellState?
        let word: String
        let symbol: String
        let meaning: String
        let action: String

        var id: String { word }
    }

    /// Every word a badge can show, in the engine's severity order, then the two that are
    /// not results.
    static let stateLegend: [StateLegendEntry] = [
        StateLegendEntry(
            state: .missing, word: CellState.missing.gridWord, symbol: CellState.missing.gridSymbol,
            meaning: "The file, block or key the rule expects is not there.",
            action: "Needs action:  install it."
        ),
        StateLegendEntry(
            state: .drift, word: CellState.drift.gridWord, symbol: CellState.drift.gridSymbol,
            meaning: "The config is there, but differs from what the fleet expects.",
            action: "Needs action:  bring it back in line, or change the rule."
        ),
        StateLegendEntry(
            state: .unknown, word: CellState.unknown.gridWord, symbol: CellState.unknown.gridSymbol,
            meaning: "No answer for this cell, so nothing is claimed about it.",
            action: "Needs a look:  the detail panel says why."
        ),
        StateLegendEntry(
            state: .unproven, word: CellState.unproven.gridWord, symbol: CellState.unproven.gridSymbol,
            meaning: "The config was read, but the proof the rule needs was not reached.",
            action: "Needs a look:  the proof is not there yet."
        ),
        StateLegendEntry(
            state: .unsupported, word: CellState.unsupported.gridWord, symbol: CellState.unsupported.gridSymbol,
            meaning: "The engine cannot check this on this platform yet.",
            action: "No action now:  a known gap."
        ),
        StateLegendEntry(
            state: .passed, word: CellState.passed.gridWord, symbol: CellState.passed.gridSymbol,
            meaning: "Every check the rule asks for was proven, and nothing more.",
            action: "No action."
        ),
        StateLegendEntry(
            state: .covered, word: CellState.covered.gridWord, symbol: CellState.covered.gridSymbol,
            meaning: "The manifest says this kind does not apply to this seat, and says why.",
            action: "No action."
        ),
        StateLegendEntry(
            state: nil, word: GridCell.noGuardWord, symbol: GridCell.noGuardSymbol,
            meaning: "The seat has no guard, so none was checked and none was proven.",
            action: "No action until the platform can run one."
        ),
    ]

    // MARK: The detail panel

    /// The panel's "as of" line, from the run the panel's cell came from.
    static func asOfLine(generatedAt: Date?) -> String {
        guard let generatedAt else { return "Not checked yet" }
        return "As of \(FleetTime.clock(generatedAt))"
    }

    /// The citation a rule carries, or nil when it has none, so the panel shows no empty line.
    static func shownSource(_ source: String?) -> String? {
        guard let source else { return nil }
        let trimmed = source.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// The word a rule's own line shows, in the grid's one spelling.
    static func ruleWord(_ rule: CheckResult) -> String {
        rule.isNotApplicable ? CellState.covered.gridWord : CellState(checkState: rule.checkState).gridWord
    }

    // MARK: The filter

    /// What the grid says when no seat is left to show.
    static func emptyFilterText(
        filter: GridStateFilter,
        search: String,
        rosterCount: Int,
        checkedAt: Date?
    ) -> String {
        if rosterCount == 0 {
            return "The grid has no seats:  neither the registry nor the manifest named one."
        }
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        var parts: [String] = []
        if filter != .all { parts.append("\u{201C}\(filter.rawValue)\u{201D}") }
        if !query.isEmpty { parts.append("a tag with \u{201C}\(query)\u{201D}") }
        let what = parts.isEmpty ? "this filter" : parts.joined(separator: " and ")
        var text = "No seat matches \(what)."
        if filter != .all, query.isEmpty, let checkedAt {
            text += gap + "Nothing to show here in the check at \(FleetTime.clock(checkedAt))."
        }
        return text
    }

    // MARK: Watching

    /// What the drift watcher is set to see, and whether that is worth a warning.
    struct WatchLine: Equatable {
        enum Tone: Equatable {
            /// Paths are set to be watched.
            case watching
            /// Nothing is watched, so a change is not seen when it happens.
            case warning
        }

        let text: String
        let tone: Tone

        /// The line, from the monitor's own numbers.
        ///
        /// The path count is the number of paths the manifest names on the Mac, which is
        /// what the watcher is asked to watch.  Zero is a warning, never a green.
        static func make(
            isWatching: Bool,
            pathCount: Int,
            seatCount: Int,
            recheckSeconds: TimeInterval
        ) -> WatchLine {
            let minutes = max(1, Int((recheckSeconds / 60).rounded()))
            let every = minutes == 1 ? "every minute" : "every \(minutes) minutes"
            guard isWatching else {
                return WatchLine(
                    text: "Not watching:  a change to a seat's config is seen only when the seat setup is checked again.",
                    tone: .warning
                )
            }
            guard pathCount > 0 else {
                return WatchLine(
                    text: "Watching nothing:  the manifest names no path on the Mac to watch, so a change is seen only at the check \(every).",
                    tone: .warning
                )
            }
            let paths = pathCount == 1 ? "1 path" : "\(pathCount) paths"
            let seats = seatCount == 1 ? "1 seat" : "\(seatCount) seats"
            return WatchLine(
                text: "Set to watch \(paths) on the Mac for \(seats)." + GridPresentation.gap
                    + "Secret store checks cannot be watched, so they are checked again \(every).",
                tone: .watching
            )
        }
    }

    // MARK: Compliance rules

    /// The heading over the manifest's fleet-wide rules.
    static let complianceHeading = "Compliance Rules In The Manifest"

    /// What that list is, and what it is not.
    static let complianceExplanation = "These rules are listed from fleet-seats.json."
        + gap + "Their results are not shown in this grid, so a rule here says only whether its check exists, never that it passed."

    /// The status a compliance rule shows, in place of a result the app does not have.
    static func complianceStatus(_ rule: ComplianceRule) -> (text: String, isBuilt: Bool) {
        if rule.implemented == false { return ("NOT BUILT YET", false) }
        return ("CHECK EXISTS, RESULT NOT SHOWN", true)
    }

    /// One scope value from the manifest, in the app's place words.
    static func scopeWords(_ raw: String) -> String {
        switch raw.lowercased() {
        case "mac": return FleetPlace.machine(.mac).title
        case "hetzner", "server": return FleetPlace.machine(.server).title
        case "fleet": return FleetPlace.acrossFleet.title
        default: return raw
        }
    }

    /// The compliance rules under one place.
    struct ComplianceGroup: Identifiable, Equatable {
        let heading: String
        let ruleIDs: [String]
        var id: String { heading }
    }

    /// The rules grouped by the places they are about, each rule once, in manifest order
    /// within a group.
    static func complianceGroups(_ rules: [ComplianceRule]) -> [ComplianceGroup] {
        var order: [String] = []
        var members: [String: [String]] = [:]
        var rank: [String: Int] = [:]
        for rule in rules {
            let scopes = (rule.scope ?? []).filter { !$0.isEmpty }
            let heading = scopes.isEmpty ? "No Place Named" : scopes.map { scopeWords($0) }.joined(separator: " And ")
            if members[heading] == nil {
                order.append(heading)
                rank[heading] = scopeRank(scopes)
            }
            members[heading, default: []].append(rule.id)
        }
        let sorted = order.enumerated().sorted { lhs, rhs in
            let left = rank[lhs.element] ?? 9
            let right = rank[rhs.element] ?? 9
            return left == right ? lhs.offset < rhs.offset : left < right
        }
        return sorted.map { ComplianceGroup(heading: $0.element, ruleIDs: members[$0.element] ?? []) }
    }

    /// Where a group sorts:  the Mac, then the server, then the fleet, then the rest.
    private static func scopeRank(_ scopes: [String]) -> Int {
        guard let first = scopes.first?.lowercased() else { return 4 }
        switch first {
        case "mac": return 0
        case "hetzner", "server": return 1
        case "fleet": return 2
        default: return 3
        }
    }
}

// MARK: - The grid's one spelling of each state

extension CellState {
    /// The one word the grid, its legend and its detail panel use for this state.
    ///
    /// The six results keep the engine's own words, which the rules, the tooltips and Drift
    /// History already print.  The cover is not a result, so it says what it means.
    var gridWord: String {
        switch self {
        case .covered: return "DOES NOT APPLY"
        case .passed, .drift, .missing, .unproven, .unsupported, .unknown: return rawValue
        }
    }

    /// The symbol beside the word, so a state never rests on its colour alone.
    var gridSymbol: String {
        switch self {
        case .passed: return "checkmark.circle.fill"
        case .drift: return "arrow.left.arrow.right.circle.fill"
        case .missing: return "xmark.circle.fill"
        case .unproven: return "questionmark.diamond.fill"
        case .unsupported: return "minus.circle.fill"
        case .unknown: return "exclamationmark.triangle.fill"
        case .covered: return "circle.dashed"
        }
    }
}

extension GridCell {
    /// The word for a cell whose seat has no guard to check.
    static let noGuardWord = "NO GUARD"
    /// Its symbol.
    static let noGuardSymbol = "shield.slash"

    /// The word the badge and the panel show.
    var gridWord: String { isNoGuard ? Self.noGuardWord : state.gridWord }

    /// The symbol the badge shows.
    var gridSymbol: String { isNoGuard ? Self.noGuardSymbol : state.gridSymbol }
}

/// The cell the detail panel is open on, by identity, never by copy.
///
/// The panel looks the cell up in the store on every render, so a run that lands while it
/// is open shows the new cell, and its file read is keyed on the run as well as the cell.
struct GridSelection: Hashable {
    let seat: String
    let kind: ConfigKind

    init(seat: String, kind: ConfigKind) {
        self.seat = seat
        self.kind = kind
    }

    init(_ cell: GridCell) {
        self.init(seat: cell.seat, kind: cell.kind)
    }

    /// The cell's id, which is the grid's own.
    var cellID: String { "\(seat)/\(kind.rawValue)" }

    /// The key the panel's file read runs under:  the cell and the run it came from.
    func readKey(generatedAt: Date?) -> String {
        let run = generatedAt.map { String($0.timeIntervalSince1970) } ?? "no-run"
        return "\(cellID)@\(run)"
    }

    /// True while the seat's row is on screen, so a filter that hides it closes the panel.
    func isShown(in visibleRoster: [String]) -> Bool {
        visibleRoster.contains(seat)
    }
}
