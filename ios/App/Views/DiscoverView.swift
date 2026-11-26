import SwiftUI
import BookdoeKit

/// Discover chat: ask the assistant for book suggestions. Only reachable when the server's
/// `discover_chat` capability is true (see RootView).
struct DiscoverView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            if let p = model.provider as? BookdoeProvider {
                DiscoverChatScreen(chat: p.discoverChat()).id(model.activeServer?.id)
            } else {
                Text("Discover needs a bookdoe server.").foregroundStyle(.secondary)
            }
        }
        .readableWidth()
        .navigationTitle("Discover")
        .navigationDestination(for: String.self) { ItemDetailView(itemID: $0) }
    }
}

private struct DiscoverChatScreen: View {
    @StateObject private var vm: DiscoverChatViewModel
    @EnvironmentObject private var model: AppModel
    @State private var draft = ""
    @FocusState private var focused: Bool
    private static let bottomID = "discover-bottom"

    init(chat: DiscoverChatClient) {
        _vm = StateObject(wrappedValue: DiscoverChatViewModel(chat: chat))
    }

    var body: some View {
        ScrollViewReader { proxy in
            // The input bar is a plain VStack row, not a safeAreaInset on the ScrollView: nested under the
            // tab-level mini player inset it was left with no room on iPhone (#725). A row always takes its
            // own height, and the VStack still lifts above the keyboard.
            VStack(spacing: 0) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if vm.messages.isEmpty && !vm.isWaiting {
                        Text("Tell me what you feel like reading and I'll suggest books. I'm an AI, so I can be wrong: check anything that matters.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(vm.messages) { m in MessageBubble(message: m, vm: vm) }
                    if vm.isWaiting {
                        HStack { ProgressView(); Text("Thinking…").foregroundStyle(.secondary); Spacer()
                            Button("Cancel") { Task { await vm.cancel() } }
                        }
                    }
                    Color.clear.frame(height: 1).id(Self.bottomID)
                }.padding()
            }
            .scrollDismissesKeyboard(.interactively)
            inputBar
            // The mini player is this row, not the tab-level inset (RootView skips it while Discover shows).
            if !focused { MiniPlayerBar(player: model.player, showPlayer: $model.showPlayer) }
            }
            .onChange(of: vm.messages) { _, _ in scrollToEnd(proxy) }
            .onChange(of: vm.isWaiting) { _, _ in scrollToEnd(proxy) }
            .onChange(of: focused) { _, now in
                model.hidesMiniPlayer = now
                if now { scrollToEnd(proxy, delay: true) }
            }
            .onAppear { model.discoverShowsMiniPlayer = true }
            .onDisappear { model.hidesMiniPlayer = false; model.discoverShowsMiniPlayer = false }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("New chat") { vm.startOver() }.disabled(vm.isWaiting || vm.messages.isEmpty)
            }
        }
        .task { await vm.load() }
    }

    private var inputBar: some View {
        VStack(spacing: 0) {
            if let e = vm.errorText {
                Text(e).font(.footnote).foregroundStyle(.red).padding(.horizontal).frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                TextField("What do you want to read?", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .focused($focused)
                    .accessibilityIdentifier("discover.input")
                    .onSubmit(send)
                Button(action: send) { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                    .disabled(vm.isWaiting || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }.padding()
        }
        .background(.bar)
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy, delay: Bool = false) {
        Task { @MainActor in
            // Wait out the keyboard animation so the target is laid out above the field.
            if delay { try? await Task.sleep(nanoseconds: 350_000_000) }
            withAnimation { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
        }
    }

    private func send() {
        let text = draft
        guard !vm.isWaiting, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        draft = ""
        Task { await vm.send(text) }
    }
}

private struct MessageBubble: View {
    let message: DiscoverMessage
    @ObservedObject var vm: DiscoverChatViewModel

    var body: some View {
        VStack(alignment: message.isAssistant ? .leading : .trailing, spacing: 8) {
            if message.status == .failed || message.status == .cancelled {
                Text(message.errorMessage ?? "That didn't work.").foregroundStyle(.red)
            } else if !message.text.isEmpty {
                Text(message.text)
                    .padding(10)
                    .background(message.isAssistant ? Color.secondary.opacity(0.15) : Color.accentColor.opacity(0.2), in: RoundedRectangle(cornerRadius: 12))
            }
            ForEach(message.cards) { card in CardRow(card: card, message: message, vm: vm) }
            if let f = message.followUp { Text(f).foregroundStyle(.secondary) }
        }
        .frame(maxWidth: .infinity, alignment: message.isAssistant ? .leading : .trailing)
    }
}

private struct CardRow: View {
    let card: DiscoverCard
    let message: DiscoverMessage
    @ObservedObject var vm: DiscoverChatViewModel

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if let id = card.libraryItemID {
                NavigationLink(value: id) { info }.buttonStyle(.plain)
            } else {
                info
            }
            Spacer(minLength: 0)
            VStack {
                thumb("hand.thumbsup", "up")
                thumb("hand.thumbsdown", "down")
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
    }

    private var info: some View {
        HStack(alignment: .top, spacing: 12) {
            CoverImage(url: card.coverURL, size: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text(card.title).font(.headline)
                if !card.author.isEmpty { Text(card.author).font(.subheadline).foregroundStyle(.secondary) }
                if !card.why.isEmpty { Text(card.why).font(.footnote) }
                Text(card.inLibrary ? "In your library" : "Not in your library").font(.caption2).foregroundStyle(card.inLibrary ? Color.green : Color.secondary)
            }
        }
    }

    private func thumb(_ symbol: String, _ verdict: String) -> some View {
        Button { Task { await vm.setFeedback(verdict, on: card, in: message) } } label: {
            Image(systemName: card.feedback == verdict ? symbol + ".fill" : symbol)
        }.buttonStyle(.borderless)
    }
}
