import Foundation

public extension Chapter {
    /// What to show for a chapter. Many audiobooks name chapters by bare numbers ("018") or not at
    /// all; those read as "Chapter 18" and "Chapter <position>".
    func displayTitle(index: Int) -> String {
        let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return "Chapter \(index + 1)" }
        if t.allSatisfy(\.isNumber), let n = Int(t) { return "Chapter \(n)" }
        return t
    }
}
