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
/// non-secret knobs only.  A secret knob shows its presence and never its value.
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
            /// It is a secret,  and the panel never shows one.
            case secretPresent(Bool)
            /// The copy is too old to say,  or could not be read.
            case unknown(String)

            /// The text the panel shows.
            var displayText: String {
                switch self {
                case .current(let value): return value
                case .defaultValue: return "default"
                case .secretPresent(let present): return present ? "set" : "not set"
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
                let present = copy.knobs[name] != nil
                current = .secretPresent(present)
            } else if let value = copy.knobs[name] {
                current = .current(value)
            } else {
                current = .defaultValue
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
        let isStale: Bool
    }

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
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let knobs = object["knobs"] as? [String: Any]
        else {
            return Copy(knobs: [:], ageText: "no local copy", isStale: true)
        }
        var values: [String: String] = [:]
        for (key, value) in knobs {
            if let text = value as? String { values[key] = text }
        }
        let saved = object["saved_epoch"] as? Double
            ?? (object["saved_at"] as? String).flatMap { parseISO($0) }
        let age = saved.map { Int(Date().timeIntervalSince1970 - $0) } ?? 0
        // The engine's own freshness window is a quarter of an hour,  and a copy
        // older than that is not a current reading of the knob.
        let stale = age > 900
        return Copy(
            knobs: values,
            ageText: age <= 0 ? "just now" : FleetTime.age(age),
            isStale: stale
        )
    }

    private static func parseISO(_ text: String) -> Double? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: text)?.timeIntervalSince1970
    }
}
