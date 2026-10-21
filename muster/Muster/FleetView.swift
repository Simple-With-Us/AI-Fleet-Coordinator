import SwiftUI

/// The Fleet View (AFC-394):  lanes, open PRs and Linear claims per seat.
///
/// Read only.  The data comes from one `fleet-view status --json` run,
/// decoded by `schemaVersion`.  A failed refresh clears the lists and shows
/// UNKNOWN:  the view never keeps the last green report on screen as if it
/// were fresh.  Unknown and blocked render red, per the Muster grid rule.
struct FleetView: View {
    @State private var envelope: FleetViewEnvelope?
    @State private var failed = false
    @State private var loading = false
    @State private var refreshedAt: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Fleet View", systemImage: "list.bullet.rectangle")
                    .font(.headline)
                Spacer()
                if let refreshedAt {
                    Text("Refreshed \(FleetTime.clock(refreshedAt))")
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

            if failed {
                Label("Fleet data unknown.  The last refresh failed, so nothing here is treated as current.", systemImage: "exclamationmark.triangle")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.red)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
            }

            if loading && envelope == nil {
                ProgressView("Loading fleet data…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let envelope {
                List {
                    sectionHeader("Lanes", section: envelope.sections.lanes)
                    ForEach(envelope.sections.lanes.items) { lane in
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

                    sectionHeader("Open Pull Requests", section: envelope.sections.pullRequests)
                    ForEach(envelope.sections.pullRequests.items) { pr in
                        HStack {
                            seatBadge(pr.seat)
                            VStack(alignment: .leading) {
                                Text("#\(pr.number) \(pr.title)").font(.body.weight(.medium)).lineLimit(1)
                                Text(pr.branch).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(pr.blocker)
                                .font(.caption.weight(.medium))
                                .foregroundStyle(pr.isBlocked ? .red : .secondary)
                        }
                    }

                    sectionHeader("Linear Claims In Progress", section: envelope.sections.claims)
                    ForEach(envelope.sections.claims.items) { claim in
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
                .listStyle(.inset)
            } else if !loading {
                Text("No fleet data yet.  Refresh to run the collector.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(24)
        .task { await refresh() }
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
        do {
            envelope = try await FleetViewService.load()
            failed = false
            refreshedAt = Date()
        } catch {
            // Fail closed:  clear the lists, never show the last green.
            envelope = nil
            failed = true
            refreshedAt = Date()
        }
    }
}
