import SwiftUI

struct FleetOverviewView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 32) {
            HStack(spacing: 14) {
                Image(systemName: "square.stack.3d.up")
                    .font(.system(size: 27, weight: .medium))
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 5) {
                    Text("Muster")
                        .font(.largeTitle.weight(.semibold))
                    Text("Fleet Overview")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }

                Spacer()
            }

            VStack(alignment: .leading, spacing: 24) {
                HStack {
                    Label("Fleet Status", systemImage: "network")
                        .font(.headline)

                    Spacer()

                    Label("Unavailable", systemImage: "minus.circle")
                        .font(.callout.weight(.medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(.quaternary, in: Capsule())
                }

                Divider()

                VStack(spacing: 16) {
                    Image(systemName: "tray")
                        .font(.system(size: 42, weight: .light))
                        .foregroundStyle(.tertiary)
                        .accessibilityHidden(true)

                    Text("Waiting for Fleet Data")
                        .font(.title2.weight(.semibold))

                    Text("Fleet status is unavailable until a data source is connected.")
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 420)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 52)
            }
            .padding(24)
            .background(.background, in: RoundedRectangle(cornerRadius: 16))
            .overlay {
                RoundedRectangle(cornerRadius: 16)
                    .strokeBorder(.quaternary, lineWidth: 1)
            }

            ApplyActivityView()

            Spacer(minLength: 0)
        }
        .applyPlanSheet()
        .padding(36)
        .frame(minWidth: 660, minHeight: 520)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

#Preview {
    FleetOverviewView()
        .preferredColorScheme(.light)
}
