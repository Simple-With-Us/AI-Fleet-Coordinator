import XCTest
/// The grid's ages:  one shared check time,  and a cell that is the exception.
///
/// The engine checks every rule in one pass,  so every cell carried the same age and
/// the grid read as ninety copies of one number.  The shared time goes beside the
/// table once,  and a cell shows its own age only when it was checked apart from the
/// rest,  which is a rule served from the cache.
final class CommonCheckTimeTests: XCTestCase {
    /// A result with a given check time.
    private func result(_ ruleId: String, checkedAt: String) -> CheckResult {
        CheckResult(
            ruleId: ruleId,
            seat: "MM",
            platform: "minimax",
            kind: "instructions",
            state: "PASS",
            reason: "ok",
            reasonCode: "ok",
            expected: [:],
            observed: [:],
            proofLevelRequired: "L0",
            proofLevelAchieved: "L0",
            collector: "mac",
            checkType: "marked_block_hash",
            checkedAt: checkedAt,
            expiresAt: nil,
            ttlSeconds: 900,
            stale: false,
            failing: false
        )
    }

    /// The time most rules carry is the run's own time.
    func test_the_shared_check_time_is_the_one_most_rules_carry() {
        let results = [
            result("a", checkedAt: "2026-10-21T08:59:27Z"),
            result("b", checkedAt: "2026-10-21T08:59:27Z"),
            result("c", checkedAt: "2026-10-21T08:59:27Z"),
            result("d", checkedAt: "2026-10-21T08:41:02Z"),
        ]
        let common = FleetStore.commonCheckTime(in: results)
        XCTAssertEqual(common, FleetTime.parse("2026-10-21T08:59:27Z"))
    }

    /// A run where every rule was checked at the same moment has one time.
    func test_a_single_check_time_is_the_shared_one() {
        let results = [
            result("a", checkedAt: "2026-10-21T08:59:27Z"),
            result("b", checkedAt: "2026-10-21T08:59:27Z"),
        ]
        XCTAssertEqual(
            FleetStore.commonCheckTime(in: results),
            FleetTime.parse("2026-10-21T08:59:27Z")
        )
    }

    /// A run with no usable time has no shared time,  and the grid says nothing.
    func test_a_run_with_no_check_time_has_no_shared_time() {
        XCTAssertNil(FleetStore.commonCheckTime(in: []))
    }
}
