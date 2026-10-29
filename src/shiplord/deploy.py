"""The deployment transaction: deploy, rollback, recover, and status for one application.

Every step that changes the host happens between a journal ``begin`` and a verified
``clear``. A failure restores the last committed release (or removes the failed
first release), verifies the restoration, and only then clears the journal. If the
restoration itself fails, the journal stays and the outcome says intervention is
required: nothing here ever reports success it did not verify.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .probes import ProbeFailure, Probes
from .registry import validate_registration
from .release_state import ReleaseState, StateError, validate_release
from .routing import RouteManager, RoutingError
from .runtime import Runtime, RuntimeFailure, release_id
from .shutdown import ShutdownError, check_budget, policy
from .storage import container_path

MAX_FUTURE_SECONDS = 24 * 3600  # clock skew allowance; a far-future sequence would otherwise freeze the application
SUCCESS = {"succeeded", "unchanged", "already_applied", "recovered", "nothing_to_recover"}


@dataclass(frozen=True)
class Outcome:
    status: str
    exit_code: int
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS


class _StepFailure(Exception):
    def __init__(self, message: str, phase: str, detail: str = ""):
        super().__init__(message)
        self.phase, self.detail = phase, detail


def identity(release: dict | None) -> dict | None:
    if release is None:
        return None
    artifact = release["artifact"]
    return {"commit": release["commit"], "sequence": release["sequence"], "release_id": release_id(release),
            "artifact": artifact.get("image") or artifact.get("sha256")}


class Deployer:
    """Runs transactions for one registered application. The caller holds the host lock."""

    def __init__(self, *, registration: dict, state: ReleaseState, routes: RouteManager, runtime: Runtime,
                 probes: Probes, progress: Callable[[str, dict], None] = lambda phase, info: None,
                 clock: Callable[[], float] = time.time):
        self.registration = validate_registration(registration)
        self.app = self.registration["application_id"]
        self.state, self.routes, self.runtime, self.probes = state, routes, runtime, probes
        self.progress = progress
        self.clock = clock

    # request checks -----------------------------------------------------------
    def _candidate(self, request: dict) -> dict:
        candidate = validate_release({"commit": request.get("commit"), "sequence": request.get("sequence"),
                                      "artifact": request.get("artifact"),
                                      "configuration": request.get("configuration")}, self.app)
        registration = self.registration
        if candidate["sequence"] > self.clock() + MAX_FUTURE_SECONDS:
            raise StateError("the release sequence is in the future; it would block every later release")
        requested = candidate["configuration"]["resources"]["memory_mb"]
        if requested > registration["max_memory_mb"]:
            raise StateError(f"memory_mb {requested} exceeds the {registration['max_memory_mb']} MiB limit registered for "
                             "this application; an administrator can raise it")
        if candidate["artifact"]["kind"] != registration["runtime"]:
            raise StateError("the artifact kind does not match the registered runtime")
        if candidate["configuration"]["http"]["api_domain"] != registration["api_domain"]:
            raise StateError("the hostname differs from the registration; changing it needs an administrator")
        deploy = candidate["configuration"]["deploy"]
        if "shutdown_timeout_seconds" in deploy or "shutdown_timeout_action" in deploy:
            try:  # only an explicit policy is checked: configurations that predate it keep working as they were
                check_budget(candidate["configuration"])
            except ShutdownError as error:
                raise StateError(str(error)) from None
        if "owner_uid" in candidate["configuration"].get("storage", {}):
            # The initializer that would establish ownership needs a pinned, reviewed image that does not exist yet.
            raise StateError("storage ownership (owner_uid/owner_gid) is not supported by this helper yet")
        if candidate["artifact"]["kind"] == "docker":
            if candidate["artifact"]["image"].split("@", 1)[0] != registration["image_repository"]:
                raise StateError("the image repository is not the one registered for this application")
        return candidate

    @staticmethod
    def _stale(request: dict, candidate: dict, current: dict | None) -> Outcome | None:
        if current is not None and not request.get("allow_older") and candidate["sequence"] < current["sequence"]:
            return Outcome("failed", 1, {"error": "stale_release", "current": identity(current),
                                         "message": "a newer release is already deployed; use rollback to go back"})
        return None

    def _gate(self, request_id: str) -> Outcome | None:
        """Refuse work while a transaction is unfinished; recognise repeated request IDs."""
        if self.state.read_journal() is not None:
            return Outcome("failed", 1, {"error": "recovery_required",
                                         "message": "an unfinished transaction exists; run recover first"})
        committed = self.state.read_state()
        if request_id == committed["transaction_id"]:
            return Outcome("already_applied", 0, {"current": identity(committed["current"])})
        if request_id in committed["seen_requests"]:
            return Outcome("rejected", 2, {"error": "request_id_reused"})
        return None

    # operations ---------------------------------------------------------------
    def deploy(self, request: dict) -> Outcome:
        request_id = request.get("request_id")
        try:
            candidate = self._candidate(request)
            refusal = self._gate(request_id)
            if refusal:
                return refusal
            committed = self.state.read_state()
        except StateError as error:
            return Outcome("rejected", 2, {"error": "invalid_request", "message": str(error)})
        current = committed["current"]
        stale = self._stale(request, candidate, current)
        if stale is not None:
            self._abandon(candidate, committed)  # whatever an upload left behind
            return stale
        if current == candidate and self.runtime.is_running(self.registration, current) \
                and self.probes.ready_once(self.registration, current):
            getattr(self.runtime, "forget_upload", lambda registration, release: None)(self.registration, candidate)
            return Outcome("unchanged", 0, {"current": identity(current)})
        return self._transition(request_id, candidate, committed)

    def preflight(self, request: dict) -> Outcome | None:
        """Cheap synchronous checks before a job is created; None means the request may proceed."""
        try:
            candidate = self._candidate(request)
            refusal = self._gate(request.get("request_id"))
            # A release that would be refused anyway is refused before any upload is accepted or stored.
            return refusal or self._stale(request, candidate, self.state.read_state()["current"])
        except StateError as error:
            return Outcome("rejected", 2, {"error": "invalid_request", "message": str(error)})

    def plan(self, request: dict) -> dict:
        """Report what a deploy would do, without changing anything."""
        refusal = self.preflight(request)
        if refusal is not None:
            return {"action": "none", "reason": refusal.status, **refusal.detail}
        candidate = self._candidate(request)
        current = self.state.read_state()["current"]
        route = self.routes.inspect_route(self.registration)
        base = {"candidate": identity(candidate), "current": identity(current), "route": route,
                "domain": self.registration["api_domain"], "host_port": self.registration["host_port"]}
        if current is not None and not request.get("allow_older") and candidate["sequence"] < current["sequence"]:
            return {"action": "none", "reason": "stale_release", **base}
        if current == candidate and self.runtime.is_running(self.registration, current) \
                and self.probes.ready_once(self.registration, current):
            return {"action": "unchanged", **base}
        storage = {}
        if candidate["artifact"]["kind"] == "docker":
            old, new = (container_path(current["configuration"]) if current else None), container_path(candidate["configuration"])
            storage = {"storage": {"current_path": old, "candidate_path": new, "same_volume": True,
                                   "note": "the application's single volume is mounted at the candidate path; no files are moved "
                                           "and no data is migrated" if old not in (None, new) else "no path change"}}
        shutdown = {}
        if candidate["artifact"]["kind"] == "docker":
            outgoing = policy(current["configuration"]) if current else None
            shutdown = {"shutdown": {"outgoing_policy": None if outgoing is None else {"timeout_seconds": outgoing[0], "action": outgoing[1]},
                                     "candidate_policy": dict(zip(("timeout_seconds", "action"), policy(candidate["configuration"]))),
                                     "note": "the outgoing container's own policy governs its stop; the candidate's applies "
                                             "when it is later replaced"}}
        return {"action": "deploy", "restarts_application": current is not None, **storage, **shutdown,
                "route_change": {"absent": "create", "different": "replace", "identical": "none"}[route], **base}

    def rollback(self, request_id: str) -> Outcome:
        try:
            refusal = self._gate(request_id)
            if refusal:
                return refusal
            committed = self.state.read_state()
        except StateError as error:
            return Outcome("failed", 1, {"error": "state_unreadable", "message": str(error)})
        if committed["previous"] is None:
            return Outcome("failed", 1, {"error": "no_previous_release"})
        return self._transition(request_id, committed["previous"], committed)

    def recover(self) -> Outcome:
        try:
            if self.state.read_journal() is None:
                return Outcome("nothing_to_recover", 0, {"current": identity(self.state.read_state()["current"])})
            target = self.state.reconciliation_target()
        except StateError as error:
            return Outcome("intervention_required", 6, {"error": "state_inconsistent", "message": str(error)})
        try:
            candidate = self.state.read_journal()["candidate"]
            self._reconcile(target, route=True)
            self.state.clear_after_reconciliation()
            committed = self.state.read_state()
        except (RuntimeFailure, ProbeFailure, RoutingError, _StepFailure, StateError) as failure:
            return Outcome("intervention_required", 6, {"error": "recovery_failed", **self._describe(failure)})
        # The interrupted candidate's artifact goes unless it became the committed release or another one uses it.
        warnings = self._abandon(candidate, {"current": committed["current"], "previous": committed["previous"]})
        return Outcome("recovered", 0, {"target": identity(target), "warnings": warnings})

    def status(self) -> dict:
        committed = self.state.read_state()
        journal = self.state.read_journal()
        current = committed["current"]
        return {"application_id": self.app, "generation": committed["generation"],
                "current": identity(current), "previous": identity(committed["previous"]),
                "unfinished_transaction": None if journal is None else {"request_id": journal["request_id"],
                                                                         "phase": journal["phase"]},
                "running": None if current is None else self.runtime.is_running(self.registration, current),
                "domain": self.registration["api_domain"]}

    # the transaction ---------------------------------------------------------
    def _transition(self, request_id: str, candidate: dict, before: dict) -> Outcome:
        self.progress("prepare", {})
        try:
            prepared = self.runtime.prepare(self.registration, candidate)
        except RuntimeFailure as failure:  # nothing has changed yet
            return Outcome("failed", 1, {"error": "prepare_failed", "restored": True, **self._describe(failure),
                                         "warnings": self._abandon(candidate, before)})
        try:
            self.state.begin(request_id, candidate)
        except StateError as error:
            return Outcome("failed", 1, {"error": "journal_unavailable", "message": str(error)})
        probes: dict = {}
        try:
            self._advance("runtime-changing")
            self.runtime.start(self.registration, candidate)
            probes["local_ready"] = self.probes.wait_ready(
                self.registration, candidate, alive=lambda: self.runtime.is_running(self.registration, candidate))
            self._advance("runtime-ready")
            self._advance("routing-changing")
            routed = self.routes.apply(self.registration)
            probes["route_changed"] = routed.changed
            probes["proxy_routing"] = self.probes.routing(self.registration)
            if probes["proxy_routing"] is None:
                probes["warning"] = ("Caddy's admin API could not be read, so the active routing was not verified; "
                                     "run `shiplord doctor --server <alias> --deep`")
            if probes["proxy_routing"] is False:
                raise _StepFailure("the active Caddy configuration does not route this hostname to the application",
                                   "routing")
            self._advance("routing-ready")
        except (RuntimeFailure, ProbeFailure, RoutingError, _StepFailure, StateError) as failure:
            if isinstance(failure, RuntimeFailure) and failure.intervention_required:
                # The outgoing process was signalled and may still be draining: restoring would restart or remove it.
                return self._intervention(failure, probes)
            return self._restore(failure, probes, candidate, before)
        try:
            self.state.record_commit()
            self.state.clear_after_reconciliation()
        except StateError as error:
            return Outcome("intervention_required", 6, {"error": "commit_unconfirmed", "message": str(error),
                                                       "release": identity(candidate)})
        probes["https"] = self.probes.https(self.registration, candidate)
        probes["dns"] = self.probes.dns(self.registration)
        warnings = self._retire(before, candidate)
        return Outcome("succeeded", 0, {"release": identity(candidate), "previous": identity(before["current"]),
                                        "image": prepared, "probes": probes, "warnings": warnings, **self._shutdown_report()})

    def _shutdown_report(self) -> dict:
        report = getattr(self.runtime, "stop_report", None)
        return {"shutdown": list(report)} if report else {}

    def _intervention(self, failure: Exception, probes: dict) -> Outcome:
        """Retain the journal: the stop attempt cannot be retracted, so only a verified recovery may end it."""
        return Outcome("intervention_required", 6, {"error": "intervention_required", **self._describe(failure),
                                                     "probes": probes, **self._shutdown_report(),
                                                     "recovery": "run `shiplord recover` once the application has exited"})

    def _advance(self, phase: str) -> None:
        self.progress(phase, {})
        self.state.advance(phase)

    def _retire(self, before: dict, candidate: dict) -> list[str]:
        """After success, drop the image of the release that left the two-release retention window."""
        dropped = before["previous"]
        in_use = [candidate["artifact"], (before["current"] or {}).get("artifact")]
        if dropped is None or dropped["artifact"] in in_use:  # never remove an image a retained release uses
            return []
        try:
            return self.runtime.discard(self.registration, dropped)
        except RuntimeFailure as failure:
            return [str(failure)]

    def _reconcile(self, target: dict | None, *, route: bool) -> None:
        """Make the runtime and routing match ``target`` (None means no release at all)."""
        if target is None:
            self.runtime.stop(self.registration)
            if route:
                self.routes.reconcile(self.app, None)
            return
        if getattr(self.runtime, "stop_in_progress", lambda registration: False)(self.registration):
            # A signalled process is still draining. Declaring the journal reconciled (or recreating the container) would
            # hide or repeat the stop attempt, so this stays unresolved until it has exited.
            raise RuntimeFailure("a shutdown is still in progress for the application", "stop", intervention_required=True,
                                 kind="shutdown_in_progress")
        if not (self.runtime.is_running(self.registration, target) and self.probes.ready_once(self.registration, target)):
            self.runtime.start(self.registration, target)
            self.probes.wait_ready(self.registration, target,
                                   alive=lambda: self.runtime.is_running(self.registration, target))
        if route:
            self.routes.reconcile(self.app, self.registration)

    def _abandon(self, candidate: dict, before: dict) -> list[str]:
        """Remove what a failed candidate left on the host, unless a retained release uses the same artifact."""
        retained = [release["artifact"] for release in (before["current"], before["previous"]) if release]
        if candidate["artifact"] in retained:
            return []
        try:
            return self.runtime.discard(self.registration, candidate)
        except RuntimeFailure as failure:
            return [str(failure)]

    def _restore(self, failure: Exception, probes: dict, candidate: dict, before: dict) -> Outcome:
        """Return to the last committed release; clear the journal only after verifying that."""
        failed = {"error": "deployment_failed", **self._describe(failure), "probes": probes}
        try:
            journal = self.state.read_journal()
            target = self.state.reconciliation_target()
            routed = journal is not None and journal["phase"] in ("routing-changing", "routing-ready")
            self._reconcile(target, route=routed)
            self.state.clear_after_reconciliation()
        except (RuntimeFailure, ProbeFailure, RoutingError, _StepFailure, StateError) as restore_failure:
            failed["restore_error"] = self._describe(restore_failure)
            return Outcome("intervention_required", 6, failed)
        failed["restored_release"] = identity(target)
        failed["warnings"] = self._abandon(candidate, before)  # only after a verified restore: never on uncertainty
        if target is None:
            return Outcome("failed", 1, failed)
        return Outcome("rolled_back", 5, failed)

    @staticmethod
    def _describe(failure: Exception) -> dict:
        info = {"message": str(failure)}
        for attribute in ("phase", "kind"):
            if getattr(failure, attribute, None):
                info[attribute] = getattr(failure, attribute)
        if getattr(failure, "detail", ""):
            info["detail"] = failure.detail
        if getattr(failure, "intervention_required", False):
            info["intervention_required"] = True
        return info
