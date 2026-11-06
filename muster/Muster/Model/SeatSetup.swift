import Foundation

/// What the Seat Setup panel says about one seat, built from the engine's cells and from
/// nothing else.
///
/// The panel is the same for every platform:  a seat's setup is the six config kinds the
/// engine checks (instructions, hooks, MCP, skills, sub-agents and permissions), each with
/// the rules behind it and the file each rule is about, so it never depends on how one
/// platform lays its files out.  The engine's manifest names every seat's files, which is
/// how a platform with a different layout is covered without a line of code for it here.
///
/// This is display data and never a verdict:  the states come from the engine.
enum SeatSetup {
    /// One thing on a seat that needs a look, in plain words.
    struct Attention: Equatable {
        let kind: ConfigKind
        /// The cell's state, as the grid prints it.
        let state: String
        /// What the engine said about the worst rule behind the cell.
        let text: String
    }

    /// What a kind with no answer yet says while the engine has not finished a run.
    static let waitingForEngine = "Waiting for the engine to answer."

    /// The seat the panel shows:  the one chosen before when it is still on the roster,
    /// else the first seat on it, and nil when the roster is empty.
    static func resolvedSeat(stored: String, roster: [String]) -> String? {
        roster.contains(stored) ? stored : roster.first
    }

    /// The platform a seat runs on, as the engine's rules name it.
    static func platform(of cells: [GridCell]) -> String? {
        cells.lazy.flatMap { $0.rules }.first?.platform
    }

    /// Whether a cell is something to look at.
    ///
    /// A state the platform simply does not have (UNSUPPORTED), a pair that does not apply
    /// (COVERED) and a seat with no guard to check are facts about the platform and not
    /// problems with the seat, so they are shown in the panel and are not raised here.
    static func needsALook(_ cell: GridCell) -> Bool {
        if cell.isNoGuard { return false }
        switch cell.state {
        case .missing, .drift, .unknown, .unproven:
            return true
        case .passed, .unsupported, .covered:
            return false
        }
    }

    /// The cells of a seat that need a look, in the grid's column order.
    ///
    /// `answered` is whether the engine has finished a run.  Until it has, the grid holds an
    /// empty UNKNOWN cell for every seat and kind, which is silence and not a missing rule, so
    /// those placeholders are never raised.  A cell that carries rules is a real observation
    /// from an earlier run and is raised either way, so a run in flight does not hide it.
    static func attention(for cells: [GridCell], answered: Bool = true) -> [Attention] {
        ConfigKind.columnOrder.compactMap { kind -> Attention? in
            guard let cell = cells.first(where: { $0.kind == kind }), needsALook(cell) else { return nil }
            if !answered && cell.rules.isEmpty { return nil }
            // The rules are worst first, so the first one is the reason the cell is not green.
            // A cell with no rule at all is a hole in the coverage:  the grid counts it as
            // unknown, and the panel says why instead of showing a blank.
            let reason = cell.askedRules.first?.reason ?? cell.rules.first?.reason ?? ""
            let text: String
            if cell.rules.isEmpty {
                text = "The engine has no rule for this kind on this seat, so nothing was checked."
            } else {
                text = reason.isEmpty ? "The engine gave no reason." : reason
            }
            return Attention(kind: kind, state: cell.stateFullName, text: text)
        }
    }

    /// The one line a kind's folded panel shows under its title.
    static func summary(for cell: GridCell?, answered: Bool = true) -> String {
        // A cell with no rules before the engine has answered is not a missing rule.
        if !answered, cell?.rules.isEmpty ?? true { return waitingForEngine }
        guard let cell else { return "The engine has no rule for this seat and kind." }
        if cell.rules.isEmpty {
            return cell.coverReason ?? "No rule covers this seat and kind."
        }
        if cell.isNoGuard {
            return "No guard to check on this platform."
        }
        let asked = cell.askedRules.count
        let rules = asked == 1 ? "1 rule" : "\(asked) rules"
        let checked = cell.checkedAt == nil ? "not checked yet" : "checked \(cell.ageText) ago"
        return "\(cell.stateFullName), \(rules), proof \(cell.proofBadgeText), \(checked)"
    }
}
