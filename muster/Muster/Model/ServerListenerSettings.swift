import Foundation

// The server Eavesdropper's own settings.
//
// Since the Nov 10 one-listener cutover the server listener holds every seat, and its live config is
// `/data/listener.toml` inside the server's `eavesdropper` container.  Muster cannot write that
// file, and must not:  the one writer is the server's `eavesdropper-settings` editor
// (`scripts/simple_tools/server/eavesdropper_settings_edit.py`), reached through
// `fleet-ops-control.py server eavesdropper-settings server` over the `fleet-ops-ctl` alias.
//
// These rules hold:
//
//   * a read is the editor's `get`:  `--plan` with nothing on standard input.  It answers the
//     allowlisted settings only, never the file.
//   * a change is the editor's `plan` then `apply`, through `OpsController`:  Show Plan runs the
//     plan and shows what it changes, the lines it changes and the Eavesdropper's own check;  the
//     owner approves with Touch ID;  Muster plans again and runs only when the second plan hashes
//     to the one approved, and the apply carries that hash (`expectPlanSHA256`), so the server does
//     exactly what was shown or nothing.  There is no other write path here.
//   * the server's allowlist (`scripts/fleet-ops-eavesdropper-settings.json` beside its helper) is
//     the judge.  Muster reads this checkout's copy only to know the type and range of each key,
//     and offers a key only when it is a seat's `enabled` or `wake` and that copy lists it.
//     It never adds a key to either copy, and a key the server answers that this copy does not
//     list is shown read only.
//   * a pin, a credential name or a key that reads as a secret, a path or a URL is never offered,
//     whatever any allowlist says, and every read-only row says why.
//   * only a seat's `enabled` and `wake` can be sent (`ServerSettingsBuilder.seatStructureKeys`).
//     Every other allowlisted key (a seat's `routine`, `budget` and `live` tables, and every key of
//     the daemon and feature tables) is shown read only, because a knob's home is Infisical (the
//     owner's rule, Nov 11, 2026) and AFC-773 registers them there.  Nothing is ever sent to
//     remove a key, and a seat with no routine table is never given one.

// MARK: - Values

/// One JSON value as the server's editor reads it, with whole numbers and other numbers kept
/// apart:  the editor compares by type as well as value, so `30` and `30.0` are two values.
indirect enum ServerSettingValue: Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case number(Double)
    case text(String)
    case list([ServerSettingValue])
    case table([String: ServerSettingValue])

    /// The value as JSON, with a table's keys sorted.  Nil for a number that is not finite, which
    /// JSON cannot carry.
    var json: String? {
        switch self {
        case .null: return "null"
        case .bool(let value): return value ? "true" : "false"
        case .int(let value): return String(value)
        case .number(let value):
            guard value.isFinite else { return nil }
            let text = String(value)
            // Swift writes 30.0 as "30.0" and 1e-05 as "1e-05";  both read back as a float.
            return text.contains(".") || text.contains("e") ? text : text + ".0"
        case .text(let value): return Self.quoted(value)
        case .list(let items):
            var parts: [String] = []
            for item in items {
                guard let part = item.json else { return nil }
                parts.append(part)
            }
            return "[" + parts.joined(separator: ",") + "]"
        case .table(let entries):
            var parts: [String] = []
            for key in entries.keys.sorted() {
                guard let part = entries[key]?.json else { return nil }
                parts.append(Self.quoted(key) + ":" + part)
            }
            return "{" + parts.joined(separator: ",") + "}"
        }
    }

    /// How the value reads on screen.
    var display: String {
        switch self {
        case .null: return "none"
        case .bool(let value): return value ? "On" : "Off"
        case .int(let value): return String(value)
        case .number(let value):
            return value == value.rounded() && abs(value) < 1e15 ? String(format: "%.1f", value) : String(value)
        case .text(let value): return value
        case .list(let items):
            // An item that is itself a list (a channel and topic pair) is bracketed, so the pairs
            // of `daemon.presence_topics` can be told apart.
            return items.map { item -> String in
                if case .list = item { return "[" + item.display + "]" }
                return item.display
            }.joined(separator: ", ")
        case .table(let entries):
            return entries.keys.sorted().map { "\($0) = \(entries[$0]?.display ?? "")" }.joined(separator: ", ")
        }
    }

    /// The text a field starts from.
    var fieldText: String {
        switch self {
        case .number(let value):
            return value == value.rounded() && abs(value) < 1e15 ? String(format: "%.1f", value) : String(value)
        default:
            return display
        }
    }

    var tableEntries: [String: ServerSettingValue]? {
        if case .table(let entries) = self { return entries }
        return nil
    }

    private static func quoted(_ text: String) -> String {
        var out = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if scalar.value < 0x20 || scalar.value == 0x7F {
                    out += String(format: "\\u%04x", scalar.value)
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        return out + "\""
    }
}

/// A small JSON reader that keeps `30` and `30.0` apart, which Foundation's readers do not
/// promise.  It reads one document and refuses anything after it.
enum ServerSettingJSON {
    static let maximumDepth = 32

    static func parse(_ text: String) -> ServerSettingValue? {
        var reader = Reader(bytes: Array(text.utf8))
        reader.skipSpace()
        guard let value = reader.value(depth: 0) else { return nil }
        reader.skipSpace()
        return reader.atEnd ? value : nil
    }

    private struct Reader {
        let bytes: [UInt8]
        var index = 0

        var atEnd: Bool { index >= bytes.count }

        mutating func skipSpace() {
            while index < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[index]) { index += 1 }
        }

        mutating func literal(_ word: String) -> Bool {
            let wanted = Array(word.utf8)
            guard index + wanted.count <= bytes.count, Array(bytes[index..<index + wanted.count]) == wanted else {
                return false
            }
            index += wanted.count
            return true
        }

        mutating func value(depth: Int) -> ServerSettingValue? {
            guard depth <= ServerSettingJSON.maximumDepth, !atEnd else { return nil }
            switch bytes[index] {
            case UInt8(ascii: "{"): return table(depth: depth)
            case UInt8(ascii: "["): return list(depth: depth)
            case UInt8(ascii: "\""): return string().map(ServerSettingValue.text)
            case UInt8(ascii: "t"): return literal("true") ? .bool(true) : nil
            case UInt8(ascii: "f"): return literal("false") ? .bool(false) : nil
            case UInt8(ascii: "n"): return literal("null") ? .null : nil
            default: return number()
            }
        }

        mutating func table(depth: Int) -> ServerSettingValue? {
            index += 1
            var entries: [String: ServerSettingValue] = [:]
            skipSpace()
            if !atEnd, bytes[index] == UInt8(ascii: "}") { index += 1; return .table(entries) }
            while true {
                skipSpace()
                guard !atEnd, bytes[index] == UInt8(ascii: "\""), let key = string() else { return nil }
                skipSpace()
                guard !atEnd, bytes[index] == UInt8(ascii: ":") else { return nil }
                index += 1
                skipSpace()
                guard let item = value(depth: depth + 1) else { return nil }
                entries[key] = item
                skipSpace()
                guard !atEnd else { return nil }
                if bytes[index] == UInt8(ascii: ",") { index += 1; continue }
                if bytes[index] == UInt8(ascii: "}") { index += 1; return .table(entries) }
                return nil
            }
        }

        mutating func list(depth: Int) -> ServerSettingValue? {
            index += 1
            var items: [ServerSettingValue] = []
            skipSpace()
            if !atEnd, bytes[index] == UInt8(ascii: "]") { index += 1; return .list(items) }
            while true {
                skipSpace()
                guard let item = value(depth: depth + 1) else { return nil }
                items.append(item)
                skipSpace()
                guard !atEnd else { return nil }
                if bytes[index] == UInt8(ascii: ",") { index += 1; continue }
                if bytes[index] == UInt8(ascii: "]") { index += 1; return .list(items) }
                return nil
            }
        }

        mutating func hex4() -> UInt32? {
            guard index + 4 <= bytes.count,
                  let value = UInt32(String(decoding: bytes[index..<index + 4], as: UTF8.self), radix: 16) else {
                return nil
            }
            index += 4
            return value
        }

        mutating func string() -> String? {
            index += 1
            var scalars = String.UnicodeScalarView()
            var raw: [UInt8] = []
            func flush() {
                if !raw.isEmpty {
                    scalars.append(contentsOf: String(decoding: raw, as: UTF8.self).unicodeScalars)
                    raw.removeAll()
                }
            }
            while !atEnd {
                let byte = bytes[index]
                if byte == UInt8(ascii: "\"") {
                    index += 1
                    flush()
                    return String(scalars)
                }
                if byte < 0x20 { return nil }
                if byte != UInt8(ascii: "\\") {
                    raw.append(byte)
                    index += 1
                    continue
                }
                flush()
                index += 1
                guard !atEnd else { return nil }
                let escape = bytes[index]
                index += 1
                switch escape {
                case UInt8(ascii: "\""): scalars.append("\"")
                case UInt8(ascii: "\\"): scalars.append("\\")
                case UInt8(ascii: "/"): scalars.append("/")
                case UInt8(ascii: "b"): scalars.append("\u{08}")
                case UInt8(ascii: "f"): scalars.append("\u{0C}")
                case UInt8(ascii: "n"): scalars.append("\n")
                case UInt8(ascii: "r"): scalars.append("\r")
                case UInt8(ascii: "t"): scalars.append("\t")
                case UInt8(ascii: "u"):
                    guard var code = hex4() else { return nil }
                    if (0xD800...0xDBFF).contains(code) {
                        guard literal("\\u"), let low = hex4(), (0xDC00...0xDFFF).contains(low) else { return nil }
                        code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    }
                    guard let scalar = Unicode.Scalar(code) else { return nil }
                    scalars.append(scalar)
                default:
                    return nil
                }
            }
            return nil
        }

        mutating func number() -> ServerSettingValue? {
            let start = index
            var isWhole = true
            if !atEnd, bytes[index] == UInt8(ascii: "-") { index += 1 }
            var digits = 0
            while !atEnd, (48...57).contains(bytes[index]) { index += 1; digits += 1 }
            guard digits > 0 else { return nil }
            if !atEnd, bytes[index] == UInt8(ascii: ".") {
                isWhole = false
                index += 1
                var fraction = 0
                while !atEnd, (48...57).contains(bytes[index]) { index += 1; fraction += 1 }
                guard fraction > 0 else { return nil }
            }
            if !atEnd, bytes[index] == UInt8(ascii: "e") || bytes[index] == UInt8(ascii: "E") {
                isWhole = false
                index += 1
                if !atEnd, bytes[index] == UInt8(ascii: "+") || bytes[index] == UInt8(ascii: "-") { index += 1 }
                var exponent = 0
                while !atEnd, (48...57).contains(bytes[index]) { index += 1; exponent += 1 }
                guard exponent > 0 else { return nil }
            }
            let lexeme = String(decoding: bytes[start..<index], as: UTF8.self)
            if isWhole, let whole = Int(lexeme) { return .int(whole) }
            guard let number = Double(lexeme), number.isFinite else { return nil }
            return .number(number)
        }
    }
}

// MARK: - The allowlist this checkout holds

/// What one key of the allowlist may take.
indirect enum ServerSettingSpec: Equatable {
    case bool
    case whole(min: Double, max: Double)
    case number(min: Double, max: Double)
    case choice([String])
    case text(pattern: String, maxLength: Int)
    case pairs
    case seatEnv
    case table([String: ServerSettingSpec])
}

/// This checkout's copy of the server editor's allowlist, read only, to know each key's type and
/// range.  The server reads its own copy and judges every request against it.
struct ServerSettingsAllowlist: Equatable {
    /// The settings of every `[seat.X]`.
    var seat: [String: ServerSettingSpec]
    /// The other tables' settings, by table.
    var tables: [String: [String: ServerSettingSpec]]

    /// Where the copy sits in a checkout.
    static let relativePath = "scripts/fleet-ops-eavesdropper-settings.json"

    /// The editor's `HARD_DENY`:  never offered, whatever an allowlist says.
    static let neverOfferedSeatKeys: [String] = [
        "instance", "creds", "bot", "bearer_file", "bearer_locator", "allow_admin", "email_env", "key_env", "site_env",
    ]
    static let neverOfferedDaemonKeys: [String] = [
        "instance", "owner_user_id", "eligible_user_ids", "owner_clients", "outgoing_webhook_seats",
    ]

    /// The editor's `_DENY_NAME_RE`:  a name that reads as a secret, a token, a credential, a path
    /// or a URL.
    static func readsAsSecret(_ name: String) -> Bool {
        let lower = name.lowercased()
        for word in ["secret", "token", "passw", "credential", "creds", "bearer", "private", "locator", "url"]
            where lower.contains(word) {
            return true
        }
        if lower.contains("apikey") || lower.contains("api_key") { return true }
        if lower == "key" || lower.hasSuffix("_key") || lower == "keys" || lower.hasSuffix("_keys") { return true }
        for suffix in ["_env", "_file", "_path", "_dir"] where lower.hasSuffix(suffix) { return true }
        return false
    }

    /// Whether a top-level setting name may ever be offered for this table.
    static func mayOffer(table: String, name: String) -> Bool {
        let denied = table == "seat" ? neverOfferedSeatKeys : (table == "daemon" ? neverOfferedDaemonKeys : [])
        return !denied.contains(name) && !readsAsSecret(name)
    }

    static func url(repoRoot: URL) -> URL {
        repoRoot.appendingPathComponent(relativePath)
    }

    static func read(at url: URL) -> ServerSettingsAllowlist? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return parse(data)
    }

    /// The copy, or nil when it is not one.  An entry this app cannot read, or one it must never
    /// offer, is left out, never guessed at.
    static func parse(_ data: Data) -> ServerSettingsAllowlist? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let seatObject = object["seat"] as? [String: Any],
              let tablesObject = object["tables"] as? [String: Any] else { return nil }
        var seat: [String: ServerSettingSpec] = [:]
        for (name, raw) in seatObject where mayOffer(table: "seat", name: name) {
            if let spec = spec(raw, nested: false) { seat[name] = spec }
        }
        var tables: [String: [String: ServerSettingSpec]] = [:]
        for (table, raw) in tablesObject where table != "seat" {
            guard let keys = raw as? [String: Any] else { continue }
            var specs: [String: ServerSettingSpec] = [:]
            for (name, rawSpec) in keys where mayOffer(table: table, name: name) {
                if let spec = spec(rawSpec, nested: false) { specs[name] = spec }
            }
            if !specs.isEmpty { tables[table] = specs }
        }
        return ServerSettingsAllowlist(seat: seat, tables: tables)
    }

    private static func bound(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let double = number.doubleValue
        return double.isFinite ? double : nil
    }

    private static func spec(_ raw: Any, nested: Bool) -> ServerSettingSpec? {
        guard let object = raw as? [String: Any], let type = object["type"] as? String else { return nil }
        switch type {
        case "bool": return .bool
        case "int", "number":
            guard let low = bound(object["min"]), let high = bound(object["max"]), low <= high else { return nil }
            return type == "int" ? .whole(min: low, max: high) : .number(min: low, max: high)
        case "enum":
            guard let values = object["values"] as? [String], !values.isEmpty else { return nil }
            return .choice(values)
        case "string":
            guard let pattern = object["pattern"] as? String, !pattern.isEmpty,
                  let limit = (object["maxLength"] as? NSNumber)?.intValue,
                  (0...256).contains(limit) else { return nil }
            return .text(pattern: pattern, maxLength: limit)
        case "pairs": return .pairs
        case "seat-env": return nested ? .seatEnv : nil
        case "table":
            guard !nested, let keys = object["keys"] as? [String: Any], !keys.isEmpty else { return nil }
            var specs: [String: ServerSettingSpec] = [:]
            for (name, rawSpec) in keys {
                guard let sub = spec(rawSpec, nested: true) else { continue }
                // A sub-key that reads as a secret is kept only as a variable name, which is never offered.
                if case .seatEnv = sub {
                    specs[name] = sub
                } else if !readsAsSecret(name) {
                    specs[name] = sub
                }
            }
            return specs.isEmpty ? nil : .table(specs)
        default:
            return nil
        }
    }
}

// MARK: - The editor's answer

/// The `eavesdropperSettings` document of one answer:  a get's settings, or a plan's or an
/// apply's changes.
struct ServerSettingsAnswer: Equatable {
    struct Change: Equatable {
        var key: String
        var from: ServerSettingValue?
        var to: ServerSettingValue
        var removes: [String]

        var line: String {
            "\(key):  \(from?.display ?? "not set") \u{2192} \(to.display)"
        }
    }

    var result: String
    var ok: Bool
    var applyEnabled: Bool?
    var baseSHA256: String?
    var planSHA256: String?
    var settings: [String: ServerSettingValue] = [:]
    var changes: [Change] = []
    var diff: [String] = []
    /// The Eavesdropper's own `check-config` verdict on a plan's candidate, when it gave one.
    var checkOK: Bool?
    var checkProblems: [String] = []
    var errorCode: String?
    var detail: String?
    /// The reload's code after an apply, such as `reloaded` or `reload_unconfirmed`.
    var reloadCode: String?
    var rolledBack: Bool?
    /// After a failed apply, whether the live file holds the new bytes:  false when it was never
    /// replaced or the old bytes went back.
    var replaced: Bool?
    /// The name of the copy an apply made of the old file, when the editor named one.
    var backup: String?

    /// The editor's backup name prefix, `listener.toml` plus its `BACKUP_INFIX`.
    static let backupPrefix = "listener.toml.bak-settings-"

    /// Whether a text is a bare backup file name in the editor's own form:  the prefix, then only
    /// letters, digits, dots, dashes and underscores, never a slash, and at most 80 characters.
    static func isBackupName(_ value: String) -> Bool {
        guard value.count <= 80, value.hasPrefix(backupPrefix), value.count > backupPrefix.count else { return false }
        return value.unicodeScalars.allSatisfy { scalar in
            (65...90).contains(scalar.value) || (97...122).contains(scalar.value) || (48...57).contains(scalar.value)
                || scalar == "." || scalar == "-" || scalar == "_"
        }
    }

    static func isHash(_ value: String) -> Bool {
        value.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    /// The document inside a helper's answer, from the whole of stdout or the last line that is
    /// a JSON object, as `OpsResultParser.decode` finds the answer.
    init?(stdout: String) {
        let trimmed = stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        var candidates = [trimmed]
        candidates += stdout.split(whereSeparator: \.isNewline).reversed()
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { $0.hasPrefix("{") }
        for text in candidates {
            if case .table(let answer)? = ServerSettingJSON.parse(text),
               case .table(let document)? = answer["eavesdropperSettings"] {
                self.init(document: document)
                return
            }
        }
        return nil
    }

    init(document: [String: ServerSettingValue]) {
        func text(_ key: String, in object: [String: ServerSettingValue]) -> String? {
            if case .text(let value)? = object[key] { return value }
            return nil
        }
        func flag(_ key: String, in object: [String: ServerSettingValue]) -> Bool? {
            if case .bool(let value)? = object[key] { return value }
            return nil
        }
        result = text("result", in: document) ?? ""
        ok = flag("ok", in: document) ?? false
        applyEnabled = flag("applyEnabled", in: document)
        baseSHA256 = text("baseSHA256", in: document).flatMap { Self.isHash($0) ? $0 : nil }
        planSHA256 = text("planSHA256", in: document).flatMap { Self.isHash($0) ? $0 : nil }
        settings = document["settings"]?.tableEntries ?? [:]
        if case .list(let items)? = document["changes"] {
            changes = items.compactMap { item in
                guard let entry = item.tableEntries, let key = text("key", in: entry), let to = entry["to"] else { return nil }
                var removes: [String] = []
                if case .list(let names)? = entry["removes"] {
                    removes = names.compactMap { if case .text(let name) = $0 { return name } else { return nil } }
                }
                let from = entry["from"].flatMap { $0 == .null ? nil : $0 }
                return Change(key: key, from: from, to: to, removes: removes)
            }
        }
        if case .list(let lines)? = document["diff"] {
            diff = lines.compactMap { if case .text(let line) = $0 { return OpsResultParser.sanitizedForDisplay(line) } else { return nil } }
        }
        if let check = document["check"]?.tableEntries {
            checkOK = flag("ok", in: check)
            if case .list(let problems)? = check["problems"] {
                checkProblems = problems.map { OpsResultParser.sanitizedForDisplay($0.display) }
            }
        }
        errorCode = text("errorCode", in: document).flatMap { OpsResultParser.isErrorCode($0) ? $0 : nil }
        detail = text("detail", in: document).map { String(OpsResultParser.sanitizedForDisplay($0).prefix(400)) }
        if let reload = document["reload"]?.tableEntries {
            reloadCode = text("code", in: reload).flatMap { OpsResultParser.isErrorCode($0) ? $0 : nil }
        }
        rolledBack = flag("rolledBack", in: document)
        replaced = flag("replaced", in: document)
        backup = text("backup", in: document).flatMap { Self.isBackupName($0) ? OpsResultParser.sanitizedForDisplay($0) : nil }
    }
}

extension ServerSettingsAnswer {
    /// The app's result word and words for one settings answer.
    ///
    /// A plan counts only when the editor planned it, gave its hash, and the Eavesdropper's own
    /// check would take the result:  a plan it would refuse, or one that changes nothing, is not a
    /// plan anyone may approve.  An apply is done only when the editor says it applied.
    static func outcome(
        _ answer: ServerSettingsAnswer?,
        helperKind: OpsResultKind,
        mode: OpsMode,
        detail: String
    ) -> (OpsResultKind, String) {
        let gap = ServerStatusCopy.sentenceGap
        guard let answer else {
            if helperKind == .refused || helperKind == .failed { return (helperKind, detail) }
            return (.failed, "The server's answer carried no settings document, so nothing is known about it.")
        }
        switch (mode, answer.result) {
        case (.plan, "planned"):
            guard answer.ok, answer.planSHA256 != nil else {
                return (.failed, "The server's plan carried no plan hash, so it cannot be approved.")
            }
            guard answer.checkOK == true else {
                let problems = answer.checkProblems.isEmpty ? "" : gap + answer.checkProblems.joined(separator: "; ")
                return (.refused, "The Eavesdropper's own check would not take this change, so it cannot be applied." + problems)
            }
            return (.planned, detail)
        case (_, "unchanged"):
            return (.refused, "Every setting is already as asked, so there is nothing to apply.")
        case (.yes, "applied"):
            return (answer.ok ? .done : .unknown, detail)
        case (_, "refused"):
            return (.refused, answer.detail ?? detail)
        case (_, "failed"):
            // A failed apply may have replaced the file and not put it back:  the answer says.
            // `replaced` false means the live file does not hold the new bytes, so the apply failed
            // and nothing changed;  only a replaced (or unsaid) file that was not rolled back is unknown.
            let words = (answer.detail ?? detail) + (answer.backup.map { " (a copy of the old file is kept as \($0))" } ?? "")
            if mode == .yes, answer.replaced != false, answer.rolledBack != true {
                return (.unknown, words)
            }
            return (.failed, words)
        default:
            return (mode == .yes ? .unknown : .failed, "The server answered \"\(answer.result)\", which is not an answer to this request.")
        }
    }
}

// MARK: - A change request

/// What one Show Plan sends:  the settings to set, each by its full key (`seat.CLAUDE.enabled`,
/// `seat.CLAUDE.wake`).  Only a seat's `enabled` and `wake` can be sent;  every other key is a
/// knob whose home is Infisical (AFC-773).  Nothing is ever removed.
struct ServerListenerSettingsRequest: Equatable {
    var set: [String: ServerSettingValue]
    /// The approved plan's hash, set only on the run that follows the approval.
    var expectPlanSHA256: String?

    /// The most settings one request carries:  the editor's `MAX_CHANGES`.
    static let maximumChanges = 32
    /// The longest request:  the helper's `EAVESDROPPER_SETTINGS_REQUEST_MAX_BYTES`.
    static let maximumBytes = 32 * 1024

    var keys: [String] { self.set.keys.sorted() }

    /// The standard input of one run:  `{"set": {...}}`, with the approved hash on an apply.
    func standardInput(for mode: OpsMode) -> Data? {
        var document: [String: ServerSettingValue] = ["set": .table(set)]
        if mode == .yes {
            guard let hash = expectPlanSHA256 else { return nil }
            document["expectPlanSHA256"] = .text(hash)
        }
        return ServerSettingValue.table(document).json.map { Data($0.utf8) }
    }

    /// Whether a full key is one Muster may send:  only `seat.SEAT.name` where `name` is one of
    /// `ServerSettingsBuilder.seatStructureKeys`, in the editor's own shape, and never a pin, a
    /// credential name or a name that reads as a secret.  Every `table.name` key and every other
    /// seat key is refused, because a knob's home is Infisical (AFC-773).
    static func isOfferableKey(_ key: String) -> Bool {
        let parts = key.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        func isName(_ text: String) -> Bool {
            guard (1...48).contains(text.count), let first = text.unicodeScalars.first,
                  (97...122).contains(first.value) else { return false }
            return text.unicodeScalars.allSatisfy { (97...122).contains($0.value) || (48...57).contains($0.value) || $0 == "_" }
        }
        if parts.count == 3, parts[0] == "seat" {
            let seat = parts[1]
            guard (1...40).contains(seat.count), let first = seat.unicodeScalars.first, (65...90).contains(first.value),
                  seat.unicodeScalars.allSatisfy({ (65...90).contains($0.value) || (48...57).contains($0.value) || $0 == "-" }),
                  isName(parts[2]), ServerSettingsBuilder.seatStructureKeys.contains(parts[2]) else { return false }
            return ServerSettingsAllowlist.mayOffer(table: "seat", name: parts[2])
        }
        return false
    }

    /// Whether the request is one Muster will send.
    var isSendable: Bool {
        guard !set.isEmpty, set.count <= Self.maximumChanges, set.keys.allSatisfy(Self.isOfferableKey) else { return false }
        if let hash = expectPlanSHA256, !ServerSettingsAnswer.isHash(hash) { return false }
        guard let data = standardInput(for: .plan), data.count <= Self.maximumBytes else { return false }
        return true
    }
}

// MARK: - The rows

/// The control a row offers.
enum ServerSettingControl: Equatable {
    case toggle
    case choice([String])
    case whole(min: Double, max: Double)
    case number(min: Double, max: Double)
    case text(pattern: String, maxLength: Int)
}

/// One setting on screen.
struct ServerSettingRow: Identifiable, Equatable {
    /// The full path:  `seat.CLAUDE.budget.wakes_per_day`, `daemon.coalesce_seconds`.
    let id: String
    /// The key a request would set:  `seat.CLAUDE.enabled`, `seat.CLAUDE.wake`.  For a read-only
    /// row it is the key the server answered with, such as `seat.CLAUDE.budget`.
    let settingKey: String
    /// The key inside a table, for a table's setting.
    let subKey: String?
    /// The name on screen.
    let title: String
    /// What the server answered, or nil when the file does not set it (or it was never read).
    let current: ServerSettingValue?
    /// The control, or nil when the row is read only.
    let control: ServerSettingControl?
    /// Why the row is read only, when it is.
    let readOnlyReason: String?
    /// For a table's setting, the whole table as the server answered it, every sub-key included,
    /// so an edit is laid over it and nothing the server holds is dropped.
    var table: [String: ServerSettingValue]? = nil
    /// The value the daemon takes when the file sets none, for a seat's `enabled` (on) and `wake`
    /// (`inbox`) only.  `current` stays nil, so a plan still reads "not set" for the old value.
    var defaultValue: ServerSettingValue? = nil

    var isEditable: Bool { control != nil }

    /// The value the daemon acts on:  the server's answer, else the daemon's default.
    var effective: ServerSettingValue? { current ?? defaultValue }

    /// Whether the value shown is the daemon's default because the file sets none.
    var showsDefault: Bool { current == nil && defaultValue != nil }

    /// A note under the row, when the value shown is the daemon's default.
    var defaultNote: String? { showsDefault ? ServerSettingsCopy.daemonDefault : nil }

    /// The current value, in words.
    var currentText: String { effective?.display ?? (isEditable ? "Not set" : "Not read") }
}

/// One table on screen:  a seat, the daemon, or one of the switches.
struct ServerSettingsGroup: Identifiable, Equatable {
    let id: String
    let title: String
    let rows: [ServerSettingRow]
}

enum ServerSettingsCopy {
    static let gap = ServerStatusCopy.sentenceGap

    static let neverOffered = "A pin or a credential name.\(gap)The server's editor refuses it whatever its allowlist says, so Muster never offers it, and the server never sends its value."
    static let variableName = "This names the variable that holds the routine's address or key, never its value.\(gap)The server lets it be only the seat's own name, so Muster shows it and offers no control."
    static let routerContract = "How a routine is signed and sent is fixed by the receiver it calls, so Muster shows it and offers no control for it."
    static let daemonDefault = "The file sets no value here, so this is the daemon's default."
    static let pairs = "A list of channel and topic pairs.\(gap)Muster shows it and offers no control for it."
    static let notEnforced = "The eavesdropper counts wakes and not dollars, so this cap is never enforced and Muster will not write it."
    static let noRoutine = "The seat has no routine table, so it Captures Only.\(gap)Muster does not give a seat a routine."
    static let notOnThisCopy = "The server's allowlist lists this, and this checkout's copy does not, so Muster does not offer it.\(gap)Update the checkout to offer it."

    /// Owner's rule (Nov 11, 2026):  a tunable knob (a timing, a cap, a feature switch) has its home in
    /// Infisical.  Only a seat's routing structure has no Infisical home, so only that is written through
    /// the server's settings editor.
    static let knobHome = "A knob, and a knob's home is Infisical.\(gap)Muster shows the value the server holds and does not write it into the server's listener file.\(gap)It becomes editable in Controls once it is a registered knob."

    static let applyOff = "The server's allowlist has applyEnabled off, which is the owner's switch.\(gap)Show Plan still works, and every apply is refused until the owner turns it on."
    static let applyOn = "Each change goes Show Plan, then Touch ID, then the server plans again and applies only the plan you approved."

    static func groupTitle(table: String) -> String {
        switch table {
        case "daemon": return "Daemon"
        case "linear_comments": return "Linear Comment Mirror"
        case "linear_projects": return "Linear Project Mirror"
        case "notes_queue": return "Notes Queue"
        case "listener_api": return "Listener API"
        default: return table.split(separator: "_").map { $0.prefix(1).uppercased() + $0.dropFirst() }.joined(separator: " ")
        }
    }

    /// A key name as a Title Case label:  `wakes_per_day` reads "Wakes Per Day", and a short word that is
    /// an abbreviation (`usd`, `url`, `api`) is all capitals.
    static func rowTitle(_ name: String) -> String {
        name.split(separator: "_").map { word -> String in
            ["usd", "url", "api", "id"].contains(word.lowercased())
                ? word.uppercased()
                : word.prefix(1).uppercased() + word.dropFirst()
        }.joined(separator: " ")
    }
}

enum ServerSettingsBuilder {
    /// A seat's routing structure:  whether the seat is on and how it is woken.  These have no
    /// Infisical home, and they are the only settings Muster sends.  Every other allowlisted setting
    /// (every sub-key of a seat's routine, budget and live tables, and every key of the daemon and
    /// feature tables) is shown read only:  a timing or a cap is a knob (`ServerSettingsCopy.knobHome`),
    /// and the rest says its own reason.
    static let seatStructureKeys: Set<String> = ["enabled", "wake"]

    /// The value the daemon takes for a structure key the file does not set
    /// (`scripts/simple_tools/config.py`:  `enabled` defaults to true, `wake` to `inbox`).
    static let seatStructureDefaults: [String: ServerSettingValue] = ["enabled": .bool(true), "wake": .text("inbox")]

    /// The rows for a get's settings, against this checkout's allowlist copy.
    static func groups(settings: [String: ServerSettingValue], allowlist: ServerSettingsAllowlist) -> [ServerSettingsGroup] {
        var seats = Set<String>()
        var unknownByGroup: [String: [String]] = [:]
        for key in settings.keys {
            let parts = key.split(separator: ".").map(String.init)
            if parts.count == 3, parts[0] == "seat" {
                seats.insert(parts[1])
                if allowlist.seat[parts[2]] == nil { unknownByGroup["seat.\(parts[1])", default: []].append(key) }
            } else if parts.count == 2 {
                if allowlist.tables[parts[0]]?[parts[1]] == nil { unknownByGroup[parts[0], default: []].append(key) }
            }
        }
        var groups: [ServerSettingsGroup] = []
        for seat in seats.sorted() {
            let prefix = "seat.\(seat)"
            var rows: [ServerSettingRow] = []
            let hasRoutine = settings["\(prefix).routine"]?.tableEntries != nil
            for name in allowlist.seat.keys.sorted() {
                guard let spec = allowlist.seat[name] else { continue }
                rows += self.rows(key: "\(prefix).\(name)", name: name, spec: spec,
                                  current: settings["\(prefix).\(name)"], hasRoutine: hasRoutine,
                                  isStructure: seatStructureKeys.contains(name))
            }
            rows += (unknownByGroup[prefix] ?? []).sorted().map { readOnly($0, current: settings[$0], reason: ServerSettingsCopy.notOnThisCopy) }
            rows += ServerSettingsAllowlist.neverOfferedSeatKeys.map {
                readOnly("\(prefix).\($0)", current: nil, reason: ServerSettingsCopy.neverOffered)
            }
            groups.append(ServerSettingsGroup(id: prefix, title: "Seat \(seat)", rows: rows))
        }
        let tableNames = Set(allowlist.tables.keys).union(unknownByGroup.keys.filter { !$0.hasPrefix("seat.") })
        for table in tableNames.sorted(by: { ($0 == "daemon" ? "" : $0) < ($1 == "daemon" ? "" : $1) }) {
            var rows: [ServerSettingRow] = []
            for name in (allowlist.tables[table] ?? [:]).keys.sorted() {
                guard let spec = allowlist.tables[table]?[name] else { continue }
                rows += self.rows(key: "\(table).\(name)", name: name, spec: spec, current: settings["\(table).\(name)"],
                                  hasRoutine: false, isStructure: false)
            }
            rows += (unknownByGroup[table] ?? []).sorted().map { readOnly($0, current: settings[$0], reason: ServerSettingsCopy.notOnThisCopy) }
            if table == "daemon" {
                rows += ServerSettingsAllowlist.neverOfferedDaemonKeys.map {
                    readOnly("daemon.\($0)", current: nil, reason: ServerSettingsCopy.neverOffered)
                }
            }
            groups.append(ServerSettingsGroup(id: table, title: ServerSettingsCopy.groupTitle(table: table), rows: rows))
        }
        return groups
    }

    private static func readOnly(_ key: String, current: ServerSettingValue?, reason: String) -> ServerSettingRow {
        let name = key.split(separator: ".").last.map(String.init) ?? key
        return ServerSettingRow(id: key, settingKey: key, subKey: nil, title: ServerSettingsCopy.rowTitle(name),
                                current: current, control: nil, readOnlyReason: reason)
    }

    private static func control(_ spec: ServerSettingSpec) -> ServerSettingControl? {
        switch spec {
        case .bool: return .toggle
        case .whole(let low, let high): return .whole(min: low, max: high)
        case .number(let low, let high): return .number(min: low, max: high)
        case .choice(let values): return .choice(values)
        case .text(let pattern, let limit): return .text(pattern: pattern, maxLength: limit)
        case .pairs, .seatEnv, .table: return nil
        }
    }

    private static func rows(key: String, name: String, spec: ServerSettingSpec, current: ServerSettingValue?,
                             hasRoutine: Bool, isStructure: Bool) -> [ServerSettingRow] {
        guard case .table(let keys) = spec else {
            var control = control(spec)
            var reason: String? = control == nil ? ServerSettingsCopy.pairs : nil
            if reason == nil, !isStructure {
                reason = ServerSettingsCopy.knobHome
                control = nil
            }
            return [ServerSettingRow(id: key, settingKey: key, subKey: nil, title: ServerSettingsCopy.rowTitle(name),
                                     current: current, control: control, readOnlyReason: reason,
                                     defaultValue: isStructure ? seatStructureDefaults[name] : nil)]
        }
        let entries = current?.tableEntries ?? [:]
        return keys.keys.sorted().map { sub in
            let subSpec = keys[sub] ?? .pairs
            var control = control(subSpec)
            var reason: String?
            if case .seatEnv = subSpec {
                reason = ServerSettingsCopy.variableName
            } else if name == "routine", ["method", "auth", "header", "signature_prefix"].contains(sub) {
                reason = ServerSettingsCopy.routerContract
            } else if name == "routine", ["timeout_seconds", "cost_usd"].contains(sub) {
                // A timing and a cap:  knobs, whatever the seat's routine holds.
                reason = ServerSettingsCopy.knobHome
            } else if name == "budget", sub == "usd_per_day" {
                reason = ServerSettingsCopy.notEnforced
            } else if name == "routine", !hasRoutine {
                reason = ServerSettingsCopy.noRoutine
            } else if control == nil {
                reason = ServerSettingsCopy.pairs
            } else {
                // No sub-key of a table is a seat's routing structure, so every one is read only.
                reason = ServerSettingsCopy.knobHome
            }
            if reason != nil { control = nil }
            return ServerSettingRow(id: "\(key).\(sub)", settingKey: key, subKey: sub,
                                    title: "\(ServerSettingsCopy.rowTitle(name)):  \(ServerSettingsCopy.rowTitle(sub))",
                                    current: entries[sub], control: control, readOnlyReason: reason,
                                    table: current?.tableEntries)
        }
    }

    // MARK: Edits

    enum EditProblem: Error, Equatable {
        case readOnly(String)
        case notAValue(String)
        case nothingChanged
        case notSendable

        var sentence: String {
            switch self {
            case .readOnly(let title): return "\(title) is read only, so nothing was planned."
            case .notAValue(let why): return why
            case .nothingChanged: return "Nothing is edited, so there is nothing to plan."
            case .notSendable: return "Muster will not send this request, so nothing was planned."
            }
        }
    }

    /// The value a field's text stands for, by the row's control, or a sentence saying why not.
    static func value(_ text: String, for row: ServerSettingRow) -> Result<ServerSettingValue, EditProblem> {
        guard let control = row.control else { return .failure(.readOnly(row.title)) }
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        switch control {
        case .toggle:
            switch trimmed {
            case "true", "On": return .success(.bool(true))
            case "false", "Off": return .success(.bool(false))
            default: return .failure(.notAValue("\(row.title) must be on or off."))
            }
        case .choice(let values):
            guard values.contains(trimmed) else {
                return .failure(.notAValue("\(row.title) must be one of \(values.joined(separator: ", "))."))
            }
            return .success(.text(trimmed))
        case .whole(let low, let high):
            guard let whole = Int(trimmed), Double(whole) >= low, Double(whole) <= high else {
                return .failure(.notAValue("\(row.title) must be a whole number from \(Int(low)) to \(Int(high))."))
            }
            return .success(.int(whole))
        case .number(let low, let high):
            guard let number = Double(trimmed), number.isFinite, number >= low, number <= high else {
                return .failure(.notAValue("\(row.title) must be a number from \(low.cleanText) to \(high.cleanText)."))
            }
            let isWhole = !trimmed.contains(".") && !trimmed.lowercased().contains("e")
            if isWhole, let whole = Int(trimmed) { return .success(.int(whole)) }
            return .success(.number(number))
        case .text(let pattern, let limit):
            guard trimmed.count <= limit,
                  let expression = try? NSRegularExpression(pattern: "^(?:\(pattern))$"),
                  expression.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)) != nil else {
                return .failure(.notAValue("\(row.title) does not have the form the server allows."))
            }
            return .success(.text(trimmed))
        }
    }

    /// The request for a set of edits, by row id.  A table's setting is sent as the table the
    /// server answered with, the edits laid over it, so no sub-key is dropped.  An edit that holds
    /// the value the row already has is not sent.
    static func request(edits: [String: String], rows: [ServerSettingRow]) -> Result<ServerListenerSettingsRequest, EditProblem> {
        let byID = Dictionary(rows.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        var set: [String: ServerSettingValue] = [:]
        var tables: [String: [String: ServerSettingValue]] = [:]
        for id in edits.keys.sorted() {
            guard let row = byID[id], let text = edits[id] else { continue }
            guard row.isEditable else { return .failure(.readOnly(row.title)) }
            let value: ServerSettingValue
            switch Self.value(text, for: row) {
            case .success(let parsed): value = parsed
            case .failure(let problem): return .failure(problem)
            }
            // Against the daemon's default when the file sets none, so On on a seat that is on by
            // default plans nothing.
            if let current = row.effective, sameValue(current, value) { continue }
            if let sub = row.subKey {
                // A table is sent whole, the edit laid over what the server answered.  No row can
                // reach this until AFC-773 registers the knobs:  every table's sub-key is read only.
                var table = tables[row.settingKey] ?? row.table ?? [:]
                table[sub] = value
                tables[row.settingKey] = table
            } else {
                set[row.settingKey] = value
            }
        }
        for (key, table) in tables { set[key] = .table(table) }
        guard !set.isEmpty else { return .failure(.nothingChanged) }
        let request = ServerListenerSettingsRequest(set: set)
        guard request.isSendable else { return .failure(.notSendable) }
        return .success(request)
    }

    /// Equal for an edit's purpose:  a whole number typed where the server holds `30.0` is the same
    /// setting, so nothing is sent for it.
    static func sameValue(_ a: ServerSettingValue, _ b: ServerSettingValue) -> Bool {
        switch (a, b) {
        case (.int(let x), .number(let y)), (.number(let y), .int(let x)): return Double(x) == y
        default: return a == b
        }
    }
}

private extension Double {
    var cleanText: String { self == rounded() ? String(Int(self)) : String(self) }
}

// MARK: - Reading

/// What one read of the server's settings came to.
enum ServerSettingsReading: Equatable {
    case read(ServerSettingsAnswer)
    case unavailable(String)
}

/// Reads the server Eavesdropper's allowlisted settings through the editor's get.  It changes
/// nothing, so it asks for no Touch ID and writes no audit row.
struct ServerListenerSettingsReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read() async -> ServerSettingsReading {
        let invocation = makeBuilder().listenerSettingsReadInvocation()
        let result: OpsProcessResult
        do {
            result = try await runner.run(invocation)
        } catch {
            return .unavailable("The server could not be asked:  \(error.localizedDescription)")
        }
        return Self.reading(result)
    }

    static func reading(_ result: OpsProcessResult) -> ServerSettingsReading {
        if result.timedOut {
            return .unavailable("The server did not answer in time, so its settings are not known.")
        }
        guard let answer = ServerSettingsAnswer(stdout: result.stdout) else {
            if result.exitCode == 255 {
                return .unavailable("The server could not be reached over the fleet-ops-ctl route.")
            }
            let words = OpsResultParser.decode(result.stdout)?.detail.map(OpsResultParser.sanitizedForDisplay)
            return .unavailable("The server gave no settings" + (words.map { ":  \($0)" } ?? ".") )
        }
        guard answer.ok, answer.result == "read" else {
            return .unavailable("The server refused the read:  \(answer.detail ?? answer.errorCode ?? answer.result)")
        }
        return .read(answer)
    }
}

/// The server settings section's store.  It lives as long as the app, so an edit the owner has
/// not planned is still there when the panel opens again.
@MainActor
final class ServerListenerSettingsStore: ObservableObject {
    static let shared = ServerListenerSettingsStore()

    @Published private(set) var reading: ServerSettingsReading?
    @Published private(set) var isReading = false
    @Published private(set) var message: String?
    /// The edited values, by row id.
    @Published var drafts: [String: String] = [:]

    private let reader: ServerListenerSettingsReader
    private let readAllowlist: () -> ServerSettingsAllowlist?
    private let controller: OpsController

    init(
        reader: ServerListenerSettingsReader = ServerListenerSettingsReader(),
        readAllowlist: @escaping () -> ServerSettingsAllowlist? = {
            ServerSettingsAllowlist.read(at: ServerSettingsAllowlist.url(repoRoot: FleetConfigLocation.resolve().repoRoot))
        },
        controller: OpsController = .shared
    ) {
        self.reader = reader
        self.readAllowlist = readAllowlist
        self.controller = controller
    }

    /// This checkout's allowlist copy, or nil when it could not be read:  then nothing is offered.
    var allowlist: ServerSettingsAllowlist? { readAllowlist() }

    var groups: [ServerSettingsGroup] {
        guard case .read(let answer)? = reading, let allowlist else { return [] }
        return ServerSettingsBuilder.groups(settings: answer.settings, allowlist: allowlist)
    }

    var applyEnabled: Bool? {
        if case .read(let answer)? = reading { return answer.applyEnabled }
        return nil
    }

    /// Set when a read is asked for while one is running, so one more read follows it and a read
    /// asked for after an apply is never dropped.
    private var readAgain = false

    /// Reads the server's settings.  Called while a read is running, it asks for one more read
    /// after that one, however many times it was called, so no call starts a read of its own.
    func refresh() async {
        guard !isReading else {
            readAgain = true
            return
        }
        isReading = true
        defer { isReading = false }
        repeat {
            readAgain = false
            reading = await reader.read()
        } while readAgain
    }

    func revert() {
        drafts = [:]
        message = nil
    }

    /// Shows the plan for the edits in the shared action sheet, which asks for Touch ID before it
    /// runs anything.  Nothing is written here.
    @discardableResult
    func showPlan() -> Bool {
        let rows = groups.flatMap(\.rows)
        switch ServerSettingsBuilder.request(edits: drafts, rows: rows) {
        case .failure(let problem):
            message = problem.sentence
            return false
        case .success(let request):
            message = nil
            let started = controller.plan(.serverListenerSettings(request))
            if !started { message = OpsControlError.busy.errorDescription }
            return started
        }
    }
}
