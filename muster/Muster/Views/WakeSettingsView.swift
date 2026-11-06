import SwiftUI

/// The wake settings panel:  what every seat's wake is,  what the configuration file says
/// it should be,  and a way to change it.
///
/// The panel has two columns of truth and never mixes them.  The left column is the file,
/// and it is editable.  The middle column is the running eavesdropper's own account of itself.
/// When the two disagree the row says DRIFT,  and it is never a quiet green.  When the
/// eavesdropper's account is old, or it has stopped, a pass or a drift reads NOT CURRENT.
///
/// A setting the engine does not enforce is shown as NOT ENFORCED and is not editable:  the
/// eavesdropper counts wakes and not dollars,  so a dollar cap in the file would never bind.
///
/// The store lives as long as the app, so a test wake that is still running, its result, and
/// the owner's unapplied edits are all still here when the panel is opened again.
struct WakeSettingsView: View {
    enum SeatFilter: String, CaseIterable, Identifiable {
        case all = "All Seats"
        case problems = "Problems Only"
        case deferred = "With Deferred Wakes"

        var id: String { rawValue }
    }

    /// How often an open panel reads again on its own.  A read is local:  one status call and a
    /// few small files, so a minute keeps the age and the counters honest without cost.
    static let refreshSeconds: UInt64 = 60

    @EnvironmentObject private var fleet: FleetStore
    @Environment(\.scenePhase) private var scenePhase
    @ObservedObject private var store = WakeSettingsStore.shared
    @State private var seatFilter: SeatFilter = .all
    @State private var searchText: String = ""
    @State private var pendingWakeAction: PendingWakeAction?
    /// Whether the owner opened (true) or closed (false) a seat's settings by hand.  A seat the
    /// owner has not touched is open when it needs attention or holds an edit, and closed
    /// otherwise.  A choice the owner made stays, so typing an edit never folds the field away.
    @State private var seatOpen: [String: Bool] = [:]

    private var filteredSeats: [WakeSeat] {
        let base: [WakeSeat]
        switch seatFilter {
        case .all:
            base = store.model.seats
        case .problems:
            base = store.model.seats.filter(\.needsAttention)
        case .deferred:
            base = store.model.seats.filter {
                $0.usage.deferredTotalThisDay > 0 || $0.heldBackQueueCount > 0 || $0.heldBackTotalCount > 0 || $0.chatNudgesPending > 0 || $0.chatNudgesBlocked > 0
            }
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let matched = query.isEmpty ? base : base.filter {
            $0.seat.lowercased().contains(query)
                || ($0.bot?.lowercased().contains(query) ?? false)
                || ($0.liveWakeText?.lowercased().contains(query) ?? false)
                || ($0.configuredWake?.rawValue.contains(query) ?? false)
        }
        // Seats that need attention first, each group in roster order.
        return matched.filter(\.needsAttention) + matched.filter { !$0.needsAttention }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                listenerLine
                if !store.model.listenerErrors.isEmpty {
                    failureBox(
                        title: "The Mac's Eavesdropper Reports Problems",
                        text: store.model.listenerErrors.joined(separator: "\n")
                    )
                }
                if !store.model.problems.isEmpty {
                    problemBox(store.model.problems.joined(separator: "\n"))
                }
                if let message = store.message {
                    if store.messageIsFailure {
                        failureBox(title: "That Did Not Work", text: message)
                    } else {
                        messageBox(message)
                    }
                }
                daemonSection
                operatorSection
                seatFilterBar
                ForEach(filteredSeats) { seat in
                    seatSection(seat)
                    Divider()
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task {
            await store.refresh(roster: fleet.roster)
            // The slow timer:  the panel reads again on its own while it is open, so the age line
            // and the last-hour counters do not sit still.  A read never drops an edit, and one is
            // not started while another action runs.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.refreshSeconds * 1_000_000_000)
                if Task.isCancelled { break }
                if !store.isBusy { await store.refresh(roster: fleet.roster) }
            }
        }
        .onChange(of: scenePhase) { _, phase in
            // Back from the background or from sleep:  read again, so an old reading is not shown
            // as current beside live buttons.
            guard phase == .active, !store.isBusy else { return }
            Task { await store.refresh(roster: fleet.roster) }
        }
        .confirmationDialog(
            pendingWakeAction?.title ?? "",
            isPresented: Binding(get: { pendingWakeAction != nil }, set: { if !$0 { pendingWakeAction = nil } }),
            titleVisibility: .visible,
            presenting: pendingWakeAction
        ) { action in
            Button(action.confirmTitle, role: .destructive) {
                pendingWakeAction = nil
                Task { await perform(action) }
            }
            Button("Cancel", role: .cancel) { pendingWakeAction = nil }
        } message: { action in
            Text(action.message)
        }
    }

    /// Runs a confirmed action.  A change to what runs (the wake type, the fallback's container
    /// rule, or a pin) asks for the owner's Touch ID first, and a refusal runs nothing.
    @MainActor
    private func perform(_ action: PendingWakeAction) async {
        let needsOwner: Bool
        switch action.kind {
        case .apply: needsOwner = store.needsOwnerCheck(seat: action.seat)
        case .pin: needsOwner = true
        case .testWake, .removeSection, .resume: needsOwner = false
        }
        if needsOwner {
            // Anything but a yes runs nothing:  a cancel, a refused approval and a Mac that
            // cannot ask all stop here with their own words.
            switch await OwnerCheck.ask(reason: action.ownerReason) {
            case .approved:
                break
            case .notApproved(let why), .unavailable(let why):
                store.noteRefusal(why)
                return
            }
        }
        switch action.kind {
        case .apply: await store.apply(seat: action.seat)
        case .testWake: await store.testWake(seat: action.seat, runtime: action.runtime)
        case .pin: await store.pin(seat: action.seat, runtime: action.runtime)
        case .removeSection: await store.removeRefusedSection(seat: action.seat)
        case .resume: await store.resume(seat: action.seat)
        }
    }

    private var seatFilterBar: some View {
        HStack(spacing: 12) {
            Picker("Filter", selection: $seatFilter) {
                ForEach(SeatFilter.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 420)
            TextField("Filter seats by tag, bot, or wake type…", text: $searchText)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 11, design: .monospaced))
                .frame(maxWidth: 260)
            Spacer()
            Text("\(filteredSeats.count) of \(store.model.seats.count) seat(s)")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(MusterDestination.wakeAndListener.title)
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

    /// The Mac's eavesdropper's own account of itself, how old it is, and where it came from.
    private var listenerLine: some View {
        let model = store.model
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: runStateSymbol(model.runState))
                    .foregroundStyle(runStateTint(model.runState))
                    .accessibilityHidden(true)
                Text(model.runState.title)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                if let pid = model.pid, model.runState == .running {
                    Text("pid \(pid)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            if case .unknown(let why) = model.runState {
                Text(why)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 14) {
                if let updated = model.updated {
                    Label(
                        "Eavesdropper published \(FleetTime.clock(updated)), \(SeatActivityReader.ageText((model.readAt ?? Date()).timeIntervalSince(updated)))",
                        systemImage: model.statusIsCurrent ? "clock" : "clock.badge.exclamationmark"
                    )
                    .foregroundStyle(model.statusIsCurrent ? Color.secondary : Color.orange)
                } else if model.runState == .running {
                    Label("The status does not say when it was published", systemImage: "clock.badge.questionmark")
                        .foregroundStyle(.orange)
                }
                if let readAt = model.readAt {
                    Label("Read \(FleetTime.clock(readAt))", systemImage: "arrow.clockwise")
                        .foregroundStyle(.secondary)
                }
            }
            .font(.system(size: 10, design: .monospaced))
            if model.runState == .running, !model.statusIsCurrent {
                Text("Not current:  a running eavesdropper publishes every few seconds, so its values here may not be what it uses now.")
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if model.reportingSeats > 0 {
                let allConnected = model.connectedSeats == model.reportingSeats
                Label(
                    "\(model.connectedSeats) of \(model.reportingSeats) Mac seats connected",
                    systemImage: allConnected ? "link" : "link.badge.plus"
                )
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(allConnected ? Color.secondary : Color.orange)
            }
            Text("The server's eavesdropper is not read by this panel.\u{00A0} Seats it holds are listed as On The Server, read only, with nothing from the Mac's files.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !model.configPath.isEmpty {
                Text("Configuration file:  \(model.configPath)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private func runStateSymbol(_ state: ListenerRunState) -> String {
        switch state {
        case .running: return "checkmark.circle.fill"
        case .stopped: return "xmark.octagon.fill"
        case .unknown: return "questionmark.circle.fill"
        }
    }

    private func runStateTint(_ state: ListenerRunState) -> Color {
        switch state {
        case .running: return .green
        case .stopped: return .red
        case .unknown: return .gray
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
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    /// A failure:  a named heading and an icon as well as the colour.
    private func failureBox(title: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "xmark.octagon.fill")
                .foregroundStyle(.red)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.red)
                Text(text)
                    .font(.system(size: 11, design: .monospaced))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.red.opacity(0.10))
        .accessibilityElement(children: .combine)
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
            Text("The Eavesdropper")
                .font(.headline)
            WakeGridHeader(labels: ["Setting", "The File", "The Eavesdropper"])
            ForEach(store.model.daemonSettings) { setting in
                WakeSettingRow(
                    setting: setting,
                    editable: false,
                    changed: false,
                    moved: false,
                    draft: .constant(setting.configuredText)
                )
            }
        }
    }

    // MARK: The Operator

    /// The Operator:  one line of state, and where its names live behind a disclosure.
    private var operatorSection: some View {
        let options = store.model.operatorOptions
        return VStack(alignment: .leading, spacing: 6) {
            Text("The Operator")
                .font(.headline)
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: !options.isConfigured
                    ? "info.circle"
                    : (options.hasRequiredIdentity ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"))
                    .foregroundStyle(!options.isConfigured
                        ? Color.secondary
                        : (options.hasRequiredIdentity ? Color.green : Color.orange))
                    .accessibilityHidden(true)
                Text(options.summary)
                    .font(.system(size: 11))
                    .fixedSize(horizontal: false, vertical: true)
            }
            DisclosureGroup("Where The Operator's Names Live") {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(OperatorOptions.names) { name in
                        VStack(alignment: .leading, spacing: 1) {
                            GridCellText(value: name.name, emphasised: true)
                            GridCellText(value: "\(name.location):  \(name.purpose)")
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Text("Muster reads no credentials,  so it cannot say whether the Operator's key exists.")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 4)
            }
            .font(.caption)
        }
    }

    // MARK: One seat

    /// One seat.  A seat another eavesdropper holds gets no fields and no buttons:  a section for it
    /// would stop the Mac's eavesdropper, and its pause, test wake and pin could not reach it.
    @ViewBuilder
    private func seatSection(_ seat: WakeSeat) -> some View {
        if seat.host.isElsewhere {
            elsewhereSeatSection(seat)
        } else {
            localSeatSection(seat)
        }
    }

    /// Where a seat runs, in words and a symbol.
    private func hostBadge(_ host: ListenerSeatHost) -> some View {
        Label(host.badge, systemImage: host.systemImage)
            .font(.caption)
            .foregroundStyle(.secondary)
    }

    /// A seat the Mac's eavesdropper does not hold.
    private func elsewhereSeatSection(_ seat: WakeSeat) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(seat.seat)
                    .font(.headline)
                if let bot = seat.bot {
                    Text(bot)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                hostBadge(seat.host)
                Spacer()
            }
            if let reason = seat.host.readOnlyReason {
                Text(reason)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if seat.hasRefusedSection {
                if let problem = seat.refusedSectionProblem {
                    failureBox(title: "This Section Stops The Mac's Eavesdropper", text: problem)
                }
                Button {
                    pendingWakeAction = PendingWakeAction(kind: .removeSection, seat: seat.seat, runtime: nil)
                } label: {
                    Label("Remove This Section…", systemImage: "trash")
                }
                .disabled(store.isBusy)
            }
        }
        .padding(.vertical, 6)
    }

    /// What the eavesdropper says about a seat it holds, or could hold.
    @ViewBuilder
    private func seatNotices(_ seat: WakeSeat) -> some View {
        if let reason = seat.host.readOnlyReason {
            problemBox(reason)
        }
        if !seat.skippedReasons.isEmpty {
            failureBox(
                title: "The Eavesdropper Will Not Run This Seat",
                text: seat.skippedReasons.joined(separator: "\n")
            )
        }
        if seat.disabledInFile {
            Text("The file turns this seat off, so the Mac's eavesdropper holds no queue for it.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else if !seat.hasSection {
            Text("The file has no section for this seat, so the Mac's eavesdropper holds no queue for it.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// The seat's wake, by the eavesdropper's own word.  A word this app does not know is shown as
    /// itself, never as no wake.
    @ViewBuilder
    private func wakeLabel(_ seat: WakeSeat) -> some View {
        if let wake = seat.liveWake {
            Text(wake.title)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(wake.runsWorker ? Color.primary : Color.orange)
            if !wake.runsWorker {
                Text("runs no worker")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .help("An inbox seat captures every DM and tag,  and runs nothing.")
            }
        } else if let raw = seat.liveWakeText {
            Label("\(raw), a wake this app does not know", systemImage: "questionmark.circle")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.orange)
        } else {
            Text("wake not reported")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
        }
    }

    /// The seat's connection, in words and a symbol.
    @ViewBuilder
    private func connectionLabel(_ seat: WakeSeat) -> some View {
        switch seat.connected {
        case .some(true):
            Label("Connected", systemImage: "link")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .some(false):
            Label("Disconnected", systemImage: "bolt.horizontal.circle")
                .font(.caption)
                .foregroundStyle(.orange)
        case .none:
            EmptyView()
        }
    }

    /// The pause that covers a seat, in words:  what it stops, who set it and when.
    @ViewBuilder
    private func pauseLabel(_ seat: WakeSeat) -> some View {
        if seat.paused || seat.wakesPaused {
            let what = seat.paused ? "Paused" : "Wakes Paused"
            let scope = seat.pauseScope == "*" ? " by the pause on every seat" : ""
            let by = seat.pauseBy.map { " by \($0)" } ?? ""
            let at = seat.pauseAt.map { " at \(FleetTime.clock($0))" } ?? ""
            Label("\(what)\(scope)\(by)\(at)", systemImage: "pause.circle.fill")
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .foregroundStyle(seat.paused ? Color.red : Color.orange)
        }
    }

    /// A seat on the Mac, or one whose holder is not known.  The second shows what it can and
    /// offers only the pause.
    private func localSeatSection(_ seat: WakeSeat) -> some View {
        let changed = store.changedKeys(seat: seat.seat)
        let expanded = seat.settingsOpen(choice: seatOpen[seat.seat], hasEdits: !changed.isEmpty)
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(seat.seat)
                    .font(.headline)
                hostBadge(seat.host)
                if let bot = seat.bot {
                    Text(bot)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                wakeLabel(seat)
                connectionLabel(seat)
                pauseLabel(seat)
                Spacer()
                pauseButtons(seat)
            }
            summaryLine(seat)
            seatNotices(seat)
            runsLine(seat)
            if !seat.redWarnings.isEmpty {
                ForEach(seat.redWarnings, id: \.self) { warning in
                    problemBox(warning)
                }
            }
            Button {
                seatOpen[seat.seat] = !expanded
            } label: {
                Label(
                    expanded ? "Hide Settings" : "Show Settings",
                    systemImage: expanded ? "chevron.down" : "chevron.right"
                )
                .font(.caption)
            }
            .buttonStyle(.borderless)
            if expanded {
                settingsGrid(seat, changed: changed)
            }
            usageLine(seat)
            deferredAndQuotaLine(seat)
            pinsLine(seat)
            recentWakes(seat)
            if let running = store.running, running.seat == seat.seat {
                runningLine(running)
            }
            // The seat's own wake and its fallback each keep their last answer, newest first.
            ForEach(Array(store.allTestReports(seat: seat.seat).enumerated()), id: \.offset) { _, report in
                testReportBox(report)
            }
            if seat.host.isTheMac {
                actionButtons(seat, changed: changed)
            }
        }
        .padding(.vertical, 6)
    }

    /// The pause and resume, only where they do something.  A seat under a full pause is not
    /// offered a wakes-only pause, which would loosen it.
    @ViewBuilder
    private func pauseButtons(_ seat: WakeSeat) -> some View {
        if seat.paused || seat.wakesPaused {
            Button {
                pendingWakeAction = PendingWakeAction(kind: .resume, seat: seat.seat, runtime: nil)
            } label: {
                Label("Resume…", systemImage: "play.circle")
            }
            .disabled(store.isBusy || !seat.host.isTheMac)
            .help(seat.host.readOnlyReason ?? "Lifts this seat's pause on the Mac's eavesdropper.")
        } else {
            Button {
                Task { await store.pause(seat: seat.seat) }
            } label: {
                Label("Pause Wakes", systemImage: "pause.circle")
            }
            .disabled(store.isBusy)
            .help("Pauses this seat's wakes on the Mac's eavesdropper.\u{00A0} Its messages are still captured.")
        }
    }

    /// One line that says how the seat stands, before any of its rows.
    private func summaryLine(_ seat: WakeSeat) -> some View {
        var parts: [String] = []
        if seat.usageProblem != nil {
            parts.append("use not known")
        } else {
            let floor = seat.usage.atLeast ? "at least " : ""
            let cap = seat.cap(.wakesPerHour).map { " of \($0)" } ?? ""
            parts.append("\(floor)\(seat.usage.wakesThisHour)\(cap) wakes in the last hour")
        }
        if seat.heldBackQueueCount > 0 {
            parts.append("\(seat.heldBackQueueCount) held for later")
        }
        let problems = seat.problems.count
        if problems > 0 {
            parts.append("\(problems) setting\(problems == 1 ? "" : "s") need\(problems == 1 ? "s" : "") a look")
        }
        if seat.pinState.isProblem {
            parts.append("pin needs a look")
        }
        return HStack(spacing: 6) {
            Image(systemName: seat.needsAttention ? "exclamationmark.triangle.fill" : "checkmark.circle")
                .foregroundStyle(seat.needsAttention ? Color.orange : Color.secondary)
                .accessibilityHidden(true)
            Text((seat.needsAttention ? "Needs a look:  " : "No problem found:  ") + parts.joined(separator: ", "))
                .font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// What the seat runs, as the eavesdropper resolved it.
    @ViewBuilder
    private func runsLine(_ seat: WakeSeat) -> some View {
        let facts: [String] = {
            var facts: [String] = []
            if let claude = seat.claudeBinary { facts.append("claude at \(claude)") }
            if let provider = seat.provider, !provider.isEmpty { facts.append("provider \(provider)") }
            if let routine = seat.routine {
                if routine.ready == true {
                    facts.append("routine ready\(routine.host.map { " at \($0)" } ?? "")")
                } else {
                    let why = routine.detail ?? routine.why ?? "no reason given"
                    facts.append("routine not ready:  \(why)")
                }
            }
            return facts
        }()
        if !facts.isEmpty {
            HStack(alignment: .top, spacing: 6) {
                Text("Runs")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(facts.joined(separator: ";  "))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(seat.routine?.ready == false ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
    }

    /// The settings that mean something for the seat's wake, plus any the file holds anyway.
    private func settingsGrid(_ seat: WakeSeat, changed: Set<WakeSettingKey>) -> some View {
        let values = store.draft[seat.seat] ?? [:]
        let draftWake = Self.setValue(values[.wakeType]).flatMap(WakeType.init(rawValue:))?.rawValue
        let wake = draftWake ?? seat.configuredWake?.rawValue ?? seat.liveWakeText
        let fallback = Self.setValue(values[.fallback])
        let shown = seat.settings.filter {
            $0.key.applies(toWake: wake, fallback: fallback) || $0.configured != nil
        }
        let hidden = seat.settings.count - shown.count
        let hiddenText = hidden == 1
            ? "1 setting that does not apply to a \(wake ?? "inbox") wake is not shown."
            : "\(hidden) settings that do not apply to a \(wake ?? "inbox") wake are not shown."
        let moved = store.movedUnderneath[seat.seat] ?? []
        return VStack(alignment: .leading, spacing: 4) {
            WakeGridHeader(labels: ["Setting", "The File", "The Eavesdropper"])
            ForEach(shown) { setting in
                WakeSettingRow(
                    setting: setting,
                    editable: seat.host.isTheMac && setting.key.refusalReason == nil,
                    changed: changed.contains(setting.key),
                    moved: moved.contains(setting.key),
                    draft: draftBinding(seat: seat.seat, key: setting.key)
                )
            }
            if hidden > 0 {
                Text(hiddenText)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// A draft value with its spaces taken off, or nil when it says nothing is set.
    private static func setValue(_ text: String?) -> String? {
        guard let trimmed = text?.trimmingCharacters(in: .whitespaces),
              !trimmed.isEmpty, trimmed != WakeSetting.unsetText else { return nil }
        return trimmed
    }

    /// Apply and Revert, then the test wake and the pin, each enabled only where it can work.
    private func actionButtons(_ seat: WakeSeat, changed: Set<WakeSettingKey>) -> some View {
        let testRuntime = seat.testRuntime
        let noTest = seat.configuredWake?.noPinReason
            ?? (seat.configuredWake == nil ? "The file names no wake for this seat, so it is an inbox seat and runs nothing to test." : nil)
        let pinGate = testRuntime.map { store.pinGate(seat: seat.seat, runtime: $0, pinKey: $0) }
        let pinBlocker: String?
        if let gate = pinGate {
            pinBlocker = gate.blocker
        } else {
            pinBlocker = noTest
        }
        let fallback = seat.fallbackRuntime
        let fallbackKey = fallback.map(WakeType.fallbackPinKey)
        let fallbackPinGate = fallbackKey.map { store.pinGate(seat: seat.seat, runtime: "fallback", pinKey: $0) }
        let fallbackPinBlocker = fallbackPinGate?.blocker
        let busy = store.isBusy || store.running != nil
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Button {
                    pendingWakeAction = PendingWakeAction(
                        kind: .apply,
                        seat: seat.seat,
                        runtime: nil,
                        detail: store.applySummary(seat: seat.seat)
                    )
                } label: {
                    Label("Apply…", systemImage: "square.and.arrow.down")
                }
                .disabled(busy || changed.isEmpty)
                .help(changed.isEmpty ? "Nothing is edited for this seat." : "Shows each change before anything is written.")
                Button {
                    store.revert(seat: seat.seat)
                } label: {
                    Label("Revert", systemImage: "arrow.uturn.backward")
                }
                .disabled(changed.isEmpty)
                .help("Puts this seat's fields back to what the file holds.")
                Divider().frame(height: 16)
                Button {
                    pendingWakeAction = PendingWakeAction(kind: .testWake, seat: seat.seat, runtime: testRuntime)
                } label: {
                    Label("Test Wake…", systemImage: "play.and.pause")
                }
                .disabled(busy || testRuntime == nil)
                .help(noTest ?? "Runs this seat's wake on canned hostile messages.\u{00A0} Nothing is posted.")
                Button {
                    pendingWakeAction = PendingWakeAction(
                        kind: .pin, seat: seat.seat, runtime: testRuntime,
                        candidateAt: pinGate?.candidateAt
                    )
                } label: {
                    Label("Pin…", systemImage: "pin")
                }
                .disabled(busy || testRuntime == nil || pinBlocker != nil)
                .help(pinBlocker ?? pinGate.map(Self.pinHelp) ?? "Pins what the passing test wake ran.\u{00A0} It asks for Touch ID.")
                if let fallback, testRuntime != nil {
                    Divider().frame(height: 16)
                    Button {
                        pendingWakeAction = PendingWakeAction(kind: .testWake, seat: seat.seat, runtime: "fallback", fallback: fallback)
                    } label: {
                        Label("Test Fallback…", systemImage: "play.and.pause")
                    }
                    .disabled(busy)
                    .help("Runs the \(fallback) fallback on the same canned hostile messages.\u{00A0} Nothing is posted.")
                    Button {
                        pendingWakeAction = PendingWakeAction(
                            kind: .pin, seat: seat.seat, runtime: "fallback", fallback: fallback,
                            candidateAt: fallbackPinGate?.candidateAt
                        )
                    } label: {
                        Label("Pin Fallback…", systemImage: "pin")
                    }
                    .disabled(busy || fallbackPinBlocker != nil)
                    .help(fallbackPinBlocker ?? fallbackPinGate.map(Self.pinHelp) ?? "Pins what the passing fallback test ran.\u{00A0} It asks for Touch ID.")
                }
            }
            if let noTest {
                Text(noTest)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let pinBlocker {
                Text("Pin:  \(pinBlocker)")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let note = pinGate?.confirmationNote {
                Label("Pin:  \(note)", systemImage: "eye.slash")
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// What Pin's tooltip says when it is offered:  that it pins the test this panel showed, or
    /// that Muster never saw the answer of a test run elsewhere.
    private static func pinHelp(_ gate: WakeSettingsStore.PinGate) -> String {
        if let note = gate.confirmationNote {
            return note + "\u{00A0} It asks for Touch ID."
        }
        return "Pins what the passing test shown here ran.\u{00A0} It asks for Touch ID."
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

    /// A small grid of labelled facts that wraps to the panel's width.
    private func factGrid(title: String, facts: [(text: String, emphasised: Bool)]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 220), spacing: 12, alignment: .leading)],
                alignment: .leading,
                spacing: 3
            ) {
                ForEach(Array(facts.enumerated()), id: \.offset) { _, fact in
                    GridCellText(value: fact.text, emphasised: fact.emphasised)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    /// How much of the budget the seat has used, beside the cap it is held to.  The windows are
    /// rolling:  the last hour and the last 24 hours, counted the way the runtime counts them.
    @ViewBuilder
    private func usageLine(_ seat: WakeSeat) -> some View {
        if let problem = seat.usageProblem {
            problemBox(problem)
        } else {
            factGrid(title: "Used", facts: usageFacts(seat))
        }
    }

    /// The counters beside their caps.  A count at or over its cap is emphasised.
    private func usageFacts(_ seat: WakeSeat) -> [(text: String, emphasised: Bool)] {
        let usage = seat.usage
        let floor = usage.atLeast ? "at least " : ""
        func of(_ count: Int, _ key: WakeSettingKey, _ what: String) -> (text: String, emphasised: Bool) {
            let cap = seat.cap(key)
            let capped = cap.flatMap { Double($0) }.map { Double(count) >= $0 } ?? false
            let limit = cap.map { " of \($0)" } ?? " (no cap read)"
            return ("\(floor)\(count)\(limit) \(what)", capped)
        }
        var facts = [
            of(usage.wakesThisHour, .wakesPerHour, "wakes in the last hour"),
            of(usage.wakesThisDay, .wakesPerDay, "wakes in the last 24 hours"),
            of(usage.ownerThisDay, .ownerPerDay, "owner wakes in the last 24 hours"),
            of(usage.worstTopicThisHour, .perTopicPerHour, "on the busiest topic, last hour"),
            of(usage.worstOwnerTopicThisHour, .ownerPerTopicPerHour, "on the busiest owner topic, last hour"),
        ]
        if usage.failedThisDay > 0 {
            facts.append(("\(floor)\(usage.failedThisDay) failed in the last 24 hours", true))
        }
        return facts
    }

    /// Deferred and held-back wakes, chat nudges, and provider quota.
    private func deferredAndQuotaLine(_ seat: WakeSeat) -> some View {
        var facts: [(text: String, emphasised: Bool)] = []
        if seat.usageProblem == nil {
            let usage = seat.usage
            let floor = usage.atLeast ? "at least " : ""
            facts.append(("\(floor)\(usage.heldBackThisDay) held back in the last 24 hours", false))
            facts.append(("\(floor)\(usage.droppedThisDay) dropped in the last 24 hours", usage.droppedThisDay > 0))
            facts.append(("\(floor)\(usage.skippedThisDay) skipped in the last 24 hours", false))
        }
        if seat.heldBackQueueCount > 0 || seat.heldBackTotalCount > 0 {
            let reasons = seat.heldBackReasons.sorted { $0.key < $1.key }
                .map { "\($0.value) \(WakeLedger.words(forReason: $0.key))" }
                .joined(separator: ", ")
            let oldest = seat.heldBackOldestAge.map { ", oldest \(FleetTime.age(Int($0)))" } ?? ""
            let why = reasons.isEmpty ? "" : " (\(reasons))"
            facts.append(("held for later now:  \(seat.heldBackQueueCount)\(why)\(oldest)", seat.heldBackQueueCount > 0))
            facts.append(("held since start:  \(seat.heldBackTotalCount), \(seat.heldBackSurfacedCount) shown to the seat", false))
        }
        if seat.chatNudgesPending > 0 || seat.chatNudgesBlocked > 0 || seat.chatNudgesAccepted > 0 {
            facts.append(("chat nudges:  \(seat.chatNudgesPending) pending, \(seat.chatNudgesBlocked) blocked, \(seat.chatNudgesAccepted) accepted", seat.chatNudgesBlocked > 0))
        }
        if seat.quotaRemaining != nil || seat.quotaExhausted {
            var text = seat.quotaExhausted ? "quota exhausted" : "quota \(Int(seat.quotaRemaining ?? 0))% left"
            if seat.quotaExhausted, let remaining = seat.quotaRemaining {
                text += " (\(Int(remaining))% reported)"
            }
            if let source = seat.quotaSource { text += ", from \(source)" }
            if let reason = seat.quotaReason, !reason.isEmpty { text += ":  \(reason)" }
            if let reset = seat.quotaResetAt { text += ", resets \(FleetTime.clock(reset))" }
            facts.append((text, seat.quotaExhausted))
        }
        return factGrid(title: "Deferred And Held", facts: facts)
    }

    /// The pin the seat's own wake needs, and the fallback's, judged by the eavesdropper.
    private func pinsLine(_ seat: WakeSeat) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("Pin")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            pinFact(seat.pinState)
            if let fallbackState = seat.fallbackPinState {
                pinFact(fallbackState, missingAdvice: "Test the fallback, read the result, then pin it.")
            }
        }
    }

    /// One pin, in words.  A pin file nobody gave a current verdict on is shown with a question
    /// mark, never a check mark:  that it is there does not say it holds.
    @ViewBuilder
    private func pinFact(_ state: SeatPinState, missingAdvice: String? = nil) -> some View {
        switch state {
        case .notNeeded(let why):
            factLine("No pin needed.\u{00A0} \(why)", problem: false)
        case .pinned(let pin):
            factLine("\(PinEvidence.runtimeTitle(pin.runtime)) pin holds:  \(pin.binary ?? pin.model ?? "no binary named")\(pin.model.map { ", model \($0)" } ?? ""), \(pin.age ?? "no date")", problem: false)
        case .notHolding(let pin, let why):
            factLine("\(PinEvidence.runtimeTitle(pin.runtime)) pin does not hold.\u{00A0} \(PinEvidence.words(forWhy: why))\u{00A0} Pinned \(pin.binary ?? pin.model ?? "nothing named"), \(pin.age ?? "no date").", problem: true)
        case .fileOnly(let pin, let why, let needsLook):
            factLine("\(PinEvidence.runtimeTitle(pin.runtime)) pin file is there:  \(pin.binary ?? pin.model ?? "no binary named"), \(pin.age ?? "no date").\u{00A0} \(why)", problem: needsLook, unknown: true)
        case .missing(let runtime, let why):
            factLine("No \(PinEvidence.runtimeTitle(runtime)) pin.\u{00A0} \(missingAdvice ?? PinEvidence.words(forWhy: why ?? "not_pinned"))", problem: true)
        case .unreadable(let runtime, let problem):
            factLine("The \(PinEvidence.runtimeTitle(runtime)) pin could not be read.\u{00A0} \(problem)", problem: true)
        case .unknownWake:
            factLine("The seat's wake is not one this app knows, so it cannot say which pin the seat needs.", problem: true)
        }
    }

    /// One fact, with a symbol as well as a colour, so colour is never the only sign:  a warning
    /// for a problem, a question mark for something not known, and a check mark otherwise.
    private func factLine(_ text: String, problem: Bool, unknown: Bool = false) -> some View {
        HStack(alignment: .top, spacing: 5) {
            Image(systemName: problem ? "exclamationmark.triangle.fill" : (unknown ? "questionmark.circle" : "checkmark.circle"))
                .font(.system(size: 10))
                .foregroundStyle(problem ? Color.orange : Color.secondary)
                .accessibilityHidden(true)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(problem ? Color.primary : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }

    /// The seat's last few wakes and their outcomes.
    private func recentWakes(_ seat: WakeSeat) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Last Wakes")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            if seat.usageProblem != nil {
                GridCellText(value: "not known:  the ledger could not be read")
            } else if seat.recentWakes.isEmpty {
                GridCellText(value: "the Mac's ledger holds no wake for this seat")
            } else {
                ForEach(seat.recentWakes) { outcome in
                    HStack(spacing: 12) {
                        GridCellText(value: WakeLedger.words(forState: outcome.state))
                            .frame(width: 110, alignment: .leading)
                        GridCellText(value: outcome.reason.map(WakeLedger.words(forReason:)) ?? "—")
                            .frame(width: 140, alignment: .leading)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .help(outcome.reason ?? "")
                        GridCellText(value: outcome.topic ?? "—")
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .help(outcome.topic ?? "")
                        GridCellText(value: outcome.owner ? "owner" : "seat")
                            .frame(width: 50, alignment: .leading)
                        GridCellText(value: outcome.at.map { "\(FleetTime.clock($0)), \(FleetTime.ageText(since: $0)) ago" } ?? "—")
                            .frame(width: 140, alignment: .leading)
                    }
                }
            }
        }
    }

    /// A test wake or a pin that is still running, with how long it has run.
    private func runningLine(_ running: WakeSettingsStore.RunningAction) -> some View {
        TimelineView(.periodic(from: running.startedAt, by: 1)) { context in
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                let seconds = max(0, Int(context.date.timeIntervalSince(running.startedAt)))
                Text("\(running.title) running since \(FleetTime.clock(running.startedAt)), \(seconds / 60)m \(seconds % 60)s so far.\u{00A0} It can take up to \(Int(AgentSyncInvocation.testWakeTimeout / 60)) minutes, and leaving this panel does not stop it.")
                    .font(.system(size: 11))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The last test wake's answer, in words and in full.
    private func testReportBox(_ report: TestWakeReport) -> some View {
        let title: String
        let symbol: String
        let tint: Color
        switch report.verdict {
        case .passed:
            title = report.kind == .run ? "Test Wake Passed" : "Pinned"
            symbol = "checkmark.seal.fill"
            tint = .green
        case .failed:
            title = report.kind == .run ? "Test Wake Did Not Pass" : "Not Pinned"
            symbol = "xmark.octagon.fill"
            tint = .red
        case .unclear:
            title = "Test Wake Gave No Verdict"
            symbol = "questionmark.circle.fill"
            tint = .orange
        }
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: symbol)
                    .foregroundStyle(tint)
                    .accessibilityHidden(true)
                Text("\(title)\(report.runtime.map { " (\($0))" } ?? "")")
                    .font(.system(size: 11, weight: .semibold))
                Text("finished \(FleetTime.clock(report.finishedAt))")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            ForEach(Array(report.summary.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.system(size: 11))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            DisclosureGroup("Everything It Printed") {
                ScrollView {
                    Text(report.output + (report.errors.isEmpty ? "" : "\n\nstderr:\n" + report.errors))
                        .font(.system(size: 10, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 280)
            }
            .font(.caption)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(tint.opacity(0.08))
    }
}

// MARK: - The grid header

/// The header over the setting rows, in the rows' own columns:  three that share the width and
/// the state badge's fixed column, so each label sits over its values.
private struct WakeGridHeader: View {
    let labels: [String]

    var body: some View {
        HStack(spacing: 12) {
            ForEach(labels, id: \.self) { label in
                Text(label)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("State")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 108, alignment: .leading)
        }
    }
}

// MARK: - One row

/// One setting of one seat,  with the file's value and the eavesdropper's value side by side.
private struct WakeSettingRow: View {
    let setting: WakeSetting
    /// False when Muster will not write this setting.
    let editable: Bool
    /// Whether the owner edited this setting and has not applied it.
    let changed: Bool
    /// Whether the file's value moved under that edit.
    let moved: Bool
    @Binding var draft: String

    /// The text field shows nothing for a setting the file does not hold, with a placeholder, and
    /// an emptied field means take the setting out, as the dash does.
    private var fieldText: Binding<String> {
        Binding(
            get: { draft == WakeSetting.unsetText ? "" : draft },
            set: { draft = $0 }
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 12) {
                HStack(spacing: 4) {
                    GridCellText(value: setting.title, emphasised: changed)
                    if changed {
                        Label("Edited", systemImage: "pencil")
                            .labelStyle(.iconOnly)
                            .font(.system(size: 9))
                            .foregroundStyle(.blue)
                            .help("Edited here and not applied yet.")
                            .accessibilityLabel("Edited, not applied")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if editable, let options = setting.key.pickerOptions(current: draft) {
                    // A short fixed list is a picker, so a typo cannot drop the seat to inbox.
                    Picker(setting.title, selection: $draft) {
                        ForEach(options, id: \.self) { option in
                            Text(option == WakeSetting.unsetText ? "not set" : option).tag(option)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else if editable {
                    TextField(setting.title, text: fieldText, prompt: Text("not set, default"))
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    GridCellText(value: setting.configured?.displayText ?? "not set")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                GridCellText(value: setting.live?.displayText ?? "not reported")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(setting.live?.displayText ?? "The eavesdropper did not report a value.")
                WakeStateBadge(state: setting.state, width: 108)
            }
            if !setting.note.isEmpty {
                Text(setting.note)
                    .font(.system(size: 10))
                    .foregroundStyle(setting.state.isProblem ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if moved {
                Label(
                    "The file now holds \(setting.configuredText == WakeSetting.unsetText ? "nothing" : setting.configuredText).\u{00A0} Your edit is kept, and Apply writes it over that.",
                    systemImage: "exclamationmark.arrow.triangle.2.circlepath"
                )
                .font(.system(size: 10))
                .foregroundStyle(.orange)
            }
            if setting.key == .fallbackUncontained, changed, draft == "true" {
                Label(
                    "This lets the mcode fallback answer with its tools live, outside any container.\u{00A0} A side effect it causes cannot be undone.\u{00A0} Apply asks for Touch ID.",
                    systemImage: "exclamationmark.shield.fill"
                )
                .font(.system(size: 10))
                .foregroundStyle(.red)
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
        case .drift, .stale: return .orange
        case .unproven: return .indigo
        case .notOwned, .unpublished: return .gray
        case .unset, .off, .heldElsewhere, .seatOff: return .secondary
        case .unknown: return .red
        }
    }

    /// The badge's tooltip.
    var note: String {
        switch self {
        case .match: return "The eavesdropper is using the value the file holds.\u{00A0} This compares two values, and it does not prove the setting works."
        case .drift: return "The eavesdropper is using something other than the file's value."
        case .unproven: return "Configured,  but no pin has ever proved it works."
        case .notOwned: return "The engine does not enforce this setting,  so Muster will not write it."
        case .unpublished: return "The engine reads this one,  but does not publish it."
        case .unset: return "The file holds nothing,  so the eavesdropper's default is in effect."
        case .off: return "Off,  by the owner's ruling."
        case .unknown: return "Not known:  the eavesdropper did not report it, or the file was not read."
        case .heldElsewhere: return "The Mac's eavesdropper does not hold this seat."
        case .seatOff: return "The Mac's eavesdropper is not running this seat, so it is not using this setting."
        case .stale: return "The eavesdropper's status is old, or it has stopped, so this may not be what it uses now."
        }
    }
}

#Preview {
    WakeSettingsView()
        .environmentObject(FleetStore())
}

/// A wake action waiting for the owner's confirmation, with its concrete effect.
private struct PendingWakeAction: Identifiable {
    enum Kind { case apply, testWake, pin, removeSection, resume }
    let kind: Kind
    let seat: String
    let runtime: String?
    /// The fallback runtime, when the action is on the seat's fallback.
    var fallback: String? = nil
    /// The changes this would make, in words.
    var detail: String = ""
    /// For a pin let through by a candidate from a test run outside this panel:  when that test
    /// left it.  Muster never saw its answer, so the confirmation says so.
    var candidateAt: Date? = nil
    var id: String { "\(kind)-\(seat)-\(runtime ?? "")" }

    /// What is being tested or pinned, in words.
    private var subject: String {
        if let fallback { return "\(seat)'s \(fallback) fallback" }
        return "\(seat)'s wake"
    }

    var title: String {
        switch kind {
        case .apply: return "Apply Settings For \(seat)?"
        case .testWake: return fallback == nil ? "Run A Test Wake For \(seat)?" : "Test The Fallback For \(seat)?"
        case .pin: return fallback == nil ? "Pin The Wake For \(seat)?" : "Pin The Fallback For \(seat)?"
        case .removeSection: return "Remove The \(seat) Section From The Mac's File?"
        case .resume: return "Resume Wakes For \(seat)?"
        }
    }
    var confirmTitle: String {
        switch kind {
        case .apply: return "Apply"
        case .testWake: return "Run Test Wake"
        case .pin: return "Pin"
        case .removeSection: return "Remove Section"
        case .resume: return "Resume"
        }
    }
    /// Why Touch ID is asked, as the system dialog shows it.
    var ownerReason: String {
        switch kind {
        case .apply: return "change \(seat)'s wake settings"
        case .pin: return "pin \(subject)"
        case .testWake, .removeSection, .resume: return "change \(seat)'s wake"
        }
    }
    var message: String {
        switch kind {
        case .apply:
            let opening = "This checks \(seat)'s edited settings with the eavesdropper's own rules, writes them to the Mac's eavesdropper configuration file, and asks the eavesdropper to re-read it.\u{00A0} Muster then says whether the eavesdropper took the change."
            return detail.isEmpty ? opening : opening + "\n\n" + detail
        case .testWake:
            return "This runs \(subject) on this Mac against canned hostile messages.\u{00A0} Nothing is posted, and no wake budget is spent.\u{00A0} It uses the provider's quota and can take up to \(Int(AgentSyncInvocation.testWakeTimeout / 60)) minutes.\u{00A0} You then read its answer and decide whether to pin it."
        case .pin:
            let pins = "This pins what the last passing test of \(subject) ran, as proved.\u{00A0} The eavesdropper refuses a test over a day old, or one whose binary or model has changed since.\u{00A0} It asks for Touch ID."
            guard let at = candidateAt,
                  let note = WakeSettingsStore.PinGate.candidate(at).confirmationNote else { return pins }
            return note + "\n\n" + pins
        case .removeSection:
            return "This takes the [seat.\(seat)] section out of the Mac's eavesdropper configuration file and keeps a backup of the old file.\u{00A0} The Mac's eavesdropper refuses this section, so it can only stop the eavesdropper."
        case .resume:
            return "This lifts \(seat)'s own pause on the Mac's eavesdropper, so its wakes can run again.\u{00A0} A pause on every seat, if one is in force, stays."
        }
    }
}
