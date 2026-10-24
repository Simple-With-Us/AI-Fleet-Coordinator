import SwiftUI

/// The Seat Activity panel (AFC-395):  which seats are live right now.
///
/// The panel is read only.  It never posts,  never replies  and never acts as Jay:  the
/// only thing it runs is the fleet chat CLI's read-only verbs,  and the seat it reads as
/// is a bot seat.
///
/// Every column names its own source,  and a source that could not be read shows
/// UNKNOWN in that column  with the reason under the row.  A row never shows an empty
/// list or a zero  where a source failed:  an unknown column is unknown.
struct SeatActivityView: View {
    @EnvironmentObject private var store: FleetStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var model: SeatActivityModel?
    @State private var loading = false

    /// How often the panel reads again on its own.
    ///
    /// The clock is slow on purpose:  a refresh asks the fleet chat CLI one question per
    /// seat,  and the panel is a glance  not a monitor.  Five minutes is the fleet's own
    /// cadence for this kind of read (the doctor runs every fifteen, the fleet view every
    /// five),  and the panel reads again as soon as the app comes back to the front.
    static let refreshSeconds = 300

    private let seatWidth: CGFloat = 128
    private let sessionWidth: CGFloat = 84
    private let listenerWidth: CGFloat = 150
    private let presenceWidth: CGFloat = 92
    private let postWidth: CGFloat = 250
    private let quotaWidth: CGFloat = 84

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                if loading && model == nil {
                    ProgressView("Reading the fleet…")
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if let model {
                    summary(model)
                    sources(model)
                    if !model.problems.isEmpty {
                        problems(model.problems)
                    }
                    tableHeader
                    ForEach(model.rows) { row in
                        rowView(row)
                    }
                } else {
                    Text("No reading yet.  Refresh to read the fleet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task {
            await load()
            // The slow timer:  the panel reads again on its own,  and a read that is
            // already running is not started twice.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(SeatActivityView.refreshSeconds) * 1_000_000_000)
                if Task.isCancelled { break }
                await load()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            // A read costs one question per seat,  so the panel does not read while the
            // app is in the background:  the reading on screen is still the last one.
            guard phase == .active else { return }
            Task { await load() }
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Seat Activity")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("Which seats are live,  what the listener says about them,  and when each one last posted.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// When the panel read,  and how many calls it made.
    private func summary(_ model: SeatActivityModel) -> some View {
        HStack(spacing: 12) {
            if let readAt = model.readAt {
                Text("Read \(FleetTime.clock(readAt))")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Text("\(model.requestsMade) of \(model.requestCeiling) calls")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .help("The fleet chat calls this refresh made,  and the ceiling it was held to.")
            let working = model.seatsWithSessions.count
            Text(working == 1 ? "1 seat with an open session" : "\(working) seats with an open session")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(working > 0 ? Color.primary : Color.secondary)
                .help("The seats the listener holds a live session for.")
            if !model.readingSeat.isEmpty {
                Text("Read as \(model.readingSeat)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .help("The bot seat the fleet chat CLI read as.  The panel never reads as Jay.")
            }
            Spacer(minLength: 0)
        }
    }

    /// Where each column's number came from.
    ///
    /// The source is on the screen,  not in a tooltip:  a number without a source is a
    /// number nobody can check.
    private func sources(_ model: SeatActivityModel) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Sources")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text("Open sessions and the listener state come from the listener's own state file, "
                + "\(model.listenerPath.isEmpty ? "the listener's state file" : model.listenerPath).  "
                + "Presence and the last post come from the fleet chat CLI's read-only verbs.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// What could not be read,  across every seat.
    private func problems(_ problems: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(problems, id: \.self) { problem in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .accessibilityHidden(true)
                    Text(problem)
                        .font(.system(size: 11, design: .monospaced))
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

    // MARK: The table

    private var tableHeader: some View {
        HStack(alignment: .top, spacing: 8) {
            Text("Seat")
                .frame(width: seatWidth, alignment: .leading)
            Text("Open Sessions")
                .frame(width: sessionWidth, alignment: .leading)
            Text("Listener")
                .frame(width: listenerWidth, alignment: .leading)
            Text("Presence")
                .frame(width: presenceWidth, alignment: .leading)
            Text("Last Zulip Post")
                .frame(width: postWidth, alignment: .leading)
            Text("At Quota")
                .frame(width: quotaWidth, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.bottom, 2)
    }

    /// One seat's row.
    private func rowView(_ row: SeatActivityRow) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(row.seat)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    if let bot = row.botName, !bot.isEmpty {
                        Text(bot)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(width: seatWidth, alignment: .leading)

                sessionCell(row)
                listenerCell(row)
                presenceCell(row)
                postCell(row)
                quotaCell(row)
            }
            if !row.unknowns.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(row.unknowns, id: \.column) { unknown in
                        Text("\(unknown.column):  \(unknown.reason)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.leading, seatWidth + 8)
            }
        }
        .padding(.vertical, 4)
    }

    /// The open session count.
    private func sessionCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.openSessions {
            case .known(let count):
                Text(count == 1 ? "1 session" : "\(count) sessions")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(count > 0 ? Color.primary : Color.secondary)
            case .unknown:
                unknownText("UNKNOWN")
            }
        }
        .frame(width: sessionWidth, alignment: .leading)
        .help("The leases the listener holds alive for this seat.")
    }

    /// The listener's state.
    private func listenerCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.listener {
            case .known(let state):
                Text(state.label)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(state.isHealthy ? Color.secondary : Color.orange)
            case .unknown:
                unknownText("UNKNOWN")
            }
        }
        .frame(width: listenerWidth, alignment: .leading)
        .help("What the listener's own published state says about this seat.")
    }

    /// The seat's Zulip presence.
    private func presenceCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.presence {
            case .known(let presence):
                Text(presence.label)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(presence == .active ? Color.green : Color.secondary)
            case .unknown:
                unknownText("UNKNOWN")
            }
        }
        .frame(width: presenceWidth, alignment: .leading)
        .help("What Zulip reports about this seat's bot.")
    }

    /// The seat's last Zulip post:  when it was,  and where it went.
    ///
    /// The body is never shown.  The topic is a label,  and it is styled as one.
    private func postCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.lastPost {
            case .known(.none):
                VStack(alignment: .leading, spacing: 1) {
                    Text("No post")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Text("The search found nothing.")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
            case .known(.post(let at, let channel, let label, _)):
                VStack(alignment: .leading, spacing: 1) {
                    if let at {
                        Text(FleetTime.clock(at))
                            .font(.system(size: 11, design: .monospaced))
                    } else {
                        Text("No time")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Text(label.isEmpty ? "—" : label)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            case .unknown:
                unknownText("UNKNOWN")
            }
        }
        .frame(width: postWidth, alignment: .leading)
        .help(postHelp(row))
    }

    /// The at-quota flag.
    private func quotaCell(_ row: SeatActivityRow) -> some View {
        Group {
            switch row.atQuota {
            case .known(true):
                Text("At quota")
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.orange)
            case .known(false):
                Text("—")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            case .unknown:
                unknownText("UNKNOWN")
            }
        }
        .frame(width: quotaWidth, alignment: .leading)
        .help("Read out of the seat's last Zulip post.  A post that could not be read leaves this unknown.")
    }

    /// The unknown marker.
    private func unknownText(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold, design: .monospaced))
            .foregroundStyle(.orange)
    }

    /// What the last post was,  in words.
    private func postHelp(_ row: SeatActivityRow) -> String {
        switch row.lastPost {
        case .known(.none):
            return "The search found no post for this seat."
        case .known(.post(let at, let channel, let label, let quota)):
            var text = "Last post in \(channel.isEmpty ? "Zulip" : channel)"
            if !label.isEmpty { text += ":  \(label)" }
            if let at { text += ",  at \(FleetTime.clock(at))" }
            if quota { text += ".  It reports the seat at quota." }
            return text
        case .unknown(let reason):
            return "The last post could not be read:  \(reason)"
        }
    }

    // MARK: The read

    /// Read the fleet,  off the main thread.
    private func load() async {
        // A read that is already running is not started twice:  the timer and a pressed
        // Refresh button can land on the same moment.
        guard !loading else { return }
        loading = true
        defer { loading = false }
        let roster = store.roster
        let reader = SeatActivityReader()
        let result = await Task.detached(priority: .userInitiated) {
            await reader.read(roster: roster)
        }.value
        model = result
    }
}

#Preview {
    SeatActivityView()
        .environmentObject(FleetStore())
}
