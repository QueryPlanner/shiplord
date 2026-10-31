"""Configurable Docker storage: validation, legacy compatibility, planning, and capability negotiation."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import configuration, record, request  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.commands import CommandError  # noqa: E402
from shiplord.manifest import ManifestError, validate_manifest  # noqa: E402
from shiplord.release_state import StateError, validate_release  # noqa: E402
from shiplord.storage import DEFAULT_CONTAINER_PATH, StorageError, container_path, validate_container_path  # noqa: E402
from test_client import ClientTestCase  # noqa: E402
from test_commands import COMMIT, IMAGE  # noqa: E402
from test_deploy import DeployTestCase  # noqa: E402


def with_storage(storage, **kwargs):
    value = configuration(**kwargs)
    value["storage"] = storage
    return value


class PathTests(unittest.TestCase):
    def test_canonical_paths_are_accepted(self):
        for path in ("/data", "/var/lib/handwriting", "/app/src/.adk", "/data/etc", "/a.b_c-d", "/var/runtime", "/rootfs"):
            with self.subTest(path):
                self.assertEqual(validate_container_path(path), path)

    def test_everything_else_is_refused_not_normalised(self):
        bad = ["", "/", "data", "/data/", "//data", "/data//x", "/data/.", "/data/..", "/./data", "/data/../etc",
               "/da ta", "/data\n", "/data\t", "/da\\ta", "/da:ta", "/da,ta", "/da=ta", "/daté", "/data\x00", "/" + "a" * 4096,
               "/da\"ta", "/data*", None, 5, b"/data", ["/data"]]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(StorageError):
                validate_container_path(path)

    def test_reserved_roots_and_their_descendants_are_refused_by_component(self):
        for root in ("/proc", "/sys", "/dev", "/run", "/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/root",
                     "/var/run", "/var/lock"):
            for path in (root, root + "/x", root + "/x/y"):
                with self.subTest(path), self.assertRaises(StorageError):
                    validate_container_path(path)

    def test_the_path_limit_is_exactly_4096_bytes(self):
        path = "/" + "a" * 4095
        self.assertEqual(len(path), 4096)
        self.assertEqual(validate_container_path(path), path)
        with self.assertRaises(StorageError):
            validate_container_path(path + "a")


class ManifestTests(unittest.TestCase):
    def test_a_manifest_without_storage_is_unchanged_and_uses_the_legacy_default(self):
        result = validate_manifest(configuration())
        self.assertNotIn("storage", result)  # defaults are applied when interpreting, never written into records
        self.assertEqual(container_path(result), DEFAULT_CONTAINER_PATH)

    def test_storage_round_trips_with_only_the_given_fields(self):
        self.assertEqual(validate_manifest(with_storage({"container_path": "/data"}))["storage"], {"container_path": "/data"})
        both = {"container_path": "/data", "owner_uid": 10001, "owner_gid": 10002}
        self.assertEqual(validate_manifest(with_storage(both))["storage"], both)

    def test_invalid_storage_tables_are_refused(self):
        bad = [{}, {"owner_uid": 1, "owner_gid": 1}, {"container_path": "/etc"}, {"container_path": "/data", "extra": 1},
               {"container_path": "/data", "owner_uid": 10001}, {"container_path": "/data", "owner_gid": 10001},
               *({"container_path": "/data", "owner_uid": uid, "owner_gid": 1}
                 for uid in (0, -1, 2147483648, True, "10001", 1.0, None)),
               *({"container_path": "/data", "owner_uid": 1, "owner_gid": gid} for gid in (0, False, "1")),
               "/data", ["/data"], None]
        for storage in bad:
            with self.subTest(storage=storage), self.assertRaises(ManifestError):
                validate_manifest(with_storage(storage))

    def test_native_applications_refuse_storage(self):
        value = with_storage({"container_path": "/data"})
        value["runtime"] = "native"
        with self.assertRaisesRegex(ManifestError, "only to Docker"):
            validate_manifest(value)

    def test_the_largest_owner_ids_are_accepted(self):
        storage = {"container_path": "/data", "owner_uid": 2147483647, "owner_gid": 1}
        self.assertEqual(validate_manifest(with_storage(storage))["storage"], storage)


class LegacyRecordTests(unittest.TestCase):
    def test_generic_records_keep_the_explicit_profile(self):
        raw = record()
        raw["configuration"]["template"] = "generic"
        self.assertEqual(validate_release(raw, "orders"), raw)

    def test_a_legacy_release_record_loads_equal_to_its_raw_form(self):
        raw = record()
        before = copy.deepcopy(raw)
        self.assertEqual(validate_release(raw, "orders"), before)
        self.assertEqual(raw, before)

    def test_a_stored_release_keeps_and_restores_its_own_path(self):
        raw = record()
        raw["configuration"]["storage"] = {"container_path": "/data"}
        loaded = validate_release(raw, "orders")
        self.assertEqual(container_path(loaded["configuration"]), "/data")
        raw["configuration"]["storage"] = {"container_path": "/etc"}
        with self.assertRaises(StateError):
            validate_release(raw, "orders")


class DeployStorageTests(DeployTestCase):
    def storage_request(self, storage, **kwargs):
        value = request(**kwargs)
        value["configuration"] = with_storage(storage)
        return value

    def test_ownership_is_refused_before_anything_changes(self):
        outcome = self.run_locked(self.deployer.deploy, self.storage_request(
            {"container_path": "/data", "owner_uid": 10001, "owner_gid": 10001}))
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertIn("owner_uid", outcome.detail["message"])
        self.assertEqual(self.runtime.calls, [])
        self.assertIsNone(self.state.read_journal())

    def test_a_path_only_change_with_the_same_image_commit_and_sequence_replaces_the_container(self):
        first = self.deploy()
        self.assertEqual(first.status, "succeeded")
        self.runtime.calls.clear()
        changed = self.run_locked(self.deployer.deploy, self.storage_request({"container_path": "/data"}, request_id="req-2"))
        self.assertEqual(changed.status, "succeeded")
        self.assertIn("start", self.runtime.names())  # not "unchanged"
        self.assertEqual(self.state.read_state()["current"]["configuration"]["storage"], {"container_path": "/data"})
        self.assertEqual(self.state.read_state()["previous"]["configuration"].get("storage"), None)

    def test_plan_reports_old_and_new_paths_and_that_no_data_moves(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.runtime.calls.clear()
        plan = self.run_locked(self.deployer.plan, self.storage_request({"container_path": "/data"}, request_id="req-2"))
        self.assertEqual(plan["action"], "deploy")
        self.assertEqual((plan["storage"]["current_path"], plan["storage"]["candidate_path"]),
                         (DEFAULT_CONTAINER_PATH, "/data"))
        self.assertIn("no files are moved", plan["storage"]["note"])
        self.assertEqual(self.runtime.names(), [])  # planning starts, stops and prepares nothing

    def test_plan_of_a_first_release_has_no_current_path(self):
        plan = self.run_locked(self.deployer.plan, self.storage_request({"container_path": "/data"}))
        self.assertEqual(plan["storage"], {"current_path": None, "candidate_path": "/data", "same_volume": True,
                                           "note": "no path change"})
        self.assertEqual(self.runtime.calls, [])


class NegotiationTests(ClientTestCase):
    def storage_manifest(self):
        return validate_manifest(with_storage({"container_path": "/data"}))

    def test_a_current_helper_advertises_the_feature(self):
        self.assertIn("docker_storage_v1", self.client.call("capabilities")["actions"])
        self.assertIn("docker_generic_v1", self.client.call("capabilities")["actions"])

    def test_combined_features_negotiate_once_before_any_plan_or_job(self):
        manifest = self.storage_manifest() | {"template": "generic"}
        manifest["deploy"].update(shutdown_timeout_seconds=8, shutdown_timeout_action="fail")
        supported = ["docker_storage_v1", "docker_shutdown_v1", "docker_generic_v1"]
        for operation in (commands.plan_application, commands.deploy_application):
            with self.subTest(operation=operation.__name__):
                for features in ([], *(supported[:i] + supported[i + 1:] for i in range(len(supported)))):
                    with patch.object(self.client, "call", return_value={"actions": features}) as call:
                        with self.assertRaises(CommandError) as caught:
                            operation(manifest, self.client, image=IMAGE, commit=COMMIT, sequence=1)
                        self.assertEqual(caught.exception.code, 4)
                        self.assertIn("shiplord server bootstrap", str(caught.exception))
                        self.assertEqual([c.args[0] for c in call.call_args_list], ["capabilities"])
        with patch.object(self.client, "call", return_value={"actions": supported}) as call:
            commands.require_feature_support(manifest, self.client)
            call.assert_called_once_with("capabilities")

    def test_a_generic_plan_through_the_helper_creates_no_job(self):
        manifest = validate_manifest(configuration() | {"template": "generic"})
        code, _ = commands.plan_application(manifest, self.client, image=IMAGE, commit=COMMIT, sequence=1000)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])

    def test_a_helper_without_the_feature_is_refused_before_any_job_or_plan_call(self):
        old = {"status": "ok", "request_id": "x", "version": 1, "helper_version": "0.1.0",
               "actions": ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover"]}
        with patch.object(self.client, "call", return_value=old) as call:
            for operation in (lambda: commands.deploy_application(self.storage_manifest(), self.client, image=IMAGE,
                                                                  commit=COMMIT, sequence=1),
                              lambda: commands.plan_application(self.storage_manifest(), self.client, image=IMAGE,
                                                                commit=COMMIT, sequence=1)):
                with self.assertRaisesRegex(CommandError, "does not support \\[storage\\]"):
                    operation()
            self.assertEqual([c.args[0] for c in call.call_args_list], ["capabilities", "capabilities"])

    def test_legacy_manifests_never_probe_capabilities(self):
        with patch.object(self.client, "call", side_effect=AssertionError("unexpected call")):
            commands.require_feature_support(validate_manifest(configuration()), self.client)

    def test_a_plan_through_the_real_helper_shows_the_path_and_creates_no_job(self):
        code, plan = commands.plan_application(self.storage_manifest(), self.client, image=IMAGE, commit=COMMIT, sequence=1000)
        self.assertEqual((code, plan["storage"]["candidate_path"]), (0, "/data"))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])


if __name__ == "__main__":
    unittest.main()
