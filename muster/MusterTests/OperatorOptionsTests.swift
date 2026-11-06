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

    /// The rules the runtime enforces on the service seat are the ones the panel checks.
    func test_the_breaks_of_the_runtime_rules_are_named() {
        let document = ListenerConfigDocument(text: """
        [seat.OPERATOR]
        bot = "OperatorBot"
        creds = "file"
        wake = "muse"
        """)
        let options = OperatorOptions.build(from: document)
        XCTAssertTrue(options.isConfigured)
        XCTAssertFalse(options.hasRequiredIdentity)
        XCTAssertEqual(options.brokenRules.count, 3)
        XCTAssertTrue(options.brokenRules.contains { $0.contains("bot") })
        XCTAssertTrue(options.brokenRules.contains { $0.contains("creds") })
        XCTAssertTrue(options.brokenRules.contains { $0.contains("wake") })
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
    /// `scripts/agent_sync/service_identity.py` holds `OPERATOR_INFISICAL_PATH =
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
