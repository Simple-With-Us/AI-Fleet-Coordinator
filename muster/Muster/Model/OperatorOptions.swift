import Foundation

/// The Operator's own options.
///
/// The Operator is a service seat:  a bot that would carry fleet operations under its own
/// name and issue the fleet's session letters.  It is not provisioned yet.
///
/// The app reads no credentials,  so it cannot say whether the Operator's key exists.  What
/// it shows is the identity the Operator must have,  where each name lives,  and whether the
/// configuration file gives it one.
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
    /// The wake type the Operator must have.  It holds no agent,  so it never wakes one.
    static let requiredWake = "inbox"

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
            location: "the listener's environment",
            purpose: "the pinned bot check"
        ),
        Name(
            name: "OPERATOR_ZULIP_EMAIL",
            location: "the listener's environment",
            purpose: "the pinned bot check"
        ),
    ]

    /// The letters the Operator issues session letters from.
    static let letterPool = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    /// Whether the configuration file has a section for the Operator.
    var isConfigured: Bool = false
    /// The bot name the configuration file gives it.
    var bot: String?
    /// Where the Operator's credentials come from.
    var creds: String?
    /// The wake type the configuration file gives it.
    var wake: String?
    /// The rules the configuration file breaks.
    var brokenRules: [String] = []
    /// The one thing that is wrong,  when the Operator cannot be provisioned.
    var problem: String?

    /// Whether the configuration file gives the Operator the identity the runtime requires.
    ///
    /// This says nothing about the credentials:  the app reads none,  and says so.
    var hasRequiredIdentity: Bool {
        isConfigured && brokenRules.isEmpty
    }

    /// The options the configuration file gives the Operator.
    static func build(from config: ListenerConfigDocument) -> OperatorOptions {
        let section = "seat.\(seatTag)"
        let isConfigured = config.sections.contains { $0.name == section }
        guard isConfigured else {
            return OperatorOptions(
                isConfigured: false,
                bot: nil,
                creds: nil,
                wake: nil,
                brokenRules: [],
                problem: "The configuration file has no [seat.\(seatTag)] section,  so the Operator is not provisioned."
            )
        }
        let bot = config.value(section: section, key: "bot")?.scalarText
        let creds = config.value(section: section, key: "creds")?.scalarText
        let wake = config.value(section: section, key: "wake")?.scalarText
        var broken: [String] = []
        if bot != requiredBot {
            broken.append("bot must be \(requiredBot)")
        }
        if creds != requiredCreds {
            broken.append("creds must be \(requiredCreds)")
        }
        if wake != requiredWake {
            broken.append("wake must be \(requiredWake)")
        }
        return OperatorOptions(
            isConfigured: true,
            bot: bot,
            creds: creds,
            wake: wake,
            brokenRules: broken,
            problem: broken.isEmpty ? nil : "The configuration file breaks the rules the runtime enforces."
        )
    }
}
