import SwiftUI

// The plan preview, the single confirmation, and the in-app status of applies in flight.  Every
// piece of state lives in `ApplyCoordinator`, so closing the sheet or the window changes nothing
// about an apply that has started.

// MARK: - Entry points for other views

/// A button for a red cell.  The grid passes the seat and the rule id (and the kind when it has
/// it);  the button opens the plan preview through the coordinator.
struct ApplyFixButton: View {
    let seat: String
    let ruleID: String
    var kind: String?

    @ObservedObject private var coordinator = ApplyCoordinator.shared

    var body: some View {
        Button("Review Fix") {
            coordinator.beginPlan(seat: seat, ruleID: ruleID, kind: kind)
        }
        .help("Show the planned writes for this cell before anything changes.")
    }
}

extension View {
    /// Attach once near the root of a window so any view can present a plan.
    func applyPlanSheet() -> some View {
        modifier(ApplyPlanSheetModifier())
    }
}

private struct ApplyPlanSheetModifier: ViewModifier {
    @ObservedObject private var coordinator = ApplyCoordinator.shared

    func body(content: Content) -> some View {
        content.sheet(item: presented) { session in
            ApplyPlanView(sessionID: session.id)
        }
    }

    private var presented: Binding<ApplySession?> {
        Binding(
            get: { coordinator.presentedSessionID.flatMap { coordinator.session(id: $0) } },
            set: { newValue in
                if newValue == nil, let id = coordinator.presentedSessionID {
                    coordinator.dismiss(sessionID: id)
                }
            }
        )
    }
}

// MARK: - Plan sheet

struct ApplyPlanView: View {
    let sessionID: UUID
    @ObservedObject private var coordinator = ApplyCoordinator.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let session = coordinator.session(id: sessionID) {
                header(session)
                Divider()
                content(session)
                Spacer(minLength: 0)
                footer(session)
            } else {
                Text("This apply is no longer available.")
                    .foregroundStyle(.secondary)
                Button("Close") { coordinator.dismiss(sessionID: sessionID) }
            }
        }
        .padding(28)
        .frame(width: 600, height: 520)
    }

    // MARK: Header

    private func header(_ session: ApplySession) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Apply Fix")
                .font(.title2.weight(.semibold))
            Text("\(session.ruleID) on \(session.seat)")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: Content

    @ViewBuilder
    private func content(_ session: ApplySession) -> some View {
        switch session.phase {
        case .planning:
            progress(ApplyCopy.join("Reading the plan.", "Nothing is changed while it loads."))
        case .review(let plan, _):
            review(session, plan: plan)
        case .applying:
            VStack(alignment: .leading, spacing: 12) {
                progress("Applying in the background.")
                Text(ApplyCopy.join(
                    "You can close this window and keep working.",
                    "Muster tells you when the apply finishes."
                ))
                .font(.callout)
                .foregroundStyle(.secondary)
            }
        case .reproving:
            progress("Re-checking the cell with a fresh proof.")
        case .finished(let outcome):
            finished(outcome)
        case .failedToPlan(let message):
            statusBlock(
                symbol: "exclamationmark.triangle.fill", tint: .orange,
                title: "The Plan Could Not Be Read", detail: message
            )
        }
    }

    private func progress(_ text: String) -> some View {
        HStack(spacing: 12) {
            ProgressView().controlSize(.small)
            Text(text)
        }
    }

    private func review(_ session: ApplySession, plan: ApplyPlan) -> some View {
        let target = plan.entry(ruleID: session.ruleID)
        return VStack(alignment: .leading, spacing: 14) {
            if let notice = session.notice {
                Label(notice, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }

            Text("Planned Writes")
                .font(.headline)

            if plan.entries.isEmpty {
                Text(ApplyCopy.join(
                    "Nothing needs to change in this scope.",
                    "The cell already matches the manifest."
                ))
                .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(plan.entries) { entry in
                            entryRow(entry, isTarget: entry.ruleID == session.ruleID)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 230)
            }

            if let target, !target.autofix.isAllowed, case .blocked(let reason) = target.autofix {
                Label(ApplyCopy.normalize(reason), systemImage: "hand.raised.fill")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if target == nil, !plan.entries.isEmpty {
                Label("This rule is not in the plan, so there is nothing to apply for it.", systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            if plan.autofixable.count > 1 || (plan.autofixable.count == 1 && target?.autofix.isAllowed != true) {
                Text(ApplyCopy.join(
                    "Apply writes every auto-fixable entry listed here, not only this rule.",
                    "Review all of them."
                ))
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            if plan.needsTouchID {
                Label(
                    ApplyCopy.join(
                        "This includes a persistence-class change.",
                        "Touch ID is required before anything is written."
                    ),
                    systemImage: "touchid"
                )
                .font(.callout)
            }
        }
    }

    private func entryRow(_ entry: ApplyPlanEntry, isTarget: Bool) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text(entry.ruleID)
                    .font(.callout.weight(isTarget ? .semibold : .regular))
                Text(entry.state.label)
                    .font(.caption.weight(.medium))
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(.quaternary, in: Capsule())
                if entry.isPersistenceClass {
                    Label("Touch ID", systemImage: "touchid")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.orange)
                }
                Spacer(minLength: 0)
                Text(entry.autofix.isAllowed ? "Will Change" : "Report Only")
                    .font(.caption)
                    .foregroundStyle(entry.autofix.isAllowed ? Color.primary : Color.secondary)
            }
            Text(ApplyCopy.normalize(entry.change))
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(entry.files, id: \.self) { file in
                Text("~/" + file)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(entry.autofix.isAllowed ? Color.primary : Color.secondary)
                    .textSelection(.enabled)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(isTarget ? Color.accentColor.opacity(0.6) : Color.secondary.opacity(0.2), lineWidth: 1)
        }
    }

    private func finished(_ outcome: ApplyOutcome) -> some View {
        switch outcome {
        case .green(let result):
            return statusBlock(
                symbol: "checkmark.circle.fill", tint: .green,
                title: "Green Again",
                detail: ApplyCopy.normalize(result.reason)
            )
        case .stillFailing(let result):
            return statusBlock(
                symbol: "exclamationmark.triangle.fill", tint: .orange,
                title: "Still Not Passing",
                detail: result.map { ApplyCopy.normalize($0.reason) }
                    ?? "The cell could not be re-proved after the apply."
            )
        case .refused(let kind, let message):
            return statusBlock(
                symbol: "hand.raised.fill", tint: .orange,
                title: kind.title,
                detail: ApplyCopy.join(ApplyCopy.normalize(message), "The cell stays red and nothing was retried.")
            )
        case .failed(let message):
            return statusBlock(
                symbol: "xmark.octagon.fill", tint: .red,
                title: "Apply Failed",
                detail: ApplyCopy.join(ApplyCopy.normalize(message), "Nothing was retried.")
            )
        }
    }

    private func statusBlock(symbol: String, tint: Color, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol)
                .font(.title)
                .foregroundStyle(tint)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.headline)
                Text(detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: Footer

    @ViewBuilder
    private func footer(_ session: ApplySession) -> some View {
        HStack {
            Spacer()
            switch session.phase {
            case .review(let plan, _):
                Button("Cancel") { coordinator.dismiss(sessionID: sessionID) }
                    .keyboardShortcut(.cancelAction)
                Button("Apply") { coordinator.confirmApply(sessionID: sessionID) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(plan.entry(ruleID: session.ruleID)?.autofix.isAllowed != true)
            case .planning, .applying, .reproving:
                Button("Close") { coordinator.dismiss(sessionID: sessionID) }
                    .keyboardShortcut(.cancelAction)
            case .finished, .failedToPlan:
                Button("Done") { coordinator.dismiss(sessionID: sessionID) }
                    .keyboardShortcut(.defaultAction)
            }
        }
    }
}

// MARK: - Activity

/// Applies in flight and recently finished, for when the sheet is closed.  Hidden when empty.
struct ApplyActivityView: View {
    @ObservedObject private var coordinator = ApplyCoordinator.shared

    private var visible: [ApplySession] {
        coordinator.sessions.filter { session in
            if case .planning = session.phase { return false }
            return true
        }
    }

    var body: some View {
        if !visible.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Fix Activity").font(.headline)
                    Spacer()
                    if coordinator.sessions.contains(where: \.isFinished) {
                        Button("Clear Finished") { coordinator.clearFinished() }
                            .buttonStyle(.link)
                    }
                }
                ForEach(visible) { session in
                    HStack(spacing: 10) {
                        if session.isWriting {
                            ProgressView().controlSize(.small)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(session.ruleID) on \(session.seat)")
                                .font(.callout.weight(.medium))
                            Text(statusLine(session))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Show") { coordinator.present(sessionID: session.id) }
                            .buttonStyle(.link)
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.background, in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12).strokeBorder(.quaternary, lineWidth: 1)
            }
        }
    }

    private func statusLine(_ session: ApplySession) -> String {
        switch session.phase {
        case .planning: return "Reading the plan."
        case .review: return "Plan ready for review."
        case .applying: return "Applying in the background."
        case .reproving: return "Re-checking the cell."
        case .finished(let outcome): return outcome.summary
        case .failedToPlan: return "The plan could not be read."
        }
    }
}
