import SwiftUI

/// The focused-scene value that lets the Go menu open Quick Open in the key window.
private struct QuickOpenShownKey: FocusedValueKey {
    typealias Value = Binding<Bool>
}

extension FocusedValues {
    var quickOpenShown: Binding<Bool>? {
        get { self[QuickOpenShownKey.self] }
        set { self[QuickOpenShownKey.self] = newValue }
    }
}

/// Command-K:  type a panel's name or a word that goes with it, and Return opens it.
///
/// The counts are the sidebar's own badges, handed in, so this view reads nothing.
struct QuickOpenView: View {
    let attention: [MusterDestination: String]
    let onChoose: (MusterDestination) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var highlighted = 0
    @FocusState private var fieldFocused: Bool

    private var results: [MusterDestination] { QuickOpenSearch.results(query) }

    var body: some View {
        VStack(spacing: 0) {
            TextField("Go To A Panel", text: $query)
                .textFieldStyle(.plain)
                .font(.title3)
                .padding(14)
                .focused($fieldFocused)
                // On the field itself, which holds the focus, so the arrows move the highlight.
                .onKeyPress(.downArrow) {
                    highlighted = min(highlighted + 1, max(results.count - 1, 0))
                    return .handled
                }
                .onKeyPress(.upArrow) {
                    highlighted = max(highlighted - 1, 0)
                    return .handled
                }
                .onSubmit { choose(highlighted) }
                .accessibilityLabel("Go To A Panel")
            Divider()
            if results.isEmpty {
                Text("No panel matches.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollViewReader { proxy in
                    List {
                        ForEach(Array(results.enumerated()), id: \.element) { index, destination in
                            row(destination, selected: index == highlighted)
                                .id(destination)
                                .contentShape(Rectangle())
                                .onTapGesture { choose(index) }
                        }
                    }
                    .listStyle(.plain)
                    .onChange(of: highlighted) { _, index in
                        if results.indices.contains(index) { proxy.scrollTo(results[index]) }
                    }
                }
            }
        }
        .frame(width: 460, height: 380)
        .onAppear { fieldFocused = true }
        .onChange(of: query) { _, _ in highlighted = 0 }
        .onExitCommand { dismiss() }
    }

    private func row(_ destination: MusterDestination, selected: Bool) -> some View {
        HStack {
            Label(destination.title, systemImage: destination.systemImage)
            Text(destination.subtitle)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            if let count = attention[destination] {
                Text(count)
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(Color.secondary.opacity(0.2)))
            }
            // No Command-digit hint here: the Go menu's digits wait while this sheet is up.
        }
        .padding(.vertical, 2)
        .listRowBackground(selected ? Color.accentColor.opacity(0.18) : Color.clear)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func choose(_ index: Int) {
        guard results.indices.contains(index) else { return }
        onChoose(results[index])
        dismiss()
    }
}
