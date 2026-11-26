# bookdoe for iOS

Native SwiftUI client for bookdoe, Jellyfin and Audiobookshelf servers.

- `Packages/BookdoeKit`: all logic (providers, models, keychain, offline progress
  queue, download bookkeeping) as a SwiftPM package with unit tests. `swift test`
  runs on macOS and Linux (the Keychain store is compiled only where Security exists).
- `App`: the SwiftUI app target (player, downloads, views). Generated into an Xcode
  project with XcodeGen from `project.yml`; the `.xcodeproj` is not committed.

Build steps, signing and TestFlight: `docs/IOS_BUILD.md`. What the app does and which
server API assumptions are unverified: `docs/IOS_APP.md`.

## App Transport Security

The app enforces ATS for all non-local domains. `NSAllowsLocalNetworking` is enabled
to allow LAN HTTP servers (self-hosted media servers on the local network). Global
`NSAllowsArbitraryLoads` is intentionally disabled (F-1 remediation, 2026-11-01).

## bookdoe TestFlight build (bundle `com.bookdoe.app`)

The public build signs with `App/Bookdoe.entitlements`, which carries the CarPlay Audio entitlement (`com.apple.developer.carplay-audio`, enabled for com.bookdoe.app on 2026-11-15, #557):
`xcodebuild archive ... PRODUCT_BUNDLE_IDENTIFIER=com.bookdoe.app CODE_SIGN_ENTITLEMENTS=App/Bookdoe.entitlements`.
Export with the App Store profile "Bookdoe AppStore CarPlay"; the older "Bookdoe AppStore" profile has no CarPlay entitlement.
The user-visible name is bookdoe (`CFBundleDisplayName`); the Xcode scheme and target are `Bookdoe`, the product is `bookdoe`.

## Legacy compatibility (kept on purpose)

Builds before the rename shipped under the old product name. These identifiers keep it so an update installs over them:

- `DownloadManager.sessionIdentifier` (`com.example.alexandria.downloads`): the background URLSession id; changing it would orphan in-flight downloads.
- `KeychainCredentialStore.legacyService` (`com.example.alexandria.credentials`): credentials found there are copied to the current service.
- `ServerKind` decoding accepts the old raw value `alexandria` as `.bookdoe`; `LegacyFiles` moves the old `alexandria-*.json` files in Application Support to their `bookdoe-*` names.
