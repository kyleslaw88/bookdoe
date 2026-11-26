import SwiftUI
import BookdoeKit

struct LibraryView: View {
    @EnvironmentObject private var model: AppModel
    @State private var query = ""
    @State private var kind: MediaKind?
    @State private var sort: LibrarySort = .title
    @State private var items: [MediaItem] = []
    @State private var nextOffset: Int?
    @State private var loading = false
    @State private var libraries: [LibraryRef] = []
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        Group {
            if sizeClass == .regular { grid } else { list }
        }
        .navigationTitle("Library")
        .navigationDestination(for: String.self) { ItemDetailView(itemID: $0) }
        .searchable(text: $query, prompt: "Title or author")
        .onSubmit(of: .search) { Task { await reload() } }
        .onChange(of: query) { _, new in if new.isEmpty { Task { await reload() } } }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Picker("Kind", selection: $kind) {
                        Text("All").tag(MediaKind?.none)
                        Text("Audiobooks").tag(MediaKind?.some(.audiobook))
                        Text("Ebooks").tag(MediaKind?.some(.ebook))
                    }
                    Picker("Sort", selection: $sort) {
                        Text("Title").tag(LibrarySort.title)
                        Text("Recently added").tag(LibrarySort.added)
                        Text("Recently updated").tag(LibrarySort.updated)
                    }
                    if libraries.count > 1, let server = model.activeServer {
                        Picker("Library", selection: Binding(get: { server.libraryID ?? "" }, set: { id in
                            var s = server; s.libraryID = id; model.updateServer(s); Task { await reload() }
                        })) {
                            ForEach(libraries) { Text($0.name).tag($0.id) }
                        }
                    }
                } label: { Image(systemName: "line.3.horizontal.decrease.circle") }
            }
        }
        .onChange(of: kind) { _, _ in Task { await reload() } }
        .onChange(of: sort) { _, _ in Task { await reload() } }
        .refreshable { await reload() }
        .task(id: model.activeServer?.id) {
            libraries = (try? await model.provider?.libraries()) ?? []
            await reload()
        }
    }

    private var list: some View {
        List {
            ForEach(items) { item in
                NavigationLink(value: item.id) { ItemRow(item: item) }
                    .onAppear { if item.id == items.last?.id { Task { await loadMore() } } }
            }
            status
        }
    }

    /// Regular width: a grid of covers whose column count follows the available width.
    private var grid: some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150, maximum: 220), spacing: 20, alignment: .top)], spacing: 24) {
                ForEach(items) { item in
                    NavigationLink(value: item.id) {
                        VStack(alignment: .leading, spacing: 6) {
                            CoverImage(url: item.coverURL, size: 130)
                            Text(item.title).font(.subheadline).lineLimit(2).multilineTextAlignment(.leading)
                            if !item.authors.isEmpty { Text(item.authorLine).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                        }
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                    }
                    .buttonStyle(.plain)
                    .onAppear { if item.id == items.last?.id { Task { await loadMore() } } }
                }
            }
            .padding()
            status.padding()
        }
    }

    @ViewBuilder private var status: some View {
        if loading { HStack { Spacer(); ProgressView(); Spacer() } }
        if !loading && items.isEmpty { Text(query.isEmpty ? "The library is empty." : "No matches.").foregroundStyle(.secondary) }
    }

    private func reload() async {
        guard let p = model.provider else { return }
        loading = true
        defer { loading = false }
        do {
            let page = try await p.browse(query: query, kind: kind, sort: sort, offset: 0, limit: 50)
            items = page.items
            nextOffset = page.nextOffset
        } catch { model.report(error) }
    }

    private func loadMore() async {
        guard let p = model.provider, let offset = nextOffset, !loading else { return }
        loading = true
        defer { loading = false }
        do {
            let page = try await p.browse(query: query, kind: kind, sort: sort, offset: offset, limit: 50)
            items += page.items.filter { new in !items.contains { $0.id == new.id } }
            nextOffset = page.nextOffset
        } catch { model.report(error) }
    }
}
