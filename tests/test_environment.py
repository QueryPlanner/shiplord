from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shiplord.environment import (MAX_ENVIRONMENT_BYTES, MAX_VALUE_BYTES, EnvironmentError_, load_environment_file,
                                  parse_environment, render_environment)


class ParseTests(unittest.TestCase):
    def test_valid_files_parse_literally(self):
        text = "# secrets\n\nDATABASE_URL=postgres://u:p@h/db?x=1&y=2\nEMPTY=\nQUOTED=\"a b\"\nSPACED = not-a-key-format-is-ok\n".replace(
            "SPACED = not-a-key-format-is-ok\n", "")
        self.assertEqual(parse_environment(text), {"DATABASE_URL": "postgres://u:p@h/db?x=1&y=2", "EMPTY": "",
                                                   "QUOTED": '"a b"'})

    def test_values_are_never_interpreted(self):
        value = "$(touch /x) `id` ${HOME} ; rm -rf / && echo $PATH"
        self.assertEqual(parse_environment(f"TOKEN={value}\n"), {"TOKEN": value})

    def test_invalid_input_is_rejected(self):
        cases = {
            "bare key passes through the host environment": "AWS_SECRET_ACCESS_KEY\n",
            "space before equals": "A =1\n",
            "bad key": "1A=x\n",
            "dash key": "A-B=x\n",
            "duplicate": "A=1\nA=2\n",
            "managed key": "ALLOW_ORIGINS=[]\n",
            "managed port": "PORT=1\n",
            "nul": "A=x\0y\n",
            "carriage return": "A=x\ry=1\n",
            "huge value": "A=" + "x" * (MAX_VALUE_BYTES + 1) + "\n",
            "huge file": "".join(f"K{n}=" + "x" * 100 + "\n" for n in range(MAX_ENVIRONMENT_BYTES // 100)),
            "shell export syntax": "export A=1\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(EnvironmentError_):
                parse_environment(text)

    def test_crlf_line_endings_are_accepted_and_python_only_separators_stay_inside_values(self):
        self.assertEqual(parse_environment("A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})
        self.assertEqual(parse_environment("A=x\u2028PORT=1\n"), {"A": "x\u2028PORT=1"})
        self.assertEqual(parse_environment("A=x\x0bB\n"), {"A": "x\x0bB"})

    def test_render_round_trips_and_is_sorted(self):
        values = {"B": "2", "A": "x=y"}
        self.assertEqual(render_environment(values), "A=x=y\nB=2\n")
        self.assertEqual(parse_environment(render_environment(values)), values)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app.env"

    def test_missing_file_is_none_and_private_file_loads(self):
        self.assertIsNone(load_environment_file(self.path))
        self.path.write_text("A=1\n")
        self.path.chmod(0o600)
        self.assertEqual(load_environment_file(self.path), {"A": "1"})

    def test_readable_symlinked_or_non_utf8_files_are_refused(self):
        self.path.write_text("A=1\n")
        self.path.chmod(0o644)
        with self.assertRaises(EnvironmentError_):
            load_environment_file(self.path)
        self.path.chmod(0o600)
        link = Path(self.temp.name) / "link.env"
        link.symlink_to(self.path)
        with self.assertRaises(EnvironmentError_):
            load_environment_file(link)
        self.path.write_bytes(b"A=\xff\xfe\n")
        with self.assertRaises(EnvironmentError_):
            load_environment_file(self.path)


if __name__ == "__main__":
    unittest.main()
