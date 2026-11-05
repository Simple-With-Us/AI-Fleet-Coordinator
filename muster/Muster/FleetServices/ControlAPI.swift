import Foundation

public struct LocalVM: Decodable, Equatable, Sendable {
    public let key: String
    public let name: String
    public let running: Bool
    public let state: String?

    public init(key: String, name: String, running: Bool, state: String? = nil) {
        self.key = key
        self.name = name
        self.running = running
        self.state = state
    }
}

/// The server's answer to `/api/system/control`.
///
/// Muster reads only the active-bot count and the blocking work.  The Local
/// VM list and the persist-on-exit flag are still decoded, because they are
/// part of the answer's shape and an answer without them is not the
/// BotFleet Server's;  nothing in Muster acts on them.
public struct ControlStatus: Decodable, Equatable, Sendable {
    public let activeBotCount: Int
    public let localVms: [LocalVM]
    public let persistOnExit: Bool
    /// The work in flight that would block a stop, per kind.
    ///
    /// The server counts ~18 kinds of work before it will stop, but this app
    /// only knew the bot-turn count, so its confirmation could say "0 bot
    /// turn(s) are active" and then the server would refuse the stop for
    /// something else entirely.  The server sends the same breakdown it uses
    /// for that refusal, so the dialog and the refusal agree.
    public let blockingWork: [BlockingWork]?

    public init(
        activeBotCount: Int,
        localVms: [LocalVM],
        persistOnExit: Bool = true,
        blockingWork: [BlockingWork]? = nil
    ) {
        self.activeBotCount = activeBotCount
        self.localVms = localVms
        self.persistOnExit = persistOnExit
        self.blockingWork = blockingWork
    }

    /// The blocking work as a sentence, or nil when nothing is in flight.
    public var blockingWorkSummary: String? {
        let named = (blockingWork ?? []).filter { $0.count > 0 }
        guard !named.isEmpty else { return nil }
        return named.map { "\($0.count) \($0.label)\($0.count == 1 ? "" : "s")" }.joined(separator: ", ")
    }
}

/// One kind of work that is in flight and would block a stop.
public struct BlockingWork: Decodable, Equatable, Sendable {
    public let count: Int
    public let label: String

    public init(count: Int, label: String) {
        self.count = count
        self.label = label
    }
}

public enum ControlAPIError: LocalizedError, Equatable {
    case unavailable
    /// The server answered the health check but did not answer the status
    /// route in time.  It is running; it was just too slow.
    case unresponsive
    case foreignService
    case invalidResponse
    case refused(String)
    /// A change was sent and no answer came back, so the server may or may
    /// not have applied it.  It is not the same as `unavailable`:  retrying
    /// blindly could apply the change twice, so the state is read first.
    case outcomeUnknown

    public var errorDescription: String? {
        switch self {
        case .unavailable: return "BotFleet Server is unavailable."
        case .unresponsive: return "BotFleet Server is running but did not answer in time."
        case .foreignService: return "Port is not answering as BotFleet Server."
        case .invalidResponse: return "BotFleet Server returned an invalid response.  Update BotFleet and try again."
        case .refused(let message): return message
        case .outcomeUnknown:
            return "BotFleet Server did not answer, so the change may or may not have been made.\u{00A0} Refresh before trying again."
        }
    }
}

/// The BotFleet Server on this Mac, asked over plain http on loopback.
///
/// This client talks to that one server and no other:  a base URL that is
/// not loopback http is refused before anything is sent, and no request
/// carries a credential, so Muster holds none.  The remote-host, token and
/// VM power paths it was ported with had no caller and were removed.
public final class ControlAPI {
    /// Where the BotFleet Server listens on this Mac.
    public static let defaultBaseURL = URL(string: "http://127.0.0.1:8799")!

    private let session: URLSession
    private let baseURL: URL
    private let decoder = JSONDecoder()

    public init(session: URLSession = .shared, baseURL: URL = ControlAPI.defaultBaseURL) {
        self.session = session
        self.baseURL = baseURL
    }

    /// True for the loopback names the BotFleet Server answers on.
    static func isLoopback(_ host: String?) -> Bool {
        host == "127.0.0.1" || host == "localhost"
    }

    /// How long the status probe waits for the server's own answer.
    ///
    /// The route reads the state of every Local VM target, so on a busy Mac
    /// it takes a while.  Fifteen seconds was close enough to what the route
    /// actually cost under load that a slow server read as a stopped one.
    public static let statusTimeout: TimeInterval = 30

    public func status(timeout: TimeInterval = ControlAPI.statusTimeout) async throws -> ControlStatus {
        try await verifyIdentity()
        let data: Data
        do {
            data = try await send("/api/system/control", method: "GET", timeout: timeout)
        } catch ControlAPIError.unavailable {
            // The identity check already proved the server is alive and ready,
            // so a failure here means it was too slow to answer, not that it
            // is gone.  Reporting that as unavailable would offer to start a
            // server that is already running.
            throw ControlAPIError.unresponsive
        }
        guard let status = try? decoder.decode(ControlStatus.self, from: data), status.activeBotCount >= 0 else {
            throw ControlAPIError.invalidResponse
        }
        return status
    }

    /// The process behind the server's port,  or nil when nothing there said who it is.
    ///
    /// A listener proves something holds the port.  This says which process does,  so a
    /// restart can tell a new server from the one that was already there.
    public func serverPID() async -> Int? {
        guard let data = try? await send("/api/health", method: "GET"),
              let health = try? decoder.decode(Health.self, from: data),
              health.app == "botfleet",
              let pid = health.pid, pid > 0 else { return nil }
        return pid
    }

    public func stopServer() async throws { try await post("/api/system/control/stop") }

    private func post(_ path: String) async throws {
        try await verifyIdentity()
        let data = try await send(path, method: "POST", body: Data("{}".utf8), timeout: 300)
        guard let result = try? decoder.decode(ActionResult.self, from: data), result.ok else {
            throw ControlAPIError.invalidResponse
        }
    }

    private func verifyIdentity() async throws {
        let data = try await send("/api/health", method: "GET")
        guard let value = try? decoder.decode(Health.self, from: data),
              value.app == "botfleet",
              (value.pid ?? 0) > 0,
              value.ready == true else {
            throw ControlAPIError.foreignService
        }
    }

    private func send(_ path: String, method: String, body: Data? = nil, timeout: TimeInterval = 3) async throws -> Data {
        // Only the BotFleet Server on this Mac is ever asked, so anything but
        // loopback http is refused here, before a request exists.
        guard baseURL.scheme == "http", Self.isLoopback(baseURL.host),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw ControlAPIError.invalidResponse
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request, delegate: NoRedirects()) }
        catch {
            // A cancelled caller is not a server that failed:  the cancellation
            // is passed on, so a refresh that was called off is not read as
            // the server being down.
            if isCancellation(error) { throw CancellationError() }
            // A read that failed changed nothing.  A change that failed may
            // have reached the server before the connection dropped or timed
            // out, and URLSession cannot say whether it did, so its outcome
            // is unknown rather than unavailable.
            throw method == "GET" ? ControlAPIError.unavailable : ControlAPIError.outcomeUnknown
        }
        guard let http = response as? HTTPURLResponse else { throw ControlAPIError.invalidResponse }
        guard http.url?.scheme == "http",
              Self.isLoopback(http.url?.host),
              http.url?.port == baseURL.port else { throw ControlAPIError.foreignService }
        if !(200..<300).contains(http.statusCode) {
            if path == "/api/health" { throw ControlAPIError.foreignService }
            let message = (try? decoder.decode(ErrorResponse.self, from: data))?.message
            if http.statusCode == 409 {
                throw ControlAPIError.refused(message ?? "BotFleet is busy with active bots or leases.  Try again after they finish.")
            }
            throw ControlAPIError.refused(message ?? "BotFleet Server refused the request (HTTP \(http.statusCode)).")
        }
        return data
    }
}

private struct Health: Decodable {
    let app: String
    let pid: Int?
    let ready: Bool?
}

private struct ActionResult: Decodable { let ok: Bool }

/// Refuses every redirect, so a request never leaves the server it was sent
/// to.  The 3xx answer itself comes back to the caller, which treats any
/// status outside 2xx as a refusal.
final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

private struct ErrorResponse: Decodable {
    let message: String?
    let error: String?
    private enum CodingKeys: String, CodingKey { case message, error }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        message = try container.decodeIfPresent(String.self, forKey: .message)
            ?? container.decodeIfPresent(String.self, forKey: .error)
        error = nil
    }
}
