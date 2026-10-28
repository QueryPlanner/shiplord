from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest

from shiplord.dispatch import DispatchError, plan_dispatch
from shiplord.framing import read_frame, write_frame
from shiplord.layout import DISPATCH_PATH, HOST_PATH
from shiplord.protocol import PROTOCOL_VERSION


class PlanTests(unittest.TestCase):
    def plan(self, argv, original=None, user="ship-orders"):
        environ = {} if original is None else {"SSH_ORIGINAL_COMMAND": original}
        return plan_dispatch(["dispatch", *argv], environ, user)

    def test_admin_probe_without_an_app_is_capabilities_only(self):
        self.assertEqual(self.plan([]), ("capabilities", []))

    def test_bound_login_execs_the_fixed_sudo_helper_with_no_client_input(self):
        for original in (None, "", DISPATCH_PATH):
            with self.subTest(original=original):
                self.assertEqual(self.plan(["orders"], original),
                                 ("exec", ["/usr/bin/sudo", "-n", "--", HOST_PATH]))

    def test_client_commands_are_never_accepted(self):
        for original in ("id", DISPATCH_PATH + " extra", DISPATCH_PATH + "; id", "sh -c true", " " + DISPATCH_PATH,
                         "scp -t /tmp", "sftp"):
            with self.subTest(original=original), self.assertRaises(DispatchError) as caught:
                self.plan(["orders"], original)
            self.assertEqual(caught.exception.code, 2)

    def test_login_must_match_the_bound_application(self):
        for user in ("ship-billing", "appuser", "root", "ship-orders2"):
            with self.subTest(user=user), self.assertRaises(DispatchError) as caught:
                self.plan(["orders"], user=user)
            self.assertEqual(caught.exception.code, 3)

    def test_invalid_applications_and_argument_counts_are_rejected(self):
        for argv in (["../x"], ["Orders"], [""], ["a", "b"], ["-orders"]):
            with self.subTest(argv=argv), self.assertRaises(DispatchError) as caught:
                self.plan(argv)
            self.assertEqual(caught.exception.code, 2)


class ProcessTests(unittest.TestCase):
    def run_dispatch(self, args, stdin=b"", env_extra=None):
        env = dict(os.environ, **(env_extra or {}))
        return subprocess.run([sys.executable, "-m", "shiplord.dispatch", *args], input=stdin,
                              capture_output=True, env=env, timeout=30)

    def test_capabilities_probe_works_end_to_end(self):
        request = io.BytesIO()
        write_frame(request, {"version": PROTOCOL_VERSION, "request_id": "r1", "action": "capabilities",
                              "application_id": None, "commit": None, "arguments": {}})
        result = self.run_dispatch([], request.getvalue())
        self.assertEqual(result.returncode, 0, result.stderr)
        response = read_frame(io.BytesIO(result.stdout))
        self.assertEqual((response["status"], response["actions"]), ("ok", ["capabilities"]))

    def test_rejections_exit_nonzero_with_clean_stdout(self):
        result = self.run_dispatch(["orders"], env_extra={"SSH_ORIGINAL_COMMAND": "id"})
        self.assertEqual((result.returncode, result.stdout), (2, b""))
        self.assertIn(b"unsupported command", result.stderr)
        result = self.run_dispatch(["orders"])
        self.assertEqual((result.returncode, result.stdout), (3, b""))


if __name__ == "__main__":
    unittest.main()
