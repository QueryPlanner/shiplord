from __future__ import annotations

import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from shiplord.system import CommandError, HostSystem, run_command


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def script(self, body):
        path = self.directory / "tool"
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    def test_returns_code_and_combined_output(self):
        result = run_command([self.script('echo out; echo err >&2; exit 3\n')])
        self.assertEqual((result.code, result.output), (3, "out\nerr\n"))

    def test_arguments_are_never_interpreted_by_a_shell(self):
        marker = self.directory / "injected"
        tool = self.script('printf "%s" "$1"\n')
        result = run_command([tool, f"; touch {marker}; $(touch {marker})"])
        self.assertEqual(result.code, 0)
        self.assertFalse(marker.exists())
        self.assertIn("touch", result.output)

    def test_stdin_is_delivered_and_large_payloads_do_not_deadlock(self):
        payload = b"x" * 500_000
        result = run_command([self.script("wc -c\n")], input=payload)
        self.assertEqual(result.output.strip(), "500000")

    def test_output_is_capped_and_control_characters_are_replaced(self):
        result = run_command([self.script('printf "a\\033[0mb"; head -c 300000 /dev/zero | tr "\\0" y\n')])
        self.assertLessEqual(len(result.output), 4096)
        self.assertTrue(result.output.startswith("a?[0mb"))

    def test_environment_is_fixed_not_inherited(self):
        os.environ["SHIPLORD_LEAK_TEST"] = "secret"
        self.addCleanup(os.environ.pop, "SHIPLORD_LEAK_TEST", None)
        names = {line.split("=", 1)[0] for line in run_command([self.script("env\n")]).output.splitlines()}
        self.assertNotIn("SHIPLORD_LEAK_TEST", names)
        custom = run_command([self.script("env\n")], env={"ONLY": "1"}).output
        self.assertIn("ONLY=1", custom)

    def test_timeout_kills_the_whole_process_group(self):
        marker = self.directory / "survived"
        tool = self.script(f"(sleep 2; touch {marker}) &\nsleep 30\n")
        started = time.monotonic()
        with self.assertRaises(CommandError) as caught:
            run_command([tool], timeout=0.5)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 5)
        time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_missing_executable_and_invalid_arguments(self):
        for argv in ([str(self.directory / "absent")], [], ["tool\0x"]):
            with self.subTest(argv=argv), self.assertRaises(CommandError) as caught:
                run_command(argv)
            self.assertEqual(caught.exception.kind, "start")

    def test_host_system_runs_commands(self):
        self.assertEqual(HostSystem().run(["/bin/echo", "ok"]).output, "ok\n")


if __name__ == "__main__":
    unittest.main()
