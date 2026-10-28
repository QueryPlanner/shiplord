"""The native lifecycle through the real CLI on a host with real systemd (SHIPLORD_TEST_SYSTEMD=1).

Runs only inside a disposable container whose PID 1 is systemd (real transient jobs, real units,
real journald, real `systemctl reload caddy`, sshd under socket activation). The fixture app is a
small C program compiled for the machine under test, so the same test runs on x86-64 and arm64.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path("/tmp/e2e")


def _systemd_running() -> bool:
    try:
        result = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() in ("running", "degraded")


ENABLED = (os.environ.get("SHIPLORD_TEST_SYSTEMD") == "1" and os.path.exists("/.dockerenv") and os.geteuid() == 0
           and shutil.which("gcc") and shutil.which("caddy") and _systemd_running())

FIXTURE = r'''
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#ifndef VERSION
#define VERSION "v0"
#endif
static void reply(int fd, int status, const char *body) {
    char out[4096];
    int n = snprintf(out, sizeof out, "HTTP/1.1 %d X\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s", status, strlen(body), body);
    write(fd, out, n);
}
int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
#ifdef MODE_CRASH
    puts("fixture crashing on purpose");
    return 1;
#endif
#ifdef SLOW_START
    sleep(SLOW_START);
#endif
#ifdef ALLOC_MB
    size_t bytes = (size_t)ALLOC_MB * 1024 * 1024;
    char *block = malloc(bytes);
    if (block) for (size_t i = 0; i < bytes; i += 4096) block[i] = 1;
#endif
    const char *port = getenv("PORT"), *host = getenv("HOST");
    printf("fixture started version=%s port=%s\n", VERSION, port ? port : "?");
    int server = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(atoi(port ? port : "0"))};
    inet_pton(AF_INET, host ? host : "127.0.0.1", &address.sin_addr);
    if (bind(server, (struct sockaddr *)&address, sizeof address) != 0 || listen(server, 16) != 0) { perror("bind"); return 2; }
    for (;;) {
        int client = accept(server, NULL, NULL);
        if (client < 0) continue;
        char request[2048] = {0};
        read(client, request, sizeof request - 1);
        char path[512] = "/";
        sscanf(request, "GET %511s", path);
        char body[2048] = "ok";
        int status = 200;
        if (!strcmp(path, "/ready")) {
#ifdef MODE_UNHEALTHY
            status = 503;
#endif
        } else if (!strcmp(path, "/version")) snprintf(body, sizeof body, "%s", VERSION);
        else if (!strcmp(path, "/whoami")) snprintf(body, sizeof body, "%d", (int)getuid());
        else if (!strncmp(path, "/env/", 5)) { const char *v = getenv(path + 5); snprintf(body, sizeof body, "%s", v ? v : "<unset>"); }
        else if (!strcmp(path, "/counter")) {
            long n = 0; FILE *f = fopen("counter", "r"); if (f) { fscanf(f, "%ld", &n); fclose(f); }
            f = fopen("counter", "w"); if (f) { fprintf(f, "%ld", ++n); fclose(f); } snprintf(body, sizeof body, "%ld", n);
        } else if (!strcmp(path, "/write-outside")) {
            FILE *f = fopen("/etc/shiplord-should-not-exist", "w"); if (f) { fclose(f); snprintf(body, sizeof body, "wrote"); } else snprintf(body, sizeof body, "denied");
        } else if (!strcmp(path, "/caps")) {
            FILE *f = fopen("/proc/self/status", "r"); char line[256]; body[0] = 0;
            while (f && fgets(line, sizeof line, f)) if (!strncmp(line, "CapEff", 6)) { snprintf(body, sizeof body, "%s", line); break; }
            if (f) fclose(f);
        }
        reply(client, status, body);
        close(client);
    }
}
'''


def sh(*argv, check=True, input=None, timeout=300):
    result = subprocess.run(argv, capture_output=True, text=True, input=input, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"{argv} failed: {result.stdout} {result.stderr}")
    return result


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_SYSTEMD=1 in a disposable container running systemd")
class NativeSystemdTests(unittest.TestCase):
    MEMORY_MB = 128

    @classmethod
    def setUpClass(cls):
        ROOT.mkdir(exist_ok=True)
        cls.app = "native-" + secrets.token_hex(3)
        cls.project = ROOT / "project"
        cls.project.mkdir(exist_ok=True)
        cls.config = ROOT / "config"
        cls.config.mkdir(exist_ok=True)
        cls.executable = shutil.which("shiplord")
        cls.environment = dict(os.environ, XDG_CONFIG_HOME=str(cls.config), HOME="/root")
        cls.setup_ssh()
        cls.build_fixtures()
        cls.addClassCleanup(cls.cleanup)

    @classmethod
    def setup_ssh(cls):
        sh("useradd", "-m", "-s", "/bin/bash", "appuser")
        Path("/etc/sudoers.d/e2e-admin").write_text("appuser ALL=(ALL) NOPASSWD: ALL\n")
        os.chmod("/etc/sudoers.d/e2e-admin", 0o440)
        sh("ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", str(ROOT / "dev_key"))
        directory = Path("/home/appuser/.ssh")
        directory.mkdir(mode=0o700)
        (directory / "authorized_keys").write_text((ROOT / "dev_key.pub").read_text())
        os.chmod(directory / "authorized_keys", 0o600)
        sh("chown", "-R", "appuser:appuser", str(directory))
        sh("ssh-keygen", "-A")
        sh("systemctl", "start", "ssh.socket", check=False)
        host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2]
        (ROOT / "known_hosts").write_text(f"127.0.0.1 {host_key[0]} {host_key[1]}\n")
        os.makedirs("/root/.ssh", exist_ok=True)
        Path("/root/.ssh/config").write_text(f"Host *\n  IdentityFile {ROOT / 'dev_key'}\n  IdentitiesOnly yes\n"
                                             f"  UserKnownHostsFile {ROOT / 'known_hosts'}\n")
        for _ in range(60):
            if sh("ssh-keyscan", "127.0.0.1", check=False).stdout:
                break
            time.sleep(0.5)

    @classmethod
    def build_fixtures(cls):
        source = ROOT / "fixture.c"
        source.write_text(FIXTURE)
        cls.binaries = {}
        variants = {"v1": [], "v2": [], "unhealthy": ["-DMODE_UNHEALTHY"], "crash": ["-DMODE_CRASH"],
                    "hungry": ["-DALLOC_MB=300"], "slow": ["-DSLOW_START=4"], "stuck": ["-DSLOW_START=40"], "v3": []}
        for index, (name, flags) in enumerate(variants.items()):
            target = ROOT / f"app-{name}"
            version = name if name.startswith("v") else name
            sh("gcc", "-O1", f'-DVERSION="{version}"', *flags, "-o", str(target), str(source))
            cls.binaries[name] = {"path": target, "commit": f"{index + 1:x}" * 40}

    @classmethod
    def cleanup(cls):
        sh("systemctl", "disable", "--now", f"shiplord-{cls.app}.service", check=False)

    # helpers ------------------------------------------------------------------
    def cli(self, *args, check=None, **kwargs):
        result = subprocess.run([self.executable, *args], cwd=self.project, env=self.environment, capture_output=True,
                                text=True, timeout=900, **kwargs)
        if check is not None and result.returncode != check:
            self.fail(f"shiplord {' '.join(args)} exited {result.returncode}, expected {check}\n{result.stdout}\n{result.stderr}")
        return result

    def cli_json(self, *args, check=0):
        return json.loads(self.cli(*args, "--json", check=check).stdout)

    def deploy(self, name, sequence, *extra, check=0):
        binary = self.binaries[name]
        return self.cli_json("deploy", "--binary", str(binary["path"]), "--commit", binary["commit"], "--sequence",
                             str(sequence), "--no-verify-public", *extra, check=check)

    def get(self, path, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as response:
                    return response.read().decode()
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)

    def show(self, *properties):
        output = sh("systemctl", "show", f"shiplord-{self.app}.service", *[f"--property={p}" for p in properties]).stdout
        return dict(line.split("=", 1) for line in output.splitlines())

    def wait_until(self, condition, timeout=60, interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(interval)
        self.fail("timed out waiting for a condition")

    # the scenario ----------------------------------------------------------------
    def test_01_bootstrap_and_provision_a_native_application(self):
        self.cli("server", "add", "production", "--ssh", "appuser@127.0.0.1", check=0)
        first = self.cli_json("server", "bootstrap", "production")
        self.assertTrue(first["ok"] and first["changed"]["caddy_import"] and first["changed"]["sshd"], first)
        self.assertEqual(sh("systemctl", "is-active", "caddy").stdout.strip(), "active")  # still running after the reload
        self.cli("configure", "--name", self.app, "--server", "production", "--api-domain", "fixture.example.test",
                 "--no-frontend", "--runtime", "native", check=0)
        manifest = self.project / "ship.toml"
        text = manifest.read_text().replace("memory_mb = 512", f"memory_mb = {self.MEMORY_MB}")
        manifest.write_text(text.replace("readiness_timeout_seconds = 90", "readiness_timeout_seconds = 12"))
        result = self.cli_json("provision", "--public-key", str(ROOT / "dev_key.pub"))
        type(self).port = result["host_port"]
        self.assertEqual(sh("getent", "passwd", f"app-{self.app}").stdout.split(":")[6].strip(), "/usr/sbin/nologin")
        self.assertEqual(oct(Path(f"/var/lib/shiplord/data/{self.app}").stat().st_mode & 0o777), "0o700")

    def test_02_environment_values_survive_systemd_quoting_exactly(self):
        tricky = 'a "quoted" \\back $HOME ${X} %h \'single\' #hash = é日本'
        (ROOT / "secrets.env").write_text(f"TRICKY={tricky}\nPLAIN=value\n")
        self.cli("env", "set", "--from-file", str(ROOT / "secrets.env"), "--json", check=0)
        type(self).tricky = tricky

    def test_03_first_native_deploy_runs_a_confined_unprivileged_service(self):
        result = self.deploy("v1", 1000)
        self.assertEqual((result["status"], result["release"]["commit"]), ("succeeded", self.binaries["v1"]["commit"]))
        self.assertTrue(result["probes"]["proxy_routing"], result["probes"])  # real caddy reload + admin API
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(self.get("/env/TRICKY"), self.tricky)
        self.assertEqual(self.get("/env/HOST"), "127.0.0.1")
        self.assertEqual(self.get("/env/ALLOW_ORIGINS"), "[]")
        runtime_uid = int(sh("id", "-u", f"app-{self.app}").stdout)
        self.assertEqual(int(self.get("/whoami")), runtime_uid)
        self.assertEqual(self.get("/caps").split()[-1], "0000000000000000")  # no capabilities at all
        self.assertEqual(self.get("/write-outside"), "denied")  # ProtectSystem=strict
        self.assertFalse(Path("/etc/shiplord-should-not-exist").exists())
        properties = self.show("MemoryMax", "NoNewPrivileges", "User", "ActiveState", "Restart", "UnitFileState")
        self.assertEqual((properties["MemoryMax"], properties["NoNewPrivileges"], properties["User"]),
                         (str(self.MEMORY_MB * 1024 * 1024), "yes", f"app-{self.app}"))
        self.assertEqual((properties["ActiveState"], properties["Restart"], properties["UnitFileState"]),
                         ("active", "always", "enabled"))  # enabled: it comes back after a reboot
        binary = next(Path(f"/opt/shiplord/{self.app}").glob("*/app"))
        self.assertEqual((binary.stat().st_uid, oct(binary.stat().st_mode & 0o777)), (0, "0o755"))
        self.assertIn("fixture started version=v1", self.cli("logs", "--tail", "20", check=0).stdout)  # from journald

    def test_04_a_crashed_process_is_restarted_by_systemd(self):
        pid = self.show("MainPID")["MainPID"]
        os.kill(int(pid), signal.SIGKILL)
        self.wait_until(lambda: self.show("MainPID")["MainPID"] not in (pid, "0"))
        self.assertEqual(self.get("/ready"), "ok")

    def test_05_a_second_release_replaces_the_first_and_keeps_data(self):
        self.assertEqual(self.get("/counter"), "1")
        result = self.deploy("v2", 1001)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "2")  # data outside the release directories survived
        self.assertEqual(len(list(Path(f"/opt/shiplord/{self.app}").glob("*/app"))), 2)  # previous kept for rollback

    def test_06_failing_releases_roll_back_with_the_real_cause(self):
        unhealthy = self.deploy("unhealthy", 1002, check=5)
        self.assertEqual((unhealthy["status"], unhealthy["kind"]), ("rolled_back", "timeout"))
        crash = self.deploy("crash", 1003, check=5)
        self.assertEqual((crash["status"], crash["kind"]), ("rolled_back", "exited"))
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "3")

    def test_07_the_memory_limit_is_enforced_by_the_kernel(self):
        result = self.deploy("hungry", 1004, check=5)  # allocates 300 MB against a 128 MB limit
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.get("/version"), "v2")

    def test_08_a_rejected_wrong_architecture_binary_never_reaches_the_server_staging(self):
        wrong = ROOT / "wrong-arch"
        data = bytearray(self.binaries["v1"]["path"].read_bytes())
        data[18:20] = (0xB7 if int.from_bytes(data[18:20], "little") == 0x3E else 0x3E).to_bytes(2, "little")
        wrong.write_bytes(data)
        result = self.cli("deploy", "--binary", str(wrong), "--commit", "e" * 40, "--sequence", "2000", "--json")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("runs linux-", json.loads(result.stdout)["error"])
        self.assertEqual(list(Path(f"/var/lib/shiplord/apps/{self.app}/staging").glob("*")), [])

    def test_09_a_client_that_dies_mid_deploy_does_not_stop_the_deployment(self):
        binary = self.binaries["slow"]
        process = subprocess.Popen([self.executable, "deploy", "--binary", str(binary["path"]), "--commit", binary["commit"],
                                    "--sequence", "3000", "--no-verify-public", "--request-id", "disconnect-1", "--json"],
                                   cwd=self.project, env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        unit = f"shiplord-job-{self.app}:disconnect-1.service"
        self.wait_until(lambda: sh("systemctl", "is-active", unit, check=False).stdout.strip() == "active", timeout=60)
        process.kill()  # the SSH session and the CLI are gone; the transient unit must carry on
        process.wait()
        self.wait_until(lambda: self.cli_json("status")["application"]["current"]["commit"] == binary["commit"], timeout=90)
        self.assertEqual(self.get("/version"), "slow")
        self.assertIsNone(self.cli_json("status")["application"]["unfinished_transaction"])

    def test_10_a_killed_worker_leaves_a_journal_and_recover_restores_the_committed_release(self):
        binary = self.binaries["stuck"]
        process = subprocess.Popen([self.executable, "deploy", "--binary", str(binary["path"]), "--commit", binary["commit"],
                                    "--sequence", "4000", "--no-verify-public", "--request-id", "killed-1", "--json"],
                                   cwd=self.project, env=self.environment, stdout=subprocess.PIPE, text=True)
        unit = f"shiplord-job-{self.app}:killed-1.service"
        self.wait_until(lambda: (self.cli_json("status")["application"]["unfinished_transaction"] or {}).get("phase")
                        == "runtime-changing", timeout=60)
        sh("systemctl", "kill", "--signal=SIGKILL", unit)  # the worker dies mid-transaction
        stdout, _ = process.communicate(timeout=120)
        outcome = json.loads(stdout)
        self.assertEqual((process.returncode, outcome["status"]), (6, "interrupted"))
        self.assertIsNotNone(self.cli_json("status")["application"]["unfinished_transaction"])
        blocked = self.deploy("v3", 4001, check=1)
        self.assertEqual(blocked["error"], "recovery_required")
        recovered = self.cli_json("recover")
        self.assertEqual(recovered["status"], "recovered")
        self.assertIsNone(self.cli_json("status")["application"]["unfinished_transaction"])
        self.assertEqual(self.get("/version"), "slow")  # the last committed release is serving again

    def test_11_rollback_restores_the_previous_release(self):
        result = self.cli_json("rollback")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.get("/version"), "v2")

    def test_13_the_deep_doctor_sees_a_healthy_systemd_host(self):
        result = self.cli_json("doctor", "--server", "production", "--deep", check=3)  # no Docker daemon in this container
        checks = result["checks"]
        for name in ("helper", "caddy", "sshd", "disk", "applications"):
            self.assertTrue(checks[name]["ok"], (name, checks[name]))
        self.assertTrue(checks["caddy"]["service_active"])  # the real caddy.service, through real systemctl
        self.assertFalse(checks["docker"]["ok"])
        application = checks["applications"]["applications"][0]
        self.assertEqual((application["runtime"], application["deployed"], application["unfinished_transaction"]),
                         ("native", True, None))

    def test_12_old_release_directories_are_removed_once_they_leave_the_retention_window(self):
        self.deploy("v3", 5000)
        self.assertEqual(self.get("/version"), "v3")
        releases = list(Path(f"/opt/shiplord/{self.app}").glob("*/app"))
        self.assertEqual(len(releases), 2)  # exactly current and previous: failed and interrupted candidates are gone
        self.assertEqual(list(Path(f"/var/lib/shiplord/apps/{self.app}/staging").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
