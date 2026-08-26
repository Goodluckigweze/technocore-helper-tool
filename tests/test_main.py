import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import main


class HelperToolTests(unittest.TestCase):
    def test_clean_message_replaces_control_characters_and_trims(self):
        self.assertEqual(main.clean_message("  hello " + chr(10) + "world\t "), "hello world")

    def test_clean_message_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            main.clean_message(chr(10) + chr(9))

    def test_did_derivation_uses_ed25519_multicodec(self):
        key = Ed25519PrivateKey.generate()
        self.assertTrue(main.did_for(key).startswith("did:key:z6Mk"))

    def test_encrypted_identity_loads(self):
        key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.pem"
            path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"correct-passphrase")))
            loaded = main.load_identity(path, "correct-passphrase")
            self.assertEqual(main.did_for(loaded), main.did_for(key))

    def test_nonce_advances_past_recent_nonce_for_same_did(self):
        did = "did:key:z6Mktest"
        nonce = main.choose_nonce(did, [{"from": did, "nonce": "200"}])
        self.assertGreaterEqual(int(nonce), 201)


if __name__ == "__main__":
    unittest.main()
