import Foundation
import Combine

// The server Eavesdropper's own settings.
//
// Since the Nov 10 one-listener cutover the server listener holds every seat, and its live config is
// `/data/listener.toml` inside the server's `eavesdropper` container.  Muster cannot write that
// file, and must not:  the one writer is the server's `eavesdropper-settings` editor
// (`scripts/simple_tools/server/eavesdropper_settings_edit.py`), reached through
// `fleet-ops-control.py server eavesdropper-settings server` over the `fleet-ops-ctl` alias.
//
// Reads use the editor's get over the existing forced command.  The settings panel edits every
// allowlisted nonsecret field and coalesces changes into one revision-fenced autosave at a time.
// OwnerCheck runs automatically before a write, without an action sheet or Apply button.
// Saved requires canonical Infisical readback, the exact requested typed values and confirmed
// runtime application.  A failed or uncertain save keeps the draft and never blindly retries.
// Identity/credential pins remain separate status, and routine references are only existing or
// canonical variable names.  The server's allowlist remains authoritative.
// The old plan/apply request types below remain for existing non-panel callers; the live panel
// uses ServerSettingsSaveRequest and ServerListenerSettingsSaver exclusively (AFC-787/AFC-789).

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
    /// The longest document read, in UTF-8 bytes (2 MiB).  The real answer is a few tens of
    /// kilobytes, and the process runner already stops at `OpsProcessRunner.maximumOutputBytes`.
    static let maximumBytes = 2_097_152
    /// The most values one document may hold, every table, list, string, number and literal
    /// counted, so a document dense with tiny values is refused too.
    static let maximumElements = 200_000

    static func parse(_ text: String) -> ServerSettingValue? {
        // Checked before the byte array is built, so an oversized text is never copied.
        guard text.utf8.count <= maximumBytes else { return nil }
        var reader = Reader(bytes: Array(text.utf8))
        reader.skipSpace()
        guard let value = reader.value(depth: 0) else { return nil }
        reader.skipSpace()
        return reader.atEnd ? value : nil
    }

    private struct Reader {
        let bytes: [UInt8]
        var index = 0
        /// The values still allowed:  each `value(depth:)` call spends one.
        var budget = ServerSettingJSON.maximumElements

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
            guard depth <= ServerSettingJSON.maximumDepth, budget > 0, !atEnd else { return nil }
            budget -= 1
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
            // A length that is not a whole number (`true`, `8.5`) is not read as one:  the entry is
            // left out, so its key is not offered.
            guard let pattern = object["pattern"] as? String, !pattern.isEmpty,
                  let length = bound(object["maxLength"]), length == length.rounded(),
                  (0.0...256.0).contains(length) else { return nil }
            return .text(pattern: pattern, maxLength: Int(length))
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

// MARK: - Where the allowlist copy comes from

/// The result of looking for the allowlist copy:  the copy and where it came from, or why there is
/// none, in words that name what was tried.
struct ServerSettingsAllowlistLoad: Equatable {
    enum Source: Equatable {
        /// The copy built into this app (`Contents/Resources`).
        case bundled
        /// The copy in the fleet checkout, used when the app holds none of its own.
        case checkout
    }

    var allowlist: ServerSettingsAllowlist?
    var source: Source?
    /// Why there is no copy, or nil when there is one.  It names both places tried.
    var problem: String?
}

enum ServerSettingsAllowlistLoader {
    static let resourceName = "fleet-ops-eavesdropper-settings"
    static let resourceExtension = "json"

    /// The copy built into the app, or nil when this build holds none.  A build run from Xcode's
    /// test host, or one made before the copy was bundled, holds none.
    static func bundledURL(in bundle: Bundle = .main) -> URL? {
        bundle.url(forResource: resourceName, withExtension: resourceExtension)
    }

    /// The parsed copy, or why the file's text is not one.  The words say what is wrong, and never
    /// quote a value.  The file is parsed once.
    static func parse(_ data: Data) -> (list: ServerSettingsAllowlist?, problem: String?) {
        guard let object = try? JSONSerialization.jsonObject(with: data) else { return (nil, "it is not valid JSON") }
        guard let dictionary = object as? [String: Any] else { return (nil, "it is not a JSON object") }
        if !(dictionary["seat"] is [String: Any]) { return (nil, "it has no seat table") }
        if !(dictionary["tables"] is [String: Any]) { return (nil, "it has no tables") }
        guard let list = ServerSettingsAllowlist.parse(data) else { return (nil, "it could not be read") }
        return (list, nil)
    }

    /// Why a file's text is not an allowlist copy, or nil when it is one.
    static func parseProblem(_ data: Data) -> String? {
        parse(data).problem
    }

    private enum Attempt {
        case found(ServerSettingsAllowlist)
        case failed(String)
    }

    private static func attempt(at url: URL, fileManager: FileManager) -> Attempt {
        guard fileManager.fileExists(atPath: url.path) else { return .failed("missing at \(url.path)") }
        guard let data = try? Data(contentsOf: url) else { return .failed("could not be opened at \(url.path)") }
        let parsed = parse(data)
        if let list = parsed.list { return .found(list) }
        return .failed("parse failed: \(parsed.problem ?? "it could not be read")")
    }

    /// The bundled copy first, then the checkout's.  Both failing is one sentence naming both.
    static func load(bundled: URL?, checkout: URL?, fileManager: FileManager = .default) -> ServerSettingsAllowlistLoad {
        let gap = ServerStatusCopy.sentenceGap
        var bundledWhy = "not bundled"
        if let bundled {
            switch attempt(at: bundled, fileManager: fileManager) {
            case .found(let list): return ServerSettingsAllowlistLoad(allowlist: list, source: .bundled, problem: nil)
            case .failed(let why): bundledWhy = "bundled file \(why)"
            }
        }
        var checkoutWhy = "no checkout to look in"
        if let checkout {
            switch attempt(at: checkout, fileManager: fileManager) {
            case .found(let list): return ServerSettingsAllowlistLoad(allowlist: list, source: .checkout, problem: nil)
            case .failed(let why): checkoutWhy = "checkout file \(why)"
            }
        }
        return ServerSettingsAllowlistLoad(
            allowlist: nil, source: nil,
            problem: "The allowlist that says what each setting may take could not be read, so Muster offers nothing to change.\(gap)Update Muster, or open it from the checkout.\(gap)In the app: \(bundledWhy).\(gap)In the checkout: \(checkoutWhy)."
        )
    }

    /// The copy as the app finds it:  its own, then the fleet checkout's.
    static func standard() -> ServerSettingsAllowlistLoad {
        load(
            bundled: bundledURL(),
            checkout: ServerSettingsAllowlist.url(repoRoot: FleetConfigLocation.resolve().repoRoot)
        )
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
    var action: String?
    var source: String?
    var revision: String?
    var persisted: Bool?
    var readback: Bool?
    var runtimeApplied: Bool?
    var autosaveEnabled: Bool?
    var channelNames: [String] = []
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
    /// False when the document is not one this app can read in full:  `result` is missing or not
    /// text, `ok` is missing or not true or false, `changes`, `diff` or `check` is there and of
    /// another type, or any element of `changes` or `diff` could not be read.  Such an answer is
    /// never a plan anyone may approve or an apply that is done (`outcome`), because what it shows
    /// may be less than what the server would do.  A key or section this app does not know is not
    /// a fault:  the real server's answer carries several.
    var isWellFormed = true

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
        // A missing or mistyped `result` or `ok` still reads as an empty word and false, and the
        // answer is marked not well formed, so it can never pass for a plan or an apply.
        var wellFormed = true
        if let word = text("result", in: document) {
            result = word
        } else {
            result = ""
            wellFormed = false
        }
        if let said = flag("ok", in: document) {
            ok = said
        } else {
            ok = false
            wellFormed = false
        }
        applyEnabled = flag("applyEnabled", in: document)
        action = text("action", in: document)
        source = text("source", in: document)
        revision = text("revision", in: document).flatMap { Self.isHash($0) ? $0 : nil }
        persisted = flag("persisted", in: document)
        readback = flag("readback", in: document)
        runtimeApplied = flag("runtimeApplied", in: document)
        autosaveEnabled = flag("autosaveEnabled", in: document)
        if case .list(let names)? = document["channelNames"], names.count <= 1000 {
            channelNames = names.compactMap { value in
                guard case .text(let name) = value, !name.isEmpty, name.count <= 60,
                      !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
                return name
            }
        }
        baseSHA256 = text("baseSHA256", in: document).flatMap { Self.isHash($0) ? $0 : nil }
        planSHA256 = text("planSHA256", in: document).flatMap { Self.isHash($0) ? $0 : nil }
        settings = document["settings"]?.tableEntries ?? [:]
        if document["settings"] != nil, document["settings"]?.tableEntries == nil { wellFormed = false }
        if document["source"] != nil, source == nil { wellFormed = false }
        if document["revision"] != nil, revision == nil { wellFormed = false }
        for key in ["persisted", "readback", "runtimeApplied", "autosaveEnabled"] {
            if document[key] != nil, flag(key, in: document) == nil { wellFormed = false }
        }
        if let raw = document["changes"] {
            if case .list(let items) = raw {
                changes = items.compactMap { item in
                    guard let entry = item.tableEntries, let key = text("key", in: entry), let to = entry["to"] else { return nil }
                    var removes: [String] = []
                    if let rawRemoves = entry["removes"] {
                        guard case .list(let names) = rawRemoves else { return nil }
                        removes = names.compactMap { if case .text(let name) = $0 { return name } else { return nil } }
                        // A name that is not text is a change this app cannot read in full.
                        guard removes.count == names.count else { return nil }
                    }
                    let from = entry["from"].flatMap { $0 == .null ? nil : $0 }
                    return Change(key: key, from: from, to: to, removes: removes)
                }
                // A change that could not be read is never dropped quietly:  the plan would show
                // fewer changes than the server would make.
                if changes.count != items.count { wellFormed = false }
            } else {
                wellFormed = false
            }
        }
        if let raw = document["diff"] {
            if case .list(let lines) = raw {
                diff = lines.compactMap { if case .text(let line) = $0 { return OpsResultParser.sanitizedForDisplay(line) } else { return nil } }
                if diff.count != lines.count { wellFormed = false }
            } else {
                wellFormed = false
            }
        }
        if let raw = document["check"] {
            if let check = raw.tableEntries {
                checkOK = flag("ok", in: check)
                if case .list(let problems)? = check["problems"] {
                    checkProblems = problems.map { OpsResultParser.sanitizedForDisplay($0.display) }
                }
            } else {
                wellFormed = false
            }
        }
        isWellFormed = wellFormed
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
            // The helper's own word stands when it says the run was refused or failed, and when it
            // says an apply is not known:  an apply whose link dropped may have worked, so it is
            // never made a failure that invites a blind retry.  A plan changes nothing, so a plan
            // that is not known falls through and fails.
            if helperKind == .refused || helperKind == .failed || (helperKind == .unknown && mode == .yes) {
                return (helperKind, detail)
            }
            // Any other word with no document behind it is not a plan or an apply this app can
            // stand behind;  after an apply was sent it may still have landed, so it is not known.
            return (mode == .yes ? .unknown : .failed,
                    "The server's answer carried no settings document, so nothing is known about it.")
        }
        switch (mode, answer.result) {
        case (.plan, "planned"):
            guard answer.isWellFormed else {
                return (.failed, "The server's plan was not well formed, so it was not used and cannot be approved.")
            }
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
            // The server says it applied, and its answer could not be read in full:  the change
            // may have landed, so the result is not known rather than failed.
            guard answer.isWellFormed else {
                return (.unknown, "The server's answer to the apply was not well formed, so it was not used.\(gap)Read the server's settings again before changing anything.")
            }
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

/// Typed edits by full key, shared by legacy plan/apply and the canonical autosave builder.
/// Legacy plan/apply sends only seat structure; autosave may explicitly remove a table override.
struct ServerListenerSettingsRequest: Equatable {
    var set: [String: ServerSettingValue]
    /// Used only by typed autosave; legacy plan/apply callers never remove fields.
    var remove: [String] = []
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
        guard remove.isEmpty, !set.isEmpty, set.count <= Self.maximumChanges, set.keys.allSatisfy(Self.isOfferableKey) else { return false }
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
    case pairs
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
    var isLimit: Bool { subKey != nil && (settingKey.hasSuffix(".budget") || settingKey.hasSuffix(".live")) }

    var editText: String {
        if case .pairs? = control { return ServerPresenceTopics.canonical(effective, names: channelNames)?.json ?? "[]" }
        return effective?.fieldText ?? ""
    }

    /// The value the daemon acts on:  the server's answer, else the daemon's default.
    var effective: ServerSettingValue? { current ?? defaultValue }
    var channelNames: [String] = []
    var information: String? = nil

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

    static func groupCaption(table: String) -> String? {
        switch table {
        case "linear_projects": return "Keep Linear project codes linked in Zulip and fleet references."
        case "linear_comments": return "Keep one comment per linked issue updated from each public Zulip topic."
        default: return nil
        }
    }

    static let neverOffered = "A pin or a credential name.\(gap)The server's editor refuses it whatever its allowlist says, so Muster never offers it, and the server never sends its value."
    /// The server editor's rule (`seat_env_names` in `eavesdropper_settings_edit.py`):  a routine's
    /// `url_env` and `key_env` may stay as they are, or be one of the two names it derives from the
    /// seat's code (the seat's name in capitals, every other character an underscore).
    static let variableName = "This names the variable that holds the routine's address or key, never its value.\(gap)The server lets it stay as it is or be one of two names made from the seat's code:  CODE_ROUTINE_URL or ZULIP_ALERT_CODE_ENDPOINT for the address, and CODE_ROUTINE_KEY or ZULIP_ALERT_CODE_KEY for the key.\(gap)Muster shows the name, never a value, and offers no control."

    /// A typed value with a credential's shape is never sent.  The sentence names the row and
    /// never the value or any part of it.
    static func credentialShaped(_ title: String) -> String {
        "\(title) has the shape of a credential, so Muster did not send it.\(gap)The server's listener file holds names and settings only, never a secret."
    }

    /// An edit whose row is not among the settings last read, so it is never sent or dropped quietly.
    static func notRead(_ id: String) -> String {
        "\(id) is not among the settings last read from the server, so nothing was planned.\(gap)Read the server's settings again, or revert the edit."
    }
    static let routerContract = "How a routine is signed and sent is fixed by the receiver it calls, so Muster shows it and offers no control for it."
    static let daemonDefault = "The file sets no value here, so this is the daemon's default."
    static let pairs = "A list of channel and topic pairs.\(gap)Muster shows it and offers no control for it."
    static let notEnforced = "The eavesdropper counts wakes and not dollars, so this cap is never enforced and Muster will not write it."
    static let usdRecordedOnly = "This amount is recorded, but dollar spending is not enforced.\(gap)Wake limits are enforced."
    static let noRoutine = "The seat has no routine table, so it Captures Only.\(gap)Muster does not give a seat a routine."
    static let notOnThisCopy = "The server's allowlist lists this, and this checkout's copy does not, so Muster does not offer it.\(gap)Update the checkout to offer it."

    /// Owner's rule (Nov 11, 2026):  a tunable knob (a timing, a cap, a feature switch) has its home in
    /// Infisical.  Only a seat's routing structure has no Infisical home, so only that is written through
    /// the server's settings editor.
    static let knobHome = "A knob, and a knob's home is Infisical.\(gap)Muster shows the value the server holds and does not write it into the server's listener file.\(gap)It becomes editable in Controls once it is a registered knob."

    static let applyOff = "Applying changes is switched off on the server.\(gap)Show Plan still works, and an apply is refused until the owner switches it on."
    static let applyUnknown = "The server did not say whether applying is switched on."
    static let applyOn = "Each change goes Show Plan, then Touch ID, then the server plans again and applies only the plan you approved."

    static func groupTitle(table: String) -> String {
        switch table {
        case "daemon": return "Daemon"
        case "linear_comments": return "Linear Comment Mirror"
        case "linear_projects": return "Linear Project Label Sync"
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
    static func groups(settings: [String: ServerSettingValue], allowlist: ServerSettingsAllowlist, editable: Bool = false,
                       channelNames: [String] = []) -> [ServerSettingsGroup] {
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
                                  isStructure: seatStructureKeys.contains(name), editable: editable)
            }
            rows += (unknownByGroup[prefix] ?? []).sorted().map { readOnly($0, current: settings[$0], reason: ServerSettingsCopy.notOnThisCopy) }
            if !editable {
                rows += ServerSettingsAllowlist.neverOfferedSeatKeys.map {
                    readOnly("\(prefix).\($0)", current: nil, reason: ServerSettingsCopy.neverOffered)
                }
            }
            groups.append(ServerSettingsGroup(id: prefix, title: "Seat \(seat)", rows: rows))
        }
        let tableNames = Set(allowlist.tables.keys).union(unknownByGroup.keys.filter { !$0.hasPrefix("seat.") })
        for table in tableNames.sorted(by: { ($0 == "daemon" ? "" : $0) < ($1 == "daemon" ? "" : $1) }) {
            var rows: [ServerSettingRow] = []
            for name in (allowlist.tables[table] ?? [:]).keys.sorted() {
                guard let spec = allowlist.tables[table]?[name] else { continue }
                rows += self.rows(key: "\(table).\(name)", name: name, spec: spec, current: settings["\(table).\(name)"],
                                  hasRoutine: false, isStructure: false, editable: editable)
            }
            rows += (unknownByGroup[table] ?? []).sorted().map { readOnly($0, current: settings[$0], reason: ServerSettingsCopy.notOnThisCopy) }
            if table == "daemon", !editable {
                rows += ServerSettingsAllowlist.neverOfferedDaemonKeys.map {
                    readOnly("daemon.\($0)", current: nil, reason: ServerSettingsCopy.neverOffered)
                }
            }
            groups.append(ServerSettingsGroup(id: table, title: ServerSettingsCopy.groupTitle(table: table), rows: rows))
        }
        return groups.map { group in
            ServerSettingsGroup(id: group.id, title: group.title, rows: group.rows.map { row in
                var row = row
                if row.settingKey == "daemon.presence_topics" { row.channelNames = channelNames }
                return row
            })
        }
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
                             hasRoutine: Bool, isStructure: Bool, editable: Bool) -> [ServerSettingRow] {
        if editable {
            func editableControl(_ spec: ServerSettingSpec, sub: String? = nil, current: ServerSettingValue? = nil) -> ServerSettingControl? {
                switch spec {
                case .pairs: return .pairs
                case .seatEnv:
                    let parts = key.split(separator: ".")
                    guard parts.count == 3, parts[0] == "seat", let sub else { return nil }
                    let seat = String(parts[1]).replacingOccurrences(of: "-", with: "_")
                    var names = sub == "url_env"
                        ? [seat + "_ROUTINE_URL", "ZULIP_ALERT_" + seat + "_ENDPOINT"]
                        : [seat + "_ROUTINE_KEY", "ZULIP_ALERT_" + seat + "_KEY"]
                    if case .text(let existing)? = current, !names.contains(existing) { names.insert(existing, at: 0) }
                    return .choice(names)
                default: return control(spec)
                }
            }
            if case .table(let keys) = spec {
                let entries = current?.tableEntries ?? [:]
                return keys.keys.sorted().map { sub in
                    ServerSettingRow(id: "\(key).\(sub)", settingKey: key, subKey: sub,
                                     title: "\(ServerSettingsCopy.rowTitle(name)): \(ServerSettingsCopy.rowTitle(sub))",
                                     current: entries[sub], control: keys[sub].flatMap { editableControl($0, sub: sub, current: entries[sub]) },
                                     readOnlyReason: nil, table: entries,
                                     information: name == "budget" && sub == "usd_per_day" ? ServerSettingsCopy.usdRecordedOnly : nil)
                }
            }
            return [ServerSettingRow(id: key, settingKey: key, subKey: nil,
                                     title: ServerSettingsCopy.rowTitle(name), current: current,
                                     control: editableControl(spec), readOnlyReason: nil,
                                     defaultValue: isStructure ? seatStructureDefaults[name] : nil)]
        }
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
        // Defence in depth, before any control reads the text:  no row takes free text today, and
        // a value with a credential's shape is refused whatever the row, by the app's own detectors.
        guard !looksLikeCredential(trimmed) else {
            return .failure(.notAValue(ServerSettingsCopy.credentialShaped(row.title)))
        }
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
        case .pairs:
            guard case .list(let pairs)? = ServerSettingJSON.parse(trimmed), pairs.count <= 32 else {
                return .failure(.notAValue("\(row.title) needs at most 32 channel and topic pairs."))
            }
            for pair in pairs {
                guard case .list(let fields) = pair, fields.count == 2 else {
                    return .failure(.notAValue("\(row.title) needs a channel and a topic in every pair."))
                }
                for field in fields {
                    guard case .text(let value) = field, !value.isEmpty, value.count <= 60,
                          !value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
                          !looksLikeCredential(value) else {
                        return .failure(.notAValue("\(row.title) needs nonsecret channel and topic names of 1 to 60 characters."))
                    }
                }
            }
            return .success(ServerPresenceTopics.canonical(.list(pairs), names: row.channelNames) ?? .list(pairs))
        case .text(let pattern, let limit):
            guard trimmed.count <= limit,
                  let expression = try? NSRegularExpression(pattern: "^(?:\(pattern))$"),
                  expression.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)) != nil else {
                return .failure(.notAValue("\(row.title) does not have the form the server allows."))
            }
            return .success(.text(trimmed))
        }
    }

    /// Whether a typed value has the shape of a credential, by the detectors the app already uses
    /// to hide one:  the config panel's value check (`ConfigFileReader.isSecretString`:  a long
    /// opaque run, a URL with a credential in it, a `Bearer` or `Basic` value, a secret-named
    /// `name=value`) and the command-output scrubber (`FleetViewFailure.scrub`:  the GitHub,
    /// GitLab, OpenAI and Anthropic, Slack, Linear and AWS prefixes, a JWT, a base64 run).  It
    /// over-refuses on purpose, because a refused edit costs less than a relayed secret.
    static func looksLikeCredential(_ text: String) -> Bool {
        guard !text.isEmpty else { return false }
        return ConfigFileReader.isSecretString(text)
            || FleetViewFailure.scrub(text).contains(ConfigFileReader.redactionMark)
    }

    /// The request for a set of edits, by row id.  A table's setting is sent as the table the
    /// server answered with, the edits laid over it, so no sub-key is dropped.  An edit that holds
    /// the value the row already has is not sent.  An edit whose row is not among `rows` (a seat
    /// gone since the last read) plans nothing, so no edit is ever dropped from a plan quietly.
    static func request(edits: [String: String], rows: [ServerSettingRow], forAutosave: Bool = false,
                        removing: Set<String> = []) -> Result<ServerListenerSettingsRequest, EditProblem> {
        let byID = Dictionary(rows.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        var set: [String: ServerSettingValue] = [:]
        var tables: [String: [String: ServerSettingValue]] = [:]
        var remove: [String] = []
        guard removing.isDisjoint(with: Set(edits.keys)), forAutosave || removing.isEmpty else { return .failure(.notSendable) }
        for id in edits.keys.sorted() {
            guard let row = byID[id] else { return .failure(.notAValue(ServerSettingsCopy.notRead(id))) }
            guard let text = edits[id] else { continue }
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
        for id in removing.sorted() {
            guard let row = byID[id], row.isEditable, let sub = row.subKey,
                  row.settingKey.hasPrefix("seat."), let current = row.table else {
                return .failure(.notAValue(ServerSettingsCopy.notRead(id)))
            }
            guard current[sub] != nil else { continue }
            var table = tables[row.settingKey] ?? current
            table.removeValue(forKey: sub)
            tables[row.settingKey] = table
            remove.append(id)
        }
        for (key, table) in tables { set[key] = .table(table) }
        guard !set.isEmpty else { return .failure(.nothingChanged) }
        let request = ServerListenerSettingsRequest(set: set, remove: remove)
        guard forAutosave ? (ServerSettingsSaveRequest.validSet(set) && ServerSettingsSaveRequest.validRemovals(remove, set: set)) : request.isSendable else { return .failure(.notSendable) }
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
        guard answer.isWellFormed, answer.ok, answer.result == "read" else {
            return .unavailable("The server refused the read:  \(answer.detail ?? answer.errorCode ?? answer.result)")
        }
        return .read(answer)
    }
}

/// The server settings store keeps pending edits across panel closes and serializes autosaves.
@MainActor
final class ServerListenerSettingsStore: ObservableObject {
    static let shared = ServerListenerSettingsStore(saver: ServerListenerSettingsSaver())

    /// The last read.  Each new one reads this checkout's allowlist copy again and builds the rows
    /// once (`rebuild`), so drawing the section, or typing in it, reads no file and builds no row.
    @Published private(set) var reading: ServerSettingsReading? {
        didSet { rebuild() }
    }
    @Published private(set) var isReading = false
    @Published private(set) var message: String?
    @Published private(set) var refreshProblem: String?
    /// The edited values, by row id.
    @Published var drafts: [String: String] = [:]
    @Published private(set) var removalDrafts: Set<String> = []
    private var pendingIDs: Set<String> { Set(drafts.keys).union(removalDrafts) }

    private let reader: ServerListenerSettingsReader
    private let saver: ServerListenerSettingsSaver?
    private let debounceNanoseconds: UInt64
    private let waitForDebounce: (UInt64) async throws -> Void
    private var debounceTask: Task<Void, Never>?
    private var editVersions: [String: UInt64] = [:]
    @Published private(set) var isSaving = false
    @Published private(set) var saveStates: [String: ServerSettingsSaveState] = [:]
    private let loadAllowlist: () -> ServerSettingsAllowlistLoad
    private let controller: OpsController

    /// - Parameters:
    ///   - readAllowlist: a stand-in that answers with the copy alone, for a test.  It wins over
    ///     `loadAllowlist` when both are given.
    ///   - loadAllowlist: where the copy comes from.  The default is the app's own bundled copy,
    ///     then the fleet checkout's (`ServerSettingsAllowlistLoader.standard`).
    init(
        reader: ServerListenerSettingsReader = ServerListenerSettingsReader(),
        saver: ServerListenerSettingsSaver? = nil,
        debounceNanoseconds: UInt64 = 450_000_000,
        waitForDebounce: @escaping (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0) },
        readAllowlist: (() -> ServerSettingsAllowlist?)? = nil,
        loadAllowlist: @escaping () -> ServerSettingsAllowlistLoad = { ServerSettingsAllowlistLoader.standard() },
        controller: OpsController = .shared
    ) {
        self.reader = reader
        self.saver = saver
        self.debounceNanoseconds = debounceNanoseconds
        self.waitForDebounce = waitForDebounce
        if let readAllowlist {
            self.loadAllowlist = {
                let list = readAllowlist()
                return ServerSettingsAllowlistLoad(
                    allowlist: list, source: nil,
                    problem: list == nil ? "The allowlist that says what each setting may take could not be read, so Muster offers nothing to change." : nil
                )
            }
        } else {
            self.loadAllowlist = loadAllowlist
        }
        self.controller = controller
        rebuild()
    }

    /// The allowlist copy, or nil when it could not be read:  then nothing is offered.  Read at
    /// init and again with every new reading, never on a draw.
    private(set) var allowlist: ServerSettingsAllowlist?

    /// Why there is no allowlist copy, in words that name what was tried, or nil when there is one.
    private(set) var allowlistProblem: String?

    /// The rows of the last read, built once per reading.  An edit changes `drafts` only, never these.
    private(set) var groups: [ServerSettingsGroup] = []

    /// Reads the allowlist copy again and builds the rows for the current reading.
    private func rebuild() {
        let load = loadAllowlist()
        allowlist = load.allowlist
        allowlistProblem = load.problem
        guard case .read(let answer)? = reading, let allowlist else {
            groups = []
            return
        }
        groups = ServerSettingsBuilder.groups(settings: answer.settings, allowlist: allowlist, editable: true,
                                              channelNames: answer.channelNames)
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
        guard !isReading, !isSaving else {
            readAgain = true
            return
        }
        isReading = true
        defer {
            isReading = false
            if pendingIDs.contains(where: { saveStates[$0] == .saving }) { scheduleSave() }
        }
        repeat {
            readAgain = false
            let result = await reader.read()
            if case .unavailable(let why) = result {
                refreshProblem = why
                // A failed refresh must not make retained edits disappear from the panel.
                if pendingIDs.isEmpty || !hasBaseline { reading = result }
            } else {
                reading = result
                refreshProblem = nil
            }
        } while readAgain
    }

    private var hasBaseline: Bool {
        if case .read? = reading { return true }
        return false
    }

    func revert() {
        debounceTask?.cancel()
        debounceTask = nil
        saveStates = [:]
        editVersions = [:]
        drafts = [:]
        removalDrafts = []
        message = nil
    }

    /// Every edit schedules one coalesced write; an active write is never cancelled by typing.
    func edit(_ id: String, text: String) {
        guard let row = groups.flatMap(\.rows).first(where: { $0.id == id }), row.isEditable else { return }
        editVersions[id, default: 0] += 1
        removalDrafts.remove(id)
        if !isSaving, text == row.editText {
            drafts[id] = nil
            saveStates[id] = nil
        } else {
            drafts[id] = text
            saveStates[id] = .saving
        }
        message = nil
        scheduleSave()
    }

    /// Removing an optional cap is an explicit edit; zero remains an explicit numeric value.
    func removeOverride(_ id: String) {
        guard let row = groups.flatMap(\.rows).first(where: { $0.id == id }), row.isEditable, row.isLimit else { return }
        editVersions[id, default: 0] += 1
        drafts[id] = nil
        if row.current == nil, !isSaving {
            removalDrafts.remove(id)
            saveStates[id] = nil
        } else {
            removalDrafts.insert(id)
            saveStates[id] = .saving
        }
        message = nil
        scheduleSave()
    }

    private func scheduleSave() {
        debounceTask?.cancel()
        debounceTask = Task { [weak self] in
            guard let self else { return }
            do { try await self.waitForDebounce(self.debounceNanoseconds) } catch { return }
            guard !Task.isCancelled else { return }
            self.debounceTask = nil
            await self.savePending()
        }
    }

    /// One revision-fenced batch, also directly callable by deterministic model tests.
    func savePending() async {
        guard !isSaving, !isReading, !pendingIDs.isEmpty,
              pendingIDs.contains(where: { saveStates[$0] == .saving }) else { return }
        debounceTask?.cancel()
        debounceTask = nil
        let snapshot = drafts
        let removed = removalDrafts
        let ids = pendingIDs
        let versions = editVersions
        func fail(_ why: String) {
            debounceTask?.cancel()
            debounceTask = nil
            message = why
            // Also stop edits queued behind an uncertain write: none is blindly replayed.
            for id in pendingIDs { saveStates[id] = .failed(why) }
        }
        guard let saver else {
            fail("Settings saving is unavailable in this instance, so the edit was not sent.")
            return
        }
        guard case .read(let baseline)? = reading, baseline.source == "infisical",
              baseline.persisted == true, baseline.readback == true, baseline.autosaveEnabled == true,
              let revision = baseline.revision ?? baseline.baseSHA256 else {
            fail("The server has not confirmed its canonical settings revision, so the edit was not sent.")
            return
        }
        let request: ServerSettingsSaveRequest
        switch ServerSettingsBuilder.request(edits: snapshot, rows: groups.flatMap(\.rows), forAutosave: true, removing: removed) {
        case .failure(.nothingChanged):
            // A matching value is a read, not a successful write receipt.
            for id in ids where editVersions[id] == versions[id] {
                drafts[id] = nil
                removalDrafts.remove(id)
                saveStates[id] = nil
            }
            return
        case .failure(let problem): fail(problem.sentence); return
        case .success(let typed):
            request = ServerSettingsSaveRequest(set: typed.set, expectBaseSHA256: revision,
                                                expectedKeys: Set(baseline.settings.keys), remove: typed.remove)
        }
        isSaving = true
        let result = await saver.save(request)
        isSaving = false
        switch result {
        case .saved(let answer):
            var canonical = answer
            canonical.result = "read"
            if canonical.channelNames.isEmpty { canonical.channelNames = baseline.channelNames }
            reading = .read(canonical)
            refreshProblem = nil
            for id in ids {
                if editVersions[id] == versions[id], drafts[id] == snapshot[id],
                   removalDrafts.contains(id) == removed.contains(id) {
                    drafts[id] = nil
                    removalDrafts.remove(id)
                    saveStates[id] = .saved
                } else {
                    saveStates[id] = pendingIDs.contains(id) ? .saving : nil
                }
            }
            message = nil
            if !pendingIDs.isEmpty { scheduleSave() }
        case .failed(let why, let canonical):
            if var canonical {
                canonical.result = "read"
                if canonical.channelNames.isEmpty { canonical.channelNames = baseline.channelNames }
                reading = .read(canonical)
                refreshProblem = nil
            }
            fail(why)
        }
        if readAgain { await refresh() }
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
