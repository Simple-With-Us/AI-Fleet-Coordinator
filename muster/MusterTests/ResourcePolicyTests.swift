import Foundation
import XCTest

final class ResourcePolicyTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_793_709_000)

    func test_current_nested_receipt_decodes_and_keeps_borrowing_false() throws {
        let reading = decode(try json())
        XCTAssertEqual(reading.state, .current)
        XCTAssertTrue(reading.canReviewChanges(at: now))
        XCTAssertEqual(reading.snapshot?.desired.allowBorrowing, false)
        XCTAssertEqual(reading.snapshot?.desired.cpuCores, 8)
        XCTAssertEqual(reading.snapshot?.observed?.cpuWeight, 100)
    }

    func test_direct_receipt_older_than_one_minute_is_stale() throws {
        let reading = decode(try json(), receiptAge: 61)
        XCTAssertEqual(reading.state, .stale)
        XCTAssertFalse(reading.canReviewChanges(at: now))
    }

    func test_local_receipt_expires_after_one_minute() throws {
        let reading = decode(try json())
        XCTAssertTrue(reading.canReviewChanges(at: now))
        XCTAssertFalse(reading.canReviewChanges(at: now.addingTimeInterval(61)))
        XCTAssertEqual(reading.state(at: now.addingTimeInterval(61)), .stale)
    }

    func test_collector_source_age_is_separate_from_fresh_direct_read_age() throws {
        let reading = decode(try json(generatedAt: "2026-11-03T12:10:00Z"))
        XCTAssertEqual(reading.state, .current)
        XCTAssertTrue(reading.canReviewChanges(at: now))
        XCTAssertEqual(reading.collectorAge, 1_200)
    }

    func test_stale_source_report_cannot_be_reviewed_after_45_minutes() throws {
        let reading = decode(try json(generatedAt: "2026-11-03T11:44:00Z"))
        XCTAssertEqual(reading.state, .stale)
        XCTAssertFalse(reading.canReviewChanges(at: now))
        XCTAssertTrue(reading.reason?.contains("source is older than 45 minutes") == true)
    }

    func test_stale_collector_observation_cannot_be_reviewed_after_45_minutes() throws {
        let source = String(decoding: try json(), as: UTF8.self)
            .replacingOccurrences(of: "2026-11-03T12:25:00Z", with: "2026-11-03T11:44:00Z")
        let reading = decode(Data(source.utf8))
        XCTAssertEqual(reading.state, .stale)
        XCTAssertFalse(reading.canReviewChanges(at: now))
        XCTAssertTrue(reading.reason?.contains("collector observation is older than 45 minutes") == true)
    }

    func test_observation_age_is_distinct_from_fresh_source_report_age() throws {
        let data = try json(generatedAt: "2026-11-03T12:25:00Z")
        let reading = decode(try changingObserved(data) { $0["updatedAt"] = "2026-11-03T11:40:00Z" })
        XCTAssertEqual(reading.collectorAge, 300)
        XCTAssertEqual(reading.observationAge, 3_000)
        XCTAssertEqual(reading.state, .stale)
        XCTAssertFalse(reading.canReviewChanges(at: now))
        XCTAssertTrue(reading.reason?.contains("collector observation is older than 45 minutes") == true)
    }

    func test_missing_observation_timestamp_remains_unknown() throws {
        let reading = decode(try changingObserved(try json()) { $0.removeValue(forKey: "updatedAt") })
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.collectorAge, 0)
        XCTAssertNil(reading.observationAge)
    }

    // MARK: - Clock skew (RESOURCES-15,  TESTS-12)

    func test_a_source_time_just_inside_the_skew_tolerance_reads_as_now() throws {
        let reading = decode(try json(generatedAt: "2026-11-03T12:31:00Z"))
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.collectorAge, 0)
        XCTAssertTrue(reading.canReviewChanges(at: now))
    }

    func test_a_source_time_just_outside_the_skew_tolerance_is_unknown() throws {
        let reading = decode(try json(generatedAt: "2026-11-03T12:31:01Z"))
        XCTAssertEqual(reading.state, .invalid)
        XCTAssertTrue(reading.reason?.contains("ahead of this Mac's clock") == true)
    }

    func test_a_collector_time_just_inside_the_skew_tolerance_reads_as_now() throws {
        let reading = decode(try changingObserved(try json()) { $0["updatedAt"] = "2026-11-03T12:30:59Z" })
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.observationAge, 0)
    }

    func test_future_collector_timestamp_beyond_the_tolerance_is_unknown() throws {
        let reading = decode(try changingObserved(try json()) { $0["updatedAt"] = "2026-11-03T12:31:01Z" })
        XCTAssertEqual(reading.state, .invalid)
        XCTAssertTrue(reading.reason?.contains("ahead of this Mac's clock") == true)
    }

    func test_the_tolerance_matches_the_other_server_readers() {
        XCTAssertEqual(ResourcePolicyReading.clockSkewTolerance, 60)
    }

    // MARK: - Timestamps as the server writes them (TESTS-10)

    func test_server_timestamps_parse_with_any_fraction_length() throws {
        let whole = try XCTUnwrap(ResourcePolicyReading.parseDate("2026-11-04T19:45:29Z"))
        let cases = [
            "2026-11-04T19:45:29.602Z",
            "2026-11-04T19:45:29.602189Z",
            "2026-11-04T19:45:29.60218900Z",
            "2026-11-04T19:45:29.602189003Z",
            "2026-11-04T19:45:29.602189+00:00",
        ]
        for text in cases {
            let date = try XCTUnwrap(ResourcePolicyReading.parseDate(text), text)
            XCTAssertEqual(date.timeIntervalSince(whole), 0.602, accuracy: 0.001, text)
        }
        XCTAssertNil(ResourcePolicyReading.parseDate("not a time"))
        XCTAssertEqual(ResourcePolicyReading.millisecondPrecision("2026-11-04T19:45:29.602189Z"), "2026-11-04T19:45:29.602Z")
        XCTAssertEqual(ResourcePolicyReading.millisecondPrecision("2026-11-04T19:45:29Z"), "2026-11-04T19:45:29Z")
    }

    func test_a_receipt_with_six_digit_fractions_is_current() throws {
        let data = try json(generatedAt: "2026-11-03T12:29:59.602189Z")
        let reading = decode(try changingObserved(data) { $0["updatedAt"] = "2026-11-03T12:29:58.54450348Z" })
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.collectorAge ?? 0, 0.398, accuracy: 0.002)
    }

    // MARK: - Reports that fail closed

    func test_unsupported_schema_is_unknown() throws {
        let reading = decode(try json(schemaVersion: 2))
        XCTAssertEqual(reading.state, .invalid)
        XCTAssertTrue(reading.reason?.contains("unsupported") == true)
    }

    func test_nonfinite_policy_number_is_unknown() throws {
        let source = String(decoding: try json(), as: UTF8.self)
            .replacingOccurrences(of: "\"ramPressureGiB\":8", with: "\"ramPressureGiB\":1e999")
        let reading = decode(Data(source.utf8))
        XCTAssertEqual(reading.state, .invalid)
    }

    func test_out_of_bounds_desired_values_are_unknown() throws {
        let reading = decode(try json(cpuCores: 15))
        XCTAssertEqual(reading.state, .invalid)
    }

    func test_zero_observed_ram_target_with_running_coding_groups_is_unknown() throws {
        let source = String(decoding: try json(), as: UTF8.self)
            .replacingOccurrences(of: "\"ramTargetBytes\":8589934592", with: "\"ramTargetBytes\":0")
        let reading = decode(Data(source.utf8))
        XCTAssertEqual(reading.state, .invalid)
    }

    func test_zero_ram_target_with_no_coding_groups_is_a_valid_idle_report() throws {
        let data = try json(status: "unavailable")
        let reading = decode(try changingObserved(data) {
            $0["ramTargetBytes"] = 0
            $0["codingGroups"] = 0
        })
        XCTAssertEqual(reading.state, .unavailable)
        XCTAssertNotNil(reading.snapshot)
        XCTAssertFalse(reading.canReviewChanges(at: now))
    }

    func test_a_zero_memory_total_before_the_first_check_is_unknown_not_corrupt() throws {
        let data = try changingObserved(try json(status: "unavailable")) {
            $0["ramTotalBytes"] = 0
            $0["ramCurrentBytes"] = 0
            $0["ramTargetBytes"] = 0
            $0["codingGroups"] = 0
        }
        let reading = decode(data)
        XCTAssertEqual(reading.state, .unavailable)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertNil(snapshot.totalMemoryGiB)
        XCTAssertNil(ResourcePolicyField.ramBudgetGiB.range(in: snapshot))
        XCTAssertEqual(snapshot.desired.ramPressureGiB, 8)
    }

    func test_fractional_saved_values_are_shown_as_they_are() throws {
        let source = String(decoding: try json(), as: UTF8.self)
            .replacingOccurrences(of: "\"reserveGiB\":4", with: "\"reserveGiB\":4.5")
        let reading = decode(Data(source.utf8))
        XCTAssertEqual(reading.state, .current)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(ResourcePolicyField.ramReserveGiB.display(ResourcePolicyField.ramReserveGiB.persistedValue(in: snapshot)), "4.5 GiB")
        XCTAssertNotNil(ResourcePolicyField.ramReserveGiB.validate(value: 4.5, in: snapshot))
    }

    func test_absent_optional_values_remain_unknown_instead_of_defaulting() throws {
        let reading = decode(try json(cpuCores: nil))
        XCTAssertEqual(reading.state, .current)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertNil(snapshot.desired.cpuCores)
        XCTAssertNil(ResourcePolicyField.cpuCores.persistedValue(in: snapshot))
        // RESOURCES-10:  an unknown value is never shown as zero.
        XCTAssertEqual(ResourcePolicyField.cpuCores.display(ResourceDrafts().value(.cpuCores, in: snapshot)), "Unknown")
    }

    func test_applied_report_with_incomplete_measurements_is_unknown() throws {
        let reading = decode(try changingObserved(try json()) { $0.removeValue(forKey: "cpuOnline") })
        XCTAssertEqual(reading.state, .invalid)
        XCTAssertTrue(reading.reason?.contains("no complete measurements") == true)
    }

    func test_unavailable_report_with_incomplete_measurements_keeps_the_saved_values() throws {
        let reading = decode(try changingObserved(try json(status: "unavailable")) { $0.removeValue(forKey: "cpuOnline") })
        XCTAssertEqual(reading.state, .unavailable)
        XCTAssertNil(reading.snapshot?.observed)
        XCTAssertEqual(reading.snapshot?.desired.ramPressureGiB, 8)
        XCTAssertFalse(reading.canReviewChanges(at: now))
    }

    func test_unavailable_report_with_no_online_cpus_keeps_the_saved_values_and_the_reason() throws {
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: try json(status: "unavailable", cpuOnline: 0)) as? [String: Any])
        var report = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        report["reasons"] = ["no-online-cpus"]
        root["resourcePolicy"] = report
        let reading = decode(try JSONSerialization.data(withJSONObject: root))
        XCTAssertEqual(reading.state, .unavailable)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(snapshot.desired.ramPressureGiB, 8)
        XCTAssertEqual(snapshot.desired.cpuCores, 8)
        XCTAssertEqual(snapshot.reasons, ["no-online-cpus"])
        XCTAssertEqual(ResourceLimitsCopy.explanation(snapshot), [ResourceLimitsCopy.reasonSentence("no-online-cpus")])
        XCTAssertNil(ResourcePolicyField.cpuCores.range(in: snapshot))
        // Zero online cores is unknown,  never shown as a count.
        XCTAssertEqual(ResourceLimitsCopy.cpuCoresHelp(snapshot, cores: 8), "The server's core count is unknown.")
        XCTAssertFalse(reading.canReviewChanges(at: now))
    }

    func test_an_applied_report_with_no_online_cpus_is_still_invalid() throws {
        XCTAssertEqual(decode(try json(status: "applied", cpuOnline: 0)).state, .invalid)
    }

    func test_malformed_revision_is_unknown() throws {
        let data = try json()
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var report = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        report["policyRevision"] = "not-a-revision"
        root["resourcePolicy"] = report
        let reading = decode(try JSONSerialization.data(withJSONObject: root))
        XCTAssertEqual(reading.state, .invalid)
    }

    func test_stale_policy_source_cannot_be_reviewed_even_with_a_fresh_receipt() throws {
        let reading = decode(try changingObserved(try json()) { $0["policyStale"] = true })
        XCTAssertEqual(reading.state, .stale)
        XCTAssertFalse(reading.canReviewChanges)
    }

    func test_drift_stays_explicit_while_a_fresh_receipt_can_be_reviewed() throws {
        let reading = decode(try json(status: "drift"))
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.snapshot?.status, "drift")
        XCTAssertTrue(reading.canReviewChanges(at: now))
    }

    func test_unavailable_policy_status_cannot_be_reviewed() throws {
        let reading = decode(try json(status: "unavailable"))
        XCTAssertEqual(reading.state, .unavailable)
        XCTAssertFalse(reading.canReviewChanges)
    }

    func test_the_page_starts_reading_not_unavailable() {
        XCTAssertEqual(ResourcePolicyReading.loading.state, .loading)
        XCTAssertEqual(ResourcePolicyReading.loading.state.title, "Reading The Server")
        XCTAssertNil(ResourcePolicyReading.loading.snapshot)
    }

    // MARK: - Fields,  bounds and names

    func test_field_validation_uses_saved_counterparts_and_says_what_to_change_first() throws {
        let snapshot = try XCTUnwrap(decode(try json()).snapshot)

        XCTAssertNil(ResourcePolicyField.ramBudgetGiB.validate(value: 6, in: snapshot))
        XCTAssertNotNil(ResourcePolicyField.ramCriticalGiB.validate(value: 9, in: snapshot))
        XCTAssertNil(ResourcePolicyField.ramCriticalGiB.validate(value: 7, in: snapshot))
        XCTAssertTrue(ResourcePolicyField.ramBudgetGiB.validate(value: 3, in: snapshot)?.contains("Lower Critical RAM Limit to 3 GiB or less first") == true)
        XCTAssertTrue(ResourcePolicyField.ramCriticalGiB.validate(value: 9, in: snapshot)?.contains("Raise Pressure RAM Limit first") == true)
        XCTAssertFalse(ResourcePolicyField.ramBudgetGiB.validate(value: 3, in: snapshot)?.contains("persisted") == true)
    }

    func test_memory_sliders_stop_where_the_server_stops() throws {
        // 16 GiB server, saved reserve 4, critical limit 4 and pressure limit 8.  The pressure
        // slider starts at the saved critical limit,  because the server refuses anything below.
        let snapshot = try XCTUnwrap(decode(try json()).snapshot)
        let expected: [(ResourcePolicyField, Double, Double)] = [
            (.ramBudgetGiB, 4, 12), (.ramReserveGiB, 2, 8), (.ramCriticalGiB, 1, 8), (.cpuCores, 1, 14),
        ]
        for (field, low, high) in expected {
            let range = field.range(in: snapshot)
            XCTAssertEqual(range?.lowerBound, low, field.rawValue)
            XCTAssertEqual(range?.upperBound, high, field.rawValue)
        }
        XCTAssertNil(ResourcePolicyField.cpuWeight.range(in: snapshot))
        XCTAssertNil(ResourcePolicyField.ramBudgetGiB.validate(value: 12, in: snapshot))
        XCTAssertTrue(ResourcePolicyField.ramBudgetGiB.validate(value: 13, in: snapshot)?.contains("at most 12 GiB") == true)
        XCTAssertNil(ResourcePolicyField.ramReserveGiB.validate(value: 8, in: snapshot))
        XCTAssertTrue(ResourcePolicyField.ramReserveGiB.validate(value: 9, in: snapshot)?.contains("Lower Pressure RAM Limit first") == true)
    }

    func test_borrowing_false_serializes_as_zero_and_true_as_one() throws {
        let snapshot = try XCTUnwrap(decode(try json()).snapshot)
        XCTAssertEqual(ResourcePolicyField.borrowIdle.persistedValue(in: snapshot), 0)
        XCTAssertNil(ResourcePolicyField.borrowIdle.validate(value: 0, in: snapshot))
        XCTAssertNil(ResourcePolicyField.borrowIdle.validate(value: 1, in: snapshot))
        XCTAssertNotNil(ResourcePolicyField.borrowIdle.validate(value: 2, in: snapshot))
        XCTAssertEqual(ResourcePolicyField.borrowIdle.serialized(1), "1")
        XCTAssertEqual(ResourcePolicyField.borrowIdle.serialized(0), "0")
        XCTAssertEqual(ResourcePolicyField.borrowIdle.display(1), "On")
    }

    func test_the_review_sheet_names_the_field_and_its_unit() {
        XCTAssertEqual(ResourcePolicyField.changePhrase(field: "ram-budget-gib", value: "24"), "Set Pressure RAM Limit to 24 GiB")
        XCTAssertEqual(ResourcePolicyField.changePhrase(field: "ram-critical-gib", value: "6"), "Set Critical RAM Limit to 6 GiB")
        XCTAssertEqual(ResourcePolicyField.changePhrase(field: "cpu-cores", value: "8"), "Set CPU Cores to 8 cores")
        XCTAssertEqual(ResourcePolicyField.changePhrase(field: "cpu-weight", value: "50"), "Set CPU Weight to 50")
        XCTAssertEqual(ResourcePolicyField.changePhrase(field: "borrow-idle", value: "1"), "Turn on Borrow Idle Memory")
        XCTAssertEqual(ResourcePolicyField.changePhrase(field: "borrow-idle", value: "0"), "Turn off Borrow Idle Memory")
        let action = OpsAction.serverResource(field: "ram-budget-gib", value: "24", revision: String(repeating: "a", count: 64))
        XCTAssertTrue(action.title.hasPrefix("Set Pressure RAM Limit to 24 GiB "), action.title)
        XCTAssertTrue(action.title.hasSuffix(action.changes.inProse), action.title)
        XCTAssertEqual(action.subjectTitle, "Pressure RAM Limit")
    }

    func test_a_custom_cpu_weight_is_checked_as_typed() {
        XCTAssertEqual(ResourceLimitsCopy.parseCPUWeight("75"), 75)
        XCTAssertEqual(ResourceLimitsCopy.parseCPUWeight(" 10,000 "), 10_000)
        XCTAssertNil(ResourceLimitsCopy.parseCPUWeight("20000"))
        XCTAssertNil(ResourceLimitsCopy.parseCPUWeight("0"))
        XCTAssertNil(ResourceLimitsCopy.parseCPUWeight("1.5"))
        XCTAssertNil(ResourceLimitsCopy.parseCPUWeight(""))
        XCTAssertNil(ResourceLimitsCopy.parseCPUWeight("-5"))
    }

    // MARK: - Drafts (RESOURCES-9)

    func test_an_untouched_field_follows_the_server_and_an_edit_names_a_moved_value() throws {
        let first = try XCTUnwrap(decode(try json()).snapshot)
        var drafts = ResourceDrafts()
        drafts.set(.ramBudgetGiB, to: 10, in: first)
        XCTAssertTrue(drafts.isEdited(.ramBudgetGiB))
        XCTAssertEqual(drafts.value(.ramBudgetGiB, in: first), 10)
        XCTAssertNil(drafts.movedValue(.ramBudgetGiB, in: first))

        // Someone else saves a new pressure limit and reserve.
        let source = String(decoding: try json(), as: UTF8.self)
            .replacingOccurrences(of: "\"ramPressureGiB\":8", with: "\"ramPressureGiB\":9")
            .replacingOccurrences(of: "\"reserveGiB\":4", with: "\"reserveGiB\":3")
        let second = try XCTUnwrap(decode(Data(source.utf8)).snapshot)
        drafts.follow(second)
        XCTAssertEqual(drafts.value(.ramReserveGiB, in: second), 3, "an untouched field shows the new saved value")
        XCTAssertFalse(drafts.isEdited(.ramReserveGiB))
        XCTAssertEqual(drafts.value(.ramBudgetGiB, in: second), 10, "the edit stays")
        XCTAssertEqual(drafts.movedValue(.ramBudgetGiB, in: second), 9)

        drafts.rebase(.ramBudgetGiB, in: second)
        XCTAssertNil(drafts.movedValue(.ramBudgetGiB, in: second))
        drafts.revert(.ramBudgetGiB)
        XCTAssertFalse(drafts.isEdited(.ramBudgetGiB))
        XCTAssertEqual(drafts.value(.ramBudgetGiB, in: second), 9)
    }

    func test_setting_a_field_back_to_the_saved_value_clears_the_edit() throws {
        let snapshot = try XCTUnwrap(decode(try json()).snapshot)
        var drafts = ResourceDrafts()
        drafts.set(.cpuWeight, to: 50, in: snapshot)
        XCTAssertTrue(drafts.isEdited(.cpuWeight))
        drafts.set(.cpuWeight, to: 100, in: snapshot)
        XCTAssertFalse(drafts.isEdited(.cpuWeight))
    }

    func test_an_edit_the_server_now_matches_is_done() throws {
        let first = try XCTUnwrap(decode(try json()).snapshot)
        var drafts = ResourceDrafts()
        drafts.set(.ramBudgetGiB, to: 9, in: first)
        let source = String(decoding: try json(), as: UTF8.self)
            .replacingOccurrences(of: "\"ramPressureGiB\":8", with: "\"ramPressureGiB\":9")
        drafts.follow(try XCTUnwrap(decode(Data(source.utf8)).snapshot))
        XCTAssertFalse(drafts.isEdited(.ramBudgetGiB))
    }

    // MARK: - Words built from live values

    func test_pressure_limit_help_says_it_is_a_floor_while_borrowing() throws {
        let contract = try loadContract()
        let applied = try contract.snapshot("applied")
        let help = ResourceLimitsCopy.pressureHelp(applied, pressure: 8)
        XCTAssertTrue(help.hasPrefix("Coding may use up to 8 GiB right now because Borrow Idle Memory is on."), help)
        XCTAssertTrue(help.contains("Under memory pressure coding is held to 8 GiB, and in critical mode to 6 GiB."), help)
        let off = try decode(try json()).snapshot.map { ResourceLimitsCopy.pressureHelp($0, pressure: 8) }
        XCTAssertTrue(off?.hasPrefix("Borrow Idle Memory is off") == true)
    }

    func test_pressure_limit_help_follows_the_borrowing_draft() throws {
        let applied = try loadContract().snapshot("applied")
        let off = ResourceLimitsCopy.pressureHelp(applied, pressure: 8, borrowing: false)
        XCTAssertTrue(off.hasPrefix("Borrow Idle Memory is off"), off)
        let saved = try decode(try json()).snapshot.map { ResourceLimitsCopy.pressureHelp($0, pressure: 8, borrowing: true) }
        XCTAssertTrue(saved?.hasPrefix("With Borrow Idle Memory on") == true, saved ?? "")
    }

    func test_an_unreadable_saved_policy_cannot_be_reviewed() throws {
        var root = try XCTUnwrap(try JSONSerialization.jsonObject(with: try json()) as? [String: Any])
        var payload = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        payload.removeValue(forKey: "desiredRevision")
        payload["reasons"] = ["saved-policy-unreadable"]
        root["resourcePolicy"] = payload
        let reading = decode(try JSONSerialization.data(withJSONObject: root))
        XCTAssertEqual(reading.state, .current)
        XCTAssertNotNil(reading.snapshot, "the values stay visible, read only")
        XCTAssertNil(reading.snapshot?.reviewRevision)
        XCTAssertFalse(reading.canReviewChanges(at: now))
    }

    func test_cpu_cores_help_counts_the_cores_coding_cannot_use() throws {
        let applied = try loadContract().snapshot("applied")
        XCTAssertTrue(ResourceLimitsCopy.cpuCoresHelp(applied, cores: 12).hasPrefix(
            "Coding runs on the first 12 of the server's 16 cores and cannot use the other 4, even when they are idle."
        ))
    }

    func test_production_protection_is_shown_and_a_lower_value_warns() throws {
        let contract = try loadContract()
        let applied = try contract.snapshot("applied")
        XCTAssertEqual(applied.productionProtectionGiB(), contract.bounds["productionProtectionGiB"])
        let same = try XCTUnwrap(ResourceLimitsCopy.productionLine(applied, pressure: 8, reserve: 6))
        XCTAssertFalse(same.warning)
        XCTAssertTrue(same.text.contains("up to 18 GiB"), same.text)
        let raise = try XCTUnwrap(ResourceLimitsCopy.productionLine(applied, pressure: 24, reserve: 6))
        XCTAssertTrue(raise.warning)
        XCTAssertEqual(raise.text, "Warning: This change leaves production up to 2 GiB of protected memory (now up to 18 GiB).")
    }

    func test_receipt_rows_flag_only_nonzero_problems() throws {
        let data = try changingObserved(try json()) {
            $0["oomKillDelta"] = 2
            $0["availableBytes"] = 4_294_967_296
            $0["excludedUnits"] = 1
        }
        let snapshot = try XCTUnwrap(decode(data).snapshot)
        let rows = ResourceLimitsCopy.receiptRows(snapshot)
        XCTAssertEqual(rows.first(where: { $0.title == "Out-Of-Memory Kills Since The Last Check" })?.kind, .problem)
        XCTAssertEqual(rows.first(where: { $0.title == "Limits That Could Not Be Written" })?.kind, .detail)
        XCTAssertEqual(rows.first(where: { $0.title == "Memory Available" })?.value, "4 GiB of 16 GiB")
        XCTAssertNil(rows.first(where: { $0.title == "Production Services Without Protection" }), "an unmeasured count is not shown as zero")
    }

    func test_every_reason_code_the_server_sends_has_plain_words() {
        let codes = [
            "receipt-missing", "receipt-stale", "receipt-incomplete", "receipt-invalid",
            "saved-policy-unreadable", "policy-not-in-effect", "cpu-values-differ",
            "no-controller-report", "policy-stale", "no-coding-groups", "cpu-controls-unreadable",
            "no-online-cpus", "no-production-groups", "production-unprotected", "cpu-policy-missing",
            "control-write-failures", "controls-suspended", "cpu-mismatch", "memory-limit-mismatch",
        ]
        for code in codes {
            XCTAssertFalse(ResourceLimitsCopy.reasonSentence(code).contains("does not recognize"), code)
        }
        XCTAssertTrue(ResourceLimitsCopy.reasonSentence("brand-new").contains("does not recognize"))
    }

    // MARK: - The contract with the server (RESOURCES-44)

    func test_contract_titles_match_the_server() throws {
        let contract = try loadContract()
        for field in ResourcePolicyField.allCases {
            XCTAssertEqual(contract.titles[field.rawValue], field.title, field.rawValue)
        }
        XCTAssertEqual(contract.titles.count, ResourcePolicyField.allCases.count)
    }

    func test_contract_applied_receipt_is_current_and_bounded_like_the_server() throws {
        let contract = try loadContract()
        let reading = contract.reading("applied")
        XCTAssertEqual(reading.state, .current)
        XCTAssertTrue(reading.canReviewChanges(at: contract.now))
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(snapshot.reviewRevision, snapshot.desiredRevision)
        XCTAssertEqual(ResourcePolicyField.ramBudgetGiB.range(in: snapshot)?.upperBound, contract.bounds["ramBudgetMaxGiB"])
        XCTAssertEqual(ResourcePolicyField.ramReserveGiB.range(in: snapshot)?.upperBound, contract.bounds["ramReserveMaxGiB"])
        XCTAssertEqual(ResourcePolicyField.cpuCores.range(in: snapshot)?.upperBound, contract.bounds["cpuCoresMax"])
        XCTAssertEqual(ResourceLimitsCopy.explanation(snapshot), [])
    }

    func test_contract_saved_policy_ahead_reviews_against_the_saved_revision() throws {
        let contract = try loadContract()
        let reading = contract.reading("savedAhead")
        XCTAssertEqual(reading.state, .current)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(snapshot.status, "drift")
        XCTAssertTrue(snapshot.savedPolicyNotInEffect)
        XCTAssertNotEqual(snapshot.desiredRevision, snapshot.policyRevision)
        XCTAssertEqual(snapshot.reviewRevision, snapshot.desiredRevision)
        let lines = ResourceLimitsCopy.explanation(snapshot)
        XCTAssertEqual(lines.filter { $0.contains("not yet in effect") }.count, 1, "\(lines)")
        XCTAssertTrue(lines.contains("CPU weight 50 is in effect; the saved policy says 100."), "\(lines)")
    }

    func test_contract_missing_receipt_keeps_the_saved_values_read_only() throws {
        let contract = try loadContract()
        let reading = contract.reading("receiptMissing")
        XCTAssertEqual(reading.state, .unavailable)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertNil(snapshot.observed)
        XCTAssertEqual(snapshot.desired.ramPressureGiB, 8)
        XCTAssertFalse(reading.canReviewChanges(at: contract.now))
        XCTAssertTrue(ResourceLimitsCopy.explanation(snapshot).first?.contains("no status receipt") == true)
        XCTAssertEqual(ResourceLimitsCopy.receiptRows(snapshot), [])
    }

    func test_contract_idle_server_is_not_called_corrupt() throws {
        let contract = try loadContract()
        let reading = contract.reading("idle")
        XCTAssertEqual(reading.state, .unavailable)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(snapshot.observed?.codingGroups, 0)
        XCTAssertTrue(ResourceLimitsCopy.explanation(snapshot).contains { $0.contains("No coding services are running") })
    }

    func test_contract_no_online_cpus_keeps_the_saved_values_and_names_the_reason() throws {
        let contract = try loadContract()
        let reading = contract.reading("noOnlineCpus")
        XCTAssertEqual(reading.state, .unavailable)
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(snapshot.observed?.cpuOnline, 0)
        XCTAssertEqual(snapshot.desired.cpuCores, 12)
        XCTAssertEqual(snapshot.desired.ramPressureGiB, 8)
        XCTAssertEqual(snapshot.reasons, ["no-online-cpus"])
        XCTAssertTrue(ResourceLimitsCopy.explanation(snapshot).contains { $0.contains("could not count the server's cores") })
        XCTAssertFalse(reading.canReviewChanges(at: contract.now))
    }

    func test_contract_fractional_timestamps_read_as_current() throws {
        let contract = try loadContract()
        let reading = contract.reading("fractional")
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.collectorAge ?? -1, 0.25, accuracy: 0.002)
    }

    func test_the_server_report_copy_is_read_only_and_names_its_time() throws {
        let contract = try loadContract()
        var report = ServerStatusReading()
        report.resourcePolicy = try contract.snapshot("applied")
        report.generatedAt = contract.now.addingTimeInterval(-120)
        let reading = try XCTUnwrap(ResourcePolicyReading.fromServerReport(report, directFailure: "The server did not answer, so nothing ran.", now: contract.now))
        XCTAssertEqual(reading.state, .fromReport)
        XCTAssertNotNil(reading.snapshot)
        XCTAssertFalse(reading.canReviewChanges(at: contract.now))
        XCTAssertTrue(reading.reason?.hasPrefix("The server did not answer, so nothing ran.\u{00A0} Showing the server report from ") == true, reading.reason ?? "")
        XCTAssertNil(ResourcePolicyReading.fromServerReport(ServerStatusReading(), directFailure: nil, now: contract.now))
    }

    func test_a_pulled_report_summary_never_states_a_present_success() throws {
        let contract = try loadContract()
        var report = ServerStatusReading()
        report.resourcePolicy = try contract.snapshot("applied")
        report.generatedAt = contract.now.addingTimeInterval(-3 * 3600)
        let reading = try XCTUnwrap(ResourcePolicyReading.fromServerReport(report, directFailure: nil, now: contract.now))
        let snapshot = try XCTUnwrap(reading.snapshot)
        XCTAssertEqual(snapshot.status, "applied")
        let summary = ResourceLimitsCopy.summary(snapshot, state: reading.state, at: reading.reportTime)
        XCTAssertFalse(summary.contains("are in effect"), summary)
        XCTAssertFalse(summary.contains("is normal"), summary)
        XCTAssertTrue(summary.hasPrefix("The last report at \(FleetTime.clock(contract.now.addingTimeInterval(-3 * 3600))) said these limits were in effect."), summary)
    }

    func test_a_stale_direct_read_summary_is_qualified_and_a_current_one_is_not() throws {
        let contract = try loadContract()
        let snapshot = try contract.snapshot("applied")
        XCTAssertTrue(ResourceLimitsCopy.summary(snapshot).hasPrefix("These limits are in effect."))
        XCTAssertTrue(ResourceLimitsCopy.summary(snapshot, state: .current).hasPrefix("These limits are in effect."))
        let stale = ResourceLimitsCopy.summary(snapshot, state: .stale, at: nil)
        XCTAssertFalse(stale.contains("are in effect"), stale)
        XCTAssertTrue(stale.hasPrefix("The last report said these limits were in effect."), stale)
    }

    // MARK: - Helpers

    private struct Contract {
        let now: Date
        let titles: [String: String]
        let bounds: [String: Double]
        let scenarios: [String: Any]

        func reading(_ name: String) -> ResourcePolicyReading {
            guard let scenario = scenarios[name],
                  let data = try? JSONSerialization.data(withJSONObject: scenario) else {
                return .unavailable("missing scenario \(name)")
            }
            return ResourcePolicyReading.decode(data, now: now, directReceiptAt: now)
        }

        func snapshot(_ name: String) throws -> ResourcePolicySnapshot {
            try XCTUnwrap(reading(name).snapshot, name)
        }
    }

    /// The broker's own answers,  written by scripts/test_fleet_resource_control.py.
    private func loadContract() throws -> Contract {
        let text = try fixture("resource_policy_contract", "json")
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any])
        let now = try XCTUnwrap((root["now"] as? String).flatMap(ResourcePolicyReading.parseDate))
        let titles = try XCTUnwrap(root["fieldTitles"] as? [String: String])
        let rawBounds = try XCTUnwrap(root["bounds"] as? [String: Any])
        var bounds: [String: Double] = [:]
        for (key, value) in rawBounds {
            if let number = value as? NSNumber { bounds[key] = number.doubleValue }
        }
        let scenarios = try XCTUnwrap(root["scenarios"] as? [String: Any])
        return Contract(now: now, titles: titles, bounds: bounds, scenarios: scenarios)
    }

    private func changingObserved(_ data: Data, _ change: (inout [String: Any]) -> Void) throws -> Data {
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var report = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        var observed = try XCTUnwrap(report["observed"] as? [String: Any])
        change(&observed)
        report["observed"] = observed
        root["resourcePolicy"] = report
        return try JSONSerialization.data(withJSONObject: root)
    }

    private func json(
        schemaVersion: Int = 1,
        generatedAt: String = "2026-11-03T12:30:00Z",
        status: String = "applied",
        cpuOnline: Int = 16,
        cpuCores: Int? = 8
    ) throws -> Data {
        var desired: [String: Any] = [
            "cpuWeight": 100,
            "ramPressureGiB": 8,
            "ramCriticalGiB": 4,
            "reserveGiB": 4,
            "allowBorrowing": false
        ]
        if let cpuCores { desired["cpuCores"] = cpuCores }
        var observed: [String: Any] = [
            "cpuWeight": 100,
            "cpuGroups": 4,
            "cpuMismatches": 0,
            "ramTotalBytes": 17_179_869_184,
            "ramCurrentBytes": 5_368_709_120,
            "ramTargetBytes": 8_589_934_592,
            "mode": "normal",
            "codingGroups": 4,
            "productionGroups": 2,
            "controlWriteFailures": 0,
            "controlSuspended": 0,
            "policyStale": false,
            "actualHighMismatches": 0,
            "updatedAt": "2026-11-03T12:25:00Z"
        ]
        observed["cpuOnline"] = cpuOnline
        if let cpuCores { observed["cpuCores"] = cpuCores }
        let revision = String(repeating: "a", count: 64)
        let payload: [String: Any] = [
            "schemaVersion": schemaVersion,
            "generatedAt": generatedAt,
            "policyRevision": revision,
            "desiredRevision": revision,
            "desired": desired,
            "observed": observed,
            "status": status,
            "reasons": status == "applied" ? [] : ["cpu-mismatch"]
        ]
        return try JSONSerialization.data(withJSONObject: ["resourcePolicy": payload])
    }

    private func decode(_ data: Data, receiptAge: TimeInterval = 0) -> ResourcePolicyReading {
        ResourcePolicyReading.decode(data, now: now, directReceiptAt: now.addingTimeInterval(-receiptAge))
    }
}
