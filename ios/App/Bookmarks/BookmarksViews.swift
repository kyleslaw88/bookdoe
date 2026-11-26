import SwiftUI
import BookdoeKit

/// The colours a highlight can have, by the name the server stores.
enum HighlightColor: String, CaseIterable, Identifiable {
    case yellow, green, blue, pink
    var id: String { rawValue }
    var color: Color {
        switch self {
        case .yellow: return Color(red: 0.97, green: 0.82, blue: 0.29)
        case .green: return Color(red: 0.49, green: 0.85, blue: 0.34)
        case .blue: return Color(red: 0.42, green: 0.72, blue: 1.0)
        case .pink: return Color(red: 1.0, green: 0.56, blue: 0.69)
        }
    }
    /// The same colour for the page, which the reader's JavaScript paints.
    var hex: String {
        switch self {
        case .yellow: return "#f7d24a"
        case .green: return "#7ed957"
        case .blue: return "#6ab7ff"
        case .pink: return "#ff8fb1"
        }
    }
    static func named(_ name: String?) -> HighlightColor { name.flatMap(HighlightColor.init(rawValue:)) ?? .yellow }
}

func timeText(_ seconds: Double) -> String {
    Duration.seconds(max(0, seconds)).formatted(.time(pattern: seconds >= 3600 ? .hourMinuteSecond : .minuteSecond))
}

struct BookmarkRow: View {
    let bookmark: Bookmark

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if bookmark.kind == .highlight {
                RoundedRectangle(cornerRadius: 2).fill(HighlightColor.named(bookmark.color).color).frame(width: 4)
            } else {
                Image(systemName: "bookmark.fill").foregroundStyle(.tint)
            }
            VStack(alignment: .leading, spacing: 3) {
                if bookmark.kind == .highlight {
                    Text(bookmark.title ?? "Highlight").lineLimit(3)
                    if let n = bookmark.note, !n.isEmpty {
                        Label(n, systemImage: "note.text").font(.subheadline).foregroundStyle(.secondary).lineLimit(3)
                    }
                } else {
                    Text(bookmark.title?.isEmpty == false ? bookmark.title! : "Bookmark").lineLimit(2)
                    if let n = bookmark.note, !n.isEmpty { Text(n).font(.subheadline).foregroundStyle(.secondary).lineLimit(3) }
                }
                HStack(spacing: 8) {
                    if let p = bookmark.position { Text(timeText(p)).monospacedDigit() }
                    else if let p = bookmark.percent { Text("\(Int((p * 100).rounded()))%") }
                    Text(bookmark.createdAt.formatted(date: .abbreviated, time: .omitted))
                }.font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

/// Edit the words of a bookmark or the note and colour of a highlight.
struct BookmarkEditor: View {
    let bookmark: Bookmark
    let onSave: (BookmarkChange) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @State private var note: String
    @State private var color: HighlightColor

    init(bookmark: Bookmark, onSave: @escaping (BookmarkChange) -> Void) {
        self.bookmark = bookmark
        self.onSave = onSave
        _title = State(initialValue: bookmark.title ?? "")
        _note = State(initialValue: bookmark.note ?? "")
        _color = State(initialValue: HighlightColor.named(bookmark.color))
    }

    var body: some View {
        NavigationStack {
            Form {
                if bookmark.kind == .highlight {
                    Section("Passage") { Text(bookmark.title ?? "").foregroundStyle(.secondary) }
                    Section("Colour") { HighlightColorRow(selection: $color) }
                } else {
                    Section("Name") { TextField("Name", text: $title) }
                }
                if bookmark.kind == .highlight || bookmark.note != nil || bookmark.position == nil {
                    Section("Note") {
                        TextEditor(text: $note).frame(minHeight: 120).accessibilityIdentifier("note.editor")
                    }
                }
            }
            .navigationTitle(bookmark.kind == .highlight ? "Highlight" : "Bookmark")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        var change = BookmarkChange()
                        if bookmark.kind == .bookmark, title != (bookmark.title ?? "") { change.title = .set(title.isEmpty ? nil : title) }
                        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
                        if trimmed != (bookmark.note ?? "") { change.note = .set(trimmed.isEmpty ? nil : trimmed) }
                        if bookmark.kind == .highlight, color.rawValue != HighlightColor.named(bookmark.color).rawValue { change.color = .set(color.rawValue) }
                        onSave(change)
                        dismiss()
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

/// Four colour dots to pick from.
struct HighlightColorRow: View {
    @Binding var selection: HighlightColor

    var body: some View {
        HStack(spacing: 18) {
            ForEach(HighlightColor.allCases) { c in
                Circle()
                    .fill(c.color)
                    .frame(width: 34, height: 34)
                    .overlay(Circle().stroke(Color.primary, lineWidth: selection == c ? 3 : 0))
                    .contentShape(Circle())
                    .onTapGesture { selection = c }
                    .accessibilityElement()
                    .accessibilityLabel(c.rawValue.capitalized)
                    .accessibilityIdentifier("color.\(c.rawValue)")
                    .accessibilityAddTraits(selection == c ? [.isButton, .isSelected] : .isButton)
            }
        }
        .padding(.vertical, 4)
    }
}

/// A book's bookmarks and highlights; tap one to go there, swipe to delete or edit.
struct BookmarksListView: View {
    @ObservedObject var model: BookmarksModel
    var showBookmarks = true
    var showHighlights = true
    var emptyText = "Nothing saved in this book yet."
    let onOpen: (Bookmark) -> Void
    @State private var editing: Bookmark?

    private var shown: [Bookmark] { (showBookmarks ? model.bookmarks : []) + (showHighlights ? model.highlights : []) }

    var body: some View {
        List {
            if let e = model.error { Text(e).font(.footnote).foregroundStyle(.red) }
            if showBookmarks, !model.bookmarks.isEmpty { Section("Bookmarks") { ForEach(model.bookmarks) { row($0) } } }
            if showHighlights, !model.highlights.isEmpty { Section("Highlights & notes") { ForEach(model.highlights) { row($0) } } }
            if shown.isEmpty, model.loaded { Text(emptyText).foregroundStyle(.secondary) }
        }
        .overlay { if !model.loaded { ProgressView() } }
        .sheet(item: $editing) { b in
            BookmarkEditor(bookmark: b) { change in Task { await model.update(b, change: change) } }
        }
        .task { if !model.loaded { await model.load() } }
    }

    private func row(_ b: Bookmark) -> some View {
        Button { onOpen(b) } label: { BookmarkRow(bookmark: b) }
            .tint(.primary)
            .swipeActions(edge: .trailing) {
                Button(role: .destructive) { Task { await model.remove(b) } } label: { Label("Delete", systemImage: "trash") }
                Button { editing = b } label: { Label("Edit", systemImage: "pencil") }.tint(.blue)
            }
    }
}
