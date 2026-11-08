// Иконка PT Backup: волна звука уходит стрелкой в серверный диск (в стиле make_diff_icon.swift).
// swiftc make_backup_icon.swift -o /tmp/mk && /tmp/mk out.png
import AppKit

let cs = CGColorSpaceCreateDeviceRGB()
let ctx = CGContext(data: nil, width: 1024, height: 1024, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
func col(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(colorSpace: cs, components: [r/255, g/255, b/255, a])! }
func grad(_ a: CGColor, _ b: CGColor) -> CGGradient { CGGradient(colorsSpace: cs, colors: [a, b] as CFArray, locations: [0, 1])! }

let body = CGRect(x: 100, y: 100, width: 824, height: 824)
let bodyPath = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)

ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 28, color: col(0, 0, 0, 0.45))
ctx.addPath(bodyPath); ctx.setFillColor(col(18, 26, 34)); ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(bodyPath); ctx.clip()
ctx.drawLinearGradient(grad(col(40, 56, 72), col(14, 20, 28)), start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])

// волна: зелёные столбики, симметричные относительно центральной линии
ctx.setLineCap(.round); ctx.setLineWidth(22)
var x: CGFloat = 214
var k = 0
while x <= 810 {
    let kd = Double(k)
    let env: Double = sin(Double(x - 214) / 596.0 * Double.pi)    // огибающая: тише по краям
    let w: Double = abs(sin(kd * 1.3)) * 0.6 + abs(sin(kd * 0.41)) * 0.4
    let h = CGFloat(18.0 + w * 120.0 * env)
    ctx.setStrokeColor(col(70, 200, 120))
    ctx.move(to: CGPoint(x: x, y: 720 - h)); ctx.addLine(to: CGPoint(x: x, y: 720 + h)); ctx.strokePath()
    x += 40; k += 1
}

// стрелка вниз — в диск
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -6), blur: 14, color: col(0, 0, 0, 0.5))
let arrow = CGMutablePath()
arrow.move(to: CGPoint(x: 472, y: 590)); arrow.addLine(to: CGPoint(x: 552, y: 590))
arrow.addLine(to: CGPoint(x: 552, y: 505)); arrow.addLine(to: CGPoint(x: 612, y: 505))
arrow.addLine(to: CGPoint(x: 512, y: 410)); arrow.addLine(to: CGPoint(x: 412, y: 505))
arrow.addLine(to: CGPoint(x: 472, y: 505)); arrow.closeSubpath()
ctx.addPath(arrow); ctx.setFillColor(col(245, 248, 250)); ctx.fillPath()
ctx.restoreGState()

// серверный диск: два блока с индикаторами
func slab(_ y: CGFloat, led: CGColor) {
    let r = CGRect(x: 230, y: y, width: 564, height: 112)
    let p = CGPath(roundedRect: r, cornerWidth: 30, cornerHeight: 30, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 18, color: col(0, 0, 0, 0.5))
    ctx.addPath(p); ctx.setFillColor(col(60, 110, 200)); ctx.fillPath()
    ctx.restoreGState()
    ctx.saveGState()
    ctx.addPath(p); ctx.clip()
    ctx.drawLinearGradient(grad(col(96, 150, 240), col(44, 86, 170)), start: CGPoint(x: 0, y: r.maxY), end: CGPoint(x: 0, y: r.minY), options: [])
    ctx.restoreGState()
    ctx.setStrokeColor(col(255, 255, 255, 0.35)); ctx.setLineWidth(3); ctx.addPath(p); ctx.strokePath()
    // прорези и индикатор
    ctx.setStrokeColor(col(255, 255, 255, 0.45)); ctx.setLineWidth(10); ctx.setLineCap(.round)
    for i in 0..<4 {
        let sx = CGFloat(290 + i * 42)
        ctx.move(to: CGPoint(x: sx, y: r.midY - 24)); ctx.addLine(to: CGPoint(x: sx, y: r.midY + 24)); ctx.strokePath()
    }
    ctx.setFillColor(led); ctx.fillEllipse(in: CGRect(x: 714, y: r.midY - 17, width: 34, height: 34))
}
slab(268, led: col(70, 220, 120))
slab(150, led: col(70, 220, 120, 0.55))
ctx.restoreGState()

let rep = NSBitmapImageRep(cgImage: ctx.makeImage()!)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
