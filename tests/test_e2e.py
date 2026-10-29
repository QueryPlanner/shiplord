"""The real shiplord CLI against a real Linux host: sshd, sudo, Caddy, Docker, and a registry.

Runs only with SHIPLORD_TEST_E2E=1 inside a disposable container prepared by the e2e runner
(systemctl, systemd-run, and docker shims installed; docker.real present). It changes /etc,
/usr/local, and system accounts, so it refuses to run anywhere else.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import test_deploy_integration as fixtures  # noqa: E402  (module import: its test class is not collected)
from shiplord.client import AdminClient, ClientError  # noqa: E402

ENABLED = (os.environ.get("SHIPLORD_TEST_E2E") == "1" and os.path.exists("/.dockerenv") and os.geteuid() == 0
           and os.path.exists("/usr/bin/docker.real") and shutil.which("caddy") and shutil.which("sshd"))
PORT, ADMIN_PASSWORD = 2222, "correct-horse-battery"
ROOT = Path("/tmp/e2e")


def sh(*argv, check=True, input=None):
    result = subprocess.run(argv, capture_output=True, text=True, input=input, timeout=300)
    if check and result.returncode != 0:
        raise AssertionError(f"{argv} failed: {result.stdout} {result.stderr}")
    return result


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_E2E=1 in the prepared disposable container")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ROOT.mkdir(exist_ok=True)
        cls.config = ROOT / "config"
        cls.config.mkdir(exist_ok=True)
        cls.app = "fixture-" + secrets.token_hex(3)
        cls.project = ROOT / "project"
        cls.project.mkdir(exist_ok=True)
        cls.executable = shutil.which("shiplord")
        cls.environment = dict(os.environ, XDG_CONFIG_HOME=str(cls.config), HOME="/root")
        cls.setup_fake_gh()
        cls.setup_accounts_and_ssh()
        cls.setup_caddy()
        cls.setup_images()
        cls.addClassCleanup(cls.remove_resources)

    @classmethod
    def setup_fake_gh(cls):
        """A stand-in for the GitHub CLI that records secrets and variables as files, like GitHub would hold them."""
        cls.gh_store = ROOT / "gh"
        (cls.gh_store / "secrets").mkdir(parents=True, exist_ok=True)
        (cls.gh_store / "variables").mkdir(parents=True, exist_ok=True)
        binary = ROOT / "bin"
        binary.mkdir(exist_ok=True)
        (binary / "gh").write_text(f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
store = Path({str(cls.gh_store)!r})
args = sys.argv[1:]
def names(kind): return sorted(p.name for p in (store / kind).iterdir())
if args[:2] == ["api", "user"]: print("octocat")
elif args[:2] == ["repo", "view"]: print(json.dumps({{"viewerPermission": "ADMIN"}}))
elif args[:2] in (["secret", "set"], ["variable", "set"]):
    (store / (args[0] + "s") / args[2]).write_bytes(sys.stdin.buffer.read())
elif args[:2] == ["secret", "list"]: print(json.dumps([{{"name": n}} for n in names("secrets")]))
elif args[:2] == ["variable", "list"]:
    print(json.dumps([{{"name": n, "value": (store / "variables" / n).read_text()}} for n in names("variables")]))
elif args[:2] == ["run", "list"]: print("[]")
else: sys.exit("unsupported: " + " ".join(args))
""")
        os.chmod(binary / "gh", 0o755)
        cls.environment["PATH"] = f"{binary}:{os.environ['PATH']}"

    @classmethod
    def setup_accounts_and_ssh(cls):
        sh("useradd", "-m", "-s", "/bin/bash", "appuser")
        sh("useradd", "-m", "-s", "/bin/bash", "adminpw")
        sh("chpasswd", input=f"adminpw:{ADMIN_PASSWORD}\n")
        Path("/etc/sudoers.d/e2e-admins").write_text("appuser ALL=(ALL) NOPASSWD: ALL\nadminpw ALL=(ALL) ALL\n")
        os.chmod("/etc/sudoers.d/e2e-admins", 0o440)
        sh("ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", str(ROOT / "dev_key"))
        public = (ROOT / "dev_key.pub").read_text()
        for user in ("appuser", "adminpw"):
            directory = Path(f"/home/{user}/.ssh")
            directory.mkdir(mode=0o700)
            (directory / "authorized_keys").write_text(public)
            os.chmod(directory / "authorized_keys", 0o600)
            sh("chown", "-R", f"{user}:{user}", str(directory))
        sh("ssh-keygen", "-A")
        os.makedirs("/run/sshd", exist_ok=True)
        cls_sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-p", str(PORT), "-o", "ListenAddress=127.0.0.1"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls_sshd.kill)
        Path("/run/test-sshd.pid").write_text(str(cls_sshd.pid))
        host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2]
        (ROOT / "known_hosts").write_text(f"[127.0.0.1]:{PORT} {host_key[0]} {host_key[1]}\n")
        os.makedirs("/root/.ssh", exist_ok=True)
        Path("/root/.ssh/config").write_text(f"Host *\n  Port {PORT}\n  IdentityFile {ROOT / 'dev_key'}\n  IdentitiesOnly yes\n"
                                             f"  UserKnownHostsFile {ROOT / 'known_hosts'}\n")
        for _ in range(50):
            if sh("ssh-keyscan", "-p", str(PORT), "127.0.0.1", check=False).stdout:
                break
            time.sleep(0.1)

    @classmethod
    def setup_caddy(cls):
        cls.caddy = subprocess.Popen(["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.caddy.kill)
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:2019/config/", timeout=1)
                return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("caddy did not start")

    @classmethod
    def setup_images(cls):
        name = "shiplord-e2e-registry"
        fixtures.docker("run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{fixtures.REGISTRY_PORT}:5000", "registry:2")
        cls.addClassCleanup(lambda: fixtures.docker("rm", "-f", name, check=False))
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{fixtures.REGISTRY_PORT}/v2/", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        context = ROOT / "fixture"
        context.mkdir(exist_ok=True)
        (context / "server.py").write_text(fixtures.SERVER)
        (context / "Dockerfile").write_text(fixtures.DOCKERFILE)
        cls.images = {}
        for version, mode, commit in (("v1", "healthy", "a"), ("v2", "healthy", "b"), ("v3", "unhealthy", "c")):
            tag = f"{fixtures.REPOSITORY}:{version}"
            fixtures.docker("build", "-q", "--build-arg", f"VERSION={version}", "--build-arg", f"MODE={mode}",
                            "--build-arg", f"REVISION={commit * 40}", "-t", tag, str(context))
            digest = re.search(r"digest: (sha256:[0-9a-f]{64})", fixtures.docker("push", tag).stdout).group(1)
            fixtures.docker("rmi", tag, check=False)
            fixtures.docker("rmi", f"{fixtures.REPOSITORY}@{digest}", check=False)
            cls.images[version] = {"image": f"ghcr.io/shiplord/fixture@{digest}", "commit": commit * 40}

    @classmethod
    def remove_resources(cls):
        ids = fixtures.docker("ps", "-aq", "--filter", f"label=shiplord.app={cls.app}", check=False).stdout.split()
        if ids:
            fixtures.docker("rm", "-f", *ids, check=False)
        fixtures.docker("volume", "rm", "-f", f"shiplord-{cls.app}-artifacts", check=False)
        for image in cls.images.values():
            fixtures.docker("rmi", image["image"].replace("ghcr.io/", "localhost:5000/"), check=False)

    # helpers -------------------------------------------------------------------
    def cli(self, *args, check=None, cwd=None, environment=None):
        result = subprocess.run([self.executable, *args], cwd=cwd or self.project, env=environment or self.environment,
                                capture_output=True, text=True, timeout=600)
        if check is not None and result.returncode != check:
            self.fail(f"shiplord {' '.join(args)} exited {result.returncode}, expected {check}\n{result.stdout}\n{result.stderr}")
        return result

    def cli_json(self, *args, check=0):
        return json.loads(self.cli(*args, "--json", check=check).stdout)

    def deploy(self, version, sequence, *extra, check=0):
        image = self.images[version]
        return self.cli_json("deploy", "--image", image["image"], "--commit", image["commit"], "--sequence", str(sequence),
                             "--no-verify-public", *extra, check=check)

    def get(self, path):
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as response:
                    return response.read().decode()
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)

    # the scenario, in order ------------------------------------------------------
    def test_01_register_the_server_and_probe_the_unbootstrapped_host(self):
        self.cli("server", "add", "production", "--ssh", "appuser@127.0.0.1", check=0)
        self.assertEqual(self.cli("doctor", "--server", "production", "--json").returncode, 3)  # no dispatcher yet

    def test_02_bootstrap_installs_the_host_and_repeating_it_changes_nothing(self):
        first = self.cli_json("server", "bootstrap", "production")
        self.assertTrue(first["ok"] and all(first["changed"].values()), first)
        for path in ("/usr/local/sbin/shiplord-host", "/usr/local/sbin/shiplord-admin", "/usr/local/sbin/shiplord-worker",
                     "/usr/local/libexec/shiplord-dispatch"):
            self.assertTrue(os.access(path, os.X_OK), path)
        self.assertIn("import /etc/caddy/ship/*.caddy", Path("/etc/caddy/Caddyfile").read_text())
        second = self.cli_json("server", "bootstrap", "production")
        self.assertFalse(any(second["changed"].values()), second)
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(self.cli_json("doctor", "--server", "production")["helper_version"], "0.1.0")
        self.assertEqual(list(Path("/tmp").glob("shiplord-install.*")), [])  # the upload was removed

    def test_03_configure_and_provision(self):
        self.cli("configure", "--name", self.app, "--server", "production", "--api-domain", "fixture.example.test",
                 "--frontend", "https://app.example.test", check=0)
        manifest = self.project / "ship.toml"
        manifest.write_text(manifest.read_text() + '\n[github]\nrepository = "shiplord/fixture"\ndeployment_branch = "main"\n')
        result = self.cli_json("provision", "--public-key", str(ROOT / "dev_key.pub"))
        type(self).port = result["host_port"]
        self.assertEqual((result["deployment_user"], result["deploy_login"]), (f"ship-{self.app}", f"ship-{self.app}@127.0.0.1"))
        self.assertTrue(all(result["created"].values()), result["created"])
        self.assertEqual(sh("getent", "passwd", result["deployment_user"]).returncode, 0)
        again = self.cli_json("provision", "--public-key", str(ROOT / "dev_key.pub"))
        self.assertFalse(any(again["created"].values()), again["created"])
        self.assertEqual(again["host_port"], result["host_port"])

    def test_04_doctor_reports_each_check_and_only_dns_is_missing(self):
        result = self.cli_json("doctor", check=3)
        checks = result["checks"]
        self.assertTrue(checks["server_access"]["ok"] and checks["helper_actions"]["ok"] and checks["application"]["ok"])
        self.assertFalse(checks["dns"]["ok"])

    def test_05_environment_is_stored_privately_and_values_are_never_printed(self):
        secret_file = ROOT / "secrets.env"
        secret_file.write_text("CUSTOM_TOKEN=s3cret-value\n")
        result = self.cli("env", "set", "--from-file", str(secret_file), "--json", check=0)
        self.assertNotIn("s3cret-value", result.stdout + result.stderr)
        stored = Path(f"/etc/shiplord/env/{self.app}/app.env")
        self.assertEqual((oct(stored.stat().st_mode & 0o777), stored.stat().st_uid), ("0o600", 0))
        self.assertEqual(self.cli_json("env", "keys")["keys"], ["CUSTOM_TOKEN"])
        empty = subprocess.run([self.executable, "env", "set", "--from-file", "-", "--json"], cwd=self.project,
                               env=self.environment, input="", capture_output=True, text=True)
        self.assertEqual(empty.returncode, 2)  # an empty file is refused, never a silent wipe
        reserved = subprocess.run([self.executable, "env", "set", "--from-file", "-", "--json"], cwd=self.project,
                                  env=self.environment, input="PORT=9\n", capture_output=True, text=True)
        self.assertEqual(reserved.returncode, 2)
        self.assertEqual(self.cli_json("env", "keys")["keys"], ["CUSTOM_TOKEN"])  # unchanged by the refused attempts

    def test_06_plan_is_read_only(self):
        image = self.images["v1"]
        plan = self.cli_json("plan", "--image", image["image"], "--commit", image["commit"], "--sequence", "1000")
        self.assertEqual((plan["action"], plan["route_change"]), ("deploy", "create"))
        self.assertEqual(fixtures.docker("ps", "-aq", "--filter", f"label=shiplord.app={self.app}").stdout.split(), [])
        self.assertFalse(Path(f"/etc/caddy/ship/{self.app}.caddy").exists())

    def test_07_first_deploy_goes_live_through_the_whole_stack(self):
        result = self.deploy("v1", 1000)
        self.assertEqual((result["status"], result["release"]["commit"]), ("succeeded", "a" * 40))
        self.assertTrue(result["probes"]["proxy_routing"], result["probes"])
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "s3cret-value")
        route = Path(f"/etc/caddy/ship/{self.app}.caddy").read_text()
        self.assertEqual(route, f"fixture.example.test {{\n\treverse_proxy 127.0.0.1:{self.port}\n}}\n")
        status = self.cli_json("status")
        self.assertEqual((status["application"]["current"]["commit"], status["application"]["running"]), ("a" * 40, True))
        self.assertIn("fixture started version=v1", self.cli("logs", "--tail", "20", check=0).stdout)

    def test_08_a_second_release_replaces_the_first_and_keeps_the_volume(self):
        self.assertEqual(self.get("/counter"), "1")
        self.assertEqual(self.deploy("v2", 1001)["status"], "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "2")

    def test_09_an_unhealthy_release_exits_5_and_leaves_the_previous_one_serving(self):
        result = self.deploy("v3", 1002, check=5)
        self.assertEqual((result["status"], result["ok"], result["kind"]), ("rolled_back", False, "timeout"))
        self.assertEqual(self.get("/version"), "v2")

    def test_10_a_stale_release_is_refused_with_exit_1(self):
        result = self.deploy("v1", 5, check=1)
        self.assertEqual(result["error"], "stale_release")
        self.assertEqual(self.get("/version"), "v2")

    def test_11_a_retried_request_returns_the_recorded_result_without_redeploying(self):
        first = self.deploy("v1", 2000, "--request-id", "retry-1")
        containers = fixtures.docker("ps", "-q", "--filter", f"label=shiplord.app={self.app}").stdout
        again = self.deploy("v1", 2000, "--request-id", "retry-1")
        self.assertEqual((first["status"], again["status"]), ("succeeded", "succeeded"))
        self.assertEqual(fixtures.docker("ps", "-q", "--filter", f"label=shiplord.app={self.app}").stdout, containers)
        self.assertEqual(self.get("/version"), "v1")

    def test_12_rollback_restores_the_previous_release(self):
        result = self.cli_json("rollback")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.get("/version"), "v2")

    def test_13_recover_with_nothing_to_recover_succeeds(self):
        self.assertEqual(self.cli_json("recover")["status"], "nothing_to_recover")

    def test_14_a_password_protected_sudo_is_supported_without_the_password_leaving_stdin(self):
        client = AdminClient("adminpw@127.0.0.1", prompt=lambda text: ADMIN_PASSWORD)
        listing = client.call("keys", self.app, {"operation": "list"})
        self.assertEqual(len(listing["keys"]), 1)
        with self.assertRaises(ClientError) as caught:
            AdminClient("adminpw@127.0.0.1", prompt=lambda text: "wrong").call("keys", self.app, {"operation": "list"})
        self.assertEqual(caught.exception.code, 3)
        self.assertNotIn("wrong", str(caught.exception))

    def test_15_a_deployment_login_cannot_use_admin_operations(self):
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", f"ship-{self.app}@127.0.0.1", "sudo", "-n",
                                 "/usr/local/sbin/shiplord-admin"], capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)

    # GitHub setup and the CI path ---------------------------------------------------------
    def ci_environment(self, secret: bytes | None = None):
        """Exactly what a CI job has: GitHub's secret and variables, no personal SSH config, no server alias."""
        home = Path(tempfile.mkdtemp(prefix="ci-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        variables = {p.name: p.read_text() for p in (self.gh_store / "variables").iterdir()}
        key = home / "key"
        key.write_bytes(secret if secret is not None else (self.gh_store / "secrets" / "SHIPLORD_SSH_PRIVATE_KEY").read_bytes())
        key.chmod(0o600)
        known = home / "known_hosts"
        known.write_text(variables["SHIPLORD_SSH_KNOWN_HOSTS"])
        environment = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                       "SHIPLORD_SSH_TARGET": f"{variables['SHIPLORD_DEPLOY_USER']}@{variables['SHIPLORD_SERVER_HOST']}",
                       "SHIPLORD_SSH_IDENTITY": str(key), "SHIPLORD_SSH_KNOWN_HOSTS_FILE": str(known)}
        if variables.get("SHIPLORD_SSH_PORT"):
            environment["SHIPLORD_SSH_PORT"] = variables["SHIPLORD_SSH_PORT"]
        return environment

    def ci_cli(self, environment, *args):
        return subprocess.run([self.executable, *args], cwd=self.project, env=environment, capture_output=True, text=True,
                              timeout=600)

    def test_16_github_setup_proves_the_ci_key_then_stores_it_and_writes_the_workflow(self):
        result = self.cli_json("github", "setup", "--repo", "shiplord/fixture", "--tooling-ref", "ab" * 20,
                               "--tailscale-client-id", "client123", "--tailscale-audience", "aud456")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["github_account"], "octocat")
        self.assertIn("authenticated against the pinned host keys", result["steps"]["ssh_check"])
        secrets, variables = self.gh_store / "secrets", self.gh_store / "variables"
        self.assertTrue((secrets / "SHIPLORD_SSH_PRIVATE_KEY").read_bytes().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertEqual((variables / "SHIPLORD_DEPLOY_USER").read_text(), f"ship-{self.app}")
        self.assertEqual((variables / "SHIPLORD_SSH_PORT").read_text(), str(PORT))
        self.assertTrue((variables / "SHIPLORD_SSH_KNOWN_HOSTS").read_text().startswith(f"[127.0.0.1]:{PORT} ssh-"))
        self.assertEqual((variables / "SHIPLORD_TS_CLIENT_ID").read_text(), "client123")
        workflow = (self.project / ".github" / "workflows" / "shiplord-release.yml").read_text()
        self.assertIn("ab" * 20, workflow)
        self.assertEqual(oct((self.config / "shiplord" / "ci" / self.app).stat().st_mode & 0o777), "0o700")
        self.assertEqual([p.name for p in (self.config / "shiplord" / "ci" / self.app).iterdir()], ["state.json"])
        type(self).first_label = result["key_label"]
        type(self).first_secret = (secrets / "SHIPLORD_SSH_PRIVATE_KEY").read_bytes()

    def test_17_the_ci_credentials_alone_can_operate_the_application_over_the_pinned_host_keys(self):
        environment = self.ci_environment()
        status = self.ci_cli(environment, "status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout)["application"]["running"], True)
        image = self.images["v1"]
        deployed = self.ci_cli(environment, "deploy", "--image", image["image"], "--commit", image["commit"], "--sequence",
                               "9000", "--request-id", "gh-1-1", "--no-verify-public", "--json")
        self.assertEqual(deployed.returncode, 0, deployed.stdout + deployed.stderr)
        self.assertEqual(json.loads(deployed.stdout)["status"], "succeeded")
        self.assertEqual(self.get("/version"), "v1")

    def test_18_a_wrong_pinned_host_key_makes_the_ci_connection_fail_closed(self):
        environment = self.ci_environment()
        known = Path(environment["SHIPLORD_SSH_KNOWN_HOSTS_FILE"])
        known.write_text(f"[127.0.0.1]:{PORT} " + "ssh-ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAIPzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\n")
        result = self.ci_cli(environment, "status", "--json")
        self.assertEqual(result.returncode, 3, result.stdout)

    def test_19_rotating_the_ci_key_revokes_the_old_one_and_the_new_one_works(self):
        rotated = self.cli_json("github", "rotate-key")
        self.assertNotEqual(rotated["key_label"], self.first_label)
        self.assertEqual(rotated["steps"]["revoke"], f"{self.first_label} removed")
        new = self.ci_cli(self.ci_environment(), "status", "--json")
        self.assertEqual(new.returncode, 0, new.stdout + new.stderr)
        old = self.ci_cli(self.ci_environment(secret=self.first_secret), "status", "--json")
        server_keys = Path(f"/etc/shiplord/authorized_keys/ship-{self.app}").read_text()
        self.assertEqual(old.returncode, 3, f"{old.stdout}\nserver keys:\n{server_keys}\nfirst label: {self.first_label}\n"
                                            f"sshd said: {old.stderr[-300:]}")  # the revoked key is refused by the server
        listed = self.cli_json("env", "keys")  # the personal key and application are unaffected
        self.assertIn("keys", listed)

    def test_20_github_status_reflects_the_real_configuration(self):
        code = self.cli("github", "status", "--check-server", "--json")
        result = json.loads(code.stdout)
        self.assertEqual(code.returncode, 3)  # only the (never performed) workflow run is missing
        failing = [name for name, check in result["checks"].items() if not check["ok"]]
        self.assertEqual(failing, ["last_run"])
        self.assertTrue(result["checks"]["server_key"]["ok"])

    def test_21_a_registry_login_is_stored_root_only_used_for_pulls_and_never_shown(self):
        token = "ghp_e2eExampleToken1234567890"
        (ROOT / "token").write_text(token + "\n")
        result = self.cli("registry", "login", "--username", "ci-bot", "--token-file", str(ROOT / "token"), "--json", check=0)
        self.assertNotIn(token, result.stdout + result.stderr)
        stored = Path(f"/etc/shiplord/docker/{self.app}/config.json")
        self.assertEqual((oct(stored.stat().st_mode & 0o777), stored.stat().st_uid), ("0o600", 0))
        self.assertIn(token, base64.b64decode(json.loads(stored.read_text())["auths"]["ghcr.io"]["auth"]).decode())
        self.assertEqual(self.cli_json("registry", "status")["registries"], [{"registry": "ghcr.io", "username": "ci-bot"}])
        image = self.images["v2"]  # a real docker pull now runs with --config pointing at this application's login
        deployed = self.deploy("v2", 9500)
        self.assertEqual(deployed["status"], "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        self.cli("registry", "logout", "--json", check=0)
        self.assertFalse(stored.exists())
        self.assertEqual(self.cli_json("registry", "status")["registries"], [])

    def test_22_the_deep_doctor_reports_the_real_host(self):
        result = self.cli_json("doctor", "--server", "production", "--deep", check=3)  # caddy is not a systemd service here
        checks = result["checks"]
        for name in ("helper", "sshd", "docker", "disk", "applications"):
            self.assertTrue(checks[name]["ok"], (name, checks[name]))
        self.assertTrue(checks["caddy"]["import_present"])
        self.assertEqual([a["application"] for a in checks["applications"]["applications"]], [self.app])

    # A second application on the same host ----------------------------------------------------
    def containers_of(self, app):
        return fixtures.docker("ps", "-q", "--filter", f"label=shiplord.app={app}").stdout.split()

    def get_from(self, port, path):
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
                    return response.read().decode()
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)

    def test_23_a_second_application_is_isolated_from_the_first(self):
        second = "second-" + secrets.token_hex(3)
        type(self).second_app = second
        self.addCleanup(lambda: [fixtures.docker("rm", "-f", c, check=False) for c in self.containers_of(second)])
        self.addCleanup(lambda: fixtures.docker("volume", "rm", "-f", f"shiplord-{second}-artifacts", check=False))
        project = ROOT / "project2"
        project.mkdir(exist_ok=True)
        type(self).project2 = project
        self.cli("configure", "--name", second, "--server", "production", "--api-domain", "second.example.test",
                 "--no-frontend", check=0, cwd=project)
        manifest = project / "ship.toml"
        manifest.write_text(manifest.read_text() + '\n[github]\nrepository = "shiplord/fixture"\ndeployment_branch = "main"\n')
        first_containers = self.containers_of(self.app)
        provisioned = json.loads(self.cli("provision", "--public-key", str(ROOT / "dev_key.pub"), "--json", check=0, cwd=project).stdout)
        self.assertNotEqual(provisioned["host_port"], self.port)
        self.assertEqual(provisioned["deployment_user"], f"ship-{second}")
        type(self).port2 = provisioned["host_port"]
        image = self.images["v1"]
        result = json.loads(self.cli("deploy", "--image", image["image"], "--commit", image["commit"], "--sequence", "100",
                                     "--no-verify-public", "--json", check=0, cwd=project).stdout)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((self.get_from(self.port2, "/version"), self.get_from(self.port, "/version")), ("v1", "v2"))
        self.assertEqual(self.containers_of(self.app), first_containers)  # the first application was not touched
        self.assertEqual(self.get_from(self.port2, "/counter"), "1")  # its own volume, not the first one's counter
        self.assertNotEqual(self.get("/counter"), "1")
        bad = self.images["v3"]
        failed = self.cli("deploy", "--image", bad["image"], "--commit", bad["commit"], "--sequence", "101", "--no-verify-public",
                          "--json", check=5, cwd=project)
        self.assertEqual(json.loads(failed.stdout)["status"], "rolled_back")
        self.assertEqual(self.containers_of(self.app), first_containers)
        self.assertEqual(self.get("/version"), "v2")

    def test_24_each_applications_login_is_refused_for_the_other_and_both_routes_are_live(self):
        def as_login(login_app, cwd):
            environment = dict(self.environment, SHIPLORD_SSH_TARGET=f"ship-{login_app}@127.0.0.1",
                               SHIPLORD_SSH_IDENTITY=str(ROOT / "dev_key"), SHIPLORD_SSH_KNOWN_HOSTS_FILE=str(ROOT / "known_hosts"),
                               SHIPLORD_SSH_PORT=str(PORT))
            return self.cli("status", "--json", cwd=cwd, environment=environment)
        self.assertEqual(as_login(self.app, self.project).returncode, 0)  # its own login works
        self.assertEqual(as_login(self.second_app, self.project2).returncode, 0)
        denied = as_login(self.app, self.project2)  # the first application's login asking for the second application
        self.assertEqual(denied.returncode, 3, denied.stdout)
        denied = as_login(self.second_app, self.project)
        self.assertEqual(denied.returncode, 3, denied.stdout)
        configuration = json.loads(urllib.request.urlopen("http://127.0.0.1:2019/config/apps/http/servers", timeout=5).read())
        from shiplord.probes import config_routes_to
        self.assertTrue(config_routes_to(configuration, "fixture.example.test", f"127.0.0.1:{self.port}"))
        self.assertTrue(config_routes_to(configuration, "second.example.test", f"127.0.0.1:{self.port2}"))
        self.assertFalse(config_routes_to(configuration, "second.example.test", f"127.0.0.1:{self.port}"))  # no crossed routes


if __name__ == "__main__":
    unittest.main()
