import Combine
import Foundation

/// The wake settings panel's store.
///
/// It reads the listener's own status and the configuration file,  and it writes the
/// configuration file and then asks the listener to act on the change.
///
/// Two rules hold throughout:
///
///   * a change is never reported as done until the listener has been asked to read the
///     file,  because a file the listener has not read is not a change
///   * a setting the engine does not enforce is refused with a reason,  rather than
///     written as a number that would never bind
@MainActor
final class WakeSettingsStore: ObservableObject {
    /// What the panel shows.
    @Published private(set) var model = WakeSettingsModel()
    /// True while a read or a write is running.
    @Published private(set) var isBusy = false
    /// The last thing that happened,  in words.
    @Published private(set) var message: String?
    /// The edited values,  by seat and setting.
    @Published var draft: [String: [WakeSettingKey: String]] = [:]

    /// Where the listener's state lives.
    private let location: ListenerStateLocation
    /// The listener's own command line.
    private let runner: AgentSyncRunning
    /// The writer,  when the panel may write.
    private let writer: WakeSettingsWriter?

    /// The values the file held when the draft was made,  so only a changed value is
    /// written.
    private var original: [String: [WakeSettingKey: String]] = [:]
    /// The roster the last full read was given.  The refreshes that follow an apply,  a
    /// pause or a pin are parameterless,  and a parameterless refresh would read with an
    /// empty roster:  a seat that is on the roster but has no `[seat.X]` section and is not
    /// in the listener's status would drop out of the panel until the next manual refresh.
    /// Keeping the last roster makes those refreshes read the same seats.
    private var roster: [String] = []

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: AgentSyncRunning = AgentSyncCLI(),
        writes: Bool = true
    ) {
        self.location = location
        self.runner = runner
        self.writer = writes ? WakeSettingsWriter(configURL: location.configURL) : nil
    }

    // MARK: Reading

    /// Reads everything and shows it.
    func refresh(roster: [String]? = nil) async {
        if let roster { self.roster = roster }
        isBusy = true
        defer { isBusy = false }
        let reader = WakeSettingsReader(location: location, runner: runner, roster: self.roster)
        let sources = await Task.detached(priority: .userInitiated) {
            await reader.read()
        }.value
        model = WakeSettingsBuilder.build(sources)
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

    // MARK: Writing

    /// Applies a seat's edited settings,  then asks the listener to read the file.
    func apply(seat: String) async {
        guard let writer else {
            message = "Muster was not allowed to write the configuration file."
            return
        }
        let values = draft[seat] ?? [:]
        let before = original[seat] ?? [:]
        var edits: [WakeSettingsWriter.Edit] = []
        var refusals: [String] = []

        for key in WakeSettingKey.seatKeys {
            guard let text = values[key] else { continue }
            let changed = before[key] != text
            guard changed else { continue }
            if let reason = key.refusalReason {
                refusals.append("\(key.title):  \(reason)")
                continue
            }
            if key == .wakeType {
                let trimmed = text.trimmingCharacters(in: .whitespaces)
                guard WakeType(rawValue: trimmed) != nil else {
                    refusals.append("\(key.title):  the listener knows inbox, claude, http and muse,  and not \(trimmed).")
                    continue
                }
            }
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty {
                edits.append(WakeSettingsWriter.Edit(seat: seat, key: key, value: nil))
                continue
            }
            guard let value = key.value(from: trimmed) else {
                refusals.append("\(key.title):  \(trimmed) is not a value this app can write.")
                continue
            }
            edits.append(WakeSettingsWriter.Edit(seat: seat, key: key, value: value))
        }

        guard !edits.isEmpty else {
            message = refusals.isEmpty
                ? "Nothing was changed,  so nothing was written."
                : refusals.joined(separator: "\n")
            return
        }

        isBusy = true
        defer { isBusy = false }
        do {
            try writer.apply(edits)
        } catch {
            message = "The configuration file was not changed:  \(error.localizedDescription)"
            return
        }

        // The file is written;  now the listener has to read it.
        let reloaded = await reload()
        var report: [String] = ["The configuration file was written."]
        report.append(reloaded
            ? "The listener was asked to re-read it."
            : "The listener could not be asked to re-read it,  so the change is not in effect yet.")
        if edits.contains(where: { $0.key.isBudget || $0.key.isLiveCap }) {
            report.append("A reload does not re-read a seat's budget,  so the listener must be restarted before the new cap takes effect.")
        }
        if !refusals.isEmpty {
            report.append(refusals.joined(separator: "\n"))
        }
        message = report.joined(separator: "\n")
        await refresh()
    }

    /// Asks the listener to re-read the configuration file.
    @discardableResult
    func reload() async -> Bool {
        do {
            let result = try await runner.run(AgentSyncInvocation.reload())
            if result.exitCode != 0 {
                message = "The listener did not re-read the configuration file:  \(result.stderrTail)"
                return false
            }
            return true
        } catch {
            message = "The listener did not re-read the configuration file:  \(error.localizedDescription)"
            return false
        }
    }

    // MARK: The kill switch

    /// Stops a seat's wakes.
    func pause(seat: String) async {
        guard let invocation = try? AgentSyncInvocation.pause(seat: seat) else {
            message = "The seat tag is not a plain token,  so nothing was paused."
            return
        }
        await run(invocation, success: "The seat's wakes are paused.")
    }

    /// Lifts the kill switch.
    func resume(seat: String) async {
        guard let invocation = try? AgentSyncInvocation.resume(seat: seat) else {
            message = "The seat tag is not a plain token,  so nothing was resumed."
            return
        }
        await run(invocation, success: "The pause was lifted.")
    }

    // MARK: The pin

    /// Runs the seat's wake for real,  so a changed setting is proved by a wake that ran.
    func testWake(seat: String, runtime: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let result = try await runner.run(AgentSyncInvocation.testWakeRun(seat: seat, runtime: runtime))
            if result.exitCode == 0 {
                message = "The test wake ran.  Read its result before you pin it."
            } else {
                message = "The test wake did not pass:  \(result.stderrTail)"
            }
        } catch {
            message = "The test wake could not be run:  \(error.localizedDescription)"
        }
    }

    /// Pins the binary whose test wake passed.
    func pin(seat: String, runtime: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let result = try await runner.run(AgentSyncInvocation.testWakePin(seat: seat, runtime: runtime))
            if result.exitCode == 0 {
                message = "The binary was pinned.  A failed run never pins."
            } else {
                message = "The binary was not pinned:  \(result.stderrTail)"
            }
        } catch {
            message = "The binary could not be pinned:  \(error.localizedDescription)"
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
                message = success
            } else {
                message = "The listener refused the run:  \(result.stderrTail)"
            }
        } catch {
            message = "The listener could not be asked:  \(error.localizedDescription)"
        }
        await refresh()
    }
}
