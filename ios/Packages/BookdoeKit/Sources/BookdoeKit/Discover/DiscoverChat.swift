import Foundation
#if canImport(Combine)
import Combine
#endif

// MARK: - models

public struct DiscoverConversation: Identifiable, Equatable, Sendable {
    public let id: String
    public var title: String
    public var turns: Int

    public init(id: String, title: String = "New chat", turns: Int = 0) {
        self.id = id
        self.title = title
        self.turns = turns
    }
}

/// One suggested book. `workID` is set when the server matched it to a library work.
public struct DiscoverCard: Identifiable, Equatable, Sendable {
    public var id: String { "\(index)" }
    public let index: Int
    public var title: String
    public var author: String
    public var why: String
    public var inLibrary: Bool
    public var workID: String?
    public var coverURL: URL?
    public var feedback: String?
    public var unverified: Bool

    public init(index: Int, title: String, author: String = "", why: String = "", inLibrary: Bool = false, workID: String? = nil,
                coverURL: URL? = nil, feedback: String? = nil, unverified: Bool = false) {
        self.index = index
        self.title = title
        self.author = author
        self.why = why
        self.inLibrary = inLibrary
        self.workID = workID
        self.coverURL = coverURL
        self.feedback = feedback
        self.unverified = unverified
    }

    /// The library item to open on a tap, or nil for a book the library does not have.
    public var libraryItemID: String? { inLibrary ? workID : nil }
}

public enum DiscoverStatus: String, Equatable, Sendable {
    case pending, done, failed, cancelled
}

public struct DiscoverMessage: Identifiable, Equatable, Sendable {
    public let id: String
    public var isAssistant: Bool
    public var status: DiscoverStatus
    public var text: String
    public var followUp: String?
    public var cards: [DiscoverCard]
    /// Plain-language reason when `status` is failed or cancelled.
    public var errorMessage: String?

    public init(id: String, isAssistant: Bool, status: DiscoverStatus = .done, text: String = "", followUp: String? = nil,
                cards: [DiscoverCard] = [], errorMessage: String? = nil) {
        self.id = id
        self.isAssistant = isAssistant
        self.status = status
        self.text = text
        self.followUp = followUp
        self.cards = cards
        self.errorMessage = errorMessage
    }
}

// MARK: - client

/// The Discover chat routes (`/api/player/v1/discover/chat/...`). The assistant answers off the
/// request, so a post returns a pending message that `waitForReply` polls until it settles.
public final class DiscoverChatClient: BaseProvider {
    private let api = "/api/player/v1/discover/chat"

    private func authed(_ req: HTTPRequest) throws -> HTTPRequest {
        var r = req
        r.headers["Authorization"] = "Bearer \(try credential().accessToken)"
        return r
    }

    public func conversations() async throws -> [DiscoverConversation] {
        let obj = try await send(try authed(HTTPRequest(url: url(api + "/conversations")))).jsonObject()
        return JSON.array(obj, "items").compactMap(parseConversation)
    }

    public func createConversation() async throws -> DiscoverConversation {
        let obj = try await send(try authed(HTTPRequest(method: .post, url: url(api + "/conversations")))).jsonObject()
        guard let c = parseConversation(obj) else { throw APIError.decoding("conversation without an id") }
        return c
    }

    public func messages(in conversationID: String) async throws -> [DiscoverMessage] {
        let obj = try await send(try authed(HTTPRequest(url: url(api + "/conversations/\(conversationID)")))).jsonObject()
        return JSON.array(obj, "messages").compactMap(parseMessage)
    }

    /// Post the reader's text. Returns the assistant message, normally still `pending`.
    public func post(_ text: String, in conversationID: String) async throws -> DiscoverMessage {
        let req = try HTTPRequest.json(.post, url(api + "/conversations/\(conversationID)/messages"), body: ["text": text])
        let obj = try await send(try authed(req)).jsonObject()
        guard let message = JSON.object(obj, "message").flatMap(parseMessage) else { throw APIError.decoding("no message in the answer") }
        return message
    }

    public func message(_ messageID: String, in conversationID: String) async throws -> DiscoverMessage {
        let obj = try await send(try authed(HTTPRequest(url: url(api + "/conversations/\(conversationID)/messages/\(messageID)")))).jsonObject()
        guard let message = parseMessage(obj) else { throw APIError.decoding("message without an id") }
        return message
    }

    /// Poll until the message is no longer pending. A server that never settles ends after
    /// `maxPolls` polls with `.network`, so a stuck reply cannot hold the screen forever.
    public func waitForReply(_ messageID: String, in conversationID: String, interval: TimeInterval = 1.5, maxPolls: Int = 120,
                             sleep: @Sendable (TimeInterval) async -> Void = { _ = try? await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000)) }) async throws -> DiscoverMessage {
        for _ in 0..<maxPolls {
            if Task.isCancelled { throw CancellationError() }
            let m = try await message(messageID, in: conversationID)
            if m.status != .pending { return m }
            await sleep(interval)
        }
        throw APIError.network("The assistant took too long to answer.")
    }

    public func cancel(_ messageID: String, in conversationID: String) async throws -> DiscoverMessage {
        let obj = try await send(try authed(HTTPRequest(method: .post, url: url(api + "/conversations/\(conversationID)/messages/\(messageID)/cancel")))).jsonObject()
        guard let message = parseMessage(obj) else { throw APIError.decoding("message without an id") }
        return message
    }

    /// Thumbs on a card: "up", "down" or nil to take it back.
    public func feedback(messageID: String, cardIndex: Int, verdict: String?) async throws {
        let body: [String: Any] = ["index": cardIndex, "verdict": verdict ?? NSNull()]
        let req = try HTTPRequest.json(.post, url(api + "/messages/\(messageID)/feedback"), body: body)
        _ = try await send(try authed(req))
    }

    // MARK: parsing

    func parseConversation(_ o: [String: Any]) -> DiscoverConversation? {
        guard let id = JSON.string(o, "id") else { return nil }
        return DiscoverConversation(id: id, title: JSON.string(o, "title") ?? "New chat", turns: JSON.int(o, "turns") ?? 0)
    }

    func parseMessage(_ o: [String: Any]) -> DiscoverMessage? {
        guard let id = JSON.string(o, "id") else { return nil }
        let status = JSON.string(o, "status").flatMap(DiscoverStatus.init(rawValue:)) ?? .failed
        let cards = JSON.array(o, "cards").enumerated().compactMap { parseCard($0.element, index: $0.offset) }
        var error = JSON.string(o, "error_message")
        if error == nil && (status == .failed || status == .cancelled) { error = status == .cancelled ? "Cancelled." : "Something went wrong. Please try again." }
        return DiscoverMessage(id: id, isAssistant: JSON.string(o, "role") != "user", status: status, text: JSON.string(o, "text") ?? "",
                               followUp: JSON.string(o, "follow_up").flatMap { $0.isEmpty ? nil : $0 }, cards: cards, errorMessage: error)
    }

    func parseCard(_ o: [String: Any], index: Int) -> DiscoverCard? {
        guard let title = JSON.string(o, "title"), !title.isEmpty else { return nil }
        let workID = JSON.string(o, "work_id")
        let inLibrary = JSON.string(o, "group") == "library" && workID != nil
        let cover = JSON.string(o, "cover_url").flatMap { server.baseURL.resolving($0) }
        return DiscoverCard(index: index, title: title, author: JSON.string(o, "author") ?? "", why: JSON.string(o, "why") ?? "",
                            inLibrary: inLibrary, workID: workID, coverURL: cover, feedback: JSON.string(o, "feedback"),
                            unverified: JSON.bool(o, "unverified") ?? false)
    }
}

// MARK: - availability

/// Whether the Discover entry is offered: only on a bookdoe (bookdoe) server whose
/// `discover_chat` capability is true. Anything else, including an unreachable server, hides it.
public enum DiscoverAvailability {
    public static func isOffered(kind: ServerKind?, info: ServerInfo?) -> Bool {
        kind == .bookdoe && info?.supportsDiscoverChat == true
    }

    public static func resolve(provider: MediaProvider?) async -> Bool {
        guard let provider, provider.kind == .bookdoe else { return false }
        let info = try? await provider.probe()
        return isOffered(kind: provider.kind, info: info)
    }
}

// MARK: - view model

/// One chat screen's state. A failed or cancelled reply is shown in the transcript with its
/// error and the conversation stays usable: the next message goes to the same conversation.
@MainActor
public final class DiscoverChatViewModel {
    public private(set) var messages: [DiscoverMessage] = [] { didSet { changed() } }
    public private(set) var isWaiting = false { didSet { changed() } }
    public private(set) var errorText: String? { didSet { changed() } }
    public private(set) var conversationID: String?

    private let chat: DiscoverChatClient
    private let interval: TimeInterval
    private var pendingMessageID: String?
    private let sleep: @Sendable (TimeInterval) async -> Void

    public init(chat: DiscoverChatClient, pollInterval: TimeInterval = 1.5,
                sleep: @escaping @Sendable (TimeInterval) async -> Void = { _ = try? await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000)) }) {
        self.chat = chat
        self.interval = pollInterval
        self.sleep = sleep
    }

    private func changed() {
        #if canImport(Combine)
        objectWillChange.send()
        #endif
    }

    /// Open the most recent conversation with its messages, if there is one.
    public func load() async {
        do {
            guard let latest = try await chat.conversations().first else { return }
            conversationID = latest.id
            messages = try await chat.messages(in: latest.id)
        } catch { errorText = Self.describe(error) }
    }

    /// Forget the current conversation on screen; the next message starts a new one.
    public func startOver() {
        conversationID = nil
        messages = []
        errorText = nil
    }

    public func send(_ rawText: String) async {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isWaiting else { return }
        errorText = nil
        isWaiting = true
        defer { isWaiting = false; pendingMessageID = nil }
        do {
            let convID: String
            if let existing = conversationID { convID = existing } else {
                convID = try await chat.createConversation().id
                conversationID = convID
            }
            let reply = try await chat.post(text, in: convID)
            messages.append(DiscoverMessage(id: "local-\(messages.count)", isAssistant: false, text: text))
            messages.append(reply)
            pendingMessageID = reply.id
            let settled = try await chat.waitForReply(reply.id, in: convID, interval: interval, sleep: sleep)
            replace(settled)
            if settled.status != .done { errorText = settled.errorMessage }
        } catch {
            errorText = Self.describe(error)
            if let id = pendingMessageID { replace(DiscoverMessage(id: id, isAssistant: true, status: .failed, errorMessage: errorText)) }
        }
    }

    /// Stop waiting for the pending reply. The conversation stays usable.
    public func cancel() async {
        guard let convID = conversationID, let id = pendingMessageID else { return }
        do {
            let m = try await chat.cancel(id, in: convID)
            replace(m)
        } catch { errorText = Self.describe(error) }
    }

    public func setFeedback(_ verdict: String?, on card: DiscoverCard, in message: DiscoverMessage) async {
        do {
            try await chat.feedback(messageID: message.id, cardIndex: card.index, verdict: verdict)
            update(message.id) { m in
                if let i = m.cards.firstIndex(where: { $0.index == card.index }) { m.cards[i].feedback = m.cards[i].feedback == verdict ? nil : verdict }
            }
        } catch { errorText = Self.describe(error) }
    }

    private func replace(_ m: DiscoverMessage) {
        if let i = messages.firstIndex(where: { $0.id == m.id }) { messages[i] = m } else { messages.append(m) }
    }

    private func update(_ id: String, _ change: (inout DiscoverMessage) -> Void) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        var m = messages[i]
        change(&m)
        messages[i] = m
    }

    static func describe(_ error: Error) -> String {
        (error as? APIError)?.errorDescription ?? error.localizedDescription
    }
}

#if canImport(Combine)
extension DiscoverChatViewModel: ObservableObject {}
#endif
