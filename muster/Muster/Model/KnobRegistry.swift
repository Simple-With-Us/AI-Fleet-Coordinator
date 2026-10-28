import Foundation

/// The fleet's control knobs,  read from the pinned registry.
///
/// The registry is the generated block in `INFISICAL.md`,  which is the human-readable
/// form of `scripts/fleet_knobs/registry.py`.  It names every variable that governs a
/// script or a background process,  who owns it,  what it may be,  and which way it is
/// allowed to move.  The block is generated and pinned,  so the app reads it and never
/// guesses a knob that is not there.
///
/// A knob's current value comes from the last-known-good copy,  which holds the
/// non-secret knobs only.  The copy is read under the engine's own rules,  so a
/// copy the engine would refuse is not a reading here either.  A secret knob's
/// presence is not something this reader can say,  because the copy never
/// carries one.
struct KnobRegistry {
    /// One knob,  as the registry declares it.
    struct Knob: Identifiable {
        let name: String
        /// Who owns the knob,  which is the accountable team.
        let owner: String
        /// The kind of value it takes,  with the choices or the bounds.
        let kind: String
        /// The value it holds when nothing has changed it.
        let defaultValue: String
        /// Whether the knob holds a secret.
        let isSecret: Bool
        /// The secret column,  as the registry wrote it.
        ///
        /// The column is "no" or "secret".  The panel treats anything else as a
        /// secret,  and this is the raw text,  so a change in the generator's
        /// wording shows up in a test rather than in a value on the screen.
        let secretColumn: String
        /// How far a local file may move it.
        let localMay: String
        /// What the knob does.
        let note: String
        /// The value it holds now,  and where that value came from.
        let current: CurrentValue

        var id: String { name }

        /// The text the panel shows in the "Now" column.
        ///
        /// It is the one renderer,  so the panel and the model cannot say two
        /// different things about the same knob.
        var nowText: String { current.displayText }

        /// The value a knob holds now.
        enum CurrentValue: Equatable {
            /// The last-known-good copy has a value for it.
            case current(String)
            /// It has never been set,  so it holds its default.
            ///
            /// The value the registry declares rides along here,  because a column
            /// that says only "default" does not tell the operator what the knob is
            /// holding.
            case defaultValue(String)
            /// It is a secret,  and the copy never carries one,  so whether it is set
            /// is not something this reader can say.
            case secretUnconfirmed
            /// The copy could not be read,  so the value is not known.
            case unknown(String)

            /// The text the panel shows.
            ///
            /// A knob that holds its default says which value that is,  because a
            /// column that says only "default" does not tell the operator what the
            /// knob is holding.  A secret's default is not shown:  the secret's own
            /// presence is the thing that is unknown.
            var displayText: String {
                switch self {
                case .current(let value): return value
                case .defaultValue(let value):
                    return value.isEmpty ? "default" : "default (\(value))"
                case .secretUnconfirmed: return "not known (secret)"
                case .unknown(let reason): return reason
                }
            }
        }
    }

    /// One knob,  as the panel shows it.
    ///
    /// The panel renders this,  and the tests read it,  so what a secret row shows is a
    /// thing that can be checked rather than a thing buried in a view.
    struct Row: Identifiable {
        /// The knob's name.
        let name: String
        /// Who owns it.
        let owner: String
        /// The kind of value it takes.
        let kind: String
        /// The value it holds now,  as the panel shows it.
        let now: String
        /// True when the "Now" cell belongs to a secret,  so it is marked as one.
        let nowIsSecret: Bool
        /// How far a local file may move it.
        let localMay: String
        /// What the knob does,  in full.
        ///
        /// The panel puts the note on a line of its own,  because the notes are far
        /// longer than a column of a table can hold.
        let note: String
        /// The editor the knob gets,  or nil for a secret,  which is never edited here.
        var editor: KnobEditorKind? = nil
        /// The value an editor starts from.
        var editValue: String = ""

        var id: String { name }
    }

    /// The knobs,  in the order the registry lists them.
    let knobs: [Knob]
    /// The age of the copy,  or why there is no copy to read.
    let copyAge: String
    /// True when the copy is older than the engine rewrites it.
    let copyIsStale: Bool
    /// The sentence the panel shows about the copy.
    let copyHeadline: String
    /// How many rows of the generated block were there but could not be read.
    let unreadableRows: Int
    /// Why the registry itself could not be read,  when it could not.
    let problem: String?

    /// The knobs,  as the panel shows them.
    var rows: [Row] {
        knobs.map { knob in
            Row(
                name: knob.name,
                owner: knob.owner,
                kind: knob.kind,
                now: knob.nowText,
                nowIsSecret: knob.isSecret,
                localMay: knob.localMay,
                note: knob.note,
                editor: knob.editor,
                editValue: knob.isSecret ? "" : knob.editValue
            )
        }
    }
}

enum KnobReader {
    /// The generated block in `INFISICAL.md` that carries the registry.
    static let blockStart = "<!-- fleet-knobs:begin"
    static let blockEnd = "<!-- fleet-knobs:end -->"

    /// How many columns a row of the generated block has.
    ///
    /// The block is a table of seven:  key,  owner,  kind,  default,  secret,  the
    /// direction a local file may move it,  and what the knob does.  A row with any
    /// other number of columns is not a row,  and a row that is not read is a knob
    /// the panel would not show,  so the count of them is reported.
    static let columnCount = 7

    /// Read the registry and the current values.
    ///
    /// The registry is a table in a markdown file,  so the read is a table read and not
    /// a parse of somebody's code.  A missing or unreadable file yields an empty
    /// registry and the reason,  and never a guessed knob.
    ///
    /// The identity is the engine's own:  the project and environment the engine's
    /// copies carry.  The app does not know them by itself,  so the caller asks the
    /// engine.  Without that answer the copy cannot be confirmed as this fleet's,  and
    /// the panel says so rather than reading a copy it cannot vouch for.
    static func read(
        repoRoot: URL,
        home: URL,
        identity: EngineIdentity.Identity?
    ) -> KnobRegistry {
        let table = readTable(in: repoRoot.appendingPathComponent("INFISICAL.md"))
        let copy = readCopy(in: home, identity: identity)
        let knobs = table.rows.map { row -> KnobRegistry.Knob in
            let name = row.value(at: 0)
            let defaultValue = row.value(at: 3)
            // The secret column fails closed.  A column that says anything other
            // than "no" is treated as a secret,  so a wording the app does not know
            // cannot have a secret's value shown in plain text.
            let secret = row.value(at: 4).lowercased() != "no"
            let current: KnobRegistry.Knob.CurrentValue
            if secret {
                // The copy holds the non-secret knobs only,  so a secret is never in
                // it and its absence says nothing about whether the secret is set.
                // The panel says the presence is unconfirmed rather than reporting a
                // secret as not set,  which would be a false statement about a
                // credential that is very likely set.
                current = .secretUnconfirmed
            } else if copy.isReadable, let value = copy.knobs[name] {
                current = .current(value)
            } else if copy.isReadable {
                // The copy is there and it does not carry this knob.  The engine
                // writes the copy after a good fetch and leaves out every knob that
                // is unset,  so a knob missing from a copy the engine wrote holds
                // whatever the registry says it holds.
                current = .defaultValue(defaultValue)
            } else {
                // There is no copy to read,  so the value is not known.  Saying
                // "default" here would claim the knob equals its default,  which
                // is a statement the app cannot make.  A copy the engine would
                // refuse carries no reading at all,  expired or not.
                current = .unknown(copy.reason ?? "the copy could not be read")
            }
            return KnobRegistry.Knob(
                name: name,
                owner: row.value(at: 1),
                kind: row.value(at: 2),
                defaultValue: defaultValue,
                isSecret: secret,
                secretColumn: row.value(at: 4),
                localMay: row.value(at: 5),
                note: row.value(at: 6),
                current: current
            )
        }
        return KnobRegistry(
            knobs: knobs,
            copyAge: copy.ageText,
            copyIsStale: copy.isStale,
            copyHeadline: copy.headline,
            unreadableRows: table.unreadable,
            problem: table.problem
        )
    }

    /// One row of the registry table.
    private struct Row {
        let cells: [String]

        func value(at index: Int) -> String {
            guard index < cells.count else { return "" }
            // The registry writes its values in code spans,  so a key arrives as
            // `FLEET_AUTOPUSH_MODE` and a default as `off`.  The panel shows the
            // value itself,  and a key has to match the name the copy uses.
            return cells[index].replacingOccurrences(of: "`", with: "")
        }
    }

    /// The rows of the generated table,  and what was wrong with the rest.
    private struct Table {
        let rows: [Row]
        /// The rows that were there but were not a row this app can read.
        let unreadable: Int
        /// Why the block could not be read at all,  when it could not.
        let problem: String?
    }

    /// Read the generated table.
    ///
    /// A row is split on its pipes with the empty cells kept,  because a knob with no
    /// default or no note is a row with an empty cell in it,  and dropping the empty
    /// cells would move every later value one column to the left.
    private static func readTable(in file: URL) -> Table {
        let text: String
        do {
            text = try String(contentsOf: file, encoding: .utf8)
        } catch {
            // A file that is missing,  unreadable or not UTF-8 is not the same thing
            // as a file without a block,  and the panel says which it was.
            return Table(rows: [], unreadable: 0, problem: describe(error))
        }
        var rows: [Row] = []
        var unreadable = 0
        var inside = false
        var found = false
        var ended = false
        for line in text.components(separatedBy: "\n") {
            if line.hasPrefix(blockStart) {
                inside = true
                found = true
                continue
            }
            if line.hasPrefix(blockEnd) {
                ended = true
                break
            }
            guard inside else { continue }
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("|") else { continue }
            let cells = splitRow(trimmed)
            // The header row and the rule under it are not knobs.
            guard cells[0] != "Key" else { continue }
            if cells.allSatisfy({ $0.allSatisfy { $0 == "-" } }) { continue }
            guard cells.count == columnCount else {
                // A row that is not the right shape is a knob the panel would not
                // show,  so it is counted rather than dropped in silence.
                unreadable += 1
                continue
            }
            rows.append(Row(cells: cells))
        }
        if !found {
            return Table(rows: [], unreadable: 0, problem: "the registry block is not in the file")
        }
        if !ended {
            // The block has a start marker and no end marker,  so the file is
            // truncated and the rows read so far are part of an inventory that
            // was never finished.  A partial inventory is not an inventory:  the
            // panel would show a few knobs and call the set complete.
            return Table(
                rows: [],
                unreadable: 0,
                problem: "the registry block is not finished:  the file ends before its end marker"
            )
        }
        return Table(rows: rows, unreadable: unreadable, problem: nil)
    }

    /// Split one table row into its cells,  keeping the empty ones.
    ///
    /// The outer pipes are the row's delimiters and are not cells.  A pipe inside a
    /// cell is escaped with a backslash and does not split it.
    private static func splitRow(_ line: String) -> [String] {
        var body = Substring(line)
        if body.hasPrefix("|") { body = body.dropFirst() }
        if body.hasSuffix("|") { body = body.dropLast() }
        var cells: [String] = []
        var current = ""
        var escaped = false
        for character in body {
            if escaped {
                current.append(character == "|" ? "|" : character)
                escaped = false
                continue
            }
            if character == "\\" {
                escaped = true
                continue
            }
            if character == "|" {
                cells.append(current.trimmingCharacters(in: .whitespaces))
                current = ""
                continue
            }
            current.append(character)
        }
        cells.append(current.trimmingCharacters(in: .whitespaces))
        return cells
    }

    /// Say what went wrong with the registry file,  in the app's own words.
    private static func describe(_ error: Error) -> String {
        let reason = (error as NSError).localizedDescription
        if reason.isEmpty {
            return "the registry file could not be read"
        }
        return "the registry file could not be read: \(reason)"
    }

    /// The last-known-good copy,  with the age of the copy.
    private struct Copy {
        let knobs: [String: String]
        let ageText: String
        /// True when the copy is older than the engine rewrites it.
        let isStale: Bool
        /// False when there is no copy to read,  or the engine would refuse it.
        let isReadable: Bool
        /// Why the copy is not readable,  when it is not.
        let reason: String?
        /// The sentence the panel shows about the copy.
        let headline: String
    }

    /// How often the engine rewrites an unchanged copy,  in seconds.
    ///
    /// These are the engine's own numbers,  from `scripts/fleet_knobs/lkg.py`:
    /// `REFRESH_SECONDS` and `MAX_AGE_SECONDS`.  A copy between one refresh and the
    /// expiry is stale,  and a copy past the expiry is not a reading of anything.
    static let refreshSeconds = 3600
    static let maxAgeSeconds = 48 * 3600

    /// The engine's own identity for a copy,  asked of the engine.
    ///
    /// The engine only trusts a copy that carries its own identity,  so the panel reads
    /// it the same way:  a copy without it is not a reading of anything,  even when it
    /// parses and holds a plausible value.
    static let copySchema = 1
    /// The engine treats a copy more than this far in the future as expired.
    static let futureToleranceSeconds = 300
    /// The largest copy the engine will read,  in bytes.
    static let maxCopyBytes = 256 * 1024

    /// The copy the fleet's own tool wrote.
    ///
    /// It holds the non-secret knobs only,  and the tool refuses to write a secret
    /// value into it,  so reading it cannot put a secret on the screen.
    ///
    /// The file is checked the way the engine checks it:  a file that is a symlink,
    /// not a regular file, owned by somebody else, readable by the group or by
    /// others, or too large is never trusted.
    private static func readCopy(in home: URL, identity: EngineIdentity.Identity?) -> Copy {
        let directory = home.appendingPathComponent(".local/state/fleet-knobs", isDirectory: true)
        let path = directory.appendingPathComponent("lkg.json", isDirectory: false)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path.path, isDirectory: &isDirectory),
              isDirectory.boolValue == false else {
            return Copy(
                knobs: [:],
                ageText: "no local copy",
                isStale: true,
                isReadable: false,
                reason: "no local copy",
                headline: "No local copy of the knobs."
            )
        }
        guard isPrivateFile(at: path, in: directory) else {
            return Copy(
                knobs: [:],
                ageText: "no usable copy",
                isStale: true,
                isReadable: false,
                reason: "the copy is not a private file of this user",
                headline: "No local copy of the knobs."
            )
        }
        guard
            let data = try? Data(contentsOf: path),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return Copy(
                knobs: [:],
                ageText: "no local copy",
                isStale: true,
                isReadable: false,
                reason: "no local copy",
                headline: "No local copy of the knobs."
            )
        }
        // The engine accepts a copy only when it carries its own identity,  a map of
        // knobs,  and a time.  This reader takes the same conditions,  because a copy
        // the engine would refuse is not a reading of a knob,  and reading one anyway
        // would put a value on the screen that the fleet's own tools do not believe.
        guard let identity else {
            // The engine did not say which project its copy belongs to,  so the copy
            // cannot be confirmed as this fleet's.  It is not read at all.
            return Copy(
                knobs: [:],
                ageText: "not a copy this app knows",
                isStale: true,
                isReadable: false,
                reason: "the engine did not say which project its copy belongs to",
                headline: "The engine did not say which project its copy belongs to,  so the copy is not read."
            )
        }
        let schema = (object["schema"] as? NSNumber)?.doubleValue
        let project = object["project"] as? String
        let environment = object["environment"] as? String
        let knobs = object["knobs"] as? [String: Any]
        let saved = (object["saved_epoch"] as? NSNumber)?.doubleValue
        guard
            schema == Double(copySchema),
            project == identity.project,
            environment == identity.environment,
            let knobs,
            let saved
        else {
            return Copy(
                knobs: [:],
                ageText: "not a copy this app knows",
                isStale: true,
                isReadable: false,
                reason: "the copy is not one the engine wrote",
                headline: "The local copy is not one the engine wrote."
            )
        }
        // A time that is not a number a clock could hold is not a time.  A value such
        // as 1e300 is valid JSON and would trap the conversion to an integer,  so it
        // is refused before anything tries to read it as an age.
        guard saved.isFinite, saved > 0, saved < 4_102_444_800 else {
            return Copy(
                knobs: [:],
                ageText: "no usable copy",
                isStale: true,
                isReadable: false,
                reason: "the copy has no time this app can read",
                headline: "The local copy has no time this app can read."
            )
        }
        var values: [String: String] = [:]
        for (key, value) in knobs {
            if let text = value as? String { values[key] = text }
        }
        let age = Date().timeIntervalSince1970 - saved
        // The engine rewrites an unchanged copy every hour,  ignores one older than
        // two days,  and treats one more than five minutes in the future as expired.
        // The panel says the same thing the engine does.
        let stale = age > Double(refreshSeconds)
        let expired = age > Double(maxAgeSeconds) || age < -Double(futureToleranceSeconds)
        if expired {
            return Copy(
                knobs: [:],
                ageText: "past its expiry",
                isStale: true,
                isReadable: false,
                reason: "the copy is past its expiry",
                headline: "The local copy is past its expiry,  so it says nothing."
            )
        }
        let ageText = age <= 0 ? "just now" : FleetTime.age(Int(age))
        return Copy(
            knobs: values,
            ageText: ageText,
            isStale: stale,
            isReadable: true,
            reason: nil,
            headline: "Local copy \(ageText) old."
        )
    }

    /// Check a file the way the engine's own reader checks it.
    ///
    /// The engine reads the copy without following a symlink,  and refuses a file
    /// that is not a regular file of this user that nobody else can read.  A file
    /// that does not pass is never trusted here either.
    private static func isPrivateFile(at path: URL, in directory: URL) -> Bool {
        guard let values = try? path.resourceValues(forKeys: [
            .isRegularFileKey,
            .isSymbolicLinkKey,
            .fileSizeKey,
        ]) else { return false }
        guard values.isSymbolicLink != true, values.isRegularFile == true else { return false }
        guard (values.fileSize ?? 0) <= maxCopyBytes else { return false }
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path.path) else {
            return false
        }
        let owner = (attributes[.ownerAccountID] as? NSNumber)?.uint32Value
        guard owner == getuid() else { return false }
        let mode = (attributes[.posixPermissions] as? NSNumber)?.int16Value ?? 0
        guard mode & 0o077 == 0 else { return false }
        // The directory the copy sits in is private too,  or the file's name could be
        // pointed somewhere else by whoever made the directory.
        guard let directoryAttributes = try? FileManager.default
            .attributesOfItem(atPath: directory.path) else { return false }
        let directoryMode = (directoryAttributes[.posixPermissions] as? NSNumber)?.int16Value ?? 0
        return directoryMode & 0o077 == 0
    }
}

// MARK: - Editing a knob

/// The editor a knob gets,  read from the registry's kind column.
///
/// The kind column is the generator's own words:  `bool`,  `enum: a < b < c`,
/// `int 1 to 168`,  `str`,  `list` and `caps`.  A kind this app does not know gets a text
/// field,  because the knob engine checks every value against the knob's own spec before it
/// writes,  and a refusal there is the real gate.
enum KnobEditorKind: Equatable {
    /// A yes or a no.
    case toggle
    /// One of a fixed list,  in the registry's order.
    case choice([String])
    /// A whole number,  inside the bounds when the registry gives them.
    case integer(min: Int?, max: Int?)
    /// Free text,  with a hint about its shape.
    case text(hint: String)

    /// The editor for a kind column.
    init(kind: String) {
        let trimmed = kind.trimmingCharacters(in: .whitespaces)
        let lower = trimmed.lowercased()
        if lower == "bool" {
            self = .toggle
            return
        }
        if lower.hasPrefix("enum:") {
            let choices = trimmed.dropFirst("enum:".count)
                .split(separator: "<")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            self = choices.isEmpty ? .text(hint: "one of the knob's values") : .choice(choices)
            return
        }
        if lower == "int" || lower.hasPrefix("int ") {
            let words = lower.split(separator: " ").map(String.init)
            // `int A to B`:  the bounds are the second and the fourth words.
            if words.count == 4, words[2] == "to", let low = Int(words[1]), let high = Int(words[3]) {
                self = .integer(min: low, max: high)
            } else {
                self = .integer(min: nil, max: nil)
            }
            return
        }
        switch lower {
        case "list": self = .text(hint: "comma-separated entries, empty for none")
        case "caps": self = .text(hint: "name=number pairs, comma-separated")
        default: self = .text(hint: "text")
        }
    }

    /// Why a value is not one this editor takes,  or nil when it is.
    ///
    /// The check is a convenience for the sheet:  the knob engine checks the value again
    /// against the knob's own spec,  and its answer is the one that counts.
    func problem(with value: String) -> String? {
        switch self {
        case .toggle:
            return value == "true" || value == "false" ? nil : "The value must be true or false."
        case .choice(let choices):
            return choices.contains(value) ? nil : "The value must be one of \(choices.joined(separator: ", "))."
        case .integer(let low, let high):
            guard let number = Int(value) else { return "The value must be a whole number." }
            if let low = low, number < low { return "The value must be at least \(low)." }
            if let high = high, number > high { return "The value must be at most \(high)." }
            return nil
        case .text:
            if value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) {
                return "The value must be one line."
            }
            return nil
        }
    }
}

extension KnobRegistry.Knob {
    /// The editor this knob gets,  or nil for a secret:  a secret is never edited here,
    /// and the knob engine refuses one anyway.
    var editor: KnobEditorKind? {
        isSecret ? nil : KnobEditorKind(kind: kind)
    }

    /// The value an editor starts from:  the value the knob holds now,  or its default.
    ///
    /// The registry writes an empty default as a word (`empty`,  `unset`,  `none`),  and
    /// the editor starts from no text at all for those.
    var editValue: String {
        if case .current(let value) = current { return value }
        return KnobRegistry.Knob.plainDefault(defaultValue)
    }

    /// A registry default,  with the words that mean "nothing" turned into no text.
    static func plainDefault(_ value: String) -> String {
        let lower = value.lowercased()
        if lower == "empty" || lower == "unset" || lower.hasPrefix("none") {
            return ""
        }
        return value
    }
}
