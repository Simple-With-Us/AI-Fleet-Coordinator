import Foundation

// How old a report is,  and whether it is still current,  at this moment.
//
// A report's age and its current or stale verdict used to be worked out once,  when a panel
// read the file.  A panel left open,  or a Mac that slept,  kept that answer for as long as it
// stayed on screen,  so a report crossed its window and still read as current.  This file
// works both out again from the report's own time and the clock,  every time it is asked,
// so a view that asks on a tick and on wake can never hold a frozen answer.
//
// It also says what the file is.  Most of the reports Muster shows are copies:  the server
// writes its report,  and the doctor copies it to this Mac.  "Report read 5m ago" said the
// age of that copy as if it were a live look at the server.  The words here name who wrote
// the report,  where,  and how it came to this Mac.
//
// Nothing here reads a file,  reaches the network or touches the interface,  so the tests
// drive it with fixed times.

// MARK: - The verdict

/// How a report stands against its window at one moment.
enum FreshnessVerdict: Equatable {
    /// Inside its window.
    case current
    /// Past its window:  its rows are old evidence,  not current health.
    case stale
    /// Dated more than a minute ahead of the Mac's clock,  so nothing in it is current.
    case futureDated
    /// The Mac woke after the report was last read,  and it has not been read again yet.
    case checking
    /// The report carries no time this app can read.
    case undated

    /// True only inside the window.  Every other verdict is not current.
    var isCurrent: Bool { self == .current }
}

/// Who wrote a report and how it reached this Mac,  so its age line says what it is.
enum ReportOrigin: Equatable {
    /// The server wrote it,  and the doctor copied it to this Mac (`server-status.json`).
    case serverCopiedByDoctor
    /// A VM wrote it,  and the doctor copied it to this Mac (`vms/<label>.json`).
    case vmCopiedByDoctor
    /// The doctor wrote it on this Mac,  from both machines (`report.json`).
    case doctorOnMac
    /// A collector on this Mac wrote it,  named in words (`Hog Hunter`).
    case macCollector(String)
    /// Muster ran a read-only collector on this Mac itself,  and no file was written.
    case collectedNow
}

/// A report's time against the clock,  at one moment.
struct ReportFreshness: Equatable {
    /// How often an open panel works the age out again.
    static let tickInterval: TimeInterval = 30
    /// How often an open panel reads its local files again.
    static let rereadInterval: TimeInterval = 60
    /// How far ahead of the Mac's clock a report may be dated and still be read,  the same
    /// minute every reader allows.
    static let futureTolerance: TimeInterval = 60

    /// When the report says it was written.
    let generatedAt: Date?
    /// When this Mac's copy was written,  from the file's own modification date.
    let copiedAt: Date?
    /// The report's window.
    let maxAge: TimeInterval
    /// The moment this is worked out for.
    let now: Date
    /// When the panel last read the file,  when it keeps that.
    let readAt: Date?
    /// When the Mac last woke,  when the panel heard it.
    let wokeAt: Date?

    init(generatedAt: Date?, copiedAt: Date? = nil, maxAge: TimeInterval, now: Date,
         readAt: Date? = nil, wokeAt: Date? = nil) {
        self.generatedAt = generatedAt
        self.copiedAt = copiedAt
        self.maxAge = maxAge
        self.now = now
        self.readAt = readAt
        self.wokeAt = wokeAt
    }

    /// The verdict at `now`.
    ///
    /// A read from before the Mac woke is not trusted until the file is read again:  what it
    /// says is from before the sleep,  however young the report looked then.
    var verdict: FreshnessVerdict {
        guard let generatedAt else { return .undated }
        if let wokeAt, (readAt ?? .distantPast) < wokeAt { return .checking }
        let seconds = now.timeIntervalSince(generatedAt)
        if seconds < -Self.futureTolerance { return .futureDated }
        return seconds <= maxAge ? .current : .stale
    }

    /// How old the report is,  in seconds,  never below zero.
    var ageSeconds: TimeInterval? {
        generatedAt.map { max(0, now.timeIntervalSince($0)) }
    }

    /// The age in the app's short form (`5m`),  for a line that already uses it.
    var ageShort: String? {
        generatedAt.map { FleetTime.ageText(since: $0, now: now) }
    }

    /// The age in words (`5 minutes ago`).
    var ageWords: String? {
        ageSeconds.map(FreshnessWords.ago)
    }
}

// MARK: - The words for times

/// Ages and times in the owner's words:  12-hour clock times on Jay's clock,  with a day word
/// whenever the day is not today,  so a time from last night never reads as this morning's.
enum FreshnessWords {
    /// `less than a minute ago`,  `1 minute ago`,  `5 minutes ago`,  `2 hours ago`,  `3 days ago`.
    static func ago(_ seconds: TimeInterval) -> String {
        let whole = max(0, Int(seconds))
        if whole < 60 { return "less than a minute ago" }
        if whole < 3600 { return plural(whole / 60, "minute") + " ago" }
        if whole < 86400 { return plural(whole / 3600, "hour") + " ago" }
        return plural(whole / 86400, "day") + " ago"
    }

    /// A window as an adjective:  `45 minute`,  `1 hour`.
    static func window(_ seconds: TimeInterval) -> String {
        let minutes = Int(seconds / 60)
        if minutes >= 60, minutes % 60 == 0 { return "\(minutes / 60) hour" }
        return "\(minutes) minute"
    }

    /// A moment without a preposition:  `2:14 PM` today,  `11:32 PM yesterday`,  `Nov 3, 11:32 PM`
    /// on another day this year,  and `Nov 3, 2025, 11:32 PM` in another year.
    static func stamp(_ date: Date, now: Date) -> String {
        let clock = FleetTime.clock(date)
        switch day(of: date, now: now) {
        case .today: return clock
        case .yesterday: return "\(clock) yesterday"
        case .otherDay(let words): return "\(words), \(clock)"
        }
    }

    /// A moment to follow a verb:  `at 2:14 PM`,  `at 11:32 PM yesterday`,  `on Nov 3 at 11:32 PM`.
    static func when(_ date: Date, now: Date) -> String {
        let clock = FleetTime.clock(date)
        switch day(of: date, now: now) {
        case .today: return "at \(clock)"
        case .yesterday: return "at \(clock) yesterday"
        case .otherDay(let words): return "on \(words) at \(clock)"
        }
    }

    private enum Day {
        case today
        case yesterday
        case otherDay(String)
    }

    /// Which day a moment falls on,  on the same clock `FleetTime.clock` uses.
    private static func day(of date: Date, now: Date) -> Day {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Chicago") ?? .current
        if calendar.isDate(date, inSameDayAs: now) { return .today }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
           calendar.isDate(date, inSameDayAs: yesterday) {
            return .yesterday
        }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.locale = Locale(identifier: "en_US_POSIX")
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        formatter.dateFormat = sameYear ? "MMM d" : "MMM d, yyyy"
        return .otherDay(formatter.string(from: date))
    }

    private static func plural(_ count: Int, _ noun: String) -> String {
        count == 1 ? "1 \(noun)" : "\(count) \(noun)s"
    }
}

// MARK: - The words for a report

/// What a report's age line says,  from its origin and its freshness.
enum ReportFreshnessCopy {
    private static let gap = ServerStatusCopy.sentenceGap

    /// The age line,  naming who wrote the report and how it came here.  Nil for a report with
    /// no time,  whose problem is said elsewhere.
    static func ageLine(_ origin: ReportOrigin, _ freshness: ReportFreshness) -> String? {
        // A report dated ahead of the clock has no age to say;  its note says why.
        guard freshness.verdict != .futureDated, let age = freshness.ageWords else { return nil }
        let copied = freshness.copiedAt.map { " \(FreshnessWords.when($0, now: freshness.now))" } ?? ""
        switch origin {
        case .serverCopiedByDoctor:
            return "Server report written \(age), copied to this Mac by the doctor\(copied)."
        case .vmCopiedByDoctor:
            return "VM report written \(age), copied to this Mac by the doctor\(copied)."
        case .doctorOnMac:
            return "Doctor's report written on this Mac \(age)."
        case .macCollector(let name):
            return "Written by \(name) on this Mac \(age)."
        case .collectedNow:
            return "Collected live from this Mac \(age), without writing a file."
        }
    }

    /// What the verdict means,  or nil when the report is current.
    static func verdictNote(_ origin: ReportOrigin, _ freshness: ReportFreshness) -> String? {
        switch freshness.verdict {
        case .current, .undated:
            return nil
        case .checking:
            return "This Mac woke since the last read, so the file is being read again."
        case .futureDated:
            return "It is dated ahead of this Mac's clock, so nothing in it is current."
        case .stale:
            let window = FreshnessWords.window(freshness.maxAge)
            let opening = "It is past its \(window) window, so its rows are old evidence, not current health."
            guard let next = staleNextStep(origin) else { return opening }
            return "\(opening)\(gap)\(next)"
        }
    }

    /// What to do about a copy that has gone stale,  or nil when the panel says it itself.
    static func staleNextStep(_ origin: ReportOrigin) -> String? {
        switch origin {
        case .serverCopiedByDoctor:
            return "The doctor copies a new one every 15 minutes;  if none arrives, use Fetch From The Server Again."
        case .vmCopiedByDoctor:
            return "The doctor copies VM reports when it runs with --vms;  docs/protocols/fleet-vm-status.md has the steps."
        case .doctorOnMac:
            return "The doctor writes a new one every 15 minutes;  if none arrives, check the Fleet Tools Doctor in Background Agents."
        case .macCollector, .collectedNow:
            return nil
        }
    }
}

// MARK: - How often to read

/// How often an open panel reads its files,  slower in Low Power Mode,  where a read that
/// can wait should.
enum RereadSchedule {
    /// How much slower the reads are in Low Power Mode.
    static let lowPowerFactor: Double = 4

    static func interval(base: TimeInterval, lowPower: Bool) -> TimeInterval {
        lowPower ? base * lowPowerFactor : base
    }
}

// MARK: - The seat check on the Mac

/// Whether the engine's last check of the seats is recent enough to count.
///
/// The engine runs when a watched file changes and at least every ten minutes from the safety
/// net,  so a check older than fifteen minutes means it has not been running:  most often the
/// Mac slept.  An old check is not current,  whatever it counted then,  so the menu and the
/// Overview read it as not known until a new one lands.
enum SeatCheckFreshness {
    /// The safety net's ten minutes,  with room for its leeway and for the run itself.
    static let window: TimeInterval = 15 * 60

    static func isCurrent(checkedAt: Date, now: Date) -> Bool {
        now.timeIntervalSince(checkedAt) <= window
    }

    /// The menu's seat line for a check that is no longer current.
    static func menuLine(checkedAt: Date, now: Date) -> String {
        "Seat Setup:  not current, last checked \(FreshnessWords.when(checkedAt, now: now))"
    }

    /// The run's verdict,  unless the check is no longer current:  then "Not Current",  never
    /// the old "All Passing".  A run in flight still says so.
    static func verdict(_ verdict: RunVerdict, isCurrent: Bool) -> RunVerdict {
        guard !isCurrent, verdict.tone == .pass || verdict.tone == .fail else { return verdict }
        return RunVerdict(words: "Not Current", tone: .notKnown)
    }

    /// The Overview's line beside the verdict,  or nil before any check.
    static func overviewLine(checkedAt: Date?, now: Date) -> String? {
        guard let checkedAt else { return nil }
        let when = FreshnessWords.when(checkedAt, now: now)
        let age = FreshnessWords.ago(now.timeIntervalSince(checkedAt))
        if isCurrent(checkedAt: checkedAt, now: now) {
            return "Last checked \(when), \(age)"
        }
        return "Not current:  last checked \(when), \(age)"
    }
}

// MARK: - The server:  what to do next

extension ServerReach {
    /// True when the next step is to fetch the report again:  the Mac's own fetch stopped,
    /// the server could not be reached,  or the copy on the Mac is missing or not the server's.
    /// The menu and Server Status both offer Fetch From The Server Again from this.
    var offersFetch: Bool {
        switch self {
        case .fetchStopped, .unreachable, .noReport:
            return true
        case .cannotRead(let refusal):
            return refusal == .notFromServer || refusal == .unreadable
        case .notReadYet, .reportStopped, .futureDated, .current:
            return false
        }
    }
}

/// The job that copies the server's report to this Mac,  and when it next runs.
enum DoctorSchedule {
    /// The doctor's LaunchAgent runs every fifteen minutes (`StartInterval` 900).
    static let interval: TimeInterval = 15 * 60
    /// How late a run may be before the line says it has not come.
    static let grace: TimeInterval = 5 * 60

    /// Names the job and when it next runs,  from the doctor's own last report.
    static func serverCopyLine(lastRunAt: Date?, now: Date) -> String {
        let opening = "The Fleet Tools Doctor on this Mac copies the server's report every 15 minutes."
        guard let lastRunAt else { return opening }
        let due = lastRunAt.addingTimeInterval(interval)
        let when = FreshnessWords.when(due, now: now)
        if now.timeIntervalSince(due) <= grace {
            return "\(opening)\(ServerStatusCopy.sentenceGap)Its next run is due \(when)."
        }
        return "\(opening)\(ServerStatusCopy.sentenceGap)Its next run was due \(when) and has not come."
    }
}

/// What Server Status says to do when its report is not current.
enum ServerNextStep {
    private static let gap = ServerStatusCopy.sentenceGap

    /// The next step for the reach,  or nil when there is nothing to do.
    static func sentence(_ reach: ServerReach) -> String? {
        switch reach {
        case .notReadYet, .current:
            return nil
        case .fetchStopped:
            return "The Mac's copy is not being refreshed.\(gap)Fetch From The Server Again restarts the Fleet Tools Doctor on this Mac, which asks the server for a new copy."
        case .unreachable:
            return "The Mac could not reach the server on its last try.\(gap)Ask The Server Now tries the connection and changes nothing;  once it answers, Fetch From The Server Again asks for a new copy."
        case .reportStopped:
            return "The server answered, but its report stopped.\(gap)Restart the Server Report Timer below, or read its logs."
        case .noReport:
            return "This Mac holds no copy yet.\(gap)Fetch From The Server Again asks the server for one."
        case .cannotRead(let refusal):
            switch refusal {
            case .appOutOfDate: return "Update Muster to read this report."
            case .reporterOutOfDate: return "Update the server's reporter, then fetch the report again."
            case .notFromServer: return "Fetch From The Server Again replaces this file with the server's own report."
            case .unreadable: return "Fetch From The Server Again asks the server for a new copy."
            }
        case .futureDated:
            return "Check the clocks on this Mac and on the server, then fetch the report again."
        }
    }
}

// MARK: - The server:  whether an action can run

/// Whether a server action is offered,  from what the app already knows about reaching it.
///
/// A plan reads the server first,  so a server the Mac could not reach only gives a plan that
/// fails.  The action is turned off with the reason beside it,  and Ask The Server Now,  which
/// changes nothing,  turns it back on once the server answers.  When the Mac only lacks a
/// current word,  the action stays on with a warning:  the plan finds out,  and runs nothing
/// if it cannot reach the server.
struct ServerActionGate: Equatable {
    /// True when the action is turned off.
    let blocked: Bool
    /// Why,  or the warning beside an action that stays on.  Nil says nothing.
    let note: String?

    static let open = ServerActionGate(blocked: false, note: nil)

    static func make(reach: ServerReach, asked: ServerAskResult?) -> ServerActionGate {
        let gap = ServerStatusCopy.sentenceGap
        switch reach {
        case .unreachable(let checkedAt, _):
            // A later answer to Ask The Server Now outranks the doctor's failed try.
            if let asked, asked.answered, asked.at >= (checkedAt ?? .distantPast) {
                return ServerActionGate(blocked: false,
                                        note: "The server answered at \(FleetTime.clock(asked.at)), after the Mac's last failed try.")
            }
            let when = checkedAt.map { " at \(FleetTime.clock($0))" } ?? ""
            return ServerActionGate(
                blocked: true,
                note: "The Mac could not reach the server\(when), so this cannot run.\(gap)Ask The Server Now tries the connection again and changes nothing."
            )
        case .fetchStopped:
            return ServerActionGate(
                blocked: false,
                note: "The Mac has no current word from the server, so it may not be reachable.\(gap)The plan asks the server first, and nothing runs if it cannot."
            )
        case .notReadYet, .reportStopped, .noReport, .cannotRead, .futureDated, .current:
            if let asked, !asked.answered {
                return ServerActionGate(
                    blocked: false,
                    note: "The server did not answer at \(FleetTime.clock(asked.at)).\(gap)The plan asks the server first, and nothing runs if it cannot."
                )
            }
            return .open
        }
    }
}

// MARK: - The server:  what "changed" means

/// The words for the server report's file list.
enum ServerConfigCopy {
    /// The collector compares each watched file with its own previous report and nothing else,
    /// so a changed file is one that moved since then,  not one that differs from what the
    /// checkout should hold.
    static let comparisonNote = "Each file is compared with the server's previous report only, not with the checked-in version.\(ServerStatusCopy.sentenceGap)An edit made before the previous report no longer shows here."
}

// MARK: - The server:  what a redeploy restarts

/// What a redeploy of one row restarts,  said on the plan:  the whole application,  every
/// container in it,  and not only the row the button was on.
struct RedeployScope: Equatable {
    let application: String
    /// How many containers the report says the application has,  when it says.
    let containers: Int?
    /// The application's services,  from the report's application list or else from its rows.
    let services: [String]

    static func make(reading: ServerStatusReading, resource: CoolifyResource) -> RedeployScope {
        let application = reading.applications.first { $0.uuid == resource.uuid }
        var services = application?.services ?? []
        if services.isEmpty {
            var seen: Set<String> = []
            for row in reading.coolify where row.uuid == resource.uuid && row.resourceStatus != .absent {
                let name = row.service ?? row.name
                if !name.isEmpty, !seen.contains(name) {
                    seen.insert(name)
                    services.append(name)
                }
            }
        }
        return RedeployScope(application: reading.redeployName(of: resource),
                             containers: application?.containers,
                             services: services)
    }

    /// The sentence on the plan.
    var sentence: String {
        let listed = services.isEmpty ? "" : ":  \(Self.list(services))"
        guard let containers else {
            if services.count > 1 {
                return "This redeploys all of \(application), not only one container\(listed)."
            }
            return "The report does not say how many containers a redeploy of \(application) restarts."
        }
        if containers == 1 { return "This redeploys the one container of \(application)\(listed)." }
        return "This redeploys all \(containers) containers of \(application)\(listed)."
    }

    /// `a`,  `a and b`,  `a, b and c`.
    static func list(_ names: [String]) -> String {
        guard names.count > 1 else { return names.first ?? "" }
        return names.dropLast().joined(separator: ", ") + " and " + (names.last ?? "")
    }
}

/// The scope a panel noted when it planned a redeploy,  so the plan sheet can say it.
///
/// The action carries only the application's uuid and name,  and every row of the report that
/// shares the uuid goes with it,  so the panel that planned it writes the scope here and the
/// sheet reads it back by uuid.
final class RedeployScopeNotes: @unchecked Sendable {
    static let shared = RedeployScopeNotes()

    private let lock = NSLock()
    private var scopes: [String: RedeployScope] = [:]

    init() {}

    func note(_ scope: RedeployScope, uuid: String) {
        lock.lock()
        scopes[uuid] = scope
        lock.unlock()
    }

    func scope(for uuid: String) -> RedeployScope? {
        lock.lock()
        defer { lock.unlock() }
        return scopes[uuid]
    }
}

// MARK: - The Mac's agents:  which reading to show

/// Where the Background Agents rows came from.
enum MacAgentsSource: Equatable {
    /// The file the scheduled collector writes.
    case savedFile
    /// A read-only collection Muster ran itself,  which writes no file.
    case collectedLive
}

/// What Background Agents shows:  the newest good reading,  and why a collection failed when
/// one did.
///
/// The saved file is read on every tick,  and a newer file replaces older rows,  so the rows
/// never go stale beside a fresh file.  A collection that fails keeps the last good rows,  which
/// then age past their window on their own,  and says why it failed beside them.
struct MacAgentsShown {
    var reading: MacServicesReading?
    var source: MacAgentsSource = .savedFile
    /// Why the last collection failed,  when it did and older rows are still shown.
    var collectProblem: String?

    /// Takes a read of the saved file when it is newer than what is shown.
    ///
    /// Returns true when the rows shown are now this read's:  it replaced them,  or it is the
    /// same report the rows came from,  read again.  Only then has the shown reading been read
    /// just now,  so only then may a panel count it as read after a wake.  A read that was
    /// discarded,  such as a file that could not be read beside good rows,  returns false and
    /// leaves the rows to read as checking.
    @discardableResult
    mutating func takeSaved(_ saved: MacServicesReading) -> Bool {
        guard let shown = reading else {
            reading = saved
            source = .savedFile
            return true
        }
        // A file that could not be read does not replace good rows.
        if saved.problem != nil, shown.problem == nil { return false }
        if shown.problem != nil || Self.isNewer(saved, than: shown) {
            reading = saved
            source = .savedFile
            collectProblem = nil
            return true
        }
        // The same saved report,  read again:  the shown rows are confirmed,  and a failed
        // collection's note stays.
        if source == .savedFile, let savedAt = saved.generatedAt, savedAt == shown.generatedAt {
            reading = saved
            return true
        }
        return false
    }

    /// Takes a live collection.  A failed one keeps the rows that are shown.
    mutating func takeCollected(_ collected: MacServicesReading) {
        if collected.problem == nil, collected.generatedAt != nil {
            reading = collected
            source = .collectedLive
            collectProblem = nil
            return
        }
        guard let shown = reading, shown.problem == nil else {
            reading = collected
            source = .collectedLive
            collectProblem = nil
            return
        }
        collectProblem = collected.problem ?? "The collection gave no time this app can read."
    }

    private static func isNewer(_ left: MacServicesReading, than right: MacServicesReading) -> Bool {
        guard let leftAt = left.generatedAt else { return false }
        guard let rightAt = right.generatedAt else { return true }
        return leftAt > rightAt
    }
}

// MARK: - The readings,  aged at a moment

extension HogHunterMacReading {
    func freshness(now: Date) -> ReportFreshness {
        ReportFreshness(generatedAt: generatedAt, maxAge: TimeInterval(HogHunterMacContract.maxAgeSeconds), now: now)
    }

    /// The reading as it stands at the freshness's moment.  Ageing only takes a verdict away:
    /// past its window it reads as the reader reads a stale report,  Hog Hunter not reporting,
    /// with no rows.
    func aged(by freshness: ReportFreshness) -> HogHunterMacReading {
        var reading = self
        guard problem == nil, generatedAt != nil else { return reading }
        reading.age = freshness.ageShort
        if !freshness.verdict.isCurrent {
            reading.isCurrent = false
            reading.state = .unknown
            reading.problem = HogHunterMacReading.notReportingText
        }
        return reading
    }
}

extension MacServicesReading {
    /// The reading's freshness at a moment.
    func freshness(now: Date, readAt: Date? = nil, wokeAt: Date? = nil) -> ReportFreshness {
        ReportFreshness(generatedAt: generatedAt, maxAge: TimeInterval(MacServicesContract.maxAgeSeconds),
                        now: now, readAt: readAt, wokeAt: wokeAt)
    }

    /// The reading as it stands at the freshness's moment.  Ageing only ever takes a verdict
    /// away:  a reading that was not current when it was read is never made current.
    func aged(by freshness: ReportFreshness) -> MacServicesReading {
        var reading = self
        guard problem == nil, generatedAt != nil else { return reading }
        reading.age = freshness.ageShort
        if !freshness.verdict.isCurrent {
            reading.isCurrent = false
            reading.state = .unknown
        }
        return reading
    }
}

extension VmStatusReading {
    func freshness(now: Date, readAt: Date? = nil, wokeAt: Date? = nil) -> ReportFreshness {
        ReportFreshness(generatedAt: generatedAt, copiedAt: copiedAt,
                        maxAge: TimeInterval(VmStatusContract.maxAgeSeconds),
                        now: now, readAt: readAt, wokeAt: wokeAt)
    }

    /// The reading as it stands at the freshness's moment.  Ageing only takes a verdict away.
    func aged(by freshness: ReportFreshness) -> VmStatusReading {
        var reading = self
        guard problem == nil, generatedAt != nil else { return reading }
        reading.age = freshness.ageShort
        if !freshness.verdict.isCurrent {
            reading.isCurrent = false
            reading.state = .unknown
        }
        return reading
    }
}

extension WorkerHealthReading {
    func freshness(now: Date, readAt: Date? = nil, wokeAt: Date? = nil) -> ReportFreshness {
        ReportFreshness(generatedAt: generatedAt, maxAge: TimeInterval(WorkerHealthContract.maxAgeSeconds),
                        now: now, readAt: readAt, wokeAt: wokeAt)
    }

    /// The reading as it stands at the freshness's moment.  Ageing only takes a verdict away.
    func aged(by freshness: ReportFreshness) -> WorkerHealthReading {
        var reading = self
        guard problem == nil, generatedAt != nil else { return reading }
        reading.age = freshness.ageShort
        if !freshness.verdict.isCurrent {
            reading.isCurrent = false
            reading.state = .unknown
        }
        return reading
    }
}

extension FleetToolsReading {
    func freshness(now: Date, readAt: Date? = nil, wokeAt: Date? = nil) -> ReportFreshness {
        ReportFreshness(generatedAt: generatedAt, maxAge: TimeInterval(FleetToolsContract.maxAgeSeconds),
                        now: now, readAt: readAt, wokeAt: wokeAt)
    }

    /// The reading as it stands at the freshness's moment.  Ageing only takes a verdict away:
    /// past its window every host reads unproven and the whole report unknown,  as the reader
    /// says of a report that was already out of its window when it was read.
    func aged(by freshness: ReportFreshness) -> FleetToolsReading {
        var reading = self
        guard problem == nil, generatedAt != nil else { return reading }
        reading.age = freshness.ageShort
        let lapsed = !freshness.verdict.isCurrent
        if lapsed {
            reading.isCurrent = false
            reading.verdict = .unknown
        }
        reading.hosts = hosts.map { host in
            FleetToolsHostVerdict(
                host: host.host,
                reachable: host.reachable,
                state: lapsed ? .unproven : host.state,
                failing: host.failing,
                unknown: host.unknown,
                stale: host.stale,
                current: host.current,
                absent: host.absent,
                notableRows: host.notableRows,
                allRows: host.allRows,
                reason: host.reason,
                generatedAt: host.generatedAt,
                age: host.generatedAt.map { FleetTime.ageText(since: $0, now: freshness.now) },
                expected: host.expected,
                doctorCommit: host.doctorCommit
            )
        }
        return reading
    }
}

// MARK: - Apps & Work's header

/// How Apps & Work's collection reads as it ages.
enum FleetViewFreshness {
    /// Past this the header turns amber:  the collection is getting old.
    static let agingAfter: TimeInterval = 15 * 60
    /// Past this the header says the collection is not current.
    static let notCurrentAfter: TimeInterval = 60 * 60

    enum Tone: Equatable {
        case fresh
        case aging
        case notCurrent
    }

    static func tone(collectedAt: Date?, now: Date) -> Tone {
        guard let collectedAt else { return .notCurrent }
        let seconds = now.timeIntervalSince(collectedAt)
        if seconds > notCurrentAfter { return .notCurrent }
        return seconds > agingAfter ? .aging : .fresh
    }
}

extension FleetViewRefreshClock {
    /// The header line at a moment:  the collection's clock time and its age,  which a view
    /// asks for again on a tick,  so an open page never keeps a frozen age.
    func headerText(now: Date) -> String? {
        guard let lastAttemptAt else { return nil }
        var collected: String?
        if hasCollected {
            if let collectedAt {
                let age = FreshnessWords.ago(now.timeIntervalSince(collectedAt))
                let ending = FleetViewFreshness.tone(collectedAt: collectedAt, now: now) == .notCurrent
                    ? ", not current" : ""
                collected = "Collected \(FreshnessWords.stamp(collectedAt, now: now)), \(age)\(ending)"
            } else {
                collected = "Collection time unknown"
            }
        }
        guard lastAttemptFailed else { return collected }
        let attempt = "Last attempt \(FleetTime.clock(lastAttemptAt)) failed"
        guard let collected else { return attempt }
        return "\(attempt) \u{00B7} \(collected)"
    }
}
