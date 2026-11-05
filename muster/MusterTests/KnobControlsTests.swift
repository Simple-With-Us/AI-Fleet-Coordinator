import XCTest

/// The Controls tab tells the truth:  every filter finds its knobs,  each knob says who
/// reads it,  a value says where it came from,  and the editor says which way a change
/// moves the knob.  The engine's own generated files are read where they live,  so the
/// Python side and the Swift side cannot drift with CI staying green.
final class KnobControlsTests: XCTestCase {
    /// The repository this test sits in.
    private var repoRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private let identity = EngineIdentity.Identity(project: "test-infisical-project", environment: "prod")

    /// A home with no copy,  so no value is known until a live read is applied.
    private func emptyHome() throws -> URL {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-controls-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: home) }
        return home
    }

    /// The real registry,  read the way the panel reads it.
    private func realRegistry() throws -> KnobRegistry {
        let inventory = repoRoot.appendingPathComponent(KnobReader.inventoryPath)
        guard FileManager.default.fileExists(atPath: inventory.path) else {
            throw XCTSkip("The repository's registry.json is not beside this test.")
        }
        return KnobReader.read(repoRoot: repoRoot, home: try emptyHome(), identity: identity)
    }

    /// The engine's answer the Python tests keep in the engine's own shape.
    private func statusFixture() throws -> KnobLiveStatus {
        let url = repoRoot.appendingPathComponent("scripts/fleet_knobs/tests/fixtures/status_values.json")
        guard let data = try? Data(contentsOf: url) else {
            throw XCTSkip("The engine's status fixture is not beside this test.")
        }
        return try XCTUnwrap(KnobLiveStatus.parse(data))
    }

    private func knob(_ registry: KnobRegistry, _ name: String) throws -> KnobRegistry.Knob {
        try XCTUnwrap(registry.knobs.first { $0.name == name }, name)
    }

    // MARK: The registry

    func test_the_inventory_is_read_and_says_who_reads_each_knob() throws {
        let registry = try realRegistry()
        XCTAssertEqual(registry.registryFile, KnobReader.inventoryPath)
        XCTAssertEqual(registry.unreadableRows, 0)
        XCTAssertNil(registry.problem)
        for knob in registry.knobs {
            XCTAssertFalse(knob.label.isEmpty, knob.name)
            XCTAssertFalse(knob.group.isEmpty, knob.name)
            XCTAssertEqual(knob.consumers.isEmpty, knob.hosts == .unknown, knob.name)
        }
        XCTAssertEqual(try knob(registry, "NEEDS_OWNER_NOTIFY_PAUSED").hosts, .mac)
        XCTAssertEqual(try knob(registry, "CF_USAGE_WATCHDOG_MODE").hosts, .server)
        XCTAssertEqual(try knob(registry, "OPERATOR_API_URL").hosts, .both)
        let sweep = try knob(registry, "LANE_SWEEP_MODE")
        XCTAssertEqual(sweep.hosts, .unknown)
        XCTAssertTrue(sweep.affectsText.hasPrefix("No job in this repository reads this"))
        XCTAssertEqual(try knob(registry, "NEEDS_OWNER_CAPS").editor, .caps(["per_day", "per_hour", "urgent_per_hour"]))
        // The one secret fails closed and has no editor.
        let secrets = registry.knobs.filter { $0.isSecret }
        XCTAssertEqual(secrets.map { $0.name }, ["FLEET_FACTS_SIGNING_KEY"])
        XCTAssertNil(secrets.first?.editor)
    }

    /// The table in INFISICAL.md and the inventory name the same knobs in the same order,
    /// so an app that falls back to the table shows the same list.
    func test_the_table_fallback_lists_the_same_knobs() throws {
        let registry = try realRegistry()
        let table = try String(contentsOf: repoRoot.appendingPathComponent("INFISICAL.md"), encoding: .utf8)
        let root = try emptyHome()
        try table.write(to: root.appendingPathComponent("INFISICAL.md"), atomically: true, encoding: .utf8)
        let fallback = KnobReader.read(repoRoot: root, home: try emptyHome(), identity: identity)
        XCTAssertEqual(fallback.registryFile, "INFISICAL.md")
        XCTAssertEqual(fallback.knobs.map { $0.name }, registry.knobs.map { $0.name })
        XCTAssertEqual(fallback.knobs.map { $0.isSecret }, registry.knobs.map { $0.isSecret })
    }

    /// An inventory of a schema this app does not know is not used;  the table is.
    func test_an_unknown_inventory_schema_falls_back_to_the_table() throws {
        let root = try emptyHome()
        let folder = root.appendingPathComponent("scripts/fleet_knobs", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try #"{"schema": 99, "knobs": [{"name": "X", "kind": "bool", "kind_text": "bool"}]}"#
            .write(to: folder.appendingPathComponent("registry.json"), atomically: true, encoding: .utf8)
        try """
        <!-- fleet-knobs:begin -->
        | Key | Owner | Kind | Default | Secret | Local files may | What it does |
        |---|---|---|---|---|---|---|
        | `CONSULT_ENABLED` | C7 | bool | `false` | no | only set it to `false` | false refuses every consult verb |
        <!-- fleet-knobs:end -->
        """.write(to: root.appendingPathComponent("INFISICAL.md"), atomically: true, encoding: .utf8)
        let registry = KnobReader.read(repoRoot: root, home: try emptyHome(), identity: identity)
        XCTAssertEqual(registry.registryFile, "INFISICAL.md")
        XCTAssertEqual(registry.knobs.map { $0.name }, ["CONSULT_ENABLED"])
    }

    // MARK: Filters

    /// The Modes filter used to look for the words mode, flag and bool in the kind text,
    /// which no enum's kind holds, so it hid every mode knob.
    func test_the_modes_filter_finds_every_mode_knob() throws {
        let registry = try realRegistry()
        let names = Set(registry.filtered(.choices, host: .any, query: "").map { $0.name })
        for mode in ["FLEET_AUTOPUSH_MODE", "CLAIM_EXPIRY_MODE", "LANE_SWEEP_MODE", "CF_USAGE_WATCHDOG_MODE"] {
            XCTAssertTrue(names.contains(mode), mode)
        }
        XCTAssertTrue(names.contains("NEEDS_OWNER_NOTIFY_PAUSED"))
    }

    func test_every_kind_filter_finds_knobs_and_together_they_cover_all() throws {
        let registry = try realRegistry()
        var seen = Set<String>()
        for filter in [KnobFilter.choices, .numbers, .text] {
            let rows = registry.filtered(filter, host: .any, query: "")
            XCTAssertFalse(rows.isEmpty, filter.rawValue)
            seen.formUnion(rows.map { $0.name })
        }
        XCTAssertEqual(seen, Set(registry.knobs.map { $0.name }))
        XCTAssertTrue(registry.filtered(.text, host: .any, query: "").contains { $0.name == "OPERATOR_API_URL" })
    }

    func test_the_machine_filter_follows_the_readers() throws {
        let registry = try realRegistry()
        let server = Set(registry.filtered(.all, host: .server, query: "").map { $0.name })
        XCTAssertTrue(server.contains("CF_USAGE_WATCHDOG_MODE"))
        XCTAssertTrue(server.contains("OPERATOR_API_URL"))
        XCTAssertFalse(server.contains("NEEDS_OWNER_CAPS"))
        let unread = Set(registry.filtered(.all, host: .noReader, query: "").map { $0.name })
        XCTAssertTrue(unread.contains("LANE_SWEEP_MODE"))
        XCTAssertFalse(unread.contains("CF_USAGE_WATCHDOG_MODE"))
    }

    /// An empty Changed list with nothing read is "not known",  never an all-clear.
    func test_an_empty_changed_list_says_why() throws {
        let registry = try realRegistry()
        XCTAssertFalse(registry.valuesAreKnown)
        XCTAssertTrue(registry.filtered(.changed, host: .any, query: "").isEmpty)
        XCTAssertEqual(
            registry.emptyMessage(.changed, host: .any, query: ""),
            "The values could not be read, so which knobs are changed is not known."
        )
        XCTAssertEqual(registry.emptyMessage(.all, host: .any, query: "zzz"), "No knob matches this search.")
    }

    func test_groups_keep_the_registry_order() throws {
        let registry = try realRegistry()
        let groups = KnobRegistry.grouped(registry.rows)
        XCTAssertEqual(groups.first?.title, "Autopush")
        XCTAssertEqual(groups.map { $0.rows.count }.reduce(0, +), registry.knobs.count)
        XCTAssertEqual(Set(groups.map { $0.title }).count, groups.count)
    }

    // MARK: The live read

    /// On the Mac today Muster runs the engine with no reader token, so the live read
    /// fails and the values are this Mac's copy:  the panel says exactly that.
    func test_a_failed_live_read_says_why_and_shows_the_copy() throws {
        let status = try statusFixture()
        XCTAssertFalse(status.fetchOK)
        XCTAssertEqual(status.writeToken, .missing)
        let registry = try realRegistry().applying(status)
        let sweep = try knob(registry, "LANE_SWEEP_MODE")
        XCTAssertEqual(sweep.current, .current("act"))
        XCTAssertEqual(sweep.source, .macCopy)
        XCTAssertTrue(sweep.isChanged)
        XCTAssertTrue(sweep.isWiderThanDefault)
        let consult = try knob(registry, "CONSULT_ENABLED")
        XCTAssertEqual(consult.current, .defaultValue("false"))
        XCTAssertEqual(consult.source, .macCopy)
        let signing = try knob(registry, "FLEET_FACTS_SIGNING_KEY")
        XCTAssertEqual(signing.current, .secretUnconfirmed)
        XCTAssertTrue(registry.writesUnavailable)
        let first = try XCTUnwrap(registry.freshnessLines().first)
        XCTAssertTrue(first.isWarning)
        XCTAssertTrue(first.text.hasPrefix("Infisical was not read at "), first.text)
        XCTAssertTrue(first.text.contains("without the knob reader's Infisical token"), first.text)
        XCTAssertFalse(first.text.contains("UTC"))
    }

    func test_a_good_live_read_names_infisical_and_a_secret_only_as_set() throws {
        let json = """
        {"schema": 1, "fetch": "ok", "fetch_reason": null, "checked_epoch": 1794000000,
         "write_token": "present", "knobs": [
          {"name": "LANE_SWEEP_MODE", "owner": "C5", "set": true, "source": "infisical", "stale": false, "value": "ACT"},
          {"name": "CONSULT_ENABLED", "owner": "C7", "set": false, "source": "default", "stale": false, "value": "false"},
          {"name": "FLEET_FACTS_SIGNING_KEY", "owner": "F7", "set": true, "source": "infisical", "stale": false}
        ]}
        """
        let status = try XCTUnwrap(KnobLiveStatus.parse(Data(json.utf8)))
        let registry = try realRegistry().applying(status)
        let sweep = try knob(registry, "LANE_SWEEP_MODE")
        XCTAssertEqual(sweep.current, .current("act"), "a value typed in capitals reads in the engine's form")
        XCTAssertEqual(sweep.source, .infisical)
        XCTAssertEqual(try knob(registry, "CONSULT_ENABLED").source, .infisical)
        XCTAssertEqual(try knob(registry, "FLEET_FACTS_SIGNING_KEY").nowText, "set (secret)")
        XCTAssertFalse(registry.writesUnavailable)
        XCTAssertTrue(registry.valuesAreKnown)
        XCTAssertEqual(registry.freshnessLines().count, 1)
        XCTAssertTrue(registry.freshnessLines()[0].text.hasPrefix("Values read from Infisical at "))
        XCTAssertTrue(registry.freshnessLines()[0].text.hasSuffix(" PM.") || registry.freshnessLines()[0].text.hasSuffix(" AM."))
    }

    func test_a_status_of_another_schema_is_not_read() {
        XCTAssertNil(KnobLiveStatus.parse(Data(#"{"schema": 2, "fetch": "ok", "knobs": []}"#.utf8)))
        XCTAssertNil(KnobLiveStatus.parse(Data("not json".utf8)))
    }

    // MARK: The editor

    func test_values_typed_by_hand_open_in_the_engines_form() throws {
        var registry = try realRegistry()
        let index = try XCTUnwrap(registry.knobs.firstIndex { $0.name == "NEEDS_OWNER_NOTIFY_PAUSED" })
        registry.knobs[index].current = .current("yes")
        XCTAssertEqual(registry.knobs[index].editValue, "true")
        let sweepIndex = try XCTUnwrap(registry.knobs.firstIndex { $0.name == "LANE_SWEEP_MODE" })
        registry.knobs[sweepIndex].current = .current("RETIRE")
        XCTAssertEqual(registry.knobs[sweepIndex].editValue, "retire")
    }

    func test_the_direction_of_a_change_follows_the_registry() throws {
        let registry = try realRegistry()
        let sweep = try knob(registry, "LANE_SWEEP_MODE")
        XCTAssertEqual(sweep.direction(from: "report", to: "retire"), .wider)
        XCTAssertEqual(sweep.direction(from: "act", to: "report"), .narrower)
        XCTAssertEqual(KnobDirection.wider.sentence, "This makes the fleet do more than it does now.")
        let paused = try knob(registry, "NEEDS_OWNER_NOTIFY_PAUSED")
        XCTAssertEqual(paused.direction(from: "false", to: "true"), .narrower)
        let hours = try knob(registry, "FLEET_AUTOPUSH_IDLE_HOURS")
        XCTAssertEqual(hours.direction(from: "2", to: "4"), .narrower)
        let caps = try knob(registry, "NEEDS_OWNER_CAPS")
        XCTAssertEqual(caps.direction(from: "per_day=30,per_hour=6", to: "per_day=40,per_hour=6"), .wider)
        XCTAssertEqual(try knob(registry, "OPERATOR_API_URL").direction(from: "", to: "https://a.example"), .none)
        XCTAssertEqual(sweep.consequence(for: "retire"),
                       "The sweep may merge, retire merged-PR lanes and make the Phase 3 deletions.")
    }

    func test_the_editor_holds_the_engines_rules() throws {
        let registry = try realRegistry()
        let owner = try knob(registry, "OWNER_LINEAR_USER_ID")
        XCTAssertEqual(owner.problem(with: ""), "Use Reset To Default to clear this.")
        XCTAssertTrue(owner.problem(with: "not-a-uuid")?.contains("UUID") ?? false)
        XCTAssertNil(owner.problem(with: owner.example))
        let hours = try knob(registry, "FLEET_AUTOPUSH_IDLE_HOURS")
        XCTAssertNotNil(hours.problem(with: "+5"), "the engine takes digits only")
        XCTAssertNil(hours.problem(with: "5"))
        let caps = try knob(registry, "NEEDS_OWNER_CAPS")
        XCTAssertNotNil(caps.problem(with: "per_dya=5"))
        XCTAssertNil(caps.problem(with: "per_day=5,per_hour=2,urgent_per_hour=1"))
        let timeout = try knob(registry, "FLEET_SHEET_INGEST_TIMEOUT_MS")
        XCTAssertEqual(timeout.unitText(for: "5400000"), "90 minutes")
        XCTAssertEqual(timeout.step, 300000)
    }

    func test_an_unmet_requirement_is_named() throws {
        let registry = try realRegistry().applying(try statusFixture())
        let claim = try knob(registry, "CLAIM_EXPIRY_MODE")
        // The fixture does not carry the launch time, so its value is not known.
        XCTAssertEqual(
            claim.unmetRequirement(for: "act", in: registry),
            "act also needs CLAIM_EXPIRY_LAUNCH_AT, and whether that is set is not known."
        )
        var known = registry
        let index = try XCTUnwrap(known.knobs.firstIndex { $0.name == "CLAIM_EXPIRY_LAUNCH_AT" })
        known.knobs[index].current = .defaultValue("unset")
        XCTAssertEqual(
            claim.unmetRequirement(for: "act", in: known),
            "act also needs CLAIM_EXPIRY_LAUNCH_AT, which is not set, so it will do less than its name says."
        )
        known.knobs[index].current = .current("2026-11-05T09:00:00-06:00")
        XCTAssertNil(claim.unmetRequirement(for: "act", in: known))
        XCTAssertNil(claim.unmetRequirement(for: "ping-only", in: known))
    }

    /// The copy rules the app copies from the engine are read from the engine's own file,
    /// so a change there fails here rather than passing against a literal.
    func test_the_copy_rules_match_the_engines_file() throws {
        let url = repoRoot.appendingPathComponent("scripts/fleet_knobs/lkg.py")
        guard let source = try? String(contentsOf: url, encoding: .utf8) else {
            throw XCTSkip("The engine's lkg.py is not beside this test.")
        }
        let lines = Set(source.components(separatedBy: "\n").map {
            $0.components(separatedBy: "#")[0].trimmingCharacters(in: .whitespaces)
        })
        XCTAssertTrue(lines.contains("SCHEMA = \(KnobReader.copySchema)"))
        XCTAssertTrue(lines.contains("REFRESH_SECONDS = \(KnobReader.refreshSeconds)"))
        XCTAssertTrue(lines.contains("MAX_AGE_SECONDS = \(KnobReader.maxAgeSeconds / 3600) * 3600"))
        XCTAssertTrue(lines.contains("MAX_BYTES = \(KnobReader.maxCopyBytes / 1024) * 1024"))
    }

    /// A compliance threshold nobody set is not a threshold, and the row says so.
    func test_an_unset_compliance_threshold_does_not_read_as_configured() throws {
        let registry = try realRegistry().applying(try statusFixture())
        var known = registry
        let index = try XCTUnwrap(known.knobs.firstIndex { $0.name == "COMPLIANCE_CERT_EXPIRY_DAYS" })
        known.knobs[index].current = .defaultValue("30")
        XCTAssertEqual(known.knobs[index].nowText, "not set, so its rule reads unknown (suggested 30)")
        known.knobs[index].current = .current("30")
        XCTAssertEqual(known.knobs[index].nowText, "30")
    }

    // MARK: Sentences

    func test_problems_are_whole_sentences() {
        let sentences = KnobRegistry.problemSentences(
            engineReason: "the engine did not say which project its copy belongs to:  boom",
            registryProblem: "the registry block is not in the file",
            liveProblem: nil
        )
        XCTAssertEqual(sentences.count, 2)
        for sentence in sentences {
            XCTAssertTrue(sentence.first?.isUppercase ?? false, sentence)
            XCTAssertTrue(sentence.hasSuffix("."), sentence)
        }
        XCTAssertEqual(KnobRegistry.sentence("the knob engine stopped"), "The knob engine stopped.")
        XCTAssertTrue(KnobRegistry.describeFetchReason("recent-timeout").contains("within 8 seconds"))
    }
}

/// The secrets panel shows what the doctor found,  including a problem,  and only the
/// checks it names.
final class SecretsDoctorPanelTests: XCTestCase {
    private final class Commands: CommandRunning {
        var failure: Error?
        var reply = ""
        func run(_ executable: String, _ arguments: [String]) throws -> String {
            if let failure { throw failure }
            return reply
        }
    }

    func test_a_count_of_one_stays_a_count_and_unknown_keys_are_dropped() throws {
        let json = #"{"ok": false, "names_count": 1, "unresolved_reference_count": 0, "fetch_names_ok": true, "unlisted_check": "xxxxxxxx","failure_reason": "Infisical login failed (exit 1)", "ai_fleet_coordinator_fetch_ok": false}"#
        let report = try FleetSecrets.parseDoctor(json)
        XCTAssertEqual(report["names_count"], "1")
        XCTAssertEqual(report["unresolved_reference_count"], "0")
        XCTAssertEqual(report["fetch_names_ok"], "true")
        XCTAssertEqual(report["failure_reason"], "Infisical login failed (exit 1)")
        XCTAssertNil(report["unlisted_check"], "a key the panel does not name is dropped")
        let rows = FleetSecrets.doctorRows(report)
        XCTAssertEqual(rows.first, SecretsDoctorRow(label: "Overall", value: "Problem Found"))
        XCTAssertTrue(rows.contains(SecretsDoctorRow(label: "Ai Fleet Coordinator Read", value: "No")))
    }

    func test_a_doctor_that_found_a_problem_is_shown_not_hidden() throws {
        let fake = Commands()
        fake.failure = CommandExitError(output: #"{"ok": false, "cli_present": true}"#, status: 1)
        let report = try FleetSecrets(commands: fake, home: URL(fileURLWithPath: "/Users/example"))
            .doctorReport()
        XCTAssertEqual(report["ok"], "false")
        XCTAssertEqual(report["cli_present"], "true")
    }

    func test_a_missing_command_reads_as_not_installed() {
        let missing = NSError(domain: NSCocoaErrorDomain, code: NSFileNoSuchFileError)
        XCTAssertEqual(FleetSecretError.fromNonKeyRead(missing, name: "/"), .notInstalled)
        XCTAssertEqual(FleetSecretError.from(missing, name: "A_KEY"), .notInstalled)
        XCTAssertFalse(FleetSecretError.notInstalled.localizedDescription.contains("in time"))
    }

    /// A blank address is refused before any token is read.
    func test_a_blank_recall_address_reads_no_secret() async {
        final class Recorder: SecretResolving {
            var asked: [String] = []
            func secret(named name: String) throws -> String {
                asked.append(name)
                return "fake-token-for-test"
            }
        }
        let recorder = Recorder()
        let config = RecallSettingsConfig(baseURLString: "", tokenKeyName: "RECALL_ADMIN_TOKEN")
        do {
            _ = try await RecallSettingsClient.fetch(config: config, secrets: recorder)
            XCTFail("a blank address must be refused")
        } catch {
            XCTAssertEqual(error as? RecallSettingsError, .invalidBaseURL)
        }
        XCTAssertTrue(recorder.asked.isEmpty)
    }
}
