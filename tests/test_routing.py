from __future__ import annotations

import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from shiplord import routing
from shiplord.release_state import HostLock
from shiplord.routing import RouteManager, RoutingError, SubprocessCaddy, render_route


def registration(app="orders", domain="orders.example.com", port=20000):
    return {"schema_version": 1, "application_id": app, "deployment_uid": 1001, "deployment_user": "ship-orders",
            "runtime": "docker", "api_domain": domain, "host_port": port,
            "image_repository": f"ghcr.io/example/{app}", "max_memory_mb": 4096}


class FakeCaddy:
    """Records calls; failures are scripted by 1-based call number."""

    def __init__(self, ship: Path):
        self.ship = ship
        self.validate_calls: list[dict] = []
        self.reload_calls = 0
        self.fail_validate: set[int] = set()
        self.fail_reload: set[int] = set()
        self.on_reload = None

    def validate(self, config: Path) -> None:
        number = len(self.validate_calls) + 1
        self.validate_calls.append({"config": config, "text": config.read_text(),
                                    "live": {p.name: p.read_bytes() for p in self.ship.glob("*.caddy")}})
        if number in self.fail_validate:
            raise RoutingError("Caddy command failed", "validation", detail="bad config")

    def reload(self) -> None:
        self.reload_calls += 1
        if self.on_reload:
            self.on_reload(self.reload_calls)
        if self.reload_calls in self.fail_reload:
            raise RoutingError("Caddy command failed", "reload", detail="reload refused")


class RoutingTestCase(unittest.TestCase):
    MAIN = "{\n\temail admin@example.com\n}\nexample.org {\n\trespond \"ok\"\n}\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "caddy"
        self.ship = self.root / "ship"
        self.ship.mkdir(parents=True)
        self.root.chmod(0o755)
        self.ship.chmod(0o755)
        self.write_main(self.MAIN + f"import {self.root}/ship/*.caddy\n")
        lock_temp = tempfile.TemporaryDirectory()
        self.addCleanup(lock_temp.cleanup)
        lock_fd = os.open(lock_temp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, lock_fd)
        self.lock = HostLock(lock_fd)
        self.caddy = FakeCaddy(self.ship)
        self.manager = RouteManager(self.root, self.lock, self.caddy)

    def write_main(self, text):
        path = self.root / "Caddyfile"
        path.write_text(text)
        path.chmod(0o644)

    def write_route(self, name, text, mode=0o644):
        path = self.ship / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def snapshot(self):
        """Every file under the Caddy root, for byte-for-byte comparison."""
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def assertNoTransactionLeftovers(self):
        leftovers = [p.name for p in self.root.rglob(".*") if p.is_file()]
        self.assertEqual(leftovers, [])


class ApplyTests(RoutingTestCase):
    def test_first_route_is_rendered_validated_before_publish_and_reloaded(self):
        other = self.write_route("billing.caddy", "billing.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        before_other, before_main = other.read_bytes(), (self.root / "Caddyfile").read_bytes()
        with self.lock.acquire():
            result = self.manager.apply(registration())
        self.assertTrue(result.changed)
        self.assertEqual((self.ship / "orders.caddy").read_bytes(),
                         b"orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n")
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertEqual(len(self.caddy.validate_calls), 1)
        seen = self.caddy.validate_calls[0]
        self.assertNotIn("orders.caddy", seen["live"])  # candidate was not live during validation
        self.assertIn(f"import {self.root}/ship/billing.caddy", seen["text"])
        self.assertIn(f"import {self.root}/ship/.tmp-", seen["text"])
        self.assertNotIn("*.caddy", seen["text"])
        self.assertIn("example.org", seen["text"])
        self.assertEqual(other.read_bytes(), before_other)
        self.assertEqual((self.root / "Caddyfile").read_bytes(), before_main)
        self.assertNoTransactionLeftovers()

    def test_validation_runs_inside_the_caddy_root_so_relative_imports_resolve(self):
        with self.lock.acquire():
            self.manager.apply(registration())
        self.assertEqual(self.caddy.validate_calls[0]["config"].parent, self.root)

    def test_identical_route_skips_validation_and_reload(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            result = self.manager.apply(registration())
        self.assertFalse(result.changed)
        self.assertEqual((len(self.caddy.validate_calls), self.caddy.reload_calls), (1, 1))

    def test_changed_route_replaces_the_file_and_drops_the_backup(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            result = self.manager.apply(registration(port=20005))
        self.assertTrue(result.changed)
        self.assertIn(b"127.0.0.1:20005", (self.ship / "orders.caddy").read_bytes())
        self.assertEqual(self.caddy.reload_calls, 2)
        self.assertNoTransactionLeftovers()

    def test_requires_the_host_lock_and_writes_nothing_without_it(self):
        before = self.snapshot()
        with self.assertRaisesRegex(RoutingError, "lock"):
            self.manager.apply(registration())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.reload_calls, 0)

    def test_invalid_registration_or_injection_is_rejected_before_any_write(self):
        before = self.snapshot()
        bad = [registration(domain="a.example.com {\n\tfile_server\n}\nb.example.com"),
               registration(domain="a.example.com }"), registration(domain="A.example.com"),
               registration(port=2019), registration(port=22), dict(registration(), extra=1)]
        with self.lock.acquire():
            for value in bad:
                with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                    self.manager.apply(value)
            with self.assertRaises(ValueError):
                self.manager.remove("../ship/orders")
            with self.assertRaises(RoutingError):
                self.manager.reconcile("orders", registration(app="billing", domain="b.example.com"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.validate_calls, [])

    def test_render_route_validates_its_inputs(self):
        with self.assertRaises(ValueError):
            render_route("bad domain", 20000)
        with self.assertRaises(RoutingError):
            render_route("orders.example.com", 2019)


class FailureTests(RoutingTestCase):
    def test_first_release_validation_failure_leaves_no_route_and_no_reload(self):
        self.caddy.fail_validate = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(registration())
        self.assertEqual(caught.exception.kind, "validation")
        self.assertEqual(caught.exception.detail, "bad config")
        self.assertNotIn("bad config", str(caught.exception))
        self.assertEqual(self.caddy.reload_calls, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertNoTransactionLeftovers()

    def test_validation_failure_keeps_the_previous_route_live(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_validate = {2}  # the candidate check; call 3 is the baseline check
            with self.assertRaises(RoutingError):
                self.manager.apply(registration(port=20007))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertNoTransactionLeftovers()

    def test_preexisting_invalid_configuration_is_not_blamed_on_the_candidate(self):
        self.caddy.fail_validate = {1, 2}
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "already invalid"):
            self.manager.apply(registration())
        self.assertEqual(self.caddy.reload_calls, 0)

    def test_reload_failure_restores_the_previous_route_and_reconciles(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_reload = {2}
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20007))
        self.assertEqual(caught.exception.kind, "reload")
        self.assertFalse(caught.exception.intervention_required)
        self.assertEqual(self.caddy.reload_calls, 3)  # apply, failed reload, reconciling reload
        self.assertEqual(self.snapshot(), before)
        self.assertNoTransactionLeftovers()

    def test_first_release_reload_failure_removes_the_route_and_reloads_again(self):
        self.caddy.fail_reload = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError):
            self.manager.apply(registration())
        self.assertEqual(self.caddy.reload_calls, 2)
        self.assertEqual(self.snapshot(), before)
        self.assertNoTransactionLeftovers()

    def test_second_reload_failure_requires_intervention_with_the_old_file_restored(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_reload = {2, 3}
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20007))
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(self.snapshot(), before)

    def test_failed_restore_requires_intervention(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.caddy.fail_reload = {2}
            self.caddy.on_reload = lambda n: (self.ship / ".prior-orders").unlink() if n == 2 else None
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20007))
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(self.caddy.reload_calls, 2)  # never claims a reconciling reload happened

    def test_other_routes_are_untouched_by_every_outcome(self):
        other = self.write_route("billing.caddy", "billing.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        expected = other.read_bytes()
        outcomes = [{"fail_validate": {1, 2}}, {"fail_validate": {3}}, {"fail_reload": {1}}, {"fail_reload": {4, 5}}]
        with self.lock.acquire():
            for outcome in outcomes:
                self.caddy.fail_validate = outcome.get("fail_validate", set())
                self.caddy.fail_reload = outcome.get("fail_reload", set())
                self.caddy.validate_calls.clear()
                self.caddy.reload_calls = 0
                try:
                    self.manager.apply(registration())
                except RoutingError:
                    pass
                self.assertEqual(other.read_bytes(), expected)


class RemoveAndReconcileTests(RoutingTestCase):
    def test_remove_is_validated_reloaded_and_idempotent(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.assertTrue(self.manager.remove("orders").changed)
            self.assertFalse((self.ship / "orders.caddy").exists())
            self.assertFalse(self.manager.remove("orders").changed)
        self.assertEqual(self.caddy.reload_calls, 2)
        self.assertNotIn("orders.caddy", self.caddy.validate_calls[1]["text"])
        self.assertNoTransactionLeftovers()

    def test_remove_reload_failure_restores_the_route(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_reload = {2}
            with self.assertRaises(RoutingError):
                self.manager.remove("orders")
        self.assertEqual(self.snapshot(), before)

    def test_reconcile_always_validates_and_reloads_even_when_the_file_matches(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            result = self.manager.reconcile("orders", registration())
        self.assertFalse(result.changed)
        self.assertEqual((len(self.caddy.validate_calls), self.caddy.reload_calls), (2, 2))

    def test_reconcile_to_absent_reloads_even_when_the_file_is_already_gone(self):
        with self.lock.acquire():
            result = self.manager.reconcile("orders", None)
        self.assertFalse(result.changed)
        self.assertEqual(self.caddy.reload_calls, 1)

    def test_reconcile_failure_of_an_unchanged_route_flags_intervention(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.caddy.fail_reload = {2}
            with self.assertRaises(RoutingError) as caught:
                self.manager.reconcile("orders", registration())
        self.assertTrue(caught.exception.intervention_required)

    def test_interrupted_transaction_blocks_apply_but_reconcile_recovers_it(self):
        # Crash after publishing the candidate: new file live, old file kept as the backup.
        old = b"orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n"
        self.write_route("orders.caddy", render_route("orders.example.com", 20009).decode())
        self.write_route(".prior-orders", old.decode())
        with self.lock.acquire():
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20009))
            self.assertEqual(caught.exception.kind, "interrupted")
            self.assertEqual(self.caddy.reload_calls, 0)
            self.manager.reconcile("orders", registration())  # desired state is the old route
        self.assertEqual((self.ship / "orders.caddy").read_bytes(), old)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertNoTransactionLeftovers()

    def test_stale_temporary_candidates_are_discarded(self):
        self.write_route(".tmp-deadbeef", "x.example.com {\n}\n")
        with self.lock.acquire():
            self.manager.apply(registration())
        self.assertNoTransactionLeftovers()


class ConflictTests(RoutingTestCase):
    def assertConflict(self, kind="conflict", **options):
        before = self.snapshot()
        manager = RouteManager(self.root, self.lock, self.caddy, **options)
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            manager.apply(registration())
        self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(self.caddy.validate_calls, [])
        self.assertEqual(self.caddy.reload_calls, 0)
        self.assertEqual(self.snapshot(), before)

    def main_with(self, text):
        self.write_main(self.MAIN + text + f"\nimport {self.root}/ship/*.caddy\n")

    def test_hostname_forms_in_the_main_caddyfile_are_conflicts(self):
        forms = ["orders.example.com {\n}", "https://orders.example.com {\n}", "http://orders.example.com:8080 {\n}",
                 "orders.example.com:443 {\n}", "ORDERS.Example.com {\n}", "a.example.com, orders.example.com {\n}",
                 "a.example.com,orders.example.com {\n}", '"orders.example.com" {\n}',
                 "orders.example.com. {\n}", "https://ORDERS.example.com.:443 {\n}",
                 "site.example.net {\n\theader Link https://orders.example.com/x\n}"]
        for form in forms:
            with self.subTest(form=form):
                self.main_with(form)
                self.assertConflict()

    def test_internationalized_spellings_of_a_hostname_are_the_same_site(self):
        self.assertEqual(list(routing._addresses("bücher.example.org.")), ["xn--bcher-kva.example.org"])

    def test_dot_named_caddy_files_are_inspected_because_caddy_globs_match_them(self):
        self.write_route(".stray.caddy", "orders.example.com {\n}\n")
        self.assertConflict()

    def test_conflict_in_another_managed_route_is_refused(self):
        self.write_route("billing.caddy", "orders.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        self.assertConflict()

    def test_wildcard_overlap_is_refused_unless_explicitly_allowed(self):
        self.main_with("*.example.com {\n\trespond \"wild\"\n}")
        self.assertConflict()
        with self.lock.acquire():
            RouteManager(self.root, self.lock, self.caddy, allow_wildcard_overlap=True).apply(registration())
        self.assertTrue((self.ship / "orders.caddy").exists())

    def test_unrelated_wildcard_and_similar_hostnames_do_not_conflict(self):
        self.main_with("*.example.net {\n}\nxorders.example.com {\n}\norders.example.com.evil.test {\n}")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)

    def test_comments_and_heredoc_bodies_are_not_hostnames(self):
        self.main_with("# orders.example.com used to live here\nother.example.net {\n\trespond <<EOT\n"
                       "orders.example.com\nEOT 200\n}")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)

    def test_followed_imports_are_inspected(self):
        (self.root / "extra").mkdir()
        (self.root / "extra" / "legacy.conf").write_text("orders.example.com {\n}\n")
        for argument in (f"{self.root}/extra/*.conf", "extra/legacy.conf", "./extra/*"):
            with self.subTest(argument=argument):
                self.write_main(self.MAIN + f"import {argument}\nimport {self.root}/ship/*.caddy\n")
                self.assertConflict()

    def test_snippet_imports_are_ignored_and_own_glob_is_not_followed(self):
        self.write_main(self.MAIN + "(common) {\n\tencode gzip\n}\nimport common\n" + f"import {self.root}/ship/*.caddy\n")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)

    def test_uninspectable_configuration_fails_closed(self):
        (self.root / "extra").mkdir()
        (self.root / "extra" / "dir.conf").mkdir()
        cases = {
            "directory import": f"import {self.root}/extra/dir.conf\n",
            "placeholder address": "{$SITE_ADDRESS} {\n}\n",
            "unbalanced open": "example.net {\n",
            "unbalanced close": "}\n",
            "unterminated quote": 'example.net "unterminated\n',
            "open heredoc": "example.net {\n\trespond <<EOT\nbody\n}\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write_main(self.MAIN + text + f"import {self.root}/ship/*.caddy\n")
                self.assertConflict("uninspectable")

    def test_import_nesting_and_count_are_bounded(self):
        for index in range(4):
            (self.root / f"level{index}.conf").write_text(f"import level{index + 1}.conf\n")
        (self.root / "level4.conf").write_text("example.net {\n}\n")
        self.write_main(self.MAIN + "import level0.conf\n" + f"import {self.root}/ship/*.caddy\n")
        self.assertConflict("uninspectable")
        many = self.root / "many"
        many.mkdir()
        for index in range(65):
            (many / f"{index}.conf").write_text(f"site{index}.example.net {{\n}}\n")
        self.write_main(self.MAIN + "import many/*.conf\n" + f"import {self.root}/ship/*.caddy\n")
        self.assertConflict("uninspectable")


class PreconditionTests(RoutingTestCase):
    def assertPrecondition(self, pattern):
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, pattern) as caught:
            self.manager.apply(registration())
        self.assertEqual(caught.exception.kind, "precondition")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.validate_calls, [])

    def test_missing_directory_or_caddyfile(self):
        (self.root / "Caddyfile").unlink()
        self.assertPrecondition("missing")
        self.ship.rmdir()
        self.assertPrecondition("not bootstrapped")

    def test_missing_nested_or_duplicate_import_is_refused(self):
        for text in (self.MAIN, self.MAIN + f"example.net {{\n\timport {self.root}/ship/*.caddy\n}}\n",
                     self.MAIN + f"import {self.root}/ship/*.caddy\nimport {self.root}/ship/*.caddy\n",
                     self.MAIN + f"import {self.root}/other/*.caddy\n"):
            with self.subTest(text=text[-60:]):
                self.write_main(text)
                self.assertPrecondition("exactly once")

    def test_symlinked_or_writable_directories_and_files_are_refused(self):
        self.ship.chmod(0o775)
        self.assertPrecondition("not trusted")
        self.ship.chmod(0o755)
        moved = self.root / "real-ship"
        self.ship.rename(moved)
        self.ship.symlink_to(moved)
        self.assertPrecondition("not bootstrapped")
        self.ship.unlink()
        moved.rename(self.ship)
        (self.root / "Caddyfile").chmod(0o666)
        with self.lock.acquire(), self.assertRaises(RoutingError):
            self.manager.apply(registration())
        (self.root / "Caddyfile").chmod(0o644)
        target = self.root / "elsewhere"
        target.write_text("billing.example.com {\n}\n")
        (self.ship / "billing.caddy").symlink_to(target)
        with self.lock.acquire(), self.assertRaises(RoutingError):
            self.manager.apply(registration())

    def test_relative_root_is_refused(self):
        with self.assertRaises(RoutingError):
            RouteManager(Path("relative"), self.lock, self.caddy)


class EnsureImportTests(RoutingTestCase):
    def setUp(self):
        super().setUp()
        self.write_main(self.MAIN)
        self.line = f"import {self.root}/ship/*.caddy\n"

    def test_appends_the_import_once_preserving_existing_content(self):
        with self.lock.acquire():
            self.assertTrue(self.manager.ensure_import().changed)
            self.assertEqual((self.root / "Caddyfile").read_text(), self.MAIN + self.line)
            self.assertFalse(self.manager.ensure_import().changed)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertIn(self.line, self.caddy.validate_calls[0]["text"])
        self.assertNoTransactionLeftovers()
        self.assertEqual(stat.S_IMODE((self.root / "Caddyfile").stat().st_mode), 0o644)

    def test_adds_a_newline_when_the_file_does_not_end_with_one(self):
        self.write_main("example.org {\n}")
        with self.lock.acquire():
            self.manager.ensure_import()
        self.assertEqual((self.root / "Caddyfile").read_text(), "example.org {\n}\n" + self.line)

    def test_duplicate_imports_and_missing_files_are_refused(self):
        self.write_main(self.MAIN + self.line + self.line)
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "more than once"):
            self.manager.ensure_import()
        (self.root / "Caddyfile").unlink()
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "missing"):
            self.manager.ensure_import()

    def test_validation_failure_leaves_the_caddyfile_untouched(self):
        self.caddy.fail_validate = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertEqual(caught.exception.kind, "validation")
        self.assertEqual((self.snapshot(), self.caddy.reload_calls), (before, 0))

    def test_reload_failure_restores_the_caddyfile_and_reloads_again(self):
        self.caddy.fail_reload = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertFalse(caught.exception.intervention_required)
        self.assertEqual((self.snapshot(), self.caddy.reload_calls), (before, 2))
        self.assertNoTransactionLeftovers()

    def test_interrupted_change_and_missing_lock_are_refused(self):
        with self.assertRaisesRegex(RoutingError, "lock"):
            self.manager.ensure_import()
        (self.root / ".prior-Caddyfile").write_text("old")
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertTrue(caught.exception.intervention_required)


class SubprocessCaddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def script(self, name, body):
        path = self.directory / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    def test_validate_and_reload_run_fixed_argv_without_a_shell(self):
        log = self.directory / "log"
        caddy = self.script("caddy", f'echo "$@" > {log}\n')
        systemctl = self.script("systemctl", f'echo "$@" >> {log}\n')
        runner = SubprocessCaddy(caddy, systemctl)
        runner.validate(Path("/etc/caddy/with space; rm -rf x"))
        runner.reload()
        self.assertEqual(log.read_text().splitlines(),
                         ["validate --config /etc/caddy/with space; rm -rf x --adapter caddyfile", "reload caddy"])

    def test_environment_is_minimal_and_fixed(self):
        out = self.directory / "env"
        caddy = self.script("caddy", f'env > {out}\n')
        os.environ["SHIPLORD_SECRET_TEST"] = "do-not-leak"
        self.addCleanup(os.environ.pop, "SHIPLORD_SECRET_TEST", None)
        SubprocessCaddy(caddy).validate(Path("/x"))
        names = {line.split("=", 1)[0] for line in out.read_text().splitlines()}
        self.assertNotIn("SHIPLORD_SECRET_TEST", names)
        self.assertIn("PATH", names)

    def test_failure_exposes_capped_clean_detail_but_not_in_the_message(self):
        caddy = self.script("caddy", 'printf "bad\\033[31m config\\n"; head -c 200000 /dev/zero | tr "\\0" x; exit 3\n')
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy(caddy).validate(Path("/x"))
        error = caught.exception
        self.assertEqual(error.kind, "validation")
        self.assertLessEqual(len(error.detail), 4096)
        self.assertIn("bad?[31m config", error.detail)
        self.assertNotIn("bad", str(error))

    def test_timeout_kills_the_process_and_reports_it(self):
        marker = self.directory / "survived"
        caddy = self.script("caddy", f"sleep 5\ntouch {marker}\n")
        started = __import__("time").monotonic()
        with self.assertRaisesRegex(RoutingError, "timed out"):
            SubprocessCaddy(caddy, timeout=0.5).validate(Path("/x"))
        self.assertLess(__import__("time").monotonic() - started, 4)
        self.assertFalse(marker.exists())

    def test_missing_executable_is_a_routing_error(self):
        with self.assertRaises(RoutingError):
            SubprocessCaddy(str(self.directory / "absent")).validate(Path("/x"))

    def test_reload_failure_has_reload_kind(self):
        systemctl = self.script("systemctl", "exit 1\n")
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy("/bin/true", systemctl).reload()
        self.assertEqual(caught.exception.kind, "reload")


if __name__ == "__main__":
    unittest.main()
