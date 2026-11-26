import SwiftUI
import WebKit
import PDFKit
import BookdoeKit

/// A book file on disk ready to open, with where the reader should start.
struct ReaderFile: Identifiable, Equatable {
    let id = UUID()
    var url: URL
    var format: String      // "epub" or "pdf"
    var title: String
    var itemID: String
    var assetID: String
    var start: String?      // EPUB CFI or PDF page, as the server stores it
}

/// A passage the person has selected in markup mode.
struct TextSelection: Equatable {
    var cfiRange: String
    var text: String
    var percent: Double?
}

/// Owns the web view bridge and the debounced position saving for one reading session.
@MainActor
final class ReaderController: ObservableObject {
    struct TOCEntry: Identifiable { let id = UUID(); let label: String; let href: String; let depth: Int }

    @Published var chromeVisible = false
    /// "live" (the web view is showing), "preparing" or "curl" (page pictures ready); the UI tests wait on it.
    @Published var pagingMode = "live"
    /// Debug builds: a running log of page moves, for the UI tests.
    @Published var debugLog = ""
    func log(_ line: String) {
        #if DEBUG
        debugLog += String(format: "%.2f ", Date().timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1000)) + line + "\n"
        #endif
    }
    @Published var ready = false
    @Published var error: String?
    @Published var toc: [TOCEntry] = []
    @Published var percent: Double?
    @Published var chapter = ""

    weak var webView: WKWebView?
    weak var host: ReaderViewController?
    weak var pdfView: PDFView?

    /// Markup mode: the live page with text selection, to highlight passages. Page curls are off meanwhile.
    @Published var markupMode = false { didSet { host?.markupChanged(markupMode) } }
    @Published var selection: TextSelection?
    @Published var activeHighlightID: String?
    /// Ids of the bookmarks on the page being shown (ebooks).
    @Published var pageBookmarkIDs: [String] = []
    @Published var pdfPage = 1
    /// Where the page being read starts (an EPUB CFI), for a bookmark made now.
    @Published var currentLocation: String?

    private(set) var highlightList: [Bookmark] = []
    private(set) var bookmarkList: [Bookmark] = []

    /// Tell the page which highlights to draw and which bookmarks to flag. A changed highlight set also
    /// means the page pictures of the curl are stale, so they are made again.
    func setMarks(highlights: [Bookmark], bookmarks: [Bookmark]) {
        func signature(_ l: [Bookmark]) -> [String] { l.map { "\($0.id)|\($0.location ?? "")|\($0.color ?? "")" } }
        let changed = signature(highlights) != signature(highlightList)
        highlightList = highlights
        bookmarkList = bookmarks
        pushMarks()
        if changed, ready, !markupMode { host?.invalidateSnapshots(rebuildAfter: 0.6) }
    }

    func pushMarks() {
        guard ready else { return }
        let hs: [[String: Any]] = highlightList.compactMap { b in b.location.map { ["id": b.id, "cfiRange": $0, "color": b.color ?? "yellow"] } }
        let bs: [[String: Any]] = bookmarkList.compactMap { b in b.location.map { ["id": b.id, "cfi": $0] } }
        run("alx.setHighlights(\(Self.json(hs)))")
        run("alx.setBookmarks(\(Self.json(bs)))")
    }

    func clearSelection() {
        selection = nil
        run("alx.clearSelection()")
    }

    func goToPDFPage(_ n: Int) {
        guard let doc = pdfView?.document, let page = doc.page(at: max(0, min(doc.pageCount - 1, n - 1))) else { return }
        pdfView?.go(to: page)
    }

    var onSave: ((String, Double?) -> Void)?
    /// "curl" animates page turns, "none" does not.
    var turnStyle = "curl"
    /// The page colour behind the curl, so a turning page matches the theme.
    var pageColor: UIColor = .white

    private var baseline: String?
    private var pending: (String, Double?)?
    private var saveTask: Task<Void, Never>?

    func run(_ js: String) { webView?.evaluateJavaScript(js, completionHandler: nil) }
    func next() { if let host { host.turn(forward: true) } else { run("alx.next()") } }
    func prev() { if let host { host.turn(forward: false) } else { run("alx.prev()") } }
    func goto(_ href: String) {
        run("alx.goto(\(Self.jsString(href)))")
        host?.invalidateSnapshots(rebuildAfter: 0.7)
    }
    func setPrefs(_ prefs: [String: Any]) {
        run("alx.setPrefs(\(Self.json(prefs)))")
        host?.invalidateSnapshots(rebuildAfter: 0.7)
    }
    func toggleChrome() { withAnimation { chromeVisible.toggle() } }

    /// A position came from the page. The first one is the page the book opened on and is
    /// not saved, so opening a book never overwrites a real position with the start.
    func noteLocation(_ location: String, percent: Double?) {
        currentLocation = location
        self.percent = percent ?? self.percent
        if baseline == nil { baseline = location }
        guard location != baseline else { return }
        pending = (location, percent)
        saveTask?.cancel()
        saveTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            if !Task.isCancelled { self?.flush() }
        }
    }

    func flush() {
        saveTask?.cancel()
        if let p = pending { onSave?(p.0, p.1); pending = nil }
    }

    static func json(_ object: Any) -> String {
        guard JSONSerialization.isValidJSONObject(object), let d = try? JSONSerialization.data(withJSONObject: object), let s = String(data: d, encoding: .utf8) else { return "{}" }
        return s
    }
    static func jsString(_ s: String) -> String {
        let arr = json([s])
        return String(arr.dropFirst().dropLast())
    }
}

// MARK: EPUB web view

/// Serves the bundled reader page and the book file to the web view over a private
/// scheme, so the page and the book share one origin and need no file access.
final class ReaderSchemeHandler: NSObject, WKURLSchemeHandler {
    let bookURL: URL
    init(bookURL: URL) { self.bookURL = bookURL }

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        guard let url = urlSchemeTask.request.url else { urlSchemeTask.didFailWithError(URLError(.badURL)); return }
        let name = url.lastPathComponent
        var data: Data?
        var mime = "text/plain"
        if name == "book" {
            data = try? Data(contentsOf: bookURL, options: .mappedIfSafe)
            mime = "application/epub+zip"
        } else {
            let ext = (name as NSString).pathExtension
            let base = (name as NSString).deletingPathExtension
            if let file = Bundle.main.url(forResource: base, withExtension: ext), let d = try? Data(contentsOf: file) {
                data = d
                mime = ext == "html" ? "text/html" : (ext == "js" ? "application/javascript" : "application/octet-stream")
            }
        }
        let headers = ["Content-Type": mime, "Content-Length": String(data?.count ?? 0), "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"]
        let response = HTTPURLResponse(url: url, statusCode: data == nil ? 404 : 200, httpVersion: "HTTP/1.1", headerFields: headers)!
        urlSchemeTask.didReceive(response)
        if let data { urlSchemeTask.didReceive(data) }
        urlSchemeTask.didFinish()
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {}
}

/// WKUserContentController retains its handlers; this keeps the coordinator from being
/// retained by the web view it owns.
private final class WeakMessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(c, didReceive: message)
    }
}

struct ReaderWebView: UIViewControllerRepresentable {
    let file: ReaderFile
    let controller: ReaderController
    let prefs: () -> [String: Any]

    func makeUIViewController(context: Context) -> ReaderViewController {
        ReaderViewController(file: file, controller: controller, prefs: prefs)
    }

    func updateUIViewController(_ vc: ReaderViewController, context: Context) {}
}

/// One page of the curl: a picture of the reader at that moment.
private final class SnapshotViewController: UIViewController {
    enum Role { case previous, current, next }
    let image: UIImage
    let color: UIColor
    let role: Role
    init(image: UIImage, color: UIColor, role: Role) {
        self.image = image
        self.color = color
        self.role = role
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError() }
    override func loadView() {
        let iv = UIImageView(image: image)
        iv.contentMode = .scaleToFill
        iv.backgroundColor = color
        view = iv
    }
}

/// Resolves once, whichever of the two callers (the page moved, the clock ran out) comes first.
private final class OneShot {
    private var fired = false
    func fire(_ body: () -> Void) { if !fired { fired = true; body() } }
}

/// Hosts the book's web view and the page-turn layer.
///
/// Turning like Apple Books: while a book is open the reader keeps pictures of the previous,
/// current and next page (made by stepping the hidden web view), and shows them in a
/// UIPageViewController with the page-curl style. A drag then curls the page with the finger
/// at the system's own speed. When the curl finishes the web view is moved to the new page
/// behind it and the missing neighbour is made again. Until the pictures exist (just after
/// opening, a settings change or a jump) the live web view is showing and swipes turn it
/// directly. Taps are native: the edges turn the page, the middle shows or hides the bars.
@MainActor
final class ReaderViewController: UIViewController, WKScriptMessageHandler, UIGestureRecognizerDelegate,
                                  UIPageViewControllerDataSource, UIPageViewControllerDelegate {
    private enum Mode { case live, preparing, curl }
    private struct Pages { var previous: UIImage?; var current: UIImage?; var next: UIImage? }

    /// Space kept above and below the text, so lines never touch the screen edge.
    private let margin = UIEdgeInsets(top: 8, left: 0, bottom: 24, right: 0)

    private let file: ReaderFile
    private let controller: ReaderController
    private let prefs: () -> [String: Any]
    private let scheme: ReaderSchemeHandler
    private var webView: WKWebView!
    private var mode: Mode = .live
    private var pages = Pages()
    private var pageVC: UIPageViewController?
    private var cover: UIImageView?
    private var generation = 0
    private var muted = false          // the web view is being stepped for pictures: do not report its positions
    private var settling = false       // the web view is catching up after a turn
    private var stepWaiter: (() -> Void)?
    private var lastCfi: String?       // where the web view last said it was (every report, muted or not)
    private var swipes: [UISwipeGestureRecognizer] = []
    private var tapGesture: UITapGestureRecognizer?
    private var markup = false
    private var lastSize = CGSize.zero

    init(file: ReaderFile, controller: ReaderController, prefs: @escaping () -> [String: Any]) {
        self.file = file
        self.controller = controller
        self.prefs = prefs
        self.scheme = ReaderSchemeHandler(bookURL: file.url)
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError() }

    private var curlEnabled: Bool { controller.turnStyle == "curl" && !UIAccessibility.isReduceMotionEnabled }

    override func loadView() {
        view = UIView()
        view.backgroundColor = .clear
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        let cfg = WKWebViewConfiguration()
        cfg.setURLSchemeHandler(scheme, forURLScheme: "alxreader")
        cfg.userContentController.add(WeakMessageHandler(self), name: "alx")
        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.isOpaque = false
        wv.backgroundColor = .clear
        wv.scrollView.isScrollEnabled = false
        wv.scrollView.bounces = false
        wv.scrollView.contentInsetAdjustmentBehavior = .never
        #if DEBUG
        wv.isInspectable = true
        #endif
        view.addSubview(wv)
        webView = wv

        // Swipes turn the live page; once the pictures exist the page-curl layer takes over.
        for direction in [UISwipeGestureRecognizer.Direction.left, .right] {
            let g = UISwipeGestureRecognizer(target: self, action: #selector(swiped(_:)))
            g.direction = direction
            g.delegate = self
            g.cancelsTouchesInView = false
            view.addGestureRecognizer(g)
            swipes.append(g)
        }
        let tap = UITapGestureRecognizer(target: self, action: #selector(tapped(_:)))
        tap.delegate = self
        tap.cancelsTouchesInView = false
        view.addGestureRecognizer(tap)
        tapGesture = tap

        controller.host = self
        controller.webView = wv
        wv.load(URLRequest(url: URL(string: "alxreader://app/reader.html")!))
    }

    deinit {
        MainActor.assumeIsolated { webView?.configuration.userContentController.removeScriptMessageHandler(forName: "alx") }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let frame = view.bounds.inset(by: margin)
        guard frame.size != lastSize else { return }
        let resized = lastSize != .zero
        lastSize = frame.size
        webView.frame = frame
        pageVC?.view.frame = frame
        cover?.frame = frame
        if resized { invalidateSnapshots(rebuildAfter: 0.6) }
    }

    func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }

    @objc private func swiped(_ g: UISwipeGestureRecognizer) {
        guard mode == .live else { return }
        turn(forward: g.direction == .left)   // a swipe left moves forward, like turning a page
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        let x = g.location(in: view).x / max(1, view.bounds.width)
        if x < 0.25 { turn(forward: false) } else if x > 0.75 { turn(forward: true) } else { controller.toggleChrome() }
    }

    private func updateGestures() {
        controller.pagingMode = mode == .curl ? "curl" : (mode == .preparing ? "preparing" : "live")
        for g in swipes { g.isEnabled = (mode == .live && !markup) }
        tapGesture?.isEnabled = !markup
        setPagingEnabled(mode == .curl && !settling)
    }

    private func setPagingEnabled(_ on: Bool) {
        for g in pageVC?.gestureRecognizers ?? [] {
            // Taps are handled above (edges turn, middle toggles the bars); only drags belong to the curl.
            g.isEnabled = on && !(g is UITapGestureRecognizer)
        }
    }

    // MARK: turning

    func turn(forward: Bool) {
        switch mode {
        case .live:
            controller.run(forward ? "alx.next()" : "alx.prev()")
        case .preparing:
            break
        case .curl:
            guard !settling, let pvc = pageVC, let target = forward ? pages.next : pages.previous else { return }
            settling = true
            setPagingEnabled(false)
            let vc = SnapshotViewController(image: target, color: controller.pageColor, role: forward ? .next : .previous)
            pvc.setViewControllers([vc], direction: forward ? .forward : .reverse, animated: true) { [weak self] _ in
                self?.turned(forward: forward)
            }
        }
    }

    /// The curl finished (by a drag or a tap): shift the pictures, move the live page to match,
    /// and make the neighbour that is now missing.
    private func turned(forward: Bool) {
        controller.log("turned forward=\(forward) settling=\(settling)")
        settling = true
        setPagingEnabled(false)
        if forward {
            pages = Pages(previous: pages.current, current: pages.next, next: nil)
        } else {
            pages = Pages(previous: nil, current: pages.previous, next: pages.current)
        }
        resetChain()
        let gen = generation
        Task { [weak self] in
            guard let self else { return }
            _ = await self.step(forward ? "alx.next()" : "alx.prev()")   // the real move: updates and saves the position
            guard gen == self.generation else { return }
            self.muted = true
            let image = await self.neighbour(forward: forward)
            self.muted = false
            guard gen == self.generation else { return }
            if forward { self.pages.next = image } else { self.pages.previous = image }
            self.settling = false
            self.updateGestures()
        }
    }

    /// Show the current picture again as a fresh page so the previous/next lookups start from it.
    private func resetChain() {
        guard let pvc = pageVC, let image = pages.current else { return }
        pvc.setViewControllers([SnapshotViewController(image: image, color: controller.pageColor, role: .current)], direction: .forward, animated: false)
    }

    // MARK: the pictures

    /// Markup mode on: the live page, with selection, no curl and no swipe or tap turns. Off: back to normal.
    func markupChanged(_ on: Bool) {
        markup = on
        controller.run("alx.setMarkup(\(on))")
        if on {
            invalidateSnapshots()
        } else {
            controller.clearSelection()
            controller.activeHighlightID = nil
            invalidateSnapshots(rebuildAfter: 0.4)
        }
    }

    /// Throw the pictures away (a setting changed, the book jumped, the screen turned) so the live
    /// page shows, and optionally make new ones once it has settled.
    func invalidateSnapshots(rebuildAfter delay: TimeInterval? = nil) {
        generation += 1
        let gen = generation
        muted = false
        settling = false
        teardownOverlay()
        mode = .live
        updateGestures()
        if let delay {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                guard let self, gen == self.generation else { return }
                self.rebuildSnapshots()
            }
        }
    }

    func rebuildSnapshots() {
        generation += 1
        let gen = generation
        teardownOverlay()
        mode = .live
        muted = false
        settling = false
        guard curlEnabled, !markup, controller.ready, controller.error == nil else { updateGestures(); return }
        mode = .preparing
        updateGestures()
        Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .milliseconds(350))   // let the page paint
            guard gen == self.generation, let current = await self.snapshot(), gen == self.generation else { return }
            self.showCover(current)
            self.muted = true
            let next = await self.neighbour(forward: true)
            guard gen == self.generation else { return }
            let previous = await self.neighbour(forward: false)
            self.muted = false
            guard gen == self.generation else { return }
            self.pages = Pages(previous: previous, current: current, next: next)
            self.installPageVC()
            self.mode = .curl
            self.updateGestures()
        }
    }

    /// A picture of the page one step ahead or behind, then back to where the web view was.
    /// Nil at the first or last page.
    private func neighbour(forward: Bool) async -> UIImage? {
        guard await step(forward ? "alx.next()" : "alx.prev()") else { return nil }
        try? await Task.sleep(for: .milliseconds(130))
        let image = await snapshot()
        _ = await step(forward ? "alx.prev()" : "alx.next()")
        return image
    }

    private func snapshot() async -> UIImage? {
        await withCheckedContinuation { cont in
            webView.takeSnapshot(with: nil) { image, _ in cont.resume(returning: image) }
        }
    }

    /// Run a page command and wait for the page to report it moved. False if it did not (a book end).
    private func step(_ js: String, timeout: TimeInterval = 1.2) async -> Bool {
        controller.log("step \(js) muted=\(muted)")
        let before = lastCfi
        let reported = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            let once = OneShot()
            stepWaiter = { once.fire { cont.resume(returning: true) } }
            controller.run(js)
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { [weak self] in
                once.fire {
                    self?.stepWaiter = nil
                    cont.resume(returning: false)
                }
            }
        }
        // At the first or last page the reader still reports a position, but the same one: that is
        // not a move, and nothing may be done to "undo" it.
        let ok = reported && lastCfi != before
        controller.log("step \(js) -> \(ok ? "moved" : (reported ? "SAME PAGE" : "TIMEOUT"))")
        return ok
    }

    private func showCover(_ image: UIImage) {
        let iv = UIImageView(image: image)
        iv.frame = webView.frame
        iv.backgroundColor = controller.pageColor
        view.addSubview(iv)
        cover = iv
    }

    private func installPageVC() {
        guard let current = pages.current else { return }
        let pvc = UIPageViewController(transitionStyle: .pageCurl, navigationOrientation: .horizontal,
                                       options: [.spineLocation: NSNumber(value: UIPageViewController.SpineLocation.min.rawValue)])
        pvc.isDoubleSided = false
        pvc.dataSource = self
        pvc.delegate = self
        pvc.setViewControllers([SnapshotViewController(image: current, color: controller.pageColor, role: .current)], direction: .forward, animated: false)
        addChild(pvc)
        pvc.view.frame = webView.frame
        view.addSubview(pvc.view)
        pvc.didMove(toParent: self)
        pageVC = pvc
        cover?.removeFromSuperview()
        cover = nil
    }

    private func teardownOverlay() {
        cover?.removeFromSuperview()
        cover = nil
        if let pvc = pageVC {
            pvc.willMove(toParent: nil)
            pvc.view.removeFromSuperview()
            pvc.removeFromParent()
            pageVC = nil
        }
    }

    // MARK: page view controller

    func pageViewController(_ p: UIPageViewController, viewControllerAfter vc: UIViewController) -> UIViewController? {
        guard !settling, let image = pages.next else { return nil }
        return SnapshotViewController(image: image, color: controller.pageColor, role: .next)
    }

    func pageViewController(_ p: UIPageViewController, viewControllerBefore vc: UIViewController) -> UIViewController? {
        guard !settling, let image = pages.previous else { return nil }
        return SnapshotViewController(image: image, color: controller.pageColor, role: .previous)
    }

    func pageViewController(_ p: UIPageViewController, didFinishAnimating finished: Bool, previousViewControllers: [UIViewController], transitionCompleted completed: Bool) {
        guard completed, let shown = p.viewControllers?.first as? SnapshotViewController, shown.role != .current else { return }
        turned(forward: shown.role == .next)
    }

    // MARK: messages from reader.js

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        if ["selected", "selectionCleared", "highlightTapped", "bookmarkState", "log"].contains(type) {
            controller.log("msg \(type) \(String(describing: body).replacingOccurrences(of: "\n", with: " ").prefix(220))")
        }
        switch type {
        case "log": break
        case "loaded":
            var opts: [String: Any] = ["prefs": prefs()]
            if let s = file.start { opts["start"] = s }
            controller.run("alx.open(\(ReaderController.json(opts)))")
        case "ready":
            controller.ready = true
            controller.pushMarks()
            rebuildSnapshots()
        case "selected":
            guard controller.markupMode, let range = body["cfiRange"] as? String else { break }
            let text = (body["text"] as? String) ?? ""
            controller.activeHighlightID = nil
            controller.selection = TextSelection(cfiRange: range, text: text.isEmpty ? "Selected passage" : text, percent: body["percent"] as? Double)
        case "selectionCleared":
            controller.selection = nil
        case "highlightTapped":
            guard controller.markupMode else { break }
            controller.selection = nil
            controller.activeHighlightID = body["id"] as? String
        case "bookmarkState":
            controller.pageBookmarkIDs = (body["ids"] as? [String]) ?? []
        case "relocated":
            guard let cfi = body["cfi"] as? String else { return }
            controller.log("reloc \(cfi) pct=\(body["percent"] ?? "nil") muted=\(muted) moved=\(body["moved"] ?? "-") waiter=\(stepWaiter != nil)")
            lastCfi = cfi
            if !muted {
                controller.chapter = (body["chapter"] as? String) ?? controller.chapter
                controller.noteLocation(cfi, percent: body["percent"] as? Double)
            }
            // A report that only adds the percentage (once locations are ready) is not a page move.
            if (body["moved"] as? Bool) != false {
                let waiter = stepWaiter
                stepWaiter = nil
                waiter?()
            }
        case "toc":
            let items = (body["items"] as? [[String: Any]]) ?? []
            controller.toc = items.compactMap { i in
                guard let href = i["href"] as? String else { return nil }
                return .init(label: (i["label"] as? String) ?? href, href: href, depth: (i["depth"] as? Int) ?? 0)
            }
        case "error": controller.error = (body["message"] as? String) ?? "Could not open this book."
        default: break
        }
    }
}

// MARK: PDF

struct PDFReaderView: UIViewRepresentable {
    let file: ReaderFile
    let controller: ReaderController

    func makeCoordinator() -> Coordinator { Coordinator(controller: controller) }

    func makeUIView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        v.displayMode = .singlePage
        v.displayDirection = .horizontal
        v.usePageViewController(true)
        v.document = PDFDocument(url: file.url)
        controller.pdfView = v
        if let doc = v.document {
            if let n = file.start.flatMap({ Int($0) }), n >= 1, let page = doc.page(at: min(n, doc.pageCount) - 1) { v.go(to: page) }
            controller.ready = true
        } else {
            controller.error = "Could not open this PDF."
        }
        NotificationCenter.default.addObserver(context.coordinator, selector: #selector(Coordinator.pageChanged(_:)), name: .PDFViewPageChanged, object: v)
        return v
    }

    func updateUIView(_ uiView: PDFView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject {
        let controller: ReaderController
        init(controller: ReaderController) { self.controller = controller }
        @objc func pageChanged(_ note: Notification) {
            guard let v = note.object as? PDFView, let doc = v.document, let page = v.currentPage else { return }
            let n = doc.index(for: page) + 1
            controller.pdfPage = n
            controller.noteLocation(String(n), percent: doc.pageCount > 0 ? Double(n) / Double(doc.pageCount) : nil)
            controller.chapter = "Page \(n) of \(doc.pageCount)"
        }
    }
}

// MARK: screen

struct EbookReaderView: View {
    let file: ReaderFile
    @ObservedObject var bookmarks: BookmarksModel
    let onSave: (String, Double?) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var controller = ReaderController()
    @AppStorage("reader.theme") private var theme = "auto"
    @AppStorage("reader.size") private var size = 100
    @AppStorage("reader.font") private var font = "serif"
    @AppStorage("reader.turn") private var turn = "curl"
    @State private var showTOC = false
    @State private var showSettings = false
    @State private var contentsTab = "toc"
    @State private var noteTarget: Bookmark?

    private var isPDF: Bool { file.format == "pdf" }
    private var resolvedTheme: String { theme == "auto" ? (colorScheme == .dark ? "dark" : "light") : theme }
    private var background: Color {
        switch resolvedTheme {
        case "dark": return Color(red: 0.07, green: 0.07, blue: 0.07)
        case "sepia": return Color(red: 0.957, green: 0.925, blue: 0.847)
        default: return .white
        }
    }
    private var prefsObject: [String: Any] { ["theme": resolvedTheme, "size": size, "font": font] }
    private var showBars: Bool { controller.chromeVisible || controller.markupMode || isPDF || controller.error != nil }

    var body: some View {
        ZStack {
            background.ignoresSafeArea()
            if isPDF {
                PDFReaderView(file: file, controller: controller).ignoresSafeArea(edges: .bottom)
                    .frame(maxWidth: 900)   // keep pages a readable width on iPad; no effect on iPhone
            } else {
                ReaderWebView(file: file, controller: controller, prefs: { prefsObject })
                    .frame(maxWidth: 780)   // readable line length on iPad; no effect on iPhone
            }
            if !controller.ready && controller.error == nil { ProgressView().controlSize(.large) }
            Text(controller.pagingMode).font(.system(size: 1)).opacity(0.01).accessibilityIdentifier("reader.mode")
            #if DEBUG
            Text(controller.debugLog).font(.system(size: 1)).opacity(0.01).accessibilityIdentifier("reader.debug")
            #endif
            if let e = controller.error {
                VStack(spacing: 12) {
                    Text("Could not open this book").font(.headline)
                    Text(e).font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button("Close") { dismiss() }.buttonStyle(.borderedProminent)
                }.padding()
            }
            if showBars {
                VStack(spacing: 0) {
                    topBar
                    Spacer()
                    if controller.markupMode { markupPanel }
                    bottomBar
                }
            }
        }
        .preferredColorScheme(theme == "auto" ? nil : (resolvedTheme == "dark" ? .dark : .light))
        .onAppear {
            controller.onSave = onSave
            controller.turnStyle = turn
            controller.pageColor = UIColor(background)
            contentsTab = isPDF ? "marks" : "toc"
        }
        .task { await bookmarks.load() }
        .onChange(of: bookmarks.items) { _, _ in syncMarks() }
        .onChange(of: bookmarks.loaded) { _, _ in syncMarks() }
        .onChange(of: turn) { _, _ in
            controller.turnStyle = turn
            controller.host?.invalidateSnapshots(rebuildAfter: 0.2)
        }
        .onChange(of: resolvedTheme) { _, _ in controller.pageColor = UIColor(background) }
        .onDisappear { controller.flush() }
        .onChange(of: theme) { _, _ in controller.setPrefs(prefsObject) }
        .onChange(of: colorScheme) { _, _ in controller.setPrefs(prefsObject) }
        .onChange(of: size) { _, _ in controller.setPrefs(prefsObject) }
        .onChange(of: font) { _, _ in controller.setPrefs(prefsObject) }
        .sheet(isPresented: $showTOC) { contentsSheet }
        .sheet(isPresented: $showSettings) { settingsSheet.presentationDetents([.height(380)]) }
        .sheet(item: $noteTarget) { b in
            BookmarkEditor(bookmark: b) { change in Task { await bookmarks.update(b, change: change) } }
        }
    }

    private func syncMarks() {
        controller.setMarks(highlights: bookmarks.highlights, bookmarks: isPDF ? [] : bookmarks.bookmarks)
    }

    // MARK: bars

    private var pageBookmarked: Bool {
        isPDF ? bookmarks.bookmarks.contains { $0.location == String(controller.pdfPage) } : !controller.pageBookmarkIDs.isEmpty
    }

    private var topBar: some View {
        HStack(spacing: 16) {
            Button("Done") { controller.markupMode = false; controller.flush(); dismiss() }
            Spacer()
            Text(controller.chapter.isEmpty ? file.title : controller.chapter).font(.subheadline).lineLimit(1)
            Spacer()
            Button { Task { await toggleBookmark() } } label: { Image(systemName: pageBookmarked ? "bookmark.fill" : "bookmark") }
                .accessibilityLabel(pageBookmarked ? "Remove bookmark" : "Add bookmark")
                .accessibilityIdentifier("reader.bookmark")
            if !isPDF, bookmarks.highlightsSupported {
                Button {
                    controller.markupMode.toggle()
                    if controller.markupMode { controller.chromeVisible = true }
                } label: {
                    Image(systemName: "highlighter").foregroundStyle(controller.markupMode ? Color.accentColor : Color.primary)
                }
                .accessibilityLabel("Highlight text")
                .accessibilityIdentifier("reader.markup")
            }
            Button { showTOC = true } label: { Image(systemName: "list.bullet") }
                .accessibilityIdentifier("reader.contents")
            if !isPDF {
                Button { showSettings = true } label: { Image(systemName: "textformat.size") }
            }
        }
        .padding(.horizontal).padding(.vertical, 10)
        .background(.regularMaterial)
    }

    private var bottomBar: some View {
        HStack {
            if !isPDF { Button { controller.prev() } label: { Image(systemName: "chevron.left") } }
            Spacer()
            if let p = controller.percent {
                Text("\(Int((p * 100).rounded()))%").font(.footnote).monospacedDigit().accessibilityIdentifier("reader.percent")
            }
            Spacer()
            if !isPDF { Button { controller.next() } label: { Image(systemName: "chevron.right") } }
        }
        .padding(.horizontal).padding(.vertical, 10)
        .background(.regularMaterial)
    }

    // MARK: bookmarks

    private func toggleBookmark() async {
        if pageBookmarked {
            let ids = isPDF ? bookmarks.bookmarks.filter { $0.location == String(controller.pdfPage) }.map(\.id) : controller.pageBookmarkIDs
            for id in ids { if let b = bookmarks.items.first(where: { $0.id == id }) { await bookmarks.remove(b) } }
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            return
        }
        let new: NewBookmark
        if isPDF {
            new = NewBookmark(location: String(controller.pdfPage), percent: controller.percent, title: "Page \(controller.pdfPage)")
        } else {
            guard let cfi = controller.currentLocation else { return }
            new = NewBookmark(location: cfi, percent: controller.percent, title: controller.chapter.isEmpty ? nil : controller.chapter)
        }
        if await bookmarks.add(new) != nil { UINotificationFeedbackGenerator().notificationOccurred(.success) }
        else { UINotificationFeedbackGenerator().notificationOccurred(.error) }
    }

    // MARK: markup

    @ViewBuilder private var markupPanel: some View {
        if let sel = controller.selection {
            selectionPanel(sel)
        } else if let id = controller.activeHighlightID, let h = bookmarks.items.first(where: { $0.id == id }) {
            highlightPanel(h)
        } else {
            VStack(spacing: 4) {
                if let e = bookmarks.error { Text(e).font(.footnote).foregroundStyle(.red) }
                Text("Select text to highlight it. Tap a highlight to change it.").font(.footnote).foregroundStyle(.secondary)
            }
            .padding(10).frame(maxWidth: .infinity).background(.regularMaterial)
        }
    }

    private func selectionPanel(_ sel: TextSelection) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("\u{201C}\(sel.text)\u{201D}").font(.footnote).lineLimit(3)
            HStack(spacing: 14) {
                ForEach(HighlightColor.allCases) { c in
                    Button { Task { await highlight(sel, color: c, thenNote: false) } } label: {
                        Circle().fill(c.color).frame(width: 34, height: 34)
                    }
                    .accessibilityLabel("Highlight \(c.rawValue)")
                    .accessibilityIdentifier("highlight.\(c.rawValue)")
                }
                Spacer()
                Button("Note") { Task { await highlight(sel, color: .yellow, thenNote: true) } }.accessibilityIdentifier("highlight.note")
                Button("Cancel") { controller.clearSelection() }
            }
            if let e = bookmarks.error { Text(e).font(.footnote).foregroundStyle(.red) }
        }
        .padding().frame(maxWidth: .infinity, alignment: .leading).background(.regularMaterial)
    }

    private func highlightPanel(_ h: Bookmark) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("\u{201C}\(h.title ?? "")\u{201D}").font(.footnote).lineLimit(3)
            if let n = h.note, !n.isEmpty { Label(n, systemImage: "note.text").font(.footnote).foregroundStyle(.secondary).lineLimit(3) }
            HStack(spacing: 14) {
                ForEach(HighlightColor.allCases) { c in
                    Button { Task { await bookmarks.update(h, change: BookmarkChange(color: .set(c.rawValue))) } } label: {
                        Circle().fill(c.color).frame(width: 30, height: 30)
                            .overlay(Circle().stroke(Color.primary, lineWidth: HighlightColor.named(h.color) == c ? 3 : 0))
                    }
                    .accessibilityLabel("Colour \(c.rawValue)")
                }
                Spacer()
                Button(h.note?.isEmpty == false ? "Edit note" : "Add note") { noteTarget = h }.accessibilityIdentifier("highlight.editnote")
                Button("Delete", role: .destructive) {
                    controller.activeHighlightID = nil
                    Task { await bookmarks.remove(h) }
                }.accessibilityIdentifier("highlight.delete")
                Button("Done") { controller.activeHighlightID = nil }
            }
        }
        .padding().frame(maxWidth: .infinity, alignment: .leading).background(.regularMaterial)
    }

    private func highlight(_ sel: TextSelection, color: HighlightColor, thenNote: Bool) async {
        let made = await bookmarks.add(NewBookmark(kind: .highlight, location: sel.cfiRange, percent: sel.percent ?? controller.percent,
                                                   title: String(sel.text.prefix(200)), color: color.rawValue))
        controller.clearSelection()
        guard let made else { return }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        if thenNote { noteTarget = made }
    }

    // MARK: sheets

    private func open(_ b: Bookmark) {
        showTOC = false
        controller.chromeVisible = false
        if isPDF {
            if let n = Int(b.location ?? "") { controller.goToPDFPage(n) }
        } else if let loc = b.location {
            controller.goto(loc)
        }
    }

    private var contentsSheet: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("", selection: $contentsTab) {
                    if !isPDF { Text("Contents").tag("toc") }
                    Text("Bookmarks").tag("marks")
                    if !isPDF { Text("Highlights").tag("notes") }
                }
                .pickerStyle(.segmented)
                .padding()
                switch contentsTab {
                case "marks":
                    BookmarksListView(model: bookmarks, showHighlights: false, emptyText: "No bookmarks yet. Tap the ribbon at the top of a page.") { open($0) }
                case "notes":
                    BookmarksListView(model: bookmarks, showBookmarks: false, emptyText: "No highlights yet. Tap the highlighter, then select text.") { open($0) }
                default:
                    List(controller.toc) { entry in
                        Button {
                            controller.goto(entry.href)
                            showTOC = false
                            controller.chromeVisible = false
                        } label: {
                            Text(entry.label).padding(.leading, CGFloat(entry.depth) * 16)
                        }.tint(.primary)
                    }
                }
            }
            .navigationTitle(contentsTab == "toc" ? "Contents" : (contentsTab == "marks" ? "Bookmarks" : "Highlights"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showTOC = false } } }
        }
    }

    private var settingsSheet: some View {
        NavigationStack {
            Form {
                Picker("Theme", selection: $theme) {
                    Text("Auto").tag("auto"); Text("Light").tag("light"); Text("Sepia").tag("sepia"); Text("Dark").tag("dark")
                }.pickerStyle(.segmented)
                Picker("Font", selection: $font) { Text("Serif").tag("serif"); Text("Sans").tag("sans") }.pickerStyle(.segmented)
                Picker("Page turn", selection: $turn) { Text("Curl").tag("curl"); Text("None").tag("none") }.pickerStyle(.segmented)
                HStack {
                    Text("Text size")
                    Spacer()
                    Button { size = max(70, size - 10) } label: { Text("A−") }.buttonStyle(.bordered)
                    Text("\(size)%").monospacedDigit().frame(minWidth: 52)
                    Button { size = min(220, size + 10) } label: { Text("A+") }.buttonStyle(.bordered)
                }
            }
            .navigationTitle("Reading settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showSettings = false } } }
        }
    }
}

// MARK: getting the file

@MainActor
enum EbookFetcher {
    /// The book on disk: the offline download if there is one, else a cached copy, else
    /// fetched now with the server's auth headers and kept for next time.
    static func localFile(downloads: DownloadManager, serverID: UUID, detail: MediaDetail, headers: [String: String]) async throws -> URL {
        if let f = downloads.ebookFileURL(serverID: serverID, itemID: detail.item.id) { return f }
        guard let remote = detail.ebookDownloadURL else { throw APIError.notFound }
        var ext = (detail.ebookFormat ?? remote.pathExtension).lowercased()
        if ext != "pdf" { ext = "epub" }
        let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        let dir = caches.appendingPathComponent("ebooks", isDirectory: true).appendingPathComponent(serverID.uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent("\(DownloadPlanNaming.safe(detail.item.id)).\(ext)")
        if let size = (try? FileManager.default.attributesOfItem(atPath: dest.path)[.size] as? Int64), size > 0 { return dest }
        var req = URLRequest(url: remote, timeoutInterval: 180)
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        let (tmp, resp) = try await URLSession.shared.download(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw APIError.server(status: status, message: "The server refused the book download.") }
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.moveItem(at: tmp, to: dest)
        return dest
    }
}
