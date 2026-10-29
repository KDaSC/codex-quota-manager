// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "IdeaInbox",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "idea-inbox", targets: ["IdeaInboxCLI"]),
        .executable(name: "idea-inbox-app", targets: ["IdeaInboxApp"]),
        .executable(name: "idea-inbox-check", targets: ["IdeaInboxCheck"])
    ],
    targets: [
        .target(name: "IdeaInboxCore"),
        .executableTarget(name: "IdeaInboxCLI", dependencies: ["IdeaInboxCore"]),
        .executableTarget(name: "IdeaInboxApp", dependencies: ["IdeaInboxCore"]),
        .executableTarget(name: "IdeaInboxCheck", dependencies: ["IdeaInboxCore"])
    ]
)
