// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "GarminHealthBridge",
    platforms: [.iOS(.v17), .macOS(.v13)],
    products: [
        .library(name: "HealthBridgeCore", targets: ["HealthBridgeCore"]),
        .library(name: "HealthBridgeHealthKit", targets: ["HealthBridgeHealthKit"]),
    ],
    targets: [
        .target(name: "HealthBridgeCore"),
        .target(name: "HealthBridgeHealthKit", dependencies: ["HealthBridgeCore"]),
        .testTarget(
            name: "HealthBridgeCoreTests", dependencies: ["HealthBridgeCore"],
            resources: [.copy("Resources")]),
        .testTarget(name: "HealthBridgeHealthKitTests", dependencies: ["HealthBridgeHealthKit"]),
        .executableTarget(
            name: "BridgeFixtureDriver", dependencies: ["HealthBridgeCore"],
            path: "Tests/BridgeFixtureDriver"),
    ]
)
