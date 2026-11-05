import Foundation

/// The fleet's control knobs,  read from the pinned registry.
///
/// The registry is `scripts/fleet_knobs/registry.py`.  The engine writes two forms of it:
/// `scripts/fleet_knobs/registry.json`,  which carries every field the panel shows (who reads
/// a knob,  on which machine,  its plain name,  its default and which way a change moves it),
/// and the human-readable table in `INFISICAL.md`.  Both are generated and pinned by CI,  so
/// the app reads them and never guesses a knob that is not there.  The JSON is read first,
/// and the table is the fallback for a checkout that predates it.
///
/// A knob's current value comes from one of two places,  and the panel says which:  the
/// engine's own live read of Infisical (`fleet_knobs status --json --values`,  run on
/// Refresh),  or this Mac's last-known-good copy,  which holds the non-secret knobs only.
/// The copy is read under the engine's own rules,  so a copy the engine would refuse is not
/// a reading here either.  A secret's value is never read by the app:  the live read says
/// only whether it is set.
struct KnobRegistry {
    /// One knob,  as the registry declares it.
    struct Knob: Identifiable {
        let name: String
        /// The workstream code the registry files the knob under.
        let owner: String
        /// The kind of value it takes,  with the choices or the bounds.
        let kind: String
        /// The value it holds when nothing has changed it,  as the registry writes it.
        let defaultValue: String
        /// Whether the knob holds a secret.
        let isSecret: Bool
        /// The secret column,  as the registry wrote it.
        ///
        /// The column is "no" or "secret".  The panel treats anything else as a
        /// secret,  and this is the raw text,  so a change in the generator's
        /// wording shows up in a test rather than in a value on the screen.
        let secretColumn: String
        /// How far a local file may move it,  as the table words it.
        let localMay: String
        /// What the knob does.
        let note: String
        /// The value it holds now,  and where that value came from.
        var current: CurrentValue
        /// The knob's plain name,  in Title Case.  Empty for a registry that predates it.
        var label: String = ""
        /// The plain section the knob sits in.  Empty for a registry that predates it.
        var group: String = ""
        /// The machines that run a job that reads it.
        var hosts: KnobHosts = .unknown
        /// The jobs that read it,  in plain words.
        var consumers: [String] = []
        /// The registry's own kind word:  str,  int,  bool,  enum,  list or caps.
        var baseKind: String = ""
        /// An enum's choices,  most conservative first.
        var choices: [String] = []
        /// A bool's conservative value.
        var safe: Bool? = nil
        /// An int's narrowing direction:  "min" when a lower number is narrower.
        var narrower: String? = nil
        var minimum: Int? = nil
        var maximum: Int? = nil
        /// What an int counts,  such as hours or milliseconds.
        var unit: String = ""
        /// The stepper's step for an int.
        var step: Int = 1
        /// A caps knob's cap names.
        var capNames: [String] = []
        /// Values that do less than their name says until another knob is set.
        var requires: [KnobRequirement] = []
        /// What a risky value makes the fleet do,  by value.
        var consequences: [String: String] = [:]
        /// The engine's check for a str knob,  the format in words and one value it accepts.
        var pattern: String = ""
        var format: String = ""
        var example: String = ""
        /// True when the value is meant to be set once.
        var once: Bool = false
        /// True when the reader treats the default as "not set".
        var requiresExplicit: Bool = false
        /// The local-file rule as a sentence.  Empty for a registry that predates it.
        var localRule: String = ""
        /// The default in the engine's canonical text,  when the registry gives it.
        var canonicalDefaultText: String? = nil
        /// Where the value came from.
        var source: KnobValueSource = .notRead

        var id: String { name }

        /// The text the panel shows in the "Now" column.
        ///
        /// It is the one renderer,  so the panel and the model cannot say two
        /// different things about the same knob.  A knob whose reader treats the default
        /// as not set (a compliance threshold) says so,  rather than reading as configured.
        var nowText: String {
            if requiresExplicit, case .defaultValue = current {
                return "not set, so its rule reads unknown (suggested \(canonicalDefault))"
            }
            return current.displayText
        }

        /// The value a knob holds now.
        enum CurrentValue: Equatable {
            /// A reading carries a value for it.
            case current(String)
            /// It has never been set,  so it holds its default.
            ///
            /// The value the registry declares rides along here,  because a column
            /// that says only "default" does not tell the operator what the knob is
            /// holding.
            case defaultValue(String)
            /// It is a secret,  and nothing has said whether it is set.
            case secretUnconfirmed
            /// It is a secret,  and the engine's live read found it set.
            case secretSet
            /// It is a secret,  and the engine's live read found it missing,  with why.
            case secretMissing(String)
            /// Nothing could be read,  so the value is not known.
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
                case .secretSet: return "set (secret)"
                case .secretMissing(let reason): return "missing (\(reason))"
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
        /// The registry default,  shown on the row and in the editor.
        var defaultValue: String = ""
        /// The editor the knob gets,  or nil for a secret,  which is never edited here.
        var editor: KnobEditorKind? = nil
        /// The value an editor starts from.
        var editValue: String = ""
        /// The knob itself,  for what the editor works out from it.
        var knob: Knob? = nil

        var id: String { name }

        /// The title the owner reads:  the plain label,  or the key for an older registry.
        var title: String {
            let label = knob?.label ?? ""
            return label.isEmpty ? name : label
        }
    }

    /// The knobs,  in the order the registry lists them.
    var knobs: [Knob]
    /// The age of the copy,  or why there is no copy to read.
    let copyAge: String
    /// True when a readable copy is older than the engine rewrites it.
    let copyIsStale: Bool
    /// The sentence the panel shows about the copy.
    let copyHeadline: String
    /// How many rows of the generated block were there but could not be read.
    let unreadableRows: Int
    /// Why the registry itself could not be read,  when it could not.
    let problem: String?
    /// True when this Mac's copy was there and readable.
    var copyIsReadable: Bool = false
    /// When this Mac's copy was saved,  when it was readable.
    var copySavedAt: Date? = nil
    /// Which file the knob list came from.
    var registryFile: String = ""
    /// The engine's live read,  once Refresh has asked for it.
    var live: KnobLiveStatus? = nil

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
                defaultValue: knob.defaultValue,
                editor: knob.editor,
                editValue: knob.isSecret ? "" : knob.editValue,
                knob: knob
            )
        }
    }

    /// True when some reading of the values was had:  the live read or this Mac's copy.
    var valuesAreKnown: Bool {
        if let live, live.fetchOK { return true }
        return copyIsReadable || (live?.rows.isEmpty == false)
    }

    /// True when the engine said it could not write from here.
    var writesUnavailable: Bool { live?.writeToken == .missing }

    /// The knobs that are changed from their default,  for the header's count.
    var changedCount: Int { knobs.filter { $0.isChanged }.count }

    /// The knobs that hold a value wider than their default,  for the header's count.
    var widerCount: Int { knobs.filter { $0.isWiderThanDefault }.count }

    /// This registry with the engine's live read applied.
    ///
    /// A knob the engine did not answer for keeps what it had.  A value the engine read
    /// from Infisical says so;  one it took from this Mac's copy says that instead;  and a
    /// knob whose only answer is a fallback default with no copy behind it is not known.
    func applying(_ live: KnobLiveStatus) -> KnobRegistry {
        var copy = self
        copy.live = live
        copy.knobs = knobs.map { knob in
            var fresh = knob
            guard let row = live.rows[knob.name] else { return fresh }
            if knob.isSecret {
                if live.fetchOK {
                    fresh.current = row.isSet ? .secretSet : .secretMissing(row.reason ?? "unset")
                    fresh.source = .infisical
                } else {
                    fresh.current = .secretUnconfirmed
                    fresh.source = .notRead
                }
                return fresh
            }
            switch row.source {
            case "infisical":
                fresh.current = .current(knob.canonical(row.value ?? "") ?? row.value ?? "")
                fresh.source = .infisical
            case "lkg":
                fresh.current = .current(knob.canonical(row.value ?? "") ?? row.value ?? "")
                fresh.source = .macCopy
            case "default":
                if live.fetchOK {
                    fresh.current = .defaultValue(knob.defaultValue)
                    fresh.source = .infisical
                } else if (row.reason ?? "").contains(",lkg-") {
                    // The engine fell back to its default with no copy behind it, which says
                    // nothing about what Infisical holds.
                    fresh.current = .unknown("not known:  Infisical was not read and this Mac has no copy")
                    fresh.source = .notRead
                } else {
                    // The copy was there and does not carry the knob, so it was unset at
                    // the copy's last good read.
                    fresh.current = .defaultValue(knob.defaultValue)
                    fresh.source = .macCopy
                }
            default:
                break
            }
            return fresh
        }
        return copy
    }

    // MARK: Filters

    /// The knobs that pass a filter,  a machine filter and a search,  in registry order.
    func filtered(_ filter: KnobFilter, host: KnobHostFilter, query: String) -> [Row] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return rows.filter { row in
            guard let knob = row.knob else { return false }
            guard filter.includes(knob), host.includes(knob.hosts) else { return false }
            guard !needle.isEmpty else { return true }
            let haystack = [knob.name, knob.label, knob.group, knob.note, row.now]
                + knob.consumers
            return haystack.contains { $0.lowercased().contains(needle) }
        }
    }

    /// Why a filter shows nothing,  in a sentence.
    ///
    /// An empty "Changed" list with no reading behind it is not an all-clear,  so it says
    /// the values are not known rather than that nothing is changed.
    func emptyMessage(_ filter: KnobFilter, host: KnobHostFilter, query: String) -> String {
        if filter == .changed && !valuesAreKnown {
            return "The values could not be read, so which knobs are changed is not known."
        }
        if !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "No knob matches this search."
        }
        if filter == .changed {
            return "Every knob holds its default."
        }
        return "No knob matches this filter."
    }

    /// The rows,  grouped in the order their groups first appear.
    static func grouped(_ rows: [Row]) -> [KnobGroup] {
        var order: [String] = []
        var groups: [String: [Row]] = [:]
        for row in rows {
            let title = (row.knob?.group).flatMap { $0.isEmpty ? nil : $0 } ?? "Knobs"
            if groups[title] == nil { order.append(title) }
            groups[title, default: []].append(row)
        }
        return order.map { KnobGroup(title: $0, rows: groups[$0] ?? []) }
    }

    // MARK: What the panel says about where the values came from

    /// The lines that say where the values came from and how old they are.
    func freshnessLines() -> [KnobFreshnessLine] {
        var lines: [KnobFreshnessLine] = []
        if let live {
            let time = FleetTime.clock(live.checkedAt)
            if live.fetchOK {
                lines.append(KnobFreshnessLine(
                    text: "Values read from Infisical at \(time).", isWarning: false))
            } else {
                let why = KnobRegistry.describeFetchReason(live.fetchReason)
                lines.append(KnobFreshnessLine(
                    text: "Infisical was not read at \(time):  \(why).", isWarning: true))
            }
        }
        if live?.fetchOK != true {
            lines.append(KnobFreshnessLine(text: copyHeadline, isWarning: copyIsStale || !copyIsReadable))
            if copyIsReadable && copyIsStale {
                lines.append(KnobFreshnessLine(text: "It may not be current.", isWarning: true))
            }
        }
        return lines
    }

    /// A fetch reason token,  in words.
    static func describeFetchReason(_ token: String?) -> String {
        guard let token, !token.isEmpty else { return "the engine gave no reason" }
        if token.hasPrefix("recent-") {
            let inner = describeFetchReason(String(token.dropFirst("recent-".count)))
            return "a read failed less than a minute ago, and the engine waits a minute before it tries again (\(inner))"
        }
        if token.hasPrefix("error-") {
            return "the engine hit an error (\(token.dropFirst("error-".count)))"
        }
        switch token {
        case "service-token-required":
            return "Muster runs the knob engine without the knob reader's Infisical token, so it cannot read Infisical"
        case "scoped-reader-unavailable":
            return "the fleet-secrets reader the engine needs is not installed on this Mac"
        case "timeout":
            return "Infisical did not answer within 8 seconds"
        case "scoped-read-failed":
            return "the read was refused or failed"
        case "environment-refused":
            return "the environment names an Infisical environment other than prod"
        case "empty":
            return "Infisical answered with none of the knobs, which is what a wrong path or a broken login looks like"
        case "unresolved-reference":
            return "a knob holds a reference Infisical could not resolve"
        default:
            return "the read failed (\(token))"
        }
    }

    /// Why the values or the list are not shown,  as whole sentences,  one per cause.
    static func problemSentences(engineReason: String?, registryProblem: String?, liveProblem: String?) -> [String] {
        var out: [String] = []
        if let engineReason, !engineReason.isEmpty {
            if engineReason == "the engine was not found" {
                out.append("Muster could not find the engine checkout, so knob values are not shown.")
            } else if engineReason.hasPrefix("the engine did not say which project its copy belongs to") {
                out.append("This Mac's copy of the knobs is not read, because the engine did not say which Infisical project the copy belongs to.")
            } else if engineReason.hasPrefix("the engine could not be asked") {
                let detail = engineReason.components(separatedBy: ":").dropFirst().joined(separator: ":")
                    .trimmingCharacters(in: .whitespaces)
                out.append("Muster could not start the engine\(detail.isEmpty ? "" : " (\(detail))"), so this Mac's copy of the knobs is not read.")
            } else {
                out.append(sentence(engineReason))
            }
        }
        if let registryProblem, !registryProblem.isEmpty {
            if registryProblem == "the registry block is not in the file" {
                out.append("INFISICAL.md in the engine checkout has no knob table, so no knob is listed.")
            } else if registryProblem.contains("not finished") {
                out.append("The knob table in INFISICAL.md ends before its end marker, so no knob is listed.")
            } else {
                out.append(sentence(registryProblem) + "  No knob is listed.")
            }
        }
        if let liveProblem, !liveProblem.isEmpty {
            out.append(sentence(liveProblem))
        }
        return out.map { ApplyCopy.normalize($0) }
    }

    /// A fragment as a sentence:  a capital first letter and a full stop.
    static func sentence(_ fragment: String) -> String {
        let trimmed = fragment.trimmingCharacters(in: .whitespaces)
        guard let first = trimmed.first else { return "" }
        let body = first.uppercased() + trimmed.dropFirst()
        return body.hasSuffix(".") ? body : body + "."
    }
}

/// The knobs of one group,  such as Lane Sweep,  in registry order.
struct KnobGroup: Identifiable {
    let title: String
    let rows: [KnobRegistry.Row]
    var id: String { title }
}

/// One line about where the values came from.
struct KnobFreshnessLine: Equatable {
    let text: String
    let isWarning: Bool
}

/// Which machines run a job that reads a knob.
enum KnobHosts: String, Equatable {
    case mac, server, both, unknown

    init(registryValue: String) {
        self = KnobHosts(rawValue: registryValue) ?? .unknown
    }

    /// The chip on a row.
    var title: String {
        switch self {
        case .mac: return "The Mac"
        case .server: return "The Server"
        case .both: return "The Mac And The Server"
        case .unknown: return "No Known Reader"
        }
    }

    /// The same,  inside a sentence.
    var inProse: String {
        switch self {
        case .mac: return "the Mac"
        case .server: return "the server"
        case .both: return "the Mac and the server"
        case .unknown: return "no machine this repository knows of"
        }
    }
}

/// Where a knob's value came from.
enum KnobValueSource: Equatable {
    /// The engine read Infisical just now.
    case infisical
    /// This Mac's last-known-good copy.
    case macCopy
    /// Nothing has been read.
    case notRead

    /// The tag beside a value.
    var tag: String {
        switch self {
        case .infisical: return "Infisical"
        case .macCopy: return "This Mac's Copy"
        case .notRead: return "Not Read"
        }
    }
}

/// A value that does less than its name says until another knob is set.
struct KnobRequirement: Equatable {
    let value: String
    let knob: String
}

/// The kinds of knob the filter picks between.
enum KnobFilter: String, CaseIterable, Identifiable {
    case all = "All Knobs"
    case changed = "Changed"
    case choices = "Modes And Switches"
    case numbers = "Numbers And Caps"
    case text = "Lists And Text"

    var id: String { rawValue }

    /// True when the knob belongs under this filter.
    ///
    /// The filter reads the registry's own kind,  not words in the kind text,  so every
    /// enum is a mode and no filter is empty by construction.
    func includes(_ knob: KnobRegistry.Knob) -> Bool {
        switch self {
        case .all: return true
        case .changed: return knob.isChanged
        case .choices: return ["bool", "enum"].contains(knob.effectiveKind)
        case .numbers: return ["int", "caps"].contains(knob.effectiveKind)
        case .text: return ["str", "list"].contains(knob.effectiveKind)
        }
    }
}

/// The machines the filter picks between.
enum KnobHostFilter: String, CaseIterable, Identifiable {
    case any = "Any Machine"
    case mac = "The Mac"
    case server = "The Server"
    case noReader = "No Known Reader"

    var id: String { rawValue }

    func includes(_ hosts: KnobHosts) -> Bool {
        switch self {
        case .any: return true
        case .mac: return hosts == .mac || hosts == .both
        case .server: return hosts == .server || hosts == .both
        case .noReader: return hosts == .unknown
        }
    }
}

/// Which way a change moves a knob,  from the registry's own order.
enum KnobDirection: Equatable {
    case same, wider, narrower, mixed, none, unknown

    /// The sentence the editor shows,  or nil when there is nothing to say.
    var sentence: String? {
        switch self {
        case .wider: return "This makes the fleet do more than it does now."
        case .narrower: return "This makes the fleet do less than it does now."
        case .mixed: return "This raises some limits and lowers others."
        case .same, .none, .unknown: return nil
        }
    }
}

// MARK: - The engine's live read

/// What `python3 -m fleet_knobs status --json --values` answered.
struct KnobLiveStatus: Equatable {
    /// One knob's answer.  A secret's row carries no value.
    struct Row: Equatable {
        let name: String
        let isSet: Bool
        let source: String
        let stale: Bool
        let value: String?
        let reason: String?
    }

    /// Whether a write could run where the engine ran.
    enum WriteToken: Equatable { case present, missing, unknown }

    /// The shape this app reads.
    static let supportedSchema = 1

    let fetchOK: Bool
    let fetchReason: String?
    let checkedAt: Date
    let writeToken: WriteToken
    let rows: [String: Row]

    /// The answer,  or nil when it is not one this app reads.
    static func parse(_ data: Data, fallbackTime: Date = Date()) -> KnobLiveStatus? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            (object["schema"] as? NSNumber)?.intValue == supportedSchema,
            let fetch = object["fetch"] as? String,
            let knobs = object["knobs"] as? [[String: Any]]
        else { return nil }
        var rows: [String: Row] = [:]
        for entry in knobs {
            guard let name = entry["name"] as? String, !name.isEmpty else { return nil }
            rows[name] = Row(
                name: name,
                isSet: (entry["set"] as? Bool) ?? false,
                source: (entry["source"] as? String) ?? "unknown",
                stale: (entry["stale"] as? Bool) ?? true,
                value: entry["value"] as? String,
                reason: entry["reason"] as? String
            )
        }
        let checked = (object["checked_epoch"] as? NSNumber)
            .map { Date(timeIntervalSince1970: $0.doubleValue) } ?? fallbackTime
        let token: WriteToken
        switch object["write_token"] as? String {
        case "present": token = .present
        case "missing": token = .missing
        default: token = .unknown
        }
        return KnobLiveStatus(
            fetchOK: fetch == "ok",
            fetchReason: object["fetch_reason"] as? String,
            checkedAt: checked,
            writeToken: token,
            rows: rows
        )
    }
}

/// What asking the engine for a live read came to.
enum KnobLiveReading: Equatable {
    case read(KnobLiveStatus)
    case failed(String)
}

/// Runs `python3 -m fleet_knobs status --json --values` and reads its answer.
///
/// The engine runs with the same child environment as every other engine run,  which
/// carries no credential:  Muster holds none.  The answer never carries a secret's value.
enum KnobStatusReader {
    static let arguments = ["-s", "-m", "fleet_knobs", "status", "--json", "--values"]

    static func read(
        location: EngineLocation,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        timeout: TimeInterval = 40
    ) -> KnobLiveReading {
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        process.executableURL = location.interpreter
        process.arguments = arguments
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = errors
        process.environment = EngineRunner.childEnvironment(location: location, home: home)
        do {
            try process.run()
        } catch {
            return .failed("Muster could not start the knob engine (\(error.localizedDescription)).")
        }
        let collected = Collected()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global().async {
            collected.output = output.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }
        group.enter()
        DispatchQueue.global().async {
            collected.errors = errors.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
            group.wait()
            return .failed("The knob engine did not answer within \(Int(timeout)) seconds.")
        }
        group.wait()
        if let status = KnobLiveStatus.parse(collected.output) {
            return .read(status)
        }
        let complaint = String(data: collected.errors, encoding: .utf8) ?? ""
        let first = complaint.split(separator: "\n").last.map(String.init) ?? ""
        let because = first.isEmpty ? "" : " (\(first.prefix(160)))"
        return .failed("The knob engine's answer could not be read\(because).")
    }

    private final class Collected: @unchecked Sendable {
        var output = Data()
        var errors = Data()
    }
}

// MARK: - Reading the registry and the copy

enum KnobReader {
    /// The generated block in `INFISICAL.md` that carries the registry.
    static let blockStart = "<!-- fleet-knobs:begin"
    static let blockEnd = "<!-- fleet-knobs:end -->"
    /// The generated inventory,  relative to the repository root.
    static let inventoryPath = "scripts/fleet_knobs/registry.json"
    /// The inventory shape this app reads.
    static let inventorySchema = 1

    /// How many columns a row of the generated block has.
    ///
    /// The block is a table of seven:  key,  owner,  kind,  default,  secret,  the
    /// direction a local file may move it,  and what the knob does.  A row with any
    /// other number of columns is not a row,  and a row that is not read is a knob
    /// the panel would not show,  so the count of them is reported.
    static let columnCount = 7

    /// Read the registry and the current values.
    ///
    /// The inventory JSON is read first;  a missing one,  or one of a shape this app does
    /// not know,  falls back to the table in `INFISICAL.md`.  A missing or unreadable file
    /// yields an empty registry and the reason,  and never a guessed knob.
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
        let copy = readCopy(in: home, identity: identity)
        let declared: [KnobRegistry.Knob]
        let unreadable: Int
        let problem: String?
        let file: String
        if let inventory = readInventory(at: repoRoot.appendingPathComponent(inventoryPath)) {
            declared = inventory
            unreadable = 0
            problem = nil
            file = inventoryPath
        } else {
            let table = readTable(in: repoRoot.appendingPathComponent("INFISICAL.md"))
            declared = table.rows.map(knob(fromTableRow:))
            unreadable = table.unreadable
            problem = table.problem
            file = "INFISICAL.md"
        }
        let knobs = declared.map { declaredKnob -> KnobRegistry.Knob in
            var knob = declaredKnob
            if knob.isSecret {
                // The copy holds the non-secret knobs only,  so a secret is never in it and its
                // absence says nothing about whether the secret is set.  The panel says the
                // presence is unconfirmed rather than reporting a secret as not set,  which
                // would be a false statement about a credential that is very likely set.
                knob.current = .secretUnconfirmed
                knob.source = .notRead
            } else if copy.isReadable, let value = copy.knobs[knob.name] {
                // The copy keeps the raw text Infisical held,  so a value typed by hand
                // (`yes`,  `ACT`) is shown in the engine's own form.
                knob.current = .current(knob.canonical(value) ?? value)
                knob.source = .macCopy
            } else if copy.isReadable {
                // The copy is there and it does not carry this knob.  The engine
                // writes the copy after a good fetch and leaves out every knob that
                // is unset,  so a knob missing from a copy the engine wrote holds
                // whatever the registry says it holds.
                knob.current = .defaultValue(knob.defaultValue)
                knob.source = .macCopy
            } else {
                // There is no copy to read,  so the value is not known.  Saying
                // "default" here would claim the knob equals its default,  which
                // is a statement the app cannot make.  A copy the engine would
                // refuse carries no reading at all,  expired or not.
                knob.current = .unknown(copy.reason ?? "the copy could not be read")
                knob.source = .notRead
            }
            return knob
        }
        var registry = KnobRegistry(
            knobs: knobs,
            copyAge: copy.ageText,
            copyIsStale: copy.isStale,
            copyHeadline: copy.headline,
            unreadableRows: unreadable,
            problem: problem
        )
        registry.copyIsReadable = copy.isReadable
        registry.copySavedAt = copy.savedAt
        registry.registryFile = file
        return registry
    }

    /// One knob from a row of the table,  which carries fewer fields than the inventory.
    private static func knob(fromTableRow row: Row) -> KnobRegistry.Knob {
        let kind = row.value(at: 2)
        // The secret column fails closed.  A column that says anything other than "no" is
        // treated as a secret,  so a wording the app does not know cannot have a secret's
        // value shown in plain text.
        let secret = row.value(at: 4).lowercased() != "no"
        var knob = KnobRegistry.Knob(
            name: row.value(at: 0),
            owner: row.value(at: 1),
            kind: kind,
            defaultValue: row.value(at: 3),
            isSecret: secret,
            secretColumn: row.value(at: 4),
            localMay: row.value(at: 5),
            note: row.value(at: 6),
            current: .unknown("not read")
        )
        knob.baseKind = KnobRegistry.Knob.baseKind(fromKindText: kind)
        if case .choice(let choices) = KnobEditorKind(kind: kind) { knob.choices = choices }
        if case .integer(let low, let high) = KnobEditorKind(kind: kind) {
            knob.minimum = low
            knob.maximum = high
        }
        return knob
    }

    /// The knobs in the generated inventory,  or nil to fall back to the table.
    ///
    /// A file that is missing,  unreadable,  of another schema or with any entry this app
    /// cannot read is not used at all:  a partial inventory would show a few knobs and call
    /// the set complete.
    static func readInventory(at url: URL) -> [KnobRegistry.Knob]? {
        guard
            let data = try? Data(contentsOf: url),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            (object["schema"] as? NSNumber)?.intValue == inventorySchema,
            let entries = object["knobs"] as? [[String: Any]],
            !entries.isEmpty
        else { return nil }
        var out: [KnobRegistry.Knob] = []
        for entry in entries {
            guard let knob = inventoryKnob(entry) else { return nil }
            out.append(knob)
        }
        return out
    }

    private static func inventoryKnob(_ entry: [String: Any]) -> KnobRegistry.Knob? {
        guard
            let name = entry["name"] as? String, !name.isEmpty,
            let kindText = entry["kind_text"] as? String,
            let baseKind = entry["kind"] as? String
        else { return nil }
        func text(_ key: String) -> String { (entry[key] as? String) ?? "" }
        func int(_ key: String) -> Int? { (entry[key] as? NSNumber)?.intValue }
        // A missing secret flag fails closed.
        let secret = (entry["secret"] as? Bool) ?? true
        let defaultText = text("default_text")
        var knob = KnobRegistry.Knob(
            name: name,
            owner: text("owner"),
            kind: kindText,
            defaultValue: secret ? "" : defaultText,
            isSecret: secret,
            secretColumn: secret ? "secret" : "no",
            localMay: text("local_rule"),
            note: text("note"),
            current: .unknown("not read")
        )
        knob.label = text("label")
        knob.group = text("group")
        knob.hosts = KnobHosts(registryValue: text("hosts"))
        knob.consumers = ((entry["consumers"] as? [[String: Any]]) ?? []).compactMap { $0["name"] as? String }
        knob.baseKind = baseKind
        knob.choices = (entry["choices"] as? [String]) ?? []
        knob.safe = entry["safe"] as? Bool
        knob.narrower = entry["narrower"] as? String
        knob.minimum = int("minimum")
        knob.maximum = int("maximum")
        knob.unit = text("unit")
        knob.step = max(int("step") ?? 1, 1)
        knob.capNames = (entry["cap_names"] as? [String]) ?? []
        knob.requires = ((entry["requires"] as? [[String: Any]]) ?? []).compactMap { item in
            guard let value = item["value"] as? String, let other = item["knob"] as? String else { return nil }
            return KnobRequirement(value: value, knob: other)
        }
        var consequences: [String: String] = [:]
        for item in (entry["consequences"] as? [[String: Any]]) ?? [] {
            if let value = item["value"] as? String, let sentence = item["text"] as? String {
                consequences[value] = sentence
            }
        }
        knob.consequences = consequences
        knob.pattern = secret ? "" : text("pattern")
        knob.format = text("format")
        knob.example = text("example")
        knob.once = (entry["once"] as? Bool) ?? false
        knob.requiresExplicit = (entry["requires_explicit"] as? Bool) ?? false
        knob.localRule = text("local_rule")
        knob.canonicalDefaultText = secret ? nil : entry["default"] as? String
        return knob
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
        /// True when a readable copy is older than the engine rewrites it.
        let isStale: Bool
        /// False when there is no copy to read,  or the engine would refuse it.
        let isReadable: Bool
        /// Why the copy is not readable,  when it is not.
        let reason: String?
        /// The sentence the panel shows about the copy.
        let headline: String
        /// When the copy was saved,  when it is readable.
        var savedAt: Date? = nil
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

    /// What the panel says when this Mac has no copy at all.
    static let noCopyHeadline =
        "This Mac has no copy of the knobs yet.\(ApplyCopy.sentenceGap)A job on this Mac writes one after it reads Infisical."

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
                isStale: false,
                isReadable: false,
                reason: "no local copy",
                headline: noCopyHeadline
            )
        }
        guard isPrivateFile(at: path, in: directory) else {
            return Copy(
                knobs: [:],
                ageText: "no usable copy",
                isStale: false,
                isReadable: false,
                reason: "the copy is not a private file of this user",
                headline: "This Mac's copy of the knobs is not a private file of this user, so it is not read."
            )
        }
        guard
            let data = try? Data(contentsOf: path),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return Copy(
                knobs: [:],
                ageText: "no usable copy",
                isStale: false,
                isReadable: false,
                reason: "the copy could not be parsed",
                headline: "This Mac's copy of the knobs could not be parsed, so it is not read."
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
                isStale: false,
                isReadable: false,
                reason: "the engine did not say which project its copy belongs to",
                headline: "This Mac's copy is not read, because the engine did not say which Infisical project it belongs to."
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
                isStale: false,
                isReadable: false,
                reason: "the copy is not one the engine wrote",
                headline: "This Mac's copy of the knobs is not one the engine wrote, so it is not read."
            )
        }
        // A time that is not a number a clock could hold is not a time.  A value such
        // as 1e300 is valid JSON and would trap the conversion to an integer,  so it
        // is refused before anything tries to read it as an age.
        guard saved.isFinite, saved > 0, saved < 4_102_444_800 else {
            return Copy(
                knobs: [:],
                ageText: "no usable copy",
                isStale: false,
                isReadable: false,
                reason: "the copy has no time this app can read",
                headline: "This Mac's copy of the knobs has no time this app can read, so it is not read."
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
                isStale: false,
                isReadable: false,
                reason: "the copy is past its expiry",
                headline: "This Mac's copy of the knobs is more than two days old, so the engine and this panel ignore it."
            )
        }
        let ageText = age <= 0 ? "just now" : FleetTime.age(Int(age))
        let savedAt = Date(timeIntervalSince1970: saved)
        let ago = age <= 0 ? "just now" : "\(ageText) ago"
        return Copy(
            knobs: values,
            ageText: ageText,
            isStale: stale,
            isReadable: true,
            reason: nil,
            headline: "Values are from this Mac's copy, saved at \(FleetTime.clock(savedAt)) (\(ago)).",
            savedAt: savedAt
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
/// `int 1 to 168`,  `str`,  `list` and `caps`.  A kind this app does not know gets a text
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
    /// One number per named cap,  for a caps knob whose names the registry gives.
    case caps([String])

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

    /// True when the text is a number the engine takes:  one to nine digits,  no sign.
    static func isEngineNumber(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 9 && value.unicodeScalars.allSatisfy { $0.value >= 48 && $0.value <= 57 }
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
            // The engine takes digits only,  so `+5` and `-1` are refused here as there.
            guard Self.isEngineNumber(value), let number = Int(value) else {
                return "The value must be a whole number of up to nine digits."
            }
            if let low = low, number < low { return "The value must be at least \(low)." }
            if let high = high, number > high { return "The value must be at most \(high)." }
            return nil
        case .text:
            if value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) {
                return "The value must be one line."
            }
            return nil
        case .caps(let names):
            let pairs = value.split(separator: ",").map { $0.split(separator: "=", maxSplits: 1).map(String.init) }
            for pair in pairs {
                guard pair.count == 2, names.contains(pair[0]) else {
                    return "Every cap must be one of \(names.joined(separator: ", "))."
                }
                guard Self.isEngineNumber(pair[1]) else {
                    return "Every cap needs a whole number."
                }
            }
            return nil
        }
    }
}

extension KnobRegistry.Knob {
    /// The registry's kind word,  from the inventory or worked out from the kind text.
    var effectiveKind: String {
        baseKind.isEmpty ? Self.baseKind(fromKindText: kind) : baseKind
    }

    /// The kind word for a kind text such as `enum: off < report` or `int 1 to 168`.
    static func baseKind(fromKindText text: String) -> String {
        let lower = text.trimmingCharacters(in: .whitespaces).lowercased()
        if lower.hasPrefix("enum") { return "enum" }
        if lower == "int" || lower.hasPrefix("int ") { return "int" }
        if ["bool", "list", "caps", "str"].contains(lower) { return lower }
        return "str"
    }

    /// The editor this knob gets,  or nil for a secret:  a secret is never edited here,
    /// and the knob engine refuses one anyway.
    var editor: KnobEditorKind? {
        if isSecret { return nil }
        if effectiveKind == "caps", !capNames.isEmpty { return .caps(capNames) }
        return KnobEditorKind(kind: kind)
    }

    /// The value an editor starts from:  the value the knob holds now,  or its default.
    ///
    /// The registry writes an empty default as a word (`empty`,  `unset`,  `none`),  and
    /// the editor starts from no text at all for those.  A value is put in the engine's
    /// own form first,  so a `yes` typed in Infisical opens the toggle on.
    var editValue: String {
        if case .current(let value) = current { return canonical(value) ?? value }
        return canonicalDefault
    }

    /// The default in the engine's canonical text.
    var canonicalDefault: String {
        if let canonicalDefaultText { return canonicalDefaultText }
        let plain = Self.plainDefault(defaultValue)
        return canonical(plain) ?? plain
    }

    /// True when Infisical holds a value for it,  as far as a reading says.
    var isSet: Bool {
        if case .current = current { return true }
        return false
    }

    /// True when the value a reading carries differs from the default.
    var isChanged: Bool {
        guard case .current(let value) = current else { return false }
        return (canonical(value) ?? value) != canonicalDefault
    }

    /// True when the value now makes the fleet do more than the default does.
    var isWiderThanDefault: Bool {
        guard case .current(let value) = current else { return false }
        let way = direction(from: canonicalDefault, to: value)
        return way == .wider || way == .mixed
    }

    /// A registry default,  with the words that mean "nothing" turned into no text.
    static func plainDefault(_ value: String) -> String {
        let lower = value.lowercased()
        // `none` alone or followed by a note (`none (uses the built-in list)`),  never a
        // word that only starts with it,  such as `nonempty`.
        let noneWithNote = ["none ", "none,", "none;", "none("].contains { lower.hasPrefix($0) }
        if lower == "empty" || lower == "unset" || lower == "none" || noneWithNote {
            return ""
        }
        return value
    }

    /// A value in the engine's canonical text,  or nil when the engine would refuse it.
    ///
    /// It follows `scripts/fleet_knobs/values.py`:  a bool takes true,  1,  yes and on (and
    /// their opposites) in any case,  an enum is lower case,  an int is digits only,  a list
    /// drops repeats,  and caps are sorted by name.
    func canonical(_ raw: String) -> String? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        switch effectiveKind {
        case "bool":
            let low = text.lowercased()
            if ["true", "1", "yes", "on"].contains(low) { return "true" }
            if ["false", "0", "no", "off"].contains(low) { return "false" }
            return nil
        case "enum":
            let low = text.lowercased()
            return choices.isEmpty || choices.contains(low) ? low : nil
        case "int":
            guard KnobEditorKind.isEngineNumber(text), let number = Int(text) else { return nil }
            return String(number)
        case "list":
            var seen: [String] = []
            for item in Self.tokens(text) where !seen.contains(item) { seen.append(item) }
            return seen.joined(separator: ",")
        case "caps":
            guard let caps = Self.caps(text) else { return nil }
            return caps.keys.sorted().map { "\($0)=\(caps[$0] ?? 0)" }.joined(separator: ",")
        default:
            return text
        }
    }

    /// Items split on commas and white space,  as the engine splits them.
    static func tokens(_ text: String) -> [String] {
        text.split(whereSeparator: { $0 == "," || $0.isWhitespace }).map(String.init)
    }

    /// `a=1,b=2` as a map,  or nil when a pair is not a name and a number.
    static func caps(_ text: String) -> [String: Int]? {
        var out: [String: Int] = [:]
        for token in tokens(text) {
            let parts = token.split(separator: "=", maxSplits: 1).map(String.init)
            guard parts.count == 2, !parts[0].isEmpty, KnobEditorKind.isEngineNumber(parts[1]),
                  let number = Int(parts[1]), out[parts[0]] == nil else { return nil }
            out[parts[0]] = number
        }
        return out
    }

    /// Which way a change moves this knob,  as `values.direction` in the engine says.
    func direction(from old: String, to new: String) -> KnobDirection {
        guard let before = canonical(old), let after = canonical(new) else { return .unknown }
        if before == after { return .same }
        switch effectiveKind {
        case "bool":
            guard let safe else { return .unknown }
            return (after == "true") == safe ? .narrower : .wider
        case "enum":
            guard let a = choices.firstIndex(of: before), let b = choices.firstIndex(of: after) else {
                return .unknown
            }
            return b < a ? .narrower : .wider
        case "int":
            guard let a = Int(before), let b = Int(after) else { return .unknown }
            let lower = b < a
            return lower == ((narrower ?? "min") == "min") ? .narrower : .wider
        case "list":
            let a = Self.tokens(before), b = Self.tokens(after)
            let added = b.contains { !a.contains($0) }
            let removed = a.contains { !b.contains($0) }
            if added && removed { return .mixed }
            return added ? .wider : .narrower
        case "caps":
            guard let a = Self.caps(before), let b = Self.caps(after) else { return .unknown }
            var moves: Set<String> = []
            for key in Set(a.keys).union(b.keys) {
                if b[key] == nil { moves.insert("mixed") }
                else if a[key] == nil { moves.insert("narrower") }
                else if let x = a[key], let y = b[key], y < x { moves.insert("narrower") }
                else if let x = a[key], let y = b[key], y > x { moves.insert("wider") }
            }
            if moves.count != 1 { return .mixed }
            switch moves.first ?? "mixed" {
            case "narrower": return .narrower
            case "wider": return .wider
            default: return .mixed
            }
        default:
            return .none
        }
    }

    /// Why a value cannot be planned,  or nil when it can.
    ///
    /// It holds the engine's own rules where the app can know them,  so Show Plan does not
    /// light up for a value the plan would then refuse.
    func problem(with value: String) -> String? {
        guard let editor else { return "A secret is never edited here." }
        if effectiveKind == "str" {
            if value.isEmpty { return "Use Reset To Default to clear this." }
            if !pattern.isEmpty, !Self.matches(value, pattern: pattern) {
                let what = format.isEmpty ? "in the format this knob takes" : format
                let like = example.isEmpty ? "" : "\(ApplyCopy.sentenceGap)For example:  \(example)"
                return "This is not \(what).\(like)"
            }
        }
        return editor.problem(with: value)
    }

    /// True when the whole value matches the engine's pattern.
    static func matches(_ value: String, pattern: String) -> Bool {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return true }
        let range = NSRange(value.startIndex..<value.endIndex, in: value)
        guard let match = regex.firstMatch(in: value, range: range) else { return false }
        return match.range == range
    }

    /// The consequence a value carries,  or nil.
    func consequence(for value: String) -> String? {
        consequences[canonical(value) ?? value]
    }

    /// The sentence for a requirement the value leaves unmet,  or nil.
    ///
    /// A requirement is unmet when the other knob holds nothing (an empty value or
    /// false);  when its value is not known,  the sentence says that instead.
    func unmetRequirement(for value: String, in registry: KnobRegistry) -> String? {
        let chosen = canonical(value) ?? value
        for requirement in requires where requirement.value == chosen {
            guard let other = registry.knobs.first(where: { $0.name == requirement.knob }) else { continue }
            switch other.current {
            case .current(let held):
                let canonicalHeld = other.canonical(held) ?? held
                if canonicalHeld.isEmpty || canonicalHeld == "false" {
                    return "\(chosen) also needs \(requirement.knob), which is not set, so it will do less than its name says."
                }
            case .defaultValue:
                if other.canonicalDefault.isEmpty || other.canonicalDefault == "false" {
                    return "\(chosen) also needs \(requirement.knob), which is not set, so it will do less than its name says."
                }
            case .unknown, .secretUnconfirmed, .secretSet, .secretMissing:
                return "\(chosen) also needs \(requirement.knob), and whether that is set is not known."
            }
        }
        return nil
    }

    /// Where a change lands,  in a sentence.
    var affectsText: String {
        if consumers.isEmpty || hosts == .unknown {
            return "No job in this repository reads this, so a change may do nothing until its reader is installed."
        }
        return "Read on \(hosts.inProse) by \(consumers.joined(separator: ", "))."
    }

    /// An int value in its unit,  such as `90 minutes` for 5400000 milliseconds.
    func unitText(for value: String) -> String? {
        guard effectiveKind == "int", !unit.isEmpty, let number = Int(value) else { return nil }
        if unit == "milliseconds" {
            if number % 60000 == 0 {
                let minutes = number / 60000
                return minutes == 1 ? "1 minute" : "\(minutes) minutes"
            }
            return "\(Double(number) / 1000) seconds"
        }
        return "\(number) \(unit)"
    }
}
