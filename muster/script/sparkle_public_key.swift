// Prints the base64 Ed25519 public key that belongs to a Sparkle private key file, and nothing
// else.  make_appcast.sh compares it with the SUPublicEDKey built into the archive, because
// generate_appcast signs with whatever key it is given and a mismatch would only surface on
// installed copies, which would then refuse every update.
//
//   swift muster/script/sparkle_public_key.swift <private-key-file>
//
// The file holds Sparkle's export format:  base64 of the 32-byte Ed25519 seed, or the older
// 96-byte form whose last 32 bytes are the public key.  The private key is never printed.
import CryptoKit
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

let arguments = CommandLine.arguments
guard arguments.count == 2 else { fail("usage: sparkle_public_key.swift <private-key-file>") }
// The path and the system's reason are named, never the file's contents.
let keyPath = arguments[1]
let text: String
do {
    text = try String(contentsOfFile: keyPath, encoding: .utf8)
} catch {
    fail("cannot read the Sparkle key file at \(keyPath): \(error.localizedDescription)")
}
guard let data = Data(base64Encoded: text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
    fail("the key file is not base64")
}

let publicKey: Data
switch data.count {
case 32:
    guard let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: data) else {
        fail("the key file does not hold an Ed25519 seed")
    }
    publicKey = key.publicKey.rawRepresentation
case 96:
    publicKey = data.suffix(32)
default:
    fail("the key file holds \(data.count) bytes, not 32 or 96")
}
print(publicKey.base64EncodedString())
