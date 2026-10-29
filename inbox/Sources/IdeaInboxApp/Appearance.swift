import SwiftUI
import AppKit

// The same small palette is shared by the dashboard and the original inbox.
enum ManagerTheme {
    static var accent: Color { adaptive(0x167367, 0x84D5C1) }
    static var canvas: Color { adaptive(0xF6F4EE, 0x171E1C) }
    static var surface: Color { adaptive(0xFFFEFA, 0x222B27) }
    static var sidebar: Color { adaptive(0xEEF1E9, 0x1D2522) }
    static var selected: Color { adaptive(0xDBEAE1, 0x304C42) }
    static var apricot: Color { adaptive(0xA65B31, 0xF1BC92) }
    static var draftSurface: Color { adaptive(0xF9EBDD, 0x45362B) }
    static var border: Color { adaptive(0xDCE2D8, 0x38463F) }

    private static func adaptive(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let value = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((value >> 16) & 255) / 255,
                           green: CGFloat((value >> 8) & 255) / 255,
                           blue: CGFloat(value & 255) / 255, alpha: 1)
        })
    }
}

struct ManagerAppIcon: View {
    var size: CGFloat = 44
    private static let icon: NSImage? = {
        let packaged = Bundle.main.url(forResource: "app-icon", withExtension: "png")
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Assets/app-icon.png")
        return NSImage(contentsOf: packaged ?? source)
    }()
    var body: some View {
        Group {
            if let image = Self.icon {
                Image(nsImage: image).resizable().interpolation(.high).scaledToFit()
            } else {
                Image(systemName: "camera.macro").font(.system(size: size * 0.5))
                    .foregroundStyle(.white).frame(width: size, height: size)
                    .background(ManagerTheme.accent, in: RoundedRectangle(cornerRadius: size * 0.22))
            }
        }.frame(width: size, height: size).accessibilityHidden(true)
    }
}
