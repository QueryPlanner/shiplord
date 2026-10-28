import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shiplord.framing import check_capabilities, read_frame
from shiplord.host import serve


def frame(message):
    data = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return len(data).to_bytes(4, "big") + data


def request(**updates):
    value = {"version": 1, "request_id": "job-1", "action": "capabilities",
             "application_id": "orders", "commit": None, "arguments": {}}
    return dict(value, **updates)


class HostTests(unittest.TestCase):
    def run_helper(self, data):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "shiplord.host"], input=data, cwd=directory,
                                    capture_output=True, timeout=5, check=False)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(result.stderr, b"")
        output = io.BytesIO(result.stdout)
        response = read_frame(output)
        self.assertEqual(output.read(), b"")
        return result.returncode, response

    def test_capabilities_reports_only_the_unprivileged_action(self):
        code, response = self.run_helper(frame(request(application_id=None)))
        self.assertEqual(code, 0)
        check_capabilities(response, "job-1", "capabilities")
        self.assertEqual(response["actions"], ["capabilities"])

    def test_version_mismatch_is_correlated_and_has_a_distinct_exit(self):
        code, response = self.run_helper(frame(request(version=2)))
        self.assertEqual(code, 4)
        self.assertEqual(response, {"status": "incompatible", "request_id": "job-1", "supported_versions": [1]})

    def test_mutations_and_unexpected_arguments_are_rejected(self):
        for message, expected in [(request(action="deploy"), "unsupported_action"),
                                  (request(arguments={"unsafe": "secretvalue"}), "invalid_request"),
                                  (request(commit="a" * 40), "invalid_request")]:
            with self.subTest(message=message):
                code, response = self.run_helper(frame(message))
                self.assertNotEqual(code, 0)
                self.assertEqual(response["error"], expected)
                self.assertNotIn("secretvalue", str(response))

    def test_malformed_and_trailing_input_are_rejected(self):
        for data in (b"\x00\x00", frame(request()) + b"extra", frame(request(version=True)), frame(request(action="shell"))):
            with self.subTest(data=data):
                code, response = self.run_helper(data)
                self.assertEqual(code, 2)
                self.assertEqual(response, {"status": "error", "error": "invalid_request"})

    def test_output_failure_returns_a_failure_status(self):
        class BrokenWriter:
            def write(self, data):
                raise OSError("failed")
        self.assertEqual(serve(io.BytesIO(frame(request())), BrokenWriter()), 1)
