import SwiftUI

/// The controls surface:  every variable that governs a script or a background process.
///
/// It shows what the knob is,  who owns it,  what it may be,  which way it is allowed to
/// move,  and what it holds now.  A non-secret knob has an Edit button:  the sheet offers an
/// editor for the knob's kind,  and Save hands the value to `OpsController`,  which runs
/// `python3 -m fleet_knobs set KEY VALUE --plan`,  shows the plan,  asks Touch ID and only
/// then runs `--yes`.  The knob engine checks the value against the knob's own spec and
/// writes through its own write token,  so the app still holds no credential.  A secret knob
/// is never edited here.
struct ControlsView: View {
    enum KnobFilter: String, CaseIterable, Identifiable {
        case all = "All Knobs"
        case overridden = "Overridden"
        case modes = "Modes"
        case caps = "Caps & Numbers"
        case urls = "URLs"

        var id: String { rawValue }
    }

    @EnvironmentObject private var store: FleetStore
    @State private var registry: KnobRegistry?
    @State private var reason: String?
    @State private var knobFilter: KnobFilter = .all
    @State private var searchText: String = ""
    /// True until the first read finishes.
    ///
    /// Without it the tab would say the registry could not be read for as long as
    /// the read takes,  which is a false report about a read that is still running.
    @State private var loading = true
    /// The knob whose editor is open.
    @State private var editing: KnobRegistry.Row?
    /// The write the editor asked for,  started once the editor has closed.
    ///
    /// The plan sheet hangs off the window root,  and a window shows one sheet at a time,  so
    /// the plan is asked for after the editor is gone.
    @State private var pendingAction: OpsAction?
    @ObservedObject private var controller = OpsController.shared

    private func filteredRows(_ registry: KnobRegistry) -> [KnobRegistry.Row] {
        let overriddenNames: Set<String> = Set(
            registry.knobs.compactMap { knob in
                if case .current = knob.current { return knob.name }
                return nil
            }
        )
        let base: [KnobRegistry.Row]
        switch knobFilter {
        case .all:
            base = registry.rows
        case .overridden:
            base = registry.rows.filter { overriddenNames.contains($0.name) }
        case .modes:
            base = registry.rows.filter {
                $0.kind.lowercased().contains("mode")
                    || $0.kind.lowercased().contains("flag")
                    || $0.kind.lowercased().contains("bool")
            }
        case .caps:
            base = registry.rows.filter {
                $0.kind.lowercased().contains("int")
                    || $0.kind.lowercased().contains("float")
                    || $0.kind.lowercased().contains("cap")
            }
        case .urls:
            base = registry.rows.filter { $0.kind.lowercased().contains("url") }
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return base }
        return base.filter {
            $0.name.lowercased().contains(query)
                || $0.owner.lowercased().contains(query)
                || $0.note.lowercased().contains(query)
                || $0.now.lowercased().contains(query)
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading {
                    Text("Reading the registry…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let registry, !registry.knobs.isEmpty {
                    table(registry)
                    if let reason {
                        Text(reason)
                            .font(.callout)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    Text(reason ?? "The registry block has no readable rows.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load() }
        // A write refreshes the engine's local copy,  so the registry is read again.
        .onChange(of: controller.completedRuns) { _, _ in
            Task {
                loading = true
                await load()
            }
        }
        .sheet(item: $editing, onDismiss: startPendingAction) { row in
            KnobEditSheet(
                row: row,
                onSave: { value in
                    pendingAction = .knobSet(key: row.name, value: value)
                    editing = nil
                },
                onCancel: { editing = nil }
            )
        }
    }

    /// Ask for the plan of the write the editor saved.
    private func startPendingAction() {
        guard let action = pendingAction else { return }
        pendingAction = nil
        controller.plan(action)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Controls")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task {
                        loading = true
                        await load()
                    }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("Every variable that governs a script or a background process across the fleet.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let registry {
                HStack(spacing: 6) {
                    Text(registry.copyHeadline)
                        .font(.caption)
                        .foregroundStyle(registry.copyIsStale ? Color.orange : Color.secondary)
                    if registry.copyIsStale {
                        Text("It may not be current.")
                            .font(.caption)
                            .foregroundStyle(Color.orange)
                    }
                    Text("Edit shows the plan and asks Touch ID before anything is written.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if registry.unreadableRows > 0 {
                    Text("\(registry.unreadableRows) row(s) of the registry could not be read.")
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                }
            }
        }
    }

    private func table(_ registry: KnobRegistry) -> some View {
        let rows = filteredRows(registry)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Picker("Filter", selection: $knobFilter) {
                    ForEach(KnobFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 460)
                TextField("Filter by knob name, owner, or purpose…", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: 260)
                Spacer()
                Text("\(rows.count) of \(registry.rows.count) knob(s)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 0) {
                GridRowHeader(labels: ["Knob", "Owner", "Kind", "Now", "Local files may", ""])
                ForEach(rows) { row in
                    KnobRowView(
                        row: row,
                        canEdit: !controller.isPresenting,
                        onEdit: { editing = row }
                    )
                }
            }
        }
    }

    /// Read the registry,  off the main actor.
    ///
    /// The registry file and the copy are both read from disk,  and the app's other
    /// engine work stays off the main thread,  so this does too.
    private func load() async {
        defer { loading = false }
        guard let root = store.engineRoot else {
            reason = "No engine checkout was found,  so the registry was not read."
            return
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        // The engine is asked which project its copy belongs to,  because the app
        // does not know and does not guess:  a private identifier does not belong
        // in the app's source,  and a copy whose project cannot be confirmed is
        // not this fleet's copy.  The store keeps the answer,  so a second visit
        // to this tab does not start another interpreter.
        let answer = await store.engineAnswer()
        let loaded = await Task.detached(priority: .userInitiated) {
            KnobReader.read(repoRoot: root, home: home, identity: answer.identity)
        }.value
        registry = loaded
        // Two things can fail on their own:  the engine may not answer,  and the
        // registry file may not be readable.  Both are said,  so the operator is
        // not sent to the wrong cause.
        let combined = [answer.reason, loaded.problem]
            .compactMap { $0 }
            .joined(separator: "  ")
        if combined.isEmpty {
            reason = loaded.knobs.isEmpty ? "The registry block has no readable rows." : nil
        } else {
            reason = combined
        }
    }
}

/// One knob,  with what it does on a line of its own.
///
/// The shared grid row truncates every cell to two lines,  and the registry notes run
/// far longer than a column of a table can hold,  so the controls tab does not use
/// it.  The note goes under the row at the full width,  where it can be read.
private struct KnobRowView: View {
    let row: KnobRegistry.Row
    /// False while another action is planned or running.
    var canEdit: Bool = true
    var onEdit: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 12) {
                GridCellText(value: row.name)
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(value: row.owner)
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(value: row.kind)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(row.kind)
                // The "Now" cell of a secret row is marked,  and never carries a
                // tooltip,  so a secret's value cannot be read off a secret row.
                GridCellText(value: row.now, emphasised: row.nowIsSecret)
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(value: row.localMay)
                    .frame(maxWidth: .infinity, alignment: .leading)
                // A secret row has no button at all,  so nothing on it can start a write.
                Group {
                    if row.editor != nil {
                        Button("Edit", action: onEdit)
                            .controlSize(.small)
                            .disabled(!canEdit)
                    } else {
                        Text("secret")
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.vertical, 2)
            if !row.note.isEmpty {
                Text(row.note)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 6)
            }
        }
    }
}

/// The editor for one non-secret knob.
///
/// The editor follows the knob's kind:  a toggle for a bool,  a picker for an enum,  a
/// stepper for an int inside its bounds,  and a text field for the rest.  Its check is a
/// convenience:  the knob engine checks the value again,  and the plan says what it found.
private struct KnobEditSheet: View {
    let row: KnobRegistry.Row
    let onSave: (String) -> Void
    let onCancel: () -> Void

    @State private var text: String
    @State private var flag: Bool
    @State private var number: Int
    /// The integer field's text.  A formatted `TextField(value:format:)` binding
    /// commits only on Return or focus loss,  so clicking Show Plan straight after
    /// typing could submit the stale `number`.  The string binds every keystroke,
    /// and `value` reads it back,  so the plan always sees what is on screen.
    @State private var numberText: String

    init(row: KnobRegistry.Row, onSave: @escaping (String) -> Void, onCancel: @escaping () -> Void) {
        self.row = row
        self.onSave = onSave
        self.onCancel = onCancel
        _text = State(initialValue: row.editValue)
        _flag = State(initialValue: row.editValue.lowercased() == "true")
        var start = Int(row.editValue) ?? 0
        if case .integer(let low, let high)? = row.editor {
            if let low, start < low { start = low }
            if let high, start > high { start = high }
        }
        _number = State(initialValue: start)
        _numberText = State(initialValue: String(start))
    }

    /// The value the editor holds,  as the knob engine takes it.
    private var value: String {
        switch row.editor {
        case .toggle?: return flag ? "true" : "false"
        case .integer?: return numberText.trimmingCharacters(in: .whitespaces)
        case .choice?, .text?, nil: return text.trimmingCharacters(in: .whitespaces)
        }
    }

    private var problem: String? {
        guard let editor = row.editor else { return "A secret is never edited here." }
        return editor.problem(with: value)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Edit \(row.name)")
                .font(.title3.weight(.semibold))
            HStack(spacing: 10) {
                Text(row.kind)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                Text("now \(row.now)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if !row.note.isEmpty {
                Text(row.note)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !row.localMay.isEmpty {
                Text("Local files may \(row.localMay).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            editor
            if let problem {
                Text(problem)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            Spacer(minLength: 0)
            HStack {
                Spacer()
                Button("Cancel", role: .cancel, action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button("Show Plan") { onSave(value) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(problem != nil)
            }
        }
        .padding(20)
        .frame(minWidth: 480, idealWidth: 520, minHeight: 260)
    }

    @ViewBuilder
    private var editor: some View {
        switch row.editor {
        case .toggle?:
            Toggle(isOn: $flag) {
                Text(flag ? "true" : "false")
                    .font(.system(size: 12, design: .monospaced))
            }
            .toggleStyle(.switch)
        case .choice(let choices)?:
            Picker("Value", selection: $text) {
                ForEach(choices, id: \.self) { choice in
                    Text(choice).tag(choice)
                }
            }
            .pickerStyle(.segmented)
        case .integer(let low, let high)?:
            let lower = low ?? 0
            let upper = max(high ?? 1_000_000_000, lower)
            HStack(spacing: 10) {
                TextField("Value", text: $numberText)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12, design: .monospaced))
                    .frame(width: 140)
                    .onChange(of: numberText) { _, fresh in
                        // The stepper only takes a number the registry allows (no bound
                        // means unbounded,  as in `problem`);  any other text stays on
                        // screen and `problem` holds Show Plan back.
                        if let parsed = Int(fresh.trimmingCharacters(in: .whitespaces)),
                           low.map({ parsed >= $0 }) ?? true,
                           high.map({ parsed <= $0 }) ?? true {
                            number = parsed
                        }
                    }
                    .onChange(of: number) { _, fresh in
                        // Only a stepper click changes the number away from the text,  so
                        // what the operator typed (`007`,  `+5`) is never rewritten.
                        if Int(numberText.trimmingCharacters(in: .whitespaces)) != fresh {
                            numberText = String(fresh)
                        }
                    }
                Stepper("", value: $number, in: lower...upper)
                    .labelsHidden()
                if low != nil || high != nil {
                    Text("\(lower) to \(upper)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        case .text(let hint)?:
            TextField(hint, text: $text)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12, design: .monospaced))
        case nil:
            Text("A secret is never edited here.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }
}
