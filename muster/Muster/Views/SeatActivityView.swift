import SwiftUI

/// The Seat Activity panel (AFC-395):  which seats are live right now,  and where.
///
/// The panel is read only.  It never posts,  never replies  and never acts as Jay:  the
/// only thing it runs is the fleet chat CLI's read-only verbs,  and the seat it reads as
/// is a bot seat.
///
/// Rows sit under the machine whose eavesdropper holds the seat,  from the seat partition.
/// Muster reads only the Mac's eavesdropper,  so a seat the server holds says "Not read" in
/// its eavesdropper columns,  in plain text:  that is where the seat lives,  not a fault.
/// A source that could not be read shows Unknown in that column with the reason under the
/// row.  A row never shows an empty list or a zero where a source failed.
struct SeatActivityView: View {
    @EnvironmentObject private var store: FleetStore
    @Environment(\.scenePhase) private var scenePhase
    @ObservedObject private var activity = SeatActivityStore.shared
    /// The seats whose session list is open.
    @State private var expanded: Set<String> = []

    /// How often the panel reads again on its own,  while the app is in front.
    ///
    /// The clock is slow on purpose:  a refresh asks the fleet chat CLI one question per
    /// seat,  and the panel is a glance  not a monitor.  Five minutes is the fleet's own
    /// cadence for this kind of read (the doctor runs every fifteen, the fleet view every
    /// five),  and the panel reads again as soon as the app comes back to the front.
    static let refreshSeconds = 300

    /// The youngest reading the panel replaces when the app comes back to the front,  or
    /// when the owner comes back to the panel.
    ///
    /// A refresh costs one Zulip lookup per seat,  so switching apps or panels back and
    /// forth does not start a new one each time.  The Refresh button is not held to this.
    static let foregroundMinimumSeconds: TimeInterval = 60

    private let seatWidth: CGFloat = 120
    private let sessionWidth: CGFloat = 110
    private let listenerWidth: CGFloat = 150
    private let quotaWidth: CGFloat = 120
    private let postMinimumWidth: CGFloat = 150

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                if activity.loading && activity.model == nil {
                    ProgressView("Reading the fleet…")
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if let model = activity.model {
                    summary(model)
                    if !model.problems.isEmpty {
                        problems(model.problems)
                    }
                    if model.rosterProblem == nil {
                        sources(model)
                        ForEach(model.groups) { group in
                            groupView(group, model: model)
                        }
                        if model.groups.allSatisfy({ $0.kind != .server }), let note = model.otherServerSeatsNote {
                            groupHeading(.server)
                            Text(SeatActivityText.display(note))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        details(model)
                    }
                } else {
                    Text(ApplyCopy.join("No reading yet.", "Refresh to read the fleet."))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .onAppear {
            activity.setActive(scenePhase == .active)
        }
        .onDisappear {
            // Leaving the panel stops a read still running:  nobody is looking at it.
            activity.stopReading()
        }
        .task {
            await activity.refreshIfOlder(than: SeatActivityView.foregroundMinimumSeconds, roster: store.roster)
            // The slow timer:  it reads only while the app is in front,  and a read that is
            // already running is never started twice.  It stops when the panel goes away.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(SeatActivityView.refreshSeconds) * 1_000_000_000)
                if Task.isCancelled { break }
                await activity.timerTick(roster: store.roster, minimumAge: SeatActivityView.foregroundMinimumSeconds)
            }
        }
        .onChange(of: scenePhase) { _, phase in
            // A read costs one question per seat,  so neither the timer nor this reads while
            // the app is in the background:  the reading on screen is still the last one.
            activity.setActive(phase == .active)
            guard phase == .active else { return }
            Task {
                await activity.refreshIfOlder(than: SeatActivityView.foregroundMinimumSeconds, roster: store.roster)
            }
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(MusterDestination.seats.title)
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await activity.refresh(roster: store.roster) }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(activity.loading)
            }
            Text("Which seats are live, what the eavesdropper says about them, and when each one last posted.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// The answer first:  how many seats are connected and working,  and where the rest are.
    private func summary(_ model: SeatActivityModel) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(SeatActivityText.display(model.headline))
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)
                .help("A seat is working when it has a live session on the Mac, or the Mac's eavesdropper is running a wake for it.")
            HStack(spacing: 8) {
                if let readAt = model.readAt {
                    Text("Read \(FleetTime.clock(readAt))")
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                if activity.loading {
                    ProgressView()
                        .controlSize(.small)
                    Text("Reading…")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
        }
    }

    /// Where each column's answer came from,  and how old it is.
    ///
    /// The source is on the screen,  not in a tooltip:  a number without a source is a
    /// number nobody can check.
    private func sources(_ model: SeatActivityModel) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Sources")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(SeatActivityText.display(sourcesText(model)))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func sourcesText(_ model: SeatActivityModel) -> String {
        let path = model.listenerPath.isEmpty ? "its state file" : model.listenerPath
        var text = "Sessions, eavesdropper state and quota come from the Mac's eavesdropper,  \(path)"
        if let written = model.listenerWrittenAt {
            text += ",  written \(FleetTime.clock(written))."
        } else {
            text += ",  which could not be used this time."
        }
        text += "  Sessions shown are on the Mac only:  a session on the server attaches to no eavesdropper Muster can read."
        if !model.partitionPath.isEmpty {
            text += "  Where each seat is held comes from the seat partition,  \(model.partitionPath)."
        }
        text += "  Last posts come from Zulip,  and the panel only reads them."
        return text
    }

    /// What could not be read,  across every seat.
    private func problems(_ problems: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(problems, id: \.self) { problem in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .accessibilityHidden(true)
                    Text(SeatActivityText.display(problem))
                        .font(.subheadline.monospaced())
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    /// The calls a read made and the seat it read as:  for checking the panel,  not the fleet.
    private func details(_ model: SeatActivityModel) -> some View {
        DisclosureGroup("Details") {
            VStack(alignment: .leading, spacing: 2) {
                Text("This read made \(model.requestsMade) of at most \(model.requestCeiling) Zulip calls.")
                if !model.readingSeat.isEmpty {
                    Text(ApplyCopy.join(
                        "It read Zulip as the \(model.readingSeat) bot.",
                        "The panel never reads as Jay."
                    ))
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.caption)
    }

    // MARK: The groups

    private func groupHeading(_ kind: SeatPlaceGroup) -> some View {
        Label(kind.title, systemImage: kind.systemImage)
            .font(.headline)
            .padding(.top, 6)
    }

    private func groupView(_ group: SeatActivityGroup, model: SeatActivityModel) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            groupHeading(group.kind)
            Text(SeatActivityText.display(group.kind.explanation))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if group.kind == .server, let note = model.otherServerSeatsNote {
                Text(SeatActivityText.display(note))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            tableHeader
            ForEach(group.rows) { row in
                rowView(row, readAt: model.readAt)
            }
        }
    }

    // MARK: The table

    private var tableHeader: some View {
        HStack(alignment: .top, spacing: 8) {
            Text("Seat")
                .frame(width: seatWidth, alignment: .leading)
            Text("Sessions On The Mac")
                .frame(width: sessionWidth, alignment: .leading)
            Text("Eavesdropper")
                .frame(width: listenerWidth, alignment: .leading)
            Text("Quota")
                .frame(width: quotaWidth, alignment: .leading)
            Text("Last Zulip Post")
                .frame(minWidth: postMinimumWidth, maxWidth: .infinity, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.bottom, 2)
    }

    /// One seat's row.
    private func rowView(_ row: SeatActivityRow, readAt: Date?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(row.seat)
                        .font(.callout.weight(.semibold).monospaced())
                    if let bot = row.botName, !bot.isEmpty {
                        Text(bot)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(width: seatWidth, alignment: .leading)

                sessionCell(row)
                listenerCell(row)
                quotaCell(row, readAt: readAt)
                postCell(row)
            }
            if expanded.contains(row.seat), !row.sessions.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(row.sessions) { session in
                        Text(SeatActivityText.display(session.line))
                            .font(.caption.monospaced())
                            .foregroundStyle(session.alive == true ? Color.primary : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.leading, seatWidth + 8)
            }
            if !row.notes.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(row.notes) { note in
                        Text("\(note.column): \(SeatActivityText.display(note.text))")
                            .font(.caption.monospaced())
                            .foregroundStyle(note.isProblem ? Color.orange : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.leading, seatWidth + 8)
            }
        }
        .padding(.vertical, 4)
    }

    /// The live session count on the Mac,  which opens the session list.
    private func sessionCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.openSessions {
            case .known(let count):
                if row.sessions.isEmpty {
                    cellText(count == 0 ? "None" : sessionCount(count), emphasised: count > 0)
                } else {
                    Button {
                        if expanded.contains(row.seat) {
                            expanded.remove(row.seat)
                        } else {
                            expanded.insert(row.seat)
                        }
                    } label: {
                        HStack(spacing: 3) {
                            Image(systemName: expanded.contains(row.seat) ? "chevron.down" : "chevron.right")
                                .font(.caption2)
                                .accessibilityHidden(true)
                            cellText(count == 0 ? "None live" : sessionCount(count), emphasised: count > 0)
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(
                        expanded.contains(row.seat)
                            ? "Hide the session list for \(row.seat)"
                            : "Show the session list for \(row.seat)"
                    )
                }
            case .unknown:
                unknownText()
            case .notHere:
                notReadText()
            }
        }
        .frame(width: sessionWidth, alignment: .leading)
        .help(cellHelp(row.openSessions, known: "The live sessions the Mac's eavesdropper holds for this seat."))
    }

    private func sessionCount(_ count: Int) -> String {
        count == 1 ? "1 session" : "\(count) sessions"
    }

    /// The eavesdropper's state,  the wake it runs and when it last heard anything.
    private func listenerCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.listener {
            case .known(let state):
                VStack(alignment: .leading, spacing: 1) {
                    Text(state.label)
                        .font(.subheadline.monospaced())
                        .foregroundStyle(state.isHealthy ? Color.secondary : Color.orange)
                    if row.runningWake {
                        Text("Running a wake")
                            .font(.caption)
                    }
                    if let event = row.lastEvent {
                        Text("Last event \(FleetTime.clock(event))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            case .unknown:
                unknownText()
            case .notHere:
                notReadText()
            }
        }
        .frame(width: listenerWidth, alignment: .leading)
        .help(cellHelp(row.listener, known: "What the Mac's eavesdropper says about this seat."))
    }

    /// The provider quota the eavesdropper reads for the seat.
    private func quotaCell(_ row: SeatActivityRow, readAt: Date?) -> some View {
        Group {
            switch row.quota {
            case .known(let reading):
                VStack(alignment: .leading, spacing: 1) {
                    Text(reading.label)
                        .font(
                            reading.isExhausted
                                ? Font.subheadline.weight(.semibold).monospaced()
                                : Font.subheadline.monospaced()
                        )
                        .foregroundStyle(reading.isExhausted ? Color.orange : Color.secondary)
                    if let reset = reading.resetAt {
                        Text(SeatActivityText.resetText(reset, now: readAt ?? Date()))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            case .unknown:
                unknownText()
            case .notHere:
                notReadText()
            }
        }
        .frame(width: quotaWidth, alignment: .leading)
        .help(cellHelp(row.quota, known: ApplyCopy.join(
            "The provider quota the Mac's eavesdropper reads for this seat, the same reading its wake gate uses.",
            "Not reported means the seat's wake talks to no provider, or the quota snapshot cannot speak for it now."
        )))
    }

    /// The seat's last Zulip post:  when it was,  and where it went.
    ///
    /// The body is never shown.  The topic is a label,  and it is styled as one.
    private func postCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.lastPost {
            case .known(.none):
                VStack(alignment: .leading, spacing: 1) {
                    cellText("No post", emphasised: false)
                    Text("The search found nothing.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            case .known(.post(let at, _, let label, let saysAtQuota)):
                VStack(alignment: .leading, spacing: 1) {
                    if let at {
                        cellText(FleetTime.clock(at), emphasised: true)
                    } else {
                        cellText("No time", emphasised: false)
                    }
                    Text(label.isEmpty ? "—" : label)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if saysAtQuota {
                        Text("This post says the seat is at quota.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            case .unknown:
                unknownText()
            case .notHere:
                notReadText()
            }
        }
        .frame(minWidth: postMinimumWidth, maxWidth: .infinity, alignment: .leading)
        .help(postHelp(row))
    }

    // MARK: The pieces

    private func cellText(_ text: String, emphasised: Bool) -> some View {
        Text(text)
            .font(.subheadline.monospaced())
            .foregroundStyle(emphasised ? Color.primary : Color.secondary)
    }

    /// The unknown marker:  a read failed,  and the reason is under the row.
    private func unknownText() -> some View {
        Text("Unknown")
            .font(.subheadline.weight(.semibold).monospaced())
            .foregroundStyle(.orange)
    }

    /// The marker for a column the Mac does not hold:  plain,  because it is not a fault.
    private func notReadText() -> some View {
        Text("Not read")
            .font(.subheadline.monospaced())
            .foregroundStyle(.secondary)
    }

    /// A cell's tooltip:  what the column is,  or why it holds no answer.
    private func cellHelp<Value: Equatable & Sendable>(_ fact: SeatFact<Value>, known: String) -> String {
        switch fact {
        case .known: return known
        case .unknown(let reason): return SeatActivityText.display(reason)
        case .notHere(let reason): return SeatActivityText.display(reason)
        }
    }

    /// What the last post was,  in words.
    private func postHelp(_ row: SeatActivityRow) -> String {
        switch row.lastPost {
        case .known(.none):
            return "The search found no post for this seat."
        case .known(.post(let at, let channel, let label, let quota)):
            var text = "Last post in \(channel.isEmpty ? "Zulip" : channel)"
            if !label.isEmpty { text += ": \(label)" }
            if let at { text += ", at \(FleetTime.clock(at))" }
            text += "."
            if quota {
                text = ApplyCopy.join(text, "Its own words say the seat is at quota, which is what the seat said, not a quota reading.")
            }
            return text
        case .unknown(let reason):
            return "The last post could not be read: \(SeatActivityText.display(reason))"
        case .notHere(let reason):
            return SeatActivityText.display(reason)
        }
    }
}

#Preview {
    SeatActivityView()
        .environmentObject(FleetStore())
}
