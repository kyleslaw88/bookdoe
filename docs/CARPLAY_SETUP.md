# CarPlay Setup for bookdoe iOS

## Status

Apple has approved the CarPlay Audio entitlement (`com.apple.developer.carplay-audio`).

- The capability is enabled on the App ID used for the app.
- The entitlement is enabled in `ios/project.yml` (entitlements properties) and in
  `ios/App/Bookdoe.entitlements`.
- Use the provisioning profile **"bookdoe AppStore CarPlay"** (type IOS_APP_STORE). The older
  profile "bookdoe AppStore" is invalid and does not carry the entitlement.

## What is in the code

- **`AppDelegate.swift`** returns a `CPTemplateApplicationScene` configuration when the session role is `.carTemplateApplication` (Info.plist `UISceneRole` must be `CPTemplateApplicationSceneSessionRoleApplication`). Phone and iPad scenes use the SwiftUI `WindowGroup` as before.
- **`CarPlaySceneDelegate.swift`** implements `CPTemplateApplicationSceneDelegate` and presents the CarPlay templates.
- **`Bookdoe.entitlements`** and **`project.yml`** carry `com.apple.developer.carplay-audio` set to true.
- `project.yml` sets `UIApplicationSupportsMultipleScenes: true` and registers the CarPlay scene configuration.

## CI profile check

`.github/workflows/ios.yml` signs manually with the `PROVISIONING_PROFILE_BASE64` secret. Before the
archive step it decodes the installed profile with `security cms -D -i` and runs
`ios/scripts/check-carplay-profile.sh`. The build fails early with a clear message if the profile
does not contain `com.apple.developer.carplay-audio` set to true, so a stale profile cannot
produce a confusing signing error.

## Replacing the profile secret

1. In the Apple Developer portal download the **"bookdoe AppStore CarPlay"** `.mobileprovision`.
2. Base64-encode it without line breaks: `base64 -i Bookdoe_AppStore_CarPlay.mobileprovision | tr -d '\n'`.
3. In the repository settings, replace the `PROVISIONING_PROFILE_BASE64` secret with that value.
4. Keep the `BUNDLE_ID` secret as is; the bundle identifier is never written into the repository.
5. Trigger the iOS workflow. The profile check must print `OK: provisioning profile carries com.apple.developer.carplay-audio`.

## Simulator and unit-test builds

Builds with `CODE_SIGNING_ALLOWED=NO` keep working with the entitlement present, because no
provisioning profile is evaluated. The Xcode CarPlay simulator can be used for the scene delegate
and templates. See [CARPLAY_TESTING.md](CARPLAY_TESTING.md) for the checks, simulator steps,
crash-log reading and the persisted scene-session gotcha.

## Notes

- No signing secrets are stored in the repository.
- The phone and iPad UI is unaffected.
