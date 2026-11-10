import Foundation

// The fleet-wide compliance rules' results (GRID-18).
//
// The engine runs every implemented compliance rule once per scope and writes one result for
// each, with the kind `compliance` and no seat.  The grid's cells are one seat against one
// config kind, so these results reach no cell.  They used to be dropped there, and a list of
// the rules read only the manifest, so a failing secrets or certificate check sat beside a
// green word and was never alerted.  This file keeps each rule's real state, and the alert
// path reads failing results the same way it reads red cells.

extension CheckResult {
    /// The kind the engine writes on a fleet-wide compliance result.
    static let complianceKind = "compliance"

    /// Whether this result is for a fleet-wide compliance rule rather than a seat's config.
    var isCompliance: Bool { kind == Self.complianceKind }

    /// Whether this compliance result counts as failing.
    ///
    /// The engine's own `failing` flag decides first:  DRIFT, MISSING, and an UNKNOWN that came
    /// from an error, a timeout or an answer it could not read.  A state the grid paints red
    /// counts too, so an UNKNOWN the engine did not flag is never read as passing.
    var failsCompliance: Bool {
        failing || CellState(checkState: checkState).isRed
    }
}

/// One fleet-wide compliance rule, with the state the engine's last answer gave it.
///
/// The state never comes from the manifest alone:  a rule the engine has not run, or cannot
/// run yet, has no result, and it never reads as passing.
struct ComplianceRuleStatus: Identifiable, Equatable {
    /// Where the rule stands.
    enum Standing: Equatable {
        /// The engine ran the rule, and this is the worst state its results gave.
        case answered(CellState)
        /// The engine has no check for the rule yet, with the reason the manifest or the
        /// engine gave, when one did.
        case notBuilt(reason: String?)
        /// The rule can run, and the engine's last answer has no result for it.
        case notInAnswer
        /// The engine has not answered:  no run has landed, or the last one failed.
        case notAnswered
    }

    let ruleID: String
    /// The seat that answers for the rule, when the manifest names one.
    let ownerSeat: String?
    /// The hosts the rule is about, as the manifest names them (`mac`, `hetzner`, `fleet`).
    let scope: [String]
    let standing: Standing
    /// The engine's results for the rule, one for each scope it ran on, worst first.
    let results: [CheckResult]

    var id: String { ruleID }

    /// Whether the engine answered for the rule and every result passed.
    var isPassing: Bool {
        guard case .answered(let state) = standing else { return false }
        return state == .passed && !results.contains { $0.failsCompliance }
    }

    /// Whether the engine answered for the rule and at least one result failed.
    var isFailing: Bool {
        guard case .answered = standing else { return false }
        return results.contains { $0.failsCompliance }
    }

    /// The word a list shows for the rule.  A result uses the grid's own spelling.
    var word: String {
        switch standing {
        case .answered(let state): return state.gridWord
        case .notBuilt: return ComplianceRuleStatus.notBuiltWord
        case .notInAnswer: return ComplianceRuleStatus.noResultWord
        case .notAnswered: return ComplianceRuleStatus.notCheckedWord
        }
    }

    /// The word for a rule the engine has no check for yet.
    static let notBuiltWord = "NOT BUILT YET"
    /// The word for a rule the engine's answer left out.
    static let noResultWord = "NO RESULT"
    /// The word for a rule before the engine has answered.
    static let notCheckedWord = "NOT CHECKED YET"

    /// What the word means, in a sentence or two.
    var sentence: String {
        switch standing {
        case .answered:
            let reason = (results.first?.reason ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return reason.isEmpty ? "The engine gave no reason." : reason
        case .notBuilt(let reason):
            let base = "The engine has no check for this rule yet, so nothing is claimed about it."
            guard let reason, !reason.isEmpty else { return base }
            return base + GridPresentation.gap + reason
        case .notInAnswer:
            return "The engine's last answer has no result for this rule, so nothing is claimed about it."
        case .notAnswered:
            return "The engine has not answered, so nothing is known about this rule yet."
        }
    }

    /// The places the rule is about, in the app's words.
    var placeWords: String {
        let named = scope.filter { !$0.isEmpty }
        guard !named.isEmpty else { return "No Place Named" }
        return named.map { GridPresentation.scopeWords($0) }.joined(separator: " And ")
    }
}

/// Builds each compliance rule's status from the manifest's list and the engine's answer.
enum ComplianceStatusBuilder {
    /// One status per rule:  the manifest's rules in manifest order, then any rule the
    /// engine answered for that this copy of the manifest does not name, then any rule the
    /// engine says it cannot run that neither names.
    ///
    /// `answered` is whether `results` came from an engine answer at all.  Before the first
    /// answer and after a failed run every built rule reads as not answered.
    static func build(
        rules: [ComplianceRule],
        results: [CheckResult],
        notImplemented: [NotImplementedRule],
        answered: Bool
    ) -> [ComplianceRuleStatus] {
        let compliance = answered ? results.filter { $0.isCompliance } : []
        var byRule: [String: [CheckResult]] = [:]
        var answeredOrder: [String] = []
        for result in compliance {
            if byRule[result.ruleId] == nil { answeredOrder.append(result.ruleId) }
            byRule[result.ruleId, default: []].append(result)
        }
        var engineReasons: [String: String] = [:]
        var engineOrder: [String] = []
        for entry in notImplemented where engineReasons[entry.ruleId] == nil {
            engineReasons[entry.ruleId] = entry.reason
            engineOrder.append(entry.ruleId)
        }

        var statuses: [ComplianceRuleStatus] = []
        var seen: Set<String> = []
        for rule in rules where seen.insert(rule.id).inserted {
            statuses.append(
                status(
                    ruleID: rule.id,
                    ownerSeat: rule.ownerSeat,
                    scope: rule.scope ?? [],
                    declaredBuilt: rule.implemented,
                    declaredReason: rule.implementationReason,
                    engineReason: engineReasons[rule.id],
                    results: byRule[rule.id] ?? [],
                    answered: answered
                )
            )
        }
        for ruleID in answeredOrder where seen.insert(ruleID).inserted {
            statuses.append(
                status(
                    ruleID: ruleID,
                    ownerSeat: nil,
                    scope: [],
                    declaredBuilt: nil,
                    declaredReason: nil,
                    engineReason: engineReasons[ruleID],
                    results: byRule[ruleID] ?? [],
                    answered: answered
                )
            )
        }
        for ruleID in engineOrder where seen.insert(ruleID).inserted {
            statuses.append(
                status(
                    ruleID: ruleID,
                    ownerSeat: notImplemented.first(where: { $0.ruleId == ruleID })?.ownerSeat,
                    scope: [],
                    declaredBuilt: nil,
                    declaredReason: nil,
                    engineReason: engineReasons[ruleID],
                    results: [],
                    answered: answered
                )
            )
        }
        return statuses
    }

    /// One rule's status.
    ///
    /// A result always wins:  it is what the engine actually found, even when the copy of
    /// the manifest this app holds says the rule is not built.  With no result, a rule the
    /// manifest or the engine says is not built reads as not built, and any other rule reads
    /// as not answered or not in the answer.  None of those is a pass.
    private static func status(
        ruleID: String,
        ownerSeat: String?,
        scope: [String],
        declaredBuilt: Bool?,
        declaredReason: String?,
        engineReason: String?,
        results: [CheckResult],
        answered: Bool
    ) -> ComplianceRuleStatus {
        let sorted = results.sorted(by: worseFirst)
        let places = scope.isEmpty ? uniquePlaces(of: sorted) : scope
        let standing: ComplianceRuleStatus.Standing
        if !sorted.isEmpty {
            standing = .answered(GridBuilder.worstState(of: sorted))
        } else if declaredBuilt == false || engineReason != nil {
            standing = .notBuilt(reason: firstText(declaredReason, engineReason))
        } else if !answered {
            standing = .notAnswered
        } else {
            standing = .notInAnswer
        }
        return ComplianceRuleStatus(
            ruleID: ruleID,
            ownerSeat: ownerSeat,
            scope: places,
            standing: standing,
            results: sorted
        )
    }

    /// Worst state first, then by the place each result ran on, so the order never depends
    /// on the order the engine wrote them in.
    private static func worseFirst(_ left: CheckResult, _ right: CheckResult) -> Bool {
        let leftRank = GridBuilder.severityIndex(of: CellState(checkState: left.checkState))
        let rightRank = GridBuilder.severityIndex(of: CellState(checkState: right.checkState))
        if leftRank != rightRank { return leftRank < rightRank }
        if left.failsCompliance != right.failsCompliance { return left.failsCompliance }
        return left.platform < right.platform
    }

    /// The places a rule's results ran on, each once, in order.
    private static func uniquePlaces(of results: [CheckResult]) -> [String] {
        var seen: Set<String> = []
        return results.map(\.platform).filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    /// The first of two texts that says something.
    private static func firstText(_ first: String?, _ second: String?) -> String? {
        for text in [first, second] {
            if let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return text }
        }
        return nil
    }
}

/// One compliance result that is failing now, for the menu and the panel.
struct DriftComplianceFailure: Identifiable, Equatable {
    let ruleID: String
    /// The scope the result ran on, as the engine names it (`mac`, `hetzner`).
    let place: String
    /// The engine's state for the result.
    let state: String
    /// What the engine said.
    let reason: String

    var id: String { ComplianceAlertFinder.key(ruleID: ruleID, place: place) }

    /// The place in the app's words.
    var placeTitle: String { GridPresentation.scopeWords(place) }
}

/// Finds the compliance results that started failing between two trusted runs.
///
/// The rules are the drift alert's own (`DriftTransitionFinder`):  a result that was already
/// failing says nothing again, one that recovers and fails again is a new event, and one that
/// was not there before counts as not failing.  Each rule is judged on each place it ran on,
/// so a rule that fails on two hosts is two events.
enum ComplianceAlertFinder {
    /// What the last trusted run said about one rule on one place.
    struct Mark: Equatable {
        let state: String
        let failing: Bool
    }

    /// The key a result is remembered by:  the rule and the place it ran on.
    static func key(ruleID: String, place: String) -> String { "\(ruleID)@\(place)" }

    /// The key for one result.
    static func key(for result: CheckResult) -> String { key(ruleID: result.ruleId, place: result.platform) }

    /// What a run said about each compliance result.  Results of other kinds are left out.
    static func marks(of results: [CheckResult]) -> [String: Mark] {
        var map: [String: Mark] = [:]
        for result in results where result.isCompliance {
            let mark = Mark(state: result.state, failing: result.failsCompliance)
            let resultKey = Self.key(for: result)
            // A rule written twice for one place keeps the worse of the two.
            if let existing = map[resultKey], existing.failing, !mark.failing { continue }
            map[resultKey] = mark
        }
        return map
    }

    /// The compliance results that are failing in a run, in the order the engine wrote them.
    static func failures(in results: [CheckResult]) -> [DriftComplianceFailure] {
        var seen: Set<String> = []
        return results
            .filter { $0.isCompliance && $0.failsCompliance && seen.insert(key(for: $0)).inserted }
            .map { DriftComplianceFailure(ruleID: $0.ruleId, place: $0.platform, state: $0.state, reason: $0.reason) }
    }

    /// The compliance results that started failing since the previous trusted run.
    ///
    /// The transition's seat is the place in the app's words (a compliance rule is about a
    /// host, not a seat), and its kind is `compliance`, so the history and the notice name
    /// both.
    static func transitions(
        previous: [String: Mark],
        current: [CheckResult],
        now: Date = Date()
    ) -> [DriftTransition] {
        var found: [DriftTransition] = []
        var seen: Set<String> = []
        for result in current where result.isCompliance && result.failsCompliance {
            let resultKey = Self.key(for: result)
            guard seen.insert(resultKey).inserted else { continue }
            let was = previous[resultKey]
            guard !(was?.failing ?? false) else { continue }
            found.append(
                DriftTransition(
                    id: UUID(),
                    at: now,
                    seat: GridPresentation.scopeWords(result.platform),
                    kind: CheckResult.complianceKind,
                    ruleID: result.ruleId,
                    fromState: was?.state,
                    toState: result.state
                )
            )
        }
        return found
    }
}
