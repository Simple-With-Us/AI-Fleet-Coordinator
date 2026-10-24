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
    var proofLevel: String? {
        let levels = rules.map { $0.proofLevelRequired ?? "L0" }
        return levels.max()
    }

    /// The proof the cell's rules reached, the strongest of them.
    var proofLevelAchieved: String? {
        let levels = rules.compactMap { $0.proofLevelAchieved }
        return levels.max()
    }

    /// The oldest observation behind the cell, which is the one that goes stale first.
    var checkedAt: Date? {
        rules.compactMap { FleetTime.parse($0.checkedAt) }.min()
    }

    /// How old that observation is.
    var ageText: String { FleetTime.ageText(since: checkedAt) }

    /// The text the cell's tooltip carries.
    var tooltip: String {
        if rules.isEmpty {
            return coverReason ?? "\(seat) / \(kind.title):  no rule covers this pair."
        }
        let names = rules.map { $0.ruleId }.joined(separator: ", ")
        return "\(seat) / \(kind.title):  \(state.rawValue)\n\(names)\n\(proofDescription)"
    }

    /// The proof level,  in words.
    ///
    /// `L0` on a badge is the fleet's own shorthand,  and a level nobody has to look
    /// up is the difference between a panel and a puzzle.
    var proofDescription: String {
        let level = proofLevel ?? "L0"
        switch level {
        case "L1":
            return "L1:  a write was sent to the hook and it denied it."
        case "L2":
            return "L2:  a headless canary showed the platform loads the hook."
        default:
            return "L0:  the config is there and matches by hash."
        }
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
                    cells.append(
                        GridCell(
                            seat: seat,
                            kind: kind,
                            state: worstState(of: matching),
                            rules: matching,
                            coverReason: nil
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
        for rule in rules {
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
