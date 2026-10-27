import json
import unittest

from shiplord.protocol import MAX_MESSAGE_BYTES, ProtocolError, decode_request


class ProtocolTests(unittest.TestCase):
    def test_accepts_valid_provisional_envelope(self):
        request = {"version": 1, "request_id": "job-123", "action": "deploy", "application_id": "orders", "commit": "a" * 40, "arguments": {"image": "example"}}
        self.assertEqual(decode_request(json.dumps(request).encode()), request)

    def test_rejects_malformed_or_incompatible_envelopes(self):
        valid = {"version": 1, "request_id": "job-123", "action": "status", "application_id": "orders", "commit": None, "arguments": {}}
        invalid = [dict(valid, version=2), dict(valid, version=True), dict(valid, action="shell"),
                   dict(valid, request_id="--bad"), dict(valid, commit="abc"), dict(valid, arguments=[]),
                   dict(valid, extra="secretvalue")]
        payloads = [json.dumps(item).encode() for item in invalid]
        payloads += [b'[]', b'{"version":1,"version":1}', b'{"secretvalue":NaN}', b"x" * (MAX_MESSAGE_BYTES + 1), b"\xff"]
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ProtocolError) as caught:
                    decode_request(payload)
                self.assertNotIn("secretvalue", str(caught.exception))
