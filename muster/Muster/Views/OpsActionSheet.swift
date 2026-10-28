import SwiftUI

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
        content.sheet(isPresented: presented) {
            OpsActionSheet()
        }
    }

    private var presented: Binding<Bool> {
        Binding(
            get: { controller.isPresenting },
            set: { newValue in
                if !newValue { controller.dismiss() }
            }
        )
    }
}

/// The plan for one action,  the owner's yes,  and what came of it.
///
/// The sheet only renders what the controller publishes.  The one thing it does itself is
/// ask Touch ID,  between the plan and the run.
struct OpsActionSheet: View {
    @ObservedObject private var controller = OpsController.shared
    /// True while Touch ID is asking.
    @State private var asking = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(controller.phase.action?.title ?? "Action")
                .font(.title3.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            if let action = controller.phase.action {
                Text(action.runsOnServer
                    ? "Runs on the server through the \(OpsControlRoute.serverAlias) key."
                    : "Runs on this Mac.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            content
            if let notice = controller.notice {
                Text(notice)
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            buttons
        }
        .padding(20)
        .frame(minWidth: 520, idealWidth: 560, minHeight: 280)
        .interactiveDismissDisabled(controller.isBusy || asking)
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
        case .review(_, let outcome):
            planBlock(outcome, heading: "The plan")
        case .running(_, let outcome):
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Running.\u{00A0} Closing this sheet does not stop it.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                planBlock(outcome, heading: "Running")
            }
        case .finished(_, let outcome):
            VStack(alignment: .leading, spacing: 8) {
                resultLine(outcome)
                if !outcome.commands.isEmpty {
                    planBlock(outcome, heading: outcome.kind == .planned ? "The plan" : "What ran")
                }
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

    private func planBlock(_ outcome: OpsOutcome, heading: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(heading)
                .font(.headline)
            if let change = outcome.knobChange {
                knobChangeBlock(change, finished: outcome.kind == .done)
            }
            if outcome.commandLines.isEmpty {
                Text("The helper named no command.")
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
            if !outcome.detail.isEmpty {
                Text(outcome.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
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

    private func resultLine(_ outcome: OpsOutcome) -> some View {
        let success = outcome.isSuccess(for: .yes)
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: success ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(success ? Color.green : Color.orange)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(resultTitle(outcome))
                    .font(.callout.weight(.medium))
                if outcome.commands.isEmpty {
                    Text(outcome.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private func resultTitle(_ outcome: OpsOutcome) -> String {
        switch outcome.kind {
        case .done: return outcome.isSuccess(for: .yes) ? "Done." : "The helper said done, but did not say it worked."
        case .planned: return "The plan could not be confirmed."
        case .refused: return "The helper refused the action."
        case .failed: return "The action failed."
        case .unknown: return "The helper's answer could not be read."
        }
    }

    @ViewBuilder
    private var buttons: some View {
        HStack {
            Spacer()
            switch controller.phase {
            case .review:
                Button("Cancel", role: .cancel) { controller.dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(asking)
                Button {
                    Task { await approveAndRun() }
                } label: {
                    Label("Run With Touch ID", systemImage: "touchid")
                }
                .keyboardShortcut(.defaultAction)
                .disabled(asking)
            case .planning, .running:
                Button("Close") { }
                    .disabled(true)
            case .idle, .finished, .failed:
                Button("Close") { controller.dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    /// Ask Touch ID,  then hand the yes to the controller.
    @MainActor
    private func approveAndRun() async {
        guard let action = controller.phase.action else { return }
        asking = true
        let answer = await OwnerCheck.ask(reason: action.title)
        asking = false
        switch answer {
        case .approved:
            controller.confirm()
        case .declined(let reason):
            controller.decline(reason)
        }
    }
}
