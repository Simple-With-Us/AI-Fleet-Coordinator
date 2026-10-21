import Foundation

/// The read-only view of the file or directory a rule checks.
///
/// The panel shows the artifact itself,  the way an operator wants to see it,  and
/// never an editor:  the app reads a fleet surface,  and a fleet surface is not
/// the app's to change.
enum ConfigFileView {
    /// A file, with its text.
    case file(path: String, text: String, truncated: Bool, redacted: Bool)
    /// A directory, with the names it holds.
    case directory(path: String, entries: [String])
    /// The path does not exist on this Mac.
    case missing(path: String)
    /// The path is refused,  and the panel says why instead of reading it.
    case refused(path: String, reason: String)
    /// Nothing names a path for this rule,  so there is no artifact to read.
    case unresolved(reason: String)

    /// The heading the panel shows above the body.
    var path: String {
        switch self {
        case .file(let path, _, _, _): return path
        case .directory(let path, _): return path
        case .missing(let path): return path
        case .refused(let path, _): return path
        case .unresolved: return "no artifact"
        }
    }

    /// A one-line summary of what the body holds.
    var summary: String {
        switch self {
        case .file(_, _, let truncated, let redacted):
            if redacted && truncated { return "the file, truncated, with secret values redacted" }
            if redacted { return "the file, with secret values redacted" }
            return truncated ? "the file, truncated" : "the file"
        case .directory(_, let entries):
            return "a directory with \(entries.count) entries"
        case .missing:
            return "not on this Mac"
        case .refused:
            return "refused"
        case .unresolved:
            return "no path declared"
        }
    }
}

/// Reads the artifact a rule checks,  for the panel to show.
enum ConfigFileReader {
    /// The largest body the panel will read,  so a huge file cannot wedge the window.
    static let byteLimit = 512 * 1024

    /// Read the target of a rule.
    ///
    /// A credential path is refused rather than read.  The manifest points a
    /// `permissions` rule at the seat's own secret file,  and a fleet admin panel
    /// that renders a Zulip key on screen is a leak,  not a feature.
    static func read(target: String, checkType: String?, home: URL) -> ConfigFileView {
        if let reason = refusalReason(target: target, checkType: checkType) {
            return .refused(path: target, reason: reason)
        }
        let url = resolve(target: target, home: home)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
            return .missing(path: target)
        }
        if isDirectory.boolValue {
            let names = (try? FileManager.default.contentsOfDirectory(atPath: url.path))?.sorted() ?? []
            return .directory(path: target, entries: names)
        }
        guard let data = try? Data(contentsOf: url) else {
            return .missing(path: target)
        }
        let truncated = data.count > byteLimit
        let slice = truncated ? data.prefix(byteLimit) : data
        let text = String(data: slice, encoding: .utf8)
            ?? "This file is not text, so the panel shows it as bytes."
        // A config file can hold a token beside the setting the operator came for,  so
        // the text is redacted before it reaches the screen.
        let redacted = redact(text)
        return .file(path: target, text: redacted, truncated: truncated, redacted: redacted != text)
    }

    /// Why a path may not be read,  when it may not.
    ///
    /// A path that is itself a credential is refused outright.  A path that is merely
    /// a config file which happens to hold one is not:  the operator needs to see that
    /// the MCP servers are registered and the hooks are wired,  and the names of the
    /// servers are the point of the file.  Those files are redacted instead,  so the
    /// values never reach the screen.
    static func refusalReason(target: String, checkType: String?) -> String? {
        let kind = (checkType ?? "").lowercased()
        if kind.hasPrefix("credential") {
            return "This rule checks a credential, and the panel never reads one."
        }
        let lowered = target.lowercased()
        if lowered.contains("/.secrets/") || lowered.hasPrefix("/.secrets/") {
            return "This path holds a credential, and the panel never reads one."
        }
        if lowered.hasPrefix("/zulip") {
            return "This path is the secret store, and the panel never reads one."
        }
        return nil
    }

    /// The key names whose values the panel replaces before showing a file.
    ///
    /// A config file carries the setting that makes a seat work and,  often in the
    /// same file,  the token that authenticates it.  The panel shows the first and
    /// hides the second:  the value of `apiKey` is never read onto the screen,  and
    /// the key name still tells the operator which one is set.
    static let secretKeyNames: Set<String> = [
        "apikey", "api_key", "api-key",
        "token", "accesstoken", "access_token", "refresh_token", "refreshtoken",
        "id_token", "idtoken", "bearer_token", "bearertoken",
        "secret", "clientsecret", "client_secret",
        "password", "passwd",
        "authorization", "x-api-key", "x_api_key",
        "sentry_access_token", "sentryaccesstoken",
        "privatekey", "private_key",
        "credential", "credentials",
    ]

    /// Whether a key name names a value the panel must not show.
    static func isSecretKey(_ key: String) -> Bool {
        let lowered = key.lowercased()
        if secretKeyNames.contains(lowered) { return true }
        // A name that ends in a secret word is a secret too:  `anthropicApiKey` and
        // `openai_api_key` are both one.
        for name in secretKeyNames where name.count >= 4 {
            if lowered.hasSuffix(name) { return true }
        }
        return false
    }

    /// The text shown in place of a secret value.
    static let redactionMark = "<redacted>"

    /// Replace every secret value in a JSON or TOML text with a mark.
    ///
    /// The replacement is by key name,  so a value is only hidden when its key says
    /// it is a secret.  A key that holds a path,  a command or a server name is left
    /// alone,  because that is the information the file is for.
    static func redact(_ text: String) -> String {
        // A JSON document:  walk it and rewrite the secret values.
        if let data = text.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data),
           let redacted = try? JSONSerialization.data(
               withJSONObject: redactJSON(object),
               options: [.prettyPrinted, .sortedKeys]
           ),
           let out = String(data: redacted, encoding: .utf8) {
            return out
        }
        // A TOML or INI document:  rewrite the values on the lines whose key is a
        // secret,  and leave every other line byte for byte.
        var lines: [String] = []
        for line in text.components(separatedBy: "\n") {
            lines.append(redactLine(line))
        }
        return lines.joined(separator: "\n")
    }

    /// One JSON value, with the secret values inside it replaced.
    private static func redactJSON(_ value: Any) -> Any {
        if let map = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (key, child) in map {
                if isSecretKey(key) {
                    out[key] = redactionMark
                } else {
                    out[key] = redactJSON(child)
                }
            }
            return out
        }
        if let list = value as? [Any] {
            return list.map { redactJSON($0) }
        }
        return value
    }

    /// One line of a TOML or INI document, with a secret value replaced.
    private static func redactLine(_ line: String) -> String {
        guard let separator = line.firstIndex(where: { $0 == "=" || $0 == ":" }) else {
            return line
        }
        let key = String(line[line.startIndex..<separator]).trimmingCharacters(in: .whitespaces)
        // A TOML key can be quoted,  and a table header has no value at all.
        let bareKey = key.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        guard !bareKey.isEmpty, isSecretKey(bareKey) else { return line }
        let valueStart = line.index(after: separator)
        let padding = line[valueStart...].prefix(while: { $0 == " " || $0 == "\t" })
        let rest = String(line[valueStart...].dropFirst(padding.count))
        // A section header such as `[headers]` carries no value.
        guard !rest.isEmpty, rest != "[]" else { return line }
        let quote = rest.first == "\"" || rest.first == "'"
        let prefix = String(line[..<valueStart]) + String(padding)
        return prefix + redactionMark + (quote ? String(rest.prefix(1)) : "")
    }

    /// The absolute path a target names.
    static func resolve(target: String, home: URL) -> URL {
        if target == "~" { return home }
        if target.hasPrefix("~/") {
            return home.appendingPathComponent(String(target.dropFirst(2)))
        }
        if target.hasPrefix("/") {
            return URL(fileURLWithPath: target)
        }
        return URL(fileURLWithPath: target, relativeTo: home)
    }
}
