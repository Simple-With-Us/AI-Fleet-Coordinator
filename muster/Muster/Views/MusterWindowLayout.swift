import SwiftUI

/// The sizes the main window keeps for itself, apart from any panel's content.
///
/// It holds no environment objects, so the unit-test bundle compiles it and can host the same
/// size modifiers the window uses (`MusterWindowLayoutTests`).
enum MusterWindowLayout {
    /// Wide enough for every panel's columns and controls, which overlapped each other in a
    /// narrower window (Controls' tabs, the Apps & Work filter bar).
    static let minWidth: CGFloat = 1160
    /// Short enough for any laptop screen.
    static let minHeight: CGFloat = 560
    static let idealHeight: CGFloat = 780
    /// A column's natural height.  It stays under the window's minimum less the toolbar,
    /// so no column can ask the window for more room than that minimum.
    static let columnIdealHeight: CGFloat = 400

    private static let splitViewFramesPrefix = "NSSplitView Subview Frames "

    /// The saved split-view column frames that are taller than the window they belong to.
    ///
    /// AppKit saves the split view's column frames in the app's defaults, under
    /// `NSSplitView Subview Frames <window>, <split view>`.  On the owner's Mac (read
    /// November 6, 2026) the window's saved frame was 760 pt tall but its columns were saved
    /// at 1085 pt:  exactly the height the split view took while it overflowed (PR 1442), so
    /// the frames were most likely saved by the bug, not its cause.  Seeding them in PR 1442's
    /// harness changed nothing.  A column is never meant to be taller than its window, so
    /// such a frame is stale, and forgetting it only clears what older builds saved.  The fix
    /// is the column cap (`musterColumnHeight`).
    ///
    /// - Parameters:
    ///   - saved: the app's defaults, as `UserDefaults.dictionaryRepresentation()` gives them.
    ///   - screenHeight: the tallest screen's height, the limit for a split view whose window
    ///     has no saved frame.  With neither, nothing is called stale.
    static func oversizedSplitViewFrameKeys(in saved: [String: Any], screenHeight: CGFloat?) -> [String] {
        var keys: [String] = []
        for (key, value) in saved where key.hasPrefix(splitViewFramesPrefix) {
            guard let frames = value as? [String] else { continue }
            let heights = frames.compactMap { frame -> CGFloat? in
                let parts = frame.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                guard parts.count >= 4, let height = Double(parts[3]) else { return nil }
                return CGFloat(height)
            }
            guard let tallest = heights.max() else { continue }
            let names = String(key.dropFirst(splitViewFramesPrefix.count))
            let windowName = names.components(separatedBy: ", ").first ?? names
            guard let limit = savedWindowHeight(saved["NSWindow Frame \(windowName)"]) ?? screenHeight else {
                continue
            }
            if tallest > limit + 1 {
                keys.append(key)
            }
        }
        return keys.sorted()
    }

    /// Removes every saved split-view frame taller than its window
    /// (`oversizedSplitViewFrameKeys`), before the window opens, so AppKit cannot restore it.
    /// The split view then starts at the window's height, and only the sidebar's saved width
    /// is lost.  Returns the keys it removed.
    @discardableResult
    static func forgetOversizedSplitViewFrames(in defaults: UserDefaults, screenHeight: CGFloat?) -> [String] {
        let keys = oversizedSplitViewFrameKeys(in: defaults.dictionaryRepresentation(), screenHeight: screenHeight)
        for key in keys {
            defaults.removeObject(forKey: key)
        }
        return keys
    }

    /// The height in a saved `NSWindow Frame` value:  "x y width height" and then the screen's
    /// frame, all separated by spaces.
    private static func savedWindowHeight(_ value: Any?) -> CGFloat? {
        guard let text = value as? String else { return nil }
        let numbers = text.split(separator: " ").compactMap { Double($0) }
        guard numbers.count >= 4 else { return nil }
        return CGFloat(numbers[3])
    }
}

extension View {
    /// A split-view column's height comes from the window, never from its content.
    ///
    /// This is the fix for the owner's November 6 report (AFC-616), where both columns sat
    /// under the title bar.  Text that wraps with a vertical `fixedSize` outside a panel's
    /// ScrollView (the place strip's line, a failure banner's reason) is measured at zero
    /// width, wraps about one character per line, and claims a huge minimum:  1032 pt for
    /// Seat Setup's strip line.  Uncapped, the column passed that minimum to the split view,
    /// which grew past the window (PR 1442 measured 1085 pt in a 760-pt window).  The cap
    /// makes each column's minimum zero.  It is load-bearing:  without it even the
    /// top-aligned root runs the split view off the bottom (`MusterWindowLayoutTests`).
    func musterColumnHeight() -> some View {
        frame(minHeight: 0, idealHeight: MusterWindowLayout.columnIdealHeight,
              maxHeight: .infinity, alignment: .top)
    }

    /// The window's own size:  the window's minimum, never a panel's natural height.  Its
    /// minimum and maximum make it report the window's size whatever the split view inside
    /// asks for, and it sets the window's minimum through `.windowResizability(.contentMinSize)`.
    ///
    /// It aligns the split view to the top, so if the split view still ends up taller than
    /// the window, the overflow goes off the bottom and the columns' first rows and the
    /// toolbar's title stay where they belong.  Centred, as the earlier root placed it, the
    /// overflow went off the top as well, under the title bar.  This alone does not keep the
    /// columns inside the window;  the column cap does.
    func musterWindowSize() -> some View {
        frame(minWidth: MusterWindowLayout.minWidth, idealWidth: MusterWindowLayout.minWidth,
              maxWidth: .infinity,
              minHeight: MusterWindowLayout.minHeight, idealHeight: MusterWindowLayout.idealHeight,
              maxHeight: .infinity, alignment: .top)
    }
}
