import SwiftUI

/// The sizes the main window keeps for itself, apart from any panel's content.
///
/// It holds no environment objects, so the unit-test bundle compiles it and can host the same
/// layout the window uses (`MusterWindowLayoutTests`).
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
}

extension View {
    /// A split-view column's height comes from the window, never from its content.  The
    /// split view keeps a column's natural height as a size it resists going under, and a
    /// ScrollView's natural height is its whole content, so without this the split view grew
    /// past the window and its top and bottom could not be reached.
    func musterColumnHeight() -> some View {
        frame(minHeight: 0, idealHeight: MusterWindowLayout.columnIdealHeight,
              maxHeight: .infinity, alignment: .top)
    }

    /// The window's own size:  the window's minimum, never a panel's natural height.
    func musterWindowSize() -> some View {
        frame(minWidth: MusterWindowLayout.minWidth, idealWidth: MusterWindowLayout.minWidth,
              maxWidth: .infinity,
              minHeight: MusterWindowLayout.minHeight, idealHeight: MusterWindowLayout.idealHeight,
              maxHeight: .infinity)
    }
}
