// Face boxes (and subtitle text) with Apple's Vision framework (no extra packages).
// Usage: faces img1.jpg img2.jpg …  →  one line per image:
//   [T|]x0,y0,x1,y1;x0,y0,x1,y1   (0-1, y from the TOP; "T|" when the lower part
//   of the picture has a line of text — a subtitle — on it)
import Foundation
import Vision
import AppKit

for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print(""); continue }
    let faces = VNDetectFaceRectanglesRequest()
    let text = VNRecognizeTextRequest()
    text.recognitionLevel = .fast
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([faces, text])
    let boxes = (faces.results ?? []).map { f -> String in
        let b = f.boundingBox   // origin bottom-left, normalised
        return String(format: "%.3f,%.3f,%.3f,%.3f", b.minX, 1 - b.maxY, b.maxX, 1 - b.minY)
    }
    // a subtitle: text in the lower 40%, reasonably wide
    let sub = (text.results ?? []).contains { o in
        o.boundingBox.maxY < 0.42 && o.boundingBox.width > 0.15
    }
    print((sub ? "T|" : "") + boxes.joined(separator: ";"))
}
