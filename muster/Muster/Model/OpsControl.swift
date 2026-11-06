import Foundation
import Combine

enum FleetServiceActionRouting {
    static func usesFleetAdmin(serviceID: String, label: String, allowlist: OpsAllowlist?) -> Bool {
        if serviceID == FleetServiceRegistry.server.id { return true }
        return allowlist?.verbs(forMacAgent: label).isEmpty != false
    }
}

// Starting,  stopping,  restarting and updating the fleet's processes,  and writing a knob.
//
// Muster holds no credential.  On this path it touches no process itself.  It runs two
// helpers and reads what they answer:
//
//   * `scripts/fleet-ops-control.py`,  for the Mac's LaunchAgents (run here) and the
//     server's units,  updates and Coolify redeploys (run on the server,  over ssh to the
//     `fleet-ops-ctl` alias,  whose key's forced command is `fleet-ops-control.py --from-ssh`)
//   * `python3 -m fleet_knobs set`,  for a non-secret knob
//
// Both helpers check `scripts/fleet-ops-control.json` or the knob registry themselves,  so
// nothing outside the allowlist can be touched on this path.
//
// Every action runs `--plan` first,  which changes nothing,  and Muster shows the plan.  The
// view asks for Touch ID,  then Muster plans again,  refuses when the plan changed,  and only
// then runs `--yes`.  One action runs at a time in this app,  and the helper holds its own
// lock on each machine,  so a second app or a shell cannot run one alongside it.
//
// The one exception to Touch ID is `OpsController.runUnattended`:  a start that Muster takes on
// its own for a service whose policy says to keep it running (`ServiceReconciler`).  It plans,
// runs and audits exactly as above,  with nobody at a sheet,  and it accepts only a start.
//
// `ServiceActionRouter` is the one way in for a service row,  from the menu or the panel.
// BotFleet Server and any LaunchAgent the allowlist does not name stay on `FleetAdmin`'s
// direct route,  with no plan,  no Touch ID and no audit row:  an allowlist argv cannot say
// what their start and stop do.  Everything else comes here.

// MARK: - The route

enum OpsControlRoute {
    /// The ssh alias the server verbs go over.
    ///
    /// It is set up the same way as the doctor's read-only `fleet-tools-ro` alias
    /// (`docs/protocols/fleet-tool-parity.md`):  a `Host fleet-ops-ctl` entry in
    /// `~/.ssh/config` that names the server and the `fleet-ops-ctl` key,  whose forced
    /// command on the server is `fleet-ops-control.py --from-ssh`.  The alias is a name and
    /// not an address,  so no host is written in this source.
    static let serverAlias = "fleet-ops-ctl"
    static let ssh = "/usr/bin/ssh"
    /// The doctor's own ssh options:  never prompt,  give up quickly,  and never accept a
    /// host key that is not already known.  A connection that goes quiet after it opened is
    /// given up after three missed 15-second checks,  so a dead link ends in under a minute
    /// instead of holding the sheet until the timeout.
    static let sshOptions = [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    /// The pinned interpreter,  the one the rest of the app runs.
    static let interpreter = "/usr/bin/python3"
    /// The helper,  relative to the repository root.
    static let scriptPath = "scripts/fleet-ops-control.py"
    /// The knob engine's entry point,  relative to the repository root.
    static let knobsEntryPoint = "scripts/fleet_knobs/__main__.py"
    /// The allowlist,  relative to the repository root.
    static let allowlistPath = "scripts/fleet-ops-control.json"

    /// A plan only reads,  so it is short.
    static let planTimeout: TimeInterval = 90
    /// A run on the Mac:  a LaunchAgent bootstrap and kickstart can each take 90 seconds,
    /// and a knob write goes through the Infisical CLI,  which has no bound of its own.
    static let runTimeout: TimeInterval = 900
    /// A run on the server.  The server's helper bounds each command at 90 seconds,  its
    /// update check at 12 and a redeploy at 30 plus 30,  so three minutes covers the slowest
    /// with room for ssh,  and a lost run frees the app in minutes and not a quarter hour.
    static let serverRunTimeout: TimeInterval = 180
    /// How long a timed-out helper has to exit after SIGTERM before it gets SIGKILL.
    static let killGrace: TimeInterval = 5

    /// The longest Muster waits for a run of this action.
    static func runWait(for action: OpsAction) -> TimeInterval {
        action.runsOnServer ? serverRunTimeout : runTimeout
    }
}

// MARK: - The actions

/// What a LaunchAgent or a unit can be asked to do.
enum OpsVerb: String, CaseIterable, Identifiable {
    case start
    case stop
    case restart

    var id: String { rawValue }

    var title: String {
        switch self {
        case .start: return "Start"
        case .stop: return "Stop"
        case .restart: return "Restart"
        }
    }
}

/// One action the owner can ask for.
enum OpsAction: Equatable {
    /// `mac start|stop|restart LABEL`,  run on the Mac.
    case macAgent(verb: OpsVerb, label: String)
    /// `server start|stop|restart UNIT`,  run on the server.
    case serverUnit(verb: OpsVerb, unit: String)
    /// `server update NAME`,  run on the server.
    case serverUpdate(name: String)
    /// `server redeploy UUID`,  run on the server.  The name is for the screen only.
    case coolifyRedeploy(uuid: String, name: String)
    /// `fleet_knobs set KEY VALUE`,  run on the Mac,  changing a value every machine reads.
    case knobSet(key: String, value: String)
    /// `fleet_knobs unset KEY`,  run on the Mac:  removes the override so the default applies.
    case knobUnset(key: String)
    /// `pm2 start|stop|restart NAME`,  run on the Mac.
    case pm2App(verb: OpsVerb, name: String)
    /// `server logs UNIT`,  run on the server:  the last journal lines,  read only.
    case logs(unit: String)
    /// Change one coding resource-policy field,  reviewed against a revision on the server.
    case serverResource(field: String, value: String, revision: String)
    /// Read the server's coding resource-policy receipt,  without changing anything.
    case resourceStatus

    /// The machine the action runs on.
    ///
    /// It is an exhaustive switch with no default,  and `requiredFile` and `invocation`
    /// both branch on it first,  so ssh is reachable only from `.server`,  and the words on
    /// the screen come from the same value that picks the route.
    var runsOn: FleetHost {
        switch self {
        case .serverUnit, .serverUpdate, .coolifyRedeploy, .logs, .serverResource, .resourceStatus:
            return .server
        case .macAgent, .knobSet, .knobUnset, .pm2App:
            return .mac
        }
    }

    /// Where the action's effect lands.
    var changes: FleetPlace {
        switch self {
        case .knobSet, .knobUnset: return .acrossFleet
        case .macAgent, .serverUnit, .serverUpdate, .coolifyRedeploy, .pm2App, .logs,
             .serverResource, .resourceStatus:
            return .machine(runsOn)
        }
    }

    /// True when the action runs on the server,  over ssh.
    var runsOnServer: Bool { runsOn == .server }

    /// True when the action only reads.  Nothing can have happened on a run that went wrong
    /// part way,  and the Mac knows whether the text reached it,  so it plainly did not work.
    var changesNothing: Bool {
        switch self {
        case .logs, .resourceStatus:
            return true
        case .macAgent, .serverUnit, .serverUpdate, .coolifyRedeploy, .knobSet, .knobUnset, .pm2App,
             .serverResource:
            return false
        }
    }

    /// The name the owner reads for what the action touches.
    var subjectTitle: String {
        switch self {
        case .macAgent(_, let label): return OpsNames.agentTitle(label)
        case .serverUnit(_, let unit): return OpsNames.unitTitle(unit)
        case .serverUpdate(let name): return name
        case .coolifyRedeploy(_, let name): return name
        case .knobSet(let key, _): return key
        case .knobUnset(let key): return key
        case .pm2App(_, let name): return OpsNames.pm2Title(name)
        case .logs(let unit): return OpsNames.unitTitle(unit)
        case .serverResource(let field, _, _): return ResourcePolicyField(rawValue: field)?.title ?? "Resource Policy"
        case .resourceStatus: return "The Resource Policy"
        }
    }

    /// The action,  in a few words,  for a sheet's title and the Touch ID question.  It
    /// always names the place,  from `changes`.
    var title: String {
        let place = changes.inProse
        switch self {
        case .macAgent(let verb, _): return "\(verb.title) \(subjectTitle) \(place)"
        case .serverUnit(let verb, _): return "\(verb.title) \(subjectTitle) \(place)"
        case .serverUpdate(let name): return "Run the \(name) update \(place)"
        case .coolifyRedeploy(_, let name): return "Redeploy \(name) \(place)"
        case .knobSet(let key, let value): return "Set \(key) to \"\(value)\" \(place)"
        case .knobUnset(let key): return "Reset \(key) to the default \(place)"
        case .pm2App(let verb, _): return "\(verb.title) \(subjectTitle) \(place)"
        case .logs: return "Read the last logs of \(subjectTitle) \(place)"
        case .serverResource(let field, let value, _):
            return "\(ResourcePolicyField.changePhrase(field: field, value: value)) \(place)"
        case .resourceStatus: return "Read the resource policy \(place)"
        }
    }

    /// The button that runs the action.  The Mac asks for Touch ID,  or the login password
    /// where it offers that instead,  so the button names the approval and not one way of
    /// giving it.
    var runButtonTitle: String {
        switch changes {
        case .machine(let host): return "Approve And Run \(host.onTitle)"
        case .acrossFleet: return "Approve And Change For The Whole Fleet"
        }
    }

    /// What the action does,  in plain words,  at the top of the plan.  The exact commands
    /// are kept for the owner who opens them.
    var effectSentence: String {
        let gap = ServerStatusCopy.sentenceGap
        let place = runsOn.inProse
        func verbSentence(_ verb: OpsVerb) -> String {
            switch verb {
            case .start: return "Starts \(subjectTitle) \(place)."
            case .stop: return "Stops \(subjectTitle) \(place)."
            case .restart: return "Stops \(subjectTitle) \(place) and starts it again."
            }
        }
        switch self {
        case .macAgent(let verb, _), .serverUnit(let verb, _), .pm2App(let verb, _):
            return verbSentence(verb)
        case .serverUpdate(let name):
            return "Starts the \(name) update \(place), then checks for a short while whether it finished."
        case .coolifyRedeploy(_, let name):
            return "Asks Coolify \(place) to redeploy \(name).\(gap)Muster learns only that Coolify accepted the request, not that the new deploy is healthy."
        case .knobSet(let key, let value):
            return "Sets \(key) to \"\(value)\" for every machine that reads it."
        case .knobUnset(let key):
            return "Removes the \(key) setting, so every machine that reads it goes back to the default."
        case .logs:
            return "Reads the last journal lines of \(subjectTitle) \(place).\(gap)Nothing changes."
        case .serverResource(let field, let value, _):
            // The field's own words for the change (`ResourcePolicyField.changePhrase`),  told
            // as what the plan does:  "Set" becomes "Sets",  "Turn" becomes "Turns".
            var phrase = ResourcePolicyField.changePhrase(field: field, value: value)
            for (order, does) in [("Set ", "Sets "), ("Turn ", "Turns "), ("Change ", "Changes ")]
                where phrase.hasPrefix(order) {
                phrase = does + phrase.dropFirst(order.count)
                break
            }
            return "\(phrase) \(place)."
        case .resourceStatus:
            return "Reads the resource policy \(place).\(gap)Nothing changes."
        }
    }

    /// The sentence under the sheet's title.
    var placeSentence: String {
        let runs = "Runs \(runsOn.inProse)."
        if changes == .acrossFleet {
            return "\(runs)\(ServerStatusCopy.sentenceGap)Changes a value every machine reads."
        }
        if case .macAgent(_, let label) = self, label == OpsNames.fetchAgentLabel {
            return "\(runs)\(ServerStatusCopy.sentenceGap)Asks the server for its report."
        }
        return runs
    }

    /// What a stop does,  shown on the plan,  or nil when the action is not a stop.
    var stopWarning: String? {
        switch self {
        case .macAgent(let verb, _):
            return verb == .stop ? FleetServices.launchAgentStopBody : nil
        case .pm2App(let verb, _):
            return verb == .stop ? FleetServices.pm2StopBody : nil
        case .serverUnit(let verb, let unit):
            guard verb == .stop else { return nil }
            if unit == OpsNames.reportTimerUnit {
                return "It stops right away on the server, and the server's report stops with it, so the Mac stops knowing the server within 45 minutes."
            }
            return "It stops right away on the server, and stays stopped until you start it again."
        case .serverUpdate, .coolifyRedeploy, .knobSet, .knobUnset, .logs, .serverResource, .resourceStatus:
            return nil
        }
    }

    /// The sentence shown when a helper's answer named the machine this action runs on.
    var answeredSentence: String {
        switch runsOn {
        case .mac: return "The Mac answered this plan."
        case .server: return "The server answered this plan."
        }
    }

    /// Which case this is,  for a test that has to cover every case.
    var caseTag: OpsActionCase {
        switch self {
        case .macAgent: return .macAgent
        case .serverUnit: return .serverUnit
        case .serverUpdate: return .serverUpdate
        case .coolifyRedeploy: return .coolifyRedeploy
        case .knobSet: return .knobSet
        case .knobUnset: return .knobUnset
        case .pm2App: return .pm2App
        case .logs: return .logs
        case .serverResource: return .serverResource
        case .resourceStatus: return .resourceStatus
        }
    }

    /// The verb,  as the audit log names it.
    var auditVerb: String {
        switch self {
        case .macAgent(let verb, _): return "mac \(verb.rawValue)"
        case .serverUnit(let verb, _): return "server \(verb.rawValue)"
        case .serverUpdate: return "server update"
        case .coolifyRedeploy: return "server redeploy"
        case .knobSet: return "knob set"
        case .knobUnset: return "knob unset"
        case .pm2App(let verb, _): return "pm2 \(verb.rawValue)"
        case .logs: return "server logs"
        case .serverResource: return "server resource policy"
        case .resourceStatus: return "server resource status"
        }
    }

    /// The target,  as the audit log names it.
    ///
    /// A redeploy is named by its resource name and not its uuid,  and a knob by its key
    /// alone:  the knob engine keeps its own audit of the values.
    var auditTarget: String {
        switch self {
        case .macAgent(_, let label): return label
        case .serverUnit(_, let unit): return unit
        case .serverUpdate(let name): return name
        case .coolifyRedeploy(_, let name): return name
        case .knobSet(let key, _): return key
        case .knobUnset(let key): return key
        case .pm2App(_, let name): return name
        case .logs(let unit): return unit
        case .serverResource(let field, _, _): return "coding.\(field)"
        case .resourceStatus: return "coding"
        }
    }

    /// The words after the helper's name and before the mode flag.
    var verbArguments: [String] {
        switch self {
        case .macAgent(let verb, let label): return ["mac", verb.rawValue, label]
        case .serverUnit(let verb, let unit): return ["server", verb.rawValue, unit]
        case .serverUpdate(let name): return ["server", "update", name]
        case .coolifyRedeploy(let uuid, _): return ["server", "redeploy", uuid]
        case .knobSet(let key, let value): return ["set", key, value]
        case .knobUnset(let key): return ["unset", key]
        case .pm2App(let verb, let name): return ["pm2", verb.rawValue, name]
        case .logs(let unit): return ["server", "logs", unit]
        case .serverResource(let field, let value, let revision):
            return ["server", "resource-policy", "\(field).\(value).\(revision)"]
        case .resourceStatus: return ["server", "resource-status", "coding"]
        }
    }
}

/// A shadow of `OpsAction`'s cases.  A new case cannot compile without a tag and a sample,
/// so the tests that walk every case cover it the day it is added.
enum OpsActionCase: CaseIterable {
    case macAgent, serverUnit, serverUpdate, coolifyRedeploy, knobSet, knobUnset
    case pm2App, logs, serverResource, resourceStatus

    /// One valid action of this case.
    var sample: OpsAction {
        switch self {
        case .macAgent: return .macAgent(verb: .restart, label: OpsNames.fetchAgentLabel)
        case .serverUnit: return .serverUnit(verb: .restart, unit: OpsNames.reportTimerUnit)
        case .serverUpdate: return .serverUpdate(name: "fleet-cli")
        case .coolifyRedeploy: return .coolifyRedeploy(uuid: "abc123def456", name: "muster-web")
        case .knobSet: return .knobSet(key: "FLEET_X", value: "1")
        case .knobUnset: return .knobUnset(key: "FLEET_X")
        case .pm2App: return .pm2App(verb: .restart, name: "shellular")
        case .logs: return .logs(unit: OpsNames.reportTimerUnit)
        case .serverResource:
            return .serverResource(field: "cpu-cores", value: "4", revision: String(repeating: "a", count: 64))
        case .resourceStatus: return .resourceStatus
        }
    }
}

/// The names the owner reads for what an action touches.
///
/// A small table,  which can go stale:  anything it does not name shows its raw name.
enum OpsNames {
    /// The timer that writes the server's report.
    static let reportTimerUnit = "fleet-server-status.timer"
    /// The LaunchAgent that fetches the server's report to the Mac.
    static let fetchAgentLabel = "com.jay.fleet-tools-doctor"

    static func unitTitle(_ unit: String) -> String {
        switch unit {
        case reportTimerUnit: return "Server Report Timer"
        case "fleet-cli-git-update.timer": return "Fleet Tools Update Timer"
        default: return unit
        }
    }

    static func agentTitle(_ label: String) -> String {
        if label == fetchAgentLabel { return "Fleet Tools Doctor" }
        return FleetServiceRegistry.descriptor(for: label)?.title ?? label
    }

    static func pm2Title(_ name: String) -> String {
        let named = FleetServiceRegistry.all.first { descriptor in
            if case .pm2(let appName) = descriptor.kind { return appName == name }
            return false
        }
        return named?.title ?? name
    }
}

/// Whether a run only plans or does the thing.
enum OpsMode: String {
    case plan
    case yes

    var flag: String { "--\(rawValue)" }
}

// MARK: - The invocation

/// One process to run:  an executable,  an argv array and an explicit environment.
///
/// The arguments are never a shell string.
struct OpsInvocation: Equatable {
    var executable: String
    var arguments: [String]
    var workingDirectory: String
    var environment: [String: String]
    var timeout: TimeInterval
}

enum OpsControlError: Error, Equatable, LocalizedError {
    case invalidArgument(String)
    case helperNotFound(String)
    case launchFailed(String)
    case outputTooLarge
    case busy

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let label):
            return "The \(label) is not a value Muster will pass on,\(ServerStatusCopy.sentenceGap)so nothing was run."
        case .helperNotFound(let path):
            return ApplyCopy.join(
                "The helper was not found at \(path).",
                "Set the fleetConfigRepoRoot default or the MUSTER_REPO_ROOT variable to the repository root."
            )
        case .launchFailed(let detail):
            return "The helper could not be started: \(detail)"
        case .outputTooLarge:
            return "The helper produced more output than Muster will read."
        case .busy:
            return "Another action is still running."
        }
    }
}

/// Builds the one invocation for an action.
struct OpsCommandBuilder {
    var repoRoot: URL
    var home: URL = FileManager.default.homeDirectoryForCurrentUser
    /// The ssh agent's socket,  when this app was started with one,  so a key held in the
    /// agent can be used.
    var sshAuthSocket: String? = ProcessInfo.processInfo.environment["SSH_AUTH_SOCK"]

    /// The longest knob value Muster passes on.
    static let maximumValueLength = 1024
    private static let resourcePolicyFields: Set<String> = [
        "cpu-cores", "cpu-weight", "ram-budget-gib", "ram-critical-gib", "ram-reserve-gib", "borrow-idle",
    ]

    /// A label,  unit,  update name or uuid is a plain token:  it starts with a letter or a
    /// digit and holds only letters,  digits,  `.`,  `_`,  `-` and `@`.  A token that could
    /// be read as a flag,  or that carries whitespace,  a path separator or a shell
    /// character,  is refused before any process exists.
    static func isSafeToken(_ value: String) -> Bool {
        guard value.count <= 200,
              let first = value.unicodeScalars.first,
              CharacterSet.alphanumerics.contains(first),
              first.isASCII else { return false }
        let allowed = CharacterSet(charactersIn: "._-@")
        return value.unicodeScalars.allSatisfy {
            $0.isASCII && (CharacterSet.alphanumerics.contains($0) || allowed.contains($0))
        }
    }

    /// A knob key is the registry's own shape:  upper case letters,  digits and `_`.
    static func isKnobKey(_ value: String) -> Bool {
        guard value.count <= 128, let first = value.unicodeScalars.first,
              isUpperCaseLetter(first) else { return false }
        return value.unicodeScalars.allSatisfy {
            isUpperCaseLetter($0) || isDigit($0) || $0 == "_"
        }
    }

    private static func isUpperCaseLetter(_ scalar: Unicode.Scalar) -> Bool {
        (65...90).contains(scalar.value)
    }

    private static func isLowerCaseLetter(_ scalar: Unicode.Scalar) -> Bool {
        (97...122).contains(scalar.value)
    }

    private static func isLetter(_ scalar: Unicode.Scalar) -> Bool {
        isUpperCaseLetter(scalar) || isLowerCaseLetter(scalar)
    }

    /// A launchd label in the helper's own shape, so a label the helper would refuse
    /// is refused here and not after Touch ID.
    static func isAgentLabel(_ value: String) -> Bool {
        guard (1...127).contains(value.count),
              let first = value.unicodeScalars.first,
              isLetter(first) || isDigit(first) else { return false }
        return value.unicodeScalars.allSatisfy {
            isLetter($0) || isDigit($0) || $0 == "." || $0 == "_" || $0 == "-"
        }
    }

    /// A systemd unit in the helper's own shape:  a plain `.service` or `.timer`,
    /// with no template `@` in it.
    static func isUnitName(_ value: String) -> Bool {
        for suffix in [".service", ".timer"] where value.hasSuffix(suffix) {
            let stem = value.dropLast(suffix.count)
            guard (1...121).contains(stem.count),
                  let first = stem.unicodeScalars.first,
                  isLetter(first) || isDigit(first) else { return false }
            return stem.unicodeScalars.allSatisfy {
                isLetter($0) || isDigit($0) || $0 == "_" || $0 == "." || $0 == "-"
            }
        }
        return false
    }

    /// A server update name in the helper's own shape.
    static func isUpdateName(_ value: String) -> Bool {
        guard (1...64).contains(value.count),
              let first = value.unicodeScalars.first,
              isLowerCaseLetter(first) || isDigit(first) else { return false }
        return value.unicodeScalars.allSatisfy {
            isLowerCaseLetter($0) || isDigit($0) || $0 == "-"
        }
    }

    static func isInvocationID(_ value: String) -> Bool {
        value.count == 32 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    /// A pm2 application name in the helper's own shape:  the registry's lowercase
    /// names, never a flag or a path.
    static func isPM2Name(_ value: String) -> Bool {
        guard (1...64).contains(value.count),
              let first = value.unicodeScalars.first,
              isLowerCaseLetter(first) || isDigit(first) else { return false }
        return value.unicodeScalars.allSatisfy {
            isLowerCaseLetter($0) || isDigit($0) || $0 == "." || $0 == "_" || $0 == "-"
        }
    }

    /// A Coolify resource id in the helper's own shape:  short letters and digits.
    static func isResourceID(_ value: String) -> Bool {
        guard (6...64).contains(value.count) else { return false }
        return value.unicodeScalars.allSatisfy { isLetter($0) || isDigit($0) }
    }

    private static func isDigit(_ scalar: Unicode.Scalar) -> Bool {
        (48...57).contains(scalar.value)
    }

    /// A knob value is passed as one argument,  so any text is safe to pass except a
    /// control character,  a value too long to be a knob,  and a value the helper's
    /// parser would read as a flag (a leading `-` that is not a number).
    static func isSafeKnobValue(_ value: String) -> Bool {
        guard value.count <= maximumValueLength else { return false }
        guard !value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            return false
        }
        if value.hasPrefix("-") {
            // Only a plain negative number:  `-inf` parses as a number and still reads as
            // a flag to the helper's parser.
            let rest = value.unicodeScalars.dropFirst()
            return !rest.isEmpty
                && rest.allSatisfy { isDigit($0) || $0 == "." }
                && Double(value) != nil
        }
        return true
    }

    private static func isSafeResourceRevision(_ value: String) -> Bool {
        value.count == 64 && value.unicodeScalars.allSatisfy {
            $0.isASCII && ((65...70).contains($0.value) || (97...102).contains($0.value) || (48...57).contains($0.value))
        } && !value.contains(where: { $0.isUppercase })
    }

    private static func isSafeResourceValue(field: String, value: String) -> Bool {
        if field == "borrow-idle" { return value == "0" || value == "1" }
        guard !value.isEmpty, value.utf8.allSatisfy({ (48...57).contains($0) }),
              let number = Int(value), number <= 10_000, String(number) == value else { return false }
        switch field {
        case "cpu-cores", "cpu-weight", "ram-critical-gib": return number >= 1
        case "ram-budget-gib", "ram-reserve-gib": return number >= 2
        default: return false
        }
    }

    /// Check an action's values before anything runs.
    static func validate(_ action: OpsAction) throws {
        switch action {
        case .macAgent(_, let label):
            guard isAgentLabel(label) else { throw OpsControlError.invalidArgument("agent label") }
        case .serverUnit(_, let unit):
            guard isUnitName(unit) else { throw OpsControlError.invalidArgument("unit name") }
        case .serverUpdate(let name):
            guard isUpdateName(name) else { throw OpsControlError.invalidArgument("update name") }
        case .coolifyRedeploy(let uuid, _):
            guard isResourceID(uuid) else {
                throw OpsControlError.invalidArgument("resource id")
            }
        case .knobSet(let key, let value):
            guard isKnobKey(key) else { throw OpsControlError.invalidArgument("knob name") }
            guard isSafeKnobValue(value) else { throw OpsControlError.invalidArgument("knob value") }
        case .knobUnset(let key):
            guard isKnobKey(key) else { throw OpsControlError.invalidArgument("knob name") }
        case .pm2App(_, let name):
            guard isPM2Name(name) else {
                throw OpsControlError.invalidArgument("pm2 name")
            }
        case .logs(let unit):
            guard isUnitName(unit) else { throw OpsControlError.invalidArgument("unit name") }
        case .serverResource(let field, let value, let revision):
            guard resourcePolicyFields.contains(field) else { throw OpsControlError.invalidArgument("resource field") }
            guard isSafeResourceValue(field: field, value: value) else { throw OpsControlError.invalidArgument("resource value") }
            guard isSafeResourceRevision(revision) else { throw OpsControlError.invalidArgument("resource revision") }
        case .resourceStatus:
            break
        }
    }

    var helperURL: URL { repoRoot.appendingPathComponent(OpsControlRoute.scriptPath) }
    var scriptsDirectory: URL { repoRoot.appendingPathComponent("scripts", isDirectory: true) }

    /// The file a local action runs,  which must be there before anything is started.
    ///
    /// A server action runs the server's own copy,  so nothing local is needed.
    func requiredFile(for action: OpsAction) -> URL? {
        switch action.runsOn {
        case .server:
            return nil
        case .mac:
            switch action {
            case .macAgent, .pm2App: return helperURL
            case .knobSet, .knobUnset: return repoRoot.appendingPathComponent(OpsControlRoute.knobsEntryPoint)
            case .serverUnit, .serverUpdate, .coolifyRedeploy, .logs, .serverResource, .resourceStatus: return nil
            }
        }
    }

    func checkInstalled(for action: OpsAction, fileManager: FileManager = .default) throws {
        guard let file = requiredFile(for: action) else { return }
        guard fileManager.fileExists(atPath: file.path) else {
            throw OpsControlError.helperNotFound(file.path)
        }
    }

    /// The environment a local helper sees.
    ///
    /// It is the engine's own child environment,  so the knob engine is importable the same
    /// way,  with Homebrew's directories added:  the knob engine writes through the
    /// Infisical CLI,  which Homebrew installs.  Nothing from this app's own environment is
    /// passed on,  so no variable can point a helper at a stand-in.
    var localEnvironment: [String: String] {
        let location = EngineLocation(
            root: repoRoot,
            interpreter: URL(fileURLWithPath: OpsControlRoute.interpreter)
        )
        var environment = EngineRunner.childEnvironment(location: location, home: home)
        let path = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PATH"] = path + ":/opt/homebrew/bin:/usr/local/bin"
        return environment
    }

    /// The environment ssh sees:  a plain path,  the home directory for its config,  and the
    /// agent's socket when there is one.
    var sshEnvironment: [String: String] {
        var environment: [String: String] = [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": home.path,
            "LANG": "en_US.UTF-8",
        ]
        if let socket = sshAuthSocket, !socket.isEmpty {
            environment["SSH_AUTH_SOCK"] = socket
        }
        return environment
    }

    /// The sole receipt recheck route is observation of a captured fleet-cli invocation.
    func updateStatusInvocation(name: String, invocationID: String, baselineStartMonotonic: UInt64) throws -> OpsInvocation {
        guard name == "fleet-cli", Self.isInvocationID(invocationID) else {
            throw OpsControlError.invalidArgument("update invocation receipt")
        }
        return serverInvocation(
            tail: ["server", "update-status", "\(name).\(invocationID).\(baselineStartMonotonic)", "--plan", "--json"],
            timeout: OpsControlRoute.planTimeout
        )
    }

    private func serverInvocation(tail: [String], timeout: TimeInterval) -> OpsInvocation {
        OpsInvocation(
            executable: OpsControlRoute.ssh,
            arguments: OpsControlRoute.sshOptions + [OpsControlRoute.serverAlias, "--"] + tail,
            workingDirectory: "/",
            environment: sshEnvironment,
            timeout: timeout
        )
    }

    /// The one invocation for an action and a mode.
    func invocation(for action: OpsAction, mode: OpsMode) throws -> OpsInvocation {
        try Self.validate(action)
        if case .resourceStatus = action, mode != .plan {
            throw OpsControlError.invalidArgument("read-only resource status mode")
        }
        let timeout = mode == .plan ? OpsControlRoute.planTimeout : OpsControlRoute.runWait(for: action)
        var tail = action.verbArguments + [mode.flag, "--json"]
        if case .knobSet(let key, let value) = action {
            // The flags go first and `--` ends them,  so the value is never read as a flag
            // even when it starts with `-`.
            tail = ["set", mode.flag, "--json", "--", key, value]
        }
        if case .knobUnset(let key) = action {
            tail = ["unset", mode.flag, "--json", "--", key]
        }
        switch action.runsOn {
        case .server:
            // `--` ends ssh's own options,  so nothing after the alias is read as one.  The
            // server's forced command reads the rest from SSH_ORIGINAL_COMMAND and checks it
            // against its own allowlist again.
            return serverInvocation(tail: tail, timeout: timeout)
        case .mac:
            switch action {
            case .macAgent, .pm2App:
                return OpsInvocation(
                    executable: OpsControlRoute.interpreter,
                    arguments: ["-s", helperURL.path] + tail,
                    workingDirectory: "/",
                    environment: localEnvironment,
                    timeout: timeout
                )
            case .knobSet, .knobUnset:
                return OpsInvocation(
                    executable: OpsControlRoute.interpreter,
                    arguments: ["-s", "-m", "fleet_knobs"] + tail,
                    workingDirectory: scriptsDirectory.path,
                    environment: localEnvironment,
                    timeout: timeout
                )
            case .serverUnit, .serverUpdate, .coolifyRedeploy, .logs, .serverResource, .resourceStatus:
                // `runsOn` says these are the server's,  so this cannot be reached.  It
                // refuses rather than running a server action on the Mac.
                throw OpsControlError.invalidArgument("route")
            }
        }
    }
}

// MARK: - The run

/// What one process produced.
struct OpsProcessResult: Equatable {
    var exitCode: Int32
    var stdout: String
    var stderr: String
    var timedOut: Bool = false

    /// The last few non-empty stderr lines,  bounded.
    var stderrTail: String {
        let lines = stderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(4).joined(separator: "\n")
        return tail.count > 600 ? String(tail.suffix(600)) : tail
    }
}

protocol OpsRunning: Sendable {
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult
}

/// Runs one invocation as a child process,  with its output in private files.
struct OpsProcessRunner: OpsRunning {
    static let maximumOutputBytes = 4 * 1024 * 1024

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try Self.runBlocking(invocation))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private final class TimeoutFlag: @unchecked Sendable {
        private let lock = NSLock()
        private var value = false
        func set() { lock.lock(); value = true; lock.unlock() }
        var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
    }

    private static func runBlocking(_ invocation: OpsInvocation) throws -> OpsProcessResult {
        let fileManager = FileManager.default
        // Output goes to files and not pipes,  the way the engine wrapper does it:  nothing
        // can deadlock on a full pipe.
        let scratch = fileManager.temporaryDirectory
            .appendingPathComponent("muster-ops-\(UUID().uuidString)", isDirectory: true)
        // Every failure before the process starts is a launch failure:  nothing ran,  and
        // the controller relies on that to leave no audit row for it.
        do {
            try fileManager.createDirectory(
                at: scratch, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        } catch {
            throw OpsControlError.launchFailed(error.localizedDescription)
        }
        defer { try? fileManager.removeItem(at: scratch) }
        let stdoutURL = scratch.appendingPathComponent("stdout.txt")
        let stderrURL = scratch.appendingPathComponent("stderr.txt")
        fileManager.createFile(atPath: stdoutURL.path, contents: nil)
        fileManager.createFile(atPath: stderrURL.path, contents: nil)
        let stdoutHandle: FileHandle
        let stderrHandle: FileHandle
        do {
            stdoutHandle = try FileHandle(forWritingTo: stdoutURL)
            stderrHandle = try FileHandle(forWritingTo: stderrURL)
        } catch {
            throw OpsControlError.launchFailed(error.localizedDescription)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: invocation.executable)
        process.arguments = invocation.arguments
        process.currentDirectoryURL = URL(fileURLWithPath: invocation.workingDirectory, isDirectory: true)
        process.environment = invocation.environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            try? stdoutHandle.close()
            try? stderrHandle.close()
            throw OpsControlError.launchFailed(error.localizedDescription)
        }

        let flag = TimeoutFlag()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + invocation.timeout)
        timer.setEventHandler {
            flag.set()
            guard process.isRunning else { return }
            process.terminate()
            // A child that ignores SIGTERM gets SIGKILL a few seconds later,  so the wait
            // below always ends.
            let pid = process.processIdentifier
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + OpsControlRoute.killGrace) {
                if process.isRunning { _ = kill(pid, SIGKILL) }
            }
        }
        timer.resume()
        process.waitUntilExit()
        timer.cancel()
        try? stdoutHandle.close()
        try? stderrHandle.close()

        return OpsProcessResult(
            exitCode: process.terminationStatus,
            stdout: try read(stdoutURL),
            stderr: try read(stderrURL),
            timedOut: flag.isSet
        )
    }

    private static func read(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: maximumOutputBytes + 1) ?? Data()
        if data.count > maximumOutputBytes { throw OpsControlError.outputTooLarge }
        return String(decoding: data, as: UTF8.self)
    }
}

// MARK: - The answer

/// The helper's `--json` answer:
/// `{ "ok", "verb", "target", "commands": [[argv...]], "result", "detail" }`.
///
/// `fleet_knobs set` also answers `old`,  `old_source` and `new`,  and `lkg` after a write.
struct OpsResult: Decodable, Equatable {
    let ok: Bool
    let verb: String?
    let target: String?
    let commands: [[String]]
    let result: String?
    let detail: String?
    /// A knob's value before the write,  as the knob engine renders it.
    let old: String?
    /// Where that value came from:  `infisical`,  `lkg`,  `default` or `unknown`.
    let oldSource: String?
    /// The value the write sets.
    let new: String?
    /// How the last-known-good copy was brought up to date after a write.
    let lkg: String?
    /// A logs run's journal text:  the one helper answer that carries output.
    let output: String?
    /// How many journal lines `output` holds.
    let lines: Int?
    /// The server's direct coding resource-policy receipt,  when the status verb returned one.
    let resourcePolicy: ResourcePolicySnapshot?
    let executionState: String?
    let verified: Bool
    let invocationID: String?
    let baselineStartMonotonic: UInt64?
    /// The machine the helper says it ran on,  when it says.  Knob answers never carry it.
    let host: String?
    /// True when `host` is there and is not a string,  null included,  which is not the same
    /// as a helper too old to carry the field.
    let hostIsMalformed: Bool
    /// The helper's fixed code for why it refused or failed,  such as `update_busy`.
    let errorCode: String?
    /// The id of the row the helper wrote to its own audit log for this run.
    let auditID: String?
    /// The target that row names,  which can differ from the name on this Mac's button.
    let auditTarget: String?
    /// How many of the commands finished,  when a chain stopped part way or ran through.
    let completedCommands: Int?

    enum CodingKeys: String, CodingKey {
        case ok, verb, target, commands, result, detail, old, new, lkg, output, lines, resourcePolicy
        case executionState, verified, invocationID, baselineStartMonotonic, host
        case errorCode, auditID, auditTarget, completedCommands
        case oldSource = "old_source"
    }

    /// The answer decodes field by field.  A missing or unreadable `ok` is false:  an
    /// answer that does not say it worked did not work.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? container.decodeIfPresent(Bool.self, forKey: .ok)) == true
        verb = try? container.decodeIfPresent(String.self, forKey: .verb)
        target = try? container.decodeIfPresent(String.self, forKey: .target)
        commands = (try? container.decodeIfPresent([[String]].self, forKey: .commands)) ?? []
        result = try? container.decodeIfPresent(String.self, forKey: .result)
        detail = try? container.decodeIfPresent(String.self, forKey: .detail)
        old = try? container.decodeIfPresent(String.self, forKey: .old)
        oldSource = try? container.decodeIfPresent(String.self, forKey: .oldSource)
        new = try? container.decodeIfPresent(String.self, forKey: .new)
        lkg = try? container.decodeIfPresent(String.self, forKey: .lkg)
        output = try? container.decodeIfPresent(String.self, forKey: .output)
        lines = try? container.decodeIfPresent(Int.self, forKey: .lines)
        resourcePolicy = try? container.decodeIfPresent(ResourcePolicySnapshot.self, forKey: .resourcePolicy)
        executionState = try? container.decodeIfPresent(String.self, forKey: .executionState)
        verified = (try? container.decodeIfPresent(Bool.self, forKey: .verified)) == true
        invocationID = try? container.decodeIfPresent(String.self, forKey: .invocationID)
        baselineStartMonotonic = try? container.decodeIfPresent(UInt64.self, forKey: .baselineStartMonotonic)
        host = try? container.decodeIfPresent(String.self, forKey: .host)
        hostIsMalformed = host == nil && container.contains(.host)
        errorCode = try? container.decodeIfPresent(String.self, forKey: .errorCode)
        auditID = try? container.decodeIfPresent(String.self, forKey: .auditID)
        auditTarget = try? container.decodeIfPresent(String.self, forKey: .auditTarget)
        completedCommands = try? container.decodeIfPresent(Int.self, forKey: .completedCommands)
    }
}

/// What a knob write changes,  from the knob engine's answer.
struct OpsKnobChange: Equatable {
    /// The value before the write,  or nil when the knob has none or it could not be read.
    var old: String?
    /// Where `old` came from:  `infisical`,  `lkg`,  `default` or `unknown`.
    var oldSource: String?
    /// The value the write sets.
    var new: String?
    /// True when the action removes the override instead of setting a value.
    var removesValue: Bool = false
    /// After a write:  `refreshed`,  `patched` or `not-refreshed:<note>`.
    var lkg: String?

    /// The old value for the plan sheet.
    var oldText: String {
        if oldSource == nil || oldSource == "unknown" { return "unknown" }
        return old.map { "\"\($0)\"" } ?? "unset"
    }

    /// The new value for the plan sheet.
    var newText: String { removesValue ? "unset" : (new.map { "\"\($0)\"" } ?? "unknown") }

    /// Where the old value came from,  in a sentence.
    var sourceText: String {
        let gap = ServerStatusCopy.sentenceGap
        switch oldSource {
        case "infisical": return "The current value was read from Infisical."
        case "lkg":
            return "The current value is from the last-known-good copy,\(gap)so it may be behind Infisical."
        case "default":
            return "The knob is not set in Infisical,\(gap)so the current value is the registry default."
        default:
            return "The current value could not be read,\(gap)so this write may replace a value not shown here."
        }
    }

    /// The note when the last-known-good copy was not brought up to date,  or nil.
    var lkgWarning: String? {
        guard let lkg = lkg, lkg.hasPrefix("not-refreshed") else { return nil }
        let note = lkg.dropFirst("not-refreshed".count).drop(while: { $0 == ":" })
        let reason = note.isEmpty ? "" : " (\(note))"
        return "The Mac's last-known-good copy was not refreshed\(reason),\(ServerStatusCopy.sentenceGap)so tools may read the old value until the next fetch."
    }
}

/// The helper's result word.
enum OpsResultKind: String {
    case planned
    case submitted
    case done
    case refused
    case failed
    case unknown
}

/// What one run of a helper came to,  in the app's terms.
struct OpsOutcome: Equatable {
    var kind: OpsResultKind
    /// The helper's own `ok`.
    var ok: Bool
    /// What the helper ran,  or would run,  one argv per command.
    var commands: [[String]]
    /// The helper's own words,  or the app's when the helper gave none.
    var detail: String
    var exitCode: Int32
    /// What a knob write changes,  for a knob action whose answer said;  nil otherwise.
    var knobChange: OpsKnobChange? = nil
    /// A logs run's journal text;  nil for every other action and for a plan.
    var logText: String? = nil
    /// How many journal lines `logText` holds.
    var logLines: Int? = nil
    /// The direct resource-policy receipt,  when a status read supplied one.
    var resourcePolicy: ResourcePolicySnapshot? = nil
    /// Only a fixed-shape systemd receipt is displayed, never arbitrary helper output.
    var invocationID: String? = nil
    var executionState: String? = nil
    var baselineStartMonotonic: UInt64? = nil
    /// The machine the helper said it ran on.  Nil when it did not say,  which is not an
    /// error:  the installed copy of the helper may predate the field.
    var answeredFrom: FleetHost? = nil
    /// True when a run may have done its work and the Mac cannot say:  it lost the helper,
    /// or the answer does not count.  A plan changes nothing,  so a plan is never this.
    var runNotKnown: Bool = false
    /// True when this answers a plan,  which ran nothing however it came out.
    var isPlan: Bool = false
    /// The helper's fixed code for a refusal or a failure,  when it gave one.
    var errorCode: String? = nil
    /// The id of the row the helper wrote to its own audit log,  for a run that wrote one.
    var serverAuditID: String? = nil
    /// The target that row names.
    var serverAuditTarget: String? = nil
    /// How many of `commands` finished,  when the helper said.
    var completedCommands: Int? = nil
    /// True when a run's answer lists other commands than the plan the owner approved.
    var differsFromReview: Bool = false

    /// True when the run did what its mode asks for:  a plan that planned,  or a run that
    /// was done.  The helper's `ok`,  its result word and its exit code must all agree.
    func isSuccess(for mode: OpsMode) -> Bool {
        guard ok, exitCode == 0 else { return false }
        switch mode {
        case .plan: return kind == .planned
        case .yes: return kind == .done
        }
    }

    /// The commands,  one line each,  for the plan sheet.
    var commandLines: [String] {
        commands.map { $0.joined(separator: " ") }
    }

    /// What the sheet calls those commands once the action has ended.  It says what ran only
    /// when the helper said it ran:  a plan ran nothing,  a refusal ran nothing,  a failure
    /// stopped part way,  and a lost answer cannot say.
    var commandsHeading: String {
        if isPlan || kind == .planned { return "The Plan" }
        switch kind {
        case .refused: return "Not Run"
        case .done, .submitted: return runNotKnown ? "What Was Asked To Run" : "What Ran"
        case .failed: return "What Was Tried"
        case .unknown, .planned: return "What Was Asked To Run"
        }
    }

    /// True when the helper's own words add something under the result's title.  A bare
    /// "done" repeats the title,  so it is left out.
    var showsDetailUnderTitle: Bool {
        let plain = detail.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return !plain.isEmpty && plain != "done" && plain != "done."
    }

    /// How far a chain got before it stopped,  when the helper said.
    var progressNote: String? {
        guard kind == .failed, !isPlan, let finished = completedCommands,
              !commands.isEmpty, finished < commands.count else { return nil }
        let total = commands.count
        if total == 1 { return "The command ran and did not succeed." }
        if finished == 0 { return "The first command did not succeed, so none after it ran." }
        return "\(finished) of \(total) commands succeeded, the next one did not, and none after it ran."
    }

    /// The line at the top of a result:  what happened,  in the owner's words,  and above
    /// all whether anything ran.
    func resultTitle(for action: OpsAction) -> String {
        let gap = ServerStatusCopy.sentenceGap
        let who = action.runsOn == .mac ? "This Mac" : "The server"
        if isPlan {
            if kind == .refused { return "\(who) refused this, so nothing ran." }
            return "The plan could not be read, so nothing ran."
        }
        switch kind {
        case .done:
            return isSuccess(for: .yes) ? "Done." : "\(who) said done but did not confirm it worked."
        case .submitted:
            return "Sent.\(gap)Muster has not seen it finish yet."
        case .planned:
            return "The plan could not be confirmed, so nothing ran."
        case .refused:
            return "\(who) refused this, so nothing ran."
        case .failed:
            return "This did not work."
        case .unknown:
            guard runNotKnown else { return "The answer could not be read." }
            return "This may or may not have run \(action.runsOn.inProse).\(gap)Check its state before you run it again."
        }
    }

    /// Plain advice for a helper's fixed error code,  or nil when there is none to give.
    func advice(for action: OpsAction) -> String? {
        let gap = ServerStatusCopy.sentenceGap
        switch errorCode {
        case "update_busy":
            return "An update is already running on the server.\(gap)Check again in a minute."
        case "action_busy":
            return "Another action is still running \(action.runsOn.inProse).\(gap)Wait for it to end, then try again."
        case "action_lock_unavailable":
            return "The control script \(action.runsOn.inProse) could not take its one-action lock."
        case "authorization_unavailable":
            return "The server is not set up to let Muster start this update."
        case "update_route_changed":
            return "The way the server runs this update changed after the plan.\(gap)Plan it again."
        case "invocation_replaced":
            return "Another update run replaced this one before Muster could see it finish."
        default:
            return nil
        }
    }
}

/// Reads a helper's answer.
enum OpsResultParser {
    /// The answer,  from the whole of stdout or,  when the helper printed more than one
    /// line,  from the last line that is a JSON object.
    static func decode(_ stdout: String) -> OpsResult? {
        let decoder = JSONDecoder()
        let trimmed = stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        if let whole = try? decoder.decode(OpsResult.self, from: Data(trimmed.utf8)) {
            return whole
        }
        let lines = trimmed.split(whereSeparator: \.isNewline).reversed()
        for line in lines {
            let text = line.trimmingCharacters(in: .whitespaces)
            guard text.hasPrefix("{") else { continue }
            if let answer = try? decoder.decode(OpsResult.self, from: Data(text.utf8)) {
                return answer
            }
        }
        return nil
    }

    /// What a run came to.
    ///
    /// Every way of not knowing ends short of a success.  A plan changes nothing,  and neither
    /// does a read,  so one that timed out or is not JSON failed.  A run that timed out,  gave
    /// no answer,  reported work it did not stand behind or was answered by the wrong machine
    /// may have done its work,  so it reads as unknown,  and `refused` and `failed` stay for
    /// what is known to have ended that way.
    static func parse(
        _ result: OpsProcessResult,
        mode: OpsMode,
        action: OpsAction,
        timeout: TimeInterval
    ) -> OpsOutcome {
        let canHaveChanged = mode == .yes && !action.changesNothing
        if result.timedOut {
            return OpsOutcome(
                kind: canHaveChanged ? .unknown : .failed, ok: false, commands: [],
                detail: "The helper did not finish within \(Int(timeout)) seconds.",
                exitCode: result.exitCode,
                runNotKnown: canHaveChanged,
                isPlan: mode == .plan
            )
        }
        guard let answer = decode(result.stdout) else {
            let landed = runMayHaveLanded(result, action: action, mode: mode)
            return OpsOutcome(
                kind: landed ? .unknown : .failed, ok: false, commands: [],
                detail: unreadableDetail(result, action: action, mode: mode),
                exitCode: result.exitCode,
                runNotKnown: landed,
                isPlan: mode == .plan
            )
        }
        var kind = OpsResultKind(rawValue: answer.result ?? "") ?? .unknown
        var detail = ApplyCopy.normalize(answer.detail ?? "")
        // A plan changes nothing.  A plan that answers "done" is a helper that did something
        // it was not asked to,  and the app says so rather than calling it a plan.
        if mode == .plan && (kind == .done || kind == .submitted) {
            let reported = kind.rawValue
            kind = .failed
            let warning = "The helper answered \"\(reported)\" to a plan."
            detail = detail.isEmpty ? warning : ApplyCopy.join(warning, detail)
        }
        let invocationID = answer.invocationID.flatMap { value -> String? in
            OpsCommandBuilder.isInvocationID(value) ? value : nil
        }
        if mode == .yes && kind == .done {
            let completed: Bool
            switch action {
            case .serverUpdate:
                completed = answer.verified && answer.executionState == "completed" && invocationID != nil
            case .coolifyRedeploy:
                completed = answer.verified && answer.executionState == "completed"
            default:
                completed = answer.executionState == nil || answer.executionState == "completed"
            }
            if !answer.ok || result.exitCode != 0 {
                kind = canHaveChanged ? .unknown : .failed
                detail = unbackedDetail("done", answer: answer, exitCode: result.exitCode, helperWords: detail)
            } else if !completed {
                kind = .submitted
                detail = "The request was submitted; completion has not been verified."
            }
        }
        if kind == .submitted && (!answer.ok || result.exitCode != 0) {
            kind = canHaveChanged ? .unknown : .failed
            detail = unbackedDetail("submitted", answer: answer, exitCode: result.exitCode, helperWords: detail)
        }
        let isKnobAction: Bool
        if case .knobSet = action { isKnobAction = true }
        else if case .knobUnset = action { isKnobAction = true }
        else { isKnobAction = false }
        // The answer names the machine it came from,  when the helper is new enough to say.
        // The button named one machine and another one replied.  A plan that does is refused,
        // because nothing has run.  A run that does may have done its work on that machine,
        // so it does not count as done and the Mac cannot call it refused either.
        var answeredFrom: FleetHost?
        if !isKnobAction, answer.host != nil || answer.hostIsMalformed {
            answeredFrom = FleetHost(reportValue: answer.host)
            if answeredFrom != action.runsOn {
                kind = mode == .plan ? .refused : .unknown
                detail = wrongMachineDetail(answeredFrom, action: action, mode: mode)
            }
        }
        if detail.isEmpty {
            detail = defaultDetail(kind: kind, exitCode: result.exitCode)
        }
        var knobChange: OpsKnobChange?
        // Only a knob answer that got as far as naming a value carries a change.
        if isKnobAction, answer.new != nil || answer.oldSource != nil {
            var removes = false
            if case .knobUnset = action { removes = true }
            knobChange = OpsKnobChange(
                old: answer.old, oldSource: answer.oldSource, new: answer.new,
                removesValue: removes, lkg: answer.lkg
            )
        }
        // Log text is surfaced only for a logs run that ran:  a plan fetched
        // nothing,  and output on any other action's answer stays unread.
        var logText: String?
        var logLines: Int?
        if case .logs = action, mode == .yes, kind == .done {
            logText = answer.output.map(sanitizedForDisplay)
            logLines = answer.lines
        }
        return OpsOutcome(
            kind: kind,
            ok: answer.ok,
            commands: answer.commands,
            detail: detail,
            exitCode: result.exitCode,
            knobChange: knobChange,
            logText: logText,
            logLines: logLines,
            resourcePolicy: answer.resourcePolicy,
            invocationID: invocationID,
            executionState: answer.executionState,
            baselineStartMonotonic: answer.baselineStartMonotonic,
            answeredFrom: answeredFrom,
            runNotKnown: canHaveChanged && kind == .unknown,
            isPlan: mode == .plan,
            errorCode: answer.errorCode.flatMap { isErrorCode($0) ? $0 : nil },
            serverAuditID: mode == .yes ? answer.auditID.flatMap { isAuditID($0) ? $0 : nil } : nil,
            serverAuditTarget: mode == .yes ? answer.auditTarget.map(auditTargetText) : nil,
            completedCommands: answer.completedCommands.flatMap {
                (0...answer.commands.count).contains($0) ? $0 : nil
            }
        )
    }

    /// A helper error code is its own fixed shape:  lower case words joined by `_`.
    static func isErrorCode(_ value: String) -> Bool {
        (1...64).contains(value.count)
            && value.utf8.allSatisfy { (97...122).contains($0) || $0 == 95 }
    }

    /// A helper audit row id:  twelve lower case hex digits.
    static func isAuditID(_ value: String) -> Bool {
        value.count == 12 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    /// The target a helper's row names,  bounded and with no control characters,  for the
    /// Mac's own row.
    private static func auditTargetText(_ value: String) -> String {
        let plain = sanitizedForDisplay(value).replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\t", with: " ")
        return String(plain.prefix(120))
    }

    /// True when a run that gave no answer may still have done its work.
    ///
    /// Only what proves the helper never ran says otherwise:  ssh stopping before anything
    /// reached it,  or a shell that could not start it.  A traceback part way through,  a
    /// signal,  or an exit with nothing printed says nothing about how far it got.
    private static func runMayHaveLanded(_ result: OpsProcessResult, action: OpsAction, mode: OpsMode) -> Bool {
        guard mode == .yes, !action.changesNothing else { return false }
        if action.runsOnServer, result.exitCode == 255 {
            return sshStoppedEarly(result.stderr) == nil
        }
        return result.exitCode != 126 && result.exitCode != 127
    }

    /// The sentence for a helper that reports the work and does not stand behind the report,
    /// naming what disagrees with it,  and then the helper's own words.
    private static func unbackedDetail(_ word: String,
                                       answer: OpsResult,
                                       exitCode: Int32,
                                       helperWords: String) -> String {
        let sentence = answer.ok
            ? "The helper said \(word), but it exited with code \(exitCode)."
            : "The helper said \(word), but its answer was not ok (exit \(exitCode))."
        return helperWords.isEmpty ? sentence : ApplyCopy.join(sentence, helperWords)
    }

    /// What stopped ssh before any command was sent.
    private enum SSHStoppedEarly {
        case lookup, keyRefused, hostKey, hostKeyChanged, noConnection
    }

    /// Reads ssh's own diagnostics,  each by the shape of its whole line,  or nil when there
    /// is none.  A remote command's stderr reaches this same text and can say anything,  so
    /// a phrase somewhere in it is not proof that nothing was sent.
    ///
    /// ssh ends with the same line for a key it has never seen and for one that changed,
    /// and only the banner before it tells the two apart.
    private static func sshStoppedEarly(_ stderr: String) -> SSHStoppedEarly? {
        let lines = stderr.lowercased().split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        let changed = lines.contains { $0.contains("remote host identification has changed") }
        for text in lines {
            if text.hasPrefix("ssh: could not resolve hostname") { return .lookup }
            if text.hasPrefix("ssh: connect to host") { return .noConnection }
            if text.hasPrefix("host key verification failed") { return changed ? .hostKeyChanged : .hostKey }
            if text.contains(": permission denied ("), text.hasSuffix(").") { return .keyRefused }
        }
        return nil
    }

    /// The fixed sentence for an answer that did not come from the machine the button names.
    private static func wrongMachineDetail(_ found: FleetHost?, action: OpsAction, mode: OpsMode) -> String {
        let source = found.map { "The answer came from \($0.name)" } ?? "The answer named a machine this app does not know"
        let ending = mode == .plan ? "so nothing will run" : "so this cannot be counted as done"
        return "\(source), but this button names \(action.runsOn.name), \(ending)."
    }

    /// Journal text with escape and control sequences removed, for the screen and
    /// for copy and paste.  Newlines and tabs stay.
    static func sanitizedForDisplay(_ text: String) -> String {
        let scalars = Array(text.unicodeScalars)
        var kept = String.UnicodeScalarView()
        var index = 0
        while index < scalars.count {
            let scalar = scalars[index]
            if scalar.value == 0x1B {
                index += 1
                guard index < scalars.count else { break }
                if scalars[index] == "[" {
                    index += 1
                    while index < scalars.count, !(0x40...0x7E).contains(scalars[index].value) {
                        index += 1
                    }
                    index += 1
                } else if scalars[index] == "]" {
                    index += 1
                    while index < scalars.count,
                          scalars[index].value != 0x07, scalars[index].value != 0x1B {
                        index += 1
                    }
                    if index < scalars.count, scalars[index].value == 0x07 { index += 1 }
                } else {
                    index += 1
                }
                continue
            }
            if scalar.value == 0x0A || scalar.value == 0x09
                || !(scalar.value < 0x20 || (0x7F...0x9F).contains(scalar.value)) {
                kept.append(scalar)
            }
            index += 1
        }
        return String(kept)
    }

    private static func defaultDetail(kind: OpsResultKind, exitCode: Int32) -> String {
        switch kind {
        case .planned: return "Nothing has run yet."
        case .submitted: return "The request was submitted; completion has not been verified."
        case .done: return "Done."
        case .refused: return "The helper refused the action."
        case .failed: return "The helper said the action failed (exit \(exitCode))."
        case .unknown: return "The helper's answer has no result this app knows (exit \(exitCode))."
        }
    }

    /// Why a run produced no answer.
    ///
    /// ssh's own errors are put in fixed words,  because its messages can carry the
    /// server's address and that does not belong on the screen.
    private static func unreadableDetail(_ result: OpsProcessResult, action: OpsAction, mode: OpsMode) -> String {
        if action.runsOnServer && result.exitCode == 255 {
            let alias = OpsControlRoute.serverAlias
            switch sshStoppedEarly(result.stderr) {
            case .some(.lookup):
                return "The Mac could not look up the server.\(ServerStatusCopy.sentenceGap)Check the Mac's network, then the \(alias) alias in ~/.ssh/config."
            case .some(.keyRefused):
                return "The server refused the \(alias) key.\(ServerStatusCopy.sentenceGap)Install it on the server."
            case .some(.hostKey):
                return "This Mac has no record of the server's identity, so Muster sent nothing."
            case .some(.hostKeyChanged):
                return ApplyCopy.join(
                    "The server's identity does not match what this Mac has on record, so Muster sent nothing.",
                    "This can mean the server was rebuilt or that something is in the way.",
                    "Do not accept a new key until someone has confirmed which."
                )
            case .some(.noConnection):
                return "The server did not answer, so nothing ran."
            case .none:
                // A plan changes nothing either way,  and a read that lost the server only
                // brought nothing back.  A run that lost the server cannot say how far it got.
                if mode == .plan { return "The server did not answer, so nothing ran." }
                return action.changesNothing
                    ? "The server did not answer, so nothing came back."
                    : "The server did not answer, so the Mac cannot say whether this ran."
            }
        }
        let tail = ApplyCopy.normalize(result.stderrTail)
        if tail.isEmpty {
            return "The helper printed no answer (exit \(result.exitCode))."
        }
        return ApplyCopy.join("The helper printed no answer (exit \(result.exitCode)).", tail)
    }
}

/// Observes one captured update invocation without starting another update.
struct OpsUpdateReceiptReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read(name: String, invocationID: String, baselineStartMonotonic: UInt64) async -> OpsOutcome {
        func unverified(_ detail: String, expired: Bool = false) -> OpsOutcome {
            OpsOutcome(kind: .submitted, ok: false, commands: [], detail: detail, exitCode: 0,
                       invocationID: OpsCommandBuilder.isInvocationID(invocationID) ? invocationID : nil,
                       executionState: expired ? "expired" : "submitted",
                       baselineStartMonotonic: baselineStartMonotonic)
        }
        do {
            let invocation = try makeBuilder().updateStatusInvocation(name: name, invocationID: invocationID, baselineStartMonotonic: baselineStartMonotonic)
            let process = try await runner.run(invocation)
            guard !process.timedOut, let answer = OpsResultParser.decode(process.stdout),
                  answer.invocationID == invocationID,
                  answer.baselineStartMonotonic == baselineStartMonotonic else {
                return unverified("The recorded service invocation could not be read; completion remains unverified.")
            }
            if answer.executionState == "expired" {
                return unverified("The recorded service invocation is no longer current; its completion cannot be verified by rechecking.", expired: true)
            }
            // The transport uses --plan and cannot mutate; these result words describe
            // the recorded invocation, not an update executed by this read.
            let outcome = OpsResultParser.parse(process, mode: .yes, action: .serverUpdate(name: name), timeout: invocation.timeout)
            if outcome.isSuccess(for: .yes) { return outcome }
            if outcome.kind == .failed, answer.verified, answer.executionState == "failed" { return outcome }
            return unverified("The recorded update has no verified terminal result yet.\(ServerStatusCopy.sentenceGap)Recheck observes the same invocation and never starts another update.")
        } catch {
            return unverified("The receipt could not be checked; completion remains unverified.\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)")
        }
    }
}

/// Reads one direct policy receipt over the existing fixed,  read-only ssh route.
///
/// This deliberately bypasses the confirmation sheet because `resource-status --plan`
/// cannot change state.  Resource writes still go through `OpsController.plan`,  review,
/// Touch ID and the controller's fresh-plan comparison.
struct OpsResourcePolicyReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read(now: Date? = nil) async -> ResourcePolicyReading {
        let action = OpsAction.resourceStatus
        do {
            let invocation = try makeBuilder().invocation(for: action, mode: .plan)
            let process = try await runner.run(invocation)
            let outcome = OpsResultParser.parse(process, mode: .plan, action: action, timeout: invocation.timeout)
            guard outcome.isSuccess(for: .plan) else { return .unavailable(outcome.detail) }
            guard let snapshot = outcome.resourcePolicy else {
                return .unavailable("The server answered without a readable resource policy, so neither the saved values nor the controller's receipt could be shown.")
            }
            let data = try JSONEncoder().encode(snapshot)
            let receivedAt = now ?? Date()
            return ResourcePolicyReading.decode(data, now: receivedAt, directReceiptAt: receivedAt)
        } catch {
            return .unavailable(error.localizedDescription)
        }
    }
}

// MARK: - The allowlist

/// The allowlist,  as this Mac's checkout carries it.
///
/// The helpers check their own copy,  so this read only decides which buttons the app
/// shows:  a verb the allowlist does not name gets no button.
struct OpsAllowlist: Equatable {
    struct Entry: Equatable {
        let name: String
        let actions: [String]
    }

    struct Update: Equatable, Identifiable {
        let name: String
        let unit: String
        let scope: String

        var id: String { name }
    }

    var macAgents: [Entry] = []
    var pm2Apps: [Entry] = []
    var serverUnits: [Entry] = []
    var updates: [Update] = []
    var coolifyRedeploy: Bool = false

    /// The verbs the allowlist names for a LaunchAgent,  in the app's order.
    func verbs(forMacAgent label: String) -> [OpsVerb] {
        verbs(in: macAgents, named: label)
    }

    /// The verbs the allowlist names for a pm2 application,  in the app's order.
    func verbs(forPM2App name: String) -> [OpsVerb] {
        verbs(in: pm2Apps, named: name)
    }

    /// The verbs the allowlist names for a unit,  in the app's order.
    func verbs(forServerUnit unit: String) -> [OpsVerb] {
        verbs(in: serverUnits, named: unit)
    }

    private func verbs(in entries: [Entry], named name: String) -> [OpsVerb] {
        guard let entry = entries.first(where: { $0.name == name }) else { return [] }
        return OpsVerb.allCases.filter { entry.actions.contains($0.rawValue) }
    }

    /// Where the allowlist is in a checkout.
    static func url(repoRoot: URL) -> URL {
        repoRoot.appendingPathComponent(OpsControlRoute.allowlistPath)
    }

    /// Read the allowlist,  or nil when it is not there or cannot be read.
    static func read(at url: URL) -> OpsAllowlist? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return parse(data)
    }

    /// Parse the allowlist.  A version this app does not know is not read:  the buttons
    /// it would draw could name verbs that mean something else.
    static func parse(_ data: Data) -> OpsAllowlist? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            (object["schemaVersion"] as? NSNumber)?.intValue == 1
        else { return nil }
        var list = OpsAllowlist()
        let mac = object["mac"] as? [String: Any]
        let server = object["server"] as? [String: Any]
        for item in (mac?["agents"] as? [[String: Any]]) ?? [] {
            guard let label = item["label"] as? String else { continue }
            list.macAgents.append(Entry(name: label, actions: (item["actions"] as? [String]) ?? []))
        }
        for item in (mac?["pm2Apps"] as? [[String: Any]]) ?? [] {
            guard let name = item["name"] as? String else { continue }
            list.pm2Apps.append(Entry(name: name, actions: (item["actions"] as? [String]) ?? []))
        }
        for item in (server?["units"] as? [[String: Any]]) ?? [] {
            guard let unit = item["unit"] as? String else { continue }
            list.serverUnits.append(Entry(name: unit, actions: (item["actions"] as? [String]) ?? []))
        }
        for item in (server?["updates"] as? [[String: Any]]) ?? [] {
            guard let name = item["name"] as? String else { continue }
            list.updates.append(Update(
                name: name,
                unit: (item["unit"] as? String) ?? "",
                scope: (item["scope"] as? String) ?? "system"
            ))
        }
        let coolify = server?["coolify"] as? [String: Any]
        list.coolifyRedeploy = (coolify?["redeploy"] as? Bool) == true
        return list
    }
}

// MARK: - The controller

/// The owner's answer to the approval question,  as the view hands it over.
enum OpsApprovalAnswer: Equatable {
    /// Touch ID or the login password said yes.
    case approved
    /// The owner canceled,  or the Mac did not accept the approval.  The plan stays on
    /// screen and can be approved again.
    case notApproved(String)
    /// The Mac cannot ask at all,  so nothing can run from this sheet.
    case unavailable(String)
}

/// One approval question,  tied to the exact plan that was on screen when it was asked.
struct OpsApprovalRequest: Equatable {
    let action: OpsAction
    let reviewed: OpsOutcome
}

/// Where a run is,  for the words and the clock on the sheet.
enum OpsRunStage: Equatable {
    /// Reading the plan again after the approval.  Nothing has run.
    case checkingPlan
    /// The run was sent,  and Muster is waiting for its answer.
    case running

    /// What the sheet says while the run is at this stage.
    func sentence(for action: OpsAction, elapsed: TimeInterval) -> String {
        let gap = ServerStatusCopy.sentenceGap
        let place = action.runsOn.inProse
        let clock = OpsController.durationText(elapsed)
        switch self {
        case .checkingPlan:
            return "Checking the plan again \(place), \(clock) so far.\(gap)Nothing has run yet."
        case .running:
            let wait = OpsController.durationText(OpsControlRoute.runWait(for: action))
            return ApplyCopy.join(
                "Running \(place), \(clock) so far.",
                "Muster waits up to \(wait) for the answer.",
                "Closing this sheet does not stop the run, and the sheet comes back when it ends."
            )
        }
    }
}

/// The app-lifetime owner of the one action in flight.
///
/// Views render what is published here,  so closing a sheet never cancels a run.  The
/// approval question belongs to the view,  and its answer comes back through
/// `finishApproval`,  tied to the plan it was asked about:  a run starts only for the exact
/// plan the owner approved.
@MainActor
final class OpsController: ObservableObject {
    static let shared = OpsController()

    enum Phase: Equatable {
        case idle
        case planning(OpsAction)
        case review(OpsAction, OpsOutcome)
        case running(OpsAction, OpsOutcome)
        case finished(OpsAction, OpsOutcome)
        case failed(OpsAction, String)

        var action: OpsAction? {
            switch self {
            case .idle: return nil
            case .planning(let action): return action
            case .review(let action, _): return action
            case .running(let action, _): return action
            case .finished(let action, _): return action
            case .failed(let action, _): return action
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    /// A sentence about the last refusal,  shown in the sheet.
    @Published private(set) var notice: String?
    /// Counts every `--yes` run that ended,  and every recheck that settled one,  so a view
    /// can read its data again.
    @Published private(set) var completedRuns = 0
    @Published private(set) var isRechecking = false
    /// True while the approval question is up,  so every window holds its buttons.
    @Published private(set) var isAsking = false
    /// True when the owner closed the sheet while a plan or a run was still going.  A run
    /// goes on,  and the sheet comes back when it ends.
    @Published private(set) var isSheetHidden = false
    /// Where the run in flight is,  and since when:  the sheet's run,  or a start Muster
    /// takes on its own.
    @Published private(set) var runStage: OpsRunStage?
    @Published private(set) var stageStartedAt: Date?
    /// True while Muster runs a start of its own (`runUnattended`),  with nobody at a sheet.
    @Published private(set) var isRunningUnattended = false

    private let runner: OpsRunning
    private let makeBuilder: () -> OpsCommandBuilder
    private let auditURL: URL?
    private let checksInstall: Bool
    private let now: () -> Date
    private var task: Task<Void, Never>?
    /// True when the owner closed the sheet while a plan was being read:  its answer is
    /// dropped,  because a plan changes nothing.
    private var abandonPlan = false
    /// The last action whose run ended with no answer,  and when,  so a new plan for it
    /// says so before it is approved again.
    private var unresolved: (action: OpsAction, at: Date)?
    /// The start Muster is running on its own,  while `runUnattended` has it.
    private var unattendedAction: OpsAction?

    /// How long a run with no answer is remembered when the same action is planned again.
    static let unresolvedMemory: TimeInterval = 60 * 60

    init(
        runner: OpsRunning = OpsProcessRunner(),
        makeBuilder: @escaping () -> OpsCommandBuilder = {
            OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
        },
        auditURL: URL? = OpsAuditLog.logURL(),
        checksInstall: Bool = true,
        now: @escaping () -> Date = { Date() }
    ) {
        self.runner = runner
        self.makeBuilder = makeBuilder
        self.auditURL = auditURL
        self.checksInstall = checksInstall
        self.now = now
    }

    /// True while a helper is running.
    var isBusy: Bool {
        if isRechecking || isRunningUnattended { return true }
        switch phase {
        case .planning, .running: return true
        default: return false
        }
    }

    /// True while there is an action in hand,  shown or not.
    var isPresenting: Bool { phase != .idle }

    /// True while a new plan would be refused:  an action is in hand,  or Muster is running
    /// a start of its own.  The buttons that ask for a plan hold while this is true.
    var refusesNewPlan: Bool { isPresenting || isRunningUnattended }

    /// True while the sheet should be on screen.
    var isSheetShown: Bool { phase != .idle && !isSheetHidden }

    /// The run going on behind a closed sheet,  for the strip that brings it back.
    var hiddenRun: OpsAction? {
        guard isSheetHidden, case .running(let action, _) = phase else { return nil }
        return action
    }

    /// The question to ask before Muster quits,  or nil when quitting loses nothing.  Only a
    /// run that was sent can be lost:  a plan,  even the one read again after the approval,
    /// changes nothing.  A start Muster sent on its own counts too.
    var quitWarning: String? {
        guard runStage == .running else { return nil }
        let action: OpsAction
        if case .running(let shown, _) = phase {
            action = shown
        } else if let own = unattendedAction {
            action = own
        } else {
            return nil
        }
        // Only a server action gets a `started` row in this Mac's log:  a local action's own
        // helper writes its row when it finishes.
        let logs = action.runsOnServer
            ? "this Mac's log keeps only that it started."
            : "this Mac's log has no row for it until it ends."
        return ApplyCopy.join(
            "\(action.title) is still running.",
            "If Muster quits now, it cannot learn how it ended, and \(logs)"
        )
    }

    var canRecheckUpdate: Bool {
        guard case .finished(.serverUpdate(let name), let outcome) = phase else { return false }
        return name == "fleet-cli" && outcome.kind == .submitted && outcome.invocationID != nil
            && outcome.baselineStartMonotonic != nil && outcome.executionState != "expired"
    }

    /// A receipt read has no approval or write path and may be closed while pending.
    ///
    /// A recheck that settles the update adds a row to this Mac's log that names the same
    /// service invocation,  so the submitted row does not stay the last word.
    func recheckSubmittedUpdate() {
        guard canRecheckUpdate, task == nil,
              case .finished(let action, let previous) = phase,
              case .serverUpdate(let name) = action, let invocationID = previous.invocationID,
              let baseline = previous.baselineStartMonotonic else { return }
        isRechecking = true
        notice = nil
        let reader = OpsUpdateReceiptReader(runner: runner, makeBuilder: makeBuilder)
        task = Task { [weak self] in
            guard !Task.isCancelled else { return }
            let outcome = await reader.read(name: name, invocationID: invocationID, baselineStartMonotonic: baseline)
            guard !Task.isCancelled, let self else { return }
            self.phase = .finished(action, outcome)
            self.isRechecking = false
            self.task = nil
            if outcome.kind != .submitted {
                self.record(action, result: outcome.kind.rawValue, runID: OpsController.makeRunID(),
                            outcome: outcome, extra: ["recheck": "true"])
                self.completedRuns += 1
            }
        }
    }

    /// Plan an action and show the plan.
    ///
    /// One action at a time:  a plan asked for while another action is planned,  waiting
    /// for review or running is refused,  and the one already there is left alone and shown.
    @discardableResult
    func plan(_ action: OpsAction) -> Bool {
        switch phase {
        case .idle, .finished, .failed:
            break
        case .planning, .review, .running:
            notice = OpsControlError.busy.errorDescription
            if !abandonPlan { isSheetHidden = false }
            return false
        }
        // No sheet is up to show a refusal here,  so none is noted:  it would only surface
        // later,  under some other plan.  The buttons hold on `refusesNewPlan` instead.
        guard !isRunningUnattended else { return false }
        guard task == nil else { return false }
        notice = nil
        abandonPlan = false
        isSheetHidden = false
        phase = .planning(action)
        task = Task { [weak self] in
            await self?.runPlan(action)
        }
        return true
    }

    /// Start the approval question for the plan on screen.  Every window holds its buttons
    /// until `finishApproval` is called with the answer.
    func beginApproval() -> OpsApprovalRequest? {
        guard case .review(let action, let reviewed) = phase, task == nil, !isAsking else { return nil }
        isAsking = true
        notice = nil
        return OpsApprovalRequest(action: action, reviewed: reviewed)
    }

    /// The owner's answer to `beginApproval`.  A yes runs only when the plan on screen is
    /// still the one the question was asked about.  A cancel leaves the plan up with a
    /// plain note,  and a Mac that cannot ask ends the sheet.
    func finishApproval(_ request: OpsApprovalRequest, answer: OpsApprovalAnswer) {
        isAsking = false
        guard case .review(let action, let reviewed) = phase,
              action == request.action, reviewed == request.reviewed else {
            if case .review = phase {
                notice = ApplyCopy.join(
                    "The plan on screen changed while you were approving, so nothing ran.",
                    "Review it, then approve it again."
                )
            }
            return
        }
        switch answer {
        case .approved:
            confirm(action: action, reviewed: reviewed)
        case .notApproved(let reason):
            notice = reason
        case .unavailable(let reason):
            decline(reason)
        }
    }

    /// Run the plan the owner approved.  It runs only when that plan is the one on screen:
    /// an approval asked about one plan never runs another.
    @discardableResult
    func confirm(action: OpsAction, reviewed: OpsOutcome) -> Bool {
        guard case .review(let shown, let shownPlan) = phase, shown == action, shownPlan == reviewed,
              task == nil else { return false }
        notice = nil
        phase = .running(action, reviewed)
        enterStage(.checkingPlan)
        task = Task { [weak self] in
            await self?.runConfirmed(action, reviewed: reviewed)
        }
        return true
    }

    /// Runs one start that Muster takes on its own (`ServiceReconciler`),  through the same
    /// helper,  the same plan,  the same `--yes` run and the same bookkeeping as a start the
    /// owner approved (one run id,  the same stages,  and the same rows),  but with no sheet
    /// and no Touch ID,  because nobody is there to answer.
    ///
    /// Only a start of a pm2 application or a LaunchAgent is accepted here,  and the helper
    /// still checks its own allowlist.  The plan is read first and must succeed.  Nil when
    /// another action is in the way or the action is not a start,  and then nothing ran.
    func runUnattended(_ action: OpsAction) async -> OpsOutcome? {
        switch action {
        case .pm2App(let verb, _), .macAgent(let verb, _):
            guard verb == .start else { return nil }
        case .serverUnit, .serverUpdate, .coolifyRedeploy, .knobSet, .knobUnset, .logs,
             .serverResource, .resourceStatus:
            return nil
        }
        guard !isBusy, !isPresenting, task == nil else { return nil }
        isRunningUnattended = true
        unattendedAction = action
        defer {
            isRunningUnattended = false
            unattendedAction = nil
            runStage = nil
            stageStartedAt = nil
        }
        // True once the `--yes` helper may have started:  only then can anything have run.
        var started = false
        let runID = Self.makeRunID()
        do {
            enterStage(.checkingPlan)
            let planned = try await execute(action, mode: .plan)
            guard planned.isSuccess(for: .plan) else { return planned }
            let invocation = try prepare(action, mode: .yes)
            enterStage(.running)
            record(action, result: "started", runID: runID)
            started = true
            let outcome = try await launch(invocation, action: action, mode: .yes)
            record(action, result: outcome.kind.rawValue, runID: runID, outcome: outcome)
            remember(action, outcome)
            completedRuns += 1
            return outcome
        } catch {
            // A helper that could not be started ran nothing,  and its row says so.
            if let control = error as? OpsControlError, case .launchFailed = control, started {
                record(action, result: "not-run", runID: runID)
                started = false
            }
            guard started else {
                return OpsOutcome(kind: .failed, ok: false, commands: [],
                                  detail: error.localizedDescription, exitCode: -1)
            }
            // The helper ran,  and what it answered could not be read,  so the Mac cannot say
            // how far the start got.
            let outcome = OpsOutcome(kind: .unknown, ok: false, commands: [],
                                     detail: error.localizedDescription, exitCode: -1,
                                     runNotKnown: true)
            record(action, result: outcome.kind.rawValue, runID: runID)
            remember(action, outcome)
            completedRuns += 1
            return outcome
        }
    }

    /// The Mac cannot ask for the approval:  nothing runs.
    func decline(_ reason: String) {
        guard case .review(let action, _) = phase else { return }
        isAsking = false
        phase = .failed(action, reason)
    }

    /// Bring a closed sheet back.
    func showSheet() {
        guard phase != .idle, !abandonPlan else { return }
        isSheetHidden = false
    }

    /// Close the sheet.  A run in flight is never cut short:  closing it while it runs only
    /// hides it,  and it comes back with its result.  A plan being read is given up,  since
    /// it changes nothing.
    func dismiss() {
        if isRechecking {
            task?.cancel()
            task = nil
            isRechecking = false
            phase = .idle
            notice = nil
            return
        }
        guard !isAsking else { return }
        switch phase {
        case .planning:
            abandonPlan = true
            isSheetHidden = true
        case .running:
            isSheetHidden = true
            // A refusal noted while the run was in view is old by the time the result comes
            // back, so it is not shown under that result.
            notice = nil
        case .idle, .review, .finished, .failed:
            phase = .idle
            notice = nil
            isSheetHidden = false
        }
    }

    private func runPlan(_ action: OpsAction) async {
        defer { task = nil }
        let outcome: OpsOutcome
        do {
            outcome = try await execute(action, mode: .plan)
        } catch {
            if abandonPlan { return endAbandonedPlan() }
            phase = .failed(action, error.localizedDescription)
            return
        }
        if abandonPlan { return endAbandonedPlan() }
        if outcome.isSuccess(for: .plan) {
            phase = .review(action, outcome)
            notice = unresolvedWarning(for: action)
        } else {
            phase = .finished(action, outcome)
        }
    }

    private func endAbandonedPlan() {
        abandonPlan = false
        isSheetHidden = false
        notice = nil
        phase = .idle
    }

    private func runConfirmed(_ action: OpsAction, reviewed: OpsOutcome) async {
        defer {
            task = nil
            runStage = nil
            stageStartedAt = nil
            isSheetHidden = false
        }
        // True once the `--yes` helper may have started:  only then can anything have run.
        var started = false
        let runID = Self.makeRunID()
        do {
            // The plan is read again after the owner said yes.  A second plan that was
            // refused or failed is shown with its own words,  and a plan that changed in
            // between is shown again instead of being run.
            let fresh: OpsOutcome
            do {
                fresh = try await execute(action, mode: .plan)
            } catch {
                phase = .failed(action, error.localizedDescription)
                notice = "Nothing ran:\(ServerStatusCopy.sentenceGap)the plan could not be read again after the approval."
                return
            }
            guard fresh.isSuccess(for: .plan) else {
                phase = .finished(action, fresh)
                notice = "Nothing ran:\(ServerStatusCopy.sentenceGap)the plan read again after the approval did not succeed."
                return
            }
            guard
                fresh.commands == reviewed.commands,
                fresh.knobChange?.old == reviewed.knobChange?.old,
                fresh.knobChange?.oldSource == reviewed.knobChange?.oldSource
            else {
                phase = .review(action, fresh)
                notice = ApplyCopy.join(
                    "The plan changed after you reviewed it, so nothing ran.",
                    "Review the new plan before running it."
                )
                return
            }
            let invocation = try prepare(action, mode: .yes)
            enterStage(.running)
            // The row goes in before the run,  so a Muster that quits or crashes part way
            // still leaves a trace that something was sent.
            record(action, result: "started", runID: runID)
            started = true
            var outcome = try await launch(invocation, action: action, mode: .yes)
            // The helper plans again itself before it runs,  so what it reports running can
            // differ from what the owner approved.  That is shown and never hidden.
            if !outcome.commands.isEmpty, outcome.commands != reviewed.commands {
                outcome.differsFromReview = true
            }
            phase = .finished(action, outcome)
            record(action, result: outcome.kind.rawValue, runID: runID, outcome: outcome)
            remember(action, outcome)
        } catch {
            // A helper that could not be started ran nothing,  and its row says so.
            if let control = error as? OpsControlError, case .launchFailed = control, started {
                record(action, result: "not-run", runID: runID)
                started = false
            }
            if started, !action.changesNothing {
                // The helper ran,  and what it answered could not be read,  so the Mac cannot
                // say how far it got.
                let outcome = OpsOutcome(kind: .unknown, ok: false, commands: [],
                                         detail: error.localizedDescription, exitCode: -1,
                                         runNotKnown: true)
                phase = .finished(action, outcome)
                record(action, result: outcome.kind.rawValue, runID: runID)
                remember(action, outcome)
            } else {
                phase = .failed(action, error.localizedDescription)
                if started { record(action, result: OpsResultKind.failed.rawValue, runID: runID) }
            }
        }
        if started { completedRuns += 1 }
    }

    private func enterStage(_ stage: OpsRunStage) {
        runStage = stage
        stageStartedAt = now()
    }

    /// Keeps the last run that ended with no answer,  and forgets it once the same action
    /// ends with one.
    private func remember(_ action: OpsAction, _ outcome: OpsOutcome) {
        if outcome.runNotKnown {
            unresolved = (action, now())
        } else if unresolved?.action == action {
            unresolved = nil
        }
    }

    /// The warning on a new plan for an action whose last run ended with no answer.
    private func unresolvedWarning(for action: OpsAction) -> String? {
        guard let last = unresolved, last.action == action,
              now().timeIntervalSince(last.at) < Self.unresolvedMemory else { return nil }
        return ApplyCopy.join(
            "The last run of this action ended with no answer, so it may already have run.",
            "Check its state before you approve it again."
        )
    }

    private func execute(_ action: OpsAction, mode: OpsMode) async throws -> OpsOutcome {
        let invocation = try prepare(action, mode: mode)
        return try await launch(invocation, action: action, mode: mode)
    }

    /// Check the helper is there and build its invocation.  Nothing is started.
    private func prepare(_ action: OpsAction, mode: OpsMode) throws -> OpsInvocation {
        let builder = makeBuilder()
        if checksInstall {
            try builder.checkInstalled(for: action)
        }
        return try builder.invocation(for: action, mode: mode)
    }

    /// Start the helper and read its answer.
    private func launch(_ invocation: OpsInvocation, action: OpsAction, mode: OpsMode) async throws -> OpsOutcome {
        let result = try await runner.run(invocation)
        return OpsResultParser.parse(result, mode: mode, action: action, timeout: invocation.timeout)
    }

    /// Add a row to the Mac's audit log for a server action.
    ///
    /// A local action's helper writes its own row,  so only a server action gets one here.
    /// One run writes a `started` row before it is sent and a closing row after,  both with
    /// the same `runID`.  The closing row names the server's own row (`serverAuditID`) and
    /// its target when the server said,  so the two logs can be matched.  A row that cannot
    /// be written does not change the result:  the server's own log has the action.
    private func record(_ action: OpsAction, result: String, runID: String,
                        outcome: OpsOutcome? = nil, extra: [String: String] = [:]) {
        guard action.runsOnServer, let url = auditURL else { return }
        var row = OpsAuditLog.entry(for: action, result: result, now: now())
        row["runID"] = runID
        if let outcome {
            if let id = outcome.serverAuditID { row["serverAuditID"] = id }
            if let target = outcome.serverAuditTarget { row["serverTarget"] = target }
            if let id = outcome.invocationID { row["invocationID"] = id }
            if let baseline = outcome.baselineStartMonotonic { row["baselineStartMonotonic"] = String(baseline) }
        }
        for (key, value) in extra where row[key] == nil { row[key] = value }
        try? OpsAuditLog.append(row, to: url)
    }

    /// A short id that ties a run's rows together.
    static func makeRunID() -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(12))
    }

    /// A length of time in words:  "45 seconds",  "3 minutes",  "1 minute 5 seconds".
    nonisolated static func durationText(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds.rounded(.down)))
        func unit(_ count: Int, _ word: String) -> String { count == 1 ? "1 \(word)" : "\(count) \(word)s" }
        if total < 60 { return unit(total, "second") }
        let minutes = unit(total / 60, "minute")
        return total % 60 == 0 ? minutes : "\(minutes) \(unit(total % 60, "second"))"
    }
}
