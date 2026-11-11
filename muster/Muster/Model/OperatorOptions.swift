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
    /// The wake type the configuration file gives it.
    var wake: String?
    /// The rules the configuration file breaks.
    var brokenRules: [String] = []
    /// The one thing that is wrong with a section the file holds,  or nil when nothing is.
    var problem: String?
    /// Why the configuration file was not read,  when it was not.  Then nothing is known about
    /// its Operator section,  and the file is never said to have none.
    var notRead: String?

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
            return "The Mac's file holds a [seat.OPERATOR] section, and it keeps the rules the runtime enforces (bot \(Self.requiredBot), creds \(Self.requiredCreds), wake \(Self.requiredWake))."
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
        if let configProblem {
            return OperatorOptions(
                isConfigured: false,
                bot: nil,
                creds: nil,
                wake: nil,
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
                wake: nil,
                brokenRules: [],
                problem: nil
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
