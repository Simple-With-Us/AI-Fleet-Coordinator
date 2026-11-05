import AppKit
import SwiftUI

/// The Fleet View (AFC-394 + AFC-536):  Production & Fleet Apps, Coding Apps & Harnesses,
/// lanes, open PRs and Linear claims per seat.
///
/// Read only.  The data comes from `fleet-apps.json`, `fleet-view status --json`,
/// and `~/.local/state/fleet-tools/report.json`.  A failed refresh clears the lists and shows
/// UNKNOWN:  the view never keeps the last green report on screen as if it
/// were fresh.  Unknown and blocked render red, per the Muster grid rule.
struct FleetView: View {
    enum AppKindFilter: String, CaseIterable, Identifiable {
        case all = "All Apps"
        case product = "Products"
        case infraAndLibrary = "Infra & Library"
        case active = "Active Only"

        var id: String { rawValue }
    }

    @EnvironmentObject private var store: FleetStore
    @State private var envelope: FleetViewEnvelope?
    @State private var registrySummary = FleetRegistrySummary()
    @State private var appFilter: AppKindFilter = .all
    @State private var searchText: String = ""
    @State private var failure: FleetViewFailure?
    @State private var loading = false
    @State private var refreshClock = FleetViewRefreshClock()

    private var failed: Bool { failure != nil }

    private var filteredApps: [FleetAppStatus] {
        let base: [FleetAppStatus]
        switch appFilter {
        case .all:
            base = registrySummary.apps
        case .product:
            base = registrySummary.apps.filter { $0.kind == "product" }
        case .infraAndLibrary:
            base = registrySummary.apps.filter { $0.kind == "infra" || $0.kind == "library" || $0.kind == "marketing" }
        case .active:
            base = registrySummary.apps.filter { $0.hasActivity || $0.state != .passed }
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return base }
        return base.filter {
            $0.acronym.lowercased().contains(query)
                || $0.title.lowercased().contains(query)
                || $0.repo.lowercased().contains(query)
                || $0.kind.lowercased().contains(query)
        }
    }

    private var filteredCodingApps: [CodingAppStatus] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return registrySummary.codingApps }
        return registrySummary.codingApps.filter {
            $0.displayName.lowercased().contains(query)
                || $0.appSlug.lowercased().contains(query)
                || ($0.cliTool?.lowercased().contains(query) ?? false)
                || $0.seats.contains { $0.lowercased().contains(query) }
        }
    }

    private func filteredLanes(_ lanes: [FleetViewEnvelope.Lane]) -> [FleetViewEnvelope.Lane] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return lanes }
        return lanes.filter {
            $0.seat.lowercased().contains(query)
                || $0.repo.lowercased().contains(query)
                || $0.lane.lowercased().contains(query)
                || $0.branch.lowercased().contains(query)
        }
    }

    private func filteredPRs(_ prs: [FleetViewEnvelope.PullRequest]) -> [FleetViewEnvelope.PullRequest] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return prs }
        return prs.filter {
            $0.seat.lowercased().contains(query)
                || $0.repo.lowercased().contains(query)
                || $0.title.lowercased().contains(query)
                || $0.branch.lowercased().contains(query)
                || "\($0.number)".contains(query)
        }
    }

    private var isFiltering: Bool {
        appFilter != .all || !searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func clearFilters() {
        appFilter = .all
        searchText = ""
    }

    private func emptyState(_ title: String, description: String) -> some View {
        ContentUnavailableView {
            if failed {
                Label("Fleet Data Unknown", systemImage: "exclamationmark.triangle")
            } else {
                Label(title, systemImage: "line.3.horizontal.decrease.circle")
            }
        } description: {
            Text(failed
                ? "The last refresh failed, so this section cannot say what is here.\u{00A0} Nothing here is treated as current."
                : description)
        } actions: {
            if isFiltering && !failed {
                Button("Clear Filters") { clearFilters() }
            }
        }
    }

    private func filteredClaims(_ claims: [FleetViewEnvelope.Claim]) -> [FleetViewEnvelope.Claim] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return claims }
        return claims.filter {
            $0.seat.lowercased().contains(query)
                || $0.identifier.lowercased().contains(query)
                || $0.title.lowercased().contains(query)
        }
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
                    Task { await refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }

            HStack(spacing: 12) {
                Picker("App Kind", selection: $appFilter) {
                    ForEach(AppKindFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                // The segmented options name themselves;  a visible label
                // was squeezed into a 16pt column and wrapped letter by
                // letter beside them (AFC-592).  The title stays as the
                // accessibility label.
                .labelsHidden()
                .frame(maxWidth: 380)
                TextField("Filter apps, coding harnesses, seats, lanes, PRs, or claims…", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: 340)
                Spacer()
            }

            if let failure {
                failureBanner(failure)
            }

            if let problem = registrySummary.problem {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            if loading && envelope == nil && registrySummary.apps.isEmpty {
                ProgressView("Loading fleet data…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    if !filteredApps.isEmpty {
                        HStack {
                            Text("Production & Fleet Apps (\(filteredApps.count))").font(.headline)
                            Spacer()
                            Text("from fleet-apps.json + fleet-view")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(filteredApps) { app in
                            HStack(spacing: 10) {
                                seatBadge(app.acronym)
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack(spacing: 6) {
                                        Text(app.title).font(.body.weight(.medium))
                                        Text(app.kind)
                                            .font(.caption2.monospaced())
                                            .foregroundStyle(.secondary)
                                            .padding(.horizontal, 5)
                                            .padding(.vertical, 1)
                                            .background(.quaternary, in: Capsule())
                                    }
                                    Text(
                                        "\(app.repo) · checkout: \(app.hasIntegrationCheckout ? "present" : "absent")\(app.publishScript != nil ? " · publish: \(app.hasPublishScript ? "ready" : "missing")" : "")"
                                    )
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text("\(app.activeLanes) lane(s)\(app.dirtyLanes > 0 ? " (\(app.dirtyLanes) dirty)" : "")")
                                    .font(.caption.monospaced())
                                    .foregroundStyle(app.dirtyLanes > 0 ? .orange : .secondary)
                                Text("\(app.openPullRequests) PR(s)\(app.blockedPullRequests > 0 ? " (\(app.blockedPullRequests) blocked)" : "")")
                                    .font(.caption.monospaced())
                                    .foregroundStyle(app.blockedPullRequests > 0 ? .orange : .secondary)
                                Text("\(app.activeClaims) claim(s)\(app.expiredClaims > 0 ? " (\(app.expiredClaims) expired)" : "")")
                                    .font(.caption.monospaced())
                                    .foregroundStyle(app.expiredClaims > 0 ? .red : .secondary)
                                FleetToolsStateBadge(state: app.state, width: 92)
                            }
                        }
                    }

                    if filteredApps.isEmpty {
                        emptyState("No Apps Match", description: "No production or fleet app matches the current filter.\u{00A0} Clear the filter to see every app.")
                    }

                    if !filteredCodingApps.isEmpty {
                        HStack {
                            Text("Coding Apps & Harnesses (\(filteredCodingApps.count))").font(.headline)
                            Spacer()
                            Text("seatApps + Mac CLI currency")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(filteredCodingApps) { codingApp in
                            HStack(spacing: 10) {
                                Text(codingApp.displayName)
                                    .font(.body.weight(.medium))
                                    .frame(width: 170, alignment: .leading)
                                Text(codingApp.seats.joined(separator: ", "))
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                    .frame(width: 150, alignment: .leading)
                                if let cli = codingApp.cliTool {
                                    if !codingApp.doctorReportCurrent {
                                        Text("\(cli): unknown (doctor report stale)")
                                            .font(.caption.monospaced())
                                            .foregroundStyle(.secondary)
                                    } else {
                                        Text("\(cli): \(codingApp.cliVersion ?? "—") (\(codingApp.cliState?.rawValue ?? "unknown"))")
                                            .font(.caption.monospaced())
                                            .foregroundStyle(codingApp.cliState == .current ? .green : (codingApp.cliState == .stale ? .orange : .secondary))
                                    }
                                } else {
                                    Text("hosted / cloud seat")
                                        .font(.caption.monospaced())
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text("\(codingApp.activeLanes) lane(s) · \(codingApp.openPullRequests) PR(s) · \(codingApp.activeClaims) claim(s)")
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }

                    if filteredCodingApps.isEmpty {
                        emptyState("No Coding Apps Match", description: "No coding app or harness matches the current filter.\u{00A0} Clear the filter to see every harness.")
                    }

                    if let envelope {
                        sectionHeader("Lanes", section: envelope.sections.lanes)
                        if filteredLanes(envelope.sections.lanes.items).isEmpty {
                            emptyState("No Lanes Match", description: "No lane matches the current filter.\u{00A0} Clear the filter to see every lane.")
                        }
                        ForEach(filteredLanes(envelope.sections.lanes.items)) { lane in
                            HStack {
                                seatBadge(lane.seat)
                                VStack(alignment: .leading) {
                                    Text(lane.lane).font(.body.weight(.medium))
                                    Text("\(lane.repo) · \(lane.branch.isEmpty ? "no branch read" : lane.branch)")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if lane.state != "OK" {
                                    Text("UNKNOWN").font(.caption.weight(.bold)).foregroundStyle(.red)
                                } else {
                                    Text(lane.dirtyFiles == 0 ? "clean" : "\(lane.dirtyFiles) dirty")
                                        .font(.caption).foregroundStyle(.secondary)
                                    Text(FleetTime.age(lane.ageSeconds))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }

                        if filteredPRs(envelope.sections.pullRequests.items).isEmpty {
                            emptyState("No Pull Requests Match", description: "No open pull request matches the current filter.\u{00A0} Clear the filter to see every pull request.")
                        }
                        sectionHeader("Open Pull Requests", section: envelope.sections.pullRequests)
                        ForEach(filteredPRs(envelope.sections.pullRequests.items)) { pr in
                            HStack {
                                seatBadge(pr.seat)
                                VStack(alignment: .leading) {
                                    Text("#\(pr.number) \(pr.title)").font(.body.weight(.medium)).lineLimit(1)
                                    Text("\(pr.repo) · \(pr.branch)").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text(pr.blocker)
                                    .font(.caption.weight(.medium))
                                    .foregroundStyle(pr.isBlocked ? .red : .secondary)
                            }
                        }

                        if filteredClaims(envelope.sections.claims.items).isEmpty {
                            emptyState("No Claims Match", description: "No claim in progress matches the current filter.\u{00A0} Clear the filter to see every claim.")
                        }
                        sectionHeader("Linear Claims In Progress", section: envelope.sections.claims)
                        ForEach(filteredClaims(envelope.sections.claims.items)) { claim in
                            HStack {
                                seatBadge(claim.seat)
                                VStack(alignment: .leading) {
                                    Text("\(claim.identifier) \(claim.title)").font(.body.weight(.medium)).lineLimit(1)
                                }
                                Spacer()
                                if claim.expired {
                                    Text(claim.ageSeconds < 0 ? "EXPIRED · age unknown" : "EXPIRED · \(FleetTime.age(claim.ageSeconds))")
                                        .font(.caption.weight(.bold)).foregroundStyle(.red)
                                } else {
                                    Text(FleetTime.age(claim.ageSeconds))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
                .listStyle(.inset)
            }
        }
        .padding(24)
        .task { await refresh() }
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

    private func sectionHeader<Item>(_ title: String, section: FleetViewEnvelope.Section<Item>) -> some View {
        HStack {
            Text(title).font(.headline)
            Spacer()
            if !section.isKnown {
                Text("UNKNOWN — \(section.reason)")
                    .font(.caption.weight(.bold)).foregroundStyle(.red)
            } else {
                Text(section.reason).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func seatBadge(_ seat: String) -> some View {
        Text(seat)
            .font(.caption.weight(.bold))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(.quaternary, in: Capsule())
            .frame(minWidth: 64, alignment: .leading)
    }

    private func refresh() async {
        loading = true
        defer { loading = false }
        let root = store.engineRoot ?? EngineLocator.locate()?.root
        let tools = await Task.detached(priority: .userInitiated) {
            FleetToolsReportReader.readDefault()
        }.value
        do {
            let loaded = try await FleetViewService.load()
            envelope = loaded
            failure = nil
            refreshClock.recordSuccess(generatedAt: loaded.generatedAt)
            if let root {
                registrySummary = await Task.detached(priority: .userInitiated) {
                    FleetRegistryReader.read(repoRoot: root, viewOutput: loaded, toolsReading: tools)
                }.value
            }
        } catch {
            // Fail closed:  clear the lists, never show the last green.  The registry
            // rows go with them:  read back without the run's answer they carry lane,
            // pull request and claim counts of zero, which reads as a fleet measured
            // and found idle rather than one this refresh could not see.  The banner
            // above the list says the counts are unknown;  nothing under it may spell
            // a number of its own.  The words are worked out off the main thread, because
            // stderr can be 4 MB, and the lists clear after that, so the page is never empty
            // with no banner.
            let described = await Task.detached(priority: .userInitiated) {
                FleetViewFailure(error: error)
            }.value
            envelope = nil
            failure = described
            refreshClock.recordFailure()
            registrySummary = FleetRegistrySummary()
        }
    }
}
