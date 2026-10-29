"""Client side of the SSH exchanges with a Shiplord server.

Application calls go through the application's forced command and the privileged helper.
Administration calls go through the administrator's own sudo; the sudo password, if one is
needed, is read locally and sent only on stdin after ``sudo -n`` has said it is required.
"""

from __future__ import annotations

import getpass
import io
import re
import shlex
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from typing import Callable

from .framing import read_frame, write_frame
from .installer import helper_modules
from .layout import ADMIN_PATH, DISPATCH_PATH, deployment_user
from .protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, ProtocolError
from .servers import SshOptions, ssh_command

Runner = Callable[[list[str], bytes, float], tuple[bytes, bytes, int]]
POLL_INTERVAL_SECONDS = 2.0
POLL_FAILURE_LIMIT = 5


class ClientError(Exception):
    """A remote exchange failed; ``code`` is the CLI exit status."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def run_ssh(command: list[str], payload: bytes, timeout: float) -> tuple[bytes, bytes, int]:
    """Run an ssh command with a deadline; the response is bounded by the protocol's message size."""
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ClientError("the SSH exchange timed out", 3) from None
    except OSError:
        raise ClientError("could not start ssh", 3) from None
    if len(result.stdout) > MAX_MESSAGE_BYTES + 4:
        raise ClientError("the server returned an oversized response", 1)
    return result.stdout, result.stderr[:4096], result.returncode


PayloadRunner = Callable[[list[str], bytes, "Path", float], tuple[bytes, bytes, int]]
UPLOAD_MINIMUM_RATE = 131072  # bytes per second assumed when sizing the upload deadline
CHUNK = 1 << 20


def upload_timeout(size: int) -> float:
    return 120 + size / UPLOAD_MINIMUM_RATE


def run_ssh_stream(command: list[str], header: bytes, payload: Path, timeout: float) -> tuple[bytes, bytes, int]:
    """Send ``header`` then the file's bytes on stdin without holding the file in memory."""
    import threading
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        raise ClientError("could not start ssh", 3) from None
    stdin, process.stdin = process.stdin, None

    def feed() -> None:
        try:
            stdin.write(header)
            with payload.open("rb") as file:
                while chunk := file.read(CHUNK):
                    stdin.write(chunk)
        except OSError:  # the server may answer and close early (a refusal); its response is still read below
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise ClientError("the SSH upload timed out", 3) from None
    finally:
        writer.join(timeout=5)
    if len(stdout) > MAX_MESSAGE_BYTES + 4:
        raise ClientError("the server returned an oversized response", 1)
    return stdout, stderr[:4096], process.returncode


def _frame(message: dict) -> bytes:
    buffer = io.BytesIO()
    write_frame(buffer, message)
    return buffer.getvalue()


def _parse(stdout: bytes) -> dict:
    try:
        stream = io.BytesIO(stdout)
        response = read_frame(stream)
        if stream.read(1):
            raise ProtocolError("trailing output")
        return response
    except ProtocolError:
        raise ClientError("the server did not return a valid response; check the SSH login and that the "
                          "server is bootstrapped and the application provisioned", 3) from None


def new_request_id(prefix: str = "r") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


class HelperClient:
    """Calls the privileged helper as an application's deployment account."""

    def __init__(self, application_id: str, server_target: str, runner: Runner = run_ssh,
                 payload_runner: PayloadRunner = run_ssh_stream, *, ssh_options: SshOptions | None = None,
                 explicit_target: bool = False):
        """``server_target`` is the administrator login; the deployment account is derived from it unless
        ``explicit_target`` says it already names the deployment account (CI supplies it directly)."""
        user, _, host = server_target.partition("@")
        self.target = server_target if explicit_target else f"{deployment_user(application_id)}@{host}"
        self.application_id, self.runner, self.payload_runner = application_id, runner, payload_runner
        self.ssh_options = ssh_options

    def call(self, action: str, *, request_id: str | None = None, commit: str | None = None,
             arguments: dict | None = None, timeout: float = 60, payload: Path | None = None) -> dict:
        request_id = request_id or new_request_id()
        message = {"version": PROTOCOL_VERSION, "request_id": request_id, "action": action,
                   "application_id": self.application_id, "commit": commit, "arguments": arguments or {}}
        command = ssh_command(self.target, DISPATCH_PATH, self.ssh_options)
        if payload is not None:
            stdout, _stderr, status = self.payload_runner(command, _frame(message), payload,
                                                          timeout + upload_timeout(payload.stat().st_size))
        else:
            stdout, _stderr, status = self.runner(command, _frame(message), timeout)
        response = _parse(stdout)
        if response.get("status") == "incompatible":
            raise ClientError("the server helper speaks a different protocol version; upgrade one side", 4)
        if response.get("error") == "access_denied":
            raise ClientError("access denied: this application is not provisioned for your key, or the helper "
                              "refused the request", 3)
        if response.get("request_id") not in (request_id, None) or "status" not in response:
            raise ClientError("the server response did not match the request", 1)
        if response["status"] == "error":
            raise ClientError(response.get("message") or f"the server rejected the request ({response.get('error')})",
                              2 if response.get("error") in ("invalid_request", "invalid_payload") else 1)
        return response


def wait_for_job(client: HelperClient, job_id: str, *, timeout: float, on_progress: Callable[[str], None] = lambda p: None,
                 interval: float = POLL_INTERVAL_SECONDS, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> dict:
    """Poll a job by ID until it finishes; tolerates brief SSH failures. Returns the outcome dict."""
    deadline, failures, last_phase = clock() + timeout, 0, None
    while True:
        try:
            job = client.call("status", arguments={"job_id": job_id}, timeout=60)["result"]["job"]
            failures = 0
        except ClientError as error:
            failures += 1
            if failures >= POLL_FAILURE_LIMIT or error.code in (3, 4) and "access" in str(error):
                raise
            job = None
        if job is not None:
            if job["phase"] != last_phase:
                last_phase = job["phase"]
                on_progress(last_phase)
            if job["state"] == "finished":
                return job["outcome"]
            if job["state"] == "interrupted":
                return {"status": "interrupted", "exit_code": 6,
                        "detail": {"error": "worker_vanished", "job_id": job_id,
                                   "message": "the job stopped before finishing; run `shiplord recover`"}}
        if clock() >= deadline:
            raise ClientError(f"timed out waiting for job {job_id}; it may still be running: run `shiplord status`", 1)
        sleep(interval)


def submit_and_wait(client: HelperClient, action: str, *, request_id: str, commit: str | None = None,
                    arguments: dict | None = None, timeout: float, on_progress: Callable[[str], None] = lambda p: None,
                    payload: Path | None = None, **wait_options) -> dict:
    response = client.call(action, request_id=request_id, commit=commit, arguments=arguments, payload=payload)
    if response["status"] == "finished":
        return response["outcome"]
    if response["status"] != "accepted":
        raise ClientError("unexpected response from the helper", 1)
    job = response["job"]
    on_progress(job["phase"])
    if job["state"] == "finished":
        return job["outcome"]
    return wait_for_job(client, request_id, timeout=timeout, on_progress=on_progress, **wait_options)


class AdminClient:
    """Calls the root-only admin entry point through the administrator's own sudo."""

    def __init__(self, server_target: str, prompt: Callable[[str], str] = getpass.getpass, runner: Runner = run_ssh):
        self.target, self.prompt, self.runner = server_target, prompt, runner

    def _sudo(self, argv: list[str], payload: bytes, timeout: float) -> tuple[bytes, bytes, int]:
        command = lambda sudo: ssh_command(self.target, shlex.join([*sudo, *argv]))  # noqa: E731
        stdout, stderr, status = self.runner(command(["sudo", "-n"]), payload, timeout)
        needs_password = b"a password is required" in stderr
        if status != 0 and needs_password and not stdout:
            password = self.prompt(f"sudo password for {self.target}: ")
            stdout, stderr, status = self.runner(command(["sudo", "-S", "-p", ""]), password.encode() + b"\n" + payload,
                                                 timeout)
            if status != 0 and not stdout:
                raise ClientError("sudo did not accept the password or the account may not use sudo", 3)
        elif status != 0 and not stdout:
            raise ClientError(f"sudo failed on the server: {stderr.decode('utf-8', 'replace').strip()[:200]}", 3)
        return stdout, stderr, status

    def call(self, action: str, application_id: str, arguments: dict, *, timeout: float = 120) -> dict:
        request_id = new_request_id("admin")
        message = {"version": PROTOCOL_VERSION, "request_id": request_id, "action": action,
                   "application_id": application_id, "commit": None, "arguments": arguments}
        stdout, _stderr, _status = self._sudo([ADMIN_PATH], _frame(message), timeout)
        response = _parse(stdout)
        if response.get("status") == "incompatible":
            raise ClientError("the server helper speaks a different protocol version", 4)
        if response.get("status") != "ok":
            raise ClientError(response.get("message") or f"the server refused the request ({response.get('error')})",
                              3 if response.get("error") == "denied" else 2 if response.get("error") == "invalid_request" else 1)
        return response["result"]


_INSTALL_DIRECTORY = re.compile(r"/tmp/shiplord-install\.[A-Za-z0-9]{6,12}\Z")


def package_helper(source: Path) -> bytes:
    """A tar archive of exactly the modules the server runs, with fixed ownership and modes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in helper_modules(source):
            data = (source / name).read_bytes()
            info = tarfile.TarInfo(f"shiplord/{name}")
            info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o644, 0, 0, 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def bootstrap_server(server_target: str, source: Path, *, prompt: Callable[[str], str] = getpass.getpass,
                     runner: Runner = run_ssh) -> dict:
    """Upload the helper, run the installer as root, and remove the upload."""
    stdout, stderr, status = runner(ssh_command(server_target, "umask 077 && d=$(mktemp -d /tmp/shiplord-install.XXXXXXXX)"
                                                " && tar -x -C \"$d\" && echo \"$d\""), package_helper(source), 120)
    directory = stdout.decode("utf-8", "replace").strip()
    if status != 0 or not _INSTALL_DIRECTORY.fullmatch(directory):
        raise ClientError("could not upload the helper to the server", 3)
    try:
        admin = AdminClient(server_target, prompt, runner)
        stdout, _, _ = admin._sudo(["/usr/bin/python3", "-I", "-S", "-B", "-c",  # -B: root must not write bytecode into the upload
                                    "import sys;sys.path.insert(0,sys.argv[1]);from shiplord.installer import main;main()",
                                    directory], b"", 300)
        response = _parse(stdout)
        if response.get("status") != "ok":
            raise ClientError(f"bootstrap failed: {response.get('message', 'unknown error')}", 1)
        return response["result"]
    finally:
        runner(ssh_command(server_target, shlex.join(["rm", "-rf", "--", directory])), b"", 60)
