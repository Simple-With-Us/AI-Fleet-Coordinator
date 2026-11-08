import Foundation

// What a server seat's row in Seat Activity shows (AFC-616).
//
// A seat the seat partition gives to the server is held by the server's eavesdropper, which the
// Mac reads through the server's control helper (`ServerEavesdropperStatusStore`, the
// `server eavesdropper-status` read).  This file turns what that reading says about one seat
// into the words its row shows, and nothing more:
//
//   * a current reading shows the seat's state, wake, sessions and quota as the server said them
//   * a reading that is not current shows no state at all in the cells:  the row says why it is
//     not current, how old it is, and what the last reading said, as old evidence
//   * every other case says, in a sentence, what is known and why there is no reading
//
// It is pure:  the view hands it the reading and the clock, so each case is tested on its own
// (ServerSeatRowTests).  Mac seats never come here;  their rows are built from the Mac's own
// eavesdropper (`SeatActivityBuilder`).

/// What one server seat's row shows, from the server eavesdropper's reading of that seat.
struct ServerSeatRowPresentation: Equatable {
    /// The column name the row's sentence is shown under.
    static let noteColumn = "server eavesdropper"

    /// The eavesdropper cell's first line.
    let state: String
    /// Whether that line is something to look at, rather than a plain answer.
    let stateIsProblem: Bool
    /// The lines under it, in the eavesdropper cell.
    let details: [String]
    /// The sessions cell.
    let sessions: String
    /// Whether a current reading holds at least one live session for the seat.
    let sessionsLive: Bool
    /// One line per session the server's eavesdropper holds for the seat, from a current reading
    /// only.
    let sessionLines: [String]
    /// The quota cell.
    let quota: String
    /// Whether the quota is used up, from a current reading only.
    let quotaIsProblem: Bool
    /// When the quota resets, from a current reading only.
    let quotaReset: Date?
    /// The sentence under the row:  what is known and why.  Nil for a current reading.
    let note: String?
    /// Whether that sentence is a problem, rather than a plain explanation.
    let noteIsProblem: Bool
    /// The eavesdropper cell's tooltip.
    let help: String
    /// True only for a current reading.
    let isCurrent: Bool

    private static var gap: String { ServerStatusCopy.sentenceGap }

    /// The row for `reading`, the server's answer about one seat.
    ///
    /// - Parameters:
    ///   - reading: what the store says about the seat (`ServerEavesdropperStatusStore.seat(_:)`).
    ///   - status: the document the reading came from, current or not, for its age.
    ///   - readAt: when the store's last reading came back, or nil before the first one.
    ///   - loading: whether the store is reading now.
    ///   - now: the clock the times are written against.
    init(
        reading: ServerSeatReading,
        status: ReducedEavesdropperStatus?,
        readAt: Date?,
        loading: Bool,
        now: Date
    ) {
        let gap = Self.gap
        switch reading {
        case .read(let facts):
            let paused = facts.pauseState == .paused
            state = paused ? "Paused" : facts.connection.label
            stateIsProblem = facts.connection != .connected || facts.runState == .stopped || paused
            var lines: [String] = []
            if paused { lines.append(facts.connection.label) }
            lines.append(Self.runLine(facts.runState))
            lines.append("Wake: \(facts.wakeKind.label)")
            if facts.lastEvent != nil {
                lines.append("Last event \(facts.lastEventLabel(now: now))")
            }
            details = lines
            sessions = Self.sessionCount(facts.sessions)
            sessionsLive = facts.sessions.contains { $0.alive == true }
            sessionLines = facts.sessions.map(Self.sessionLine)
            quota = facts.quotaReading.label
            if case .known(let exhausted, _, let reset) = facts.quotaReading {
                quotaIsProblem = exhausted == true
                quotaReset = reset
            } else {
                quotaIsProblem = false
                quotaReset = nil
            }
            note = nil
            noteIsProblem = false
            var help = "What the server's eavesdropper says about this seat"
            if let readAt { help += ", read at \(OperatorVoiceReading.whenText(readAt, now: now))" }
            help += "."
            help += gap + "Wakes in the last day: \(facts.wakesLastDayText)."
            help += gap + "Held back: \(facts.heldBackText)."
            help += gap + "Jobs: \(facts.jobsText)."
            help += gap + "Session letters: \(facts.letterState.label)."
            self.help = help
            isCurrent = true

        case .notCurrent(let facts, let why):
            state = "Not current"
            stateIsProblem = true
            if let generatedAt = status?.generatedAt, generatedAt <= now {
                // The age of the server's own answer, from when it wrote it, not when Muster read it.
                details = ["Reported \(FleetTime.ageText(since: generatedAt, now: now)) ago"]
            } else {
                details = []
            }
            sessions = "Not current"
            sessionsLive = false
            sessionLines = []
            quota = "Not current"
            quotaIsProblem = false
            quotaReset = nil
            if let facts {
                note = why + gap + Self.lastKnownSentence(facts)
            } else {
                note = why + gap + "That old reading does not hold this seat, which says nothing about the seat now."
            }
            noteIsProblem = true
            help = why
            isCurrent = false

        case .notHeld(let why):
            state = "Not held"
            stateIsProblem = true
            details = []
            sessions = "Not held"
            sessionsLive = false
            sessionLines = []
            quota = "Not held"
            quotaIsProblem = false
            quotaReset = nil
            note = why + gap + "The seat partition gives this seat to the server."
            noteIsProblem = true
            help = why
            isCurrent = false

        case .notRead(let why):
            let firstRead = readAt == nil
            let word = firstRead ? (loading ? "Reading\u{2026}" : "Not read yet") : "Not read"
            state = word
            stateIsProblem = !firstRead
            details = []
            sessions = word
            sessionsLive = false
            sessionLines = []
            quota = word
            quotaIsProblem = false
            quotaReset = nil
            let sentence = firstRead && loading ? "Muster is reading the server's eavesdropper now." : why
            note = sentence
            noteIsProblem = !firstRead
            help = sentence
            isCurrent = false

        case .unavailable(let reason):
            state = "Not available"
            stateIsProblem = true
            details = []
            sessions = "Not available"
            sessionsLive = false
            sessionLines = []
            quota = "Not available"
            quotaIsProblem = false
            quotaReset = nil
            note = reason.cause
            noteIsProblem = true
            help = reason.help
            isCurrent = false

        case .notThisAppsVersion(let why):
            state = "Not readable"
            stateIsProblem = true
            details = []
            sessions = "Not readable"
            sessionsLive = false
            sessionLines = []
            quota = "Not readable"
            quotaIsProblem = false
            quotaReset = nil
            note = why
            noteIsProblem = true
            help = why
            isCurrent = false
        }
    }

    /// Whether the seat's listener runs, as one line.
    static func runLine(_ state: ServerSeatRunState) -> String {
        switch state {
        case .running: return "Running"
        case .stopped: return "Stopped"
        case .notKnown: return "Running or not is not known"
        case .notReported: return "Running or not is not reported"
        }
    }

    /// The live sessions the server's eavesdropper holds for the seat, as the cell shows them.
    /// A session whose liveness is not said is never counted as ended.
    static func sessionCount(_ sessions: [ServerSeatSession]) -> String {
        guard !sessions.isEmpty else { return "None" }
        let live = sessions.filter { $0.alive == true }.count
        if live == 0 {
            return sessions.contains { $0.alive == nil } ? EavesdropperStatusText.notReported : "None live"
        }
        return live == 1 ? "1 session" : "\(live) sessions"
    }

    /// One session in one line:  its letter, platform, where it was verified and what it holds.
    /// A server session has no title:  it never leaves the server.
    static func sessionLine(_ session: ServerSeatSession) -> String {
        var parts: [String] = [session.letter ?? SeatActivityText.noLetter]
        if let platform = session.platform { parts.append(platform) }
        switch session.verifiedHost {
        case "mac"?: parts.append("verified on the Mac")
        case "server"?: parts.append("verified on the server")
        case "cloud"?: parts.append("verified in the cloud")
        default: break
        }
        if let topics = session.topics { parts.append(topics == 1 ? "1 topic" : "\(topics) topics") }
        if let pending = session.pending, pending > 0 { parts.append("\(pending) waiting") }
        // Only a session that has not ended can be woken again, so only its watcher matters.
        if session.alive != false {
            switch session.watcher {
            case true?: break
            case false?: parts.append("no rewake watcher (a new message will not wake it again)")
            case nil: parts.append("rewake watcher not said")
            }
        }
        switch session.alive {
        case true?: break
        case false?: parts.append("ended")
        case nil: parts.append("alive or not is not said")
        }
        return parts.joined(separator: ", ")
    }

    /// What a reading that is not current last said about the seat, marked as old evidence.
    static func lastKnownSentence(_ facts: ServerSeatFacts) -> String {
        var said = [facts.connection.label, runLine(facts.runState)]
        if facts.pauseState == .paused { said.append("Paused") }
        said.append("wake \(facts.wakeKind.label)")
        said.append("quota \(facts.quotaReading.label)")
        return "The last reading said: \(said.joined(separator: ", "))."
            + gap + "That is old evidence, not the seat's state now."
    }

    /// The one line under the Held By The Server heading:  when Muster read the server's
    /// eavesdropper and whether that answer is current.  The rows say the rest.
    static func groupLine(
        reading: ServerEavesdropperReading?,
        readAt: Date?,
        loading: Bool,
        now: Date
    ) -> String {
        let gap = Self.gap
        guard let reading, let readAt else {
            return loading
                ? "Muster is reading the server's eavesdropper now."
                : "Muster has not read the server's eavesdropper yet."
        }
        let when = "Muster read the server's eavesdropper at \(OperatorVoiceReading.whenText(readAt, now: now))"
        var line: String
        switch reading {
        case .read(let status):
            if status.notCurrentReason(now: now) != nil {
                line = "\(when), but its answer is not current, so these rows show the last reading only as old evidence."
            } else {
                line = "\(when), and its answer is current."
                if let pause = status.pause, pause.paused == true {
                    line += gap + (pause.wakesOnly == true
                        ? "The server's eavesdropper has every seat's wakes paused."
                        : "The server's eavesdropper is paused for every seat.")
                }
            }
        case .notRead, .unavailable, .notThisAppsVersion:
            line = "\(when), but no reading came back, so each row says why."
        }
        if loading { line += gap + "Muster is reading it again now." }
        return line
    }
}
