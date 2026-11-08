// sign.swift — подпись релизов (Ed25519, CryptoKit).
//   sign genkey <файл-ключа>          создаёт закрытый ключ, печатает открытый (base64)
//   sign pub <файл-ключа>             печатает открытый ключ
//   sign sign <файл-ключа> <строка>   печатает подпись строки (base64)
import CryptoKit
import Foundation

let a = CommandLine.arguments
func fail(_ m: String) -> Never { FileHandle.standardError.write((m + "\n").data(using: .utf8)!); exit(1) }
guard a.count >= 3 else { fail("usage: sign genkey|pub|sign <keyfile> [message]") }
let path = a[2]
switch a[1] {
case "genkey":
    let k = Curve25519.Signing.PrivateKey()
    try k.rawRepresentation.base64EncodedString().write(toFile: path, atomically: true, encoding: .utf8)
    chmod(path, 0o600)
    print(k.publicKey.rawRepresentation.base64EncodedString())
case "pub", "sign":
    guard let s = try? String(contentsOfFile: path, encoding: .utf8), let raw = Data(base64Encoded: s.trimmingCharacters(in: .whitespacesAndNewlines)),
          let k = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) else { fail("cannot read key") }
    if a[1] == "pub" { print(k.publicKey.rawRepresentation.base64EncodedString()) }
    else {
        guard a.count >= 4 else { fail("message missing") }
        print(try k.signature(for: Data(a[3].utf8)).base64EncodedString())
    }
default: fail("unknown command")
}
