import SwiftUI
import BookdoeKit
#if canImport(UIKit)
import UIKit
#endif

struct ServersView: View {
    @EnvironmentObject private var model: AppModel
    var forceAdd = false
    @State private var showAdd = false

    var body: some View {
        List {
            ForEach(model.servers) { s in
                Button {
                    model.setActive(s)
                } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(s.name)
                            Text("\(s.kind.displayName)  \(s.baseURL.host ?? s.baseURL.absoluteString)  \(s.username)").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if s.id == model.activeServer?.id { Image(systemName: "checkmark").foregroundStyle(.tint) }
                    }
                }
                .tint(.primary)
            }
            .onDelete { idx in
                let targets = idx.map { model.servers[$0] }
                Task { for s in targets { await model.removeServer(s) } }
            }
            if model.servers.isEmpty {
                WordmarkHeader().listRowBackground(Color.clear).listRowInsets(EdgeInsets())
                Text("Add a bookdoe, Jellyfin or Audiobookshelf server to begin.").foregroundStyle(.secondary)
            }
        }
        .readableWidth()
        .navigationTitle("Servers")
        .toolbar { Button { showAdd = true } label: { Image(systemName: "plus") } }
        .sheet(isPresented: $showAdd) { NavigationStack { AddServerView() }.addServerSheetSizing() }
        .onAppear { if forceAdd && model.servers.isEmpty { showAdd = true } }
    }
}

private extension View {
    /// On iPad the default form sheet is a small centred card the keyboard covers;
    /// iOS 18 can size it to the full page so the fields stay reachable.
    @ViewBuilder func addServerSheetSizing() -> some View {
        if #available(iOS 18.0, *) { self.presentationSizing(.page) } else { self }
    }
}

struct AddServerView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    @State private var kind: ServerKind = .bookdoe
    @State private var name = ""
    @State private var urlText = ""
    @State private var username = ""
    @State private var password = ""
    @State private var busy = false
    @State private var error: String?
    @State private var info: ServerInfo?
    @State private var pairing: DevicePairing?
    @State private var pairingRecord: ServerRecord?
    @State private var pairingProvider: BookdoeProvider?
    @State private var pairingTask: Task<Void, Never>?
    @State private var codeCopied = false
    @State private var showHttpWarning = false
    @State private var pendingAction: (() -> Void)?
    private enum Field: Hashable { case name, url, username, password }
    @FocusState private var focus: Field?

    private var url: URL? {
        var text = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.contains("://") { text = "https://" + text }
        guard let u = URL(string: text), u.host != nil, let scheme = u.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return nil }
        return u
    }

    private var isHttpOnly: Bool {
        url?.scheme?.lowercased() == "http"
    }

    var body: some View {
        ScrollViewReader { proxy in
        Form {
            if model.servers.isEmpty {
                Section { WordmarkHeader() }.listRowBackground(Color.clear).listRowInsets(EdgeInsets())
            }
            Section("Server") {
                Picker("Type", selection: $kind) { ForEach(ServerKind.allCases, id: \.self) { Text($0.displayName).tag($0) } }
                TextField("Name (optional)", text: $name).focused($focus, equals: .name).id(Field.name)
                TextField("URL, e.g. https://books.example.com", text: $urlText)
                    .focused($focus, equals: .url).id(Field.url).accessibilityIdentifier("server-url")
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .onSubmit { Task { await probe() } }
                Button("Check connection") { Task { await probe() } }.disabled(url == nil || busy)
                if let info {
                    Label("\(info.name) \(info.version ?? "")", systemImage: "checkmark.circle").foregroundStyle(.green)
                    if info.setupRequired { Text("This server has not finished its first-run setup; open it in a browser first.").foregroundStyle(.orange) }
                }
            }
            if info?.setupRequired != true {
                Section("Sign in") {
                    TextField("Username", text: $username).focused($focus, equals: .username).id(Field.username).accessibilityIdentifier("server-username").textInputAutocapitalization(.never).autocorrectionDisabled().textContentType(.username)
                    SecureField("Password", text: $password).focused($focus, equals: .password).id(Field.password).accessibilityIdentifier("server-password").textContentType(.password)
                    Button(busy ? "Signing in..." : "Sign in") {
                        if isHttpOnly {
                            pendingAction = { Task { await signIn() } }
                            showHttpWarning = true
                        } else {
                            Task { await signIn() }
                        }
                    }
                        .disabled(url == nil || username.isEmpty || password.isEmpty || busy || (info?.supportsPasswordLogin == false))
                    if kind == .bookdoe {
                        Button("Pair with a code instead") {
                            if isHttpOnly {
                                pendingAction = { Task { await startPairing() } }
                                showHttpWarning = true
                            } else {
                                Task { await startPairing() }
                            }
                        }.disabled(url == nil || busy)
                    }
                    if kind == .audiobookshelf, info?.supportsOpenID == true {
                        if info?.supportsPasswordLogin == false {
                            Text("This server signs in with single sign-on.").font(.footnote).foregroundStyle(.secondary)
                        }
                        Button(info?.openIDButtonText ?? "Sign in with single sign-on") { Task { await signInWithOpenID() } }
                            .disabled(url == nil || busy)
                    }
                }
            }
            if let pairing {
                Section("Pairing") {
                    Text("On any signed-in browser open the Player, Settings, Pair, and enter this code (the dash is optional):")
                    Text(pairing.userCode).font(.system(.largeTitle, design: .monospaced)).frame(maxWidth: .infinity)
                        .textSelection(.enabled)
                        .accessibilityLabel("Pairing code " + pairing.userCode.map(String.init).joined(separator: " "))
                    Button {
                        #if canImport(UIKit)
                        UIPasteboard.general.string = pairing.userCode
                        #endif
                        codeCopied = true
                        Task { try? await Task.sleep(nanoseconds: 2_000_000_000); codeCopied = false }
                    } label: {
                        Label(codeCopied ? "Copied" : "Copy code", systemImage: codeCopied ? "checkmark" : "doc.on.doc")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    if let v = pairing.verificationURL { Link("Open the approval page", destination: v) }
                    ProgressView("Waiting for approval...")
                }
            }
            if let error { Section { Text(error).foregroundStyle(.red) } }
        }
        .navigationTitle("Add server")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { pairingTask?.cancel(); dismiss() } } }
        .scrollDismissesKeyboard(.interactively)
        .onChange(of: focus) { _, field in
            guard let field else { return }
            // Wait for the keyboard to appear, then keep the focused field on screen.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                withAnimation { proxy.scrollTo(field, anchor: .center) }
            }
        }
        .onChange(of: kind) { _, _ in info = nil }
        .alert("Unencrypted connection", isPresented: $showHttpWarning) {
            Button("Use anyway", role: .destructive) {
                pendingAction?()
                pendingAction = nil
            }
            Button("Cancel", role: .cancel) { pendingAction = nil }
        } message: {
            Text("The server URL uses HTTP instead of HTTPS. Your credentials and data will be transmitted in plaintext. Only proceed if you understand the risks.")
        }
        }
    }

    private func probe() async {
        guard let url else { return }
        busy = true; defer { busy = false }
        error = nil
        let record = ServerRecord(name: name, kind: kind, baseURL: url, username: username)
        do { info = try await model.factory.make(record).probe() } catch { self.error = (error as? APIError)?.errorDescription ?? error.localizedDescription }
    }

    private func signIn() async {
        guard let url else { return }
        busy = true; defer { busy = false }
        error = nil
        do {
            _ = try await model.addServer(kind: kind, name: name.isEmpty ? (url.host ?? kind.displayName) : name, url: url, username: username, password: password)
            dismiss()
        } catch { self.error = (error as? APIError)?.errorDescription ?? error.localizedDescription }
    }

    /// Audiobookshelf single sign-on: ask the server where to sign in, let the person do that in
    /// the system's web sheet, then swap the code it returns for a session.
    private func signInWithOpenID() async {
        guard let url else { return }
        busy = true
        defer { busy = false }
        error = nil
        do {
            let record = ServerRecord(name: name.isEmpty ? (url.host ?? "Audiobookshelf") : name, kind: .audiobookshelf, baseURL: url, username: "")
            guard let provider = model.factory.make(record) as? AudiobookshelfProvider else { return }
            let start = try await provider.beginOpenID()
            let web = WebLogin()
            let callback = try await web.run(url: start.authorizeURL, callbackScheme: AudiobookshelfProvider.openIDScheme)
            let result = try await provider.completeOpenID(callback: callback, start: start)
            model.addSignedIn(server: result.server)
            dismiss()
        } catch is CancellationError {
            // The person closed the sign-in sheet: nothing to report.
        } catch {
            self.error = (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    private func startPairing() async {
        guard let url else { return }
        busy = true
        error = nil
        do {
            let (record, provider, p) = try await model.startPairing(name: name.isEmpty ? (url.host ?? "bookdoe") : name, url: url)
            pairing = p
            pairingRecord = record
            pairingProvider = provider
            pairingTask = Task { await poll(provider: provider, record: record, pairing: p) }
        } catch {
            self.error = (error as? APIError)?.errorDescription ?? error.localizedDescription
            busy = false
        }
    }

    private func poll(provider: BookdoeProvider, record: ServerRecord, pairing: DevicePairing) async {
        var interval = Double(pairing.interval)
        let deadline = Date().addingTimeInterval(Double(pairing.expiresIn))
        // Switching to Safari to approve suspends the app, which kills the request in
        // flight ("cancelled", "connection lost"). Those are transient: keep polling.
        var failures = 0
        while !Task.isCancelled && Date() < deadline {
            try? await Task.sleep(for: .seconds(interval))
            if Task.isCancelled { break }
            do {
                switch try await provider.pollPairing(pairing) {
                case .pending: failures = 0; continue
                case .slowDown: failures = 0; interval += 5
                case .denied: error = "The pairing request was denied."; break
                case .expired: error = "The code expired; ask for a new one."; break
                case .approved(let result):
                    try model.finishPairing(record: record, result: result)
                    dismiss()
                    return
                }
                if error != nil { break }
            } catch {
                if Task.isCancelled { break }
                if case APIError.network = error, failures < 12 { failures += 1; continue }
                self.error = (error as? APIError)?.errorDescription ?? error.localizedDescription
                break
            }
        }
        self.pairing = nil
        busy = false
    }
}

/// The bookdoe logo and tagline, on a white card so it reads in light and dark mode.
struct WordmarkHeader: View {
    var body: some View {
        Image("Wordmark")
            .resizable()
            .scaledToFit()
            .padding(12)
            .frame(maxWidth: .infinity)
            .background(Color.white, in: RoundedRectangle(cornerRadius: 16))
            .padding(.vertical, 8)
            .accessibilityLabel("bookdoe: every story, one library")
    }
}
