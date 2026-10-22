import Foundation

// The plan and apply side of the engine.  The shipped CLI prints the plan and the apply report as
// text (only `status` has JSON), so this file parses that text.  Parsing fails closed:  a plan
// whose entry counts disagree with its own header, or that holds an unrecognised line, is an
// error and can never be applied.

// MARK: - Copy

enum ApplyCopy {
    /// A real U+00A0 followed by a space:  the sentence gap for rendered product copy.
    static let sentenceGap = "\u{00A0} "

    /// Joins sentences with the gap.  A fragment without final punctuation gets a period first,
    /// so engine text such as "user canceled" never runs into the next sentence.
    static func join(_ sentences: String...) -> String {
        sentences.enumerated().map { index, raw in
            var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard index < sentences.count - 1, !text.isEmpty else { return text }
            while let last = text.last, ":;,".contains(last) { text.removeLast() }
            if let last = text.last, !".!?".contains(last) { text.append(".") }
            return text
        }.joined(separator: sentenceGap)
    }

    /// Engine text separates sentences and clauses with two ASCII spaces.  Show them with the
    /// same gap the rest of the UI uses.
    static func normalize(_ text: String) -> String {
        var result = ""
        var iterator = text.makeIterator()
        var pendingSpaces = 0
        var previous: Character?
        func flush() {
            guard pendingSpaces > 0 else { return }
            if pendingSpaces >= 2, let previous, ".!?;:,".contains(previous) {
                result += sentenceGap
            } else {
                result += String(repeating: " ", count: pendingSpaces)
            }
            pendingSpaces = 0
        }
        while let character = iterator.next() {
            if character == " " {
                pendingSpaces += 1
            } else {
                flush()
                result.append(character)
                previous = character
            }
        }
        flush()
        return result
    }
}

// MARK: - Plan

enum ApplyAutofix: Equatable {
    case allowed(persistenceClass: Bool)
    case blocked(reason: String)

    var isAllowed: Bool {
        if case .allowed = self { return true }
        return false
    }
}

struct ApplyPlanEntry: Equatable, Identifiable {
    var state: FleetConfigState
    var ruleID: String
    var kind: String
    var platform: String
    var change: String
    var installer: String?
    var files: [String]
    var autofix: ApplyAutofix

    var id: String { ruleID }

    /// Hooks are the persistence class the engine gates behind Touch ID.  The plan text says so
    /// for an auto-fixable entry;  a hooks entry is treated as persistence class either way.
    var isPersistenceClass: Bool {
        if case .allowed(let persistence) = autofix, persistence { return true }
        return kind == "hooks"
    }
}

enum ApplyPlanError: Error, Equatable, LocalizedError {
    case unrecognized(String)

    var errorDescription: String? {
        switch self {
        case .unrecognized(let detail):
            return ApplyCopy.join(
                "The plan could not be read, so nothing will be applied.",
                detail
            )
        }
    }
}

struct ApplyPlan: Equatable {
    var scope: String
    var declaredEntryCount: Int
    var declaredAutofixCount: Int
    var entries: [ApplyPlanEntry]
    var notes: [String]
    /// The validated engine checkout the plan was read from.  Set by the coordinator.
    var root: FleetConfigRootStatus?

    var autofixable: [ApplyPlanEntry] { entries.filter { $0.autofix.isAllowed } }
    var needsTouchID: Bool { autofixable.contains { $0.isPersistenceClass } }

    func entry(ruleID: String) -> ApplyPlanEntry? {
        entries.first { $0.ruleID == ruleID }
    }

    /// Two plans with the same fingerprint would write the same files for the same reasons.
    /// Apply compares the fingerprint of a fresh plan with the one the owner reviewed.
    var fingerprint: String {
        let rootLine = root.map { "root|\($0.path)|\($0.head)" } ?? "root|none"
        return ([rootLine] + entries.map { entry in
            [
                entry.ruleID, entry.kind, entry.platform, entry.state.label,
                entry.files.joined(separator: ","),
                entry.installer ?? "",
                entry.autofix.isAllowed ? "auto" : "blocked",
                entry.isPersistenceClass ? "persistent" : "plain",
            ].joined(separator: "|")
        }).joined(separator: "\n")
    }
}

enum ApplyPlanParser {
    static func parse(_ text: String) throws -> ApplyPlan {
        var lines = text.split(omittingEmptySubsequences: true, whereSeparator: \.isNewline).map(String.init)
        guard let headerLine = lines.first, headerLine.hasPrefix("Plan for ") else {
            throw ApplyPlanError.unrecognized("The first line is not a plan header.")
        }
        lines.removeFirst()
        let header = try parseHeader(headerLine)

        var entries: [ApplyPlanEntry] = []
        var current: PartialEntry?
        var notes: [String] = []

        func finish() throws {
            guard let partial = current else { return }
            entries.append(try partial.build())
            current = nil
        }

        for line in lines {
            if line.hasPrefix("      ") {
                guard current != nil else {
                    throw ApplyPlanError.unrecognized("A detail line has no entry above it.")
                }
                try current!.absorb(line.trimmingCharacters(in: .whitespaces))
            } else if line.hasPrefix("  ") {
                let body = line.trimmingCharacters(in: .whitespaces)
                if body.hasPrefix("note:") {
                    try finish()
                    notes.append(value(of: body, after: "note:"))
                } else if body.hasPrefix("nothing to change") {
                    try finish()
                } else {
                    try finish()
                    current = try PartialEntry(headline: body)
                }
            } else {
                throw ApplyPlanError.unrecognized("Unexpected line: \(line.prefix(80)).")
            }
        }
        try finish()

        guard entries.count == header.entries else {
            throw ApplyPlanError.unrecognized(
                "The header names \(header.entries) entries but \(entries.count) were read."
            )
        }
        let autofixCount = entries.filter { $0.autofix.isAllowed }.count
        guard autofixCount == header.autofix else {
            throw ApplyPlanError.unrecognized(
                "The header names \(header.autofix) auto-fixable entries but \(autofixCount) were read."
            )
        }
        return ApplyPlan(
            scope: header.scope,
            declaredEntryCount: header.entries,
            declaredAutofixCount: header.autofix,
            entries: entries,
            notes: notes
        )
    }

    // "Plan for CLAUDE (hooks):  1 entry,  0 auto-fixable."
    private static func parseHeader(_ line: String) throws -> (scope: String, entries: Int, autofix: Int) {
        let normalized = line.replacingOccurrences(of: "  ", with: " ")
        guard let colon = normalized.range(of: ": "),
              normalized.hasSuffix("auto-fixable.") else {
            throw ApplyPlanError.unrecognized("The plan header is not in the expected form.")
        }
        let scope = String(normalized[normalized.index(normalized.startIndex, offsetBy: "Plan for ".count)..<colon.lowerBound])
        let counts = normalized[colon.upperBound...]
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard counts.count == 2,
              let entries = Int(counts[0].split(separator: " ").first ?? ""),
              let autofix = Int(counts[1].split(separator: " ").first ?? "") else {
            throw ApplyPlanError.unrecognized("The plan header counts could not be read.")
        }
        return (scope, entries, autofix)
    }

    fileprivate static func value(of line: String, after label: String) -> String {
        String(line.dropFirst(label.count)).trimmingCharacters(in: .whitespaces)
    }

    private struct PartialEntry {
        var state: FleetConfigState
        var ruleID: String
        var kind: String
        var platform: String
        var change: String?
        var installer: String?
        var files: [String] = []
        var autofix: ApplyAutofix?

        init(headline: String) throws {
            let tokens = headline.split(whereSeparator: \.isWhitespace).map(String.init)
            guard tokens.count == 4 else {
                throw ApplyPlanError.unrecognized("Unexpected line: \(headline.prefix(80)).")
            }
            state = FleetConfigState(rawValue: tokens[0])
            ruleID = tokens[1]
            kind = tokens[2]
            platform = tokens[3]
        }

        mutating func absorb(_ detail: String) throws {
            if detail.hasPrefix("installer:") {
                installer = ApplyPlanParser.value(of: detail, after: "installer:")
            } else if detail.hasPrefix("file:") {
                files.append(ApplyPlanParser.value(of: detail, after: "file:"))
            } else if detail.hasPrefix("autofix:") {
                let rest = ApplyPlanParser.value(of: detail, after: "autofix:")
                if rest.hasPrefix("yes") {
                    autofix = .allowed(persistenceClass: rest.contains("persistence-class"))
                } else if rest.hasPrefix("no") {
                    var reason = rest.dropFirst(2).trimmingCharacters(in: .whitespaces)
                    if reason.hasPrefix("("), reason.hasSuffix(")") {
                        reason = String(reason.dropFirst().dropLast())
                    }
                    autofix = .blocked(reason: reason.isEmpty ? "No reason was given." : reason)
                } else {
                    throw ApplyPlanError.unrecognized("Unexpected autofix line for \(ruleID).")
                }
            } else if change == nil {
                change = detail
            } else {
                throw ApplyPlanError.unrecognized("Unexpected detail line for \(ruleID).")
            }
        }

        func build() throws -> ApplyPlanEntry {
            guard let change, let autofix else {
                throw ApplyPlanError.unrecognized("The entry for \(ruleID) is incomplete.")
            }
            return ApplyPlanEntry(
                state: state, ruleID: ruleID, kind: kind, platform: platform,
                change: change, installer: installer, files: files, autofix: autofix
            )
        }
    }
}

// MARK: - Apply report

struct ApplyReportStep: Equatable {
    var outcome: String
    var ruleID: String
    var kind: String
    var platform: String
    var detail: String
    var files: [String] = []

    var isApplied: Bool { outcome == "applied" }
    var isNotWritten: Bool { outcome == "failed" || outcome == "refused" }
}

struct ApplyReport: Equatable {
    var steps: [ApplyReportStep]
    var refusalReason: String?
    var verdictAfter: String?

    func step(ruleID: String) -> ApplyReportStep? {
        steps.first { $0.ruleID == ruleID }
    }

    static let knownOutcomes: Set<String> = [
        "applied", "already-current", "refused", "failed", "skipped",
    ]

    /// Parses `ApplyResult.render()` text.  Returns nil for output that is not an apply report.
    static func parse(_ text: String) -> ApplyReport? {
        let lines = text.split(omittingEmptySubsequences: true, whereSeparator: \.isNewline).map(String.init)
        guard let first = lines.first, first.hasPrefix("Applied ") else { return nil }
        var steps: [ApplyReportStep] = []
        var refusal: String?
        var verdict: String?

        for line in lines.dropFirst() {
            if line.hasPrefix("      ") {
                let body = line.trimmingCharacters(in: .whitespaces)
                guard !steps.isEmpty else { continue }
                if body.hasPrefix("file:") {
                    steps[steps.count - 1].files.append(ApplyPlanParser.value(of: body, after: "file:"))
                } else if !body.hasPrefix("command:") {
                    steps[steps.count - 1].detail = body
                }
            } else if line.hasPrefix("  ") {
                let body = line.trimmingCharacters(in: .whitespaces)
                if body.hasPrefix("REFUSED:") {
                    refusal = ApplyPlanParser.value(of: body, after: "REFUSED:")
                } else if body.hasPrefix("status after the apply:") {
                    verdict = ApplyPlanParser.value(of: body, after: "status after the apply:")
                } else {
                    let tokens = body.split(whereSeparator: \.isWhitespace).map(String.init)
                    if tokens.count == 4, knownOutcomes.contains(tokens[0]) {
                        steps.append(ApplyReportStep(
                            outcome: tokens[0], ruleID: tokens[1], kind: tokens[2],
                            platform: tokens[3], detail: ""
                        ))
                    }
                }
            }
        }
        return ApplyReport(steps: steps, refusalReason: refusal, verdictAfter: verdict)
    }
}

// MARK: - Outcome and refusal mapping

enum ApplyRefusalKind: Equatable {
    case touchIDDeclined
    case touchIDUnavailable
    case touchIDHelperProblem
    case notAutoFixable
    case planChanged
    case untrustedRepository
    case anotherApplyRunning
    case engineRefused
    case usage

    var title: String {
        switch self {
        case .touchIDDeclined: return "Touch ID Was Not Confirmed"
        case .touchIDUnavailable: return "Touch ID Is Unavailable"
        case .touchIDHelperProblem: return "Touch ID Helper Problem"
        case .notAutoFixable: return "Not Auto-Fixable"
        case .planChanged: return "The Plan Changed"
        case .untrustedRepository: return "Repository Not Trusted"
        case .anotherApplyRunning: return "Another Apply Is Running"
        case .engineRefused: return "Apply Was Refused"
        case .usage: return "Apply Request Was Rejected"
        }
    }

    /// Classifies the engine's refusal text.  The Touch ID gate words its refusals in
    /// scripts/fleet_config/touchid.py;  anything unrecognised is a plain engine refusal.
    static func classify(message: String) -> ApplyRefusalKind {
        let text = message.lowercased()
        if text.contains("touch id helper") { return .touchIDHelperProblem }
        if text.contains("touch id is not available") { return .touchIDUnavailable }
        if text.contains("touch id") { return .touchIDDeclined }
        return .engineRefused
    }
}

/// An apply where the engine wrote some entries and not others.  Every step is accounted for.
struct ApplyPartialResult: Equatable {
    var written: [ApplyReportStep]
    var notWritten: [ApplyReportStep]
    var reproved: FleetConfigResult?

    /// Multi-line text for the sheet:  what was written and what was not.
    var detail: String {
        var lines: [String] = []
        if written.isEmpty {
            lines.append("Nothing was written.")
        } else {
            lines.append("Written:")
            lines += written.map { step in
                "  \(step.ruleID)" + (step.files.isEmpty ? "" : " (" + step.files.map { "~/" + $0 }.joined(separator: ", ") + ")")
            }
        }
        if !notWritten.isEmpty {
            lines.append("Not written:")
            lines += notWritten.map { step in
                "  \(step.ruleID): " + ApplyCopy.normalize(step.detail.isEmpty ? step.outcome : step.detail)
            }
        }
        return lines.joined(separator: "\n")
    }
}

enum ApplyOutcome: Equatable {
    /// Every step the engine took succeeded and the re-proof of the rule is PASS.
    case green(FleetConfigResult)
    /// Apply ran cleanly but the re-proof is not PASS.
    case stillFailing(FleetConfigResult?)
    /// The engine wrote some entries and refused or failed others.
    case partlyApplied(ApplyPartialResult)
    /// The engine or the Touch ID gate refused.  The cell stays as it was.
    case refused(ApplyRefusalKind, String)
    /// An installer failed, the run timed out, or the output was not understood.
    case failed(String)

    var isGreen: Bool {
        if case .green = self { return true }
        return false
    }

    /// One short line for the in-app status and the notification body.
    var summary: String {
        switch self {
        case .green: return "The cell is green again with a fresh proof."
        case .stillFailing(let result):
            if let result { return "Apply finished but the cell is still \(result.state.label.lowercased())." }
            return "Apply finished but the cell could not be re-proved."
        case .partlyApplied(let partial):
            return ApplyCopy.join(
                "Apply was partial:  \(partial.written.count) written, \(partial.notWritten.count) not written.",
                "Nothing was retried."
            )
        case .refused(let kind, _): return ApplyCopy.join("\(kind.title).", "Nothing was retried.")
        case .failed: return ApplyCopy.join("Apply failed.", "Nothing was retried.")
        }
    }

    /// Maps what the engine said to an outcome from EVERY step of the report, not only the target.
    /// Pure, so every path is unit tested.
    static func classify(
        applyResult: FleetConfigCLIResult,
        targetRuleID: String,
        reproved: FleetConfigResult?
    ) -> ApplyOutcome {
        if applyResult.timedOut {
            return .failed(FleetConfigCLIError.timedOut(Int(FleetConfigInvocation.applyTimeout)).localizedDescription)
        }
        if applyResult.endedBySignal {
            return .failed(FleetConfigCLIError.endedBySignal(applyResult.exitCode).localizedDescription)
        }
        let tail = applyResult.stderrTail

        switch applyResult.exitCode {
        case 64:
            return .refused(.usage, tail.isEmpty ? "The engine rejected the request." : tail)
        case 0, 1, 2, 3:
            break
        default:
            return .failed(tail.isEmpty ? "The engine exited \(applyResult.exitCode)." : tail)
        }

        guard let report = ApplyReport.parse(applyResult.stdout) else {
            // No report means the engine stopped before applying (a bad manifest, or the CLI-level
            // refusal).  Exit 2 is the engine's refusal code.
            if applyResult.exitCode == 2 {
                let message = tail.isEmpty ? "The engine refused the run." : tail
                return .refused(ApplyRefusalKind.classify(message: message), message)
            }
            return .failed(tail.isEmpty ? "The engine output was not an apply report." : tail)
        }

        let written = report.steps.filter(\.isApplied)
        let notWritten = report.steps.filter(\.isNotWritten)
        let target = report.step(ruleID: targetRuleID)
        let targetDone = target.map { $0.isApplied || $0.outcome == "already-current" } ?? false

        if targetDone {
            if !notWritten.isEmpty {
                return .partlyApplied(ApplyPartialResult(written: written, notWritten: notWritten, reproved: reproved))
            }
        } else {
            if !written.isEmpty {
                var missing = notWritten
                if let target, !target.isNotWritten { missing.append(target) }
                return .partlyApplied(ApplyPartialResult(written: written, notWritten: missing, reproved: reproved))
            }
            if let target, target.outcome == "skipped" {
                return .refused(.notAutoFixable, target.detail.isEmpty ? "The engine skipped this rule." : target.detail)
            }
            if let target, target.outcome == "failed", report.refusalReason == nil {
                return .failed(target.detail.isEmpty ? "The installer failed." : target.detail)
            }
            if let message = report.refusalReason ?? (target?.isNotWritten == true ? target?.detail : nil) {
                return .refused(ApplyRefusalKind.classify(message: message), message)
            }
            if applyResult.exitCode == 2 {
                let message = "The engine refused the run."
                return .refused(.engineRefused, message)
            }
        }
        if applyResult.exitCode == 2, let message = report.refusalReason {
            return .refused(ApplyRefusalKind.classify(message: message), message)
        }
        guard let reproved else { return .stillFailing(nil) }
        return reproved.isProven ? .green(reproved) : .stillFailing(reproved)
    }
}
