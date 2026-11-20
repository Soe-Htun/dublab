// Face boxes (and subtitle text) with Apple's Vision framework (no extra packages).
// Usage: faces img1.jpg img2.jpg …  →  one line per image:
//   [T|]x0,y0,x1,y1[,c];x0,y0,x1,y1   (0-1, y from the TOP; "T|" when the lower part
//   of the picture has a line of text — a subtitle — on it; ",c" when that face's
//   eyes are closed / blinking), then "#score": how good a photo it is (-1…1, Vision's
//   aesthetics score; low for snapshot-like, badly framed or dull shots), when available
import Foundation
import Vision
import AppKit

// "--text": print only the text lines' boxes per image ("x0,y0,x1,y1;…", y from the top)
let textMode = CommandLine.arguments.dropFirst().first == "--text"
if textMode {
    for path in CommandLine.arguments.dropFirst(2) {
        guard let img = NSImage(contentsOfFile: path),
              let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print(""); continue }
        let text = VNRecognizeTextRequest()
        text.recognitionLevel = .accurate
        text.usesLanguageCorrection = false
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([text])
        let boxes = (text.results ?? []).map { o -> String in
            let b = o.boundingBox
            return String(format: "%.3f,%.3f,%.3f,%.3f", b.minX, 1 - b.maxY, b.maxX, 1 - b.minY)
        }
        print(boxes.joined(separator: ";"))
    }
    exit(0)
}

for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print(""); continue }
    let faces = VNDetectFaceLandmarksRequest()
    let text = VNRecognizeTextRequest()
    text.recognitionLevel = .fast
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([faces, text])
    // how open an eye is: its outline's height / width (open ≈ 0.3+, closed ≈ 0.1)
    func openness(_ r: VNFaceLandmarkRegion2D?) -> Double? {
        guard let p = r?.normalizedPoints, p.count >= 4 else { return nil }
        let xs = p.map { Double($0.x) }, ys = p.map { Double($0.y) }
        let w = (xs.max()! - xs.min()!)
        return w > 0 ? (ys.max()! - ys.min()!) / w : nil
    }
    let boxes = (faces.results ?? []).map { f -> String in
        let b = f.boundingBox   // origin bottom-left, normalised
        var s = String(format: "%.3f,%.3f,%.3f,%.3f", b.minX, 1 - b.maxY, b.maxX, 1 - b.minY)
        let eyes = [openness(f.landmarks?.leftEye), openness(f.landmarks?.rightEye)].compactMap { $0 }
        if !eyes.isEmpty && eyes.reduce(0, +) / Double(eyes.count) < 0.17 { s += ",c" }
        return s
    }
    // a subtitle: text in the lower 40%, reasonably wide
    let sub = (text.results ?? []).contains { o in
        o.boundingBox.maxY < 0.42 && o.boundingBox.width > 0.15
    }
    var score = ""
    if #available(macOS 15.0, *) {
        let aes = VNCalculateImageAestheticsScoresRequest()
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([aes])
        if let o = aes.results?.first as? VNImageAestheticsScoresObservation {
            score = String(format: "#%.3f", o.isUtility ? min(o.overallScore, -0.5) : o.overallScore)
        }
    }
    print((sub ? "T|" : "") + boxes.joined(separator: ";") + score)
}
