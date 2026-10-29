from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shiplord.layout import Layout
from shiplord.runtime import DockerRuntime, RuntimeFailure, release_id
from shiplord.system import CommandError, CommandResult

ID_A, ID_B = "a" * 64, "b" * 64


def release(commit="a", image="b", sequence=1000, port=8080, origins=(), memory=512, storage=None):
    value = {"commit": commit * 40, "sequence": sequence,
            "artifact": {"kind": "docker", "image": "ghcr.io/example/orders@sha256:" + image * 64},
            "configuration": {"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk",
                              "runtime": "docker", "http": {"api_domain": "orders.example.com",
                              "frontend_origins": list(origins), "container_port": port, "readiness_path": "/ready"},
                              "resources": {"memory_mb": memory},
                              "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}}
    if storage is not None:
        value["configuration"]["storage"] = storage
    return value


LEGACY_VOLUME = CommandResult(0, "local|null|null\n")
EXITED_LEGACY = CommandResult(0, "exited unless-stopped |<no value>|<no value>\n")  # a stopped container from before the policy
REGISTRATION = {"application_id": "orders", "host_port": 20007, "api_domain": "orders.example.com"}


class FakeDocker:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.results: dict[str, CommandResult] = {}
        self.raise_for: dict[str, CommandError] = {}

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == "/usr/bin/docker"
        key = " ".join(argv[1:3]) if argv[1] in ("image", "volume") else argv[1]
        if key in self.raise_for:
            raise self.raise_for[key]
        return self.results.get(key, CommandResult(0, ""))

    def sub(self, name):
        return [c for c in self.calls if (" ".join(c[1:3]) if c[1] in ("image", "volume") else c[1]) == name]


class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        (self.layout.environments / "orders").mkdir(parents=True)
        self.docker = FakeDocker()
        self.docker.results["volume inspect"] = LEGACY_VOLUME  # an existing volume from before labels existed
        self.docker.results["inspect"] = EXITED_LEGACY
        self.runtime = DockerRuntime(self.docker, self.layout, platform_name="linux/amd64")


class IdentityTests(unittest.TestCase):
    def test_release_ids_are_stable_short_and_sensitive_to_every_input(self):
        base = release_id(release())
        self.assertEqual(base, release_id(release()))
        self.assertRegex(base, r"^[0-9a-f]{12}$")
        for changed in (release(commit="c"), release(image="d"), release(sequence=1001)):
            self.assertNotEqual(base, release_id(changed))
        self.assertEqual(base, release_id(release(port=9000)))  # configuration is not part of the identity


class PrepareTests(RuntimeTestCase):
    def test_pulls_by_digest_then_verifies_platform_and_revision(self):
        self.docker.results["image inspect"] = CommandResult(0, f"linux/amd64|{'a' * 40}\n")
        result = self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(result, {"platform": "linux/amd64", "revision_label": "a" * 40})
        self.assertEqual(self.docker.calls[0], ["/usr/bin/docker", "pull", "--quiet", "ghcr.io/example/orders@sha256:" + "b" * 64])
        self.assertEqual(self.docker.sub("rm"), [])  # nothing is stopped or removed while preparing

    def test_missing_revision_label_is_allowed_but_reported(self):
        self.docker.results["image inspect"] = CommandResult(0, "linux/amd64|<no value>\n")
        self.assertIsNone(self.runtime.prepare(REGISTRATION, release())["revision_label"])

    def test_each_verification_failure_is_a_prepare_failure(self):
        cases = {
            "pull denied": ("pull", CommandResult(1, "denied: requested access")),
            "inspect fails": ("image inspect", CommandResult(1, "no such image")),
            "wrong platform": ("image inspect", CommandResult(0, "linux/arm64|\n")),
            "wrong revision": ("image inspect", CommandResult(0, f"linux/amd64|{'f' * 40}\n")),
        }
        for label, (key, result) in cases.items():
            with self.subTest(label):
                self.docker.results = {"image inspect": CommandResult(0, "linux/amd64|\n"), key: result}
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.prepare(REGISTRATION, release())
                self.assertEqual(caught.exception.phase, "prepare")

    def test_a_private_image_is_pulled_with_that_applications_own_login_and_nothing_else_uses_it(self):
        login = self.layout.registry_logins / "orders"
        login.mkdir(parents=True)
        (login / "config.json").write_text('{"auths": {}}')
        self.docker.results["image inspect"] = CommandResult(0, "linux/amd64|\n")
        self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(self.docker.calls[0][:4], ["/usr/bin/docker", "--config", str(login), "pull"])
        self.docker.calls.clear()
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.start(REGISTRATION, release())
        self.assertTrue(all("--config" not in call for call in self.docker.calls))
        other = dict(REGISTRATION, application_id="billing")  # another application has no login and uses none
        self.docker.calls.clear()
        self.runtime.prepare(other, release())
        self.assertNotIn("--config", self.docker.calls[0])

    def test_a_nearly_full_disk_stops_the_deploy_before_anything_is_pulled_or_changed(self):
        with patch("shiplord.runtime.shutil.disk_usage", return_value=type("U", (), {"free": 10 * 2**20})()):
            with self.assertRaisesRegex(RuntimeFailure, "not enough free disk space.*10 MiB free") as caught:
                self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(caught.exception.phase, "prepare")
        self.assertEqual(self.docker.calls, [])
        with patch("shiplord.runtime.shutil.disk_usage", side_effect=OSError):  # unmeasurable: docker decides
            self.docker.results["image inspect"] = CommandResult(0, "linux/amd64|\n")
            self.runtime.prepare(REGISTRATION, release())
        self.assertTrue(self.docker.calls)

    def test_a_timed_out_pull_is_reported_with_its_output(self):
        self.docker.raise_for["pull"] = CommandError("timed out", "timeout", "layer 3/9")
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.prepare(REGISTRATION, release())
        self.assertEqual((caught.exception.phase, caught.exception.detail), ("prepare", "layer 3/9"))


class StartTests(RuntimeTestCase):
    def start(self, **kwargs):
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n" + ID_B + "\n")
        self.runtime.start(REGISTRATION, release(**kwargs))
        return self.docker.sub("run")[0]

    def test_replaces_existing_containers_then_runs_the_release_with_confinement(self):
        argv = self.start(origins=["https://orders.example.com"])
        names = [c[1] for c in self.docker.calls]
        self.assertLess(names.index("rm"), names.index("run"))
        self.assertEqual(self.docker.sub("rm"), [["/usr/bin/docker", "rm", ID_A], ["/usr/bin/docker", "rm", ID_B]])  # never --force
        self.assertEqual(self.docker.sub("ps")[0][-1], "label=shiplord.app=orders")
        self.assertEqual(argv[-1], "ghcr.io/example/orders@sha256:" + "b" * 64)
        pairs = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
        self.assertEqual(pairs["--publish"], "127.0.0.1:20007:8080")  # loopback only
        self.assertEqual((pairs["--memory"], pairs["--memory-swap"]), ("512m", "512m"))
        self.assertEqual(pairs["--restart"], "unless-stopped")
        self.assertEqual(pairs["--volume"], "shiplord-orders-artifacts:/app/src/.adk")
        self.assertEqual(pairs["--security-opt"], "no-new-privileges")
        self.assertEqual(pairs["--cap-drop"], "ALL")
        self.assertIn("max-size=10m", argv)
        self.assertEqual(argv[argv.index("--name") + 1], DockerRuntime.container_name(REGISTRATION, release()))
        environment = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
                       for i in range(len(argv) - 1) if argv[i] == "--env"}
        self.assertEqual(environment["ALLOW_ORIGINS"], '["https://orders.example.com"]')
        self.assertEqual((environment["HOST"], environment["PORT"], environment["SERVE_WEB_INTERFACE"],
                          environment["RELOAD_AGENTS"]), ("0.0.0.0", "8080", "false", "false"))
        self.assertNotIn("--env-file", argv)

    def test_no_frontend_serializes_an_explicit_empty_origin_list(self):
        argv = self.start()
        self.assertIn("ALLOW_ORIGINS=[]", argv)

    def test_environment_file_is_passed_before_managed_values_so_they_win(self):
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("DATABASE_URL=postgres://x\n")
        path.chmod(0o600)
        argv = self.start()
        self.assertEqual(argv[argv.index("--env-file") + 1], str(path))
        self.assertLess(argv.index("--env-file"), argv.index("--env"))

    def test_an_unusable_environment_file_fails_before_the_old_app_is_touched(self):
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("PORT=1\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeFailure, "environment file is unusable"):
            self.runtime.start(REGISTRATION, release())
        self.assertEqual(self.docker.calls, [])
        path.write_text("OK=1\n")
        path.chmod(0o644)
        with self.assertRaises(RuntimeFailure):
            self.runtime.start(REGISTRATION, release())
        self.assertEqual(self.docker.calls, [])

    def test_failures_to_list_remove_or_run_are_reported(self):
        for key, result, phase in (("ps", CommandResult(1, "daemon down"), "stop"), ("rm", CommandResult(1, "busy"), "stop"),
                                   ("run", CommandResult(125, "bad flag"), "start")):
            with self.subTest(key):
                self.docker.calls.clear()
                self.docker.results = {"volume inspect": LEGACY_VOLUME, "inspect": EXITED_LEGACY, "ps": CommandResult(0, ID_A + "\n"), key: result}
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.start(REGISTRATION, release())
                self.assertEqual(caught.exception.phase, phase)

    def test_unexpected_container_listing_output_is_not_passed_to_docker_rm(self):
        self.docker.results["ps"] = CommandResult(0, "--all\n")
        with self.assertRaisesRegex(RuntimeFailure, "unexpected container listing"):
            self.runtime.start(REGISTRATION, release())
        self.assertEqual(self.docker.sub("rm"), [])

    def test_stop_removes_only_labelled_containers(self):
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.docker.sub("rm")[0], ["/usr/bin/docker", "rm", ID_A])
        self.docker.calls.clear()
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.docker.sub("rm"), [])


class InspectTests(RuntimeTestCase):
    def inspect_output(self, state="true false", image="b", mounts=("volume:shiplord-orders-artifacts:/app/src/.adk",)):
        return CommandResult(0, f"{state} ghcr.io/example/orders@sha256:{image * 64} {' '.join(mounts)}\n")

    def test_is_running_reads_the_release_container_state(self):
        self.docker.results["inspect"] = self.inspect_output()
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        self.assertEqual(self.docker.calls[-1][-1], DockerRuntime.container_name(REGISTRATION, release()))
        for result in (self.inspect_output("false false"), self.inspect_output("true true"),  # exited; crash-looping
                       CommandResult(1, "No such object"), CommandResult(0, "true\n")):
            self.docker.results["inspect"] = result
            self.assertFalse(self.runtime.is_running(REGISTRATION, release()), result)

    def test_a_container_with_the_wrong_image_or_mount_is_not_the_release(self):
        # Same name, same readiness answer: only the inspected image and mount prove which configuration runs.
        wrong = {"another image": self.inspect_output(image="c"),
                 "old mount path": self.inspect_output(mounts=("volume:shiplord-orders-artifacts:/data",)),
                 "another volume": self.inspect_output(mounts=("volume:shiplord-other-artifacts:/app/src/.adk",)),
                 "a bind mount": self.inspect_output(mounts=("bind::/app/src/.adk",)),
                 "an extra mount": self.inspect_output(mounts=("volume:shiplord-orders-artifacts:/app/src/.adk", "volume:x:/y")),
                 "no mount": self.inspect_output(mounts=())}
        for label, result in wrong.items():
            with self.subTest(label):
                self.docker.results["inspect"] = result
                self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_a_registry_alias_does_not_change_the_identity_but_a_digest_does(self):
        mirrored = CommandResult(0, "true false localhost:5000/example/orders@sha256:" + "b" * 64
                                 + " volume:shiplord-orders-artifacts:/app/src/.adk\n")
        self.docker.results["inspect"] = mirrored
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = CommandResult(0, mirrored.output.replace("b" * 64, "c" * 64))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = CommandResult(0, "true false orders:latest volume:shiplord-orders-artifacts:/app/src/.adk\n")
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_a_configured_path_is_the_one_verified_and_a_legacy_container_matches_the_default(self):
        configured = release(storage={"container_path": "/data"})
        self.docker.results["inspect"] = self.inspect_output(mounts=("volume:shiplord-orders-artifacts:/data",))
        self.assertTrue(self.runtime.is_running(REGISTRATION, configured))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output()
        self.assertFalse(self.runtime.is_running(REGISTRATION, configured))

    def test_logs_are_bounded_and_empty_without_a_container(self):
        self.assertEqual(self.runtime.logs(REGISTRATION, 50), "")
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        self.docker.results["logs"] = CommandResult(0, "line1\nline2\n")
        self.assertEqual(self.runtime.logs(REGISTRATION, 10**9), "line1\nline2\n")
        self.assertEqual(self.docker.sub("logs")[0][2:4], ["--tail", "1000"])
        self.runtime.logs(REGISTRATION, -5)
        self.assertEqual(self.docker.sub("logs")[1][2:4], ["--tail", "1"])

    def test_discard_reports_but_does_not_raise_when_the_image_is_in_use(self):
        self.assertEqual(self.runtime.discard(REGISTRATION, release()), [])
        self.docker.results["image rm"] = CommandResult(1, "conflict: image is being used")
        self.assertEqual(len(self.runtime.discard(REGISTRATION, release())), 1)


class StorageMountTests(RuntimeTestCase):
    def run_argv(self, **kwargs):
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.start(REGISTRATION, release(**kwargs))
        return self.docker.sub("run")[0]

    def test_the_configured_path_is_mounted_from_the_same_named_volume(self):
        argv = self.run_argv(storage={"container_path": "/var/lib/handwriting"})
        self.assertEqual(argv[argv.index("--volume") + 1], "shiplord-orders-artifacts:/var/lib/handwriting")
        self.assertEqual(argv.count("--volume"), 1)

    def test_a_missing_volume_is_created_labelled_and_local_before_anything_is_removed(self):
        self.docker.results["volume inspect"] = CommandResult(1, "Error: No such volume: shiplord-orders-artifacts")
        self.run_argv()
        names = [" ".join(c[1:3]) if c[1] == "volume" else c[1] for c in self.docker.calls]
        self.assertLess(names.index("volume create"), names.index("run"))
        self.assertEqual(self.docker.sub("volume create")[0][2:], ["create", "--driver", "local", "--label",
                         "shiplord.app=orders", "shiplord-orders-artifacts"])

    def test_prepare_never_creates_a_volume(self):
        self.docker.results["volume inspect"] = CommandResult(1, "Error: No such volume: x")
        self.docker.results["image inspect"] = CommandResult(0, "linux/amd64|\n")
        self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(self.docker.sub("volume create"), [])

    def test_a_foreign_or_unexplained_volume_is_refused_before_any_container_is_touched(self):
        bad = {"another driver": "nfs|null|null", "driver options": 'local|{"device":"/etc"}|null',
               "another application's label": 'local|null|{"shiplord.app":"billing"}', "garbage": "nonsense"}
        for label, output in bad.items():
            with self.subTest(label):
                self.docker.calls.clear()
                self.docker.results["volume inspect"] = CommandResult(0, output)
                with self.assertRaises(RuntimeFailure) as caught:
                    self.run_argv()
                self.assertEqual(caught.exception.phase, "storage")
                self.assertEqual((self.docker.sub("rm"), self.docker.sub("run")), ([], []))

    def test_an_inspection_failure_other_than_not_found_is_not_treated_as_absence(self):
        self.docker.results["volume inspect"] = CommandResult(1, "Cannot connect to the Docker daemon")
        with self.assertRaises(RuntimeFailure):
            self.run_argv()
        self.assertEqual(self.docker.sub("volume create"), [])

    def test_a_labelled_volume_of_this_application_is_accepted(self):
        self.docker.results["volume inspect"] = CommandResult(0, 'local|{}|{"shiplord.app":"orders"}')
        self.run_argv()

    def test_removing_containers_never_removes_volumes(self):
        self.run_argv()
        self.assertNotIn("-v", self.docker.sub("volume inspect")[0])
        self.assertTrue(all("-v" not in c and "--volumes" not in c for c in self.docker.sub("rm")))
        self.assertEqual(self.docker.sub("volume rm"), [])


if __name__ == "__main__":
    unittest.main()
