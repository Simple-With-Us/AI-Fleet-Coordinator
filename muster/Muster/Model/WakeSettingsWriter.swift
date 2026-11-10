import Foundation

// Writing the wake settings.
//
// The eavesdropper's configuration file is the one the runtime reads, so the panel writes that file
// and then asks the eavesdropper to act on it.  These rules hold:
//
//   * only a seat the seat partition gives to the Mac is ever written, and a partition that cannot
//     be read refuses every seat.  The eavesdropper refuses to run with a section for any other seat,
//     enabled or not, so one such section stops the whole Mac eavesdropper at its next start
//     (`simple_tools/config.py` `partition_errors`, `daemon.py` `refuse_start`).
//   * only the value that changed is rewritten, so a comment or a key this app does not know about
//     survives
//   * a section holding a line this app cannot read is not written:  the unread line may already
//     define the key, and TOML refuses a duplicate
//   * a file that changed since the panel read it is not written, so another writer's change is
//     not overwritten
//   * a seat is found as the eavesdropper finds it, by its upper-cased name, and written under the
//     spelling the file already gives it;  a seat the file spells two ways is not written, because the
//     eavesdropper folds both to one seat and merges them unpredictably
//   * a routine setting is written only into a routine table the seat already has, in the form the
//     file already uses (`routine = { … }` or `[seat.X.routine]`), and never so as to take the table
//     out:  Muster does not turn a seat that Wakes into one that Captures Only, or the other way
//   * a routine table the eavesdropper's own rules (`_routine`) would refuse is not written
//   * no `wake` line is ever written:  no setting this writer knows lives there
//   * the old file is kept as a timestamped backup
//   * the new file reaches the disk under a unique name at mode 0600, and is then swapped with the
//     original.  0600 is the mode the runtime's own writer uses (`listener_cli.py` `cmd_init`).
//
// `prepare` and `commit` are separate so the store can run the eavesdropper's own check on the
// candidate between them.  The runtime takes no lock on this file, and a person's editor could not
// take one, so there is no lock to share.  The swap is the compare-and-swap instead:  it hands back
// what the file held at that instant, and anything but the text Muster read goes straight back.

/// Writes a seat's wake settings into the eavesdropper's configuration file.
struct WakeSettingsWriter {
    /// One change to one setting.
    struct Edit: Equatable {
        /// The seat the change belongs to.
        var seat: String
        /// The setting.
        var key: WakeSettingKey
        /// The new value, or nil to remove the setting from the file.
        var value: ListenerConfigValue?
    }

    /// A write that passed this app's own checks and has not touched the file yet.  Only this
    /// file can make one, so nothing reaches `commit` without having been through `prepare`.
    struct PreparedWrite: Equatable {
        /// The file's text when the write was prepared.
        let original: String
        /// The text the write would leave.
        let candidate: String
        /// The seats whose settings change.
        let seats: [String]
        /// The seat whose refused section this write takes out, when it is a repair.
        let repairedSeat: String?

        fileprivate init(original: String, candidate: String, seats: [String], repairedSeat: String?) {
            self.original = original
            self.candidate = candidate
            self.seats = seats
            self.repairedSeat = repairedSeat
        }
    }

    enum WriteError: Error, Equatable, LocalizedError {
        case unreadable(String)
        case couldNotWrite(String)
        case couldNotBackUp(String)
        case notHeldByTheMac(seat: String, reason: String)
        case notASeatName(String)
        case changedSinceRead(String)
        case unreadableLines(seat: String, problems: [String])
        case noSectionToRemove(String)
        case spelledTwice(seat: String, headers: [String])
        case anotherWritersTextMoved(path: String, keptAt: String)
        case noRoutineTable(String)
        case unreadableRoutine(seat: String, reason: String)
        case lastRoutineSetting(String)
        case routineRefused(seat: String, problems: [String])

        var errorDescription: String? {
            switch self {
            case .unreadable(let path):
                return "The configuration file at \(path) could not be read, so nothing was changed."
            case .couldNotWrite(let path):
                return "The configuration file at \(path) could not be written."
            case .couldNotBackUp(let path):
                return "The configuration file at \(path) could not be backed up, so nothing was changed."
            case .notHeldByTheMac(let seat, let reason):
                return "Nothing was written for \(seat).\u{00A0} \(reason)"
            case .notASeatName(let value):
                return "\(value) is not a seat tag the eavesdropper accepts, so nothing was written."
            case .changedSinceRead(let path):
                return "The configuration file at \(path) changed after Muster read it, so nothing was written.\u{00A0} Refresh, then apply again."
            case .unreadableLines(let seat, let problems):
                let lines = problems.joined(separator: " ")
                return "The \(seat) section holds a line Muster cannot read, so nothing was written.\u{00A0} \(lines)"
            case .noSectionToRemove(let seat):
                return "The file holds no section for \(seat), or the seat partition gives it to the Mac, so nothing was removed."
            case .spelledTwice(let seat, let headers):
                let named = headers.map { "[\($0)]" }.joined(separator: " and ")
                return "The file holds \(named), which the eavesdropper folds into one seat, \(seat.uppercased()), and merges unpredictably, so nothing was written.\u{00A0} Keep one spelling, then apply again."
            case .anotherWritersTextMoved(let path, let keptAt):
                return "Another writer changed the configuration file at \(path) during the write, and Muster could not put its text back.\u{00A0} That text is at \(keptAt), and the file now holds Muster's edit."
            case .noRoutineTable(let seat):
                return "The \(seat) section has no routine table, so the seat Captures Only and nothing was written.\u{00A0} \(WakeSeatKind.routineWaitsOnTheListener)"
            case .unreadableRoutine(let seat, let reason):
                return "Nothing was written for \(seat).\u{00A0} \(reason)"
            case .lastRoutineSetting(let seat):
                return "Taking out the last setting of \(seat)'s routine table would take the table out, and the seat would then capture only, so nothing was written."
            case .routineRefused(let seat, let problems):
                return "The eavesdropper would refuse \(seat)'s routine table, so nothing was written:  " + problems.joined(separator: "; ")
            }
        }
    }

    /// Where the file is.
    var configURL: URL
    /// Where the seat partition is, or nil when the checkout could not be found.
    var partitionURL: URL?
    /// How many old copies of the file to keep.
    static let backupsKept = 20

    init(configURL: URL, partitionURL: URL?) {
        self.configURL = configURL
        self.partitionURL = partitionURL
    }

    /// Applies the edits to the file with no further check, and returns the file's new text.
    ///
    /// The store runs the eavesdropper's own check between `prepare` and `commit`.  This is the same
    /// two steps with nothing between them.
    @discardableResult
    func apply(_ edits: [Edit]) throws -> String {
        let prepared = try prepare(edits)
        try commit(prepared)
        return prepared.candidate
    }

    /// Checks the edits and works out the new text, without touching the file.
    ///
    /// Every edit is applied to the text the last one produced, so two edits to the same file do
    /// not fight over it.  `expected` is the text the panel read, when there is one.
    func prepare(_ edits: [Edit], expecting expected: String? = nil) throws -> PreparedWrite {
        var seats: [String] = []
        for edit in edits where !seats.contains(edit.seat) {
            seats.append(edit.seat)
        }
        try requireHeldByTheMac(seats)
        let text = try readFile()
        try requireUnchanged(text, expected: expected)
        let base = ListenerConfigDocument(text: text)
        try requireHeldByTheMac(seats, in: base)
        for seat in seats {
            // The eavesdropper folds a seat's name to upper case, so a seat is found under the
            // spelling the file already gives it, and written there.  Two spellings of one seat are
            // two headers it merges unpredictably, so neither is written.
            if base.spellings(ofSeat: seat).count > 1 {
                throw WriteError.spelledTwice(seat: seat, headers: base.sections(matchingSeat: seat).map(\.name))
            }
            let problems = base.sections(matchingSeat: seat).flatMap(\.problems)
            guard problems.isEmpty else {
                throw WriteError.unreadableLines(seat: seat, problems: problems)
            }
        }
        var updated = text
        // Every value is set before any is taken out, so a batch that clears one routine setting and
        // sets another is judged on the table it leaves:  taking out the last key of an inline table
        // is refused only when nothing the same batch sets is left in it.
        let ordered = edits.filter { $0.value != nil } + edits.filter { $0.value == nil }
        for edit in ordered {
            let document = ListenerConfigDocument(text: updated)
            let location = edit.key.location(seat: edit.seat, in: document)
            if edit.key.isRoutine {
                try requireRoutineTable(edit, location: location, in: document)
            }
            if let value = edit.value {
                updated = try document.setting(
                    section: location.section,
                    key: location.key,
                    subPath: location.subPath,
                    to: value
                )
            } else {
                updated = try document.removing(
                    section: location.section,
                    key: location.key,
                    subPath: location.subPath
                )
            }
        }
        // The eavesdropper's own routine rules, on what this write would leave.  Only what the
        // write adds stops it, as `daemon check-config` judges an ordinary edit by its new errors.
        let after = ListenerConfigDocument(text: updated)
        for seat in seats where edits.contains(where: { $0.seat == seat && $0.key.isRoutine }) {
            let before = WakeRoutineRules.problems(seat: seat, routine: base.routineTable(seat: seat) ?? [:])
            let now = WakeRoutineRules.problems(seat: seat, routine: after.routineTable(seat: seat) ?? [:])
            let added = now.filter { !before.contains($0) }
            guard added.isEmpty else {
                throw WriteError.routineRefused(seat: seat, problems: added)
            }
        }
        return PreparedWrite(original: text, candidate: updated, seats: seats, repairedSeat: nil)
    }

    /// Works out the text with a refused section taken out, without touching the file.
    ///
    /// This is the one write allowed for a seat the Mac does not hold, and it is allowed only for
    /// such a seat:  the partition has to be readable, and it or the section itself has to say the
    /// Mac's eavesdropper will not run it.  The store points this writer at the partition the eavesdropper
    /// says it reads, so this does not rest on a copy the eavesdropper does not use.
    func prepareRemoval(ofSeat seat: String, expecting expected: String? = nil) throws -> PreparedWrite {
        let text = try readFile()
        try requireUnchanged(text, expected: expected)
        let document = ListenerConfigDocument(text: text)
        let host = ListenerPartition.load(from: partitionURL).host(of: seat, in: document)
        guard host.isElsewhere, !document.sections(matchingSeat: seat).isEmpty else {
            throw WriteError.noSectionToRemove(seat)
        }
        // Every spelling the eavesdropper folds to this seat is a section it refuses, so each goes.
        var candidate = text
        for spelling in document.spellings(ofSeat: seat) {
            candidate = ListenerConfigDocument(text: candidate).removingSeat(spelling)
        }
        return PreparedWrite(
            original: text,
            candidate: candidate,
            seats: [],
            repairedSeat: seat
        )
    }

    /// Writes a prepared write's text beside the file, for the eavesdropper's own check to read.
    func writeCandidate(_ prepared: PreparedWrite) throws -> URL {
        let target = configURL.resolvingSymlinksInPath()
        let candidate = target.deletingLastPathComponent()
            .appendingPathComponent(".\(target.lastPathComponent).muster-check-\(UUID().uuidString)")
        try writeExclusive(Data(prepared.candidate.utf8), to: candidate)
        return candidate
    }

    /// Puts a prepared write in place.
    ///
    /// The gate and the comparison run again, because the check between `prepare` and here takes
    /// time and the partition or the file can change in it.
    func commit(_ prepared: PreparedWrite) throws {
        let original = ListenerConfigDocument(text: prepared.original)
        try requireHeldByTheMac(prepared.seats, in: original)
        if let seat = prepared.repairedSeat {
            guard ListenerPartition.load(from: partitionURL).host(of: seat, in: original).isElsewhere else {
                throw WriteError.noSectionToRemove(seat)
            }
        }
        let text = try readFile()
        try requireUnchanged(text, expected: prepared.original)
        try backUp(prepared.original)
        try replaceFile(with: prepared.candidate, expecting: prepared.original)
    }

    // MARK: The gates

    /// Refuses a routine setting for a seat with no routine table Muster can read, and the
    /// removal of the last setting of an inline table, which would take the whole table out.  The
    /// writer applies every set before any removal, so the table judged here already holds what the
    /// batch adds.
    private func requireRoutineTable(
        _ edit: Edit,
        location: (section: String, key: String, subPath: [String]),
        in document: ListenerConfigDocument
    ) throws {
        switch document.routineForm(seat: edit.seat) {
        case .absent:
            throw WriteError.noRoutineTable(edit.seat)
        case .unreadable(let why):
            throw WriteError.unreadableRoutine(seat: edit.seat, reason: why)
        case .subTable:
            return
        case .inline:
            guard edit.value == nil,
                  let entries = document.assignment(section: document.seatSection(edit.seat), key: "routine")?.node.tableEntries,
                  entries.count == 1,
                  entries.first?.key == location.subPath.first else { return }
            throw WriteError.lastRoutineSetting(edit.seat)
        }
    }

    /// Refuses any seat the partition does not give to the Mac, and every seat when the partition
    /// cannot be read.  With the file's text, it also refuses a seat whose own section names the
    /// other instance.  The partition is read again each time, so this does not rest on what the
    /// panel last showed.
    private func requireHeldByTheMac(_ seats: [String], in document: ListenerConfigDocument? = nil) throws {
        guard !seats.isEmpty else { return }
        let partition = ListenerPartition.load(from: partitionURL)
        for seat in seats {
            // A tag goes into a `[seat.X]` header, so it has to be one the eavesdropper accepts.
            guard ListenerPartition.isSeatName(seat) else {
                throw WriteError.notASeatName(seat)
            }
            let host = document.map { partition.host(of: seat, in: $0) } ?? partition.host(of: seat)
            guard host.isTheMac else {
                throw WriteError.notHeldByTheMac(seat: seat, reason: host.readOnlyReason ?? "")
            }
        }
    }

    /// Refuses a file that is not byte for byte what was read.
    private func requireUnchanged(_ text: String, expected: String?) throws {
        guard let expected else { return }
        guard Data(text.utf8) == Data(expected.utf8) else {
            throw WriteError.changedSinceRead(configURL.path)
        }
    }

    // MARK: The file

    private func readFile() throws -> String {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else {
            throw WriteError.unreadable(configURL.path)
        }
        return text
    }

    /// Keeps a copy of the old file, and drops copies past the newest few.
    private func backUp(_ text: String) throws {
        let fileManager = FileManager.default
        let directory = configURL.resolvingSymlinksInPath().deletingLastPathComponent()
            .appendingPathComponent("listener/muster-config-backups", isDirectory: true)
        do {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = TimeZone(identifier: "America/Chicago")
            formatter.dateFormat = "yyyyMMdd-HHmmss"
            let suffix = UUID().uuidString.prefix(8)
            let name = "listener.toml.\(formatter.string(from: Date()))-\(suffix)"
            try writeExclusive(Data(text.utf8), to: directory.appendingPathComponent(name))
            let names = try fileManager.contentsOfDirectory(atPath: directory.path)
                .filter { $0.hasPrefix("listener.toml.") }
                .sorted(by: >)
            for old in names.dropFirst(Self.backupsKept) {
                try? fileManager.removeItem(at: directory.appendingPathComponent(old))
            }
        } catch {
            throw WriteError.couldNotBackUp(configURL.path)
        }
    }

    /// Replaces the file by writing a unique temporary file and swapping it with the original, and
    /// puts the original back if it is not the text this write was worked out from.
    ///
    /// The swap is atomic and hands back what the file held at that instant, so a writer that got
    /// in after the last comparison is caught here and its change survives.  A volume that cannot
    /// swap falls back to a plain rename, with only that last comparison in front of it.
    ///
    /// A symlink is followed first, so the link stays a link and the file it names is what changes.
    func replaceFile(with text: String, expecting original: String) throws {
        let target = configURL.resolvingSymlinksInPath()
        let directory = target.deletingLastPathComponent()
        let temporary = directory
            .appendingPathComponent(".\(target.lastPathComponent).muster-\(UUID().uuidString).tmp")
        try writeExclusive(Data(text.utf8), to: temporary)
        let swap = UInt32(RENAME_SWAP)
        if renamex_np(temporary.path, target.path, swap) == 0 {
            guard (try? Data(contentsOf: temporary)) == Data(original.utf8) else {
                guard renamex_np(temporary.path, target.path, swap) == 0 else {
                    // The other writer's text is at the temporary name, so it stays there.
                    throw WriteError.anotherWritersTextMoved(path: configURL.path, keptAt: temporary.path)
                }
                try? FileManager.default.removeItem(at: temporary)
                throw WriteError.changedSinceRead(configURL.path)
            }
            try? FileManager.default.removeItem(at: temporary)
        } else {
            let reason = errno
            guard reason == ENOTSUP || reason == EINVAL, rename(temporary.path, target.path) == 0 else {
                try? FileManager.default.removeItem(at: temporary)
                throw WriteError.couldNotWrite(configURL.path)
            }
        }
        // The change is only durable once the directory is on disk too.
        let descriptor = open(directory.path, O_RDONLY)
        if descriptor >= 0 {
            fsync(descriptor)
            close(descriptor)
        }
    }

    /// Creates a new file at mode 0600 and syncs it.  It refuses a name that already exists and a
    /// symlink, so a file planted at that name cannot redirect the write.
    private func writeExclusive(_ data: Data, to url: URL) throws {
        let descriptor = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else { throw WriteError.couldNotWrite(configURL.path) }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        do {
            try handle.write(contentsOf: data)
            try handle.synchronize()
            try handle.close()
        } catch {
            try? FileManager.default.removeItem(at: url)
            throw WriteError.couldNotWrite(configURL.path)
        }
    }
}

// MARK: - Turning panel values into edits

extension WakeSettingKey {
    /// Whether the setting holds a number.
    var isNumeric: Bool {
        switch self {
        case .wakesPerHour, .wakesPerDay, .perTopicPerHour, .ownerPerDay,
             .ownerPerTopicPerHour, .usdPerDay, .livePerHour, .livePerDay,
             .livePerTopicMinutes, .liveOwnerPerDay, .liveLoopTurns, .routineTimeout:
            return true
        default:
            return false
        }
    }

    /// Why a 0 cap is refused where the eavesdropper compares with `>=`.
    static let zeroStopsEveryWakeReason = "0 would stop every wake.\u{00A0} A no-limit setting waits on the listener change."

    /// What is wrong with a value for this setting on its own, by the eavesdropper's rules, or nil
    /// when nothing is.  A rule that needs the whole routine table, such as two variables that must
    /// differ, is checked by the writer on the table it would leave.
    func fieldProblem(_ value: ListenerConfigValue) -> String? {
        if zeroStopsEveryWake, case .number(let number) = value, number == 0 {
            return Self.zeroStopsEveryWakeReason
        }
        switch self {
        case .routineURLEnv, .routineKeyEnv:
            return WakeRoutineRules.envNameProblem(value, name: configLocation.subPath.first ?? configLocation.key)
                .map { $0 + ".\u{00A0} This is the name of a variable, never its value." }
        case .routineTimeout:
            guard case .number(let seconds) = value,
                  seconds >= WakeRoutineRules.minimumTimeout,
                  seconds <= WakeRoutineRules.maximumTimeout else {
                return "timeout_seconds must be a number from 1 to 60"
            }
            return nil
        default:
            return nil
        }
    }

    /// The value a field holds, when the text can be read as one.
    ///
    /// Nothing is written when the text is not a value of this kind, so a half-typed number never
    /// lands in the file.
    func value(from text: String) -> ListenerConfigValue? {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        if isNumeric {
            guard let number = Double(trimmed) else { return nil }
            // A cap is a count or an amount, so a negative or a non-finite one is not a value this
            // app can write.  `simple_tools/config.py` would refuse the setting when it read the
            // file, and the seat would stop waking after a reload:  a bad number
            // must never reach the file.
            guard number.isFinite, number >= 0 else { return nil }
            // `live_limits` keeps only whole numbers, so 2.5 would leave the default in force.
            if isLiveCap, number != number.rounded() { return nil }
            return .number(number)
        }
        return .text(trimmed)
    }
}
