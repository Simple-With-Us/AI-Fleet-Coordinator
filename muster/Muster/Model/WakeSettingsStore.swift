import Combine
import Foundation

/// The wake settings panel's store.
///
/// It reads the listener's own status and the configuration file, and it writes the configuration
/// file and then asks the listener to act on the change.
///
/// These rules hold throughout:
///
///   * a seat the seat partition does not give to the Mac is never changed from here:  the file
///     is not written for it, and its pause, test wake and pin are not run, because they could
///     only reach the Mac's listener, which does not hold it
///   * a candidate file the listener's own rules refuse never reaches the disk
///   * a change is reported as in effect only when the listener's own status says it took the
///     file.  `daemon reload` exits 0 whether it did or not.
///   * a setting the engine does not enforce is refused with a reason, rather than written as a
///     number that would never bind
@MainActor
final class WakeSettingsStore: ObservableObject {
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

    /// Where the listener's state lives.
    private let location: ListenerStateLocation
    /// The listener's own command line.
    private let runner: AgentSyncRunning
    /// Where the seat partition is.
    private let partitionURL: URL?
    /// The writer, when the panel may write.
    private let writer: WakeSettingsWriter?
    /// How long to wait between looks at the listener's status after a reload.
    private let reloadPollInterval: TimeInterval
    /// How many times to look.
    private let reloadPollAttempts: Int

    /// The values the file held when the draft was made, so only a changed value is written.
    private var original: [String: [WakeSettingKey: String]] = [:]
    /// The file's text when the panel last read it, or nil when it could not be read.  A write
    /// stops if the file no longer matches this.
    private var configTextAtRead: String?
    /// The roster the last full read was given.  The refreshes that follow an apply, a pause or a
    /// pin are parameterless, and a parameterless refresh would read with an empty roster:  a seat
    /// that is on the roster but has no `[seat.X]` section and is not in the listener's status
    /// would drop out of the panel until the next manual refresh.  Keeping the last roster makes
    /// those refreshes read the same seats.
    private var roster: [String] = []

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: AgentSyncRunning = AgentSyncCLI(),
        partitionURL: URL? = ListenerPartition.defaultURL(),
        writes: Bool = true,
        reloadPollInterval: TimeInterval = 1,
        reloadPollAttempts: Int = 15
    ) {
        self.location = location
        self.runner = runner
        self.partitionURL = partitionURL
        self.writer = writes
            ? WakeSettingsWriter(configURL: location.configURL, partitionURL: partitionURL)
            : nil
        self.reloadPollInterval = reloadPollInterval
        self.reloadPollAttempts = reloadPollAttempts
    }

    private func say(_ text: String, failure: Bool = false) {
        message = text
        messageIsFailure = failure
    }

    // MARK: Reading

    /// Reads everything and shows it.
    func refresh(roster: [String]? = nil) async {
        if let roster { self.roster = roster }
        isBusy = true
        defer { isBusy = false }
        let reader = WakeSettingsReader(
            location: location,
            runner: runner,
            roster: self.roster,
            partitionURL: partitionURL
        )
        let sources = await Task.detached(priority: .userInitiated) {
            await reader.read()
        }.value
        model = WakeSettingsBuilder.build(sources)
        configTextAtRead = sources.configReadable ? sources.config.text : nil
        seedDrafts()
    }

    /// Fills the draft with what the file holds.
    private func seedDrafts() {
        var drafts: [String: [WakeSettingKey: String]] = [:]
        for seat in model.seats {
            var values: [WakeSettingKey: String] = [:]
            for setting in seat.settings {
                values[setting.key] = setting.configuredText
            }
            drafts[seat.seat] = values
        }
        draft = drafts
        original = drafts
    }

    /// Who holds a seat, as the panel last read it.  A seat the panel has not read is not known.
    private func host(of seat: String) -> ListenerSeatHost {
        model.seats.first(where: { $0.seat == seat })?.host
            ?? .unknown("Muster has not read this seat yet.")
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
            if let reason = key.refusalReason {
                reading.refusals.append("\(key.title): \(reason)")
                continue
            }
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            let was = before[key] ?? WakeSetting.unsetText
            // A picker and a seeded field say "nothing set" with a dash, and a cleared field says
            // it with nothing.  Both mean take the setting out of the file.
            if trimmed.isEmpty || trimmed == WakeSetting.unsetText {
                reading.edits.append(WakeSettingsWriter.Edit(seat: seat, key: key, value: nil))
                reading.changes.append("\(key.title): \(was) to nothing set")
                // A seat with no wake line is an inbox seat (`section.get("wake", "inbox")`).
                if key == .wakeType { reading.stopsWaking = true }
                continue
            }
            if key == .wakeType, WakeType(rawValue: trimmed) == nil {
                let known = WakeType.allCases.map(\.rawValue).joined(separator: ", ")
                reading.refusals.append("\(key.title): the listener knows \(known), and not \(trimmed).")
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
        var lines = reading.changes
        if reading.stopsWaking {
            lines.append("This seat will stop waking.\u{00A0} It keeps capturing its messages, and no worker runs until the wake is changed back.")
        }
        lines.append(contentsOf: reading.refusals)
        return lines.joined(separator: "\n")
    }

    /// Applies a seat's edited settings, then asks the listener to read the file and says what the
    /// listener did.
    func apply(seat: String) async {
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
                say("Nothing was changed, so nothing was written.")
            } else {
                say(reading.refusals.joined(separator: "\n"), failure: true)
            }
            return
        }
        guard let expected = configTextAtRead else {
            say("The configuration file could not be read, so nothing was changed.", failure: true)
            return
        }

        isBusy = true
        defer { isBusy = false }
        let prepared: WakeSettingsWriter.PreparedWrite
        do {
            prepared = try writer.prepare(reading.edits, expecting: expected)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return
        }
        guard await passesTheListenersCheck(prepared, writer: writer, isRepair: false) else { return }
        do {
            try writer.commit(prepared)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return
        }

        // The file is written;  now the listener has to read it.
        let outcome = await reload()
        var report = ["The configuration file was written.", outcome.report]
        if reading.edits.contains(where: { $0.key.isBudget || $0.key.isLiveCap }) {
            report.append("A reload does not re-read a seat's budget, so the listener must be restarted before the new cap takes effect.")
        }
        if !reading.refusals.isEmpty {
            report.append(reading.refusals.joined(separator: "\n"))
        }
        say(report.joined(separator: "\n"), failure: outcome.isFailure)
        await refresh()
    }

    /// Takes out a section the listener refuses to run with.  It is the one write allowed for a
    /// seat the Mac does not hold, because the section is what stops the Mac's listener.
    func removeRefusedSection(seat: String) async {
        guard let writer else {
            say("Muster was not allowed to write the configuration file.", failure: true)
            return
        }
        guard let expected = configTextAtRead else {
            say("The configuration file could not be read, so nothing was changed.", failure: true)
            return
        }
        isBusy = true
        defer { isBusy = false }
        let prepared: WakeSettingsWriter.PreparedWrite
        do {
            prepared = try writer.prepareRemoval(ofSeat: seat, expecting: expected)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return
        }
        guard await passesTheListenersCheck(prepared, writer: writer, isRepair: true) else { return }
        do {
            try writer.commit(prepared)
        } catch {
            say("The configuration file was not changed: \(error.localizedDescription)", failure: true)
            return
        }
        let outcome = await reload()
        say(
            ["The [seat.\(seat)] section was removed from the configuration file.", outcome.report]
                .joined(separator: "\n"),
            failure: outcome.isFailure
        )
        await refresh()
    }

    /// Runs the listener's own rules on the candidate.  False, with the reason shown, stops the
    /// write.  A check that cannot run stops it too, because the point is not to guess.
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
            let result = try await runner.run(AgentSyncInvocation.checkConfig(file: candidate.path))
            guard let data = result.stdout.data(using: .utf8),
                  let decoded = try? JSONDecoder().decode(ListenerConfigCheck.self, from: data) else {
                say(
                    "The configuration file was not changed, because the listener's own check did not answer: \(result.stderrTail)",
                    failure: true
                )
                return false
            }
            check = decoded
        } catch {
            say(
                "The configuration file was not changed, because the listener's own check could not run: \(error.localizedDescription)",
                failure: true
            )
            return false
        }
        let problems = isRepair ? check.problemsForARepair : check.problems
        guard problems.isEmpty else {
            say(
                "The configuration file was not changed, because the listener would not take it:\n"
                    + problems.joined(separator: "\n"),
                failure: true
            )
            return false
        }
        return true
    }

    /// Asks the listener to re-read the file, and waits for its own status to say what it did.
    func reload() async -> ListenerReloadOutcome {
        let sentAt = Date().timeIntervalSince1970
        do {
            let result = try await runner.run(AgentSyncInvocation.reload())
            guard result.exitCode == 0 else { return .notAsked(result.stderrTail) }
        } catch {
            return .notAsked(error.localizedDescription)
        }
        for _ in 0..<reloadPollAttempts {
            if reloadPollInterval > 0 {
                try? await Task.sleep(nanoseconds: UInt64(reloadPollInterval * 1_000_000_000))
            }
            guard let result = try? await runner.run(AgentSyncInvocation.status()),
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

    /// Whether a seat's kill switch can do anything here, and the refusal when it cannot.
    ///
    /// A seat whose holder is not known keeps the kill switch:  the pause only writes the Mac's own
    /// pause file, and taking away the stop on a seat the Mac may well hold is the worse failure.
    private func killSwitchRefusal(seat: String, doing: String) -> String? {
        let host = self.host(of: seat)
        guard host.isElsewhere else { return nil }
        return "Nothing was \(doing) for \(seat).\u{00A0} \(host.readOnlyReason ?? "")"
    }

    /// Stops a seat's wakes on the Mac's listener.
    func pause(seat: String) async {
        if let refusal = killSwitchRefusal(seat: seat, doing: "paused") {
            say(refusal, failure: true)
            return
        }
        guard let invocation = try? AgentSyncInvocation.pause(seat: seat) else {
            say("The seat tag is not a plain token, so nothing was paused.", failure: true)
            return
        }
        await run(invocation, success: killSwitchReport("Wakes paused on the Mac's listener.", seat: seat))
    }

    /// Lifts the kill switch on the Mac's listener.
    func resume(seat: String) async {
        if let refusal = killSwitchRefusal(seat: seat, doing: "resumed") {
            say(refusal, failure: true)
            return
        }
        guard let invocation = try? AgentSyncInvocation.resume(seat: seat) else {
            say("The seat tag is not a plain token, so nothing was resumed.", failure: true)
            return
        }
        await run(invocation, success: killSwitchReport("The pause was lifted on the Mac's listener.", seat: seat))
    }

    /// The report for a kill switch that ran, saying so when the holder of the seat is not known.
    private func killSwitchReport(_ text: String, seat: String) -> String {
        guard !host(of: seat).isTheMac else { return text }
        return text + "\u{00A0} The seat partition could not be read, so Muster cannot say the Mac holds this seat."
    }

    // MARK: The pin

    /// Runs the seat's wake for real, so a changed setting is proved by a wake that ran.
    func testWake(seat: String, runtime: String?) async {
        let host = self.host(of: seat)
        guard host.isTheMac else {
            say("No test wake was run for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let result = try await runner.run(AgentSyncInvocation.testWakeRun(seat: seat, runtime: runtime))
            if result.exitCode == 0 {
                say("The test wake ran.\u{00A0} Read its result before you pin it.")
            } else {
                say("The test wake did not pass: \(result.stderrTail)", failure: true)
            }
        } catch {
            say("The test wake could not be run: \(error.localizedDescription)", failure: true)
        }
    }

    /// Pins the binary whose test wake passed.
    func pin(seat: String, runtime: String?) async {
        let host = self.host(of: seat)
        guard host.isTheMac else {
            say("Nothing was pinned for \(seat).\u{00A0} \(host.readOnlyReason ?? "")", failure: true)
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let result = try await runner.run(AgentSyncInvocation.testWakePin(seat: seat, runtime: runtime))
            if result.exitCode == 0 {
                say("The binary was pinned.\u{00A0} A failed run never pins.")
            } else {
                say("The binary was not pinned: \(result.stderrTail)", failure: true)
            }
        } catch {
            say("The binary could not be pinned: \(error.localizedDescription)", failure: true)
        }
        await refresh()
    }

    /// Runs a command line and says what it did.
    private func run(_ invocation: AgentSyncInvocation, success: String) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let result = try await runner.run(invocation)
            if result.exitCode == 0 {
                say(success)
            } else {
                say("The listener refused the run: \(result.stderrTail)", failure: true)
            }
        } catch {
            say("The listener could not be asked: \(error.localizedDescription)", failure: true)
        }
        await refresh()
    }
}
