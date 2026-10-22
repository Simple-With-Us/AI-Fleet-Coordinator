import SwiftUI

/// The wake settings panel:  what every seat's wake is,  what the configuration file says
/// it should be,  and a way to change it.
///
/// The panel has two columns of truth and never mixes them.  The left column is the file,
/// and it is editable.  The middle column is the running listener's own account of itself.
/// When the two disagree the row says DRIFT,  and it is never a quiet green.
///
/// A setting the engine does not enforce is shown as NOT OWNED and is not editable:  the
/// listener counts wakes and not dollars,  so a dollar cap in the file would never bind.
struct WakeSettingsView: View {
    @EnvironmentObject private var fleet: FleetStore
    @StateObject private var store = WakeSettingsStore()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                listenerLine
                if !store.model.problems.isEmpty {
                    problemBox(store.model.problems.joined(separator: "\n"))
                }
                if let message = store.message {
                    messageBox(message)
                }
                daemonSection
                operatorSection
                ForEach(store.model.seats) { seat in
                    seatSection(seat)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task {
            await store.refresh(roster: fleet.roster)
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Wake Settings")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await store.refresh(roster: fleet.roster) }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(store.isBusy)
            }
            Text("What every seat's wake is,  what the configuration file says it should be,  and a way to change it.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// The listener's own account of itself.
    private var listenerLine: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Circle()
                    .fill(store.model.running ? Color.green : Color.red)
                    .frame(width: 8, height: 8)
                Text(store.model.running ? "The listener is running" : "The listener is not running")
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                if let pid = store.model.pid {
                    Text("pid \(pid)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                if let updated = store.model.updated {
                    Text("read at " + updated.formatted(date: .omitted, time: .standard))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            if !store.model.configPath.isEmpty {
                Text(store.model.configPath)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    // MARK: The boxes

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

    private func messageBox(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "info.circle.fill")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.secondary.opacity(0.08))
    }

    // MARK: The daemon

    /// The daemon-wide settings,  like the host load gate.
    private var daemonSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("The Listener")
                .font(.headline)
            GridRowHeader(labels: ["Setting", "The file", "The listener", "State"])
            ForEach(store.model.daemonSettings) { setting in
                WakeSettingRow(
                    setting: setting,
                    editable: false,
                    draft: .constant(setting.configuredText)
                )
            }
        }
    }

    // MARK: The Operator

    /// The Operator's own options.
    private var operatorSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("The Operator")
                .font(.headline)
            Text("The service seat that would carry fleet operations under its own name,  and issue the fleet's session letters.")
                .font(.caption)
                .foregroundStyle(.secondary)
            GridRowHeader(labels: ["Name", "Where it lives", "What it is for", "State"])
            ForEach(OperatorOptions.names) { name in
                HStack(spacing: 12) {
                    GridCellText(value: name.name)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    GridCellText(value: name.location)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .lineLimit(2)
                    GridCellText(value: name.purpose)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .lineLimit(2)
                    WakeStateBadge(state: .notOwned, width: 108)
                }
                .padding(.vertical, 2)
            }
            HStack(spacing: 12) {
                GridCellText(value: "Configured")
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(
                    value: store.model.operatorOptions.isConfigured
                        ? (store.model.operatorOptions.bot ?? "—")
                        : "no section"
                )
                .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(
                    value: store.model.operatorOptions.creds
                        ?? (store.model.operatorOptions.isConfigured ? "—" : "—")
                )
                .frame(maxWidth: .infinity, alignment: .leading)
                WakeStateBadge(
                    state: store.model.operatorOptions.hasRequiredIdentity ? .match : .notOwned,
                    width: 108
                )
            }
            .padding(.vertical, 2)
            if let problem = store.model.operatorOptions.problem {
                Text(problem)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Muster reads no credentials,  so it cannot say whether the Operator's key exists.  The letters it issues come from \(OperatorOptions.letterPool.count) of \(OperatorOptions.letterPool.count) letters,  and the smallest free one is reused.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: One seat

    private func seatSection(_ seat: WakeSeat) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(seat.seat)
                    .font(.headline)
                if let bot = seat.bot {
                    Text(bot)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Text(seat.liveWake?.title ?? "no wake")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(seat.liveWake?.runsWorker == true ? Color.primary : Color.orange)
                if seat.liveWake?.runsWorker == false {
                    Text("runs no worker")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .help("An inbox seat captures every DM and tag,  and runs nothing.")
                }
                if seat.paused {
                    Text("PAUSED")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.red)
                } else if seat.wakesPaused {
                    Text("WAKES PAUSED")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.orange)
                }
                Spacer()
                if let scope = seat.pauseScope {
                    Text("pause scope \(scope)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Button {
                    Task { await store.pause(seat: seat.seat) }
                } label: {
                    Label("Pause wakes", systemImage: "pause.circle")
                }
                .disabled(store.isBusy)
                Button {
                    Task { await store.resume(seat: seat.seat) }
                } label: {
                    Label("Resume", systemImage: "play.circle")
                }
                .disabled(store.isBusy)
            }
            GridRowHeader(labels: ["Setting", "The file", "The listener", "State"])
            ForEach(seat.settings) { setting in
                WakeSettingRow(
                    setting: setting,
                    editable: setting.key.refusalReason == nil,
                    draft: draftBinding(seat: seat.seat, key: setting.key)
                )
            }
            usageLine(seat)
            pinsLine(seat)
            recentWakes(seat)
            HStack(spacing: 8) {
                Button {
                    Task { await store.apply(seat: seat.seat) }
                } label: {
                    Label("Apply", systemImage: "square.and.arrow.down")
                }
                .disabled(store.isBusy)
                Button {
                    Task { await store.testWake(seat: seat.seat, runtime: seat.liveWake?.rawValue ?? "muse") }
                } label: {
                    Label("Test wake", systemImage: "play.and.pause")
                }
                .disabled(store.isBusy)
                .help("Runs the seat's wake for real.  A failed run never pins.")
                Button {
                    Task { await store.pin(seat: seat.seat, runtime: seat.liveWake?.rawValue ?? "muse") }
                } label: {
                    Label("Pin", systemImage: "pin")
                }
                .disabled(store.isBusy)
                .help("Pins the binary whose test wake passed.")
            }
        }
        .padding(.vertical, 6)
    }

    /// The draft's binding for one setting.
    private func draftBinding(seat: String, key: WakeSettingKey) -> Binding<String> {
        Binding(
            get: { store.draft[seat]?[key] ?? "" },
            set: { newValue in
                store.draft[seat, default: [:]][key] = newValue
            }
        )
    }

    /// How much of the budget the seat has used.
    private func usageLine(_ seat: WakeSeat) -> some View {
        let usage = seat.usage
        return HStack(spacing: 14) {
            Text("Used")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            GridCellText(value: "\(usage.wakesThisHour) wakes this hour")
            GridCellText(value: "\(usage.wakesThisDay) today")
            GridCellText(value: "\(usage.ownerThisDay) owner today")
            GridCellText(value: "busiest topic \(usage.worstTopicThisHour) this hour")
            GridCellText(value: "owner topic \(usage.worstOwnerTopicThisHour) this hour")
        }
    }

    /// The pins that prove a binary works.
    private func pinsLine(_ seat: WakeSeat) -> some View {
        HStack(spacing: 14) {
            Text("Pin")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            if let pin = seat.pin {
                GridCellText(value: pin.binary ?? "—")
                GridCellText(value: pin.model ?? "—")
                GridCellText(value: pin.age ?? "no date")
            } else {
                GridCellText(value: "no pin:  nothing about this seat has been proved")
            }
            if let fallback = seat.fallbackPin {
                GridCellText(value: "fallback \(fallback.runtime) pinned \(fallback.age ?? "")")
            } else if seat.configuredWake != nil {
                GridCellText(value: "no fallback pin")
            }
        }
    }

    /// The seat's last few wakes and their outcomes.
    private func recentWakes(_ seat: WakeSeat) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Last wakes")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            if seat.recentWakes.isEmpty {
                GridCellText(value: "the ledger holds no wake for this seat")
            } else {
                ForEach(seat.recentWakes) { outcome in
                    HStack(spacing: 12) {
                        GridCellText(value: outcome.state)
                            .frame(width: 90, alignment: .leading)
                        GridCellText(value: outcome.topic ?? "—")
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .lineLimit(1)
                            .truncationMode(.tail)
                        GridCellText(value: outcome.owner ? "owner" : "seat")
                            .frame(width: 60, alignment: .leading)
                        GridCellText(value: outcome.at?.formatted(date: .omitted, time: .standard) ?? "—")
                            .frame(width: 80, alignment: .leading)
                    }
                }
            }
        }
    }
}

// MARK: - One row

/// One setting of one seat,  with the file's value and the listener's value side by side.
private struct WakeSettingRow: View {
    let setting: WakeSetting
    /// False when Muster will not write this setting.
    let editable: Bool
    @Binding var draft: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 12) {
                GridCellText(value: setting.title)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if editable {
                    TextField("", text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    GridCellText(value: setting.configuredText)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                GridCellText(value: setting.liveText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(setting.liveText)
                WakeStateBadge(state: setting.state, width: 108)
            }
            if !setting.note.isEmpty {
                Text(setting.note)
                    .font(.system(size: 10))
                    .foregroundStyle(setting.state.isProblem ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - The badge

/// One setting's state,  in the grid's own badge style.
private struct WakeStateBadge: View {
    let state: WakeSettingState
    var width: CGFloat = 108

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(state.tint)
                .frame(width: 7, height: 7)
            Text(state.label)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 7)
        .frame(width: width, height: 30)
        .background(state.tint.opacity(0.13), in: RoundedRectangle(cornerRadius: 7))
        .overlay {
            RoundedRectangle(cornerRadius: 7)
                .strokeBorder(state.tint.opacity(0.35), lineWidth: 1)
        }
        .contentShape(Rectangle())
        .help(state.note)
    }
}

private extension WakeSettingState {
    /// The colour the badge uses.
    ///
    /// The grid's own vocabulary:  green for a pass,  orange for drift,  indigo for a
    /// setting nobody has proved,  grey for one the engine does not own,  and red for a
    /// read that failed.
    var tint: Color {
        switch self {
        case .match: return .green
        case .drift: return .orange
        case .unproven: return .indigo
        case .notOwned, .unpublished: return .gray
        case .unset, .off: return .secondary
        case .unknown: return .red
        }
    }

    /// The badge's tooltip.
    var note: String {
        switch self {
        case .match: return "The listener is using the value the file holds."
        case .drift: return "The listener is using something other than the file's value."
        case .unproven: return "Configured,  but no pin has ever proved it works."
        case .notOwned: return "The engine does not enforce this setting,  so Muster will not write it."
        case .unpublished: return "The engine reads this one,  but does not publish it."
        case .unset: return "The file holds nothing,  so the listener's default is in effect."
        case .off: return "Off,  by the owner's ruling."
        case .unknown: return "This could not be read."
        }
    }
}

#Preview {
    WakeSettingsView()
        .environmentObject(FleetStore())
}
