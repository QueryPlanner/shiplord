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


def worst_case_seconds(candidate: dict, outgoing: dict | None = None) -> int:
    """Bound a deploy or rollback using the actual outgoing and candidate releases."""
    candidate_timeout, candidate_action = policy(candidate)
    candidate_readiness = candidate["deploy"]["readiness_timeout_seconds"]
    if outgoing is None:
        outgoing_timeout, outgoing_action, outgoing_readiness = 0, "fail", 0
    else:
        outgoing_timeout, outgoing_action = policy(outgoing)
        outgoing_readiness = outgoing["deploy"]["readiness_timeout_seconds"]
    force_overhead = (FORCE_EXIT_SECONDS if candidate_action == "force" else 0)
    force_overhead += FORCE_EXIT_SECONDS if outgoing_action == "force" and outgoing is not None else 0
    return (LOCK_WAIT_SECONDS + PULL_SECONDS + outgoing_timeout + candidate_timeout + force_overhead
            + candidate_readiness + outgoing_readiness + COMMAND_RESERVE_SECONDS)


def check_budget(candidate: dict, outgoing: dict | None = None) -> None:
    """Refuse a transaction whose actual releases cannot fit inside the worker's lifetime."""
    needed = worst_case_seconds(candidate, outgoing)
    if needed > MAX_JOB_SECONDS:
        raise ShutdownError(f"the worst-case deployment time ({needed}s) exceeds the {MAX_JOB_SECONDS}s job limit; "
                            "lower deploy.shutdown_timeout_seconds or deploy.readiness_timeout_seconds")


def check_recovery_budget(candidate: dict, target: dict | None) -> None:
    """Bound a separate recovery job using its saved candidate and reconciliation target."""
    target_configuration = target["configuration"] if target is not None else None
    candidate_configuration = candidate["configuration"]
    possible = [configuration for configuration in (candidate_configuration, target_configuration) if configuration]
    stop_timeout = max(policy(configuration)[0] for configuration in possible)
    may_force = any(policy(configuration)[1] == "force" for configuration in possible)
    target_readiness = (target_configuration["deploy"]["readiness_timeout_seconds"]
                        if target_configuration is not None else 0)
    needed = (LOCK_WAIT_SECONDS + PULL_SECONDS + stop_timeout + (FORCE_EXIT_SECONDS if may_force else 0)
              + target_readiness + COMMAND_RESERVE_SECONDS)
    if needed > MAX_JOB_SECONDS:
        raise ShutdownError(f"the worst-case recovery time ({needed}s) exceeds the {MAX_JOB_SECONDS}s job limit")
