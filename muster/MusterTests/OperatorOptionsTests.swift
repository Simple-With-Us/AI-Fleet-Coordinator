import XCTest

/// The Operator's own options:  the identity it must have,  and where each name lives.
final class OperatorOptionsTests: XCTestCase {
    /// The Mac needs no `[seat.OPERATOR]` section:  the eavesdropper builds the Operator's client
    /// from Fleet-OPS on first use.  So a file without one is not called unprovisioned.
    func test_a_file_without_a_section_is_not_a_fault() {
        let document = ListenerConfigDocument(text: "[seat.MM]\nwake = \"muse\"\n")
        let options = OperatorOptions.build(from: document)
        XCTAssertFalse(options.isConfigured)
        XCTAssertFalse(options.hasRequiredIdentity)
        XCTAssertNil(options.problem)
        XCTAssertTrue(options.summary.contains("[seat.OPERATOR]"))
        XCTAssertTrue(options.summary.contains("needs none"))
    }

    /// The Operator has the identity the runtime requires.
    func test_the_operator_has_the_identity_the_runtime_requires() {
        let document = ListenerConfigDocument(text: """
        [seat.OPERATOR]
        bot = "Operator"
        creds = "infisical"
        wake = "inbox"
        """)
        let options = OperatorOptions.build(from: document)
        XCTAssertTrue(options.isConfigured)
        XCTAssertTrue(options.hasRequiredIdentity)
        XCTAssertEqual(options.brokenRules, [])
        XCTAssertNil(options.problem)
    }

    /// The rules the runtime enforces on the service seat are the ones the panel checks.  The
    /// Operator Captures Only, so a routine table on it is a broken rule, inline or under its own
    /// header.
    func test_the_breaks_of_the_runtime_rules_are_named() {
        let document = ListenerConfigDocument(text: """
        [seat.OPERATOR]
        bot = "OperatorBot"
        creds = "file"
        routine = { url_env = "OPERATOR_ROUTINE_URL" }
        """)
        let options = OperatorOptions.build(from: document)
        XCTAssertTrue(options.isConfigured)
        XCTAssertFalse(options.hasRequiredIdentity)
        XCTAssertTrue(options.hasRoutine)
        XCTAssertEqual(options.brokenRules.count, 3)
        XCTAssertTrue(options.brokenRules.contains { $0.contains("bot") })
        XCTAssertTrue(options.brokenRules.contains { $0.contains("creds") })
        XCTAssertTrue(options.brokenRules.contains { $0.contains("no routine table") })

        let header = OperatorOptions.build(from: ListenerConfigDocument(text: """
        [seat.OPERATOR]
        bot = "Operator"
        creds = "infisical"

        [seat.OPERATOR.routine]
        url_env = "OPERATOR_ROUTINE_URL"
        """))
        XCTAssertEqual(header.brokenRules, ["the Operator must capture only, with no routine table"])
    }

    /// The Operator's rule is about the routine table alone:  a wake line left over from the old
    /// wake types breaks nothing, whatever it says.
    func test_a_leftover_wake_line_does_not_break_the_operator() {
        for wake in ["inbox", "muse", "http"] {
            let options = OperatorOptions.build(from: ListenerConfigDocument(text: """
            [seat.OPERATOR]
            bot = "Operator"
            creds = "infisical"
            wake = "\(wake)"
            """))
            XCTAssertTrue(options.hasRequiredIdentity, wake)
            XCTAssertFalse(options.hasRoutine, wake)
        }
        XCTAssertTrue(OperatorOptions.requiredKind.contains("Captures Only"))
        let kept = OperatorOptions.build(from: ListenerConfigDocument(text: "[seat.OPERATOR]\nbot = \"Operator\"\ncreds = \"infisical\"\n"))
        XCTAssertTrue(kept.summary.contains("Captures Only, with no routine table"))
    }

    /// The decider every platform shares is read from `[decider.consult_backup]`, under its own
    /// header or inline, and only its runtime and model are shown.
    func test_the_decider_is_read_from_the_file() {
        let header = OperatorDecider.read(from: ListenerConfigDocument(text: """
        [decider.consult_backup]
        runtime = "openrouter"
        model = "@preset/operator-inbox-router"
        key_env = "OPERATOR_OPENROUTER_API_KEY"
        """))
        XCTAssertEqual(header, .set(runtime: "openrouter", model: "@preset/operator-inbox-router"))
        XCTAssertFalse(header.hasUnexpectedRuntime)
        XCTAssertEqual(header.sentence, "Runtime openrouter, model @preset/operator-inbox-router.")
        // The key's variable is never shown, and nor is anything else in the table.
        XCTAssertFalse(header.sentence.contains("OPERATOR_OPENROUTER_API_KEY"))

        let inline = OperatorDecider.read(from: ListenerConfigDocument(text: """
        [decider]
        consult_backup = { runtime = "openrouter", model = "@preset/operator-inbox-router" }
        """))
        XCTAssertEqual(inline, header)

        let options = OperatorOptions.build(from: ListenerConfigDocument(text: "[decider.consult_backup]\nruntime = \"openrouter\"\nmodel = \"m\"\n"))
        XCTAssertEqual(options.decider, .set(runtime: "openrouter", model: "m"))
        XCTAssertTrue(OperatorDecider.help.contains("One decider serves every platform."))
    }

    /// A file that still holds only the leftover `[muse_bridge]` location of the deleted Muse inbox
    /// bridge is read the way the listener reads it, and `[decider]` wins when both are there.
    func test_the_leftover_location_is_read_until_the_decider_table_exists() {
        let leftover = OperatorDecider.read(from: ListenerConfigDocument(text: """
        [muse_bridge]
        enabled = false
        remote_seats = []

        [muse_bridge.consult_backup]
        runtime = "openrouter"
        model = "@preset/operator-inbox-router"
        """))
        XCTAssertEqual(leftover, .set(runtime: "openrouter", model: "@preset/operator-inbox-router"))
        let inline = OperatorDecider.read(from: ListenerConfigDocument(text: """
        [muse_bridge]
        consult_backup = { runtime = "openrouter", model = "@preset/operator-inbox-router" }
        """))
        XCTAssertEqual(inline, leftover)

        let both = OperatorDecider.read(from: ListenerConfigDocument(text: """
        [muse_bridge.consult_backup]
        runtime = "openrouter"
        model = "@preset/old"

        [decider.consult_backup]
        runtime = "openrouter"
        model = "@preset/new"
        """))
        XCTAssertEqual(both, .set(runtime: "openrouter", model: "@preset/new"))
    }

    /// A file with no decider table says so, and a file that was not read leaves it not known.
    func test_a_missing_decider_says_none_is_set() {
        let missing = OperatorDecider.read(from: ListenerConfigDocument(text: "[muse_bridge]\nenabled = false\n"))
        XCTAssertEqual(missing, .notSet)
        XCTAssertEqual(missing.sentence, "No decider is set.")
        XCTAssertFalse(missing.hasUnexpectedRuntime)
        XCTAssertEqual(OperatorOptions.build(from: ListenerConfigDocument(text: "")).decider, .notSet)
        guard case .notKnown(let why) = OperatorOptions.build(from: ListenerConfigDocument(text: ""), configProblem: "denied").decider else {
            return XCTFail("a file that was not read says nothing about the decider")
        }
        XCTAssertTrue(why.contains("denied"))
    }

    /// A decider whose runtime is not the one expected, or names none, is marked plainly.
    func test_a_decider_with_an_unexpected_runtime_is_marked() {
        let other = OperatorDecider.read(from: ListenerConfigDocument(text: "[decider.consult_backup]\nruntime = \"muse\"\nmodel = \"x\"\n"))
        XCTAssertEqual(other, .set(runtime: "muse", model: "x"))
        XCTAssertTrue(other.hasUnexpectedRuntime)
        XCTAssertTrue(other.sentence.contains("The runtime expected is openrouter."))
        let unnamed = OperatorDecider.read(from: ListenerConfigDocument(text: "[decider.consult_backup]\nmodel = \"x\"\n"))
        XCTAssertEqual(unnamed, .set(runtime: nil, model: "x"))
        XCTAssertTrue(unnamed.hasUnexpectedRuntime)
        XCTAssertTrue(unnamed.sentence.hasPrefix("Runtime not set, model x."))
    }

    /// The names the Operator needs are the ones the runtime reads.
    func test_the_names_are_the_ones_the_runtime_reads() {
        let names = OperatorOptions.names.map(\.name)
        XCTAssertTrue(names.contains("ZULIP_OPERATOR_EMAIL"))
        XCTAssertTrue(names.contains("ZULIP_OPERATOR_API_KEY"))
        XCTAssertTrue(names.contains("OPERATOR_ZULIP_USER_ID"))
        XCTAssertTrue(names.contains("OPERATOR_ZULIP_EMAIL"))
        for name in OperatorOptions.names {
            XCTAssertFalse(name.location.isEmpty)
            XCTAssertFalse(name.purpose.isEmpty)
        }
    }

    /// Every name the Operator needs names Infisical as its source.
    ///
    /// The eavesdropper's environment is where the values arrive,  not where they live:
    /// `scripts/simple_tools/service_identity.py` holds `OPERATOR_INFISICAL_PATH =
    /// "/zulip_operator"`,  so the panel must not point an operator at a plain env var.
    func test_every_operator_name_names_infisical_as_its_source() {
        for name in OperatorOptions.names {
            XCTAssertTrue(
                name.location.contains("Infisical"),
                "\(name.name) is shown as living in \(name.location)"
            )
        }
        XCTAssertEqual(OperatorOptions.requiredCreds, "infisical")
    }
}
