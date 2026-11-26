# Building the iOS app

Everything under `ios/` was written on Linux without Xcode. The SwiftPM package
compiles and its tests pass on Linux (Swift 5.10); the app target has never been
compiled. Expect the first `xcodebuild` on a Mac to surface a handful of SwiftUI or
AVFoundation compile errors to fix. The package is where the logic is, so those fixes
should be small.

## One-time Mac setup

1. Xcode 15.4 or later from the App Store (iOS 17 SDK). Open it once to accept the
   licence and install the iOS simulator.
2. XcodeGen: `brew install xcodegen`.
3. Sign in to Xcode with the Apple Developer account: Xcode, Settings, Accounts.
4. In App Store Connect create the app record: name "bookdoe" (or the final
   name), platform iOS, bundle id of your choice (reverse DNS, for example
   `com.yourname.bookdoe`). The same bundle id goes into
   `ios/project.yml` (`PRODUCT_BUNDLE_IDENTIFIER`) and the `BUNDLE_ID` secret for CI.

## Generate, open, run

```sh
cd ios
xcodegen generate            # writes Bookdoe.xcodeproj (git-ignored)
open Bookdoe.xcodeproj
```

In Xcode:

1. Select the `bookdoe` target, Signing & Capabilities: tick "Automatically manage
   signing" and pick your team. Xcode creates the development certificate and a
   provisioning profile. Alternatively put the 10-character team id into
   `DEVELOPMENT_TEAM` in `project.yml` and regenerate.
2. Run the package tests: Product, Test (the scheme runs `BookdoeKitTests`).
3. Pick a simulator (iPhone 15) and press Run. Add a server: the app needs a reachable
   bookdoe (`http://<lan-ip>:8795`), Jellyfin or Audiobookshelf. The simulator
   shares the Mac's network, so a LAN address works.
4. On a device: plug the iPhone in, trust the Mac, select it as the destination and
   Run. The first install asks you to trust the developer certificate on the phone
   (Settings, General, VPN & Device Management).

Command line equivalents:

```sh
cd ios/Packages/BookdoeKit && swift test
cd ios && xcodebuild -project Bookdoe.xcodeproj -scheme Bookdoe \
  -destination 'platform=iOS Simulator,name=iPhone 15' build
```

After a build, verify the built Info.plist carries a valid CarPlay and phone scene manifest
(`UISceneConfigurations` must be a dictionary keyed by role; CI runs this too):

```sh
sh ios/scripts/check-scene-manifest.sh path/to/bookdoe.app
```

Unit tests: `xcodebuild test -scheme Bookdoe -only-testing:BookdoeTests -destination 'platform=iOS Simulator,name=iPhone 18 Pro' CODE_SIGNING_ALLOWED=NO`.
See [CARPLAY_TESTING.md](CARPLAY_TESTING.md).

## Archive and TestFlight by hand

1. Product, Destination, "Any iOS Device (arm64)".
2. Product, Archive. When the Organizer opens, Distribute App, App Store Connect,
   Upload. Automatic signing creates the distribution certificate and profile.
3. In App Store Connect, TestFlight tab: the build appears after processing (10 to 30
   minutes). Add yourself as an internal tester; the TestFlight app on the phone
   installs it. External testers need a short beta review.
4. Bump `MARKETING_VERSION` / `CURRENT_PROJECT_VERSION` in `project.yml` (or in Xcode)
   for each upload; App Store Connect rejects a duplicate build number.

## TestFlight from GitHub Actions

`.github/workflows/ios.yml` runs `swift test` and a simulator build on every push
that touches `ios/`. The `testflight` job runs only on a `v*` tag or a manual
dispatch with "upload" ticked, and only when these repository secrets exist
(Settings, Secrets and variables, Actions). Nothing is stored in the repo:

| Secret | Value |
| --- | --- |
| `APPLE_TEAM_ID` | the 10-character team id (developer.apple.com, Membership) |
| `BUNDLE_ID` | the bundle id registered in App Store Connect |
| `ASC_KEY_ID`, `ASC_ISSUER_ID` | App Store Connect, Users and Access, Integrations, App Store Connect API: create a key with the "App Manager" role |
| `ASC_KEY_P8_BASE64` | `base64 -i AuthKey_XXXX.p8` of that key (downloadable once) |
| `DIST_CERT_P12_BASE64` | Apple Distribution certificate exported from Keychain Access as .p12, base64-encoded |
| `DIST_CERT_PASSWORD` | the password you gave the .p12 export |
| `PROVISIONING_PROFILE_BASE64` | an App Store provisioning profile for the bundle id (developer.apple.com, Profiles), base64-encoded |

The job imports the certificate into a temporary keychain, archives with manual
signing, exports with `method: app-store-connect` and uploads with the API key, then
deletes the key material. fastlane is not used; `xcodebuild -exportArchive` with
`-authenticationKeyPath` does the upload. If you prefer fastlane later, `match` with a
private certificates repo replaces the two certificate secrets.

## App Store submission checklist

- App icon: add a 1024x1024 PNG to `ios/App/Assets.xcassets/AppIcon.appiconset` and
  reference it in `Contents.json` (`"filename": "icon.png"`).
- ATS: the global `NSAllowsArbitraryLoads` exception is removed (F-1). Plain HTTP is allowed only to local-network hosts via `NSAllowsLocalNetworking`; public servers must be HTTPS. If a specific non-local HTTP host is ever required, scope it to that exact domain under `NSExceptionDomains` and document the App Review justification here.
- Privacy: the app sends no data anywhere except the servers the user adds. Privacy
  nutrition label: "Data not collected".
- Export compliance: `ITSAppUsesNonExemptEncryption` is false (HTTPS only).
- Screenshots for 6.7" and 6.5" phones (and iPad if you keep `TARGETED_DEVICE_FAMILY`
  at 1,2). On iPad the app shows a sidebar (regular width); run `StoreShotsUITests` on the
  iPad Pro 13-inch simulator (`-destination 'platform=iOS Simulator,name=iPad Pro 13-inch (M4)'`)
  for the 13" shots.
- Background audio is declared in `UIBackgroundModes`; nothing else runs in the
  background except URLSession downloads.
