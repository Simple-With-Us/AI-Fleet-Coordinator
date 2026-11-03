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
        XCTAssertEqual(reading.snapshot?.observed.cpuWeight, 50)
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
        let reading = decode(try json(generatedAt: "2026-11-03T11:40:00Z"))
        XCTAssertEqual(reading.state, .current)
        XCTAssertTrue(reading.canReviewChanges(at: now))
        XCTAssertEqual(reading.collectorAge, 3_000)
    }

    func test_future_receipt_is_unknown() throws {
        let reading = decode(try json(generatedAt: "2026-11-03T12:30:01Z"))
        XCTAssertEqual(reading.state, .invalid)
        XCTAssertTrue(reading.reason?.contains("future") == true)
    }

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

    func test_absent_optional_values_remain_unknown_instead_of_defaulting() throws {
        let reading = decode(try json(cpuCores: nil))
        XCTAssertEqual(reading.state, .current)
        XCTAssertNil(reading.snapshot?.desired.cpuCores)
        XCTAssertNil(ResourcePolicyField.cpuCores.persistedValue(in: try XCTUnwrap(reading.snapshot)))
    }

    func test_missing_required_observation_is_unknown() throws {
        let data = try json()
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var report = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        var observed = try XCTUnwrap(report["observed"] as? [String: Any])
        observed.removeValue(forKey: "cpuOnline")
        report["observed"] = observed
        root["resourcePolicy"] = report
        let reading = decode(try JSONSerialization.data(withJSONObject: root))
        XCTAssertEqual(reading.state, .invalid)
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
        let data = try json()
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var report = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        var observed = try XCTUnwrap(report["observed"] as? [String: Any])
        observed["policyStale"] = true
        report["observed"] = observed
        root["resourcePolicy"] = report
        let reading = decode(try JSONSerialization.data(withJSONObject: root))
        XCTAssertEqual(reading.state, .stale)
        XCTAssertFalse(reading.canReviewChanges)
    }

    func test_future_collector_timestamp_is_unknown() throws {
        let data = try json()
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var report = try XCTUnwrap(root["resourcePolicy"] as? [String: Any])
        var observed = try XCTUnwrap(report["observed"] as? [String: Any])
        observed["updatedAt"] = "2026-11-03T12:30:01Z"
        report["observed"] = observed
        root["resourcePolicy"] = report
        let reading = decode(try JSONSerialization.data(withJSONObject: root))
        XCTAssertEqual(reading.state, .invalid)
        XCTAssertTrue(reading.reason?.contains("future") == true)
    }

    func test_drift_stays_explicit_while_a_fresh_receipt_can_be_reviewed() throws {
        let reading = decode(try json(status: "drift"))
        XCTAssertEqual(reading.state, .current)
        XCTAssertEqual(reading.snapshot?.status, "drift")
        XCTAssertTrue(reading.canReviewChanges(at: now))
    }

    func test_field_validation_uses_persisted_counterparts_not_drafts() throws {
        let snapshot = try XCTUnwrap(decode(try json()).snapshot)

        XCTAssertNil(ResourcePolicyField.ramBudgetGiB.validate(value: 6, in: snapshot))
        XCTAssertNotNil(ResourcePolicyField.ramCriticalGiB.validate(value: 9, in: snapshot))
        XCTAssertNil(ResourcePolicyField.ramCriticalGiB.validate(value: 7, in: snapshot))
        XCTAssertTrue(ResourcePolicyField.ramBudgetGiB.validate(value: 3, in: snapshot)?.contains("persisted critical") == true)
    }

    func test_borrowing_false_serializes_as_zero_and_true_as_one() throws {
        let snapshot = try XCTUnwrap(decode(try json()).snapshot)
        XCTAssertEqual(ResourcePolicyField.borrowIdle.persistedValue(in: snapshot), 0)
        XCTAssertNil(ResourcePolicyField.borrowIdle.validate(value: 0, in: snapshot))
        XCTAssertNil(ResourcePolicyField.borrowIdle.validate(value: 1, in: snapshot))
        XCTAssertNotNil(ResourcePolicyField.borrowIdle.validate(value: 2, in: snapshot))
    }

    func test_unavailable_policy_status_cannot_be_reviewed() throws {
        let reading = decode(try json(status: "unavailable"))
        XCTAssertEqual(reading.state, .unavailable)
        XCTAssertFalse(reading.canReviewChanges)
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
            "cpuWeight": 50,
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
        let payload: [String: Any] = [
            "schemaVersion": schemaVersion,
            "generatedAt": generatedAt,
            "policyRevision": String(repeating: "a", count: 64),
            "desired": desired,
            "observed": observed,
            "status": status
        ]
        return try JSONSerialization.data(withJSONObject: ["resourcePolicy": payload])
    }

    private func decode(_ data: Data, receiptAge: TimeInterval = 0) -> ResourcePolicyReading {
        ResourcePolicyReading.decode(data, now: now, directReceiptAt: now.addingTimeInterval(-receiptAge))
    }
}
