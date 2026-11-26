import BookdoeKit
import CarPlay
import UIKit

/// Builds the CarPlay templates. Kept apart from the scene delegate so tests can create
/// them without a head unit or a `CPInterfaceController`.
@MainActor
enum CarPlayTemplates {

    static let placeholderImage = UIImage(systemName: "book.closed")

    struct Root {
        let tabBar: CPTabBarTemplate
        let library: CPListTemplate
        let continueListening: CPListTemplate
        let podcasts: CPListTemplate
    }

    /// The tab bar with the Library, Continue Listening and Podcasts lists, each still empty.
    static func makeRoot() -> Root {
        let library = list(title: "Library", symbol: "books.vertical")
        let continueListening = list(title: "Continue Listening", symbol: "headphones")
        let podcasts = list(title: "Podcasts", symbol: "mic")
        let tabBar = CPTabBarTemplate(templates: [library, continueListening, podcasts])
        return Root(tabBar: tabBar, library: library, continueListening: continueListening, podcasts: podcasts)
    }

    private static func list(title: String, symbol: String) -> CPListTemplate {
        let list = CPListTemplate(title: title, sections: [])
        list.tabTitle = title
        list.tabImage = UIImage(systemName: symbol)
        return list
    }

    /// One row. Rows with a cover start with the placeholder image until the cover loads.
    static func item(_ row: CarPlayRowModel) -> CPListItem {
        CPListItem(text: row.text, detailText: row.detailText, image: row.coverURL == nil ? nil : placeholderImage)
    }

    /// The `CPListSection`s for the mapped sections, plus the rows whose cover still has to load.
    /// `configure` runs for each row after it is created (the delegate sets handlers there).
    static func sections(_ models: [CarPlaySectionModel],
                         configure: (CarPlayRowModel, CPListItem) -> Void = { _, _ in }) -> (sections: [CPListSection], covers: [(CPListItem, URL)]) {
        var covers: [(CPListItem, URL)] = []
        let sections = models.map { section in
            CPListSection(items: section.items.map { row in
                let item = item(row)
                configure(row, item)
                if let url = row.coverURL { covers.append((item, url)) }
                return item
            }, header: section.header, sectionIndexTitle: nil)
        }
        return (sections, covers)
    }
}
