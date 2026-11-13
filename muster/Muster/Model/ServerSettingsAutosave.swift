import Foundation

struct ServerSettingsAttempt: Equatable {
    var requestID: String
    var requestCreatedAt: Int

    static func fresh() -> ServerSettingsAttempt {
        ServerSettingsAttempt(requestID: UUID().uuidString.lowercased(),
                              requestCreatedAt: Int(Date().timeIntervalSince1970 * 1000))
    }

    static func validID(_ text: String) -> Bool {
        guard let value = UUID(uuidString: text) else { return false }
        return value.uuidString.lowercased() == text
    }

    var isValid: Bool { Self.validID(requestID) && requestCreatedAt > 0 }

    var reconcileInput: Data? {
        guard isValid else { return nil }
        return ServerSettingValue.table([
            "action": .text("reconcile"), "requestID": .text(requestID),
            "requestCreatedAt": .int(requestCreatedAt),
        ]).json.map { Data($0.utf8) }
    }
}

/// Only a routine's typed reference fields accept an environment-variable name.
enum ServerSettingsReferences {
    static func names(path: String, existing: String? = nil) -> [String] {
        let parts = path.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 4, parts[0] == "seat", parts[2] == "routine",
              parts[1].range(of: "^[A-Z][A-Z0-9-]{0,39}$", options: .regularExpression) != nil,
              ["url_env", "key_env"].contains(parts[3]) else { return [] }
        let seat = parts[1].replacingOccurrences(of: "-", with: "_")
        var names = parts[3] == "url_env"
            ? [seat + "_ROUTINE_URL", "ZULIP_ALERT_" + seat + "_ENDPOINT"]
            : [seat + "_ROUTINE_KEY", "ZULIP_ALERT_" + seat + "_KEY"]
        if let existing, isName(existing), !names.contains(existing) { names.insert(existing, at: 0) }
        return names
    }

    static func isName(_ text: String) -> Bool {
        text.range(of: "^[A-Z][A-Z0-9_]{0,95}$", options: .regularExpression) != nil
    }
}

struct ServerSettingsValidationProblem: Equatable {
    let code: String
    let field: String
    let reason: String

    init(code: String, field: String, reason: String) {
        self.code = code
        // A rejected object key can itself contain a credential; never echo such a key.
        self.field = field.split(separator: ".").contains(where: { ServerSettingsBuilder.looksLikeCredential(String($0)) })
            ? "settings field" : field
        self.reason = reason
    }

    var sentence: String { "\(code): \(field) \(reason)" }
}

/// A revision-fenced edit through the existing owner-authorized server command.
struct ServerSettingsSaveRequest: Equatable {
    var set: [String: ServerSettingValue]
    var expectBaseSHA256: String
    /// A full canonical response must not silently remove fields the owner was editing.
    var expectedKeys: Set<String> = []
    var remove: [String] = []
    /// Names from the verified rows, never serialized or derived from the process environment.
    var referenceNames: [String: Set<String>] = [:]
    var attempt: ServerSettingsAttempt = .fresh()

    static func validSet(_ set: [String: ServerSettingValue], references: [String: Set<String>] = [:]) -> Bool {
        setProblem(set, references: references) == nil
    }

    static func setProblem(_ set: [String: ServerSettingValue], references: [String: Set<String>] = [:]) -> ServerSettingsValidationProblem? {
        func invalid(_ field: String, _ reason: String) -> ServerSettingsValidationProblem {
            ServerSettingsValidationProblem(code: "invalid_settings", field: field, reason: reason)
        }
        guard !set.isEmpty, set.count <= ServerListenerSettingsRequest.maximumChanges else {
            return invalid("set", "must contain 1 to 32 settings.")
        }
        for (index, key) in set.keys.sorted().enumerated() {
            guard let value = set[key] else { continue }
            let unknown = "set[\(index)]"
            let parts = key.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
            let table: String
            let name: String
            if parts.count == 3, parts[0] == "seat" {
                guard parts[1].range(of: "^[A-Z][A-Z0-9-]{0,39}$", options: .regularExpression) != nil else {
                    return invalid(unknown, "has an invalid seat path.")
                }
                table = "seat"
                name = parts[2]
            } else if parts.count == 2 {
                table = parts[0]
                name = parts[1]
                guard ["daemon", "linear_comments", "linear_projects", "notes_queue", "listener_api"].contains(table) else {
                    return invalid(unknown, "has an unknown section.")
                }
            } else { return invalid(unknown, "has an invalid field path.") }
            guard name.range(of: "^[a-z][a-z0-9_]{0,47}$", options: .regularExpression) != nil,
                  ServerSettingsAllowlist.mayOffer(table: table, name: name) else {
                return invalid(unknown, "is not an editable field.")
            }
            if let problem = valueProblem(value, path: key, references: references) { return problem }
        }
        return nil
    }

    private static func valueProblem(_ value: ServerSettingValue, path: String,
                                     references: [String: Set<String>], depth: Int = 0) -> ServerSettingsValidationProblem? {
        func invalid(_ reason: String) -> ServerSettingsValidationProblem {
            ServerSettingsValidationProblem(code: "invalid_settings", field: path, reason: reason)
        }
        guard depth <= 4 else { return invalid("is nested too deeply.") }
        let canonical = ServerSettingsReferences.names(path: path)
        if !canonical.isEmpty {
            guard case .text(let text) = value else { return invalid("must be an environment-variable name.") }
            let allowed = Set(canonical).union(references[path] ?? [])
            if ServerSettingsReferences.isName(text) && allowed.contains(text) { return nil }
            return ServerSettingsBuilder.looksLikeCredential(text)
                ? ServerSettingsValidationProblem(code: "secret_refused", field: path, reason: "looks like a credential; the edit was not sent.")
                : invalid("must name this seat's existing or canonical routine variable.")
        }
        switch value {
        case .bool, .int: return nil
        case .number(let number): return number.isFinite ? nil : invalid("must be finite.")
        case .text(let text):
            return ServerSettingsBuilder.looksLikeCredential(text)
                ? ServerSettingsValidationProblem(code: "secret_refused", field: path, reason: "looks like a credential; the edit was not sent.") : nil
        case .list(let values):
            guard values.count <= 32 else { return invalid("contains too many entries.") }
            for (index, value) in values.enumerated() {
                if let problem = valueProblem(value, path: "\(path)[\(index)]", references: references, depth: depth + 1) { return problem }
            }
            return nil
        case .table(let values):
            guard values.count <= 32 else { return invalid("contains too many fields.") }
            for (index, key) in values.keys.sorted().enumerated() {
                guard key.range(of: "^[a-z][a-z0-9_]{0,47}$", options: .regularExpression) != nil else {
                    return invalid("has an invalid subfield at index \(index).")
                }
                let field = path + "." + key
                let reference = !ServerSettingsReferences.names(path: field).isEmpty
                guard reference || !ServerSettingsAllowlist.readsAsSecret(key) else {
                    return ServerSettingsValidationProblem(code: "secret_refused", field: field, reason: "is not a nonsecret settings field.")
                }
                if let value = values[key], let problem = valueProblem(value, path: field, references: references, depth: depth + 1) { return problem }
            }
            return nil
        case .null: return invalid("must have a typed value.")
        }
    }

    var validationProblem: ServerSettingsValidationProblem? {
        if let problem = Self.setProblem(set, references: referenceNames) { return problem }
        if !Self.validRemovals(remove, set: set) {
            return ServerSettingsValidationProblem(code: "invalid_settings", field: "remove", reason: "must explicitly match omitted existing table overrides.")
        }
        if !ServerSettingsAnswer.isHash(expectBaseSHA256) {
            return ServerSettingsValidationProblem(code: "invalid_settings", field: "expectBaseSHA256", reason: "must be the last canonical revision.")
        }
        if !attempt.isValid {
            return ServerSettingsValidationProblem(code: "invalid_settings", field: "requestID", reason: "must identify one bounded save attempt.")
        }
        return nil
    }

    var standardInput: Data? {
        guard validationProblem == nil else { return nil }
        var document: [String: ServerSettingValue] = [
            "action": .text("save"), "set": .table(set), "expectBaseSHA256": .text(expectBaseSHA256),
            "requestID": .text(attempt.requestID), "requestCreatedAt": .int(attempt.requestCreatedAt),
        ]
        if !remove.isEmpty { document["remove"] = .list(remove.sorted().map(ServerSettingValue.text)) }
        guard let json = ServerSettingValue.table(document).json else { return nil }
        let data = Data(json.utf8)
        return data.count <= ServerListenerSettingsRequest.maximumBytes ? data : nil
    }

    static func validRemovals(_ remove: [String], set: [String: ServerSettingValue]) -> Bool {
        guard Set(remove).count == remove.count, set.count + remove.count <= ServerListenerSettingsRequest.maximumChanges else { return false }
        return remove.allSatisfy { key in
            let parts = key.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
            guard parts.count == 4, parts[0] == "seat", ["budget", "routine", "live"].contains(parts[2]),
                  parts[3].range(of: "^[a-z][a-z0-9_]{0,47}$", options: .regularExpression) != nil,
                  let table = set[parts.prefix(3).joined(separator: ".")]?.tableEntries else { return false }
            return table[parts[3]] == nil
        }
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
    case outcomeUnknown(String, ServerSettingsAnswer?)
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
        if let problem = request.validationProblem { return .failed(problem.sentence, nil) }
        guard request.standardInput != nil else {
            return .failed("invalid_settings: request exceeds the allowed message size.", nil)
        }
        switch await authorize() {
        case .approved: break
        case .notApproved(let why), .unavailable(let why): return .failed(why, nil)
        }
        let invocation: OpsInvocation
        do { invocation = try makeBuilder().listenerSettingsSaveInvocation(request) }
        catch { return .failed("The settings command could not be prepared, so the edit was not sent.", nil) }
        do {
            return Self.receipt(try await runner.run(invocation), request: request)
        } catch {
            return .outcomeUnknown(Self.uncertain, nil)
        }
    }

    /// Rechecking an existing request cannot send settings or repeat its provider write.
    func reconcile(_ request: ServerSettingsSaveRequest) async -> ServerSettingsSaving {
        do {
            let invocation = try makeBuilder().listenerSettingsReconcileInvocation(request.attempt)
            return Self.receipt(try await runner.run(invocation), request: request, reconciling: true)
        } catch {
            return .outcomeUnknown(Self.uncertain, nil)
        }
    }

    private static let uncertain = "The server did not confirm the save.\(ServerStatusCopy.sentenceGap)The edit is retained; it has not been retried."

    static func receipt(_ process: OpsProcessResult, request: ServerSettingsSaveRequest,
                        reconciling: Bool = false) -> ServerSettingsSaving {
        guard !process.timedOut, let answer = ServerSettingsAnswer(stdout: process.stdout) else {
            return .outcomeUnknown(uncertain, nil)
        }
        let candidates = [process.stdout.trimmingCharacters(in: .whitespacesAndNewlines)]
            + process.stdout.split(whereSeparator: \.isNewline).reversed().map(String.init)
        let outer = candidates.compactMap { ServerSettingJSON.parse($0)?.tableEntries }
            .first { $0["eavesdropperSettings"]?.tableEntries != nil }
        let document = outer?["eavesdropperSettings"]?.tableEntries
        let protocolOK = document?["schemaVersion"] == .int(1) && document?["tool"] == .text("eavesdropper-settings")
            && answer.isWellFormed && answer.requestID == request.attempt.requestID
            && answer.action == (reconciling ? "reconcile" : "save")
            && (reconciling ? ["save", "cancelled"].contains(answer.operation ?? "")
                           : answer.operation == nil || answer.operation == "save" || answer.operation == "cancelled")
        let canonical = protocolOK && answer.source == "infisical"
            && answer.revision != nil && answer.persisted == true && answer.readback == true
            && (answer.baseSHA256 == nil || answer.baseSHA256 == answer.revision)
            && !answer.settings.isEmpty && request.expectedKeys.isSubset(of: Set(answer.settings.keys))
        let coherentSave = answer.result == "saved" && answer.ok
            && outer?["ok"] == .bool(true) && outer?["result"] == .text("saved")
        let coherentFailure = answer.result == "failed" && !answer.ok
            && outer?["ok"] == .bool(false) && outer?["result"] == .text("failed")
        let readback = canonical && (coherentSave || coherentFailure) ? answer : nil
        let targetMatches = request.remove.allSatisfy({ key in
            let parts = key.split(separator: ".").map(String.init)
            guard parts.count == 4, let table = answer.settings[parts.prefix(3).joined(separator: ".")]?.tableEntries else { return false }
            return table[parts[3]] == nil
        }) && request.set.allSatisfy({ answer.settings[$0.key] == $0.value })
        if process.exitCode == 0, coherentSave, canonical, answer.outcome == "ok",
              answer.operation != "cancelled",
              answer.runtimeApplied == true, answer.autosaveEnabled == true,
              targetMatches {
            return .saved(answer)
        }
        let why = OpsResultParser.sanitizedForDisplay(answer.detail ?? answer.errorCode ?? uncertain)
        // Only the server's explicit, identity-bound terminal verdict releases an old attempt.
        let refused = ["request_refused", "provider_write_refused", "prior_outcome_unknown", "version_conflict"]
            .contains(answer.errorCode ?? "")
        let cancelled = answer.errorCode == "request_cancelled" && answer.operation == "cancelled"
            && document?["cancelled"] == .bool(true)
        let knownEarlierSuccess = answer.outcome == "ok" && answer.operation == "save"
            && ["request_superseded", "prior_result_unavailable"].contains(answer.errorCode ?? "")
        if protocolOK, coherentFailure,
           (answer.outcome == "refused" && (refused || cancelled)) || knownEarlierSuccess {
            return .failed(why, readback)
        }
        return .outcomeUnknown(uncertain, readback)
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
