import SwiftUI

/// The controls surface:  the modes,  caps and flags the fleet's jobs read from Infisical.
///
/// Each knob says what it is,  which machine's jobs read it and which jobs those are,  its
/// default,  what it holds now and where that value came from (Infisical,  read by the
/// engine on Refresh,  or this Mac's copy).  A non-secret knob opens an editor for its kind;
/// the editor says which way a change moves the knob and what a value needs,  and Show Plan
/// hands the value to `OpsController`,  which runs `python3 -m fleet_knobs set KEY VALUE
/// --plan`,  shows the plan,  asks Touch ID and only then runs `--yes`.  Reset To Default
/// runs `fleet_knobs unset` the same way.  The knob engine checks every value against the
/// knob's own spec and writes through its own write token,  so the app holds no credential.
/// A secret knob is never edited here,  and its value is never read:  the engine says only
/// whether it is set.
struct ControlsView: View {
    @EnvironmentObject private var store: FleetStore
    @State private var registry: KnobRegistry?
    /// Why the engine could not say which project its copy belongs to,  when it could not.
    @State private var engineReason: String?
    /// Why the engine's live read could not be had,  when it could not.
    @State private var liveProblem: String?
    @State private var knobFilter: KnobFilter = .all
    @State private var hostFilter: KnobHostFilter = .any
    @State private var searchText: String = ""
    /// True until the first read of the registry finishes.
    ///
    /// Without it the tab would say the registry could not be read for as long as
    /// the read takes,  which is a false report about a read that is still running.
    @State private var loading = true
    /// True while the engine reads Infisical.  The table stays on screen meanwhile.
    @State private var checking = false
    /// Which load is the newest.  The tab loads when it appears and again after a run
    /// finishes,  so two loads can overlap;  only the newest may change what is on screen,
    /// and `loading` and `checking` stay true until it is done.
    @State private var generation = 0
    /// The knob whose editor is open.
    @State private var editing: KnobRegistry.Row?
    /// The write the editor asked for,  started once the editor has closed.
    ///
    /// The plan sheet hangs off the window root,  and a window shows one sheet at a time,  so
    /// the plan is asked for after the editor is gone.
    @State private var pendingAction: OpsAction?
    @ObservedObject private var controller = OpsController.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading && registry == nil {
                    Text("Reading the registry\u{2026}")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let registry, !registry.knobs.isEmpty {
                    problemBanner
                    writeBanner(registry)
                    summary(registry)
                    table(registry)
                    SecretNamesPanel()
                    RecallSettingsPanel()
                } else {
                    problemBanner
                    if problems.isEmpty {
                        Text("The registry has no readable knobs.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load() }
        // A write changes Infisical and the engine's copy,  so the values are read again.
        // The table stays on screen while they are,  and so do the panels below it.
        .onChange(of: controller.completedRuns) { _, _ in
            Task { await load() }
        }
        .sheet(item: $editing, onDismiss: startPendingAction) { row in
            KnobEditSheet(
                row: row,
                registry: registry,
                onSave: { value in
                    pendingAction = .knobSet(key: row.name, value: value)
                    editing = nil
                },
                onReset: {
                    pendingAction = .knobUnset(key: row.name)
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
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading || checking)
            }
            Text("Modes, caps and flags the fleet's jobs read from Infisical.\u{00A0} Wake settings and resource limits have panels of their own.")
                .font(.callout)
                .foregroundStyle(.secondary)
            freshness
        }
    }

    /// Where the values came from and how old they are.
    @ViewBuilder
    private var freshness: some View {
        if let registry {
            VStack(alignment: .leading, spacing: 3) {
                if checking {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Asking the knob engine to read Infisical\u{2026}")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                ForEach(registry.freshnessLines(), id: \.text) { line in
                    Label {
                        Text(line.text)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: line.isWarning ? "exclamationmark.triangle" : "checkmark.circle")
                    }
                    .font(.caption)
                    .foregroundStyle(line.isWarning ? Color.orange : Color.secondary)
                }
                if registry.unreadableRows > 0 {
                    Text("\(registry.unreadableRows) row(s) of the registry could not be read.")
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                }
                if let root = store.engineRoot, !registry.registryFile.isEmpty {
                    Text("Knob list from \(registry.registryFile) in the engine checkout at \(root.path).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    /// Every cause that keeps values or knobs off the screen,  as whole sentences.
    private var problems: [String] {
        KnobRegistry.problemSentences(
            engineReason: engineReason,
            registryProblem: registry?.problem,
            liveProblem: liveProblem
        )
    }

    @ViewBuilder
    private var problemBanner: some View {
        if !problems.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(problems, id: \.self) { sentence in
                    Label {
                        Text(sentence).fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                    }
                }
            }
            .font(.callout)
            .foregroundStyle(Color.orange)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    /// Whether a change can be made from here,  as the engine said on its last read.
    @ViewBuilder
    private func writeBanner(_ registry: KnobRegistry) -> some View {
        if registry.writesUnavailable {
            Label {
                Text(ApplyCopy.join(
                    "Changes cannot be made from Muster on this Mac.",
                    "The knob engine has no write token when Muster runs it, so a plan is refused before Touch ID.",
                    "Change knobs in Infisical for now."
                ))
                .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "lock")
            }
            .font(.callout)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    /// How many knobs are changed from their defaults,  and how many make the fleet do more.
    private func summary(_ registry: KnobRegistry) -> some View {
        let changed = registry.changedCount
        let wider = registry.widerCount
        let text: String
        if !registry.valuesAreKnown {
            text = "The values are not known, so which knobs are changed from their defaults is not known."
        } else {
            let first = changed == 1 ? "1 knob is changed from its default" : "\(changed) knobs are changed from their defaults"
            let second = wider == 1 ? "1 of them makes the fleet do more" : "\(wider) of them make the fleet do more"
            text = changed == 0 ? "Every knob holds its default." : "\(first);  \(second)."
        }
        return Text(text)
            .font(.callout.weight(.medium))
            .fixedSize(horizontal: false, vertical: true)
    }

    private func table(_ registry: KnobRegistry) -> some View {
        let rows = registry.filtered(knobFilter, host: hostFilter, query: searchText)
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Picker("Kind", selection: $knobFilter) {
                    ForEach(KnobFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(maxWidth: 520)
                Picker("Read On", selection: $hostFilter) {
                    ForEach(KnobHostFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .frame(maxWidth: 220)
            }
            HStack(spacing: 12) {
                TextField("Search by name, job or purpose", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 320)
                Spacer()
                Text("\(rows.count) of \(registry.rows.count) knobs")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if rows.isEmpty {
                Text(registry.emptyMessage(knobFilter, host: hostFilter, query: searchText))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
            } else {
                KnobColumnHeader()
                ForEach(KnobRegistry.grouped(rows)) { group in
                    VStack(alignment: .leading, spacing: 0) {
                        Text(group.title)
                            .font(.headline)
                            .padding(.top, 8)
                            .padding(.bottom, 4)
                        ForEach(group.rows) { row in
                            KnobRowView(
                                row: row,
                                writesUnavailable: registry.writesUnavailable,
                                canEdit: !controller.isPresenting,
                                onEdit: { editing = row }
                            )
                            Divider()
                        }
                    }
                }
            }
        }
    }

    /// Read the registry and this Mac's copy,  then ask the engine to read Infisical.
    ///
    /// The file reads are quick and run first,  so the table is on screen while the engine
    /// works.  A refresh keeps the last reading on screen until the new one is in.
    private func load() async {
        generation += 1
        let mine = generation
        guard let root = store.engineRoot else {
            engineReason = "the engine was not found"
            loading = false
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
        // A newer load started while this one read the files:  it owns the screen now.
        guard mine == generation else { return }
        engineReason = answer.reason
        if let previous = registry?.live {
            // Keep the last live reading until the new one is in, so a refresh does not
            // flash the values back to the copy.
            registry = loaded.applying(previous)
        } else {
            registry = loaded
        }
        loading = false
        await checkInfisical(base: loaded, generation: mine)
    }

    /// Ask the knob engine for its live read,  and apply it.
    private func checkInfisical(base: KnobRegistry, generation mine: Int) async {
        guard let location = store.engineLocation else {
            liveProblem = "Muster could not find the engine, so Infisical was not read."
            return
        }
        // The reading on screen now,  kept if this one fails:  a failed refresh says so and
        // leaves the last reading in place, with its own time.
        let previousLive = registry?.live
        checking = true
        // Waiting for the engine does not occupy a thread:  the read is asynchronous.
        let reading = await KnobStatusReader.read(location: location)
        guard mine == generation else { return }
        checking = false
        switch reading {
        case .read(let status):
            registry = base.applying(status)
            liveProblem = nil
        case .failed(let why):
            registry = previousLive.map { base.applying($0) } ?? base
            liveProblem = why
        }
    }
}

/// The column titles,  in the widths the rows use.
private struct KnobColumnHeader: View {
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            title("Knob").frame(minWidth: 220, maxWidth: .infinity, alignment: .leading)
            title("Now").frame(width: 190, alignment: .leading)
            title("Default").frame(width: 150, alignment: .leading)
            title("Read On").frame(width: 170, alignment: .leading)
            Color.clear.frame(width: 70, height: 1)
        }
        .padding(.top, 4)
    }

    private func title(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
    }
}

/// One knob:  its plain name and key,  what it holds and from where,  its default and who
/// reads it,  with what it does on a line of its own.
private struct KnobRowView: View {
    let row: KnobRegistry.Row
    /// True when the engine said a write cannot run from here.
    var writesUnavailable: Bool = false
    /// False while another action is planned or running.
    var canEdit: Bool = true
    var onEdit: () -> Void = {}

    private var knob: KnobRegistry.Knob? { row.knob }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                // The summary is one spoken element;  the row's button stays a separate
                // control beside it, so VoiceOver can still activate it.
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(row.title)
                            .font(.body.weight(.medium))
                        Text(row.name)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    .frame(minWidth: 220, maxWidth: .infinity, alignment: .leading)
                    nowCell
                        .frame(width: 190, alignment: .leading)
                    Text(row.nowIsSecret ? "none" : defaultText)
                        .font(.system(.callout, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                        .frame(width: 150, alignment: .leading)
                    hostChip
                        .frame(width: 170, alignment: .leading)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(accessibilityText)
                actionButton
                    .frame(width: 70, alignment: .trailing)
            }
            if !row.note.isEmpty {
                Text(row.note)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 8)
        .accessibilityElement(children: .contain)
    }

    private var defaultText: String {
        let shown = knob?.canonicalDefault ?? KnobRegistry.Knob.plainDefault(row.defaultValue)
        return shown.isEmpty ? "empty" : shown
    }

    /// The value now,  the place it came from,  and whether it is changed or wider.
    private var nowCell: some View {
        let changed = knob?.isChanged ?? false
        let wider = knob?.isWiderThanDefault ?? false
        return VStack(alignment: .leading, spacing: 2) {
            Text(row.now)
                .font(.system(.callout, design: .monospaced).weight(changed ? .semibold : .regular))
                .foregroundStyle(changed ? Color.primary : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Text(sourceLine(changed: changed))
                .font(.caption)
                .foregroundStyle(.secondary)
            if wider {
                Label("Does More Than Its Default", systemImage: "arrow.up.right")
                    .font(.caption)
                    .foregroundStyle(Color.orange)
            }
        }
    }

    private func sourceLine(changed: Bool) -> String {
        let tag = knob?.source.tag ?? KnobValueSource.notRead.tag
        return changed ? "\(tag), changed" : tag
    }

    private var hostChip: some View {
        let hosts = knob?.hosts ?? .unknown
        let consumers = knob?.consumers ?? []
        return VStack(alignment: .leading, spacing: 2) {
            Label(hosts.title, systemImage: hosts == .unknown ? "questionmark.circle" : "desktopcomputer")
                .font(.caption.weight(.medium))
            if !consumers.isEmpty {
                Text(consumers.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The row's one button.  A secret row has none at all,  so nothing on it can start a
    /// write;  it says where the secret is managed instead.
    @ViewBuilder
    private var actionButton: some View {
        if row.editor != nil {
            Button(writesUnavailable ? "View" : "Edit", action: onEdit)
                .controlSize(.small)
                .disabled(!canEdit)
        } else {
            Text("Managed In Infisical")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
        }
    }

    private var accessibilityText: String {
        let source = knob?.source.tag ?? ""
        let hosts = knob?.hosts.title ?? ""
        return "\(row.title), \(row.name).  Now \(row.now), from \(source).  Default \(defaultText).  Read on \(hosts)."
    }
}

/// The editor for one non-secret knob.
///
/// The editor follows the knob's kind:  a toggle for a bool,  a picker for an enum,  a
/// stepper for an int inside its bounds,  a number per cap for caps,  and a text field for
/// the rest.  It says where the value lands and who reads it,  the default,  which way the
/// change moves the knob and what a value needs.  Its check is a convenience:  the knob
/// engine checks the value again,  and the plan says what it found.
private struct KnobEditSheet: View {
    let row: KnobRegistry.Row
    let registry: KnobRegistry?
    let onSave: (String) -> Void
    var onReset: () -> Void = {}
    let onCancel: () -> Void

    @State private var text: String
    @State private var flag: Bool
    @State private var number: Int
    /// The integer field's text.  A formatted `TextField(value:format:)` binding
    /// commits only on Return or focus loss,  so clicking Show Plan straight after
    /// typing could submit the stale `number`.  The string binds every keystroke,
    /// and `value` reads it back,  so the plan always sees what is on screen.
    @State private var numberText: String
    /// One field per cap name,  for a caps knob.
    @State private var capValues: [String: String]

    init(row: KnobRegistry.Row, registry: KnobRegistry?, onSave: @escaping (String) -> Void,
         onReset: @escaping () -> Void = {}, onCancel: @escaping () -> Void) {
        self.row = row
        self.registry = registry
        self.onSave = onSave
        self.onReset = onReset
        self.onCancel = onCancel
        _text = State(initialValue: row.editValue)
        // The edit value is in the engine's own form,  so `yes` in Infisical opens it on.
        _flag = State(initialValue: row.editValue == "true")
        var start = Int(row.editValue) ?? 0
        if case .integer(let low, let high)? = row.editor {
            if let low, start < low { start = low }
            if let high, start > high { start = high }
        }
        _number = State(initialValue: start)
        _numberText = State(initialValue: String(start))
        var caps: [String: String] = [:]
        if case .caps(let names)? = row.editor {
            let held = KnobRegistry.Knob.caps(row.editValue) ?? [:]
            for name in names { caps[name] = held[name].map { String($0) } ?? "" }
        }
        _capValues = State(initialValue: caps)
    }

    private var knob: KnobRegistry.Knob? { row.knob }

    /// The value the editor holds,  as the knob engine takes it.
    private var value: String {
        switch row.editor {
        case .toggle?: return flag ? "true" : "false"
        case .integer?: return numberText.trimmingCharacters(in: .whitespaces)
        case .caps(let names)?:
            return names.sorted()
                .map { "\($0)=\((capValues[$0] ?? "").trimmingCharacters(in: .whitespaces))" }
                .joined(separator: ",")
        case .choice?, .text?, nil: return text.trimmingCharacters(in: .whitespaces)
        }
    }

    private var problem: String? {
        guard let knob else {
            guard let editor = row.editor else { return "A secret is never edited here." }
            return editor.problem(with: value)
        }
        return knob.problem(with: value)
    }

    /// True when a live read says Infisical already holds this value,  so there is nothing
    /// to plan.  This Mac's copy never says so:  it can be days old.
    private var unchanged: Bool {
        guard let knob else { return false }
        return knob.liveReadingHolds(value)
    }

    /// True when a live read says Infisical holds no value,  so there is nothing to reset.
    private var nothingToReset: Bool {
        guard let knob else { return false }
        return knob.liveReadingHoldsNothing
    }

    private var writesUnavailable: Bool { registry?.writesUnavailable ?? false }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Edit \(row.title)")
                        .font(.title3.weight(.semibold))
                    Text(row.name)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                facts
                if !row.note.isEmpty {
                    Text(row.note)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                editor
                outlook
                if writesUnavailable {
                    Label(
                        "Muster cannot change knobs on this Mac:  the knob engine has no write token here.",
                        systemImage: "lock"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                HStack {
                    Button("Reset To Default", role: .destructive, action: onReset)
                        .disabled(nothingToReset || writesUnavailable)
                        .help(nothingToReset
                              ? "Infisical holds no value for this, so it already holds its default."
                              : "Remove the value from Infisical so the default applies.")
                    Spacer()
                    Button("Cancel", role: .cancel, action: onCancel)
                        .keyboardShortcut(.cancelAction)
                    Button("Show Plan") { onSave(value) }
                        .keyboardShortcut(.defaultAction)
                        .disabled(problem != nil || unchanged || writesUnavailable)
                }
                if unchanged && problem == nil {
                    Text("Infisical holds this value now, so there is nothing to change.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(20)
        }
        .frame(minWidth: 520, idealWidth: 580, minHeight: 360, idealHeight: 520)
    }

    /// Where the value lands,  who reads it,  what it holds now and its default.
    @ViewBuilder
    private var facts: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let knob {
                fact("Read by", knob.affectsText)
                fact("Stored in", "Infisical, shared by every machine whose jobs read it.")
                fact("Now", "\(knob.nowText) (\(knob.source.tag))")
                fact("Default", knob.canonicalDefault.isEmpty ? "empty" : knob.canonicalDefault)
                if !knob.localRule.isEmpty {
                    fact("Local files", knob.localRule)
                } else if !row.localMay.isEmpty {
                    fact("Local files", row.localMay == "nothing"
                         ? "A local file cannot change this."
                         : "A local file can \(row.localMay).")
                }
                if knob.requiresExplicit {
                    fact("Note", "The rule that reads this uses it only once it is set in Infisical;  until then the rule reads unknown.")
                }
                if knob.once, knob.isSet {
                    fact("Note", "This is meant to be set once, and it already holds a value.")
                }
                if !knob.format.isEmpty {
                    fact("Format", knob.example.isEmpty ? knob.format : "\(knob.format), such as \(knob.example)")
                }
            } else {
                fact("Now", row.now)
            }
        }
    }

    private func fact(_ title: String, _ detail: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 80, alignment: .leading)
            Text(detail)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }

    /// Which way the chosen value moves the knob,  what it makes the fleet do,  and what it
    /// needs.  None of it is colour alone:  each line says what it means in words.
    @ViewBuilder
    private var outlook: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let knob, problem == nil, !unchanged {
                let base = knob.editValue
                let way = knob.direction(from: base, to: value)
                if let sentence = way.sentence {
                    Label(sentence, systemImage: way == .wider ? "arrow.up.right" : "arrow.down.right")
                        .foregroundStyle(way == .wider || way == .mixed ? Color.orange : Color.secondary)
                }
                if let consequence = knob.consequence(for: value) {
                    Label(consequence, systemImage: "info.circle")
                }
                if let registry, let unmet = knob.unmetRequirement(for: value, in: registry) {
                    Label(unmet, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(Color.orange)
                }
                if knob.once, knob.isSet {
                    Label("This is meant to be set once, and it already holds a value.", systemImage: "clock")
                        .foregroundStyle(Color.orange)
                }
            }
            if let problem {
                Label(problem, systemImage: "xmark.circle")
                    .foregroundStyle(Color.orange)
            }
        }
        .font(.callout)
        .fixedSize(horizontal: false, vertical: true)
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
            VStack(alignment: .leading, spacing: 4) {
                Picker("Value", selection: $text) {
                    ForEach(choices, id: \.self) { choice in
                        Text(choice).tag(choice)
                    }
                }
                .pickerStyle(.segmented)
                Text("Listed from the least the fleet may do to the most.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        case .integer(let low, let high)?:
            let lower = low ?? 0
            let upper = max(high ?? 999_999_999, lower)
            let step = max(knob?.step ?? 1, 1)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 10) {
                    TextField("Value", text: $numberText)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12, design: .monospaced))
                        .frame(width: 140)
                        .onChange(of: numberText) { _, fresh in
                            // The stepper only takes a number the registry allows (no bound
                            // means unbounded,  as in `problem`);  any other text stays on
                            // screen and `problem` holds Show Plan back.
                            let trimmed = fresh.trimmingCharacters(in: .whitespaces)
                            if KnobEditorKind.isEngineNumber(trimmed), let parsed = Int(trimmed),
                               low.map({ parsed >= $0 }) ?? true,
                               high.map({ parsed <= $0 }) ?? true {
                                number = parsed
                            }
                        }
                        .onChange(of: number) { _, fresh in
                            // Only a stepper click changes the number away from the text,  so
                            // what the operator typed (`007`) is never rewritten.
                            if Int(numberText.trimmingCharacters(in: .whitespaces)) != fresh {
                                numberText = String(fresh)
                            }
                        }
                    Stepper("", value: $number, in: lower...upper, step: step)
                        .labelsHidden()
                    if let shown = knob?.unitText(for: numberText.trimmingCharacters(in: .whitespaces)) {
                        Text(shown)
                            .font(.callout)
                    }
                }
                if low != nil || high != nil {
                    let unit = knob?.unit ?? ""
                    Text("\(lower) to \(upper)\(unit.isEmpty ? "" : " \(unit)")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        case .caps(let names)?:
            VStack(alignment: .leading, spacing: 6) {
                ForEach(names, id: \.self) { name in
                    HStack(spacing: 10) {
                        Text(name)
                            .font(.system(size: 12, design: .monospaced))
                            .frame(width: 200, alignment: .leading)
                        TextField("Number", text: Binding(
                            get: { capValues[name] ?? "" },
                            set: { capValues[name] = $0 }
                        ))
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12, design: .monospaced))
                        .frame(width: 120)
                    }
                }
                Text("Every cap this knob has is listed;  a misspelt cap name cannot be written.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        case .text(let hint)?:
            TextField(knob?.example.isEmpty == false ? (knob?.example ?? hint) : hint, text: $text)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12, design: .monospaced))
        case nil:
            Text("A secret is never edited here.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }
}


/// Secret names only,  grouped by path,  plus the doctor's checks.  No value can appear
/// here:  the only verbs run are `names` and `doctor`.
private struct SecretNamesPanel: View {
    @State private var groups: [SecretNameGroup] = []
    @State private var doctor: [String: String] = [:]
    @State private var problem: String?
    @State private var doctorProblem: String?
    @State private var loading = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Secret Names").font(.headline)
                Spacer()
                Button("Refresh") { Task { await load() } }.disabled(loading)
            }
            Text("Names only, from the top folder of the fleet-ops project in Infisical, read through this Mac's fleet-secrets.\u{00A0} This does not show whether a value is set, and values are never read here.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if loading { Text("Reading names\u{2026}").font(.callout).foregroundStyle(.secondary) }
            ForEach(groups, id: \.path) { group in
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(group.names, id: \.self) { name in
                            Text(name)
                                .font(.system(size: 11, design: .monospaced))
                                .textSelection(.enabled)
                        }
                    }
                } label: {
                    Text(group.names.count == 1 ? "1 name at \(group.path)" : "\(group.names.count) names at \(group.path)")
                        .font(.callout)
                }
            }
            if let problem { Text(problem).font(.caption).foregroundStyle(.orange) }
            Text("Doctor:  This Mac's fleet-secrets Setup").font(.subheadline.weight(.semibold))
            if let doctorProblem {
                Text(doctorProblem).font(.caption).foregroundStyle(.orange)
            } else if doctor.isEmpty && !loading {
                Text("The doctor has not answered.").font(.caption).foregroundStyle(.secondary)
            }
            if doctor["ok"] == "false" {
                Label("The doctor found a problem with this Mac's fleet-secrets.", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            ForEach(FleetSecrets.doctorRows(doctor)) { item in
                HStack(spacing: 8) {
                    Text(item.label)
                        .font(.caption)
                        .frame(width: 200, alignment: .leading)
                    Text(item.value)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .task { await load() }
    }

    private func load() async {
        loading = true; defer { loading = false }
        let secrets = FleetSecrets()
        do {
            groups = try await awaitOffThePool { try secrets.secretNamesGrouped(paths: ["/"]) }
            problem = nil
        } catch { problem = error.localizedDescription }
        // The doctor is asked on its own, and its failure is shown rather than hidden.
        do {
            doctor = try await awaitOffThePool { try secrets.doctorReport() }
            doctorProblem = nil
        } catch {
            doctor = [:]
            doctorProblem = error.localizedDescription
        }
    }
}

/// Recall admin settings,  masked.  The base URL and the token KEY NAME are the
/// only stored settings;  the token itself is read by name per request.
private struct RecallSettingsPanel: View {
    @State private var config = RecallSettingsConfig.load()
    @State private var rows: [RecallSettingRow] = []
    @State private var problem: String?
    @State private var loading = false
    @State private var loadedAt: Date?

    private var hasAddress: Bool {
        !config.baseURLString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Recall Settings").font(.headline)
            Text("The recall service's own settings, read from the service on the server.\u{00A0} They are not fleet knobs.\u{00A0} This Mac stores only the service's address and the key name of its admin token, and each value shows only as set or empty.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Base URL (https)", text: $config.baseURLString)
                .textFieldStyle(.roundedBorder).font(.system(size: 11, design: .monospaced))
            TextField("Token Key Name", text: $config.tokenKeyName)
                .textFieldStyle(.roundedBorder).font(.system(size: 11, design: .monospaced))
            HStack {
                Button("Save Settings") { config.save() }
                Button("Load Settings") { Task { await load() } }.disabled(loading || !hasAddress)
                if !hasAddress {
                    Text("Enter the recall service's https address first.")
                        .font(.caption).foregroundStyle(.secondary)
                } else if let loadedAt {
                    Text("Read at \(FleetTime.clock(loadedAt)).")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(rows) { row in
                HStack {
                    Text(row.name).font(.system(size: 11, design: .monospaced))
                    Spacer()
                    Text(row.display).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                }
            }
            if let problem { Text(problem).font(.caption).foregroundStyle(.orange) }
        }
        // With an address saved, the settings are read when the panel opens.
        .task { if hasAddress { await load() } }
    }

    private func load() async {
        loading = true; defer { loading = false }
        do {
            rows = try await RecallSettingsClient.fetch(config: config, secrets: FleetSecrets())
            problem = nil
            loadedAt = Date()
        } catch { problem = error.localizedDescription }
    }
}
