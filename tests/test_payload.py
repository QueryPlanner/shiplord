from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shiplord.payload import MAX_PAYLOAD_BYTES, PayloadError, discard_upload, receive_payload


def elf(machine=0x3E, body=b"x" * 5000) -> bytes:
    return b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8 + struct.pack("<HH", 3, machine) + body


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.staging = Path(self.temp.name) / "staging"

    def receive(self, data: bytes, size=None, digest=None, extra=b"", architecture="linux-amd64"):
        return receive_payload(io.BytesIO(data + extra), len(data) if size is None else size,
                               hashlib.sha256(data).hexdigest() if digest is None else digest, self.staging, architecture)

    def test_a_valid_upload_is_published_privately_under_its_checksum(self):
        data = elf()
        path = self.receive(data)
        self.assertEqual(path, self.staging / (hashlib.sha256(data).hexdigest() + ".bin"))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual((stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(self.staging.stat().st_mode)), (0o600, 0o700))
        self.assertEqual(os.listdir(self.staging), [path.name])

    def test_large_uploads_stream_in_chunks(self):
        data = elf(body=os.urandom(3 * (1 << 20) + 17))
        self.assertEqual(self.receive(data).read_bytes(), data)

    def test_every_violation_is_rejected_and_leaves_nothing_behind(self):
        data = elf()
        cases = {
            "truncated": dict(data=data, size=len(data) + 10, expected="ended before"),
            "too long": dict(data=data, extra=b"more", expected="longer than"),
            "checksum mismatch": dict(data=data, digest="0" * 64, expected="checksum"),
            "not an ELF": dict(data=b"#!/bin/sh\n" + b"x" * 200, expected="not a Linux ELF"),
            "arm64 binary": dict(data=elf(machine=0xB7), expected="another CPU architecture"),
            "zero size": dict(data=data, size=0, expected="size is invalid"),
            "oversize": dict(data=data, size=MAX_PAYLOAD_BYTES + 1, expected="size is invalid"),
            "bool size": dict(data=data, size=True, expected="size is invalid"),
            "bad digest text": dict(data=data, digest="XYZ", expected="checksum is invalid"),
        }
        for label, case in cases.items():
            expected = case.pop("expected")
            with self.subTest(label), self.assertRaisesRegex(PayloadError, expected):
                self.receive(**case)
            self.assertEqual([n for n in os.listdir(self.staging) if n.endswith(".bin")] if self.staging.exists() else [], [])
            self.assertEqual([n for n in os.listdir(self.staging) if n.startswith(".upload")] if self.staging.exists() else [], [])

    def test_a_full_disk_is_refused_before_anything_is_written(self):
        with patch("shiplord.payload.shutil.disk_usage", return_value=type("U", (), {"free": 10})()):
            with self.assertRaisesRegex(PayloadError, "free disk space"):
                self.receive(elf())
        self.assertEqual(os.listdir(self.staging), [])

    def test_a_symlinked_staging_directory_is_not_followed(self):
        target = Path(self.temp.name) / "elsewhere"
        target.mkdir()
        self.staging.symlink_to(target)
        with self.assertRaises(PayloadError):
            self.receive(elf())
        self.assertEqual(os.listdir(target), [])

    def test_uploading_the_same_binary_twice_is_idempotent(self):
        data = elf()
        self.assertEqual(self.receive(data), self.receive(data))
        self.assertEqual(len(os.listdir(self.staging)), 1)

    def test_uploads_nothing_consumed_are_purged_on_the_next_upload_but_recent_ones_are_kept(self):
        import time
        from shiplord.payload import STALE_UPLOAD_SECONDS
        self.staging.mkdir(mode=0o700)
        old, recent, partial = (self.staging / ("a" * 64 + ".bin"), self.staging / ("b" * 64 + ".bin"),
                                self.staging / ".upload-deadbeef")
        for path in (old, recent, partial):
            path.write_bytes(b"x")
        aged = time.time() - STALE_UPLOAD_SECONDS - 60
        for path in (old, partial):
            os.utime(path, (aged, aged))
        (self.staging / "keep.txt").write_text("not an upload")
        os.utime(self.staging / "keep.txt", (aged, aged))
        self.receive(elf())
        names = sorted(os.listdir(self.staging))
        self.assertNotIn(old.name, names)
        self.assertNotIn(partial.name, names)
        self.assertIn(recent.name, names)
        self.assertIn("keep.txt", names)  # only uploads are ever purged

    def test_discard_consumes_exactly_the_declared_bytes(self):
        stream = io.BytesIO(b"x" * 100 + b"after")
        discard_upload(stream, 100)
        self.assertEqual(stream.read(), b"after")
        discard_upload(io.BytesIO(b"short"), 100)  # a short stream does not raise


if __name__ == "__main__":
    unittest.main()
