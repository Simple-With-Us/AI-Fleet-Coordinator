import SwiftUI

/// The wake settings panel:  whether each seat Wakes or Captures Only,  what the configuration
/// file says,  and a way to change what may be changed.
///
/// A seat Wakes when its `[seat.X]` section holds a routine table, and Captures Only when it holds
/// none.  Muster never reads the old `wake` line to decide which, and never writes one.  For a
/// seat that Wakes, the routine's address and key variables and its timeout can be changed, and
/// the method, signing and header the router contract fixes are shown and never offered.
///
/// The panel has two columns of truth and never mixes them.  The left column is the file,
/// and it is editable.  The middle column is the running eavesdropper's own account of itself.
/// When the two disagree the row says DRIFT,  and it is never a quiet green.  When the
/// eavesdropper's account is old, or it has stopped, a pass or a drift reads NOT CURRENT.
///
/// A setting the engine does not enforce is shown as NOT ENFORCED and is not editable:  the
/// eavesdropper counts wakes and not dollars,  so a dollar cap in the file would never bind.
///
/// The store lives as long as the app, so the owner's unapplied edits are still here when the
/// panel is opened again.
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
    /// The problem box's lines:  what could not be read,  and the Operator when it cannot post.
    /// The Operator's voice comes from the same status as the rest of the panel.
    private var problemLines: [String] {
        store.model.problems + store.model.operatorVoice.problemLines(seats: store.model.seats)
    }

    private var filteredSeats: [WakeSeat] {
        let base: [WakeSeat]
        switch seatFilter {
        case .all:
            base = store.model.seats
        case .problems:
            base = store.model.seats.filter(\.needsAttention)
        case .deferred:
            base = store.model.seats.filter {
                $0.usage.deferredTotalThisDay > 0 || $0.heldBackQueueCount > 0 || $0.heldBackTotalCount > 0 || $0.heldBackEvicted > 0 || $0.chatNudgesPending > 0 || $0.chatNudgesBlocked > 0
            }
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let matched = query.isEmpty ? base : base.filter {
            $0.seat.lowercased().contains(query)
                || ($0.bot?.lowercased().contains(query) ?? false)
                || $0.kind.title.lowercased().contains(query)
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
                if !problemLines.isEmpty {
                    problemBox(problemLines.joined(separator: "\n"))
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

    /// Runs a confirmed action.  A change to the routine's address or key variable asks for the
    /// owner's Touch ID first, and a refusal runs nothing.
    @MainActor
    private func perform(_ action: PendingWakeAction) async {
        let needsOwner: Bool
        switch action.kind {
        case .apply: needsOwner = store.needsOwnerCheck(seat: action.seat)
        case .removeSection, .resume: needsOwner = false
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
            TextField("Filter seats by tag, bot, Wakes or Captures Only…", text: $searchText)
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
            Text("Whether each seat Wakes or Captures Only,  what the configuration file says,  and a way to change what may be changed.")
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
            // The model words the reason:  a status another instance wrote can be fresh and still
            // not be the Mac's, so its age is not always why.
            if let reason = model.notCurrentReason {
                Text(reason)
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
            if let rewake = model.rewake {
                Label(rewake.sentence, systemImage: rewake.isProblem ? "exclamationmark.triangle" : "arrow.uturn.backward.circle")
                    .font(.system(size: 10))
                    .foregroundStyle(rewake.isProblem ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
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
                // A file that was not read says nothing about its Operator section, so it gets a
                // question mark, never the plain information sign of a file with no section.
                Image(systemName: !options.isKnown
                    ? "questionmark.circle"
                    : (!options.isConfigured
                        ? "info.circle"
                        : (options.hasRequiredIdentity ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")))
                    .foregroundStyle(!options.isKnown || !options.isConfigured
                        ? Color.secondary
                        : (options.hasRequiredIdentity ? Color.green : Color.orange))
                    .accessibilityHidden(true)
                Text(options.summary)
                    .font(.system(size: 11))
                    .fixedSize(horizontal: false, vertical: true)
            }
            operatorVoiceLine
            deciderLine
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

    /// The decider every platform shares, for when a seat's routine cannot pick a chat:  its
    /// runtime and its model slug, read only, from `[decider.consult_backup]`.  No key is ever
    /// shown, and there are no controls:  changing it is a deliberate edit of the file.
    private var deciderLine: some View {
        let decider = store.model.operatorOptions.decider
        let symbol: String
        switch decider {
        case .set: symbol = decider.hasUnexpectedRuntime ? "exclamationmark.triangle.fill" : "checkmark.circle"
        case .notSet: symbol = "info.circle"
        case .notKnown: symbol = "questionmark.circle"
        }
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(decider.hasUnexpectedRuntime ? Color.orange : Color.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("Decider")
                    .font(.system(size: 11, weight: .semibold))
                Text(decider.sentence)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .help(OperatorDecider.help)
    }

    /// Whether the Operator can post to Zulip,  as the Mac's eavesdropper reports it.  An
    /// eavesdropper that does not report it reads Not reported,  and a status that is not
    /// current reads Not known,  never as able to post.
    private var operatorVoiceLine: some View {
        let voice = store.model.operatorVoice
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: voice.systemImage)
                .foregroundStyle(voice.isProblem ? Color.orange : Color.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text("Posting To Zulip")
                        .font(.system(size: 11, weight: .semibold))
                    Text(voice.label)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(voice.isProblem ? Color.orange : Color.secondary)
                }
                Text(voice.sentence())
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .help(voice.tooltip())
    }

    // MARK: One seat

    /// One seat.  A seat another eavesdropper holds gets no fields and no buttons:  a section for it
    /// would stop the Mac's eavesdropper, and its pause could not reach it.
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
                    pendingWakeAction = PendingWakeAction(kind: .removeSection, seat: seat.seat)
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

    /// Whether the seat Wakes, Captures Only or still has an Old Wake Setting, by its own section
    /// of the file, with what that means in the help text.  A kind Muster cannot read is said to
    /// be not known, with why.
    @ViewBuilder
    private func kindLabel(_ seat: WakeSeat) -> some View {
        switch seat.kind {
        case .wakes:
            Label(seat.kind.title, systemImage: "bell")
                .font(.system(size: 11, design: .monospaced))
                .help(seat.kind.help)
        case .capturesOnly:
            Label(seat.kind.title, systemImage: "tray")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .help(seat.kind.help)
        case .oldWakeSetting:
            Label(seat.kind.title, systemImage: "clock.arrow.circlepath")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.orange)
                .help(seat.kind.help)
        case .notKnown(let why):
            Label(seat.kind.title, systemImage: "questionmark.circle")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.orange)
                .help(why)
        }
    }

    /// What the seat's section says about its kind, beyond the label:  a routine or a line Muster
    /// cannot read, what today's listener does with an old wake setting, why a seat that
    /// Captures Only is not offered a routine, and a `wake` line left over from the old wake types,
    /// which Muster shows and never changes.
    @ViewBuilder
    private func kindNotes(_ seat: WakeSeat) -> some View {
        if case .unreadable(let why) = seat.routineForm {
            problemBox(why)
        } else if case .notKnown(let why) = seat.kind, seat.hasSection {
            // A line Muster cannot read, or a file it did not read:  the kind is not known, and
            // the note says why.  A seat with no section says so in its notices already.
            Text(why)
                .font(.system(size: 10))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        if case .oldWakeSetting(let old) = seat.kind {
            Label(old.note, systemImage: "clock.arrow.circlepath")
                .font(.system(size: 10))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        } else if let wake = seat.leftoverWake {
            Text("The file also has wake = \"\(wake)\", left over from the old wake types.\u{00A0} Muster does not change it.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        if seat.kind == .capturesOnly, seat.host.isTheMac {
            Text(WakeSeatKind.routineWaitsOnTheListener)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
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

    /// The pause that covers a seat, in words:  what it stops, who set it and when, and where the
    /// panel read it.  A pause that is not known is said to be not known, never left blank.
    @ViewBuilder
    private func pauseLabel(_ seat: WakeSeat) -> some View {
        if let text = seat.pauseText() {
            let notKnown = seat.pauseSource.notKnownReason != nil
            Label(text, systemImage: notKnown ? "questionmark.circle" : "pause.circle.fill")
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .foregroundStyle(seat.paused ? Color.red : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
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
                kindLabel(seat)
                connectionLabel(seat)
                pauseLabel(seat)
                Spacer()
                pauseButtons(seat)
            }
            summaryLine(seat)
            seatNotices(seat)
            kindNotes(seat)
            routineLine(seat)
            if !seat.redWarnings.isEmpty {
                // An old status's warnings are its last word, and say so.
                let suffix = seat.liveIsCurrent ? "" : " (\(seat.notCurrentSuffix().dropFirst(2)))"
                ForEach(seat.redWarnings, id: \.self) { warning in
                    problemBox(warning + suffix)
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
            recentWakes(seat)
            if seat.host.isTheMac {
                actionButtons(seat, changed: changed)
            }
        }
        .padding(.vertical, 6)
    }

    /// The pause and resume, only where they do something.  A seat under a full pause is not
    /// offered a wakes-only pause, which would loosen it.  A seat whose pause is not known is
    /// offered neither as if it were unpaused:  both are shown off, with why.
    @ViewBuilder
    private func pauseButtons(_ seat: WakeSeat) -> some View {
        if let why = seat.pauseSource.notKnownReason {
            let help = "Whether this seat is paused is not known, so neither a pause nor a resume is offered.\u{00A0} \(why)"
            Button {} label: {
                Label("Pause Wakes", systemImage: "pause.circle")
            }
            .disabled(true)
            .help(help)
            Button {} label: {
                Label("Resume…", systemImage: "play.circle")
            }
            .disabled(true)
            .help(help)
        } else if seat.paused || seat.wakesPaused {
            Button {
                pendingWakeAction = PendingWakeAction(kind: .resume, seat: seat.seat)
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
            // The same limit the grid and the usage line read, so No Limit is never shown as a cap.
            let cap = seat.capLimit(.wakesPerHour).usageSuffix
            parts.append("\(floor)\(seat.usage.wakesThisHour)\(cap) wakes in the last hour")
        }
        // Worded in the model, so an old status's counts say they are not current.
        parts.append(contentsOf: seat.heldBackSummary)
        let problems = seat.problems.count
        if problems > 0 {
            parts.append("\(problems) setting\(problems == 1 ? "" : "s") need\(problems == 1 ? "s" : "") a look")
        }
        let differences = seat.contractDifferences.count
        if differences > 0 {
            parts.append("\(differences) routine setting\(differences == 1 ? "" : "s") differ\(differences == 1 ? "s" : "") from the router contract")
        }
        if seat.routineNotReady {
            parts.append("the routine is not ready")
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

    /// The seat's routine, as the eavesdropper last found it:  whether it can be reached.  The
    /// eavesdropper's status carries no record yet of which chat a wake reached and why, so no
    /// receipt is shown.
    @ViewBuilder
    private func routineLine(_ seat: WakeSeat) -> some View {
        if let fact = seat.routineReadiness {
            HStack(alignment: .top, spacing: 6) {
                Text("Routine")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                factLine(fact.text, problem: fact.emphasised)
            }
        }
        if let provider = seat.provider, !provider.isEmpty {
            HStack(alignment: .top, spacing: 6) {
                Text("Provider")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(provider)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    /// The settings that mean something for the seat, plus any the file holds anyway.  A seat that
    /// Wakes shows its routine and its caps, one with an Old Wake Setting its caps, and one that
    /// Captures Only only what its file holds.
    private func settingsGrid(_ seat: WakeSeat, changed: Set<WakeSettingKey>) -> some View {
        let shown = seat.settings.filter {
            $0.key.applies(to: seat.kind) || $0.configured != nil
        }
        let hidden = seat.settings.count - shown.count
        let hiddenText = hidden == 1
            ? "1 setting that does not apply to a seat that is \(seat.kind.title) is not shown."
            : "\(hidden) settings that do not apply to a seat that is \(seat.kind.title) are not shown."
        let moved = store.movedUnderneath[seat.seat] ?? []
        return VStack(alignment: .leading, spacing: 4) {
            WakeGridHeader(labels: ["Setting", "The File", "The Eavesdropper"])
            ForEach(shown) { setting in
                WakeSettingRow(
                    setting: setting,
                    editable: seat.host.isTheMac && setting.key.refusalReason == nil
                        && (!setting.key.isRoutine || seat.kind.hasRoutine),
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

    /// Apply and Revert, then Test Wake.
    ///
    /// Test Wake is shown for every seat the Mac holds and is off for now:  its new meaning, a
    /// dry run through the eavesdropper that shows the chat a wake would reach and why, waits on
    /// the listener.  Nothing here runs `daemon test-wake`.  The result area below the buttons is
    /// where that dry run's answer will go.
    private func actionButtons(_ seat: WakeSeat, changed: Set<WakeSettingKey>) -> some View {
        let busy = store.isBusy
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Button {
                    pendingWakeAction = PendingWakeAction(
                        kind: .apply,
                        seat: seat.seat,
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
                testWakeButton
            }
            testWakeResult
        }
    }

    /// Test Wake, off until the listener's dry run exists.
    private var testWakeButton: some View {
        Button {} label: {
            Label("Test Wake…", systemImage: "play.and.pause")
        }
        .disabled(true)
        .help(WakeSeatKind.testWakeWaits)
    }

    /// Where Test Wake's answer goes:  for a seat that Wakes, the chat a wake would reach and why,
    /// with its receipt, and for one that Captures Only, whether the eavesdropper captured it.
    /// Until the dry run exists it says only that it waits.
    private var testWakeResult: some View {
        Text(WakeSeatKind.testWakeWaits)
            .font(.system(size: 10))
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
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

    /// The counters beside the limits the seat is held to.  Worded in the model
    /// (`WakeSeat.usageFacts`), from the same limit the grid shows, so the line never names a cap
    /// the eavesdropper is not using.
    private func usageFacts(_ seat: WakeSeat) -> [(text: String, emphasised: Bool)] {
        seat.usageFacts.map { (text: $0.text, emphasised: $0.emphasised) }
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
        // The held-back wakes, chat nudges and quota are worded in the model, so an old
        // status's values say so there and never read as now.
        for fact in seat.liveFacts() {
            facts.append((fact.text, fact.emphasised))
        }
        return factGrid(title: "Deferred And Held", facts: facts)
    }

    /// One fact, with a symbol as well as a colour, so colour is never the only sign:  a warning
    /// for a problem, and a check mark otherwise.
    private func factLine(_ text: String, problem: Bool) -> some View {
        HStack(alignment: .top, spacing: 5) {
            Image(systemName: problem ? "exclamationmark.triangle.fill" : "checkmark.circle")
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
}

// MARK: - The grid header

/// The header over the setting rows, in the rows' own columns:  three that share the width and
/// the state badge's fixed column, so each label sits over its values.
private struct WakeGridHeader: View {
    let labels: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
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
            WakeStateLegend()
        }
    }
}

// MARK: - The legend

/// What each state means, in words that stay on screen (A11Y-37):  a state's meaning used to
/// live only in the badge's tooltip.  Folded until opened, so it costs one line.
private struct WakeStateLegend: View {
    var body: some View {
        DisclosureGroup("What The States Mean") {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(WakeSettingState.legendOrder, id: \.self) { state in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Label(state.label, systemImage: state.symbol)
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(state.tint)
                            .frame(width: 130, alignment: .leading)
                        Text(state.note)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.top, 4)
        }
        .font(.caption2)
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

    /// What an empty field says.  A wake cap the file holds none of is No Limit, and clearing the
    /// field is the plain way to choose it.  A file that was not read is never No Limit, so its
    /// empty field says what the file's column would.
    private var fieldPrompt: String {
        guard setting.key.isWakeCap else { return "not set, default" }
        return setting.fileLimit == .notKnown ? setting.fileWords : WakeSettingKey.noLimitText
    }

    /// Whether the field holds 0, which for a wake cap is Paused.
    private var isPausedDraft: Bool {
        Double(draft.trimmingCharacters(in: .whitespaces)) == 0
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
                if editable {
                    TextField(setting.title, text: fieldText, prompt: Text(fieldPrompt))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help(setting.key.isWakeCap ? WakeSettingKey.wakeCapHelp : setting.title)
                } else {
                    GridCellText(value: setting.fileWords)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                GridCellText(value: setting.liveWords)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(setting.live == nil && !setting.liveNoLimit ? "The eavesdropper did not report a value." : setting.liveWords)
                WakeStateBadge(state: setting.state, width: 108)
            }
            if setting.key.isWakeCap, editable, isPausedDraft {
                // 0 is Paused, which is allowed and easy to type by mistake, so it is said plainly.
                Label("Paused:  no wake of this kind is allowed while this is 0.", systemImage: "pause.circle")
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !setting.note.isEmpty {
                Text(setting.note)
                    .font(.system(size: 10))
                    .foregroundStyle(setting.state.isProblem ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if setting.state.isProblem {
                // A state that needs explaining says so in visible words, not only on hover
                // (A11Y-37).
                Text(setting.state.note)
                    .font(.system(size: 10))
                    .foregroundStyle(Color.orange)
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
            // The router contract fixes this setting, and the file holds something else:  it is
            // said plainly, and no control is offered for it.
            if let difference = setting.contractDifference {
                Label(difference, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if setting.key.needsOwnerCheck, changed {
                Label(
                    "This names where the seat's wakes go, or the key that signs them, so Apply asks for Touch ID.\u{00A0} It is the name of a variable, never its value.",
                    systemImage: "lock.shield"
                )
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
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
            // A symbol for each state, so the colour only repeats what the shape and word say.
            Image(systemName: state.symbol)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(state.tint)
                .accessibilityHidden(true)
            Text(state.label)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
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
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(state.label.capitalized):  \(state.note)")
    }
}

private extension WakeSettingState {
    /// The colour the badge uses.
    ///
    /// The grid's own vocabulary:  green for a pass,  orange for drift,  grey for one the
    /// engine does not own,  and red for a read that failed.
    var tint: Color {
        switch self {
        case .match: return .green
        case .drift, .stale: return .orange
        case .notOwned, .unpublished: return .gray
        case .unset, .off, .heldElsewhere, .seatOff: return .secondary
        case .unknown: return .red
        }
    }

    /// The badge's symbol, one per meaning, so no state is told apart by colour alone.
    var symbol: String {
        switch self {
        case .match: return "checkmark.circle"
        case .drift: return "arrow.left.arrow.right.circle"
        case .stale: return "clock.badge.exclamationmark"
        case .notOwned: return "minus.circle"
        case .unpublished: return "eye.slash"
        case .unset: return "circle.dashed"
        case .off: return "power"
        case .unknown: return "questionmark.circle"
        case .heldElsewhere: return "arrow.up.right.circle"
        case .seatOff: return "pause.circle"
        }
    }

    /// The legend's order:  the states that need a look first, then the quiet ones.
    static let legendOrder: [WakeSettingState] = [
        .drift, .unknown, .stale, .match, .unset, .off, .seatOff, .heldElsewhere,
        .notOwned, .unpublished,
    ]

    /// The badge's tooltip, the legend's words, and the line under a row whose state needs it.
    var note: String {
        switch self {
        case .match: return "The eavesdropper is using the value the file holds.\u{00A0} This compares two values, and it does not prove the setting works."
        case .drift: return "The eavesdropper is using something other than the file's value."
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
    enum Kind { case apply, removeSection, resume }
    let kind: Kind
    let seat: String
    /// The changes this would make, in words.
    var detail: String = ""
    var id: String { "\(kind)-\(seat)" }

    var title: String {
        switch kind {
        case .apply: return "Apply Settings For \(seat)?"
        case .removeSection: return "Remove The \(seat) Section From The Mac's File?"
        case .resume: return "Resume Wakes For \(seat)?"
        }
    }
    var confirmTitle: String {
        switch kind {
        case .apply: return "Apply"
        case .removeSection: return "Remove Section"
        case .resume: return "Resume"
        }
    }
    /// Why Touch ID is asked, as the system dialog shows it.
    var ownerReason: String {
        switch kind {
        case .apply: return "change where \(seat)'s wakes go or the key that signs them"
        case .removeSection, .resume: return "change \(seat)'s wake"
        }
    }
    var message: String {
        switch kind {
        case .apply:
            let opening = "This checks \(seat)'s edited settings with the eavesdropper's own rules, writes them to the Mac's eavesdropper configuration file, and asks the eavesdropper to re-read it.\u{00A0} Muster then says whether the eavesdropper took the change."
            return detail.isEmpty ? opening : opening + "\n\n" + detail
        case .removeSection:
            return "This takes the [seat.\(seat)] section out of the Mac's eavesdropper configuration file and keeps a backup of the old file.\u{00A0} The Mac's eavesdropper refuses this section, so it can only stop the eavesdropper."
        case .resume:
            return "This lifts \(seat)'s own pause on the Mac's eavesdropper, so its wakes can run again.\u{00A0} A pause on every seat, if one is in force, stays."
        }
    }
}
