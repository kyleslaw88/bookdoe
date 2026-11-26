// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "BookdoeKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "BookdoeKit", targets: ["BookdoeKit"]),
    ],
    targets: [
        .target(
            name: "BookdoeKit",
            path: "Sources/BookdoeKit"
        ),
        .testTarget(
            name: "BookdoeKitTests",
            dependencies: ["BookdoeKit"],
            path: "Tests/BookdoeKitTests"
        ),
    ]
)
