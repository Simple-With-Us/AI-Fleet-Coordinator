import SwiftUI

/// The server Eavesdropper's own settings, in the Wake & Listener panel.
///
/// Every value comes from the server editor's get, and every change goes Show Plan, then Touch ID,
/// through the shared action sheet:  the sheet shows what the plan changes and the Eavesdropper's
/// own check, the owner approves, and the server applies only that plan.  A row Muster will not
/// change says why.  `ServerListenerSettings.swift` holds the rules.
struct ServerListenerSettingsSection: View {
    @ObservedObject private var store = ServerListenerSettingsStore.shared
    @ObservedObject private var controller = OpsController.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Server Eavesdropper Settings")
                    .font(.headline)
                Spacer()
                if store.isReading { ProgressView().controlSize(.small) }
                Button("Read Server Settings") { Task { await store.refresh() } }
                    .disabled(store.isReading)
            }
            Text("The server listener holds every seat.\(ServerStatusCopy.sentenceGap)A seat's on or off switch and wake type change only through the server's allowlisted editor.\(ServerStatusCopy.sentenceGap)Every other setting here is read only, and each row says why.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            content
        }
        // Read every time the panel appears:  the store outlives the panel, so an old read would
        // otherwise be shown.
        .task { await store.refresh() }
        .onChange(of: controller.completedRuns) { _, _ in
            // Only a run of this section's own action can have changed the server's listener file,
            // so only that run is followed by a read.  A knob set, a restart or a redeploy is not.
            guard controller.lastRunWasListenerSettings else { return }
            // The edits a done apply carried are cleared;  any other end keeps them for another try.
            if case .finished(.serverListenerSettings, let outcome) = controller.phase, outcome.kind == .done {
                store.revert()
            }
            Task { await store.refresh() }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch store.reading {
        case nil:
            Text("Not read yet.")
                .font(.callout)
                .foregroundStyle(.secondary)
        case .unavailable(let why)?:
            Label {
                Text(why).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            } icon: {
                Image(systemName: "questionmark.circle").accessibilityHidden(true)
            }
            .font(.callout)
        case .read?:
            if store.allowlist == nil {
                Text("This checkout's copy of the allowlist (\(ServerSettingsAllowlist.relativePath)) could not be read, so Muster offers nothing to change.")
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Label {
                Text(store.applyEnabled == true ? ServerSettingsCopy.applyOn : ServerSettingsCopy.applyOff)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: store.applyEnabled == true ? "touchid" : "lock.fill").accessibilityHidden(true)
            }
            .font(.callout)
            .foregroundStyle(store.applyEnabled == true ? Color.primary : Color.orange)
            ForEach(store.groups) { group in
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(group.rows) { row in
                            ServerSettingRowView(row: row, draft: draftBinding(row))
                        }
                    }
                    .padding(.leading, 4)
                } label: {
                    Text(group.title).font(.subheadline.weight(.semibold))
                }
            }
            if let message = store.message {
                Text(message)
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Revert Edits") { store.revert() }
                    .disabled(store.drafts.isEmpty)
                Button {
                    store.showPlan()
                } label: {
                    Label("Show Plan", systemImage: "list.bullet.rectangle")
                }
                .disabled(store.drafts.isEmpty || controller.isBusy)
            }
        }
    }

    private func draftBinding(_ row: ServerSettingRow) -> Binding<String> {
        Binding(
            get: { store.drafts[row.id] ?? row.effective?.fieldText ?? "" },
            set: { newValue in
                if newValue == (row.effective?.fieldText ?? "") {
                    store.drafts[row.id] = nil
                } else {
                    store.drafts[row.id] = newValue
                }
            }
        )
    }
}

/// One server setting:  its name, the server's value, and a control or the reason there is none.
private struct ServerSettingRowView: View {
    let row: ServerSettingRow
    @Binding var draft: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(row.title)
                .font(.system(size: 11))
                .frame(width: 240, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                control
                if let note = row.defaultNote {
                    Text(note)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .help(row.readOnlyReason ?? row.defaultNote ?? "")
    }

    @ViewBuilder
    private var control: some View {
        switch row.control {
        case .toggle?:
            Toggle("", isOn: Binding(
                get: { draft == "On" || draft == "true" },
                set: { draft = $0 ? "On" : "Off" }
            ))
            .labelsHidden()
            .toggleStyle(.switch)
            .controlSize(.small)
        case .choice(let values)?:
            Picker("", selection: $draft) {
                if !values.contains(draft) { Text("Not set").tag(draft) }
                ForEach(values, id: \.self) { Text($0).tag($0) }
            }
            .labelsHidden()
            .frame(width: 140)
        case .whole?, .number?, .text?:
            TextField("Not set", text: $draft)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 11, design: .monospaced))
                .frame(width: 140)
        case nil:
            VStack(alignment: .leading, spacing: 2) {
                Text(row.currentText)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                if let reason = row.readOnlyReason {
                    Text(reason)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
