import SwiftUI
import BookdoeKit

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var pendingCount = 0
    @AppStorage(PodcastAutoAdvance.settingKey) private var continueToNext = true
    @AppStorage(Appearance.storageKey) private var appearance = Appearance.system.rawValue

    var body: some View {
        List {
            Section("Servers") {
                NavigationLink("Manage servers") { ServersView() }
                if let s = model.activeServer {
                    LabeledContent("Active", value: "\(s.name) (\(s.kind.displayName))")
                    LabeledContent("Signed in as", value: s.username)
                }
            }
            Section("Appearance") {
                Picker("Theme", selection: $appearance) {
                    ForEach(Appearance.allCases) { Text($0.label).tag($0.rawValue) }
                }
                .pickerStyle(.segmented)
            }
            Section("Playback") {
                Toggle("Continue to next episode", isOn: $continueToNext)
            }
            Section("Sync") {
                LabeledContent("Queued progress updates", value: String(pendingCount))
                Button("Sync now") { Task { await model.flushProgress(session: nil); await refresh() } }
            }
            Section("About") {
                LabeledContent("Version", value: "\(AppModel.appVersion) (\(AppModel.buildNumber))")
                LabeledContent("Commit", value: String(AppModel.gitSHA.prefix(12)))
                Link("bookdoe on the web", destination: model.activeServer?.baseURL ?? URL(string: "https://example.com")!)
            }
        }
        .readableWidth()
        .navigationTitle("Settings")
        .task { await refresh() }
    }

    private func refresh() async {
        pendingCount = await model.progressQueue.pending.count
    }
}
