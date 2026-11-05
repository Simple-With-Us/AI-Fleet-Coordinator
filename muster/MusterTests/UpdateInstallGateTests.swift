import XCTest

/// The update never relaunches Muster while something it started is still running.
@MainActor
final class UpdateInstallGateTests: XCTestCase {
    private final class World {
        var active = false
        var applying = false
        var operating = false
        var installs = 0
    }

    private func makeGate(_ world: World) -> UpdateInstallGate {
        UpdateInstallGate(appIsActive: { world.active }, probes: [
            { world.applying ? UpdateGateCopy.applyRunning : nil },
            { world.operating ? UpdateGateCopy.operationRunning : nil },
        ])
    }

    func testAnIdleAppInTheBackgroundInstallsAtOnce() {
        let world = World()
        let gate = makeGate(world)
        gate.holdAutomaticInstall { world.installs += 1 }
        XCTAssertEqual(world.installs, 1)
        XCTAssertFalse(gate.hasPendingInstall)
        XCTAssertEqual(gate.heldBy, [])
    }

    func testAnApplyInFlightHoldsTheInstallUntilItEnds() {
        let world = World()
        world.applying = true
        let gate = makeGate(world)
        gate.holdAutomaticInstall { world.installs += 1 }
        XCTAssertEqual(world.installs, 0)
        XCTAssertTrue(gate.hasPendingInstall)
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.applyRunning])

        // Something else changed,  and the apply is still running.
        gate.reevaluate()
        XCTAssertEqual(world.installs, 0)

        world.applying = false
        gate.reevaluate()
        XCTAssertEqual(world.installs, 1)
        XCTAssertFalse(gate.hasPendingInstall)

        // The install ran once,  and a later look runs nothing again.
        gate.reevaluate()
        XCTAssertEqual(world.installs, 1)
    }

    func testEveryRunningThingIsNamedAndAllMustEnd() {
        let world = World()
        world.applying = true
        world.operating = true
        let gate = makeGate(world)
        gate.holdAutomaticInstall { world.installs += 1 }
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.applyRunning, UpdateGateCopy.operationRunning])
        world.applying = false
        gate.reevaluate()
        XCTAssertEqual(world.installs, 0)
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.operationRunning])
        world.operating = false
        gate.reevaluate()
        XCTAssertEqual(world.installs, 1)
    }

    func testTheAutomaticInstallWaitsWhileMusterIsInFront() {
        let world = World()
        world.active = true
        let gate = makeGate(world)
        gate.holdAutomaticInstall { world.installs += 1 }
        XCTAssertEqual(world.installs, 0)
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.inUse])
        world.active = false
        gate.reevaluate()
        XCTAssertEqual(world.installs, 1)
    }

    /// The owner chose Install And Relaunch,  so being in front does not hold it,  and work
    /// in flight still does.
    func testARelaunchTheOwnerAskedForWaitsOnlyForWork() {
        let world = World()
        world.active = true
        let gate = makeGate(world)
        XCTAssertFalse(gate.shouldPostponeRelaunch { world.installs += 1 })
        XCTAssertEqual(world.installs, 0, "Sparkle relaunches itself when it is not postponed")

        world.operating = true
        XCTAssertTrue(gate.shouldPostponeRelaunch { world.installs += 1 })
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.operationRunning])
        gate.reevaluate()
        XCTAssertEqual(world.installs, 0)
        world.operating = false
        gate.reevaluate()
        XCTAssertEqual(world.installs, 1)
    }

    /// A relaunch that arrives while an automatic install waits keeps both:  each runs once
    /// its own blockers clear,  and neither drops the other.
    func testAnAutomaticInstallAndARelaunchKeepTheirOwnSlots() {
        let world = World()
        world.active = true
        let gate = makeGate(world)
        var automatic = 0
        var relaunch = 0
        gate.holdAutomaticInstall { automatic += 1 }
        world.operating = true
        XCTAssertTrue(gate.shouldPostponeRelaunch { relaunch += 1 })
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.operationRunning])

        world.operating = false
        gate.reevaluate()
        XCTAssertEqual(relaunch, 1)
        XCTAssertEqual(automatic, 0, "Muster is still in front")
        XCTAssertTrue(gate.hasPendingInstall)
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.inUse])

        world.active = false
        gate.reevaluate()
        XCTAssertEqual(automatic, 1)
        XCTAssertEqual(relaunch, 1)
        XCTAssertFalse(gate.hasPendingInstall)
    }

    // MARK: - Muster's own owners of work

    /// An engine that holds every call until the test lets it go.
    private final class HeldEngine: FleetConfigRunning, @unchecked Sendable {
        private let lock = NSLock()
        private var waiting: [CheckedContinuation<Void, Never>] = []

        var isHolding: Bool { lock.lock(); defer { lock.unlock() }; return !waiting.isEmpty }

        func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                lock.lock()
                waiting.append(continuation)
                lock.unlock()
            }
            return FleetConfigCLIResult(exitCode: 2, stdout: "", stderr: "refused")
        }

        func release() {
            lock.lock()
            let all = waiting
            waiting = []
            lock.unlock()
            all.forEach { $0.resume() }
        }
    }

    private func waitUntil(_ condition: () -> Bool, file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<500 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("The condition was never met.", file: file, line: line)
    }

    func testARealApplyCoordinatorHoldsTheInstallWhileItsPlanRuns() async throws {
        let engine = HeldEngine()
        let apply = ApplyCoordinator(runner: engine)
        let ops = OpsController(runner: OpsProcessRunner(), auditURL: nil, checksInstall: false)
        let gate = UpdateInstallGate(appIsActive: { false },
                                     probes: UpdateInstallGate.musterWork(apply: apply, ops: ops, router: nil))
        var installs = 0

        apply.beginPlan(seat: "CLAUDE", ruleID: "claude-hooks-lane-guard", kind: "hooks")
        try await waitUntil { engine.isHolding }
        XCTAssertTrue(apply.hasWorkInFlight)

        gate.holdAutomaticInstall { installs += 1 }
        XCTAssertEqual(installs, 0)
        XCTAssertEqual(gate.heldBy, [UpdateGateCopy.applyRunning])

        engine.release()
        try await waitUntil { !apply.hasWorkInFlight }
        gate.reevaluate()
        XCTAssertEqual(installs, 1)
    }

    func testIdleOwnersOfWorkHoldNothing() {
        let apply = ApplyCoordinator(runner: HeldEngine())
        let ops = OpsController(runner: OpsProcessRunner(), auditURL: nil, checksInstall: false)
        let probes = UpdateInstallGate.musterWork(apply: apply, ops: ops, router: nil)
        XCTAssertEqual(probes.compactMap { $0() }, [])
    }
}
