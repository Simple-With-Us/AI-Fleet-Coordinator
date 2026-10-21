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
    /// One knob, as the registry declares it.
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
        /// How far a local file may move it.
        let localMay: String
        /// What the knob does.
        let note: String
        /// The value it holds now,  and where that value came from.
        let current: CurrentValue

        var id: String { name }

        /// The value a knob holds now.
        enum CurrentValue: Equatable {
            /// The last-known-good copy has a value for it.
            case current(String)
            /// It has never been set,  so it holds its default.
            case defaultValue
            /// It is a secret,  and the copy never carries one,  so whether it is set
            /// is not something this reader can say.
            case secretUnconfirmed
            /// The copy could not be read,  so the value is not known.
            case unknown(String)

            /// The text the panel shows.
            var displayText: String {
                switch self {
                case .current(let value): return value
                case .defaultValue: return "default"
                case .secretUnconfirmed: return "not known (secret)"
                case .unknown(let reason): return reason
                }
            }
        }
    }

    /// The knobs,  in the order the registry lists them.
    let knobs: [Knob]
    /// When the last-known-good copy was written,  and whether it is fresh.
    let copyAge: String
    /// True when the copy is older than the engine's own freshness window.
    let copyIsStale: Bool
}

enum KnobReader {
    /// The generated block in `INFISICAL.md` that carries the registry.
    static let blockStart = "<!-- fleet-knobs:begin"
    static let blockEnd = "<!-- fleet-knobs:end -->"

    /// The last-known-good copy of the non-secret knobs.
    static let copyPath = "~/.local/state/fleet-knobs/lkg.json"

    /// Read the registry and the current values.
    ///
    /// The registry is a table in a markdown file,  so the read is a table read and not
    /// a parse of somebody's code.  A missing or unreadable file yields an empty
    /// registry and a reason,  and never a guessed knob.
    static func read(repoRoot: URL, home: URL) -> KnobRegistry {
        let table = readTable(in: repoRoot.appendingPathComponent("INFISICAL.md"))
        let copy = readCopy(in: home)
        let knobs = table.map { row -> KnobRegistry.Knob in
            let name = row.value(at: 0)
            let secret = row.value(at: 4).lowercased() == "secret"
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
                // The copy is there and it does not carry this knob,  so the knob
                // holds whatever the registry says it holds.
                current = .defaultValue
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
                defaultValue: row.value(at: 3),
                isSecret: secret,
                localMay: row.value(at: 5),
                note: row.value(at: 6),
                current: current
            )
        }
        return KnobRegistry(
            knobs: knobs,
            copyAge: copy.ageText,
            copyIsStale: copy.isStale
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

    /// The rows of the generated table.
    private static func readTable(in file: URL) -> [Row] {
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { return [] }
        var rows: [Row] = []
        var inside = false
        for line in text.components(separatedBy: "\n") {
            if line.hasPrefix(blockStart) {
                inside = true
                continue
            }
            if line.hasPrefix(blockEnd) { break }
            guard inside else { continue }
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("|") else { continue }
            let cells = trimmed.split(separator: "|", omittingEmptySubsequences: false)
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            // The header row and the rule under it are not knobs.
            guard cells.count >= 7, cells[0] != "Key" else { continue }
            if cells.allSatisfy({ $0.allSatisfy { $0 == "-" } }) { continue }
            rows.append(Row(cells: cells))
        }
        return rows
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
    }

    /// How often the engine rewrites an unchanged copy,  in seconds.
    ///
    /// These are the engine's own numbers,  from `scripts/fleet_knobs/lkg.py`:
    /// `REFRESH_SECONDS` and `MAX_AGE_SECONDS`.  A copy between one refresh and the
    /// expiry is stale,  and a copy past the expiry is not a reading of anything.
    static let refreshSeconds = 3600
    static let maxAgeSeconds = 48 * 3600

    /// The engine's own identity for a copy,  from `lkg.py` and `source.py`.
    ///
    /// The engine only trusts a copy that carries these,  so the panel reads it the
    /// same way:  a copy without them is not a reading of anything,  even when it
    /// parses and holds a plausible value.
    static let copySchema = 1
    static let copyProject = "9bf7417a-fbbb-42ca-870c-2b45207233f5"
    static let copyEnvironment = "prod"
    /// The engine treats a copy more than this far in the future as expired.
    static let futureToleranceSeconds = 300

    /// The copy the fleet's own tool wrote.
    ///
    /// It holds the non-secret knobs only,  and the tool refuses to write a secret
    /// value into it,  so reading it cannot put a secret on the screen.
    private static func readCopy(in home: URL) -> Copy {
        let path = home.appendingPathComponent(
            ".local/state/fleet-knobs/lkg.json",
            isDirectory: false
        )
        guard
            let data = try? Data(contentsOf: path),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return Copy(
                knobs: [:],
                ageText: "no local copy",
                isStale: true,
                isReadable: false,
                reason: "no local copy"
            )
        }
        // The engine accepts a copy only when it carries its own identity,  a map of
        // knobs,  and a time.  This reader takes the same conditions,  because a copy
        // the engine would refuse is not a reading of a knob,  and reading one anyway
        // would put a value on the screen that the fleet's own tools do not believe.
        let schema = (object["schema"] as? NSNumber)?.doubleValue
        let project = object["project"] as? String
        let environment = object["environment"] as? String
        let knobs = object["knobs"] as? [String: Any]
        let saved = (object["saved_epoch"] as? NSNumber)?.doubleValue
        guard
            schema == Double(copySchema),
            project == copyProject,
            environment == copyEnvironment,
            let knobs,
            let saved
        else {
            return Copy(
                knobs: [:],
                ageText: "not a copy this app knows",
                isStale: true,
                isReadable: false,
                reason: "the copy is not one the engine wrote"
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
        return Copy(
            knobs: values,
            ageText: age <= 0 ? "just now" : FleetTime.age(Int(age)),
            isStale: stale,
            isReadable: !expired,
            reason: expired ? "the copy is past its expiry" : nil
        )
    }
}
