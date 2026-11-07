import SwiftUI

/// The filter row on Apps & Work:  the App Kind choice, the filter field and Needs Attention
/// Only.
///
/// The segmented App Kind control is as wide as its four labels need, about 480 pt, and an
/// AppKit segmented control never draws narrower than its labels.  When the row capped it at
/// 460 pt, it drew past that frame on both sides:  its first segment was cut at the pane's
/// left edge, and its last ran under the filter field (owner's screenshot, November 7, 2026).
/// So the control always gets its natural width, and when the three controls do not fit on
/// one line, the field and the checkbox move to a second line under it.
///
/// It takes a binding and no store, so the test bundle compiles it (FleetViewFilterBarTests).
struct FleetViewFilterBar: View {
    @Binding var filter: FleetViewFilter

    /// The filter field never gets narrower than this, so its placeholder stays readable.
    static let fieldMinWidth: CGFloat = 220
    static let fieldMaxWidth: CGFloat = 340
    static let spacing: CGFloat = 12

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: Self.spacing) {
                kindPicker
                field
                attentionToggle
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: 8) {
                kindPicker
                HStack(spacing: Self.spacing) {
                    field
                    attentionToggle
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private var kindPicker: some View {
        Picker("App Kind", selection: $filter.kind) {
            ForEach(FleetAppKindFilter.allCases) { item in
                Text(item.rawValue).tag(item)
            }
        }
        .pickerStyle(.segmented)
        // The segmented options name themselves;  a visible label was squeezed into a 16 pt
        // column and wrapped letter by letter beside them (AFC-592).  The title stays as the
        // accessibility label.
        .labelsHidden()
        // Its natural width, never a cap:  see the type's comment.
        .fixedSize()
    }

    private var field: some View {
        TextField("Filter apps, tools, seats, lanes, pull requests, or claims\u{2026}", text: $filter.query)
            .textFieldStyle(.roundedBorder)
            .font(.system(size: 11, design: .monospaced))
            .frame(minWidth: Self.fieldMinWidth, idealWidth: Self.fieldMaxWidth, maxWidth: Self.fieldMaxWidth)
    }

    private var attentionToggle: some View {
        Toggle("Needs Attention Only", isOn: $filter.attentionOnly)
            .toggleStyle(.checkbox)
            .fixedSize()
            .help("Shows only what needs someone:  pull requests that need action, expired claims, lanes git could not read, and tools that are missing or behind.")
    }
}
