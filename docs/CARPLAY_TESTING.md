# Testing CarPlay

CarPlay failures are invisible on the phone, so check each layer separately.

## 1. Automated checks (no head unit)

- `ios/scripts/check-scene-manifest.sh <Bookdoe.app | Info.plist>` (macOS) fails unless
  `UIApplicationSceneManifest:UISceneConfigurations` is a **Dict** keyed by scene role, with a
  `CPTemplateApplicationSceneSessionRoleApplication` entry (`UISceneClassName`
  `CPTemplateApplicationScene`, a `UISceneDelegateClassName`) and a
  `UIWindowSceneSessionRoleApplication` entry with a delegate. It also checks the delegate class
  names exist as strings in the app binary (or its `.debug.dylib`). CI runs it after the simulator build.
- `ios/scripts/test-check-scene-manifest.sh` self-tests the checker against good and broken plists.
- `BookdoeTests` (`ios/Tests`) builds the `CarPlayTemplates` tab bar and rows (with and without covers, the loading/empty/offline/error rows, the `listLimit` cap) and checks `updateSections` accepts them:
  `xcodebuild test -project Bookdoe.xcodeproj -scheme Bookdoe -destination 'platform=iOS Simulator,name=iPhone 18 Pro' -only-testing:BookdoeTests CODE_SIGNING_ALLOWED=NO`

Not covered by these: the real CarPlay scene connecting, `populate` against a live server, playback from a row.

## 2. Xcode CarPlay simulator

1. Run the app on an iPhone simulator.
2. Simulator menu: **I/O > External Displays > CarPlay**. A CarPlay window opens.
3. Open Bookdoe in it. You should see the Library, Continue Listening and Podcasts tabs; a blank screen or
   a bounce back to the home grid means the scene did not connect.

Do not drive this window by script (osascript hangs); check it by hand.

## 3. Reading logs

```sh
xcrun simctl spawn booted log stream --level debug --predicate 'process == "bookdoe" OR subsystem == "com.apple.CarPlay"'
xcrun simctl spawn booted log show --last 5m --predicate 'process == "bookdoe"'
```

Look for `Application does not implement CarPlay template application lifecycle methods in its scene delegate.`
That message means iOS found no `CPTemplateApplicationSceneDelegate` for the CarPlay role: the built
`UISceneConfigurations` is wrong (builds 16-18 shipped it as an Array) or the delegate class name does not resolve.

## 4. TestFlight crash feedback and the .ips

- App Store Connect: TestFlight > Crashes shows crash reports from testers; Xcode > Window > Organizer > Crashes
  has the same with symbolication.
- On a device: Settings > Privacy & Security > Analytics & Improvements > Analytics Data, `bookdoe-*.ips`.
- TestFlight crash feedback usually has no exception message: crash reports from testers are symbolicated
  stack traces, and the message (`NSInternalInconsistencyException` reason text and similar) is kept in the
  on-device `.ips` application-specific information, which TestFlight feedback does not forward. Get the `.ips`
  from the tester's device or from a simulator run.
- The exception text lives in the `.ips` JSON under `"exception"` / `"asi"` (application specific information)
  and in the `lastExceptionBacktrace` frames. For the scene crash, search for `CarPlay template application lifecycle`.
- Confirm what shipped: unzip the build's `Info.plist` or run `check-scene-manifest.sh` on the archive's `.app`.

## 5. Gotcha: persisted scene sessions

iOS persists scene sessions. Builds 15-17 used the SwiftUI lifecycle and created sessions that are
reused when you upgrade to a UIKit-lifecycle build (build 18 onward), which can leave the phone or CarPlay
**blank** even though the new build is correct. A fresh install avoids it: delete the app (on the phone, and
in the simulator `xcrun simctl uninstall booted <bundle id>`), then install the new build. Test upgrade
paths separately from fresh installs.
