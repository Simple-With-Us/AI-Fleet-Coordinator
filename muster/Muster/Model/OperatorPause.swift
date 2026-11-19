import Foundation

/// A pause or a resume of the server Eavesdropper's kill switch (AFC-832), as the control helper's
/// `server pause|pause-wakes|resume server|SEAT` takes it.
///
/// Only a button the owner presses plans one, the plan shows the exact command and the pause in
/// force, and the run needs Touch ID.  Nothing in Muster pauses on its own (Jay, Nov 17:  a Mac
/// sign-in popup is not a reason to pause wake delivery):  `OpsController.runUnattended` refuses it.
struct OperatorPauseRequest: Equatable, Hashable {
    enum Kind: String, CaseIterable {
        /// Wakes and live interrupts stop;  capture goes on.
        case pause
        /// Routine wakes stop;  live interrupts and capture go on.
        case pauseWakes = "pause-wakes"
        /// Lifts a pause:  every pause for every seat, or one seat's own.
        case resume
    }

    let kind: Kind
    /// One seat, or nil for every seat (the helper's target word `server`).
    let seat: String?

    /// The helper's verb, after `server`.
    var verbWord: String { kind.rawValue }
    /// The helper's one target word.
    var targetWord: String { seat ?? "server" }
    /// What the owner reads for the target.
    var name: String { seat ?? "every seat" }
    /// True when the seat, if any, is in the helper's seat shape.
    var isValid: Bool { seat.map(OpsCommandBuilder.isSeatName) ?? true }
}

/// One pause, as the helper reduced it:  whether it stops wakes only, a word for who set it, and
/// when.  The helper keeps no address or other free text for who set it.
struct OperatorPauseEntry: Decodable, Equatable {
    let wakesOnly: Bool?
    let setBy: String?
    let since: String?

    enum CodingKeys: String, CodingKey {
        case wakesOnly, setBy, since
    }

    init(wakesOnly: Bool?, setBy: String?, since: String?) {
        self.wakesOnly = wakesOnly
        self.setBy = setBy
        self.since = since
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        wakesOnly = container.optionalField(Bool.self, .wakesOnly)
        setBy = container.optionalText(.setBy, limit: 32)
        since = container.optionalText(.since, limit: 40)
    }

    /// The pause in plain words, for one target.  A pause whose kind was not reported says so, and
    /// is never shown as the stronger full pause.
    func sentence(for name: String) -> String {
        var line: String
        switch wakesOnly {
        case true?: line = "\(name): wakes are paused, and live interrupts and capture go on"
        case false?: line = "\(name): wakes and live interrupts are paused, and capture goes on"
        case nil: line = "\(name): paused, and whether live interrupts stop too is not reported"
        }
        if let setBy { line += ", set by \(setBy)" }
        if let since { line += ", since \(since)" }
        return line + "."
    }
}

/// The pause in force, as a pause verb's plan or run answers it (`pause`).
struct OperatorPauseState: Decodable, Equatable {
    /// The fleet-wide pause, or nil when there is none.
    let all: OperatorPauseEntry?
    /// Each seat's own pause, by seat.
    let seats: [String: OperatorPauseEntry]

    enum CodingKeys: String, CodingKey {
        case all, seats
    }

    init(all: OperatorPauseEntry?, seats: [String: OperatorPauseEntry]) {
        self.all = all
        self.seats = seats
    }

    /// Strict for the two parts it reads:  an `all` that is not an object (or null), or `seats`
    /// that is not a map of objects, throws, so a malformed pause is shown as unreadable (the
    /// answer's `pause` is nil) and never as "Nothing is paused."  Other keys are left for later
    /// fields, as the helper's answers add them.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        all = try container.decodeIfPresent(OperatorPauseEntry.self, forKey: .all)
        let raw = try container.decodeIfPresent([String: OperatorPauseEntry].self, forKey: .seats) ?? [:]
        // Every key must be a seat:  the helper sends nothing else, so another key is a malformed
        // answer, not one to drop quietly.  No cap, because the answer itself is size-bounded.
        guard raw.keys.allSatisfy(OpsCommandBuilder.isSeatName) else {
            throw DecodingError.dataCorrupted(DecodingError.Context(
                codingPath: container.codingPath,
                debugDescription: "A pause seat key is not a seat name."
            ))
        }
        self.seats = raw
    }

    /// True when nothing is paused.
    var isClear: Bool { all == nil && seats.isEmpty }

    /// The pause in force, one line each, the fleet-wide pause first.
    var lines: [String] {
        if isClear { return ["Nothing is paused."] }
        var lines: [String] = []
        if let all { lines.append(all.sentence(for: "Every seat")) }
        for seat in seats.keys.sorted() {
            if let entry = seats[seat] { lines.append(entry.sentence(for: seat)) }
        }
        return lines
    }
}

/// The words of the Pause And Resume section.  In the model so its tests need no view.
enum OperatorPausePresentation {
    static func fleetLine(_ status: ReducedEavesdropperStatus) -> String {
        guard let pause = status.pause, let paused = pause.paused else {
            return "Whether every seat is paused is not reported."
        }
        guard paused else { return "Not paused." }
        // A pause whose kind was not reported is never shown as the stronger full pause.
        var line: String
        switch pause.wakesOnly {
        case true?: line = "Wakes are paused for every seat, and live interrupts and capture go on"
        case false?: line = "Wakes and live interrupts are paused for every seat, and capture goes on"
        case nil: line = "Paused for every seat, and whether live interrupts stop too is not reported"
        }
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
