import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from android_bridge import create_pair_code, encrypted_pair_response, validate_pair_code


class AndroidBridgeTest(unittest.TestCase):
    def test_pair_code_is_one_time_secret_with_expiry_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            code, server_id, fingerprint = create_pair_code(storage)
            self.assertEqual(len(code), 8)
            self.assertTrue(code.isdigit())
            self.assertEqual(len(server_id), 36)
            self.assertEqual(len(fingerprint), 64)
            paths = {"pair": storage / "state/android-bridge/pair-code.json"}
            self.assertTrue(validate_pair_code(paths, code))
            self.assertFalse(validate_pair_code(paths, "00000000"))

    def test_pair_response_is_encrypted_with_the_displayed_code(self):
        code = "12345678"
        wrapped = encrypted_pair_response(code, {"token": "secret"})
        key = hashlib.sha256(f"local-ledger-pair-v1:{code}".encode()).digest()
        clear = AESGCM(key).decrypt(base64.b64decode(wrapped["nonce"]), base64.b64decode(wrapped["ciphertext"]), None)
        self.assertEqual(json.loads(clear), {"token": "secret"})


if __name__ == "__main__":
    unittest.main()
