import XCTest
@testable import BookdoeKit

final class DiscoverChatTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: BookdoeProvider!
    var chat: DiscoverChatClient!
    let server = makeServer(.bookdoe)
    let base = "/api/player/v1/discover/chat"

    override func setUp() async throws {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        try creds.save(Credential(accessToken: "alxd_tok"), for: server.id)
        provider = BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1.2")
        chat = provider.discoverChat()
    }

    func caps(_ extra: [String: Any] = [:]) -> [String: Any] {
        var c: [String: Any] = ["server": "bookdoe", "version": "1", "auth": ["password_login": true]]
        for (k, v) in extra { c[k] = v }
        return c
    }

    func pending(_ id: String = "m2") -> [String: Any] {
        ["id": id, "conversation_id": "c1", "role": "assistant", "status": "pending", "text": ""]
    }

    func done(_ id: String = "m2") -> [String: Any] {
        ["id": id, "conversation_id": "c1", "role": "assistant", "status": "done", "text": "Try these.", "follow_up": "Ebook or audio?",
         "cards": [
            ["title": "Dune", "author": "Frank Herbert", "why": "Epic.", "group": "library", "work_id": "w1", "cover_url": "/api/player/v1/works/w1/cover"],
            ["title": "Elsewhere", "author": "Someone", "why": "Cozy.", "group": "not_in_library", "work_id": NSNull()],
         ]]
    }

    // MARK: capabilities

    func testCapabilityTrue() async throws {
        client.enqueueJSON(200, caps(["discover_chat": true]))
        let info = try await provider.probe()
        XCTAssertTrue(info.supportsDiscoverChat)
    }

    func testCapabilityFalse() async throws {
        client.enqueueJSON(200, caps(["discover_chat": false]))
        let info = try await provider.probe()
        XCTAssertFalse(info.supportsDiscoverChat)
    }

    func testCapabilityMissingMeansFalse() async throws {
        client.enqueueJSON(200, caps())
        let info = try await provider.probe()
        XCTAssertFalse(info.supportsDiscoverChat)
    }

    // MARK: entry visibility

    func testEntryHiddenWhenFlagFalseOrMissing() async {
        client.enqueueJSON(200, caps(["discover_chat": false]))
        let offFalse = await DiscoverAvailability.resolve(provider: provider)
        XCTAssertFalse(offFalse)
        client.enqueueJSON(200, caps())
        let offMissing = await DiscoverAvailability.resolve(provider: provider)
        XCTAssertFalse(offMissing)
    }

    func testEntryShownWhenFlagTrue() async {
        client.enqueueJSON(200, caps(["discover_chat": true]))
        let on = await DiscoverAvailability.resolve(provider: provider)
        XCTAssertTrue(on)
    }

    func testEntryHiddenWhenServerUnreachableOrNoServerOrOtherKind() async {
        // nothing queued: the probe fails
        let unreachable = await DiscoverAvailability.resolve(provider: provider)
        XCTAssertFalse(unreachable)
        let none = await DiscoverAvailability.resolve(provider: nil)
        XCTAssertFalse(none)
        XCTAssertFalse(DiscoverAvailability.isOffered(kind: .jellyfin, info: ServerInfo(name: "j", supportsPasswordLogin: true, supportsDiscoverChat: true)))
    }

    // MARK: client

    func testCreateConversationAndPostThenPollToCompletion() async throws {
        client.enqueueJSON(201, ["id": "c1", "title": "New chat", "turns": 0])
        let conv = try await chat.createConversation()
        XCTAssertEqual(conv.id, "c1")
        XCTAssertEqual(client.last?.method, .post)
        XCTAssertEqual(client.last?.url.path, base + "/conversations")
        XCTAssertEqual(client.last?.headers["Authorization"], "Bearer alxd_tok")

        client.enqueueJSON(202, ["message_id": "m2", "user_message_id": "m1", "status": "pending", "conversation_id": "c1", "message": pending()])
        let reply = try await chat.post("cozy mysteries", in: "c1")
        XCTAssertEqual(reply.status, .pending)
        XCTAssertEqual(client.last?.url.path, base + "/conversations/c1/messages")
        XCTAssertEqual(client.last?.bodyJSON?["text"] as? String, "cozy mysteries")

        client.enqueueJSON(200, pending())
        client.enqueueJSON(200, done())
        let finished = try await chat.waitForReply("m2", in: "c1", interval: 0, sleep: { _ in })
        XCTAssertEqual(finished.status, .done)
        XCTAssertEqual(finished.text, "Try these.")
        XCTAssertEqual(finished.followUp, "Ebook or audio?")
        XCTAssertEqual(finished.cards.count, 2)
        XCTAssertEqual(finished.cards[0].libraryItemID, "w1")
        XCTAssertEqual(finished.cards[0].coverURL?.path, "/api/player/v1/works/w1/cover")
        XCTAssertNil(finished.cards[1].libraryItemID)
        XCTAssertEqual(client.requests.filter { $0.url.path == base + "/conversations/c1/messages/m2" }.count, 2)
    }

    func testListConversations() async throws {
        client.enqueueJSON(200, ["items": [["id": "c1", "title": "Mystery", "turns": 2]]])
        let list = try await chat.conversations()
        XCTAssertEqual(list, [DiscoverConversation(id: "c1", title: "Mystery", turns: 2)])
    }

    func testCancelAndFeedback() async throws {
        var cancelled = pending(); cancelled["status"] = "cancelled"; cancelled["error"] = "cancelled"; cancelled["error_message"] = "Cancelled."
        client.enqueueJSON(200, cancelled)
        let m = try await chat.cancel("m2", in: "c1")
        XCTAssertEqual(m.status, .cancelled)
        XCTAssertEqual(m.errorMessage, "Cancelled.")
        XCTAssertEqual(client.last?.method, .post)
        XCTAssertEqual(client.last?.url.path, base + "/conversations/c1/messages/m2/cancel")

        client.enqueueJSON(200, ["index": 0, "feedback": "up"])
        try await chat.feedback(messageID: "m2", cardIndex: 0, verdict: "up")
        XCTAssertEqual(client.last?.url.path, base + "/messages/m2/feedback")
        XCTAssertEqual(client.last?.bodyJSON?["index"] as? Int, 0)
        XCTAssertEqual(client.last?.bodyJSON?["verdict"] as? String, "up")
    }

    func testFailedReplyCarriesMessage() async throws {
        client.enqueueJSON(200, ["id": "m2", "role": "assistant", "status": "failed", "error": "timeout", "error_message": "That took too long."])
        let m = try await chat.waitForReply("m2", in: "c1", interval: 0, sleep: { _ in })
        XCTAssertEqual(m.status, .failed)
        XCTAssertEqual(m.errorMessage, "That took too long.")
    }

    func testHTTPErrorsMap() async {
        client.enqueueJSON(503, ["detail": "Discover chat is not available right now"])
        do { _ = try await chat.post("hi", in: "c1"); XCTFail("expected an error") } catch {
            XCTAssertEqual(error as? APIError, .server(status: 503, message: "Discover chat is not available right now"))
        }
        client.enqueueJSON(403, ["detail": "Discover chat needs a signed-in session or a trusted browser, not a reader token or API key"])
        do { _ = try await chat.createConversation(); XCTFail("expected an error") } catch {
            guard case .forbidden = error as? APIError else { return XCTFail("expected forbidden, got \(error)") }
        }
    }

    func testPollGivesUpWhenNeverSettled() async {
        client.router = { _ in .json(200, ["id": "m2", "role": "assistant", "status": "pending"]) }
        do { _ = try await chat.waitForReply("m2", in: "c1", interval: 0, maxPolls: 3, sleep: { _ in }); XCTFail("expected an error") } catch {
            guard case .network = error as? APIError else { return XCTFail("expected network, got \(error)") }
        }
        XCTAssertEqual(client.requests.count, 3)
    }

    // MARK: view model

    @MainActor
    func testViewModelSendShowsReply() async {
        client.enqueueJSON(201, ["id": "c1", "title": "New chat", "turns": 0])
        client.enqueueJSON(202, ["message": pending()])
        client.enqueueJSON(200, done())
        let vm = DiscoverChatViewModel(chat: chat, pollInterval: 0, sleep: { _ in })
        await vm.send("  something cozy ")
        XCTAssertNil(vm.errorText)
        XCTAssertEqual(vm.messages.count, 2)
        XCTAssertFalse(vm.messages[0].isAssistant)
        XCTAssertEqual(vm.messages[0].text, "something cozy")
        XCTAssertEqual(vm.messages[1].status, .done)
        XCTAssertEqual(vm.messages[1].cards.first?.libraryItemID, "w1")
        XCTAssertFalse(vm.isWaiting)
    }

    @MainActor
    func testFailedReplyShowsErrorAndConversationStaysUsable() async {
        client.enqueueJSON(201, ["id": "c1", "title": "New chat", "turns": 0])
        client.enqueueJSON(202, ["message": pending("m2")])
        client.enqueueJSON(200, ["id": "m2", "role": "assistant", "status": "failed", "error": "unreachable", "error_message": "I couldn't reach my book brain."])
        let vm = DiscoverChatViewModel(chat: chat, pollInterval: 0, sleep: { _ in })
        await vm.send("hello")
        XCTAssertEqual(vm.errorText, "I couldn't reach my book brain.")
        XCTAssertEqual(vm.messages.last?.status, .failed)
        XCTAssertFalse(vm.isWaiting)

        // second message goes to the same conversation (no second create) and succeeds
        client.enqueueJSON(202, ["message": pending("m4")])
        client.enqueueJSON(200, done("m4"))
        await vm.send("try again")
        XCTAssertNil(vm.errorText)
        XCTAssertEqual(vm.messages.last?.status, .done)
        XCTAssertEqual(client.requests.filter { $0.method == .post && $0.url.path == base + "/conversations" }.count, 1)
        XCTAssertEqual(client.requests.filter { $0.url.path == base + "/conversations/c1/messages" }.count, 2)
    }

    @MainActor
    func testNetworkErrorOnPostShowsErrorAndStaysUsable() async {
        client.enqueueJSON(201, ["id": "c1", "title": "New chat", "turns": 0])
        client.enqueueJSON(409, ["detail": "I'm still working on your last message"])
        let vm = DiscoverChatViewModel(chat: chat, pollInterval: 0, sleep: { _ in })
        await vm.send("hello")
        XCTAssertEqual(vm.errorText, "I'm still working on your last message")
        XCTAssertFalse(vm.isWaiting)
        client.enqueueJSON(202, ["message": pending("m4")])
        client.enqueueJSON(200, done("m4"))
        await vm.send("again")
        XCTAssertNil(vm.errorText)
        XCTAssertEqual(vm.messages.last?.status, .done)
    }

    @MainActor
    func testCancelPendingReply() async {
        client.enqueueJSON(201, ["id": "c1", "title": "New chat", "turns": 0])
        client.enqueueJSON(202, ["message": pending("m2")])
        var cancelled = pending("m2"); cancelled["status"] = "cancelled"; cancelled["error_message"] = "Cancelled."
        // the first poll still says pending, the cancel call answers cancelled, the next poll sees it
        let vm = DiscoverChatViewModel(chat: chat, pollInterval: 0, sleep: { _ in })
        client.router = { req in
            if req.url.path.hasSuffix("/cancel") || req.method == .get { return .json(200, cancelled) }
            return nil
        }
        await vm.send("hello")
        XCTAssertEqual(vm.messages.last?.status, .cancelled)
        XCTAssertEqual(vm.errorText, "Cancelled.")
        XCTAssertFalse(vm.isWaiting)
    }
}
