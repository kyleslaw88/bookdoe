import Foundation

/// Files under the app's Application Support directory that earlier builds wrote under the
/// pre-rename product name. The first run after the rename moves each one to its new name so
/// saved servers, the offline progress queue and local positions survive the update.
enum LegacyFiles {
    static func applicationSupport(named name: String, legacy: String) -> URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory
        let url = dir.appendingPathComponent(name)
        migrate(from: dir.appendingPathComponent(legacy), to: url)
        return url
    }

    static func migrate(from old: URL, to new: URL) {
        let fm = FileManager.default
        guard !fm.fileExists(atPath: new.path), fm.fileExists(atPath: old.path) else { return }
        try? fm.moveItem(at: old, to: new)
    }
}
