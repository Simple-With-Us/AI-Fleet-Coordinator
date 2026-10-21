import Foundation

/// The read-only view of the file or directory a rule checks.
///
/// The panel shows the artifact itself,  the way an operator wants to see it,  and
/// never an editor:  the app reads a fleet surface,  and a fleet surface is not
/// the app's to change.
enum ConfigFileView {
    /// A file, with its text.
    case file(path: String, text: String, truncated: Bool)
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
        case .file(let path, _, _): return path
        case .directory(let path, _): return path
        case .missing(let path): return path
        case .refused(let path, _): return path
        case .unresolved: return "no artifact"
        }
    }

    /// A one-line summary of what the body holds.
    var summary: String {
        switch self {
        case .file(_, _, let truncated):
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
        return .file(path: target, text: text, truncated: truncated)
    }

    /// Why a path may not be read,  when it may not.
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
