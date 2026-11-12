import Foundation

/// The read-only view of the file or directory a rule checks.
///
/// The panel shows the artifact itself,  the way an operator wants to see it,  and
/// never an editor:  the app reads a fleet surface,  and a fleet surface is not
/// the app's to change.
///
/// This is the shape the panel draws.  `ConfigFileRead` carries the same artifact with
/// every state the reader tells apart (a file that is not text,  a path that could not be
/// read),  and the words for each.
enum ConfigFileView: Equatable {
    /// A file, with its text.
    ///
    /// `redacted` is true only when a value was replaced:  a file that held nothing to
    /// hide is shown as it is,  and it is not labelled as redacted.
    case file(path: String, text: String, truncated: Bool, redacted: Bool)
    /// A directory, with the names it holds.
    case directory(path: String, entries: [String])
    /// Nothing is at the path on this Mac.
    case missing(path: String)
    /// The panel does not show what is at the path,  and says why instead.
    ///
    /// A credential path is refused before it is read.  A path that is there and could
    /// not be read,  and a file that is not text,  come this way too,  each with its own
    /// sentence,  so neither one reads as a path that is not on this Mac.
    /// `ConfigFileRead.state` tells the three apart.
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
            return "not shown"
        case .unresolved:
            return "no path declared"
        }
    }
}

/// What the reader found at a rule's path,  with every state the panel tells apart.
///
/// `ConfigFileView` folds a file that is not text and a path that could not be read into
/// `refused`,  each with its own sentence,  because that is the shape the panel draws.
/// This keeps them apart,  with the words for each,  so a panel can say which it is:  a
/// missing path,  an unreadable one and a cut-off file are three different answers to
/// "where is this file".
struct ConfigFileRead: Equatable {
    /// What is at the path.
    enum State: Equatable {
        /// The whole file,  as text.
        case text
        /// The first part of a file longer than `ConfigFileReader.byteLimit`,  as text.
        case truncated
        /// A file whose bytes are not UTF-8 text,  so nothing of it is shown.
        case notText
        /// A directory,  with the names it holds.
        case directory
        /// Nothing is at the path.
        case missing
        /// Something is at the path,  and reading it failed.  The reason is the system's.
        case unreadable(reason: String)
        /// A credential,  which the panel never reads.
        case refused
        /// No path is declared for the rule.
        case unresolved
    }

    let state: State
    /// The artifact in the shape the panel draws.
    let view: ConfigFileView
    /// How many values were hidden.
    let redactedCount: Int
    /// Whether the text was rewritten for reading,  so its key order and spacing are not
    /// the file's own.  A JSON file is rewritten only when a value had to be hidden and
    /// could not be hidden in place.
    let reformatted: Bool
    /// Whether the file looks like JSON and was cut off,  so it could not be read as JSON
    /// and its values were hidden line by line.
    let cutOffJSON: Bool
    /// Whether the line reader could not check a line's values one by one,  so it hid every
    /// value on each line that could hold one.
    let failedClosed: Bool

    init(
        state: State,
        view: ConfigFileView,
        redactedCount: Int = 0,
        reformatted: Bool = false,
        cutOffJSON: Bool = false,
        failedClosed: Bool = false
    ) {
        self.state = state
        self.view = view
        self.redactedCount = redactedCount
        self.reformatted = reformatted
        self.cutOffJSON = cutOffJSON
        self.failedClosed = failedClosed
    }

    /// A one-line summary of what the body holds,  for the line beside the path.
    var summary: String {
        switch state {
        case .text, .truncated:
            var parts = [state == .truncated ? "the file's first part" : "the file"]
            if reformatted { parts.append("reformatted for reading") }
            if redactedCount == 1 {
                parts.append("with 1 secret value redacted")
            } else if redactedCount > 1 {
                parts.append("with \(redactedCount) secret values redacted")
            }
            return parts.joined(separator: ", ")
        case .notText:
            return "not text"
        case .directory:
            return view.summary
        case .missing:
            return "not on this Mac"
        case .unreadable:
            return "on this Mac, not readable"
        case .refused:
            return "refused"
        case .unresolved:
            return "no path declared"
        }
    }

    /// The sentence the panel shows in place of a body,  when there is no body to show.
    var message: String? {
        switch state {
        case .text, .truncated, .directory:
            return nil
        case .notText:
            return ConfigFileReader.notTextMessage
        case .missing:
            return ConfigFileReader.missingMessage
        case .unreadable(let reason):
            return ConfigFileReader.unreadableMessage(reason)
        case .refused, .unresolved:
            switch view {
            case .refused(_, let reason), .unresolved(let reason): return reason
            default: return nil
            }
        }
    }

    /// The sentences the panel shows above the body,  one per fact about how it was read.
    var notes: [String] {
        var notes: [String] = []
        if state == .truncated {
            notes.append("The file is longer than the panel reads, so this is its first part.")
        }
        if reformatted {
            notes.append(
                "The file was rewritten for reading, so its keys are in alphabetical order and its spacing is not the file's own."
            )
        }
        if cutOffJSON {
            notes.append(
                "The file was cut off before its end, so it could not be read as JSON, and its secret values were found line by line."
            )
        }
        if failedClosed {
            notes.append(ConfigFileReader.failedClosedNote)
        }
        return notes
    }
}

/// Reads the artifact a rule checks,  for the panel to show.
enum ConfigFileReader {
    /// The largest body the panel will read,  so a huge file cannot wedge the window.
    static let byteLimit = 512 * 1024

    /// What the panel says about a file that is not text.
    static let notTextMessage = "This file is not text, so it is not shown here."

    /// What the panel says about a path that is not there.
    static let missingMessage = "This path is not on this Mac, so there is no file to show."

    /// What the panel says when the line reader could not check values one by one.
    static let failedClosedNote =
        "The reader could not check this file's values one by one, so every value on a line that could hold one is hidden."

    /// What the panel says about a path that is there and could not be read.
    static func unreadableMessage(_ reason: String) -> String {
        "Something is at this path on this Mac, and it could not be read.\u{00A0} \(reason)"
    }

    /// Read the target of a rule,  in the shape the panel draws.
    static func read(target: String, checkType: String?, home: URL) -> ConfigFileView {
        inspect(target: target, checkType: checkType, home: home).view
    }

    /// Read the target of a rule,  with every state the reader tells apart.
    ///
    /// A credential path is refused rather than read.  The manifest points a
    /// `permissions` rule at the seat's own secret file,  and a fleet admin panel
    /// that renders a Zulip key on screen is a leak,  not a feature.
    ///
    /// A path that is not there is missing.  A path that is there and cannot be read,
    /// for a permission or a link to nothing,  is unreadable,  with the system's reason,
    /// and never reads as missing.
    static func inspect(target: String, checkType: String?, home: URL) -> ConfigFileRead {
        if let reason = refusalReason(target: target, checkType: checkType) {
            return ConfigFileRead(state: .refused, view: .refused(path: target, reason: reason))
        }
        let url = resolve(target: target, home: home)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
            // `fileExists` is false for a path that is not there,  and also for a link to
            // nothing and for a path inside a folder the app may not look into.  Only the
            // first is missing.
            if let problem = lookupProblem(at: url) {
                return unreadable(target, problem)
            }
            return ConfigFileRead(state: .missing, view: .missing(path: target))
        }
        if isDirectory.boolValue {
            do {
                let names = try FileManager.default.contentsOfDirectory(atPath: url.path).sorted()
                return ConfigFileRead(state: .directory, view: .directory(path: target, entries: names))
            } catch {
                // A folder that cannot be listed is not an empty folder.
                return unreadable(target, describe(error))
            }
        }
        // Read at most one byte past the limit, so a huge file is never
        // loaded whole just to be truncated (mirrors OpsControl's read).
        let data: Data
        do {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            data = try handle.read(upToCount: byteLimit + 1) ?? Data()
        } catch {
            return unreadable(target, describe(error))
        }
        let truncated = data.count > byteLimit
        let slice = truncated ? data.prefix(byteLimit) : data
        guard let text = decodeText(slice, truncated: truncated) else {
            return ConfigFileRead(state: .notText, view: .refused(path: target, reason: notTextMessage))
        }
        // A config file can hold a token beside the setting the operator came for,  so
        // the text is redacted before it reaches the screen.
        let hidden = redaction(of: text, truncated: truncated)
        return ConfigFileRead(
            state: truncated ? .truncated : .text,
            view: .file(path: target, text: hidden.text, truncated: truncated, redacted: hidden.replaced > 0),
            redactedCount: hidden.replaced,
            reformatted: hidden.reformatted,
            cutOffJSON: hidden.cutOffJSON,
            failedClosed: hidden.failedClosed
        )
    }

    /// The text of a file's bytes,  or nil when they are not UTF-8 text.
    ///
    /// A cut at the byte limit can land inside a character,  leaving up to three bytes of
    /// a sequence that goes on past the limit.  Those bytes are dropped,  so a long text
    /// file reads as text and never as "not text".
    static func decodeText(_ data: Data, truncated: Bool) -> String? {
        if let text = String(data: data, encoding: .utf8) { return text }
        guard truncated else { return nil }
        for drop in 1...3 where data.count > drop {
            if let text = String(data: data.dropLast(drop), encoding: .utf8) { return text }
        }
        return nil
    }

    /// An unreadable path,  in both shapes.
    private static func unreadable(_ target: String, _ reason: String) -> ConfigFileRead {
        ConfigFileRead(
            state: .unreadable(reason: reason),
            view: .refused(path: target, reason: unreadableMessage(reason))
        )
    }

    /// Why a path that `fileExists` does not see is still not simply missing,  if it is not.
    private static func lookupProblem(at url: URL) -> String? {
        do {
            // This does not follow a link,  so a link to a path that is gone is found here.
            _ = try FileManager.default.attributesOfItem(atPath: url.path)
            return "It is a link to something that is not there."
        } catch {
            return isNoSuchFile(error) ? nil : describe(error)
        }
    }

    /// Whether an error says the path is not there,  and nothing else.
    private static func isNoSuchFile(_ error: Error) -> Bool {
        let nsError = error as NSError
        if nsError.domain == NSCocoaErrorDomain,
           nsError.code == NSFileReadNoSuchFileError || nsError.code == NSFileNoSuchFileError {
            return true
        }
        if nsError.domain == NSPOSIXErrorDomain,
           nsError.code == Int(ENOENT) || nsError.code == Int(ENOTDIR) {
            return true
        }
        if let underlying = nsError.userInfo[NSUnderlyingErrorKey] as? NSError {
            return isNoSuchFile(underlying)
        }
        return false
    }

    /// The system's reason,  as one sentence.
    private static func describe(_ error: Error) -> String {
        let text = (error as NSError).localizedDescription
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return "The system gave no reason." }
        return text.hasSuffix(".") ? text : text + "."
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
        "pat",
    ]

    /// Endings that make any name a secret one:  `ZULIP_KEY`,  `GITHUB_PAT`.
    ///
    /// The separator is part of the ending,  so `monkey` and `compat` are not caught.
    static let secretKeySuffixes = ["_key", "-key", "_pat", "-pat"]

    /// Whether a key name names a value the panel must not show.
    static func isSecretKey(_ key: String) -> Bool {
        let lowered = key.lowercased()
        if secretKeyNames.contains(lowered) { return true }
        // A name that ends in a secret word is a secret too:  `anthropicApiKey` and
        // `openai_api_key` are both one.
        for name in secretKeyNames where name.count >= 4 {
            if lowered.hasSuffix(name) { return true }
        }
        for suffix in secretKeySuffixes where lowered.hasSuffix(suffix) {
            return true
        }
        return false
    }

    /// The text shown in place of a secret value.
    static let redactionMark = "<redacted>"

    /// Whether a value looks like a secret whatever its key is called.
    ///
    /// A key name is the first defence and not the whole of it:  a config file can
    /// carry a token under a name nobody thought of,  a URL with a password in it,  or a
    /// header value.  So the reader also looks at the shape of the value,  because a
    /// file that is read at all is a file that is on screen.
    static func looksLikeSecretValue(_ value: String) -> Bool {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count >= 12 else { return false }
        // A URL is a secret only when it carries a credential:  a password or a token
        // in its user part,  before an `@`,  a secret-named query value,  or a token-shaped
        // path segment or query value (a webhook's path,  `?key=…`).  A host and a port
        // (`localhost:8080`) are not a password.
        if let scheme = text.range(of: "://") {
            let afterScheme = text[scheme.upperBound...]
            let authority = afterScheme.prefix(while: { $0 != "/" && $0 != "?" && $0 != "#" })
            if let at = authority.lastIndex(of: "@") {
                let userinfo = String(authority[..<at])
                if userinfo.contains(":") || looksLikeSecretValue(userinfo) { return true }
            }
            let afterAuthority = afterScheme.dropFirst(authority.count)
            let path = afterAuthority.prefix(while: { $0 != "?" && $0 != "#" })
            for segment in path.split(separator: "/") where looksLikeOpaqueToken(String(segment)) {
                return true
            }
            if let query = afterScheme.firstIndex(where: { $0 == "?" || $0 == "#" }) {
                let pairs = afterScheme[afterScheme.index(after: query)...]
                    .split(whereSeparator: { $0 == "&" || $0 == ";" })
                for pair in pairs {
                    let name = String(pair.prefix(while: { $0 != "=" }))
                    guard pair.count > name.count + 1 else { continue }
                    let value = String(pair.dropFirst(name.count + 1))
                    if isSecretKey(name) || secretQueryNames.contains(name.lowercased()) { return true }
                    if looksLikeOpaqueToken(value) { return true }
                }
            }
            return false
        }
        // A long opaque run with no spaces:  a token,  a key,  a signature.
        if text.count >= 24 &&
            text.contains { $0.isLetter || $0.isNumber } &&
            !text.contains(" ") {
            // A path is not a secret even when it is long.
            if text.contains("/") { return false }
            return true
        }
        return false
    }

    /// Query names that carry a credential in a URL and are too short or too common to be
    /// secret names everywhere:  `?key=`,  `?sig=`,  `?signature=`,  `?code=`.
    static let secretQueryNames: Set<String> = ["key", "sig", "signature", "code"]

    /// Whether a word has the shape of a token:  long,  made of the characters a token
    /// uses,  with letters and digits,  and mixed case or very long.
    ///
    /// It is narrower than `looksLikeSecretValue`,  because a URL's path is mostly words:
    /// `fleet-config-2026-release` is a name,  and `T0123ABCDEFabcdef0123456` is a token.
    static func looksLikeOpaqueToken(_ word: String) -> Bool {
        guard word.count >= 20 else { return false }
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_~+=."))
        guard word.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return false }
        guard word.contains(where: \.isNumber), word.contains(where: \.isLetter) else { return false }
        let mixedCase = word.contains(where: \.isUppercase) && word.contains(where: \.isLowercase)
        return mixedCase || word.count >= 32
    }

    /// Words that follow `Bearer` in prose,  where no credential follows.
    private static let schemeProse: Set<String> = [
        "token", "tokens", "auth", "authentication", "header", "headers", "scheme",
        "credential", "credentials",
    ]

    /// Whether the word after `Bearer` is a credential rather than prose.
    ///
    /// A token has a digit or a sign in it,  or mixed case,  or length:  `Bearer x1`,
    /// `Bearer $TOKEN`.  "The bearer of the key" and "Bearer tokens" are prose.
    static func isBearerCredential(_ word: String) -> Bool {
        guard !word.isEmpty, !schemeProse.contains(word.lowercased()) else { return false }
        if word.contains(where: { !$0.isLetter }) { return true }
        let inner = word.dropFirst()
        return word.count >= 16 || inner.contains(where: \.isUppercase)
    }

    /// Whether the word after `Basic` is base64 credentials rather than prose.
    ///
    /// `Basic dXNlcjpwYXNz` is a credential,  and "Basic checks" and "A basic guard" are
    /// not:  the word must be at least 8 base64 characters with a digit,  a sign or a
    /// capital after its first letter.
    static func isBasicCredential(_ word: String) -> Bool {
        guard word.count >= 8 else { return false }
        let base64 = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "+/="))
        guard word.unicodeScalars.allSatisfy({ $0.isASCII && base64.contains($0) }) else { return false }
        let inner = word.dropFirst()
        return word.contains(where: { $0.isNumber || $0 == "+" || $0 == "/" || $0 == "=" })
            || inner.contains(where: \.isUppercase)
    }

    /// Whether a value carries a credential inside it,  as a header or a command does.
    ///
    /// `Authorization: Bearer abc` has spaces,  so its shape is not a token's,  and
    /// `--token abc` and `API_KEY=abc` hide the value behind a name.  This finds them:
    /// a `Bearer` or `Basic` scheme with a value after it,  a secret-named header,  a
    /// secret-named `name=value`,  a secret-named flag with a value after it,  and a
    /// long opaque word among other words.
    static func carriesCredential(_ value: String) -> Bool {
        let words = value
            .split(whereSeparator: { $0 == " " || $0 == "\t" || $0 == "\n" })
            .map(String.init)
        let edges = CharacterSet(charactersIn: "\"'`,;()[]{}")
        for (index, raw) in words.enumerated() {
            let word = raw.trimmingCharacters(in: edges)
            let lowered = word.lowercased()
            let next: String? = index + 1 < words.count
                ? words[index + 1].trimmingCharacters(in: edges)
                : nil
            if lowered == "bearer" {
                if let next, isBearerCredential(next) { return true }
                continue
            }
            if lowered == "basic" {
                if let next, isBasicCredential(next) { return true }
                continue
            }
            if lowered.hasSuffix(":"), next != nil, isSecretKey(String(lowered.dropLast())) {
                return true
            }
            if let equals = word.firstIndex(of: "="), equals != word.startIndex {
                let name = word[..<equals].trimmingCharacters(in: CharacterSet(charactersIn: "-"))
                let rest = word[word.index(after: equals)...]
                if !rest.isEmpty, isSecretKey(name) { return true }
            }
            // curl's `-H` takes a header,  `Name: value`;  `df -h /` takes none.
            if word.hasPrefix("-"), let next, isSecretFlag(word), word != "-H" || next.contains(":") {
                return true
            }
            if words.count > 1, looksLikeSecretValue(word),
               word.count >= 40 || word.contains(where: \.isNumber) {
                return true
            }
        }
        return false
    }

    /// Whether a word is a flag whose next argument is a secret.
    ///
    /// `--token` and `--api-key` say the next word is a credential,  and `-H` and
    /// `--header` say it is a header,  which may be one.  `-H` is compared as it is written,
    /// because `-h` is help or human-readable sizes (`df -h`,  `serve -h 0.0.0.0`) and never
    /// a header.
    ///
    /// `Bearer` and `Basic` are not flags:  `--auth-mode basic --port 8080` names a mode,  and
    /// the word after it is the next flag.  `NextElement.credentialAfter` hides the word after
    /// a scheme unless that word is a flag,  a phrase or a prose word.
    static func isSecretFlag(_ word: String) -> Bool {
        if word == "-H" { return true }
        let lowered = word.lowercased()
        if lowered == "--header" { return true }
        guard lowered.hasPrefix("-"), !lowered.contains("=") else { return false }
        let name = lowered.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        return !name.isEmpty && isSecretKey(name)
    }

    /// What one element of a list says about the element after it.
    enum NextElement: Equatable {
        /// Nothing:  the next element is judged on its own.
        case free
        /// A secret flag (`--token`,  `-H`):  the next element is hidden whatever it is.
        case secret
        /// A scheme word (`Bearer`,  `Basic`):  the next element is hidden unless it is a flag,
        /// a phrase or a prose word,  so `["--auth-mode", "basic", "--port", "8080"]` keeps
        /// `--port`.
        case credentialAfter(scheme: String)
    }

    /// What an element of a list says about the element after it.
    static func nextElement(after word: String) -> NextElement {
        if isSecretFlag(word) { return .secret }
        let lowered = word.lowercased()
        if lowered == "bearer" || lowered == "basic" { return .credentialAfter(scheme: lowered) }
        return .free
    }

    /// Whether an element is hidden because of the element before it.
    ///
    /// After a scheme word,  the element is hidden when it is one word,  not a flag,  and not
    /// one of the words prose puts after a scheme (`Bearer tokens`).  So a flag such as
    /// `--port` and a phrase stay shown,  and every other word stays hidden as it always was,
    /// short or lowercase ones included:  a list element right after `Bearer` is the token far
    /// more often than not,  and the flag and the phrase are still judged on their own shape.
    static func hides(_ element: String, after rule: NextElement) -> Bool {
        switch rule {
        case .free:
            return false
        case .secret:
            return true
        case .credentialAfter:
            let word = element.trimmingCharacters(in: .whitespaces)
            guard !word.isEmpty,
                  !word.hasPrefix("-"),
                  !word.contains(where: { $0.isWhitespace }) else { return false }
            return !schemeProse.contains(word.lowercased())
        }
    }

    /// Whether a string value is a secret,  by its shape or by a credential inside it.
    static func isSecretString(_ value: String) -> Bool {
        looksLikeSecretValue(value) || carriesCredential(value)
    }

    /// A text with its secret values hidden,  and how that was done.
    struct Redaction: Equatable {
        /// The text to show.
        let text: String
        /// How many values were hidden.
        let replaced: Int
        /// Whether the text was rewritten for reading rather than kept as the file had it.
        let reformatted: Bool
        /// Whether a cut-off JSON file was hidden line by line.
        let cutOffJSON: Bool
        /// Whether the line reader ran without its expressions and hid every value on each
        /// line that could hold one,  rather than checking them one by one.
        let failedClosed: Bool

        init(text: String, replaced: Int, reformatted: Bool, cutOffJSON: Bool, failedClosed: Bool = false) {
            self.text = text
            self.replaced = replaced
            self.reformatted = reformatted
            self.cutOffJSON = cutOffJSON
            self.failedClosed = failedClosed
        }
    }

    /// Replace every secret value in a JSON or TOML text with a mark.
    ///
    /// The replacement is by key name and by the shape of the value,  so a value is
    /// hidden when its key says it is a secret or when the value looks like one.  A key
    /// that holds a path,  a command or a server name is left alone,  because that is
    /// the information the file is for.
    static func redact(_ text: String) -> String {
        redaction(of: text).text
    }

    /// The text with its secret values hidden,  with what was hidden and how.
    ///
    /// A JSON document keeps its own bytes:  a file with nothing to hide is returned as
    /// it is,  and a secret string is replaced where it stands.  Only when that cannot be
    /// done (a secret key holds an object,  or the file escapes a value another way) is
    /// the document rewritten,  and the result says so.  Anything that is not JSON is
    /// redacted line by line,  which keeps every other line byte for byte.
    ///
    /// `expressions` are the line reader's patterns.  A test passes a set with one missing,
    /// to check that the reader then hides more,  never less.
    static func redaction(
        of text: String,
        truncated: Bool = false,
        expressions: LineExpressions = .standard
    ) -> Redaction {
        if let json = redactJSONDocument(text) { return json }
        var count = 0
        let out = redactLines(text, expressions: expressions, count: &count)
        return Redaction(
            text: out,
            replaced: count,
            reformatted: false,
            cutOffJSON: truncated && opensLikeJSON(text),
            failedClosed: !expressions.isComplete
        )
    }

    /// Whether a text opens the way a JSON document does:  with `{`,  or with `[` and then a
    /// value.
    ///
    /// A TOML table header opens with `[` too,  and holds a name:  `[mcp_servers]`,
    /// `[[servers]]`,  `[1password]` and `[2fa.keys]` are not JSON,  and `[{`,  `["a"`,  `[1,`
    /// and a bare `[` cut off are.  A digit or a `-` opens a number only when the run of number
    /// characters after it ends at a `,`,  a `]`,  a space or the end of the text.
    static func opensLikeJSON(_ text: String) -> Bool {
        var rest = text.drop(while: { $0.isWhitespace })
        guard let first = rest.first else { return false }
        if first == "{" { return true }
        guard first == "[" else { return false }
        while let next = rest.first, next == "[" || next.isWhitespace {
            rest = rest.dropFirst()
        }
        // Nothing after the brackets:  the cut came inside an array.
        guard let value = rest.first else { return true }
        if value == "\"" || value == "{" || value == "]" { return true }
        guard value == "-" || value.isNumber else { return false }
        let numberCharacters: Set<Character> = ["-", "+", ".", "e", "E", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
        let afterNumber = rest.drop(while: { numberCharacters.contains($0) })
        guard let end = afterNumber.first else { return true }
        return end == "," || end == "]" || end.isWhitespace
    }

    // MARK: JSON

    /// What one walk over a JSON value found.
    private struct JSONFindings {
        /// How many values were hidden.
        var count = 0
        /// The string values that were hidden.
        var strings: Set<String> = []
        /// Whether a secret key held something other than a string,  which cannot be
        /// replaced in place.
        var structural = false
    }

    /// A JSON document with its secret values hidden,  or nil when the text is not JSON.
    private static func redactJSONDocument(_ text: String) -> Redaction? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else { return nil }
        var findings = JSONFindings()
        let cleaned = redactJSON(object, findings: &findings)
        guard findings.count > 0 else {
            return Redaction(text: text, replaced: 0, reformatted: false, cutOffJSON: false)
        }
        // In place first,  and kept only when a second walk finds nothing left to hide.
        if !findings.structural,
           let inPlace = replacingLiterals(findings.strings, in: text),
           isClean(inPlace) {
            return Redaction(text: inPlace, replaced: findings.count, reformatted: false, cutOffJSON: false)
        }
        guard JSONSerialization.isValidJSONObject(cleaned),
              let rewritten = try? JSONSerialization.data(
                  withJSONObject: cleaned,
                  options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
              ),
              let out = String(data: rewritten, encoding: .utf8) else { return nil }
        return Redaction(text: out, replaced: findings.count, reformatted: true, cutOffJSON: false)
    }

    /// Whether a JSON text has nothing left to hide.
    private static func isClean(_ text: String) -> Bool {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else { return false }
        var findings = JSONFindings()
        _ = redactJSON(object, findings: &findings)
        return findings.count == 0
    }

    /// The text with every JSON string literal of the values replaced by the mark's,  or
    /// nil when a value is not written the way JSON writes it.
    ///
    /// One pass over the text finds each string literal from its opening quote to its
    /// closing one,  so the work grows with the file and not with the file times the number
    /// of hidden values.  A literal inside another string is never seen as a literal of its
    /// own,  because the pass is inside that string when it reaches it.
    ///
    /// Only a literal in a value's place is replaced:  one followed by a `:` is a key,  and
    /// a key keeps its name even when a hidden value is spelled the same,  so
    /// `{"token": "token"}` keeps its key.  When a hidden value is never found in a value's
    /// place,  as when the file escapes it another way,  the result is nil and the caller
    /// rewrites the document instead.
    private static func replacingLiterals(_ values: Set<String>, in text: String) -> String? {
        guard let mark = jsonLiteral(redactionMark) else { return nil }
        var wanted: Set<[UInt8]> = []
        var wantedLengths: Set<Int> = []
        for value in values {
            guard let literal = jsonLiteral(value) else { return nil }
            let encoded = Array(literal.utf8)
            wanted.insert(encoded)
            wantedLengths.insert(encoded.count)
        }
        let quote = UInt8(ascii: "\"")
        let backslash = UInt8(ascii: "\\")
        let colon = UInt8(ascii: ":")
        let bytes = Array(text.utf8)
        let markBytes = Array(mark.utf8)
        var out: [UInt8] = []
        out.reserveCapacity(bytes.count)
        var found: Set<[UInt8]> = []
        var copiedTo = 0
        var index = 0
        while index < bytes.count {
            guard bytes[index] == quote else {
                index += 1
                continue
            }
            let start = index
            index += 1
            while index < bytes.count, bytes[index] != quote {
                index += bytes[index] == backslash ? 2 : 1
            }
            // A string that never closes is not the JSON that was parsed.
            guard index < bytes.count else { return nil }
            index += 1
            let end = index
            guard wantedLengths.contains(end - start) else { continue }
            let literal = Array(bytes[start..<end])
            guard wanted.contains(literal) else { continue }
            var after = end
            while after < bytes.count, isJSONWhitespace(bytes[after]) { after += 1 }
            if after < bytes.count, bytes[after] == colon { continue }
            out.append(contentsOf: bytes[copiedTo..<start])
            out.append(contentsOf: markBytes)
            copiedTo = end
            found.insert(literal)
        }
        guard found.count == wanted.count else { return nil }
        out.append(contentsOf: bytes[copiedTo..<bytes.count])
        return String(decoding: out, as: UTF8.self)
    }

    /// Whether a byte is one of the four that JSON allows between tokens.
    private static func isJSONWhitespace(_ byte: UInt8) -> Bool {
        byte == 0x20 || byte == 0x09 || byte == 0x0A || byte == 0x0D
    }

    /// One string as a JSON literal,  quotes included.
    private static func jsonLiteral(_ value: String) -> String? {
        guard let data = try? JSONSerialization.data(withJSONObject: [value], options: [.withoutEscapingSlashes]),
              let wrapped = String(data: data, encoding: .utf8),
              wrapped.hasPrefix("["), wrapped.hasSuffix("]") else { return nil }
        return String(wrapped.dropFirst().dropLast())
    }

    /// One JSON value, with the secret values inside it replaced.
    ///
    /// A string anywhere is checked,  in a list as well as under a key,  and a string in
    /// a list that follows a secret flag (`--token`,  `-H`) is hidden too,  as is one that
    /// follows a scheme word (`Bearer`,  `Basic`) and is not a flag,  a phrase or a prose word.
    private static func redactJSON(_ value: Any, findings: inout JSONFindings) -> Any {
        if let map = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (key, child) in map {
                if isSecretKey(key) {
                    out[key] = hideSecretKeyValue(child, findings: &findings)
                } else {
                    out[key] = redactJSON(child, findings: &findings)
                }
            }
            return out
        }
        if let list = value as? [Any] {
            var out: [Any] = []
            var next = NextElement.free
            for element in list {
                guard let text = element as? String else {
                    next = .free
                    out.append(redactJSON(element, findings: &findings))
                    continue
                }
                if text.isEmpty || text == redactionMark {
                    next = .free
                    out.append(text)
                } else if hides(text, after: next) || isSecretString(text) {
                    next = .free
                    findings.count += 1
                    findings.strings.insert(text)
                    out.append(redactionMark)
                } else {
                    next = nextElement(after: text)
                    out.append(text)
                }
            }
            return out
        }
        if let text = value as? String, text != redactionMark, isSecretString(text) {
            findings.count += 1
            findings.strings.insert(text)
            return redactionMark
        }
        return value
    }

    /// The value under a secret key,  hidden unless it is empty.
    private static func hideSecretKeyValue(_ child: Any, findings: inout JSONFindings) -> Any {
        if child is NSNull { return child }
        if let text = child as? String {
            if text.isEmpty || text == redactionMark { return text }
            findings.count += 1
            findings.strings.insert(text)
            return redactionMark
        }
        findings.count += 1
        findings.structural = true
        return redactionMark
    }

    // MARK: TOML, INI and other text

    private static let quoteCharacters = CharacterSet(charactersIn: "\"'")

    /// The patterns the line reader checks a line's pieces with.
    ///
    /// Each is compiled once.  A pattern that failed to compile is nil,  and then the reader
    /// fails closed:  it hides every value on each line it would have checked,  rather than
    /// checking none of them.  A test builds a set with one missing to hold it to that.
    struct LineExpressions {
        /// `name = value` and `name: value` pairs inside a line,  as an inline table holds them.
        let pair: NSRegularExpression?
        /// Every quoted string in a line.
        let quoted: NSRegularExpression?

        /// Whether both patterns compiled,  so a line's pieces can be checked one by one.
        var isComplete: Bool { pair != nil && quoted != nil }

        /// The reader's own patterns.
        ///
        /// In the pair pattern,  the look-behind and the possessive runs keep the scan linear on
        /// a long line.  A value that opens a table or a list matches only its opening
        /// character,  so the scan goes on into it:  in `env = {API_KEY="abc"}` the inner pair
        /// is matched as a pair,  and a bare value never runs on through the braces after it.
        static let standard = LineExpressions(
            pair: try? NSRegularExpression(
                pattern: #"(?<![A-Za-z0-9_.\-"'])([A-Za-z0-9_.\-]++|"[^"\n]*+"|'[^'\n]*+')([ \t]*[=:][ \t]*)("(?:[^"\\]|\\.)*+"|'[^'\n]*+'|[\{\[]|[^\s,\{\}\[\]]++)"#
            ),
            quoted: try? NSRegularExpression(
                pattern: #""(?:[^"\\\n]|\\.)*+"|'[^'\n]*+'"#
            )
        )
    }

    /// Hide the secret values of a text that is not JSON,  one line at a time.
    private static func redactLines(_ text: String, expressions: LineExpressions, count: inout Int) -> String {
        var next = NextElement.free
        var lines: [String] = []
        for line in text.components(separatedBy: "\n") {
            guard let pair = expressions.pair, let quoted = expressions.quoted else {
                lines.append(hidingEveryValue(in: line, count: &count))
                continue
            }
            lines.append(redactLine(line, pair: pair, quoted: quoted, next: &next, count: &count))
        }
        return lines.joined(separator: "\n")
    }

    /// One line with every value it could hold hidden,  for a reader that cannot check the
    /// line's pieces one by one.
    ///
    /// A line the reader would check (one with a `=`,  a `:` or a quote) keeps its key and
    /// separator and loses the rest,  and any other line,  a table header among them,  holds no
    /// value and is kept.
    ///
    /// The text before the separator is kept only when it is a key:  a bare name
    /// (`[A-Za-z0-9_.-]+`) or one closed quoted name,  with only spaces between it and the
    /// separator.  Anything else loses all but its indent,  because a `=` or `:` inside a list
    /// element (`"c2VjcmV0LXZhbHVl==",`,  `"sk-abc:def",`) is part of the value,  not a
    /// separator,  and keeping the text before it would show the value.
    private static func hidingEveryValue(in line: String, count: inout Int) -> String {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty,
              trimmed != redactionMark,
              line.contains(where: { $0 == "=" || $0 == ":" || $0 == "\"" || $0 == "'" }) else { return line }
        count += 1
        let indent = line.prefix(while: { $0 == " " || $0 == "\t" })
        guard let valueStart = keyedValueStart(in: line, after: indent.endIndex) else {
            return String(indent) + redactionMark
        }
        let padding = line[valueStart...].prefix(while: { $0 == " " || $0 == "\t" })
        return String(line[..<valueStart]) + String(padding) + redactionMark
    }

    /// Where the value starts on a line that opens with a key and a separator,  just after the
    /// `=` or `:`,  or nil when the line does not open that way.
    private static func keyedValueStart(in line: String, after start: String.Index) -> String.Index? {
        guard start < line.endIndex else { return nil }
        var index = start
        let first = line[index]
        if first == "\"" || first == "'" {
            // One quoted name,  closed before any separator.
            index = line.index(after: index)
            guard let close = line[index...].firstIndex(of: first) else { return nil }
            index = line.index(after: close)
        } else {
            let bare = line[index...].prefix(while: { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "_" || $0 == "." || $0 == "-") })
            guard !bare.isEmpty else { return nil }
            index = bare.endIndex
        }
        while index < line.endIndex, line[index] == " " || line[index] == "\t" {
            index = line.index(after: index)
        }
        guard index < line.endIndex, line[index] == "=" || line[index] == ":" else { return nil }
        return line.index(after: index)
    }

    /// One line of a TOML or INI document, with a secret value replaced.
    ///
    /// `next` carries a secret flag or a scheme word at the end of one line of a list to the
    /// value on the next line,  as a list written one element per line has it.
    private static func redactLine(
        _ line: String,
        pair: NSRegularExpression,
        quoted: NSRegularExpression,
        next: inout NextElement,
        count: inout Int
    ) -> String {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        // A table header or the end of a list:  a flag before it has no value left to hide.
        if trimmed.hasPrefix("]") || trimmed.hasPrefix("}") || (trimmed.hasPrefix("[") && !trimmed.contains("=")) {
            next = .free
        }
        if let separator = line.firstIndex(where: { $0 == "=" || $0 == ":" }) {
            let key = String(line[line.startIndex..<separator]).trimmingCharacters(in: .whitespaces)
            // A TOML key can be quoted.
            let bareKey = key.trimmingCharacters(in: quoteCharacters)
            let valueStart = line.index(after: separator)
            let padding = line[valueStart...].prefix(while: { $0 == " " || $0 == "\t" })
            let rest = String(line[valueStart...].dropFirst(padding.count))
            // A list or an inline table is checked piece by piece below,  unless its key
            // is a secret,  when the whole of it goes.
            let opensCollection = rest.hasPrefix("{") || rest.hasPrefix("[")
            let value = rest.trimmingCharacters(in: quoteCharacters)
            let nothingToHide = rest.isEmpty || rest == "[]" || rest == "{}" || value == redactionMark
            if !nothingToHide, isSecretKey(bareKey) || (!opensCollection && isSecretString(value)) {
                count += 1
                next = .free
                let prefix = String(line[..<valueStart]) + String(padding)
                return prefix + marked(like: rest)
            }
        }
        return redactPieces(in: line, pair: pair, quoted: quoted, next: &next, count: &count)
    }

    /// The pairs of an inline table and the quoted strings of a list,  each checked.
    ///
    /// `http_headers = { Authorization = "Bearer x" }` and
    /// `args = ["--header", "Authorization: Bearer x"]` both carry a credential that the
    /// line's own key does not name.
    private static func redactPieces(
        in line: String,
        pair: NSRegularExpression,
        quoted: NSRegularExpression,
        next: inout NextElement,
        count: inout Int
    ) -> String {
        // A secret key that opens a table or a list inside the line,  as
        // `a = { credentials = {user="x"} }` has it:  everything from its value to the end
        // of the line goes,  because the pieces inside belong to the secret.
        if let cut = secretCollectionStart(in: line, pair: pair) {
            let head = redactPieces(
                in: (line as NSString).substring(to: cut),
                pair: pair,
                quoted: quoted,
                next: &next,
                count: &count
            )
            count += 1
            next = .free
            return head + redactionMark
        }
        var text = line
        if text.contains("=") || text.contains(":") {
            text = replacingMatches(in: text, expression: pair) { groups in
                guard groups.count > 3 else { return nil }
                let key = groups[1].trimmingCharacters(in: quoteCharacters)
                let value = groups[3].trimmingCharacters(in: quoteCharacters)
                guard !value.isEmpty, value != redactionMark else { return nil }
                let opensCollection = value.hasPrefix("{") || value.hasPrefix("[")
                guard isSecretKey(key) || (!opensCollection && isSecretString(value)) else { return nil }
                count += 1
                return groups[1] + groups[2] + marked(like: groups[3])
            }
        }
        if text.contains("\"") || text.contains("'") {
            text = replacingMatches(in: text, expression: quoted) { groups in
                let whole = groups[0]
                guard whole.count >= 2 else { return nil }
                let content = String(whole.dropFirst().dropLast())
                if content.isEmpty || content == redactionMark {
                    next = .free
                    return nil
                }
                if hides(content, after: next) || isSecretString(content) {
                    next = .free
                    count += 1
                    return marked(like: whole)
                }
                next = nextElement(after: content)
                return nil
            }
        }
        return text
    }

    /// Where the value of the first secret-keyed pair that opens a table or a list starts,
    /// in UTF-16 units,  or nil when the line has none.
    private static func secretCollectionStart(in line: String, pair expression: NSRegularExpression) -> Int? {
        guard line.contains("{") || line.contains("[") else { return nil }
        let source = line as NSString
        for match in expression.matches(in: line, range: NSRange(location: 0, length: source.length)) {
            guard match.numberOfRanges > 3 else { continue }
            let keyRange = match.range(at: 1)
            let valueRange = match.range(at: 3)
            guard keyRange.location != NSNotFound, valueRange.location != NSNotFound else { continue }
            let value = source.substring(with: valueRange)
            guard value == "{" || value == "[" else { continue }
            let key = source.substring(with: keyRange).trimmingCharacters(in: quoteCharacters)
            if isSecretKey(key) { return valueRange.location }
        }
        return nil
    }

    /// The mark,  in the quotes the replaced value was written in.
    private static func marked(like original: String) -> String {
        guard let first = original.first, first == "\"" || first == "'" else { return redactionMark }
        return "\(first)\(redactionMark)\(first)"
    }

    /// Replace each match of an expression with what `transform` makes of its groups.
    ///
    /// The matches are judged in order,  so a flag is seen before the value after it,  and
    /// the replacements are made from the end,  so each range still points at its match.
    private static func replacingMatches(
        in text: String,
        expression: NSRegularExpression,
        transform: ([String]) -> String?
    ) -> String {
        let source = text as NSString
        let matches = expression.matches(in: text, range: NSRange(location: 0, length: source.length))
        guard !matches.isEmpty else { return text }
        var edits: [(range: NSRange, replacement: String)] = []
        for match in matches {
            var groups: [String] = []
            for index in 0..<match.numberOfRanges {
                let range = match.range(at: index)
                groups.append(range.location == NSNotFound ? "" : source.substring(with: range))
            }
            if let replacement = transform(groups) {
                edits.append((range: match.range, replacement: replacement))
            }
        }
        guard !edits.isEmpty else { return text }
        let output = NSMutableString(string: text)
        for edit in edits.reversed() {
            output.replaceCharacters(in: edit.range, with: edit.replacement)
        }
        return output as String
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
