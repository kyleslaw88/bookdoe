import Foundation

/// Turns the HTML some servers put in book descriptions into plain text: tags removed,
/// paragraph breaks kept, entities (`&#8212;`, `&amp;`, ...) decoded.
public enum HTMLText {
    public static func plain(_ html: String) -> String {
        var t = html
        t = t.replacingOccurrences(of: "<\\s*br\\s*/?>", with: "\n", options: [.regularExpression, .caseInsensitive])
        t = t.replacingOccurrences(of: "<\\s*/\\s*(p|div|li|h[1-6]|tr)\\s*>", with: "\n\n", options: [.regularExpression, .caseInsensitive])
        t = t.replacingOccurrences(of: "<[^>]*>", with: "", options: .regularExpression)
        t = decodeEntities(t)
        t = t.replacingOccurrences(of: "[ \\t\u{00A0}]+", with: " ", options: .regularExpression)
        t = t.replacingOccurrences(of: " ?\\n ?", with: "\n", options: .regularExpression)
        t = t.replacingOccurrences(of: "\\n{3,}", with: "\n\n", options: .regularExpression)
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static let named: [String: String] = [
        "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "nbsp": "\u{00A0}",
        "mdash": "\u{2014}", "ndash": "\u{2013}", "hellip": "\u{2026}", "lsquo": "\u{2018}", "rsquo": "\u{2019}",
        "ldquo": "\u{201C}", "rdquo": "\u{201D}", "copy": "\u{00A9}", "eacute": "\u{00E9}", "egrave": "\u{00E8}",
        "agrave": "\u{00E0}", "aacute": "\u{00E1}", "ouml": "\u{00F6}", "uuml": "\u{00FC}", "auml": "\u{00E4}",
        "ntilde": "\u{00F1}", "ccedil": "\u{00E7}", "bull": "\u{2022}", "middot": "\u{00B7}", "trade": "\u{2122}",
    ]

    static func decodeEntities(_ s: String) -> String {
        guard s.contains("&"), let re = try? NSRegularExpression(pattern: "&(#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);") else { return s }
        let ns = s as NSString
        var out = ""
        var last = 0
        for m in re.matches(in: s, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let body = ns.substring(with: m.range(at: 1))
            var replacement: String?
            if body.hasPrefix("#") {
                let digits = body.dropFirst()
                let value = digits.first == "x" || digits.first == "X" ? UInt32(digits.dropFirst(), radix: 16) : UInt32(digits, radix: 10)
                replacement = value.flatMap(Unicode.Scalar.init).map { String(Character($0)) }
            } else {
                replacement = named[body] ?? named[body.lowercased()]
            }
            out += replacement ?? ns.substring(with: m.range)
            last = m.range.location + m.range.length
        }
        out += ns.substring(from: last)
        return out
    }
}
