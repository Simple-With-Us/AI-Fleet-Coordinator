import SwiftUI

/// Operator settings save as they are edited, through the shared authenticated server editor.
struct ServerListenerSettingsSection: View {
    @ObservedObject private var store = ServerListenerSettingsStore.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Changes save automatically.\(ServerStatusCopy.sentenceGap)Saved means Infisical confirmed the value and the Operator applied it.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                if store.isReading { ProgressView().controlSize(.small) }
                Button("Read Settings") { Task { await store.refresh() } }
                    .disabled(store.isReading || store.isSaving)
            }
            content
        }
    }

    @ViewBuilder private var content: some View {
        switch store.reading {
        case nil:
            Text("Not read yet.").font(.callout).foregroundStyle(.secondary)
        case .unavailable(let why)?:
            Label(why, systemImage: "questionmark.circle")
                .font(.callout).fixedSize(horizontal: false, vertical: true)
        case .read?:
            if let problem = store.refreshProblem {
                Label("Refresh failed: \(problem)", systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            if store.allowlist == nil {
                Text(store.allowlistProblem ?? "The settings types could not be read.")
                    .font(.callout).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(store.groups) { group in
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(group.rows) { row in
                            ServerSettingRowView(row: row, draft: draftBinding(row), state: store.saveStates[row.id],
                                removing: store.removalDrafts.contains(row.id), remove: { store.removeOverride(row.id) })
                        }
                    }.padding(.leading, 4)
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(group.title).font(.subheadline.weight(.semibold))
                        if let caption = ServerSettingsCopy.groupCaption(table: group.id) {
                            Text(caption).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if let message = store.message {
                Label(message, systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func draftBinding(_ row: ServerSettingRow) -> Binding<String> {
        Binding(get: { store.removalDrafts.contains(row.id) ? "" : (store.drafts[row.id] ?? row.editText) },
                set: { store.edit(row.id, text: $0) })
    }
}

private struct ServerSettingRowView: View {
    let row: ServerSettingRow
    @Binding var draft: String
    let state: ServerSettingsSaveState?
    let removing: Bool
    let remove: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(row.title).font(.system(size: 11)).frame(width: 240, alignment: .leading)
            VStack(alignment: .leading, spacing: 3) {
                control
                if row.isLimit, !removing, row.current != nil || !draft.isEmpty {
                    Button("Unset Limit", action: remove).buttonStyle(.link).font(.caption)
                }
                if removing {
                    Text("Limit will be unset.").font(.caption).foregroundStyle(.secondary)
                }
                if let state { saveStatus(state) }
                if let information = row.information {
                    Text(information).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let note = row.defaultNote {
                    Text(note).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }.help(row.readOnlyReason ?? row.defaultNote ?? "")
    }

    @ViewBuilder private func saveStatus(_ state: ServerSettingsSaveState) -> some View {
        switch state {
        case .saving:
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text(state.title)
            }.font(.caption).foregroundStyle(.secondary)
        case .saved:
            Label(state.title, systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(.green)
        case .failed(let why):
            Label("Failed: \(why)", systemImage: "exclamationmark.circle.fill")
                .font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private var control: some View {
        switch row.control {
        case .toggle?:
            Toggle("", isOn: Binding(get: { draft == "On" || draft == "true" }, set: { draft = $0 ? "On" : "Off" }))
                .labelsHidden().toggleStyle(.switch).controlSize(.small)
        case .choice(let values)?:
            Picker("", selection: $draft) {
                if !values.contains(draft) { Text("Not set").tag(draft) }
                ForEach(values, id: \.self) { Text($0).tag($0) }
            }.labelsHidden().frame(width: 240)
        case .whole?, .number?, .text?:
            TextField(row.isLimit ? "No limit" : "Not set", text: $draft).textFieldStyle(.roundedBorder)
                .font(.system(size: 11, design: .monospaced)).frame(width: 190)
        case .pairs?:
            ServerPresencePairsEditor(draft: $draft, channelNames: row.channelNames)
        case nil:
            // Identity pins and unknown fields are status, rather than disabled controls.
            VStack(alignment: .leading, spacing: 2) {
                Text(row.currentText).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                if let reason = row.readOnlyReason {
                    Text(reason).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// Presence entries remain typed channel/topic pairs; the flow never exposes JSON.
private struct ServerPresencePairsEditor: View {
    @Binding var draft: String
    let channelNames: [String]

    private var pairs: [[String]] {
        guard case .list(let values)? = ServerSettingJSON.parse(draft) else { return [] }
        return values.compactMap { value in
            guard case .list(let fields) = value, fields.count == 2,
                  case .text(let channel) = fields[0], case .text(let topic) = fields[1] else { return nil }
            return [channel, topic]
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(pairs.indices, id: \.self) { index in
                HStack {
                    TextField("Channel", text: field(index, 0)).accessibilityLabel("Presence Channel")
                    if !channelNames.isEmpty {
                        Menu {
                            ForEach(channelNames, id: \.self) { channel in
                                Button(channel) { field(index, 0).wrappedValue = channel }
                            }
                        } label: { Image(systemName: "chevron.down") }
                        .menuStyle(.borderlessButton).fixedSize().accessibilityLabel("Choose Channel")
                    }
                    TextField("Topic", text: field(index, 1)).accessibilityLabel("Presence Topic")
                    Button {
                        var values = pairs
                        guard values.indices.contains(index) else { return }
                        values.remove(at: index)
                        update(values)
                    } label: {
                        Image(systemName: "minus.circle")
                    }.buttonStyle(.borderless).accessibilityLabel("Remove Presence Pair")
                }.textFieldStyle(.roundedBorder)
            }
            Button("Add Presence Pair", systemImage: "plus") { update(pairs + [["", ""]]) }
                .disabled(pairs.count >= 32)
        }.font(.system(size: 11)).frame(minWidth: 300)
    }

    private func field(_ row: Int, _ column: Int) -> Binding<String> {
        Binding(get: { pairs.indices.contains(row) ? pairs[row][column] : "" }, set: { value in
            var values = pairs
            guard values.indices.contains(row) else { return }
            values[row][column] = value
            update(values)
        })
    }

    private func update(_ values: [[String]]) {
        draft = ServerSettingValue.list(values.map { .list($0.map(ServerSettingValue.text)) }).json ?? "[]"
    }
}
