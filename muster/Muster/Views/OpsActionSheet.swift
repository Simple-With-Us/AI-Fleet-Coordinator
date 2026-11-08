import AppKit
import SwiftUI

/// Asks before Muster quits while a run it sent has not answered.
@MainActor
enum OpsQuitGuard {
    /// True when Muster may quit:  nothing is in flight,  or the owner chose to quit anyway.
    static func confirmQuit() -> Bool {
        guard let warning = OpsController.shared.quitWarning else { return true }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Quit While An Action Is Running?"
        alert.informativeText = warning
        alert.addButton(withTitle: "Keep Muster Open")
        alert.addButton(withTitle: "Quit Anyway")
        return alert.runModal() == .alertSecondButtonReturn
    }
}

extension View {
    /// Attach once near the root of a window,  so any tab can start an action and the
    /// sheet that shows its plan hangs off the window.
    func opsActionSheet() -> some View {
        modifier(OpsActionSheetModifier())
    }
}

private struct OpsActionSheetModifier: ViewModifier {
    @ObservedObject private var controller = OpsController.shared

    func body(content: Content) -> some View {
        content
            .sheet(isPresented: presented) {
                OpsActionSheet()
            }
            // A run whose sheet was closed keeps a line at the foot of the window,  with its
            // time so far and a way back to the sheet.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if let action = controller.hiddenRun {
                    HiddenRunStrip(action: action)
                }
            }
    }

    private var presented: Binding<Bool> {
        Binding(
            get: { controller.isSheetShown },
            set: { newValue in
                if !newValue { controller.dismiss() }
            }
        )
    }
}

/// The foot-of-window line for a run going on behind a closed sheet.
private struct HiddenRunStrip: View {
    @ObservedObject private var controller = OpsController.shared
    let action: OpsAction

    init(action: OpsAction) {
        self.action = action
    }

    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(line(at: context.date))
                    .font(.callout)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            Button("Show") { controller.showSheet() }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.bar)
        .accessibilityElement(children: .combine)
    }

    private func line(at date: Date) -> String {
        let elapsed = controller.stageStartedAt.map { date.timeIntervalSince($0) } ?? 0
        return "\(action.title) is still running, \(OpsController.durationText(elapsed)) so far."
    }
}

/// The plan for one action,  the owner's yes,  and what came of it.
///
/// The sheet only renders what the controller publishes.  The one thing it does itself is
/// ask for the owner's approval,  between the plan and the run,  and it hands the answer
/// back tied to the plan it asked about.
struct OpsActionSheet: View {
    @ObservedObject private var controller = OpsController.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(controller.phase.action?.title ?? "Action")
                .font(.title3.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            if let action = controller.phase.action {
                VStack(alignment: .leading, spacing: 4) {
                    PlaceBadge(place: action.changes)
                    Text(action.placeSentence)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .help(action.runsOnServer ? "Through the \(OpsControlRoute.serverAlias) key." : "")
            }
            content
            if let notice = controller.notice {
                Label {
                    Text(notice)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "info.circle")
                        .accessibilityHidden(true)
                }
                .font(.callout)
                .foregroundStyle(.orange)
            }
            Spacer(minLength: 0)
            buttons
        }
        .padding(20)
        .frame(minWidth: 520, idealWidth: 560, minHeight: 280)
        .interactiveDismissDisabled(controller.isAsking)
    }

    /// What a redeploy restarts,  as the panel that planned it noted:  every container of the
    /// application,  not only the row its button was on.  Nil for any other action.
    private static func redeployScope(_ action: OpsAction) -> String? {
        switch action {
        case .coolifyRedeploy(let uuid, _):
            return RedeployScopeNotes.shared.scope(for: uuid)?.sentence
        default:
            return nil
        }
    }

    @ViewBuilder
    private var content: some View {
        switch controller.phase {
        case .idle:
            EmptyView()
        case .planning:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Reading the plan.\u{00A0} Nothing runs yet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        case .review(let action, let outcome):
            VStack(alignment: .leading, spacing: 10) {
                Text("The Plan")
                    .font(.headline)
                Text(action.effectSentence)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                if let scope = Self.redeployScope(action) {
                    Text(scope)
                        .font(.callout.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)
                }
                // A stop takes a service down at once,  so its warning is on the plan,  the
                // same words the alert uses on the direct route.
                if let warning = action.stopWarning {
                    Label {
                        Text(warning)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                            .accessibilityHidden(true)
                    }
                    .font(.callout)
                }
                if let change = outcome.knobChange {
                    knobChangeBlock(change, finished: false)
                }
                commandsDisclosure(outcome, title: "Exact Commands")
                // A helper that says where it ran is checked:  a mismatch never reaches
                // this phase.  A helper that does not say shows nothing.
                if outcome.answeredFrom == action.runsOn {
                    Text(action.answeredSentence)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        case .running(let action, let outcome):
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 8) {
                    ProgressView().controlSize(.small)
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(runningSentence(action, at: context.date))
                            .font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Text(action.effectSentence)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let scope = Self.redeployScope(action) {
                    Text(scope)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                commandsDisclosure(outcome, title: "The Approved Commands")
            }
        case .finished(let action, let outcome):
            VStack(alignment: .leading, spacing: 10) {
                if controller.isRechecking {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Checking the recorded update.\u{00A0} No update is started.")
                            .font(.callout)
                    }
                }
                resultLine(outcome, action: action)
                if outcome.differsFromReview {
                    Label {
                        Text("\(action.runsOn == .mac ? "This Mac" : "The server") reported other commands than the plan you approved.\u{00A0} Check them below.")
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .accessibilityHidden(true)
                    }
                    .font(.callout)
                    .foregroundStyle(.orange)
                }
                if case .serverUpdate = action, outcome.kind == .submitted,
                   outcome.invocationID == nil || outcome.baselineStartMonotonic == nil {
                    Text("The server did not return a receipt for this update, so Muster cannot check it again.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let change = outcome.knobChange {
                    knobChangeBlock(change, finished: outcome.kind == .done)
                }
                if let logText = outcome.logText {
                    logBlock(logText, lines: outcome.logLines)
                } else if !outcome.commands.isEmpty {
                    commandsDisclosure(outcome, title: outcome.commandsHeading,
                                       expanded: outcome.differsFromReview)
                }
                receiptDetails(outcome, action: action)
            }
        case .failed(_, let reason):
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "xmark.octagon.fill")
                    .foregroundStyle(.red)
                    .accessibilityHidden(true)
                Text(reason)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
    }

    private func runningSentence(_ action: OpsAction, at date: Date) -> String {
        let stage = controller.runStage ?? .checkingPlan
        let elapsed = controller.stageStartedAt.map { date.timeIntervalSince($0) } ?? 0
        return stage.sentence(for: action, elapsed: elapsed)
    }

    /// The exact commands,  folded away:  the plain sentence above says what they do,  and
    /// the commands are here for the owner who wants them.
    private func commandsDisclosure(_ outcome: OpsOutcome, title: String, expanded: Bool = false) -> some View {
        CommandsDisclosure(outcome: outcome, title: title, startsExpanded: expanded)
    }

    /// The fixed ids a run left,  for matching it with the logs:  folded away,  since they
    /// mean nothing at a glance.
    @ViewBuilder
    private func receiptDetails(_ outcome: OpsOutcome, action: OpsAction) -> some View {
        // The helper writes its row on the machine it ran on.
        let place = action.runsOn.inProse
        let lines = [
            outcome.invocationID.map { "Update receipt: \($0)" },
            outcome.serverAuditID.map { "Log row \(place): \($0)" },
            outcome.serverAuditTarget.map { "That row names: \($0)" },
        ].compactMap { $0 }
        if !lines.isEmpty {
            DisclosureGroup("Details") {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(lines, id: \.self) { line in
                        Text(line)
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.callout)
        }
    }

    /// A logs run's journal text:  scrollable and selectable,  so the owner can copy it.
    private func logBlock(_ text: String, lines: Int?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(lines.map { "The Last \($0) Journal Lines" } ?? "The Last Journal Lines")
                .font(.headline)
            ScrollView {
                Text(text.isEmpty ? "The journal held no lines for this unit." : text)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 320)
            .padding(10)
            .background(Color.secondary.opacity(0.10), in: RoundedRectangle(cornerRadius: 6))
        }
    }

    /// The old and the new value of a knob write,  and where the old one came from,  so the
    /// owner sees what the write replaces before approving it.
    private func knobChangeBlock(_ change: OpsKnobChange, finished: Bool) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("\(change.oldText) \u{2192} \(change.newText)")
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text(change.sourceText)
                .font(.caption)
                .foregroundStyle(change.oldSource == "infisical" ? Color.secondary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
            if finished, let warning = change.lkgWarning {
                Text(warning)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func resultLine(_ outcome: OpsOutcome, action: OpsAction) -> some View {
        let success = outcome.isSuccess(for: .yes)
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: resultSymbol(outcome))
                .foregroundStyle(success ? Color.green : Color.orange)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(outcome.resultTitle(for: action))
                    .font(.callout.weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)
                if let advice = outcome.advice(for: action) {
                    Text(advice)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let progress = outcome.progressNote {
                    Text(progress)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if outcome.showsDetailUnderTitle {
                    Text(outcome.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
    }

    /// A symbol that differs by shape and not only by colour.
    private func resultSymbol(_ outcome: OpsOutcome) -> String {
        if outcome.isSuccess(for: .yes) { return "checkmark.circle.fill" }
        if outcome.runNotKnown { return "questionmark.circle.fill" }
        if outcome.kind == .submitted { return "clock.fill" }
        return "exclamationmark.triangle.fill"
    }

    @ViewBuilder
    private var buttons: some View {
        HStack {
            Spacer()
            switch controller.phase {
            case .review(let action, _):
                Button("Cancel", role: .cancel) { controller.dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(controller.isAsking)
                Button {
                    Task { await approveAndRun() }
                } label: {
                    Label(action.runButtonTitle, systemImage: "touchid")
                }
                .keyboardShortcut(.defaultAction)
                .disabled(controller.isAsking)
            case .planning:
                // A plan changes nothing,  so closing gives it up.
                Button("Cancel", role: .cancel) { controller.dismiss() }
                    .keyboardShortcut(.cancelAction)
            case .running:
                // The run goes on without the sheet,  and the sheet comes back when it ends.
                Button("Close") { controller.dismiss() }
                    .keyboardShortcut(.cancelAction)
            case .idle, .finished, .failed:
                if controller.canRecheckUpdate {
                    Button("Recheck Update") { controller.recheckSubmittedUpdate() }
                        .disabled(controller.isRechecking)
                }
                Button("Close") { controller.dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    /// Ask for the owner's approval,  then hand the answer back with the plan it was about.
    @MainActor
    private func approveAndRun() async {
        guard let request = controller.beginApproval() else { return }
        let answer = await OwnerCheck.ask(reason: request.action.title)
        controller.finishApproval(request, answer: answer)
    }
}

/// A folded list of the exact commands,  with the helper's own note under them.
private struct CommandsDisclosure: View {
    let outcome: OpsOutcome
    let title: String
    @State private var expanded: Bool

    init(outcome: OpsOutcome, title: String, startsExpanded: Bool) {
        self.outcome = outcome
        self.title = title
        _expanded = State(initialValue: startsExpanded)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 6) {
                if outcome.commandLines.isEmpty {
                    Text("No commands were listed.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(outcome.commandLines.enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(.system(size: 11, design: .monospaced))
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.secondary.opacity(0.10), in: RoundedRectangle(cornerRadius: 6))
                }
                // A plan's note is the helper's own word for the route,  such as "user unit".
                // A result's words are already above,  in the result line.
                if outcome.kind == .planned, !outcome.detail.isEmpty {
                    Text(outcome.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, 4)
        } label: {
            Text(title)
                .font(.headline)
        }
    }
}
