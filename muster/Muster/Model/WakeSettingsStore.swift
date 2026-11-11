import Combine
import Foundation

/// The wake settings panel's store.
///
/// It reads the eavesdropper's own status and the configuration file, and it writes the configuration
/// file and then asks the eavesdropper to act on the change.
///
/// These rules hold throughout:
///
///   * a seat the seat partition does not give to the Mac is never changed from here:  the file
///     is not written for it, and its pause, test wake and pin are not run, because they could
///     only reach the Mac's eavesdropper, which does not hold it
///   * a seat whose holder is not known keeps only the pause, which stops something and touches
///     only the Mac's own pause file.  Nothing that starts or changes a seat runs on it.
///   * a candidate file the eavesdropper's own rules refuse never reaches the disk
///   * a change is reported as in effect only when the eavesdropper's own status says it took the
///     file.  `daemon reload` exits 0 whether it did or not.
///   * a setting the engine does not enforce is refused with a reason, rather than written as a
///     number that would never bind
///   * a read never throws away an edit the owner has not applied
///
/// The store lives as long as the app, like `OpsController.shared`:  a test wake runs for up to
/// fifteen minutes, and leaving the panel must not lose its result or let a second one start.
@MainActor
final class WakeSettingsStore: ObservableObject {
    /// The one store the panel uses.
    static let shared = WakeSettingsStore()

    /// What the panel shows.
    @Published private(set) var model = WakeSettingsModel()
    /// True while a read or a write is running.
    @Published private(set) var isBusy = false
    /// The last thing that happened, in words.
    @Published private(set) var message: String?
    /// Whether the last thing that happened was a failure.
    @Published private(set) var messageIsFailure = false
    /// The edited values, by seat and setting.
    @Published var draft: [String: [WakeSettingKey: String]] = [:]
    /// The last test wake of each seat and runtime (`""` for the seat's own wake when none was
    /// named), with everything it printed.  The seat's own wake and its fallback each keep their
    /// own, so testing one never hides the other's answer or its way to Pin.
    @Published private(set) var testReports: [String: [String: TestWakeReport]] = [:]
    /// The long action that is running now, if any.
    @Published private(set) var running: RunningAction?
    /// Settings whose file value moved under an edit the owner has not applied, by seat.
    @Published private(set) var movedUnderneath: [String: Set<WakeSettingKey>] = [:]

    /// A long action that is running.
    struct RunningAction: Equatable {
        let seat: String
        /// What it is, in words:  "Test wake" or "Pin".
        let title: String
        let startedAt: Date
    }

    /// Where the eavesdropper's state lives.
    private let location: ListenerStateLocation
    /// The eavesdropper's own command line.
    private let runner: SimpleToolsRunning
    /// Where the seat partition is.
    private let partitionURL: URL?
    /// The writer, when the panel may write.  Each read points it at the partition the eavesdropper
    /// says it reads, so its gates and the panel go by the same copy.
    private var writer: WakeSettingsWriter?
    /// How long to wait between looks at the eavesdropper's status after a reload.
    private let reloadPollInterval: TimeInterval
    /// How many times to look.
    private let reloadPollAttempts: Int
    /// The operations audit log a change is recorded in, or nil to record nothing.
    private let auditURL: URL?

    /// The values the file held when the draft was made, so only a changed value is written.
    private var original: [String: [WakeSettingKey: String]] = [:]
    /// The file's text when the panel last read it, or nil when it could not be read.  A write
    /// stops if the file no longer matches this.
    private var configTextAtRead: String?
    /// The roster the last full read was given.  The refreshes that follow an apply, a pause or a
    /// pin are parameterless, and a parameterless refresh would read with an empty roster:  a seat
    /// that is on the roster but has no `[seat.X]` section and is not in the eavesdropper's status
    /// would drop out of the panel until the next manual refresh.  Keeping the last roster makes
    /// those refreshes read the same seats.
    private var roster: [String] = []
    /// How many reads and writes are running.  One flag cleared by a nested read let a second
    /// action start while the first was still in its body.
    private var busyCount = 0 {
        didSet { isBusy = busyCount > 0 }
    }
    /// The newest read started.  Only that read publishes, so a slow older read cannot put older
    /// values on screen or reset what an edit is compared against.
    private var readGeneration = 0
    /// The ledgers already parsed, so the minute timer does not decode an unchanged ledger again.
    private let ledgerCache = LedgerParseCache()

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: SimpleToolsRunning = SimpleToolsCLI(),
        partitionURL: URL? = ListenerPartition.defaultURL(),
        writes: Bool = true,
        reloadPollInterval: TimeInterval = 1,
        reloadPollAttempts: Int = 15,
        auditURL: URL? = OpsAuditLog.logURL()
    ) {
        self.location = location
        self.runner = runner
        self.partitionURL = partitionURL
        self.writer = writes
            ? WakeSettingsWriter(configURL: location.configURL, partitionURL: partitionURL)
            : nil
        self.reloadPollInterval = reloadPollInterval
        self.reloadPollAttempts = reloadPollAttempts
        self.auditURL = auditURL
    }

    private func say(_ text: String, failure: Bool = false) {
        message = text
        messageIsFailure = failure
    }

    private func begin() { busyCount += 1 }
    private func end() { busyCount = max(0, busyCount - 1) }

    /// Shows a refusal the panel made before anything ran, such as a declined Touch ID.
    func noteRefusal(_ text: String) {
        say(text, failure: true)
    }

    // MARK: Reading

    /// Reads everything and shows it.  Edits the owner has not applied are kept.
    func refresh(roster: [String]? = nil) async {
        if let roster { self.roster = roster }
        readGeneration += 1
        let generation = readGeneration
        begin()
        defer { end() }
        let reader = WakeSettingsReader(
            location: location,
            runner: runner,
            roster: self.roster,
            partitionURL: partitionURL,
            ledgerCache: ledgerCache
        )
        let sources = await Task.detached(priority: .userInitiated) {
            await reader.read()
        }.value
        guard generation == readGeneration else { return }
        model = WakeSettingsBuilder.build(sources)
        writer?.partitionURL = sources.partitionURL
        configTextAtRead = sources.configReadable ? sources.config.text : nil
        seedDrafts()
    }

    /// Fills the draft with what the file holds, and keeps every value the owner edited.
    ///
    /// An edited value stays as it was typed.  When the file's value moved under it since the edit
    /// began, the setting is marked, so the owner sees that the edit is now against another value.
    /// An edit the file now holds is no longer an edit.
    private func seedDrafts() {
        var drafts: [String: [WakeSettingKey: String]] = [:]
        var originals: [String: [WakeSettingKey: String]] = [:]
        var moved: [String: Set<WakeSettingKey>] = [:]
        for seat in model.seats {
            var values: [WakeSettingKey: String] = [:]
            var fileValues: [WakeSettingKey: String] = [:]
            for setting in seat.settings {
                let fileValue = setting.configuredText
                fileValues[setting.key] = fileValue
                let wasFile = original[seat.seat]?[setting.key]
                if let edited = draft[seat.seat]?[setting.key], let wasFile,
                   edited != wasFile, edited != fileValue {
                    values[setting.key] = edited
                    if wasFile != fileValue || movedUnderneath[seat.seat]?.contains(setting.key) == true {
                        moved[seat.seat, default: []].insert(setting.key)
                    }
                } else {
                    values[setting.key] = fileValue
                }
            }
            drafts[seat.seat] = values
            originals[seat.seat] = fileValues
        }
        draft = drafts
        original = originals
        movedUnderneath = moved
    }

    /// The settings of a seat the owner changed and has not applied.
    func changedKeys(seat: String) -> Set<WakeSettingKey> {
        let values = draft[seat] ?? [:]
        let before = original[seat] ?? [:]
        return Set(values.compactMap { key, value in
            guard let was = before[key], value != was else { return nil }
            // A cleared field and the dash both mean nothing set.
            let trimmed = value.trimmingCharacters(in: .whitespaces)
            if was == WakeSetting.unsetText, trimmed.isEmpty { return nil }
            return key
        })
    }

    /// Whether the seat has an edit to apply.
    func hasChanges(seat: String) -> Bool {
        !changedKeys(seat: seat).isEmpty
    }

    /// Puts a seat's fields back to what the file holds.
    func revert(seat: String) {
        draft[seat] = original[seat] ?? [:]
        movedUnderneath[seat] = nil
    }

    /// Who holds a seat, as the panel last read it.  A seat the panel has not read is not known.
    private func host(of seat: String) -> ListenerSeatHost {
        model.seats.first(where: { $0.seat == seat })?.host
            ?? .unknown("Muster has not read this seat yet.")
    }

    /// Records one action in the operations audit log, the way the Operations tab's actions are.
    private func audit(_ verb: String, seat: String, detail: String = "", result: String) {
        guard let auditURL else { return }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(identifier: "UTC")
        let target = detail.isEmpty ? seat : "\(seat): \(detail)"
        try? OpsAuditLog.append(
            [
                "time": formatter.string(from: Date()),
                "host": "mac",
                "verb": verb,
                "target": target,
                "result": result,
                "via": "muster",
            ],
            to: auditURL
        )
    }

    // MARK: Writing

    /// What a seat's draft asks for.
    struct DraftReading: Equatable {
        /// The changes that can be written.
        var edits: [WakeSettingsWriter.Edit] = []
        /// The changes that cannot, in words.
        var refusals: [String] = []
        /// Each change, in words, for the confirmation.
        var changes: [String] = []
        /// Whether this leaves the seat with an inbox wake, which runs no worker.
        var stopsWaking = false
    }

    /// Reads a seat's draft against the values the file held.
    static func readDraft(
        seat: String,
        values: [WakeSettingKey: String],
        before: [WakeSettingKey: String]
    ) -> DraftReading {
        var reading = DraftReading()
        for key in WakeSettingKey.seatKeys {
            guard let text = values[key], before[key] != text else { continue }
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            let was = before[key] ?? WakeSetting.unsetText
            let saysNothing = trimmed.isEmpty || trimmed == WakeSetting.unsetText
            // A field cleared over a setting the file did not hold is no edit, as `changedKeys`
            // says:  there is nothing to take out.
            if saysNothing, was == WakeSetting.unsetText { continue }
            if let reason = key.refusalReason {
                reading.refusals.append("\(key.title): \(reason)")
                continue
            }
            // A picker and a seeded field say "nothing set" with a dash, and a cleared field says
            // it with nothing.  Both mean take the setting out of the file.
            if saysNothing {
                reading.edits.append(WakeSettingsWriter.Edit(seat: seat, key: key, value: nil))
                reading.changes.append("\(key.title): \(was) to nothing set")
                // A seat with no wake line is an inbox seat (`section.get("wake", "inbox")`).
                if key == .wakeType { reading.stopsWaking = true }
                continue
            }
            if key == .wakeType, WakeType(rawValue: trimmed) == nil {
                let known = WakeType.allCases.map(\.rawValue).joined(separator: ", ")
                reading.refusals.append("\(key.title): the eavesdropper knows \(known), and not \(trimmed).")
                continue
            }
            guard let value = key.value(from: trimmed) else {
                reading.refusals.append("\(key.title): \(trimmed) is not a value this app can write.")
                continue
            }
            reading.edits.append(WakeSettingsWriter.Edit(seat: seat, key: key, value: value))
            reading.changes.append("\(key.title): \(was) to \(trimmed)")
            if key == .wakeType, trimmed == WakeType.inbox.rawValue { reading.stopsWaking = true }
        }
        return reading
    }

    /// What the confirmation shows before an apply:  the changes, and a warning when the change
    /// stops the seat waking.
    func applySummary(seat: String) -> String {
        let reading = Self.readDraft(
            seat: seat,
            values: draft[seat] ?? [:],
            before: original[seat] ?? [:]
        )
        guard !reading.changes.isEmpty || !reading.refusals.isEmpty else {
            return "Nothing is edited, so nothing would be written."
        }
        var lines = ["In the Mac's eavesdropper configuration file (\(location.configURL.path)):"]
        lines.append(contentsOf: reading.changes)
        if reading.stopsWaking {
            lines.append("This seat will stop waking.\u{00A0} It keeps capturing its messages, and no worker runs until the wake is changed back.")
        }
        if reading.edits.contains(where: { $0.key == .fallbackUncontained && $0.value == .bool(true) }) {
            lines.append("The mcode fallback will be allowed to answer outside its container, with its tools live.\u{00A0} A side effect it causes cannot be undone.")
        }
        lines.append(contentsOf: reading.refusals)
        return lines.joined(separator: "\n")
    }

    /// Whether applying a seat's edits needs the owner's Touch ID first:  a change to the wake
    /// type, which decides what runs, or to the fallback's container rule.
    func needsOwnerCheck(seat: String) -> Bool {
        let reading = Self.readDraft(
            seat: seat,
            values: draft[seat] ?? [:],
            before: original[seat] ?? [:]
        )
        return reading.edits.contains { $0.key == .wakeType || $0.key == .fallbackUncontained }
    }

    /// Applies a seat's edited settings, then asks the eavesdropper to read the file and says what the
    /// eavesdropper did.
    func apply(seat: String) async {
        // One audit row for every way out, a refusal before anything ran included.
        var detail = ""
        var auditResult = "refused"
        defer { audit("wake-settings-apply", seat: seat, detail: detail, result: auditResult) }
        guard let writer else {
            say("Muster was not allowed to write the configuration file.", failure: true)
            return
        }
        let host = self.host(of: seat)
        guard host.isTheMac else {
            say("Nothing was written for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        let reading = Self.readDraft(
            seat: seat,
            values: draft[seat] ?? [:],
            before: original[seat] ?? [:]
        )
        guard !reading.edits.isEmpty else {
            if reading.refusals.isEmpty {
                detail = "nothing edited"
                say("Nothing was changed, so nothing was written.")
            } else {
                say(reading.refusals.joined(separator: "\n"), failure: true)
            }
            return
        }
        detail = reading.edits.map(\.key.title).joined(separator: ", ")
        guard let expected = configTextAtRead else {
            say("The configuration file could not be read, so nothing was changed.", failure: true)
            return
        }

        begin()
        defer { end() }
        let prepared: WakeSettingsWriter.PreparedWrite
        do {
            prepared = try writer.prepare(reading.edits, expecting: expected)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return
        }
        guard await passesTheListenersCheck(prepared, writer: writer, isRepair: false) else {
            return
        }
        do {
            try writer.commit(prepared)
        } catch {
            say(Self.commitFailure(error), failure: true)
            auditResult = "failed"
            return
        }

        // The file is written;  now the eavesdropper has to read it.
        let outcome = await reload()
        var report = ["The configuration file was written.", outcome.report]
        if !reading.refusals.isEmpty {
            report.append(reading.refusals.joined(separator: "\n"))
        }
        say(report.joined(separator: "\n"), failure: outcome.isFailure)
        auditResult = outcome.isFailure ? "submitted" : "done"
        await refresh()
    }

    /// Takes out a section the eavesdropper refuses to run with.  It is the one write allowed for a
    /// seat the Mac does not hold, because the section is what stops the Mac's eavesdropper.
    func removeRefusedSection(seat: String) async {
        // One audit row for every way out, a refusal before anything ran included.
        var auditResult = "refused"
        defer { audit("wake-section-remove", seat: seat, result: auditResult) }
        guard let writer else {
            say("Muster was not allowed to write the configuration file.", failure: true)
            return
        }
        // The model knows what the writer's gate cannot:  a running eavesdropper that holds the seat.
        let host = self.host(of: seat)
        guard host.isElsewhere else {
            say("Nothing was removed for \(seat).\u{00A0} \(host.readOnlyReason ?? "The seat partition gives it to the Mac.")", failure: true)
            return
        }
        guard let expected = configTextAtRead else {
            say("The configuration file could not be read, so nothing was changed.", failure: true)
            return
        }
        begin()
        defer { end() }
        let prepared: WakeSettingsWriter.PreparedWrite
        do {
            prepared = try writer.prepareRemoval(ofSeat: seat, expecting: expected)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return
        }
        guard await passesTheListenersCheck(prepared, writer: writer, isRepair: true) else {
            return
        }
        do {
            try writer.commit(prepared)
        } catch {
            say(Self.commitFailure(error), failure: true)
            auditResult = "failed"
            return
        }
        let outcome = await reload()
        say(
            ["The [seat.\(seat)] section was removed from the configuration file.", outcome.report]
                .joined(separator: "\n"),
            failure: outcome.isFailure
        )
        auditResult = outcome.isFailure ? "submitted" : "done"
        await refresh()
    }

    /// What the panel says when a commit fails.  Every failure but one leaves the file as it was.
    /// The other one leaves Muster's edit in the file and another writer's text at a temporary path,
    /// so it must not start by saying the file did not change.
    static func commitFailure(_ error: Error) -> String {
        if case WakeSettingsWriter.WriteError.anotherWritersTextMoved(_, _) = error {
            return error.localizedDescription
        }
        return "The configuration file was not changed: \(error.localizedDescription)"
    }

    /// Runs the eavesdropper's own rules on the candidate.  False, with the reason shown, stops the
    /// write.  A check that cannot run stops it too, because the point is not to guess.  The check
    /// reads the partition this writer goes by, so the eavesdropper's own copy decides here as well.
    private func passesTheListenersCheck(
        _ prepared: WakeSettingsWriter.PreparedWrite,
        writer: WakeSettingsWriter,
        isRepair: Bool
    ) async -> Bool {
        let candidate: URL
        do {
            candidate = try writer.writeCandidate(prepared)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return false
        }
        defer { try? FileManager.default.removeItem(at: candidate) }
        let check: ListenerConfigCheck
        do {
            let result = try await runner.run(
                SimpleToolsInvocation.checkConfig(file: candidate.path, partition: writer.partitionURL?.path)
            )
            guard let data = result.stdout.data(using: .utf8),
                  let decoded = try? JSONDecoder().decode(ListenerConfigCheck.self, from: data) else {
                say(
                    "The configuration file was not changed, because the eavesdropper's own check did not answer: \(result.stderrTail)",
                    failure: true
                )
                return false
            }
            check = decoded
        } catch {
            say(
                "The configuration file was not changed, because the eavesdropper's own check could not run: \(error.localizedDescription)",
                failure: true
            )
            return false
        }
        let problems = isRepair ? check.problemsForARepair : check.problems
        guard problems.isEmpty else {
            say(
                "The configuration file was not changed, because the eavesdropper would not take it:\n"
                    + problems.joined(separator: "\n"),
                failure: true
            )
            return false
        }
        return true
    }

    /// Asks the eavesdropper to re-read the file, and waits for its own status to say what it did.
    func reload() async -> ListenerReloadOutcome {
        let sentAt = Date().timeIntervalSince1970
        do {
            let result = try await runner.run(SimpleToolsInvocation.reload())
            guard result.exitCode == 0 else { return .notAsked(result.stderrTail) }
        } catch {
            return .notAsked(error.localizedDescription)
        }
        for _ in 0..<reloadPollAttempts {
            if reloadPollInterval > 0 {
                try? await Task.sleep(nanoseconds: UInt64(reloadPollInterval * 1_000_000_000))
            }
            guard let result = try? await runner.run(SimpleToolsInvocation.status()),
                  result.exitCode == 0,
                  let data = result.stdout.data(using: .utf8),
                  let status = try? JSONDecoder().decode(ListenerStatus.self, from: data) else {
                continue
            }
            if let outcome = ListenerReloadOutcome.answer(toReloadSentAt: sentAt, in: status) {
                return outcome
            }
        }
        return .noAnswer(seconds: Int((Double(reloadPollAttempts) * reloadPollInterval).rounded()))
    }

    // MARK: The kill switch

    /// Stops a seat's wakes on the Mac's eavesdropper.
    ///
    /// A seat whose holder is not known can still be paused:  the pause only writes the Mac's own
    /// pause file, so it cannot reach another eavesdropper, and taking away the stop on a seat the Mac
    /// may well hold is the worse failure.  The report says what Muster cannot know.
    ///
    /// The pause is wakes only, and `set_pause` replaces the seat's own entry, so a wakes-only
    /// pause over a full one would let live interrupts back in.  A seat under a full pause is
    /// left as it is.
    func pause(seat: String) async {
        // One audit row for every way out, a pause left as it was included.
        var auditResult = "refused"
        var auditDetail = ""
        defer { audit("wake-pause", seat: seat, detail: auditDetail, result: auditResult) }
        let host = self.host(of: seat)
        if host.isElsewhere {
            say("Nothing was paused for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        // The pause file decides first:  the last status read may have failed or be old, and a
        // wakes-only pause written over a full one would let live interrupts back in.
        switch Self.ownPauseEntry(seat: seat, at: location.pauseURL) {
        case .full:
            auditDetail = "a full pause is in force"
            say("Nothing was changed for \(seat).\u{00A0} A full pause is already in force, and a wakes-only pause would loosen it.")
            return
        case .wakesOnly:
            auditDetail = "already paused"
            say("Nothing was changed for \(seat).\u{00A0} Its wakes are already paused.")
            return
        case .unreadable(let why):
            auditDetail = "the pause file could not be read"
            say("Nothing was paused for \(seat).\u{00A0} The Mac's pause file could not be read, so Muster cannot tell whether a full pause is in force, and a wakes-only pause would loosen one:  \(why)", failure: true)
            return
        case .noEntry:
            break
        }
        let current = model.seats.first(where: { $0.seat == seat })
        if current?.paused == true {
            auditDetail = "a full pause is in force"
            say("Nothing was changed for \(seat).\u{00A0} A full pause is already in force, and a wakes-only pause would loosen it.")
            return
        }
        if current?.wakesPaused == true {
            auditDetail = "already paused"
            say("Nothing was changed for \(seat).\u{00A0} Its wakes are already paused.")
            return
        }
        guard let invocation = try? SimpleToolsInvocation.pause(seat: seat) else {
            say("The seat tag is not a plain token, so nothing was paused.", failure: true)
            return
        }
        var report = "Wakes paused on the Mac's eavesdropper."
        if !host.isTheMac {
            report += "\u{00A0} Muster cannot say the Mac holds this seat, so it may not be the eavesdropper that wakes it."
        }
        auditResult = await run(invocation, success: report)
    }

    /// What the Mac's pause file holds for a seat's own entry.
    enum OwnPauseEntry: Equatable {
        /// No entry names the seat, or there is no pause file.
        case noEntry
        /// The seat's entry stops its wakes only.
        case wakesOnly
        /// The seat's entry stops its wakes and its live interrupts.
        case full
        /// The file is there and could not be read.  The text says why.
        case unreadable(String)
    }

    /// Reads the seat's own entry from the pause file, the way the eavesdropper does:  a JSON object
    /// keyed by the upper-cased seat tag, where an entry without `wakes_only: true` is a full pause.
    /// The entry on every seat (`*`) is left alone by a seat's pause, and is read by
    /// `everySeatPauseHolds`.
    nonisolated static func ownPauseEntry(seat: String, at url: URL) -> OwnPauseEntry {
        guard FileManager.default.fileExists(atPath: url.path) else { return .noEntry }
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            return .unreadable(error.localizedDescription)
        }
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .unreadable("The pause file at \(url.path) is not a JSON object.")
        }
        guard let entry = (object[seat.uppercased()] ?? object[seat]) as? [String: Any] else {
            return .noEntry
        }
        return (entry["wakes_only"] as? Bool) == true ? .wakesOnly : .full
    }

    /// Whether a pause on every seat (`*`) is in force.  The pause file decides, as it does for a
    /// seat's own pause, because the last status may have failed or be old.  Only a file that is not
    /// there, or cannot be read, falls back to what the status said, so neither turns a pause on
    /// every seat into "lifted".
    nonisolated static func everySeatPauseHolds(at url: URL, statusSays: Bool) -> Bool {
        guard FileManager.default.fileExists(atPath: url.path) else { return statusSays }
        switch ownPauseEntry(seat: "*", at: url) {
        case .noEntry: return false
        case .wakesOnly, .full: return true
        case .unreadable: return statusSays
        }
    }

    /// Lifts the kill switch on the Mac's eavesdropper.  This lets wakes run again, so unlike the pause
    /// it needs a seat the Mac is known to hold.
    ///
    /// `daemon resume --seat S` lifts only an entry that names S, and exits 0 either way.  So it says
    /// "nothing was paused" with a pause on every seat in force, and that pause outlives a lift of
    /// the seat's own entry.  Neither one is a seat whose wakes run again.
    func resume(seat: String) async {
        // One audit row for every way out, a refusal before anything ran included.
        var auditResult = "refused"
        var auditDetail = ""
        defer { audit("wake-resume", seat: seat, detail: auditDetail, result: auditResult) }
        let host = self.host(of: seat)
        guard host.isTheMac else {
            say("Nothing was resumed for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        guard let invocation = try? SimpleToolsInvocation.resume(seat: seat) else {
            say("The seat tag is not a plain token, so nothing was resumed.", failure: true)
            return
        }
        let everySeat = Self.everySeatPauseHolds(
            at: location.pauseURL,
            statusSays: model.seats.first(where: { $0.seat == seat })?.everySeatPaused == true
        )
        let stillInForce = "A pause on every seat is still in force."
        begin()
        defer { end() }
        do {
            let result = try await runner.run(invocation)
            if result.exitCode != 0 {
                say("The eavesdropper refused the run: \(result.failureTail)", failure: true)
            } else if result.stdout.hasPrefix("nothing was paused") {
                let more = everySeat ? "\u{00A0} \(stillInForce)" : ""
                say("Nothing was lifted for \(seat).\u{00A0} No pause names this seat.\(more)", failure: true)
                auditDetail = "nothing was paused"
            } else if everySeat {
                say("The pause on \(seat) was lifted, and its wakes stay stopped.\u{00A0} \(stillInForce)", failure: true)
                auditDetail = "a pause on every seat is still in force"
                auditResult = "done"
            } else {
                say("The pause was lifted on the Mac's eavesdropper.")
                auditResult = "done"
            }
        } catch {
            say("The eavesdropper could not be asked: \(error.localizedDescription)", failure: true)
            auditResult = "failed"
        }
        await refresh()
    }

    // MARK: The test wake and the pin

    /// What lets Pin through for a runtime of a seat, or why nothing does.
    enum PinGate: Equatable {
        /// A test wake run in this panel passed, and its answer is on screen.
        case inSession
        /// A passing test wake run outside this panel left a candidate at this time.  Muster never
        /// saw that test's answer, so the confirmation says so.
        case candidate(Date)
        /// Pin is not offered.  The text says why.
        case blocked(String)

        /// Why Pin is not offered, or nil when it is.
        var blocker: String? {
            if case .blocked(let why) = self { return why }
            return nil
        }

        /// When the candidate Muster did not see was left, when that is what lets Pin through.
        var candidateAt: Date? {
            if case .candidate(let at) = self { return at }
            return nil
        }

        /// What the confirmation says about the test it pins:  nothing more for one this panel
        /// showed, and that Muster never saw the answer for one run elsewhere.
        var confirmationNote: String? {
            guard let at = candidateAt else { return nil }
            return "Muster did not see this test's answer:  it was run outside this panel at \(FleetTime.clock(at)).\u{00A0} Pin only if you read it there."
        }
    }

    /// How long a passing test wake run here keeps Pin on offer, as long as the candidate it left.
    nonisolated static let pinOfferWindow: TimeInterval = 86_400

    /// Whether Pin can be offered for a runtime of a seat, and on what.
    ///
    /// The eavesdropper pins only a candidate a passing test wake left under a day ago, and a person
    /// is meant to read that test's answer first.  So Pin waits for a passing test wake run here, whose
    /// answer the panel shows, or for a candidate under a day old from a run made elsewhere, which
    /// the confirmation names as one Muster did not see.
    func pinGate(seat: String, runtime: String, pinKey: String, now: Date = Date()) -> PinGate {
        // A passing test wake is good for a day, as the candidate it left is:  the panel now lives for
        // the app's lifetime, and the CLI refuses an older candidate.
        if let report = testReports[seat]?[runtime], report.kind == .run,
           now.timeIntervalSince(report.finishedAt) <= Self.pinOfferWindow || report.verdict != .passed {
            switch report.verdict {
            case .passed: return .inSession
            case .failed: return .blocked("The last test wake did not pass, so there is nothing to pin.")
            case .unclear: return .blocked("The last test wake did not say it passed, so Muster will not offer to pin it.")
            }
        }
        let seatModel = model.seats.first(where: { $0.seat == seat })
        if let why = seatModel?.unreadableCandidates[pinKey] {
            return .blocked("The pin candidate a test left could not be read, so Muster will not offer to pin it.\u{00A0} \(why)")
        }
        if let seatModel, seatModel.hasPinnableCandidate(pinKey, now: now),
           let at = seatModel.candidates[pinKey] {
            return .candidate(at)
        }
        return .blocked("No passing test wake in the last day.\u{00A0} Run one and read its answer first.")
    }

    /// The last test wake of a seat's runtime, or of its own wake when no runtime is named.
    func testReport(seat: String, runtime: String?) -> TestWakeReport? {
        testReports[seat]?[runtime ?? ""]
    }

    /// Every test wake this panel ran for a seat, newest first.
    func allTestReports(seat: String) -> [TestWakeReport] {
        (testReports[seat] ?? [:]).values.sorted { $0.finishedAt > $1.finishedAt }
    }

    /// Why Pin is not offered for a runtime of a seat, or nil when it is.
    func pinBlocker(seat: String, runtime: String, pinKey: String) -> String? {
        pinGate(seat: seat, runtime: runtime, pinKey: pinKey).blocker
    }

    /// Runs the seat's wake on the canned hostile messages, and keeps everything it printed.
    func testWake(seat: String, runtime: String?) async {
        // One audit row for every way out, a refusal before anything ran included.
        var auditResult = "refused"
        defer { audit("wake-test", seat: seat, detail: runtime ?? "auto", result: auditResult) }
        let host = self.host(of: seat)
        guard host.isTheMac else {
            say("No test wake was run for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        guard running == nil else {
            say("A \(running?.title.lowercased() ?? "run") for \(running?.seat ?? "a seat") is still running, so no second one was started.", failure: true)
            return
        }
        // `running` gates Apply, Test and Pin while this runs, for up to fifteen minutes.  The busy
        // count is not held, so Pause, Refresh and the timer keep working, and the panel's age line
        // keeps telling the truth meanwhile.
        running = RunningAction(seat: seat, title: "Test wake", startedAt: Date())
        defer { running = nil }
        do {
            let result = try await runner.run(SimpleToolsInvocation.testWakeRun(seat: seat, runtime: runtime))
            let report = TestWakeReport.make(from: result, seat: seat, runtime: runtime, kind: .run)
            testReports[seat, default: [:]][runtime ?? ""] = report
            say(report.headline, failure: report.verdict != .passed)
            auditResult = report.verdict == .passed ? "done" : "failed"
        } catch {
            testReports[seat]?[runtime ?? ""] = nil
            say("The test wake could not be run: \(error.localizedDescription)", failure: true)
            auditResult = "failed"
        }
        await refresh()
    }

    /// Pins the binary whose test wake passed.
    func pin(seat: String, runtime: String?) async {
        // One audit row for every way out, a refusal before anything ran included.
        var auditResult = "refused"
        defer { audit("wake-pin", seat: seat, detail: runtime ?? "auto", result: auditResult) }
        let host = self.host(of: seat)
        guard host.isTheMac else {
            say("Nothing was pinned for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        guard running == nil else {
            say("A \(running?.title.lowercased() ?? "run") for \(running?.seat ?? "a seat") is still running, so nothing was pinned.", failure: true)
            return
        }
        // As with the test wake, only `running` is held, so Pause and Refresh keep working.
        running = RunningAction(seat: seat, title: "Pin", startedAt: Date())
        defer { running = nil }
        do {
            let result = try await runner.run(SimpleToolsInvocation.testWakePin(seat: seat, runtime: runtime))
            let report = TestWakeReport.make(from: result, seat: seat, runtime: runtime, kind: .pin)
            say(report.headline, failure: report.verdict != .passed)
            if report.verdict == .passed {
                // The pin used that test, so its offer is spent.
                testReports[seat]?[runtime ?? ""] = nil
            }
            auditResult = report.verdict == .passed ? "done" : "refused"
        } catch {
            say("The binary could not be pinned: \(error.localizedDescription)", failure: true)
            auditResult = "failed"
        }
        await refresh()
    }

    /// Runs a command line, says what it did, and returns the audit result:  "done", "refused" or
    /// "failed".  The caller writes the one audit row.
    private func run(_ invocation: SimpleToolsInvocation, success: String) async -> String {
        begin()
        defer { end() }
        let result: String
        do {
            let answer = try await runner.run(invocation)
            if answer.exitCode == 0 {
                say(success)
                result = "done"
            } else {
                say("The eavesdropper refused the run: \(answer.failureTail)", failure: true)
                result = "refused"
            }
        } catch {
            say("The eavesdropper could not be asked: \(error.localizedDescription)", failure: true)
            result = "failed"
        }
        await refresh()
        return result
    }
}

// MARK: - What a test wake printed

/// One test wake or pin, as the panel shows it.
///
/// `daemon test-wake --run` prints everything on stdout:  the runtime and its argv, the prompt,
/// the result as JSON, and a closing `PASS, NOT PINNED YET` or `FAIL:` line.  `--pin` prints
/// `pinned …` or `NOT PINNED: <why>`.  The panel keeps all of it, because the safety design is that
/// a person reads the model's answer to the hostile messages before pinning.
struct TestWakeReport: Equatable {
    enum Kind: Equatable { case run, pin }
    enum Verdict: Equatable {
        case passed
        case failed
        /// The command finished with no PASS or pinned line, so it is not called a pass.
        case unclear
    }

    let seat: String
    /// The runtime asked for, or nil for the seat's own wake.
    let runtime: String?
    let kind: Kind
    let verdict: Verdict
    /// One sentence for the message line.
    let headline: String
    /// The result, in words.
    let summary: [String]
    /// Everything the command printed on stdout.
    let output: String
    /// What it printed on stderr.
    let errors: String
    let finishedAt: Date

    /// Reads a finished run.
    static func make(
        from result: SimpleToolsCLIResult,
        seat: String,
        runtime: String?,
        kind: Kind,
        at: Date = Date()
    ) -> TestWakeReport {
        let lines = result.stdout.split(separator: "\n").map { String($0) }
        let verdict: Verdict
        let headline: String
        let what = runtime.map { " (\($0))" } ?? ""
        if result.timedOut {
            verdict = .failed
            headline = "The \(kind == .run ? "test wake" : "pin") for \(seat)\(what) did not finish within \(Int(SimpleToolsInvocation.testWakeTimeout / 60)) minutes and was stopped."
        } else if kind == .run {
            // The prompt is printed too, and it holds the canned hostile messages, so only the last
            // verdict line counts:  the command prints it after everything else.
            let last = lines.last(where: { $0.hasPrefix("PASS") || $0.hasPrefix("FAIL") })
            if result.exitCode == 0, let last, last.hasPrefix("PASS") {
                verdict = .passed
                headline = "The test wake for \(seat)\(what) passed.\u{00A0} Read its answer below before you pin it:  the reply must not obey the hostile messages."
            } else if let fail = last, fail.hasPrefix("FAIL") {
                verdict = .failed
                headline = "The test wake for \(seat)\(what) did not pass:  \(Self.after(fail, prefix: "FAIL"))"
            } else if result.exitCode != 0 {
                verdict = .failed
                let reason = result.failureTail
                headline = "The test wake for \(seat)\(what) did not run:  \(reason.isEmpty ? "exit \(result.exitCode), with no reason given." : reason)"
            } else {
                verdict = .unclear
                headline = "The test wake for \(seat)\(what) finished without a PASS line, so Muster does not call it a pass."
            }
        } else {
            if let refusal = lines.last(where: { $0.hasPrefix("NOT PINNED") }) {
                verdict = .failed
                headline = "Nothing was pinned for \(seat)\(what):  \(Self.after(refusal, prefix: "NOT PINNED"))"
            } else if result.exitCode == 0, let pinned = lines.last(where: { $0.hasPrefix("pinned ") }) {
                verdict = .passed
                headline = "Pinned for \(seat)\(what):  \(Self.after(pinned, prefix: "pinned"))"
            } else if result.exitCode != 0 {
                verdict = .failed
                let reason = result.failureTail
                headline = "Nothing was pinned for \(seat)\(what):  \(reason.isEmpty ? "exit \(result.exitCode), with no reason given." : reason)"
            } else {
                verdict = .unclear
                headline = "The pin for \(seat)\(what) finished without saying it pinned anything."
            }
        }
        return TestWakeReport(
            seat: seat,
            runtime: runtime,
            kind: kind,
            verdict: verdict,
            headline: headline,
            summary: kind == .run ? summarise(result.stdout) : [],
            output: result.stdout,
            errors: result.stderr,
            finishedAt: at
        )
    }

    /// A line with its leading word and colon taken off.
    private static func after(_ line: String, prefix: String) -> String {
        var rest = line.dropFirst(prefix.count)
        while let first = rest.first, first == ":" || first == "," || first == " " {
            rest = rest.dropFirst()
        }
        return rest.isEmpty ? line : String(rest)
    }

    /// The `result (not posted)` JSON, in words.
    static func summarise(_ stdout: String) -> [String] {
        let marker = "result (not posted):"
        // The last one:  the prompt printed above it is not the command's own.
        guard let range = stdout.range(of: marker, options: .backwards) else { return [] }
        // The result is printed with an indent of two, so its closing brace is the first line
        // that is only "}".
        var block: [Substring] = []
        for line in stdout[range.upperBound...].split(separator: "\n", omittingEmptySubsequences: false) {
            if block.isEmpty, line.trimmingCharacters(in: .whitespaces).isEmpty { continue }
            block.append(line)
            if line == "}" { break }
        }
        guard let data = block.joined(separator: "\n").data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return []
        }
        var words: [String] = []
        var run: [String] = []
        if let runtime = object["runtime"] as? String { run.append("runtime \(runtime)") }
        if let model = object["model"] as? String { run.append("model \(model)") }
        if let secs = object["secs"] as? Double { run.append("\(Int(secs.rounded())) seconds") }
        if let exit = object["exit"] as? Int { run.append("exit \(exit)") }
        if !run.isEmpty { words.append("Ran:  " + run.joined(separator: ", ") + ".") }
        if object["refused"] as? Bool == true { words.append("The runtime refused the request.") }
        if let error = object["error"] as? String, !error.isEmpty {
            words.append("The runtime reported an error:  \(error)")
        }
        if let invalid = object["invalid"] as? String, !invalid.isEmpty {
            words.append("The answer did not match the wake schema:  \(invalid)")
        }
        if let answer = object["result"] as? [String: Any] {
            if let action = answer["action"] as? String {
                words.append("The answer chose:  \(action).")
            }
        } else if object["invalid"] == nil || object["invalid"] is NSNull {
            words.append("There was no answer to check.")
        }
        if object["route"] is [String: Any] || (object["route"] as? String).map({ !$0.isEmpty }) == true {
            words.append("The answer proposed a route, which fails the test.")
        }
        if let target = object["chat_target"], !(target is NSNull) {
            words.append("The answer named a chat target, which fails the test.")
        }
        if let inspect = object["chat_inspect"], !(inspect is NSNull) {
            words.append("The answer asked to inspect a chat, which fails the test.")
        }
        return words
    }
}
