import Foundation

/// A revision-fenced edit through the existing owner-authorized server command.
struct ServerSettingsSaveRequest: Equatable {
    var set: [String: ServerSettingValue]
    var expectBaseSHA256: String
    /// A full canonical response must not silently remove fields the owner was editing.
    var expectedKeys: Set<String> = []

    static func validSet(_ set: [String: ServerSettingValue]) -> Bool {
        guard !set.isEmpty, set.count <= ServerListenerSettingsRequest.maximumChanges else { return false }
        for (key, value) in set {
            let parts = key.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
            let table: String
            let name: String
            if parts.count == 3, parts[0] == "seat" {
                guard parts[1].range(of: "^[A-Z][A-Z0-9-]{0,39}$", options: .regularExpression) != nil else { return false }
                table = "seat"
                name = parts[2]
            } else if parts.count == 2 {
                table = parts[0]
                name = parts[1]
                guard ["daemon", "linear_comments", "linear_projects", "notes_queue", "listener_api"].contains(table) else { return false }
            } else { return false }
            guard name.range(of: "^[a-z][a-z0-9_]{0,47}$", options: .regularExpression) != nil,
                  ServerSettingsAllowlist.mayOffer(table: table, name: name), safe(value) else { return false }
        }
        return true
    }

    private static func safe(_ value: ServerSettingValue, depth: Int = 0) -> Bool {
        guard depth <= 4 else { return false }
        switch value {
        case .bool, .int: return true
        case .number(let number): return number.isFinite
        case .text(let text): return !ServerSettingsBuilder.looksLikeCredential(text)
        case .list(let values): return values.count <= 32 && values.allSatisfy { safe($0, depth: depth + 1) }
        case .table(let values):
            return values.count <= 32 && values.allSatisfy { key, value in
                let reference = key == "url_env" || key == "key_env"
                return (reference || !ServerSettingsAllowlist.readsAsSecret(key)) && safe(value, depth: depth + 1)
            }
        case .null: return false
        }
    }

    var standardInput: Data? {
        guard Self.validSet(set), ServerSettingsAnswer.isHash(expectBaseSHA256),
              let json = ServerSettingValue.table([
                "action": .text("save"), "set": .table(set), "expectBaseSHA256": .text(expectBaseSHA256),
              ]).json else { return nil }
        let data = Data(json.utf8)
        return data.count <= ServerListenerSettingsRequest.maximumBytes ? data : nil
    }
}

enum ServerSettingsSaveState: Equatable {
    case saving
    case saved
    case failed(String)

    var title: String {
        switch self {
        case .saving: return "Saving"
        case .saved: return "Saved"
        case .failed: return "Failed"
        }
    }
}

enum ServerSettingsSaving: Equatable {
    case saved(ServerSettingsAnswer)
    case failed(String, ServerSettingsAnswer?)
}

/// This is the same forced-command route and OS owner check used by existing actions.
/// An uncertain, rejected or partially applied response retains the draft and is never replayed.
struct ServerListenerSettingsSaver {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }
    var authorize: @MainActor () async -> OpsApprovalAnswer = {
        await OwnerCheck.ask(reason: "Update Operator Settings")
    }

    func save(_ request: ServerSettingsSaveRequest) async -> ServerSettingsSaving {
        guard request.standardInput != nil else {
            return .failed("The edit is not a valid nonsecret settings request, so it was not sent.", nil)
        }
        switch await authorize() {
        case .approved: break
        case .notApproved(let why), .unavailable(let why): return .failed(why, nil)
        }
        do {
            let invocation = try makeBuilder().listenerSettingsSaveInvocation(request)
            return Self.receipt(try await runner.run(invocation), request: request)
        } catch {
            return .failed("The server did not confirm the save.\(ServerStatusCopy.sentenceGap)The edit is retained; it has not been retried.", nil)
        }
    }

    static func receipt(_ process: OpsProcessResult, request: ServerSettingsSaveRequest) -> ServerSettingsSaving {
        let uncertain = "The server did not confirm the save.\(ServerStatusCopy.sentenceGap)The edit is retained; it has not been retried."
        guard !process.timedOut, let answer = ServerSettingsAnswer(stdout: process.stdout) else {
            return .failed(uncertain, nil)
        }
        let candidates = [process.stdout.trimmingCharacters(in: .whitespacesAndNewlines)]
            + process.stdout.split(whereSeparator: \.isNewline).reversed().map(String.init)
        let outer = candidates.compactMap { ServerSettingJSON.parse($0)?.tableEntries }
            .first { $0["eavesdropperSettings"]?.tableEntries != nil }
        let document = outer?["eavesdropperSettings"]?.tableEntries
        let canonical = document?["schemaVersion"] == .int(1) && document?["tool"] == .text("eavesdropper-settings")
            && answer.isWellFormed && answer.source == "infisical"
            && answer.revision != nil && answer.persisted == true && answer.readback == true
            && !answer.settings.isEmpty && request.expectedKeys.isSubset(of: Set(answer.settings.keys))
        let readback = canonical ? answer : nil
        guard process.exitCode == 0, outer?["ok"] == .bool(true), outer?["result"] == .text("saved"),
              answer.action == "save", answer.ok, answer.result == "saved", canonical,
              answer.runtimeApplied == true,
              answer.baseSHA256 == nil || answer.baseSHA256 == answer.revision,
              request.set.allSatisfy({ answer.settings[$0.key] == $0.value }) else {
            let detail = answer.detail ?? answer.errorCode ?? uncertain
            return .failed(OpsResultParser.sanitizedForDisplay(detail), readback)
        }
        return .saved(answer)
    }
}

/// Known channel spellings are presentation data; custom names and topic case stay intact.
enum ServerPresenceTopics {
    static let knownChannels = ["work-in-progress": "Work-In-Progress", "builds": "Builds", "alerts": "Alerts"]

    static func canonical(_ value: ServerSettingValue?, names: [String] = []) -> ServerSettingValue? {
        guard case .list(let pairs)? = value else { return value }
        var channels = knownChannels
        for name in names { channels[name.lowercased()] = name }
        return .list(pairs.map { pair in
            guard case .list(let fields) = pair, fields.count == 2, case .text(let channel) = fields[0] else { return pair }
            return .list([.text(channels[channel.lowercased()] ?? channel), fields[1]])
        })
    }
}
