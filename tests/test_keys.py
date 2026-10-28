from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from shiplord.keys import MAX_KEYS, KeyError_, KeyStore, parse_public_key


def keygen(directory: Path, name: str, *args: str) -> str:
    path = directory / name
    subprocess.run(["ssh-keygen", "-q", "-N", "", "-f", str(path), *args], check=True, stdout=subprocess.DEVNULL)
    return (directory / (name + ".pub")).read_text().strip()


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class KeyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        cls.keys = {"ed25519": keygen(directory, "ed", "-t", "ed25519", "-C", "someone@laptop"),
                    "ecdsa": keygen(directory, "ec", "-t", "ecdsa", "-b", "256"),
                    "rsa3072": keygen(directory, "rsa", "-t", "rsa", "-b", "3072"),
                    "rsa2048": keygen(directory, "weak", "-t", "rsa", "-b", "2048"),
                    "other": keygen(directory, "other", "-t", "ed25519")}
        cls.directory = directory


class ParseTests(KeyTestCase):
    def test_supported_keys_parse_and_match_ssh_keygen_fingerprints(self):
        for name in ("ed25519", "ecdsa", "rsa3072"):
            with self.subTest(name):
                path = self.directory / f"{name}.pub"
                path.write_text(self.keys[name] + "\n")
                expected = subprocess.run(["ssh-keygen", "-l", "-f", str(path)], capture_output=True, text=True,
                                          check=True).stdout.split()[1]
                self.assertEqual(parse_public_key(self.keys[name]).fingerprint, expected)

    def test_comments_are_dropped_and_surrounding_whitespace_tolerated(self):
        key = parse_public_key("  " + self.keys["ed25519"] + "\n")
        self.assertNotIn("someone", key.blob + key.type)
        self.assertEqual(key, parse_public_key(self.keys["ed25519"].split()[0] + " " + self.keys["ed25519"].split()[1]))

    def test_malicious_or_malformed_input_is_rejected(self):
        self.assertTrue(self.keys["ecdsa"].split()[1].endswith("="), "fixture must need padding")
        good = self.keys["ed25519"].split()
        ed_blob = base64.b64decode(good[1])
        cases = {
            "options prefix": 'command="touch /x" ' + self.keys["ed25519"],
            "restrict option": "restrict " + self.keys["ed25519"],
            "embedded newline": self.keys["ed25519"] + "\n" + self.keys["other"],
            "carriage return": self.keys["ed25519"] + "\r",
            "type mismatch": "ecdsa-sha2-nistp256 " + good[1],
            "truncated blob": good[0] + " " + base64.b64encode(ed_blob[:-5]).decode(),
            "bad base64": good[0] + " !!!notbase64!!!",
            "unpadded base64": self.keys["ecdsa"].split()[0] + " " + self.keys["ecdsa"].split()[1].rstrip("="),
            "empty": "",
            "type only": good[0],
            "unsupported type": "ssh-dss " + good[1],
            "weak rsa": self.keys["rsa2048"],
            "oversize": good[0] + " " + good[1] + " " + "x" * 9000,
            "not a string": None,
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(KeyError_):
                parse_public_key(value)


class StoreTests(KeyTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.store = KeyStore(Path(self.root.name), "orders", "ship-orders")
        self.ed, self.other = parse_public_key(self.keys["ed25519"]), parse_public_key(self.keys["other"])

    def test_add_writes_only_forced_command_lines_and_is_idempotent(self):
        self.assertTrue(self.store.add("laptop", self.ed))
        self.assertFalse(self.store.add("laptop", self.ed))
        self.assertTrue(self.store.add("ci", self.other))
        text = self.store.path.read_text()
        self.assertEqual(text.splitlines()[0],
                         f'command="/usr/local/libexec/shiplord-dispatch orders",restrict,no-pty '
                         f"{self.ed.type} {self.ed.blob} shiplord:laptop")
        self.assertEqual(len(text.splitlines()), 2)
        self.assertNotIn("someone@laptop", text)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o644)
        self.assertEqual(self.store.list(), [("laptop", self.ed.fingerprint), ("ci", self.other.fingerprint)])

    def test_conflicting_labels_and_duplicate_keys_are_refused(self):
        self.store.add("laptop", self.ed)
        with self.assertRaisesRegex(KeyError_, "different key"):
            self.store.add("laptop", self.other)
        with self.assertRaisesRegex(KeyError_, "another label"):
            self.store.add("second", self.ed)
        self.assertEqual(len(self.store.list()), 1)

    def test_remove_is_idempotent_and_only_removes_the_label(self):
        self.store.add("laptop", self.ed)
        self.store.add("ci", self.other)
        self.assertTrue(self.store.remove("ci"))
        self.assertFalse(self.store.remove("ci"))
        self.assertEqual([label for label, _ in self.store.list()], ["laptop"])

    def test_invalid_labels_are_refused(self):
        for label in ("", "Upper", "a b", "a\nb", "../x", "-x", "x" * 33, None):
            with self.subTest(label=label), self.assertRaises(KeyError_):
                self.store.add(label, self.ed)

    def test_key_limit(self):
        for number in range(MAX_KEYS):
            self.store.add(f"key{number}", parse_public_key(keygen(Path(self.root.name), f"k{number}", "-t", "ed25519")))
        with self.assertRaisesRegex(KeyError_, "too many"):
            self.store.add("extra", self.ed)

    def test_unknown_or_foreign_lines_are_never_rewritten(self):
        self.store.add("laptop", self.ed)
        original = self.store.path.read_text()
        for line in ("ssh-ed25519 AAAA manual-entry",
                     f'command="/bin/sh",restrict,no-pty {self.other.type} {self.other.blob} shiplord:evil',
                     f'command="/usr/local/libexec/shiplord-dispatch billing",restrict,no-pty '
                     f"{self.other.type} {self.other.blob} shiplord:cross"):
            with self.subTest(line=line[:50]):
                self.store.path.write_text(original + line + "\n")
                with self.assertRaisesRegex(KeyError_, "did not write"):
                    self.store.add("another", self.other)
                with self.assertRaises(KeyError_):
                    self.store.list()
                self.assertEqual(self.store.path.read_text(), original + line + "\n")

    def test_unsafe_files_are_refused(self):
        self.store.add("laptop", self.ed)
        self.store.path.chmod(0o664)
        with self.assertRaisesRegex(KeyError_, "not safe"):
            self.store.list()
        self.store.path.chmod(0o644)
        target = Path(self.root.name) / "elsewhere"
        target.write_text("")
        self.store.path.unlink()
        self.store.path.symlink_to(target)
        with self.assertRaises(KeyError_):
            self.store.list()

    def test_delete_is_idempotent(self):
        self.store.add("laptop", self.ed)
        self.store.delete()
        self.store.delete()
        self.assertEqual(self.store.list(), [])


if __name__ == "__main__":
    unittest.main()
