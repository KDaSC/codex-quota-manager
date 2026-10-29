#!/usr/bin/env swift
import AppKit
import Foundation

// Native vector source for the app icon. Run with `swift scripts/generate_icon.swift`.
let assets = CommandLine.arguments.count > 1
    ? URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    : URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Assets", isDirectory: true)
let fileManager = FileManager.default
let iconset = assets.appendingPathComponent("AppIcon.iconset", isDirectory: true)
try fileManager.createDirectory(at: iconset, withIntermediateDirectories: true)

func color(_ hex: UInt32, alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: alpha)
}

// One broad, gently turning petal, in a 1024-point drawing space.
// The five rotations form a flower, with round tips and no pointed rays.
func petalPath() -> CGPath {
    let path = CGMutablePath()
    path.move(to: CGPoint(x: -10, y: 0))
    path.addCurve(to: CGPoint(x: -132, y: 182),
                  control1: CGPoint(x: -64, y: 38), control2: CGPoint(x: -111, y: 103))
    path.addCurve(to: CGPoint(x: -31, y: 350),
                  control1: CGPoint(x: -160, y: 278), control2: CGPoint(x: -115, y: 345))
    path.addCurve(to: CGPoint(x: 124, y: 239),
                  control1: CGPoint(x: 54, y: 356), control2: CGPoint(x: 133, y: 312))
    path.addCurve(to: CGPoint(x: 29, y: 40),
                  control1: CGPoint(x: 117, y: 160), control2: CGPoint(x: 49, y: 109))
    path.addCurve(to: CGPoint(x: -10, y: 0),
                  control1: CGPoint(x: 21, y: 14), control2: CGPoint(x: 8, y: 4))
    path.closeSubpath()
    return path
}

func renderIcon(size: Int) throws -> CGImage {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    guard let context = CGContext(data: nil, width: size, height: size,
                                  bitsPerComponent: 8, bytesPerRow: size * 4,
                                  space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw NSError(domain: "AppIcon", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "Cannot create icon drawing context"])
    }
    context.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)

    // Transparent padding preserves the macOS icon silhouette in the Dock.
    let plate = CGPath(roundedRect: CGRect(x: 61, y: 61, width: 902, height: 902),
                       cornerWidth: 203, cornerHeight: 203, transform: nil)
    context.saveGState()
    context.addPath(plate)
    context.clip()
    let background = CGGradient(colorsSpace: space,
                                colors: [color(0x0B635B), color(0x124D48)] as CFArray,
                                locations: [0, 1])!
    context.drawLinearGradient(background, start: CGPoint(x: 180, y: 890),
                               end: CGPoint(x: 860, y: 140), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    context.restoreGState()

    // A restrained edge keeps the dark tile legible on both Dock backgrounds.
    context.addPath(plate)
    context.setStrokeColor(color(0xF6D6AC, alpha: 0.10))
    context.setLineWidth(2)
    context.strokePath()

    context.saveGState()
    context.translateBy(x: 512, y: 510)
    for index in 0..<5 {
        context.saveGState()
        context.rotate(by: CGFloat(index) * 2 * .pi / 5 - 0.10)
        context.addPath(petalPath())
        context.clip()
        let petal = CGGradient(colorsSpace: space,
                               colors: [color(0xEFAD88), color(0xF6D6AC)] as CFArray,
                               locations: [0, 1])!
        context.drawLinearGradient(petal, start: CGPoint(x: 32, y: 18),
                                   end: CGPoint(x: -48, y: 340), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
        context.restoreGState()
    }
    context.setFillColor(color(0xFFF0D9))
    context.fillEllipse(in: CGRect(x: -25, y: -25, width: 50, height: 50))
    context.restoreGState()
    guard let image = context.makeImage() else {
        throw NSError(domain: "AppIcon", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "Cannot finish icon image"])
    }
    return image
}

func writePNG(_ image: CGImage, to url: URL) throws {
    let bitmap = NSBitmapImageRep(cgImage: image)
    guard let data = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "AppIcon", code: 3,
                      userInfo: [NSLocalizedDescriptionKey: "Cannot encode PNG"])
    }
    try data.write(to: url, options: .atomic)
}

let representations: [(String, Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024)
]
for (name, size) in representations {
    try writePNG(renderIcon(size: size), to: iconset.appendingPathComponent(name))
}
try writePNG(renderIcon(size: 1024), to: assets.appendingPathComponent("app-icon.png"))

let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconset.path, "-o", assets.appendingPathComponent("AppIcon.icns").path]
try iconutil.run()
iconutil.waitUntilExit()
guard iconutil.terminationStatus == 0 else {
    throw NSError(domain: "AppIcon", code: Int(iconutil.terminationStatus),
                  userInfo: [NSLocalizedDescriptionKey: "iconutil failed"])
}
print("Generated \(assets.appendingPathComponent("AppIcon.icns").path)")
