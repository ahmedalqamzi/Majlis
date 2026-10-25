// Independent QR decoder for test evidence: Apple CoreImage CIDetector (not the app's own encoder).
// Usage: qr_decode <png>... ; prints one JSON line per image: {"file":..., "messages":[...]}
import Foundation
import CoreImage

for path in CommandLine.arguments.dropFirst() {
    var messages: [String] = []
    if let img = CIImage(contentsOf: URL(fileURLWithPath: path)),
       let det = CIDetector(ofType: CIDetectorTypeQRCode, context: nil, options: [CIDetectorAccuracy: CIDetectorAccuracyHigh]) {
        for f in det.features(in: img) {
            if let q = f as? CIQRCodeFeature, let m = q.messageString { messages.append(m) }
        }
    }
    let obj: [String: Any] = ["file": path, "messages": messages]
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [])
    print(String(data: data, encoding: .utf8)!)
}
