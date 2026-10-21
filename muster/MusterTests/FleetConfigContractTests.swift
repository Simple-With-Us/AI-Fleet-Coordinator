import XCTest
/// The envelope the app reads, and the gate that refuses one it cannot.
final class FleetConfigContractTests: XCTestCase {
    func test_a_valid_envelope_decodes() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.envelope.utf8))
        XCTAssertEqual(envelope.schemaVersion, FleetConfigContract.supportedSchemaVersion)
        XCTAssertEqual(envelope.tool, "fleet-config")
        XCTAssertEqual(envelope.command, "status")
        XCTAssertEqual(envelope.results.count, 1)
        XCTAssertEqual(envelope.summary.total, 1)
    }

    /// The version is an exact integer, so a newer engine is refused whole.
    func test_the_gate_refuses_another_schema_version() {
        let json = Fixtures.envelope.replacingOccurrences(
            of: "\"schemaVersion\": 1",
            with: "\"schemaVersion\": 2"
        )
        XCTAssertThrowsError(try EnvelopeDecoder.decode(Data(json.utf8))) { error in
            guard case EnvelopeDecoder.Failure.schemaVersion(let found) = error else {
                return XCTFail("expected a schema version refusal, got \(error)")
            }
            XCTAssertEqual(found, 2)
        }
    }

    func test_the_gate_refuses_another_tool() {
        let json = Fixtures.envelope.replacingOccurrences(
            of: "\"tool\": \"fleet-config\"",
            with: "\"tool\": \"something-else\""
        )
        XCTAssertThrowsError(try EnvelopeDecoder.decode(Data(json.utf8)))
    }

    func test_the_gate_refuses_a_truncated_envelope() {
        XCTAssertThrowsError(try EnvelopeDecoder.decode(Data("{\"schemaVersion\": 1,".utf8)))
    }

    /// A state the app does not know is never read as a healthy one.
    func test_a_state_the_app_does_not_know_reads_as_unknown() throws {
        let json = Fixtures.envelope.replacingOccurrences(of: "\"PASS\"", with: "\"WOBBLY\"")
        let envelope = try EnvelopeDecoder.decode(Data(json.utf8))
        XCTAssertEqual(envelope.results[0].checkState, .unknown)
    }

    func test_the_open_expected_and_observed_objects_decode() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.envelope.utf8))
        let result = envelope.results[0]
        XCTAssertEqual(result.expected["present"], .bool(true))
        XCTAssertEqual(result.expected["names"], .array([.string("lane-guard")]))
        XCTAssertEqual(result.observed["timeoutSeconds"], .number(10))
        XCTAssertEqual(result.proofLevelRequired, "L1")
        XCTAssertEqual(result.proofLevelAchieved, "L1")
        XCTAssertFalse(result.failing)
    }

    /// The proof a rule did not reach is never dressed as one it did.
    func test_a_rule_without_a_reached_proof_says_so() throws {
        let json = Fixtures.envelope.replacingOccurrences(
            of: "\"proofLevelAchieved\": \"L1\"",
            with: "\"proofLevelAchieved\": null"
        )
        let envelope = try EnvelopeDecoder.decode(Data(json.utf8))
        XCTAssertEqual(envelope.results[0].proofLevelAchieved, nil)
        XCTAssertTrue(envelope.results[0].proofText.contains("not proved"))
    }
}

/// The JSON the tests read.
enum Fixtures {
    /// One envelope with one passing hook rule.
    static let envelope = """
    {
     "schemaVersion": 1,
     "tool": "fleet-config",
     "command": "status",
     "generatedAt": "2026-10-19T14:30:00Z",
     "summary": {
      "total": 1,
      "byState": {"PASS": 1, "DRIFT": 0, "MISSING": 0, "UNPROVEN": 0, "UNSUPPORTED": 0, "UNKNOWN": 0},
      "overallVerdict": "PASS",
      "failing": 0
     },
     "results": [
      {
       "ruleId": "claude-hooks-lane-guard",
       "seat": "CLAUDE",
       "platform": "claude",
       "kind": "hooks",
       "state": "PASS",
       "reason": "Hook lane-guard is registered and denied the sentinel write at L1.",
       "reasonCode": "ok",
       "expected": {"description": "Lane guard hook registered", "present": true, "names": ["lane-guard"], "timeoutSeconds": 10},
       "observed": {"present": true, "names": ["lane-guard"], "timeoutSeconds": 10},
       "proofLevelRequired": "L1",
       "proofLevelAchieved": "L1",
       "collector": "mac",
       "checkType": "hook_registered",
       "checkedAt": "2026-10-19T14:30:00Z",
       "expiresAt": "2026-10-19T14:45:00Z",
       "ttlSeconds": 900,
       "stale": false,
       "failing": false
      }
     ]
    }
    """

    /// A manifest with one seat that has a rule and one that is covered.
    static let manifest = """
    {
     "schemaVersion": 1,
     "seats": {
      "CLAUDE": {
       "platforms": ["claude"],
       "rules": [
        {
         "id": "claude-hooks-lane-guard",
         "kind": "hooks",
         "platforms": ["claude"],
         "check": {
          "type": "hook_registered",
          "detail": "fleet-guard-hook",
          "target": "~/.claude/settings.json"
         }
        }
       ],
       "notApplicable": []
      },
      "ECHO": {
       "platforms": [],
       "rules": [],
       "notApplicable": [
        {"kind": "instructions", "platforms": [], "reason": "Echo runs elsewhere."},
        {"kind": "hooks", "platforms": [], "reason": "Echo runs elsewhere."},
        {"kind": "mcp", "platforms": [], "reason": "Echo runs elsewhere."},
        {"kind": "skills", "platforms": [], "reason": "Echo runs elsewhere."},
        {"kind": "subagents", "platforms": [], "reason": "Echo runs elsewhere."},
        {"kind": "permissions", "platforms": [], "reason": "Echo runs elsewhere."}
       ]
      }
     }
    }
    """

    /// The registry with one live seat and one retired one.
    static let registry = """
    {
     "seats": [
      {"tag": "CLAUDE"},
      {"tag": "ECHO"},
      {"tag": "GONE", "retired": true}
     ]
    }
    """

    /// One envelope with a passing rule and a drifting rule for the same seat and kind.
    static let mixedEnvelope = """
    {
     "schemaVersion": 1,
     "tool": "fleet-config",
     "command": "status",
     "generatedAt": "2026-10-19T14:30:00Z",
     "summary": {
      "total": 2,
      "byState": {"PASS": 1, "DRIFT": 1, "MISSING": 0, "UNPROVEN": 0, "UNSUPPORTED": 0, "UNKNOWN": 0},
      "overallVerdict": "FAIL",
      "failing": 1
     },
     "results": [
      {
       "ruleId": "claude-hooks-a", "seat": "CLAUDE", "platform": "claude", "kind": "hooks",
       "state": "PASS", "reason": "registered", "reasonCode": "ok",
       "expected": {}, "observed": {},
       "proofLevelRequired": "L0", "proofLevelAchieved": "L0",
       "collector": "mac", "checkType": "hook_registered",
       "checkedAt": "2026-10-19T14:30:00Z", "expiresAt": "2026-10-19T14:45:00Z",
       "ttlSeconds": 900, "stale": false, "failing": false
      },
      {
       "ruleId": "claude-hooks-b", "seat": "CLAUDE", "platform": "claude", "kind": "hooks",
       "state": "DRIFT", "reason": "the matcher does not cover the runtime tools",
       "reasonCode": "drift",
       "expected": {}, "observed": {},
       "proofLevelRequired": "L0", "proofLevelAchieved": null,
       "collector": "mac", "checkType": "hook_registered",
       "checkedAt": "2026-10-19T14:30:00Z", "expiresAt": "2026-10-19T14:45:00Z",
       "ttlSeconds": 900, "stale": false, "failing": true
      }
     ]
    }
    """
}

/// The manifest's targets,  which are the only place the panel learns a cell's file.
final class ManifestTargetTests: XCTestCase {
    /// The manifest names the file a rule checks,  and the panel reads that file.
    func test_the_manifest_names_the_file_a_rule_checks() throws {
        let manifest = try JSONDecoder().decode(
            SeatManifest.self,
            from: Data(Fixtures.manifest.utf8)
        )
        let targets = manifest.targets(seat: "CLAUDE", kind: .hooks)
        XCTAssertEqual(targets.count, 1)
        XCTAssertEqual(targets.first?.target, "~/.claude/settings.json")
        XCTAssertEqual(targets.first?.checkType, "hook_registered")
    }

    /// A pair with no rule names no file,  so the panel has nothing to show.
    func test_a_pair_with_no_rule_names_no_file() throws {
        let manifest = try JSONDecoder().decode(
            SeatManifest.self,
            from: Data(Fixtures.manifest.utf8)
        )
        XCTAssertTrue(manifest.targets(seat: "CLAUDE", kind: .instructions).isEmpty)
        XCTAssertTrue(manifest.targets(seat: "ECHO", kind: .hooks).isEmpty)
    }
}
