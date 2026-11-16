import XCTest

final class ZulipChannelDisplayNameTests: XCTestCase {
    func testTheDefaultChannelReadsWithItsCapitals() {
        XCTAssertEqual(FleetRegistrySummary().zulipChannelDisplayName, "Work-In-Progress")
    }

    func testOtherSlugsAndAnEmptyValue() {
        XCTAssertEqual(FleetRegistrySummary(zulipChannel: "policy").zulipChannelDisplayName, "Policy")
        XCTAssertEqual(FleetRegistrySummary(zulipChannel: "Already-Named").zulipChannelDisplayName, "Already-Named")
        XCTAssertEqual(FleetRegistrySummary(zulipChannel: "").zulipChannelDisplayName, "")
    }
}
