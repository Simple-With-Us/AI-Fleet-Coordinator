import SwiftUI

/// The Eavesdropper's kill switch (AFC-832):  the pause in force, fleet-wide and per seat, with a
/// button for each change.  Every button only plans:  the shared action sheet shows the exact
/// command and the pause in force, and the run needs Touch ID.  Nothing here pauses on its own.
struct OperatorPauseSection: View {
    /// The server Eavesdropper's status, only when it is current;  nil offers no button.
    let status: ReducedEavesdropperStatus?
    @ObservedObject private var controller = OpsController.shared

    var body: some View {
        SectionPanel(
            title: "Pause And Resume",
            subtitle: "The Eavesdropper's kill switch.\u{00A0} Capture goes on while paused, so nothing is lost."
        ) {
            if let status {
                VStack(alignment: .leading, spacing: 6) {
                    fleetRow(status)
                    Divider()
                    if status.seats.isEmpty {
                        EmptyNote(text: "The Eavesdropper's status lists no seats.")
                    }
                    ForEach(status.seats.map(\.seat).sorted(), id: \.self) { seat in
                        seatRow(seat, status: status)
                    }
                }
            } else {
                EmptyNote(text: "The Eavesdropper's status is not current, so nothing can be paused or resumed from here.")
            }
        }
    }

    private func fleetRow(_ status: ReducedEavesdropperStatus) -> some View {
        HStack(spacing: 8) {
            Text("Every seat")
                .font(.system(size: 11, weight: .semibold))
                .frame(width: 110, alignment: .leading)
            Text(OperatorPausePresentation.fleetLine(status))
                .font(.system(size: 11))
                .foregroundStyle(status.pause?.paused == true ? Color.orange : Color.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            if status.pause?.paused == true {
                button("Resume Every Seat", .resume, seat: nil)
            } else if status.pause?.paused == false {
                button("Pause Wakes", .pauseWakes, seat: nil)
                button("Pause Everything", .pause, seat: nil)
            }
        }
    }

    private func seatRow(_ seat: String, status: ReducedEavesdropperStatus) -> some View {
        let facts = status.seats.first { $0.seat == seat }
        let fleetPaused = status.pause?.paused == true
        return HStack(spacing: 8) {
            Text(seat)
                .font(.system(size: 11, design: .monospaced).weight(.semibold))
                .lineLimit(1)
                .frame(width: 110, alignment: .leading)
            Text(OperatorPausePresentation.seatLine(paused: facts?.paused, fleetPaused: fleetPaused))
                .font(.system(size: 11))
                .foregroundStyle(facts?.paused == true ? Color.orange : Color.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            // A fleet-wide pause covers every seat:  lift it above, not here.
            if !fleetPaused {
                if facts?.paused == true {
                    button("Resume", .resume, seat: seat)
                } else if facts?.paused == false {
                    button("Pause Wakes", .pauseWakes, seat: seat)
                    button("Pause", .pause, seat: seat)
                }
            }
        }
    }

    private func button(_ title: String, _ kind: OperatorPauseRequest.Kind, seat: String?) -> some View {
        let request = OperatorPauseRequest(kind: kind, seat: seat)
        return Button(title) {
            controller.plan(.eavesdropperPause(request))
        }
        .controlSize(.mini)
        .disabled(controller.refusesNewPlan || !request.isValid)
        .help(OperatorPausePresentation.help(request))
    }
}

/// The words of the Pause And Resume section.
enum OperatorPausePresentation {
    static func fleetLine(_ status: ReducedEavesdropperStatus) -> String {
        guard let pause = status.pause, let paused = pause.paused else {
            return "Whether every seat is paused is not reported."
        }
        guard paused else { return "Not paused." }
        var line = pause.wakesOnly == true
            ? "Wakes are paused for every seat, and live interrupts and capture go on"
            : "Wakes and live interrupts are paused for every seat, and capture goes on"
        if let setBy = pause.setBy { line += ", set by \(setBy)" }
        if let since = pause.sinceText { line += ", since \(since)" }
        return line + "."
    }

    static func seatLine(paused: Bool?, fleetPaused: Bool) -> String {
        if fleetPaused { return "Paused with every seat." }
        switch paused {
        case true?: return "Paused."
        case false?: return "Not paused."
        case nil: return "Whether it is paused is not reported."
        }
    }

    static func help(_ request: OperatorPauseRequest) -> String {
        switch request.kind {
        case .pause:
            return "Plan a pause of wakes and live interrupts for \(request.name).\u{00A0} Nothing runs until you approve the plan."
        case .pauseWakes:
            return "Plan a pause of routine wakes for \(request.name).\u{00A0} Nothing runs until you approve the plan."
        case .resume:
            return "Plan lifting the pause for \(request.name).\u{00A0} Nothing runs until you approve the plan."
        }
    }
}
