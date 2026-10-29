from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from shiplord import tailscale
from shiplord.tailscale import TailscaleError, guidance, load_defaults, resolve, subjects, validate

CLIENT = "TJYVecfQJf11CNTRL-kEhAeizRMt11CNTRL"
AUDIENCE = f"api.tailscale.com/{CLIENT}"


class SubjectTests(unittest.TestCase):
    def test_immutable_and_classic_prefixes_produce_repository_and_owner_wide_subjects(self):
        self.assertEqual(subjects("repo:QueryPlanner@86554769/hello@1437016276", "main"),
                         {"repository": "repo:QueryPlanner@86554769/hello@1437016276:ref:refs/heads/main",
                          "all_repositories": "repo:QueryPlanner@86554769/*:ref:refs/heads/main"})
        self.assertEqual(subjects("repo:octo/app", "release/prod")["all_repositories"],
                         "repo:octo/*:ref:refs/heads/release/prod")

    def test_an_unexpected_prefix_is_refused_rather_than_guessed(self):
        for prefix in ("", "octo/app", "repo:octo", "repo:a/b/c", "repo:a/b:extra"):
            with self.subTest(prefix=prefix), self.assertRaises(TailscaleError):
                subjects(prefix, "main")

    def test_guidance_fills_in_every_value(self):
        text = " ".join(guidance("repo:octo/app", "main")["steps"])
        for expected in ("tag:ci", "autogroup:admin", tailscale.TRUST_CREDENTIALS_URL, tailscale.GITHUB_ISSUER,
                         "repo:octo/*:ref:refs/heads/main", "repo:octo/app:ref:refs/heads/main", "Auth Keys"):
            self.assertIn(expected, text)
        self.assertNotIn("warning", guidance("repo:octo/app", "main"))


class ValidationTests(unittest.TestCase):
    def test_values_from_the_console_are_accepted_and_trimmed(self):
        settings, warnings = validate(f"  {CLIENT} ", f"{AUDIENCE}\n")
        self.assertEqual(settings, {"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE})
        self.assertEqual(warnings, [])

    def test_an_audience_from_a_different_credential_is_warned_about(self):
        self.assertTrue(validate(CLIENT, "api.tailscale.com/other-credential")[1])

    def test_malformed_values_are_refused(self):
        for client, audience in (("", AUDIENCE), ("has space", AUDIENCE), ("x", AUDIENCE), (CLIENT, ""),
                                 (CLIENT, "two words"), (CLIENT, "a" * 513)):
            with self.subTest(client=client, audience=audience[:20]), self.assertRaises(TailscaleError):
                validate(client, audience)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_options_win_and_are_saved_privately_for_the_next_application(self):
        settings, origin = resolve({"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE}, home=self.home)
        self.assertEqual((settings["SHIPLORD_TS_CLIENT_ID"], origin["source"]), (CLIENT, "options"))
        path = self.home / "shiplord" / "tailscale.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {"client_id": CLIENT, "audience": AUDIENCE})
        again, origin = resolve({}, home=self.home, ask=lambda g: self.fail("must not ask"), guide=dict)
        self.assertEqual((again["SHIPLORD_TS_AUDIENCE"], origin["source"]), (AUDIENCE, "saved from an earlier setup"))

    def test_only_one_of_the_two_options_is_an_error(self):
        with self.assertRaisesRegex(TailscaleError, "both"):
            resolve({"SHIPLORD_TS_CLIENT_ID": CLIENT}, home=self.home)

    def test_the_user_is_shown_the_guide_then_asked_and_may_skip(self):
        shown = []
        settings, origin = resolve({}, home=self.home, guide=lambda: {"steps": ["x"]},
                                   ask=lambda g: shown.append(g) or None)
        self.assertEqual((settings, origin["source"], shown), ({}, "skipped", [{"steps": ["x"]}]))
        self.assertIsNone(load_defaults(self.home))
        settings, origin = resolve({"SHIPLORD_TS_TAGS": "tag:deploy"}, home=self.home, guide=dict,
                                   ask=lambda g: (CLIENT, AUDIENCE))
        self.assertEqual((settings["SHIPLORD_TS_TAGS"], origin["source"]), ("tag:deploy", "entered"))
        self.assertIsNotNone(load_defaults(self.home))

    def test_without_a_terminal_nothing_is_asked_and_nothing_is_configured(self):
        self.assertEqual(resolve({}, home=self.home), ({}, {"source": "not configured", "warnings": []}))

    def test_a_damaged_saved_file_is_ignored(self):
        (self.home / "shiplord").mkdir()
        (self.home / "shiplord" / "tailscale.json").write_text("{not json")
        self.assertIsNone(load_defaults(self.home))


if __name__ == "__main__":
    unittest.main()
