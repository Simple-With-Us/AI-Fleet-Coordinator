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

    /// The pause in plain words, for one target.
    func sentence(for name: String) -> String {
        var line = wakesOnly == true
            ? "\(name): wakes are paused, and live interrupts and capture go on"
            : "\(name): wakes and live interrupts are paused, and capture goes on"
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

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        all = container.optionalField(OperatorPauseEntry.self, .all)
        let raw = (try? container.decodeIfPresent([String: OperatorPauseEntry].self, forKey: .seats)) ?? [:]
        var seats: [String: OperatorPauseEntry] = [:]
        for key in raw.keys.sorted() where OpsCommandBuilder.isSeatName(key) && seats.count < 64 {
            seats[key] = raw[key]
        }
        self.seats = seats
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
