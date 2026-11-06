import Foundation

/// The state a grid cell shows.
enum CellState: String {
    case passed = "PASS"
    case drift = "DRIFT"
    case missing = "MISSING"
    case unproven = "UNPROVEN"
    case unsupported = "UNSUPPORTED"
    case unknown = "UNKNOWN"
    case covered = "COVERED"

    /// The engine's own severity order, worst first.
    ///
    /// The engine orders its human table this way, so a cell that holds several rules
    /// shows the worst of them and the grid reads in the same order the CLI reads.
    static let severity: [CellState] = [
        .missing, .drift, .unknown, .unproven, .unsupported, .passed,
    ]

    /// The short label the badge carries.
    var shortLabel: String {
        switch self {
        case .passed: return "PASS"
        case .drift: return "DRIFT"
        case .missing: return "MISS"
        case .unproven: return "UNPRV"
        case .unsupported: return "UNSUP"
        case .unknown: return "UNK"
        case .covered: return "N/A"
        }
    }

    /// Whether the grid paints this state red.
    ///
    /// It is the same two states the badge paints red:  a missing artifact and a
    /// cell the app could not read.  The tint lives in the view and this lives in
    /// the model,  so the badge count and the grid cannot mean two different things:
    /// the count is the number of red cells on the screen.
    var isRed: Bool {
        switch self {
        case .missing, .unknown: return true
        case .passed, .drift, .unproven, .unsupported, .covered: return false
        }
    }
}

/// One cell of the grid:  one seat against one config kind.
struct GridCell: Identifiable {
    let seat: String
    let kind: ConfigKind
    let state: CellState
    /// The rules behind the cell, worst first.
    let rules: [CheckResult]
    /// Why this seat and kind do not apply, when a cover says so.
    let coverReason: String?

    var id: String { "\(seat)/\(kind.rawValue)" }

    /// The proof level the cell's rules require, the strongest of them.
    ///
    /// This is what the cells NEED, and a badge must never print it as if it had
    /// happened:  the badge and the tooltip read `proofLevelAchieved` for that.
    var proofLevel: String? {
        // A rule that does not apply needs no proof, so it does not raise the level.
        let asked = askedRules.isEmpty ? rules : askedRules
        let levels = asked.map { $0.proofLevelRequired ?? "L0" }
        return levels.max()
    }

    /// The proof the cell's rules reached, the strongest of them.
    var proofLevelAchieved: String? {
        let levels = rules.compactMap { $0.proofLevelAchieved }
        return levels.max()
    }

    /// Whether every rule behind the cell says it has no guard to check.
    ///
    /// A seat with no hook mechanism has a rule that only watches for a hook to
    /// appear.  That is not a pass, because no guard was proven, so the cell says so
    /// in words instead of wearing a state.
    var isNoGuard: Bool {
        !rules.isEmpty && rules.allSatisfy { $0.isNotApplicable }
    }

    /// The rules that ask for something to be proven.
    ///
    /// A rule that does not apply (file guarding on a harness whose hook carries no
    /// file tool) asks for nothing, so it is neither proven nor missing.
    var askedRules: [CheckResult] {
        rules.filter { !$0.isNotApplicable }
    }

    /// The rules whose every check was proven.
    var provenRules: [CheckResult] {
        askedRules.filter { $0.checkState == .passed }
    }

    /// Whether some of the cell's rules were proven and some were not.
    ///
    /// A hook that guards the shell and not the files is this:  the shell rule reads
    /// PASS for what it proved, and the cell reads as partly proven, never as a bare
    /// PASS.
    var isPartlyProven: Bool {
        !provenRules.isEmpty && provenRules.count < askedRules.count
    }

    /// The state's name as the badge and the panel print it.
    var stateName: String { isNoGuard ? "NO GUARD" : state.shortLabel }

    /// The state's name in full, for the panel and the tooltip.
    var stateFullName: String { isNoGuard ? "NO GUARD" : state.rawValue }

    /// What the badge prints beside the state:  the proof the cell REACHED.
    ///
    /// A cell that needs L1 and reached nothing prints a dash, and one that reached L0
    /// prints L0, so the badge states what was proven and never what was asked.  A cell
    /// where only some rules were proven says how many.
    var proofBadgeText: String {
        guard !rules.isEmpty, !isNoGuard else { return "—" }
        let reached = proofLevelAchieved ?? "—"
        if isPartlyProven { return "\(reached) \(provenRules.count)/\(askedRules.count)" }
        return reached
    }

    /// The oldest observation behind the cell, which is the one that goes stale first.
    var checkedAt: Date? {
        rules.compactMap { FleetTime.parse($0.checkedAt) }.min()
    }

    /// How old that observation is.
    var ageText: String { FleetTime.ageText(since: checkedAt) }

    /// The text the cell's tooltip carries.
    ///
    /// It says what each rule checked and whether that was proven, so a cell that is
    /// partly proven names the part that is not.
    var tooltip: String {
        if rules.isEmpty {
            return coverReason ?? "\(seat) / \(kind.title):  no rule covers this pair."
        }
        if isNoGuard {
            let why = coverReason ?? rules.first?.reason ?? ""
            return "\(seat) / \(kind.title):  no guard to check.\n\(why)\nNo guard was proven here, so this is not a pass."
        }
        var lines = ["\(seat) / \(kind.title):  \(state.rawValue)"]
        for rule in rules {
            lines.append("\(rule.ruleId):  \(rule.checkLine)")
        }
        lines.append(proofDescription)
        return lines.joined(separator: "\n")
    }

    /// The proof the cell REACHED,  in words,  and the proof it still needs.
    ///
    /// `L0` on a badge is the fleet's own shorthand,  and a level nobody has to look
    /// up is the difference between a panel and a puzzle.  The words come from the
    /// level the rules reached and never from the level they required:  a cell that
    /// needs L1 and reached L0 must not say a write was sent to the hook.
    var proofDescription: String {
        if isPartlyProven {
            // The strongest level any rule reached belongs to the rules that were proven,
            // so a sentence about the whole cell would claim it for the rules that were not.
            let missing = askedRules
                .filter { $0.checkState != .passed }
                .map { $0.guardScope ?? $0.ruleId }
                .joined(separator: ", ")
            return "Partly proven:  \(provenRules.count) of \(askedRules.count) rules reached the proof they need.\u{00A0} Not proven:  \(missing)."
        }
        let needed = proofLevel ?? "L0"
        guard let reached = proofLevelAchieved else {
            return "Not proved.\u{00A0} Needs \(needed):  \(GridCell.levelWords(needed))."
        }
        if GridCell.levelRank(reached) >= GridCell.levelRank(needed) {
            return "Reached \(reached):  \(GridCell.levelWords(reached))."
        }
        return "Reached \(reached):  \(GridCell.levelWords(reached)).\u{00A0} Needs \(needed):  \(GridCell.levelWords(needed))."
    }

    /// What a proof level means,  as the engine proves it.
    ///
    /// L0 compares the config with what the rule expects.  Only the instruction
    /// blocks compare a hash,  so the words do not promise one.
    static func levelWords(_ level: String) -> String {
        switch level {
        case "L1": return "a write was sent to the hook and it denied it"
        case "L2": return "a headless canary showed the platform loads the hook"
        default: return "the config was read and holds what the rule expects"
        }
    }

    /// The order of the proof levels,  so two of them compare without string tricks.
    static func levelRank(_ level: String) -> Int {
        switch level {
        case "L2": return 2
        case "L1": return 1
        default: return 0
        }
    }
}

/// What PASS means,  in one place.
///
/// PASS has one meaning across the app:  every check the cell's rule asks for was
/// proven.  A shell-only guard reads PASS where the rule asks for shell guarding and
/// nowhere else.  Where a panel checks something of its own and PASS would mean less,
/// it states what it checked in these words or uses another word.
enum PassMeaning {
    /// The sentence the legend and the overview carry.
    static let everywhere = "PASS means every check the rule asks for was proven, and nothing more."

    /// What the verdict line at the top of the overview means.
    static func verdict(_ verdict: String?) -> String {
        switch verdict {
        case "PASS": return "Every check every rule asks for was proven."
        case "FAIL": return "At least one check failed or could not be completed."
        case "UNKNOWN": return "Nothing failed, but not every check was proven."
        default: return "No verdict was returned, so nothing is claimed."
        }
    }

    /// What PASS means on a badge whose panel names no more than "this panel's own check".
    static let panelRow = "PASS means every check this panel makes on this row was proven, and nothing else about it."

    /// What PASS on a host in the Fleet Hosts panel (the tools on both machines) covers.
    static let toolsHost = "Checked: the doctor's report is current, the host answered, and none of its tool rows failed, are behind or could not be read."
}

extension CheckResult {
    /// Whether the rule says it has no guard to check, which is not a pass.
    var isNotApplicable: Bool { reasonCode == "not-applicable" }

    /// What a hook rule asks the guard to cover, from the engine's `guards` field.
    ///
    /// `nil` for a rule that names no scope:  such a rule checks one thing and its
    /// reason says which.
    var guardScope: String? {
        guard let names = expected["guards"]?.stringArray, !names.isEmpty else { return nil }
        return names.joined(separator: " and ") + " guarding"
    }

    /// The rule's state and what it checked, in one line.
    var checkLine: String {
        let scope = guardScope.map { " (\($0))" } ?? ""
        if isNotApplicable { return "does not apply\(scope)" }
        return "\(state)\(scope), \(proofText)"
    }
}

/// Builds the grid from the roster, the manifest and the envelope's flat result list.
enum GridBuilder {
    /// One cell per seat and kind, in column order.
    ///
    /// A pair with no rule and no cover is `unknown`, never a blank:  silence is not a
    /// pass, and a hole in the grid is a hole in the coverage, not a healthy cell.
    static func build(
        roster: [String],
        manifest: SeatManifest?,
        results: [CheckResult]
    ) -> [GridCell] {
        var cells: [GridCell] = []
        for seat in roster {
            for kind in ConfigKind.columnOrder {
                let matching = results
                    .filter { $0.seat == seat && $0.kind == kind.rawValue }
                    .sorted(by: worseResultFirst)
                if matching.isEmpty {
                    let reason = manifest?.coverReason(seat: seat, kind: kind)
                    cells.append(
                        GridCell(
                            seat: seat,
                            kind: kind,
                            state: reason == nil ? .unknown : .covered,
                            rules: [],
                            coverReason: reason
                        )
                    )
                } else {
                    // A seat with no guard has a rule that only watches for a hook to
                    // appear, and the manifest's cover says why there is none.  The cell
                    // keeps that sentence, so the reason is not dropped once a rule
                    // matches.
                    let noGuard = matching.allSatisfy { $0.isNotApplicable }
                    cells.append(
                        GridCell(
                            seat: seat,
                            kind: kind,
                            state: worstState(of: matching),
                            rules: matching,
                            coverReason: noGuard
                                ? manifest?.coverReason(seat: seat, kind: kind) : nil
                        )
                    )
                }
            }
        }
        return cells
    }

    /// The worst state among a cell's rules.
    static func worstState(of rules: [CheckResult]) -> CellState {
        var worst = CellState.passed
        // A rule that does not apply asks for nothing, so it cannot make a cell worse.
        // When every rule is one,  the cell keeps their state and says why in words.
        let asked = rules.filter { !$0.isNotApplicable }
        for rule in (asked.isEmpty ? rules : asked) {
            let state = CellState(checkState: rule.checkState)
            if severityIndex(of: state) < severityIndex(of: worst) {
                worst = state
            }
        }
        return worst
    }

    /// The rules ordered worst first, then by rule id, the way the CLI orders its table.
    static func worseResultFirst(_ left: CheckResult, _ right: CheckResult) -> Bool {
        let leftRank = severityIndex(of: CellState(checkState: left.checkState))
        let rightRank = severityIndex(of: CellState(checkState: right.checkState))
        if leftRank != rightRank { return leftRank < rightRank }
        return left.ruleId < right.ruleId
    }

    /// Where a state sits in the severity order.
    static func severityIndex(of state: CellState) -> Int {
        CellState.severity.firstIndex(of: state) ?? CellState.severity.count
    }
}

extension CellState {
    /// The cell state for an engine state.
    ///
    /// The two vocabularies are the same six names;  the conversion exists so a cell
    /// and a rule can never drift apart by being spelled differently.
    init(checkState: CheckState) {
        switch checkState {
        case .passed: self = .passed
        case .drift: self = .drift
        case .missing: self = .missing
        case .unproven: self = .unproven
        case .unsupported: self = .unsupported
        case .unknown: self = .unknown
        }
    }
}
