import io
import struct
import unittest

from shiplord.framing import check_capabilities, read_frame, write_frame
from shiplord.protocol import MAX_MESSAGE_BYTES, ProtocolError


class FragmentedReader(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 2) if size >= 0 else size)


class ShortWriter(io.BytesIO):
    def write(self, data):
        return super().write(data[:2])


class BrokenStream:
    def read(self, size):
        raise OSError("sensitive details")

    def write(self, data):
        raise OSError("sensitive details")


class FramingTests(unittest.TestCase):
    def test_framing_uses_byte_length_and_handles_short_io(self):
        payload = {"text": "é"}
        expected = b'\x00\x00\x00\x0d{"text":"\xc3\xa9"}'
        writer = ShortWriter()
        write_frame(writer, payload)
        self.assertEqual(writer.getvalue(), expected)
        self.assertEqual(read_frame(FragmentedReader(expected)), payload)
        concatenated = io.BytesIO(expected + expected)
        self.assertEqual(read_frame(concatenated), payload)
        self.assertEqual(read_frame(concatenated), payload)

    def test_read_rejects_truncation_invalid_length_and_malformed_json(self):
        examples = [b"", b"\x00\x00", struct.pack(">I", 0),
                    struct.pack(">I", MAX_MESSAGE_BYTES + 1), struct.pack(">I", 4) + b"{}",
                    struct.pack(">I", 1) + b"\xff", struct.pack(">I", 2) + b"[]",
                    struct.pack(">I", 13) + b'{"x":1,"x":2}',
                    struct.pack(">I", 9) + b'{"x":NaN}',
                    struct.pack(">I", 4) + b'\xff\xfe{\x00']
        for data in examples:
            with self.subTest(data=data):
                with self.assertRaises(ProtocolError):
                    read_frame(io.BytesIO(data))
        with self.assertRaises(ProtocolError):
            read_frame(BrokenStream())

    def test_write_rejects_invalid_values_oversized_data_and_io_failures(self):
        for value in ([], {"value": float("nan")}, {"value": "x" * MAX_MESSAGE_BYTES}, {"value": "\ud800"}):
            with self.subTest(value_type=type(value)):
                with self.assertRaises(ProtocolError):
                    write_frame(io.BytesIO(), value)
        with self.assertRaises(ProtocolError) as caught:
            write_frame(BrokenStream(), {"ok": True})
        self.assertNotIn("sensitive details", str(caught.exception))
        class StalledWriter:
            def write(self, data):
                return 0
        with self.assertRaises(ProtocolError):
            write_frame(StalledWriter(), {"ok": True})

    def test_capabilities_require_matching_id_version_and_action(self):
        response = {"status": "ok", "request_id": "request-1", "version": 1,
                    "helper_version": "0.1.0", "actions": ["capabilities", "status"]}
        check_capabilities(response, "request-1", "status")
        invalid = [dict(response, request_id="different"), dict(response, version=True),
                   dict(response, version=2), dict(response, helper_version="bad\nversion"),
                   dict(response, actions=[]), dict(response, actions=["status", "status"]),
                   dict(response, actions=["bad action"]), dict(response, status="error"),
                   dict(response, extra="unexpected")]
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(ProtocolError):
                    check_capabilities(item, "request-1", "status")
        with self.assertRaisesRegex(ProtocolError, "does not support"):
            check_capabilities(response, "request-1", "deploy")
        with self.assertRaises(ProtocolError):
            check_capabilities(response, "--invalid", "status")

    def test_incompatible_response_is_distinct_from_malformed_response(self):
        response = {"status": "incompatible", "request_id": "request-1", "supported_versions": [1, 2]}
        with self.assertRaisesRegex(ProtocolError, "incompatible protocol version"):
            check_capabilities(response, "request-1", "status")
        for versions in ([], [True], [0], [1, 1], "1"):
            with self.subTest(versions=versions):
                with self.assertRaisesRegex(ProtocolError, "invalid incompatibility"):
                    check_capabilities(dict(response, supported_versions=versions), "request-1", "status")
