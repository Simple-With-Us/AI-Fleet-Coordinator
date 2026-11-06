import SwiftUI

/// The three panels that were Operations:  the Mac's background agents,  the server's
/// updates,  and the history of actions run from the Mac.
///
/// Operations held all three on one page under the Server heading,  so the Mac's agents
/// could be stopped from under a heading that said Server.  Each part is now its own panel
/// under the place it acts on,  and they share one read:  the saved Mac report,  the
/// allowlist and the audit log.  Reading the Mac's agents collects current agents through
/// the existing read-only collector without writing a report.  Changes use `OpsController`:
/// plan,  review,  Touch ID,  re-plan,  then run.
struct OperationsView: View {
    /// Which of the three panels this is.
    enum Part {
        case agents
        case updates
        case history

        /// The panel's place in the sidebar,  which names it.
        var destination: MusterDestination {
            switch self {
            case .agents: return .macAgents
            case .updates: return .serverUpdates
            case .history: return .actionHistory
            }
        }
    }

    let part: Part

    @State private var services: MacServicesReading?
    @State private var allowlist: OpsAllowlist?
    @State private var allowlistPath = ""
    @State private var audit: OpsAuditReading?
    @State private var loading = false
    @State private var collectingAgents = false
    @State private var refreshRequests = OperationsRefreshRequests()
    @ObservedObject private var controller = OpsController.shared

    init(part: Part) {
        self.part = part
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                switch part {
                case .agents:
                    HogHunterMacPanel()
                    if loading && services == nil {
                        Text("Reading the Mac's agents\u{2026}")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } else {
                        TimelineView(.periodic(from: .now, by: 30)) { context in
                            agentsSection(services?.aged(at: context.date))
                        }
                    }
                case .updates:
                    if loading && allowlist == nil {
                        Text("Reading the allowlist\u{2026}")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } else {
                        updatesSection
                    }
                case .history:
                    if loading && audit == nil {
                        Text("Reading the action log\u{2026}")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } else {
                        auditSection
                    }
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load(collectAgents: false) }
        .onChange(of: controller.completedRuns) { _, _ in
            let localAgentChanged: Bool
            if part == .agents, let action = controller.phase.action, case .macAgent = action {
                localAgentChanged = true
            } else {
                localAgentChanged = false
            }
            Task { await load(collectAgents: localAgentChanged) }
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(part.destination.title)
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load(collectAgents: part == .agents) }
                } label: {
                    Label(refreshTitle, systemImage: "arrow.clockwise")
                }
                .disabled(loading || collectingAgents || controller.isBusy)
                .help(refreshHelp)
            }
            Text(description)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if loading || collectingAgents {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(collectingAgents ? "Reading the Mac's agents\u{2026}" : "Reading\u{2026}")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
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

    /// The panel's one reread,  named for what it reads.
    private var refreshTitle: String {
        switch part {
        case .agents: return "Read The Mac's Agents"
        case .updates: return "Read The Allowlist Again"
        case .history: return "Read The Log Again"
        }
    }

    private var refreshHelp: String {
        switch part {
        case .agents: return "Checks the Mac's current agents without changing them."
        case .updates: return "Reads the allowlist in the Mac's checkout again."
        case .history: return "Reads the Mac's action log again."
        }
    }

    private var description: String {
        let gap = ServerStatusCopy.sentenceGap
        switch part {
        case .agents:
            return "The LaunchAgents the Mac runs for the fleet, and Hog Hunter's report about the Mac.\(gap)A change shows its plan and asks Touch ID before it runs on the Mac."
        case .updates:
            return "The updates the allowlist names.\(gap)An update shows its plan and asks Touch ID before it runs on the server."
        case .history:
            return "What was run from the Mac, newest first, and the machine each action ran on."
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
    private func agentsSection(_ services: MacServicesReading?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let services {
                let current = services.problem == nil && services.isCurrent
                sectionHeader(
                    "LaunchAgents",
                    state: current ? services.state : nil,
                    detail: services.problem == nil
                        ? "\(services.loadedCount) of \(services.agents.count) expected loaded"
                            + (services.unreadCount > 0 ? ", \(services.unreadCount) unread" : "")
                        : ""
                )
                if let age = services.age {
                    Text("Read \(age) ago · \(services.path)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(services.isCurrent ? Color.secondary : Color.orange)
                }
                if let problem = services.problem {
                    problemBox(problem)
                } else if !services.isCurrent {
                    problemBox("This snapshot is stale.\u{00A0} Choose \(refreshTitle) to collect current state.")
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
                sectionHeader("LaunchAgents", state: nil, detail: "")
                Text("No file yet.\u{00A0} Choose \(refreshTitle) to collect it.")
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
                        let action = OpsAction.macAgent(verb: verb, label: agent.label)
                        let spoken = action.title
                        Button(verb.title) {
                            controller.plan(action)
                        }
                        .controlSize(.small)
                        .disabled(controller.isPresenting)
                        .help(spoken)
                        .accessibilityLabel(spoken)
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
    /// this checkout has no entry.  The helper checks the allowlist again either way.
    private func verbs(for agent: MacAgent) -> [OpsVerb] {
        let named = allowlist?.verbs(forMacAgent: agent.label) ?? []
        return named.isEmpty ? OpsVerb.allCases : named
    }

    // MARK: The server's updates

    private var updatesSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Updates",
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
                            let action = OpsAction.serverUpdate(name: update.name)
                            let spoken = action.title
                            Button("Run Update") {
                                controller.plan(action)
                            }
                            .controlSize(.small)
                            .disabled(controller.isPresenting)
                            .help(spoken)
                            .accessibilityLabel(spoken)
                        }
                        .padding(.vertical, 2)
                    }
                }
            } else {
                problemBox("The allowlist could not be read at \(allowlistPath), so no update is offered.")
            }
        }
    }

    // MARK: The action history

    private var auditSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Actions",
                state: nil,
                detail: "last \(OpsAuditLog.defaultLimit) rows, newest first"
            )
            if let audit {
                if let problem = audit.problem {
                    problemBox(problem)
                } else if audit.entries.isEmpty {
                    Text("Nothing has been run from the Mac yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    GridRowHeader(labels: ["Time", "Machine", "Verb", "Target", "Result"])
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
            GridCellText(value: ActionHistoryWords.host(entry.host, via: entry.via))
                .frame(maxWidth: .infinity, alignment: .leading)
            GridCellText(value: entry.verb ?? "\u{2014}")
                .frame(maxWidth: .infinity, alignment: .leading)
            GridCellText(value: entry.target ?? "\u{2014}", emphasised: true)
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

    /// Read local operations data and, when requested, obtain a fresh agent snapshot.
    private func load(collectAgents: Bool = true) async {
        refreshRequests.request(collectAgents: collectAgents)
        await drainPendingRefreshes()
    }

    private func drainPendingRefreshes() async {
        guard !loading else { return }
        if refreshRequests.metadataRequested {
            loading = true
            defer { loading = false }
            var metadataPasses = 0
            repeat {
                refreshRequests.beginMetadataRead()
                metadataPasses += 1
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
                if services == nil { services = readServices }
                allowlist = readAllowlist
                allowlistPath = allowlistURL.path
                audit = readAudit
            } while refreshRequests.shouldRepeatMetadataRead(completedPasses: metadataPasses)
        }
        if refreshRequests.agentsRequested && !collectingAgents {
            collectingAgents = true
            defer { collectingAgents = false }
            repeat {
                refreshRequests.beginAgentRead()
                schedulePendingMetadataRead()
                services = await MacServicesCollector(repoRoot: FleetConfigLocation.resolve().repoRoot).collect()
            } while refreshRequests.agentsRequested
        }
        schedulePendingMetadataRead()
    }

    private func schedulePendingMetadataRead() {
        guard refreshRequests.scheduleMetadataDrainIfNeeded() else { return }
        Task {
            refreshRequests.beginScheduledMetadataDrain()
            await drainPendingRefreshes()
        }
    }
}

#Preview {
    OperationsView(part: .agents)
}
