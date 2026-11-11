import AppKit
import SwiftUI

// The views that keep a report's age and verdict live while a panel stays open.
//
// The words and the verdict come from `ReportFreshness`,  which is plain model code with
// tests.  These views only ask it again:  `ReportAgeLine` on a thirty second tick,  and
// `rereadsOnTickAndWake` reads the panel's files again every minute and as soon as the Mac
// wakes,  because a timer does not run while the Mac sleeps.

/// Reads a panel's files when it appears,  again on a slow tick,  and again when the Mac wakes.
private struct RereadsOnTickAndWake: ViewModifier {
    let every: TimeInterval
    let onWake: (@MainActor () -> Void)?
    let action: @MainActor () async -> Void

    func body(content: Content) -> some View {
        content
            .task {
                await action()
                while !Task.isCancelled {
                    // Low Power Mode reads less often:  these reads can wait.
                    let seconds = RereadSchedule.interval(
                        base: every, lowPower: ProcessInfo.processInfo.isLowPowerModeEnabled)
                    try? await Task.sleep(nanoseconds: UInt64(max(seconds, 1) * 1_000_000_000))
                    if Task.isCancelled { break }
                    await action()
                }
            }
            .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)) { _ in
                onWake?()
                Task { await action() }
            }
    }
}

extension View {
    /// Reads the panel's files when it appears,  every `every` seconds while it stays open,
    /// and when the Mac wakes.  `onWake` runs first on a wake,  so a panel can mark what it
    /// holds as from before the sleep until the new read lands.
    func rereadsOnTickAndWake(every: TimeInterval = ReportFreshness.rereadInterval,
                              onWake: (@MainActor () -> Void)? = nil,
                              _ action: @escaping @MainActor () async -> Void) -> some View {
        modifier(RereadsOnTickAndWake(every: every, onWake: onWake, action: action))
    }
}

/// A report's age line and,  when it is not current,  what that means,  worked out again on
/// every tick.
struct ReportAgeLine: View {
    let origin: ReportOrigin
    let generatedAt: Date?
    var copiedAt: Date?
    let maxAge: TimeInterval
    var readAt: Date?
    var wokeAt: Date?

    var body: some View {
        TimelineView(.periodic(from: .now, by: ReportFreshness.tickInterval)) { context in
            ReportAgeText(origin: origin,
                          freshness: ReportFreshness(generatedAt: generatedAt, copiedAt: copiedAt, maxAge: maxAge,
                                                     now: context.date, readAt: readAt, wokeAt: wokeAt))
        }
    }
}

/// The age line and its note for one freshness.  A panel whose clock already ticks,  such as
/// Server Status on `PlaceWatch`,  draws this directly so its words and its verdict share one
/// moment.
struct ReportAgeText: View {
    let origin: ReportOrigin
    let freshness: ReportFreshness

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if let line = ReportFreshnessCopy.ageLine(origin, freshness) {
                Text(line)
                    .font(.caption)
                    .foregroundStyle(freshness.verdict.isCurrent ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let note = ReportFreshnessCopy.verdictNote(origin, freshness) {
                Text(note)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
