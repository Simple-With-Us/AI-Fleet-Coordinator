import AppKit
import SwiftUI

/// Apps & Work (AFC-394 + AFC-536 + AFC-616):  what needs attention, open pull requests, Linear
/// claims, work by app, coding apps and their tools, and the lanes on the machine that ran the
/// collector.
///
/// Read only.  The data comes from `fleet-apps.json`, `fleet-view status --json`,
/// and `~/.local/state/fleet-tools/report.json`.  A failed refresh clears the lists and shows
/// UNKNOWN:  the view never keeps the last green report on screen as if it
/// were fresh.  A count that could not be read says "not known", never zero.  Every state
/// is words first;  colour and symbols only repeat them.  Pull requests and claims come
/// from GitHub and Linear and are the same from every machine;  lanes are read on one
/// machine, and the page names it.  The words and the joins live in the model files, where
/// the tests reach them, and this file only lays them out.
struct FleetView: View {
    /// The page's foldable sections.  Lanes start folded, because there can be hundreds.
    enum PageSection: String, CaseIterable {
        case attention, pullRequests, claims, apps, codingApps, lanes
    }

    /// How old the page's reading may be before a visit reads again.  Inside it, coming back to
    /// the page shows the reading on hand at once and runs no `fleet-view status` (about 17
    /// GitHub calls, a Linear call and two git processes a lane);  the Refresh button always reads.
    static let visitFreshness: TimeInterval = 60

    @EnvironmentObject private var store: FleetStore
    /// The page's reading, held for the whole app (`FleetViewStore`), so it outlives this view.
    @ObservedObject private var reading = FleetViewStore.shared
    /// The apps and coding apps listed from fleet-apps.json while the work read runs.  Shown
    /// only while the counts are pending:  the store's rows take over when its read lands.
    @State private var earlyRegistry: FleetRegistrySummary?
    @State private var filter = FleetViewFilter()
    @State private var collapsed: Set<PageSection> = [.lanes]
    @State private var openRepos: Set<String> = []

    private var envelope: FleetViewEnvelope? { reading.envelope }
    private var failure: FleetViewFailure? { reading.failure }
    private var loading: Bool { reading.loading }
    private var refreshClock: FleetViewRefreshClock { reading.refreshClock }
    private var failed: Bool { reading.failed }

    /// The rows on screen:  the early listing while the counts are read, else the store's rows,
    /// which it rebuilds with the work read before it sets the envelope.
    private var registrySummary: FleetRegistrySummary {
        if countsPending, let earlyRegistry { return earlyRegistry }
        return reading.registrySummary
    }

    /// The apps are listed but this refresh's work read has not come back:  their lane, pull
    /// request and claim counts are being read, which is neither zero nor a failure.
    private var countsPending: Bool { loading && envelope == nil }

    private var searching: Bool {
        !filter.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(MusterDestination.appsAndWork.title, systemImage: MusterDestination.appsAndWork.systemImage)
                    .font(.headline)
                Text("Zulip #\(registrySummary.zulipChannelDisplayName)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(.quaternary, in: Capsule())
                Spacer()
                // A refresh over rows already on screen says so,  so old rows never look
                // current while the new ones are read.
                if loading && !(envelope == nil && registrySummary.apps.isEmpty) {
                    ProgressView().controlSize(.small)
                    Text("Refreshing\u{2026}")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if let stamp = refreshClock.headerText {
                    Text(stamp)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Button {
                    Task { await load(force: true) }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }

            FleetViewFilterBar(filter: $filter)

            if let failure {
                failureBanner(failure)
            }

            if let problem = registrySummary.problem {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if loading && envelope == nil && registrySummary.apps.isEmpty {
                ProgressView("Loading fleet data…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if failed && envelope == nil && registrySummary.apps.isEmpty {
                // One message on a full failure:  the banner above says what happened, and no
                // section repeats it or spells a count of its own.
                Spacer()
            } else {
                List {
                    if countsPending {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Reading pull requests, claims and lanes\u{2026}\u{00A0} The apps below are listed from fleet-apps.json, and their counts fill in when the read finishes.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if let envelope {
                        attentionSection(envelope)
                        pullRequestSection(envelope)
                        claimSection(envelope)
                    }
                    appSection
                    codingAppSection
                    if let envelope {
                        laneSection(envelope)
                    }
                }
                .listStyle(.inset)
            }
        }
        .padding(24)
        .task { await load(force: false) }
    }

    // MARK: Sections

    private func attentionSection(_ envelope: FleetViewEnvelope) -> some View {
        let items = envelope.attentionItems
        let allRead = envelope.everythingRead
        return Section {
            if !collapsed.contains(.attention) {
                if items.isEmpty {
                    Label("Nothing needs attention.", systemImage: "checkmark.circle")
                        .foregroundStyle(.green)
                }
                ForEach(items) { item in
                    attentionRow(item)
                }
                Text(envelope.sourceText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } header: {
            sectionHeader(
                .attention,
                title: "Needs Attention",
                count: "\(items.count)",
                detail: envelope.verdictText,
                detailIsProblem: !allRead
            )
        }
    }

    private func pullRequestSection(_ envelope: FleetViewEnvelope) -> some View {
        let section = envelope.sections.pullRequests
        let rows = section.items
            .filter { filter.includes($0, app: registrySummary.app(forRepo: $0.repo)) }
            .sorted(by: FleetViewEnvelope.PullRequest.listOrder)
        let action = section.items.filter(\.needsAction).count
        let detail = section.isKnown
            ? "From GitHub, the same from every machine.\u{00A0} \(FleetWords.count(action, "needs", "need")) action;  drafts and running checks are waiting, not problems."
            : "Not known:  \(section.reason)"
        return Section {
            if !collapsed.contains(.pullRequests) {
                emptyRow(FleetSectionEmpty.state(
                    total: section.items.count, shown: rows.count, isKnown: section.isKnown,
                    reason: section.reason, noneText: "No open pull requests."
                ))
                ForEach(rows) { pr in
                    pullRequestRow(pr)
                }
            }
        } header: {
            sectionHeader(
                .pullRequests,
                title: "Open Pull Requests",
                count: WorkCount(observed: section.items.count, complete: section.isKnown).text,
                detail: detail,
                detailIsProblem: !section.isKnown
            )
        }
    }

    private func claimSection(_ envelope: FleetViewEnvelope) -> some View {
        let section = envelope.sections.claims
        let rows = section.items
            .filter { filter.includes($0, app: registrySummary.app(forClaim: $0.identifier)) }
            .sorted(by: FleetViewEnvelope.Claim.listOrder)
        let rule = envelope.expiryText.map { "\u{00A0} \($0)" } ?? ""
        let detail = section.isKnown
            ? "From Linear, the same from every machine.\(rule)"
            : "Not known:  \(section.reason)"
        return Section {
            if !collapsed.contains(.claims) {
                emptyRow(FleetSectionEmpty.state(
                    total: section.items.count, shown: rows.count, isKnown: section.isKnown,
                    reason: section.reason, noneText: "No claims in progress."
                ))
                ForEach(rows) { claim in
                    claimRow(claim)
                }
            }
        } header: {
            sectionHeader(
                .claims,
                title: "Linear Claims In Progress",
                count: WorkCount(observed: section.items.count, complete: section.isKnown).text,
                detail: detail,
                detailIsProblem: !section.isKnown
            )
        }
    }

    private var appSection: some View {
        let rows = registrySummary.apps.filter { filter.includes($0) }
        // The apps the kind and text filters keep, before Needs Attention Only.  When none, the
        // ordinary no-match state applies even while the counts are read (Kody, PR 1450).
        var withoutAttention = filter
        withoutAttention.attentionOnly = false
        let matchBesidesAttention = registrySummary.apps.contains { withoutAttention.includes($0) }
        return Section {
            if !collapsed.contains(.apps), registrySummary.problem == nil {
                if rows.isEmpty, countsPending, filter.attentionOnly, matchBesidesAttention {
                    // Which apps need attention depends on the counts being read, so the filter
                    // has nothing to match yet (Sentry, PR 1450).
                    Text("Which apps need attention is still being read.")
                        .foregroundStyle(.secondary)
                } else if rows.isEmpty {
                    emptyRow(FleetSectionEmpty.state(
                        total: registrySummary.apps.count, shown: 0, isKnown: !failed,
                        reason: "The last refresh failed.", noneText: "No apps are listed in fleet-apps.json."
                    ))
                } else {
                    appColumnHeader
                    ForEach(rows) { app in
                        appRow(app)
                    }
                }
            }
        } header: {
            sectionHeader(
                .apps,
                title: "Work By App",
                count: "\(registrySummary.apps.count)",
                detail: "Open lanes, pull requests and claims for each app in fleet-apps.json.\u{00A0} This is work in progress, not whether the app is up."
            )
        }
    }

    private var codingAppSection: some View {
        let rows = registrySummary.codingApps.filter { filter.includes($0) }
        return Section {
            if !collapsed.contains(.codingApps), registrySummary.problem == nil {
                if rows.isEmpty {
                    emptyRow(FleetSectionEmpty.state(
                        total: registrySummary.codingApps.count, shown: 0, isKnown: !failed,
                        reason: "The last refresh failed.", noneText: "No coding apps are listed in fleet-apps.json."
                    ))
                }
                ForEach(rows) { codingApp in
                    codingAppRow(codingApp)
                }
            }
        } header: {
            sectionHeader(
                .codingApps,
                title: "Coding Apps & Harnesses",
                count: "\(registrySummary.codingApps.count)",
                detail: "The app each seat runs in, and its tool on each machine.\u{00A0} \(registrySummary.toolsSourceText)",
                detailIsProblem: registrySummary.toolsNote != nil
            )
        }
    }

    private func laneSection(_ envelope: FleetViewEnvelope) -> some View {
        let section = envelope.sections.lanes
        let rows = section.items.filter { filter.includes($0, app: registrySummary.app(forRepo: $0.repo)) }
        let groups = FleetLaneGroup.groups(rows)
        let detail = section.isKnown
            ? "\(envelope.lanesCaption)\u{00A0} \(section.reason)."
            : "Not known:  \(section.reason)"
        return Section {
            if !collapsed.contains(.lanes) {
                emptyRow(FleetSectionEmpty.state(
                    total: section.items.count, shown: rows.count, isKnown: section.isKnown,
                    reason: section.reason, noneText: "No lanes \(envelope.host?.inProse ?? "on this machine")."
                ))
                ForEach(groups) { group in
                    laneGroupRow(group)
                    if searching || openRepos.contains(group.repo) {
                        ForEach(group.lanes) { lane in
                            laneRow(lane, host: lane.host ?? envelope.host)
                        }
                    }
                }
            }
        } header: {
            sectionHeader(
                .lanes,
                title: envelope.lanesHeading,
                count: WorkCount(observed: section.items.count, complete: section.isKnown).text,
                detail: detail,
                detailIsProblem: !section.isKnown
            )
        }
    }

    // MARK: Rows

    private func attentionRow(_ item: FleetAttentionItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: item.kind == .notRead ? "questionmark.circle" : "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
                .accessibilityHidden(true)
            Text(item.text)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer()
            if let url = item.url {
                Link("Open", destination: url)
                    .font(.caption)
            }
        }
    }

    private func pullRequestRow(_ pr: FleetViewEnvelope.PullRequest) -> some View {
        HStack(spacing: 10) {
            seatBadge(pr.seat)
            VStack(alignment: .leading, spacing: 2) {
                Text("#\(pr.number) \(pr.title)").font(.body.weight(.medium)).lineLimit(1)
                Text("\(pr.repo) · \(pr.branch)").font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Label(pr.statusText, systemImage: pr.severity.systemImage)
                .font(.caption.weight(pr.needsAction ? .semibold : .regular))
                .foregroundStyle(severityColor(pr.severity))
                .help("\(pr.severity.label):  \(pr.statusText)")
            if let url = pr.url {
                Link("Open", destination: url)
                    .font(.caption)
            }
        }
        .contextMenu {
            if let url = pr.url {
                Button("Open In Browser") { NSWorkspace.shared.open(url) }
            }
            Button("Copy Branch") { copy(pr.branch) }
            Button("Copy Pull Request Number") { copy("#\(pr.number)") }
        }
    }

    private func claimRow(_ claim: FleetViewEnvelope.Claim) -> some View {
        HStack(spacing: 10) {
            seatBadge(claim.seat)
            Text("\(claim.identifier) \(claim.title)").font(.body.weight(.medium)).lineLimit(1)
            Spacer()
            if claim.expired {
                Label("Expired · \(claim.ageText)", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.red)
            } else {
                Text(claim.ageText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let url = claim.url {
                Link("Open", destination: url)
                    .font(.caption)
            }
        }
        .contextMenu {
            if let url = claim.url {
                Button("Open In Browser") { NSWorkspace.shared.open(url) }
            }
            Button("Copy Identifier") { copy(claim.identifier) }
        }
    }

    private var lanesColumnTitle: String {
        guard let host = envelope?.host else { return "Lanes" }
        return "Lanes \(host.onTitle)"
    }

    private var appColumnHeader: some View {
        HStack(spacing: 10) {
            Text("App").frame(maxWidth: .infinity, alignment: .leading)
            Text(lanesColumnTitle).frame(width: 120, alignment: .trailing)
            Text("Pull Requests")
                .frame(width: 110, alignment: .trailing)
                .help("Open pull requests for each app")
            Text("Claims").frame(width: 90, alignment: .trailing)
            Text("Work").frame(width: 140, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
    }

    private func appRow(_ app: FleetAppStatus) -> some View {
        HStack(spacing: 10) {
            HStack(spacing: 8) {
                seatBadge(app.acronym)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(app.title).font(.body.weight(.medium))
                        Text(app.kindLabel)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                    }
                    Text(app.checkoutText.map { "\(app.repo) · \($0)" } ?? app.repo)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if countsPending {
                pendingCell(width: 120)
                pendingCell(width: 110)
                pendingCell(width: 90)
                Label("Reading", systemImage: "hourglass")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.12), in: Capsule())
                    .frame(width: 140, alignment: .leading)
            } else {
                countCell(app.lanes, extra: app.dirtyLanes > 0 ? "\(app.dirtyLanes) with changes" : nil, problem: false, width: 120)
                countCell(app.pullRequests, extra: app.actionPullRequests > 0 ? "\(app.actionPullRequests) need action" : nil, problem: true, width: 110)
                countCell(app.claims, extra: app.expiredClaims > 0 ? "\(app.expiredClaims) expired" : nil, problem: true, width: 90)
                workBadge(app.state)
                    .frame(width: 140, alignment: .leading)
            }
        }
        .help(countsPending ? Self.pendingHelp : app.reasons.joined(separator: "\n"))
    }

    private static let pendingHelp = "Lanes, pull requests and claims are still being read."

    /// A count this refresh is still reading:  a dash in the secondary colour, never zero and
    /// never the red of a count that could not be read.
    private func pendingCell(width: CGFloat) -> some View {
        Text("\u{2013}")
            .font(.callout.monospacedDigit())
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .trailing)
            .accessibilityLabel("Still being read")
    }

    private func codingAppRow(_ codingApp: CodingAppStatus) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(codingApp.displayName).font(.body.weight(.medium))
                if let cli = codingApp.cliTool {
                    Text("Tool:  \(cli)").font(.caption.monospaced()).foregroundStyle(.secondary)
                }
            }
            .frame(width: 160, alignment: .leading)
            Text(codingApp.seatLabels.joined(separator: ", "))
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .frame(width: 150, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                if codingApp.cliTool == nil {
                    Text(codingApp.runsOn)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(codingApp.toolReadings) { reading in
                        Label(reading.text, systemImage: cliSymbol(reading.outcome))
                            .font(.caption.monospaced())
                            .foregroundStyle(cliColor(reading.outcome))
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if countsPending {
                Text("Lanes, pull requests and claims:  reading\u{2026}")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
            } else {
                Text("\(codingApp.lanes.phrase("lane", "lanes")) · \(codingApp.pullRequests.phrase("pull request", "pull requests")) · \(codingApp.claims.phrase("claim", "claims"))")
                    .font(.caption.monospaced())
                    .foregroundStyle(
                        codingApp.lanes.complete && codingApp.pullRequests.complete && codingApp.claims.complete
                            ? Color.secondary : Color.red
                    )
            }
        }
    }

    private func laneGroupRow(_ group: FleetLaneGroup) -> some View {
        let open = searching || openRepos.contains(group.repo)
        return Button {
            if openRepos.contains(group.repo) {
                openRepos.remove(group.repo)
            } else {
                openRepos.insert(group.repo)
            }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: open ? "chevron.down" : "chevron.right")
                    .font(.caption.weight(.semibold))
                    .frame(width: 12)
                    .accessibilityHidden(true)
                Text(group.repo).font(.body.weight(.medium))
                Text(group.summaryText)
                    .font(.caption)
                    .foregroundStyle(group.unreadable > 0 ? Color.red : Color.secondary)
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(searching)
        .accessibilityLabel("\(group.repo), \(group.summaryText), \(open ? "open" : "folded")")
    }

    private func laneRow(_ lane: FleetViewEnvelope.Lane, host: FleetHost?) -> some View {
        HStack(spacing: 10) {
            seatBadge(lane.seat)
            VStack(alignment: .leading, spacing: 2) {
                Text(lane.lane).font(.body.weight(.medium))
                Text(lane.branchText).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if !lane.isReadable {
                Label("Git could not read this lane", systemImage: "questionmark.circle")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.red)
            } else {
                Text(lane.changesText)
                    .font(.caption)
                    .foregroundStyle(lane.dirtyFiles > 0 ? Color.orange : Color.secondary)
                Text(lane.activityText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.leading, 20)
        .contextMenu {
            if let path = lane.path, host == .mac {
                Button("Show In Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                }
                Button("Copy Path") { copy(path) }
            }
            if !lane.branch.isEmpty, !lane.detached {
                Button("Copy Branch") { copy(lane.branch) }
            }
        }
    }

    // MARK: Pieces

    /// A section's header:  a button that folds the section, its count, and where its rows came
    /// from, or why they are not known.
    private func sectionHeader(
        _ section: PageSection,
        title: String,
        count: String,
        detail: String,
        detailIsProblem: Bool = false
    ) -> some View {
        let folded = collapsed.contains(section)
        return VStack(alignment: .leading, spacing: 3) {
            Button {
                if collapsed.contains(section) {
                    collapsed.remove(section)
                } else {
                    collapsed.insert(section)
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: folded ? "chevron.right" : "chevron.down")
                        .font(.caption.weight(.semibold))
                        .frame(width: 12)
                        .accessibilityHidden(true)
                    Text(title).font(.headline)
                    Text(count).font(.headline.monospacedDigit()).foregroundStyle(.secondary)
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(title), \(count), \(folded ? "folded" : "open")")
            .accessibilityAddTraits(.isHeader)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                if detailIsProblem {
                    Image(systemName: "questionmark.circle")
                        .foregroundStyle(.red)
                        .accessibilityHidden(true)
                }
                Text(detail)
                    .foregroundStyle(detailIsProblem ? Color.red : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            .font(.caption)
        }
        .padding(.vertical, 4)
    }

    /// What a section shows with no rows:  a filter that hides them, truly none, or not known.
    @ViewBuilder
    private func emptyRow(_ empty: FleetSectionEmpty?) -> some View {
        if let empty {
            switch empty {
            case .hiddenByFilter:
                HStack {
                    Text("Nothing here matches the filter.")
                        .foregroundStyle(.secondary)
                    Button("Clear Filters") { filter = FleetViewFilter() }
                }
            case .noRows(let text):
                Text(text)
                    .foregroundStyle(.secondary)
            case .notKnown(let reason):
                Label("Not known:  \(reason)", systemImage: "questionmark.circle")
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func countCell(_ count: WorkCount, extra: String?, problem: Bool, width: CGFloat) -> some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(count.text)
                .font(.callout.monospacedDigit())
                .foregroundStyle(count.complete ? Color.primary : Color.red)
            if let extra {
                Text(extra)
                    .font(.caption2)
                    .foregroundStyle(problem ? Color.red : Color.secondary)
            }
        }
        .frame(width: width, alignment: .trailing)
    }

    private func workBadge(_ state: AppWorkState) -> some View {
        Label(state.label, systemImage: state.systemImage)
            .font(.caption.weight(.semibold))
            .foregroundStyle(workColor(state))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(workColor(state).opacity(0.12), in: Capsule())
    }

    private func workColor(_ state: AppWorkState) -> Color {
        switch state {
        case .needsAttention: return .red
        case .notKnown: return .indigo
        case .inProgress: return .blue
        case .quiet: return .secondary
        }
    }

    private func severityColor(_ severity: PullRequestSeverity) -> Color {
        switch severity {
        case .action: return .red
        case .unknown: return .indigo
        case .waiting: return .secondary
        case .ready: return .green
        }
    }

    private func cliColor(_ outcome: CLIOutcome) -> Color {
        switch outcome {
        case .current: return .green
        case .behind: return .orange
        case .missing, .failing: return .red
        case .notKnown, .notReached: return .indigo
        case .notInstalled, .notChecked: return .secondary
        }
    }

    private func cliSymbol(_ outcome: CLIOutcome) -> String {
        switch outcome {
        case .current: return "checkmark.circle"
        case .behind: return "arrow.down.circle"
        case .missing, .failing: return "xmark.octagon"
        case .notKnown, .notReached: return "questionmark.circle"
        case .notInstalled, .notChecked: return "minus.circle"
        }
    }

    private func copy(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    /// Why the refresh failed, in words the owner can read and can pass to an agent.  Every line
    /// wraps, and Copy Details carries the end of what the command printed, not only the last
    /// line shown.  The output is capped, and a cap says so in the text (FLEET-UI-COPY.md):
    /// `… N more characters` on a long line and `… N earlier lines not shown` at the top.
    private func failureBanner(_ failure: FleetViewFailure) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Fleet Data Unknown", systemImage: "exclamationmark.triangle")
                .font(.callout.weight(.semibold))
                .foregroundStyle(.red)
            Text("The last refresh failed, so nothing here is treated as current.\u{00A0} \(failure.reason)")
                .font(.callout)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if let detail = failure.detail {
                Text(detail)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button("Copy Details") {
                let pasteboard = NSPasteboard.general
                pasteboard.clearContents()
                pasteboard.setString(failure.copyText, forType: .string)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
    }

    private func seatBadge(_ seat: String) -> some View {
        Text(seat)
            .font(.caption.weight(.bold))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(.quaternary, in: Capsule())
            .frame(minWidth: 64, alignment: .leading)
    }

    /// Reads the page through the app's store:  a visit reads only when the reading on hand is
    /// older than `visitFreshness` (`force` false), and the Refresh button always reads.  A
    /// visit after the checkout changed always reads too, since the store's window counts only
    /// age (Kody, PR 1481).  The store keeps one read in flight, fails closed, and rebuilds the
    /// rows with the work read before it sets the envelope, so pending never ends while the rows
    /// still hold counts that were not read (Sentry, PR 1450).
    @MainActor
    private func load(force: Bool) async {
        let reading = self.reading
        let named = store.engineRoot
        let checkoutChanged = FleetViewReadingOrigin.checkoutChanged(to: named)
        FleetViewReadingOrigin.record(named)
        let mustRead = force || checkoutChanged
        // The work read (`fleet_view status`) is the slow one:  it reads pull requests across the
        // fleet's repos, Linear claims and every lane, and took 8.5 s on the server.  It is asked
        // for first and runs while the local reads below finish, so the page never waits on it
        // to list the apps.  (The store reads the tools report, a local file, just before it
        // starts `fleet_view`.)  A visit inside the window to the same checkout starts nothing,
        // and one during a read of the same checkout joins it.
        let work = Task { @MainActor in
            if mustRead {
                await reading.refresh(engineRoot: named)
            } else {
                await reading.refreshIfOlder(than: Self.visitFreshness, engineRoot: named)
            }
        }
        // With no work read on screen yet, the apps and coding apps show at once from
        // fleet-apps.json and the tools report.  Their lane, pull request and claim counts say
        // they are being read (`countsPending`), never zero, until the work read comes back.
        // A listing from an earlier load is dropped first, so another checkout's apps never
        // show under this load's pending counts (Kody, PR 1481).
        if reading.envelope == nil || checkoutChanged {
            earlyRegistry = nil
        }
        if reading.envelope == nil, let root = named ?? EngineLocator.locate()?.root {
            let tools = await Task.detached(priority: .userInitiated) {
                FleetToolsReportReader.readDefault()
            }.value
            let listed = await Task.detached(priority: .userInitiated) {
                FleetRegistryReader.read(repoRoot: root, viewOutput: nil, toolsReading: tools)
            }.value
            // A later load for another checkout may have started while this listing was read;
            // its listing, not this one, belongs on screen.
            if !FleetViewReadingOrigin.checkoutChanged(to: named) {
                earlyRegistry = listed
            }
        }
        await work.value
    }
}

/// The checkout the page last asked the store to read, kept for the app's life like the
/// store's reading itself.  `FleetViewStore.refreshIfOlder` counts only the reading's age, so
/// without this a return to the page inside the window after the checkout changed would show
/// the old checkout's apps, pull requests, claims and lanes (Kody, PR 1481).
@MainActor
private enum FleetViewReadingOrigin {
    /// Whether the page has asked for a read yet in this run of the app.
    private static var asked = false
    /// The checkout that read was asked for, or nil when the store was left to find it.
    private static var engineRoot: URL?

    /// Whether `named` is another checkout than the one the last read was asked for.  Before
    /// any read there is nothing to differ from.
    static func checkoutChanged(to named: URL?) -> Bool {
        asked && engineRoot != named
    }

    static func record(_ named: URL?) {
        asked = true
        engineRoot = named
    }
}
