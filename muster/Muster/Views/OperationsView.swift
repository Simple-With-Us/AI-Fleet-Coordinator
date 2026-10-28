import SwiftUI

/// The operations tab:  the Mac's LaunchAgents,  the server's updates,  and the audit log.
///
/// It reads three local files and runs nothing by itself:  the Mac collector's
/// `mac-services.json`,  the allowlist in this Mac's checkout
/// (`scripts/fleet-ops-control.json`),  and the audit log `ops-audit.jsonl`.  Every button
/// starts an action through `OpsController`,  which plans it,  shows the plan,  asks Touch ID
/// and only then runs it.
struct OperationsView: View {
    @State private var services: MacServicesReading?
    @State private var allowlist: OpsAllowlist?
    @State private var allowlistPath = ""
    @State private var audit: OpsAuditReading?
    @State private var loading = false
    @ObservedObject private var controller = OpsController.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if loading && services == nil {
                    Text("Reading the Mac's agents…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    agentsSection
                    updatesSection
                    auditSection
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load() }
        .onChange(of: controller.completedRuns) { _, _ in
            Task { await load() }
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Operations")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("Start, stop and update the fleet's processes.\u{00A0} Every action shows its plan and asks Touch ID before it runs.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if controller.isBusy {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(controller.phase.action?.title ?? "An action is running.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func problemBox(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    private func sectionHeader(_ title: String, state: CellState?, detail: String) -> some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.headline)
            if let state {
                FleetToolsStateBadge(state: state, width: 92)
            }
            Spacer()
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: The Mac's agents

    @ViewBuilder
    private var agentsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let services {
                let current = services.problem == nil && services.isCurrent
                sectionHeader(
                    "Mac LaunchAgents",
                    state: current ? services.state : nil,
                    detail: services.problem == nil
                        ? "\(services.loadedCount) of \(services.agents.count) expected loaded"
                            + (services.unreadCount > 0 ? ", \(services.unreadCount) unread" : "")
                        : ""
                )
                if let age = services.age {
                    Text("Read \(age) ago")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(services.isCurrent ? Color.secondary : Color.orange)
                }
                if let problem = services.problem {
                    problemBox(problem)
                } else if !services.isCurrent {
                    problemBox("This file is stale.\u{00A0} The collector writes it every five minutes; old rows are not current evidence.")
                } else if services.agents.isEmpty {
                    Text("The file names no agents.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    GridRowHeader(labels: ["Agent", "Installed", "State", "Actions"])
                    ForEach(services.agents) { agent in
                        agentRow(agent)
                    }
                }
            } else {
                sectionHeader("Mac LaunchAgents", state: nil, detail: "")
                Text("No file yet.\u{00A0} Refresh to read it.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func agentRow(_ agent: MacAgent) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 1) {
                Text(agent.label)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(agent.plistInRepo ? "plist in the repository" : "named by the allowlist")
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(agent.installed ? "yes" : "no")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 6) {
                FleetToolsStateBadge(state: agent.state, width: 76)
                Text(agent.statusText)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 4) {
                if agent.controllable {
                    ForEach(verbs(for: agent), id: \.self) { verb in
                        Button(verb.title) {
                            controller.plan(.macAgent(verb: verb, label: agent.label))
                        }
                        .controlSize(.small)
                        .disabled(controller.isPresenting)
                    }
                } else {
                    Text("not in the allowlist")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 2)
    }

    /// The verbs an agent gets:  the ones the allowlist names for it,  or all three when
    /// this checkout has no entry.  The helper checks the allowlist again either way.
    private func verbs(for agent: MacAgent) -> [OpsVerb] {
        let named = allowlist?.verbs(forMacAgent: agent.label) ?? []
        return named.isEmpty ? OpsVerb.allCases : named
    }

    // MARK: The server's updates

    private var updatesSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Server Updates",
                state: nil,
                detail: allowlist.map { "\($0.updates.count) \($0.updates.count == 1 ? "update" : "updates")" } ?? ""
            )
            if let allowlist {
                if allowlist.updates.isEmpty {
                    Text("The allowlist names no updates.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(allowlist.updates) { update in
                        HStack(spacing: 10) {
                            Text(update.name)
                                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                            Text("\(update.unit) (\(update.scope))")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer(minLength: 0)
                            Button("Run Update") {
                                controller.plan(.serverUpdate(name: update.name))
                            }
                            .controlSize(.small)
                            .disabled(controller.isPresenting)
                        }
                        .padding(.vertical, 2)
                    }
                }
            } else {
                problemBox("The allowlist could not be read at \(allowlistPath), so no update is offered.")
            }
        }
    }

    // MARK: The audit log

    private var auditSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Audit Log",
                state: nil,
                detail: "last \(OpsAuditLog.defaultLimit) rows, newest first"
            )
            if let audit {
                if let problem = audit.problem {
                    problemBox(problem)
                } else if audit.entries.isEmpty {
                    Text("Nothing has been run from this Mac yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    GridRowHeader(labels: ["Time", "Host", "Verb", "Target", "Result"])
                    ForEach(audit.entries) { entry in
                        auditRow(entry)
                    }
                }
                if audit.unreadable > 0 {
                    Text("\(audit.unreadable) line(s) of the log could not be read.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                Text(audit.path)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }

    private func auditRow(_ entry: OpsAuditEntry) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 0) {
                GridCellText(value: entry.clockText)
                if let date = entry.date {
                    Text(FleetTime.ageText(since: date) + " ago")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            GridCellText(value: (entry.host ?? "—") + (entry.via.map { " via \($0)" } ?? ""))
                .frame(maxWidth: .infinity, alignment: .leading)
            GridCellText(value: entry.verb ?? "—")
                .frame(maxWidth: .infinity, alignment: .leading)
            GridCellText(value: entry.target ?? "—", emphasised: true)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(entry.result ?? "unknown")
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(entry.state.tint)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 1)
    }

    // MARK: The read

    /// Read the three files off the main thread.
    private func load() async {
        loading = true
        defer { loading = false }
        let servicesURL = MacServicesReportReader.reportURL()
        let allowlistURL = OpsAllowlist.url(repoRoot: FleetConfigLocation.resolve().repoRoot)
        let auditURL = OpsAuditLog.logURL()
        let (readServices, readAllowlist, readAudit) = await Task.detached(priority: .userInitiated) {
            (
                MacServicesReportReader.read(at: servicesURL),
                OpsAllowlist.read(at: allowlistURL),
                OpsAuditLog.read(at: auditURL)
            )
        }.value
        services = readServices
        allowlist = readAllowlist
        allowlistPath = allowlistURL.path
        audit = readAudit
    }
}

#Preview {
    OperationsView()
}
