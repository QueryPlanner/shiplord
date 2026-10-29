"""Shutdown policy of a Docker release: how long its process may drain, and what happens at the deadline.

Stdlib-only (the server imports it). Defaults are applied when interpreting configuration, never written into
stored records, so legacy records keep their exact shape.
"""

from __future__ import annotations

FEATURE = "docker_shutdown_v1"  # capability a helper advertises when it can honour the shutdown policy
DEFAULT_TIMEOUT_SECONDS = 10  # what every container had before the policy existed (`--stop-timeout 10`)
DEFAULT_ACTION = "force"
MAX_TIMEOUT_SECONDS = 300
ACTIONS = ("force", "fail")
FORCE_EXIT_SECONDS = 10  # bound on observing exit after an explicit SIGKILL
MAX_JOB_SECONDS = 3600  # jobs.MAX_RUNTIME_SECONDS; the worker is stopped after this
PULL_SECONDS = 900  # runtime.PULL_TIMEOUT_SECONDS
LOCK_WAIT_SECONDS = 900  # services.WORKER_LOCK_TIMEOUT_SECONDS
COMMAND_RESERVE_SECONDS = 120  # signalling, inspection, removal and cleanup calls


class ShutdownError(ValueError):
    """A shutdown setting cannot be used safely."""


def validate_timeout(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TIMEOUT_SECONDS:
        raise ShutdownError(f"deploy.shutdown_timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}")
    return value


def validate_action(value: object) -> str:
    if value not in ACTIONS:
        raise ShutdownError("deploy.shutdown_timeout_action must be force or fail")
    return value


def policy(configuration: dict) -> tuple[int, str]:
    """The effective (timeout, action) of a release configuration."""
    deploy = configuration["deploy"]
    return (deploy.get("shutdown_timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            deploy.get("shutdown_timeout_action", DEFAULT_ACTION))


def worst_case_seconds(candidate: dict, outgoing_timeout: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    """Longest one transaction can run: lock wait, pull, two stops, two readiness waits, and command overhead.

    The outgoing release's stop is bounded by its own recorded timeout; the candidate's stop (during restoration)
    by the candidate's. A forced stop adds FORCE_EXIT_SECONDS to each.
    """
    own, _ = policy(candidate)
    readiness = candidate["deploy"]["readiness_timeout_seconds"]
    return (LOCK_WAIT_SECONDS + PULL_SECONDS + outgoing_timeout + own + 2 * FORCE_EXIT_SECONDS
            + 2 * readiness + COMMAND_RESERVE_SECONDS)


def check_budget(candidate: dict) -> None:
    """Refuse an explicit policy whose worst case cannot finish inside the worker's lifetime."""
    needed = worst_case_seconds(candidate, max(policy(candidate)[0], DEFAULT_TIMEOUT_SECONDS))
    if needed > MAX_JOB_SECONDS:
        raise ShutdownError(f"the worst-case deployment time ({needed}s) exceeds the {MAX_JOB_SECONDS}s job limit; "
                            "lower deploy.shutdown_timeout_seconds or deploy.readiness_timeout_seconds")
