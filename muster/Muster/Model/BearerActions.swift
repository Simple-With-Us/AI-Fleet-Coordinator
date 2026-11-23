import Foundation

/// Mint, rotate and revoke for one bearer identity, over the Worker's owner action API.
///
/// `POST https://agents.simplewithus.com/admin/api/bearers/action` runs behind Cloudflare Access and
/// answers only the owner's own Access JWT, which `cloudflared access token` prints after the owner's
/// one-time `cloudflared access login`.  This type builds the request and reads the answer;  it never
/// logs, stores or returns a bearer anywhere but to the one caller that asked, who shows it once.
enum BearerAction: String, CaseIterable {
    case mint, rotate, revoke

    var verb: String {
        switch self {
        case .mint: return "Mint"
        case .rotate: return "Rotate"
        case .revoke: return "Revoke"
        }
    }
}

struct BearerActionRequest: Equatable {
    let action: BearerAction
    let identity: String
    /// The caller's single-use form id for a mint or rotate:  12 hex of milliseconds then 20 random hex.
    let mintId: String?
    /// Required by the API for a seat mint;  a rotate keeps the live bearer's own value when this is nil.
    let mcpAccept: Bool?

    static func make(_ action: BearerAction, identity: String, mcpAccept: Bool?, now: Date = Date()) -> BearerActionRequest {
        BearerActionRequest(action: action, identity: identity,
                            mintId: action == .revoke ? nil : newMintId(now: now), mcpAccept: mcpAccept)
    }

    /// A mint needs a value (the Worker refuses a seat mint without one);  a rotate passes the live bearer's own
    /// value through (nil when unknown, which the Worker keeps);  a revoke sends none.
    static func mcpAccept(for action: BearerAction, live: Bool?) -> Bool? {
        switch action {
        case .mint: return live ?? true
        case .rotate: return live
        case .revoke: return nil
        }
    }

    static func newMintId(now: Date = Date(), random: () -> UInt8 = { UInt8.random(in: 0...255) }) -> String {
        let millis = UInt64(max(0, now.timeIntervalSince1970 * 1000))
        let stamp = String(format: "%012llx", millis & 0xFFFF_FFFF_FFFF)
        let tail = (0..<10).map { _ in String(format: "%02x", random()) }.joined()
        return stamp + tail
    }

    func body() throws -> Data {
        var object: [String: Any] = ["action": action.rawValue, "identity": identity]
        if let mintId { object["mint_id"] = mintId }
        if let mcpAccept { object["mcp_accept"] = mcpAccept }
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }
}

/// What the Worker answered.  `bearer` exists only in `.minted`, and nothing here prints it.
enum BearerActionOutcome: Equatable {
    case minted(identity: String, bearer: String, expiresAt: String?)
    case revoked(identity: String)
    /// A retry of a mint that had already finished:  the Worker holds only the hash, so no bearer comes back.
    case alreadyCompleted(identity: String)
    case refused(String)

    var summary: String {
        switch self {
        case .minted(let identity, _, let expiresAt):
            return "A new bearer was minted for \(identity)" + (expiresAt.map { ", expires \($0)." } ?? ".")
        case .revoked(let identity): return "\(identity)'s bearer was revoked."
        case .alreadyCompleted(let identity):
            return "That mint for \(identity) had already finished.  The Worker keeps only a hash, so the bearer cannot be shown again."
        case .refused(let why): return why
        }
    }
}

extension BearerActionOutcome {
    static func read(status: Int, data: Data, identity: String) -> BearerActionOutcome {
        if status == 302 || status == 401 {
            return .refused("The Worker wants the owner's Cloudflare Access login.  Run  cloudflared access login https://agents.simplewithus.com/admin  on this Mac once.")
        }
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .refused("The Worker's answer was not understood (HTTP \(status)).")
        }
        if (object["ok"] as? Bool) == true {
            if (object["completed"] as? Bool) == true { return .alreadyCompleted(identity: identity) }
            if let bearer = object["token"] as? String, !bearer.isEmpty {
                return .minted(identity: identity, bearer: bearer, expiresAt: object["expiresAt"] as? String)
            }
            if (object["action"] as? String) == "revoke" { return .revoked(identity: identity) }
            return .refused("The Worker said ok but returned no bearer.")
        }
        let error = (object["error"] as? String) ?? "unknown"
        var text = "Refused by the Worker:  " + error.replacingOccurrences(of: "_", with: " ")
        if let reason = object["reason"] as? String { text += " (\(reason.replacingOccurrences(of: "_", with: " ")))" }
        if let hint = object["hint"] as? String { text += ".  " + hint }
        if let missing = object["missingRecords"] as? [String], !missing.isEmpty {
            text += ".  Missing records: " + missing.joined(separator: ", ")
        }
        return .refused(text)
    }
}

/// Where the call goes and how the owner's token is obtained, so tests can stand in for both.
protocol BearerActionTransport {
    func accessToken() async -> String?
    func post(body: Data, accessToken: String) async -> (status: Int, data: Data)?
}

struct BearerActionRunner {
    var transport: BearerActionTransport

    func run(_ request: BearerActionRequest) async -> BearerActionOutcome {
        guard let body = try? request.body() else { return .refused("The request could not be built.") }
        guard let token = await transport.accessToken() else {
            return .refused("No Cloudflare Access login on this Mac.  Run  cloudflared access login https://agents.simplewithus.com/admin  once.")
        }
        guard let answer = await transport.post(body: body, accessToken: token) else {
            return .refused("The Worker could not be reached.  If a mint was sent, check Bearers again before trying a second time.")
        }
        return BearerActionOutcome.read(status: answer.status, data: answer.data, identity: request.identity)
    }
}

/// The Cloudflare Access service token "muster-admin-api", read from a user-only file.  The Worker accepts it on
/// /admin/api/* only.  Values are held in memory for one call and never logged.
struct AccessServiceToken: Equatable {
    let clientId: String
    let clientSecret: String

    static var defaultPath: String { NSHomeDirectory() + "/.config/fleet-tools/muster-admin-api.json" }

    /// Nil unless the file is a regular file owned by this user with no group or other permission bits and holds
    /// both values:  a loose file is ignored, never trusted.
    static func load(path: String? = nil) -> AccessServiceToken? {
        let file = path ?? defaultPath
        // Open without following a symlink, then check the open descriptor, so the file that is validated
        // is the file that is read.
        let fd = open(file, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        var info = stat()
        guard fstat(fd, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG,
              info.st_mode & 0o077 == 0,
              info.st_uid == getuid(),
              let data = try? handle.readToEnd(),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let id = object["clientId"] as? String, Self.headerSafe(id),
              let secret = object["clientSecret"] as? String, Self.headerSafe(secret) else { return nil }
        return AccessServiceToken(clientId: id, clientSecret: secret)
    }

    /// Printable ASCII with no spaces, non-empty: a value from the file can never split or inject an HTTP header.
    static func headerSafe(_ value: String) -> Bool {
        !value.isEmpty && value.unicodeScalars.allSatisfy { $0.value >= 0x21 && $0.value <= 0x7E }
    }

    /// The transport carries credentials as one string:  "svc:<id>:<secret>" for this token, a JWT otherwise.
    var encoded: String { "svc:" + clientId + ":" + clientSecret }

    static func decode(_ text: String) -> AccessServiceToken? {
        guard text.hasPrefix("svc:") else { return nil }
        let parts = text.dropFirst(4).split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }
        return AccessServiceToken(clientId: String(parts[0]), clientSecret: String(parts[1]))
    }
}

/// The real route:  the service token file when present, else `cloudflared access token` for the owner's JWT, then one HTTPS POST.
struct LiveBearerActionTransport: BearerActionTransport {
    static let app = "https://agents.simplewithus.com/admin"
    static let endpoint = URL(string: "https://agents.simplewithus.com/admin/api/bearers/action")!
    static let cloudflared = ["/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"]

    func accessToken() async -> String? {
        if let service = AccessServiceToken.load() { return service.encoded }
        return await Task.detached { () -> String? in
            for path in Self.cloudflared where FileManager.default.isExecutableFile(atPath: path) {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: path)
                process.arguments = ["access", "token", "-app=" + Self.app]
                let out = Pipe()
                process.standardOutput = out
                process.standardError = Pipe()
                do { try process.run() } catch { continue }
                // A hung cloudflared must not hold the panel:  stop it after twenty seconds.
                let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + 20, execute: watchdog)
                let data = out.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                watchdog.cancel()
                let token = (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if process.terminationStatus == 0, token.split(separator: ".").count == 3 { return token }
            }
            return nil
        }.value
    }

    func post(body: Data, accessToken: String) async -> (status: Int, data: Data)? {
        var request = URLRequest(url: Self.endpoint, timeoutInterval: 30)
        request.httpMethod = "POST"
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("1", forHTTPHeaderField: "X-Admin-Api")
        if let service = AccessServiceToken.decode(accessToken) {
            request.setValue(service.clientId, forHTTPHeaderField: "CF-Access-Client-Id")
            request.setValue(service.clientSecret, forHTTPHeaderField: "CF-Access-Client-Secret")
        } else {
            request.setValue(accessToken, forHTTPHeaderField: "cf-access-token")
        }
        request.setValue("Muster", forHTTPHeaderField: "User-Agent")
        let session = URLSession(configuration: .ephemeral, delegate: NoRedirect(), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        guard let (data, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse else { return nil }
        return (http.statusCode, data)
    }

    /// An Access redirect to a login page is an answer, not a page to follow.
    private final class NoRedirect: NSObject, URLSessionTaskDelegate {
        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
            completionHandler(nil)
        }
    }
}
