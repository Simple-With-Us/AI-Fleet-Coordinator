import Foundation

/// The Operator's own options.
///
/// The Operator is the fleet's service bot:  the eavesdropper posts its digest, its notices and
/// its typing through the Operator's own Zulip identity, and builds that client on first use from
/// Fleet-OPS `/zulip_operator` (`docs/protocols/simple-tools-listener.md`).  The Mac needs no
/// `[seat.OPERATOR]` section for that, so a file without one is not a fault.
///
/// The app reads no credentials,  so it cannot say whether the Operator's key exists.  What
/// it shows is the identity the Operator must have,  where each name lives,  and whether a
/// section the file does hold keeps the rules the runtime enforces.
struct OperatorOptions: Equatable {
    /// One name the Operator needs.
    struct Name: Equatable, Identifiable {
        /// The name itself.
        let name: String
        /// Where it lives.
        let location: String
        /// What it is for.
        let purpose: String

        var id: String { name }
    }

    /// The seat tag the service seat uses.
    static let seatTag = "OPERATOR"

    /// The bot name the runtime requires.
    static let requiredBot = "Operator"
    /// Where the Operator's credentials come from.  The runtime allows no other source.
    static let requiredCreds = "infisical"
    /// What the Operator must be:  it holds no agent,  so it Captures Only and has no routine
    /// table to wake one.
    static let requiredKind = "Captures Only, with no routine table"

    /// The names the Operator needs,  and where each one lives.
    static let names: [Name] = [
        Name(
            name: "ZULIP_OPERATOR_EMAIL",
            location: "Infisical  Fleet-OPS /zulip_operator",
            purpose: "the address the Operator posts from"
        ),
        Name(
            name: "ZULIP_OPERATOR_API_KEY",
            location: "Infisical  Fleet-OPS /zulip_operator",
            purpose: "the key the Operator authenticates with"
        ),
        Name(
            name: "OPERATOR_ZULIP_USER_ID",
            location: "Infisical  Fleet-OPS /zulip_operator via fleet-secrets",
            purpose: "the pinned bot check"
        ),
        Name(
            name: "OPERATOR_ZULIP_EMAIL",
            location: "Infisical  Fleet-OPS /zulip_operator via fleet-secrets",
            purpose: "the pinned bot check"
        ),
    ]

    /// Whether the configuration file has a section for the Operator.
    var isConfigured: Bool = false
    /// The bot name the configuration file gives it.
    var bot: String?
    /// Where the Operator's credentials come from.
    var creds: String?
    /// Whether the configuration file gives it a routine table, which would make it wake.
    var hasRoutine: Bool = false
    /// The rules the configuration file breaks.
    var brokenRules: [String] = []
    /// The one thing that is wrong with a section the file holds,  or nil when nothing is.
    var problem: String?
    /// Why the configuration file was not read,  when it was not.  Then nothing is known about
    /// its Operator section,  and the file is never said to have none.
    var notRead: String?
    /// The one decider every platform shares, for when a seat's routine cannot pick a chat.
    var decider: OperatorDecider = .notSet

    /// Whether the configuration file gives the Operator the identity the runtime requires.
    ///
    /// This says nothing about the credentials:  the app reads none,  and says so.
    var hasRequiredIdentity: Bool {
        isConfigured && brokenRules.isEmpty
    }

    /// Whether the file was read,  so what it holds for the Operator is known.
    var isKnown: Bool {
        notRead == nil
    }

    /// The Operator's state, in one line.
    var summary: String {
        if notRead != nil {
            return "The Mac's configuration file was not read, so whether it holds a [seat.OPERATOR] section is not known.\u{00A0} The Mac needs none:  the eavesdropper builds the Operator's client from Fleet-OPS /zulip_operator on first use."
        }
        if !isConfigured {
            return "The Mac's file has no [seat.OPERATOR] section, and the Mac needs none:  the eavesdropper builds the Operator's client from Fleet-OPS /zulip_operator on first use."
        }
        if brokenRules.isEmpty {
            return "The Mac's file holds a [seat.OPERATOR] section, and it keeps the rules the runtime enforces (bot \(Self.requiredBot), creds \(Self.requiredCreds), \(Self.requiredKind))."
        }
        return "The Mac's file holds a [seat.OPERATOR] section that breaks the rules the runtime enforces:  " + brokenRules.joined(separator: ", ") + "."
    }

    /// The options the configuration file gives the Operator.
    ///
    /// `configProblem` is why a file that is there could not be read:  an empty document then
    /// stands for a file nobody read, not for a file with no section.  A file that is not there
    /// is known to hold no section, so the caller passes nothing for it.  The eavesdropper
    /// upper-cases a seat section's name, so `[seat.operator]` is the Operator's section too.
    static func build(from config: ListenerConfigDocument, configProblem: String? = nil) -> OperatorOptions {
        var options = identity(from: config, configProblem: configProblem)
        options.decider = OperatorDecider.read(from: config, configProblem: configProblem)
        return options
    }

    /// The identity the configuration file gives the Operator's own section.
    private static func identity(from config: ListenerConfigDocument, configProblem: String?) -> OperatorOptions {
        if let configProblem {
            return OperatorOptions(
                isConfigured: false,
                bot: nil,
                creds: nil,
                hasRoutine: false,
                brokenRules: [],
                problem: nil,
                notRead: configProblem
            )
        }
        let found = config.sections.first {
            $0.name.hasPrefix("seat.") && $0.name.dropFirst("seat.".count).uppercased() == seatTag
        }
        guard let section = found?.name else {
            return OperatorOptions(
                isConfigured: false,
                bot: nil,
                creds: nil,
                hasRoutine: false,
                brokenRules: [],
                problem: nil
            )
        }
        let bot = config.value(section: section, key: "bot")?.scalarText
        let creds = config.value(section: section, key: "creds")?.scalarText
        // The section's own name, so a lower-case `[seat.operator]` finds its own routine.
        let tag = String(section.dropFirst("seat.".count))
        // Any routine line, a table or not, is one the Operator must not have.
        let hasRoutine = config.routineForm(seat: tag) != .absent
        var broken: [String] = []
        if bot != requiredBot {
            broken.append("bot must be \(requiredBot)")
        }
        if creds != requiredCreds {
            broken.append("creds must be \(requiredCreds)")
        }
        if hasRoutine {
            broken.append("the Operator must capture only, with no routine table")
        }
        return OperatorOptions(
            isConfigured: true,
            bot: bot,
            creds: creds,
            hasRoutine: hasRoutine,
            brokenRules: broken,
            problem: broken.isEmpty ? nil : "The configuration file breaks the rules the runtime enforces."
        )
    }
}

/// The decider:  the one model every platform shares, for when a seat's routine cannot pick a
/// chat by letter, topic words or recency.  The eavesdropper reads it from `listener.toml`'s
/// `[decider.consult_backup]` table, or, while a file still holds only the old location, from the
/// leftover `[muse_bridge.consult_backup]` of the deleted Muse inbox bridge.  Muster shows its
/// runtime and its model slug, read only, and never a key:  its OpenRouter key lives in the
/// eavesdropper's environment, and Muster reads no credential.
enum OperatorDecider: Equatable {
    /// The configuration file was not read, so the decider is not known.  The text says why.
    case notKnown(String)
    /// The file holds no decider table.
    case notSet
    /// The decider the file names.  Either part may be missing from the table.
    case set(runtime: String?, model: String?)

    /// The runtime the decider is expected to have.
    static let expectedRuntime = "openrouter"

    /// What the decider line means, for its help text.
    static let help = "One decider serves every platform.\u{00A0} It runs only when a seat's routine can't pick the chat by letter, topic words or recency.\u{00A0} Changing it is a deliberate edit of listener.toml, because its slug has to match expected_models."

    /// Where the table lives, and the names its keys may take.  The block may be renamed, so each
    /// name is listed once here, and a rename is a change to one line.  The first section that holds
    /// the table wins, so `[decider]` wins over the leftover `[muse_bridge]`, as it does in the listener.
    static let sectionNames = ["decider", "muse_bridge"]
    static let tableNames = ["consult_backup"]
    static let runtimeKeys = ["runtime", "provider"]
    static let modelKeys = ["model", "slug", "preset"]

    /// The decider table, read generously:  under its own `[decider.consult_backup]` header, or
    /// inline as `consult_backup = { … }` in `[decider]`, and the same two shapes under the leftover
    /// `[muse_bridge]`.  Only the runtime and the model are read from it.
    static func read(from config: ListenerConfigDocument, configProblem: String? = nil) -> OperatorDecider {
        if let configProblem {
            return .notKnown("The configuration file was not read, so the decider is not known.\u{00A0} \(configProblem)")
        }
        var found: [String: ListenerConfigValue]?
        for section in sectionNames {
            for name in tableNames {
                if let header = config.sections.first(where: { $0.name == "\(section).\(name)" }) {
                    var entries: [String: ListenerConfigValue] = [:]
                    for line in header.assignments {
                        entries[line.key] = line.node.value
                    }
                    found = entries
                } else if case .table(let entries)? = config.value(section: section, key: name) {
                    found = entries
                }
                if found != nil { break }
            }
            if found != nil { break }
        }
        guard let table = found else { return .notSet }
        let runtime = runtimeKeys.lazy.compactMap { table[$0]?.scalarText }.first
        let model = modelKeys.lazy.compactMap { table[$0]?.scalarText }.first
        return .set(runtime: runtime, model: model)
    }

    /// Whether the decider's runtime is one other than the expected one, or is missing.
    var hasUnexpectedRuntime: Bool {
        guard case .set(let runtime, _) = self else { return false }
        return runtime != Self.expectedRuntime
    }

    /// The decider, in a sentence.
    var sentence: String {
        switch self {
        case .notKnown(let why):
            return why
        case .notSet:
            return "No decider is set."
        case .set(let runtime, let model):
            var text = "Runtime \(runtime ?? "not set"), model \(model ?? "not set")."
            if hasUnexpectedRuntime {
                text += "\u{00A0} The runtime expected is \(Self.expectedRuntime)."
            }
            return text
        }
    }
}
