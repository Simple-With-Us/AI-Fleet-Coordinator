import Foundation

/// One red cell,  as the badge and the menu show it.
struct DriftRedCell: Identifiable, Equatable {
    /// The seat the cell belongs to.
    let seat: String
    /// The config kind the cell is about.
    let kind: String
    /// The rule that made the cell red,  when there is one.
    let ruleID: String?
    /// The state the cell is in.
    let state: CellState

    /// The cell's identity,  which is the seat and the kind.
    var id: String { "\(seat)/\(kind)" }

    /// The kind as the column names it.
    var kindTitle: String { ConfigKind(rawValue: kind)?.title ?? kind }
}

/// One drift:  a cell that was not red and became red.
///
/// The record is what the history keeps and what the notification names,  so it
/// carries the seat,  the check that turned red and the two states it moved
/// between.  A recovery is not a drift and is not recorded here:  the history is
/// the list of the times something went red,  and a cell that recovered is visible
/// in the grid.
struct DriftTransition: Codable, Identifiable, Equatable {
    /// The transition's own identity.
    let id: UUID
    /// When the cell turned red.
    let at: Date
    /// The seat the cell belongs to.
    let seat: String
    /// The config kind the cell is about.
    let kind: String
    /// The rule that turned red,  when the cell has one.
    let ruleID: String?
    /// The state the cell was in,  when it was in one.
    let fromState: String?
    /// The state the cell is in.
    let toState: String

    /// The cell the transition is about.
    var cellID: String { "\(seat)/\(kind)" }

    /// The kind as the column names it.
    var kindTitle: String { ConfigKind(rawValue: kind)?.title ?? kind }

    /// The check that turned red,  in words.
    ///
    /// A cell with no rule behind it is a hole in the coverage,  and the words say
    /// that instead of naming a rule that does not exist.
    var checkText: String {
        ruleID ?? "no rule covers this pair"
    }

    /// The state the cell came from,  as the history shows it.
    var fromText: String { fromState ?? "—" }

    /// The time of the transition,  on Jay's clock.
    var timeText: String { FleetTime.clock(at) }
}

/// Finds the cells that turned red between two runs.
///
/// The rule is narrow on purpose:  a cell that is already red does not count,  and
/// a cell that recovered and turned red again counts once more.  That is what makes
/// one notice per event instead of one notice per run.
enum DriftTransitionFinder {
    /// The cells that turned red.
    ///
    /// The previous states are the last run that could be trusted.  A cell that was
    /// not there before counts as not red:  a seat that joins the grid with a
    /// failure has turned red,  and the owner has not seen it before.
    static func transitions(
        previous: [String: CellState],
        current: [GridCell],
        now: Date = Date()
    ) -> [DriftTransition] {
        var found: [DriftTransition] = []
        for cell in current {
            guard cell.state.isRed else { continue }
            let was = previous[cell.id]
            guard !(was?.isRed ?? false) else { continue }
            found.append(
                DriftTransition(
                    id: UUID(),
                    at: now,
                    seat: cell.seat,
                    kind: cell.kind.rawValue,
                    ruleID: cell.rules.first?.ruleId,
                    fromState: was?.rawValue,
                    toState: cell.state.rawValue
                )
            )
        }
        return found
    }

    /// The states of a run,  by cell.
    static func states(of cells: [GridCell]) -> [String: CellState] {
        var map: [String: CellState] = [:]
        for cell in cells { map[cell.id] = cell.state }
        return map
    }
}
