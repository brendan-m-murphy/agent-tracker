"""MCP-friendly tool handlers.

This module avoids a hard dependency on an MCP SDK. Hosts can expose these
methods as tool call handlers with the same input/output shapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict, cast

from agent_tracker.config import load_config
from agent_tracker.db import state_to_dict
from agent_tracker.models import Claim
from agent_tracker.service import Coordinator


class ClaimPayload(TypedDict):
    """JSON-friendly claim or heartbeat response."""

    project_id: str
    task_id: str
    lease_token: str
    lease_expires_at: str
    agent_id: str


class ReleasePayload(TypedDict):
    """JSON-friendly release response."""

    project_id: str
    task_id: str
    from_status: str
    status: str
    agent_id: str
    reason: str


class PathSummaryPayload(TypedDict, total=False):
    """Optional resolved path fields included when configured."""

    canonical_config_path: str
    task_source_path: str


class StatusPayload(PathSummaryPayload):
    """JSON-friendly project status response."""

    project_id: str
    name: str
    db_path: str
    config_path: str
    state_root: str
    task_source_root: str
    tasks: list[dict[str, Any]]
    ready: list[str]
    active: list[str]
    review: list[str]
    integration: list[str]
    blocked: list[str]


class OverviewGroupsPayload(TypedDict):
    """Grouped task dictionaries from a project overview response."""

    ready: list[dict[str, Any]]
    active: list[dict[str, Any]]
    review: list[dict[str, Any]]
    integration: list[dict[str, Any]]
    blocked: list[dict[str, Any]]
    recently_completed: list[dict[str, Any]]


class OverviewPayload(PathSummaryPayload):
    """JSON-friendly project overview response."""

    project_id: str
    name: str
    db_path: str
    config_path: str
    state_root: str
    task_source_root: str
    limit: int
    counts: dict[str, int]
    groups: OverviewGroupsPayload


class CompletionIntegrityIssuePayload(TypedDict):
    """One completed-task evidence integrity issue."""

    task_id: str
    title: str
    status: str
    kind: str
    reason: str
    evidence: list[str]
    completion_action: str
    completed_by: str
    completed_at: str
    direct_merge: bool


class CompletionIntegrityPayload(TypedDict):
    """Completion integrity diagnostic response."""

    project_id: str
    ok: bool
    issue_count: int
    issues: list[CompletionIntegrityIssuePayload]


class OkPayload(TypedDict):
    """JSON-friendly success response."""

    ok: bool


class PullSpoolFilePayload(TypedDict, total=False):
    """One file entry from a spool pull result."""

    source: str
    target: str
    existing: str
    action: str


class PullSpoolPayload(TypedDict):
    """JSON-friendly pull-spool response."""

    dry_run: bool
    remote_inbox: str
    local_inbox: str
    processed: int
    copied: int
    skipped: int
    conflicts: int
    files: list[PullSpoolFilePayload]


class IngestSpoolPayload(TypedDict):
    """JSON-friendly ingest-spool response."""

    processed: int
    inserted: int
    errors: int


class PromptPayload(TypedDict):
    """Rendered prompt response."""

    prompt: str


class WorkerPromptPayload(TypedDict):
    """Prompt-only worker launch helper response."""

    project_id: str
    task_id: str
    agent_id: str
    launch_mode: str
    launched: bool
    coordination_policy: dict[str, str]
    coordination: dict[str, Any]
    prompt: str
    task: dict[str, Any]


def _claim_payload(claim: Claim) -> ClaimPayload:
    """Return a stable JSON shape for claim-like operations."""
    return {
        "project_id": claim.project_id,
        "task_id": claim.task_id,
        "lease_token": claim.lease_token,
        "lease_expires_at": claim.lease_expires_at,
        "agent_id": claim.agent_id,
    }


class AgentTrackerTools:
    """Tool handlers for one project config."""

    def __init__(self, config_path: str | Path, db_path: str | Path | None = None):
        config = load_config(config_path)
        self.coordinator = Coordinator(config, db_path=db_path)

    def list_projects(self) -> dict[str, Any]:
        """Return the configured project."""
        config = self.coordinator.config
        return {"projects": [{"project_id": config.project_id, "name": config.name}]}

    def get_project_status(self) -> StatusPayload:
        """Return project status."""
        return self.status()

    def status(self, recover_stale_leases: bool = False) -> StatusPayload:
        """Return project status."""
        return cast(
            StatusPayload,
            self.coordinator.status_payload(recover_stale_leases=recover_stale_leases),
        )

    def overview(
        self,
        limit: int = 5,
        recover_stale_leases: bool = False,
    ) -> OverviewPayload:
        """Return grouped project overview data."""
        return cast(
            OverviewPayload,
            self.coordinator.overview_payload(
                limit=limit,
                recover_stale_leases=recover_stale_leases,
            ),
        )

    def check_completion_integrity(self) -> CompletionIntegrityPayload:
        """Return completed tasks whose evidence does not satisfy current policy."""
        return cast(CompletionIntegrityPayload, self.coordinator.completion_integrity_payload())

    def list_ready_tasks(self, repo: str = "", role: str = "", limit: int = 0) -> dict[str, Any]:
        """Return ready tasks."""
        return {
            "tasks": [
                state_to_dict(state)
                for state in self.coordinator.ready_tasks(repo=repo, role=role, limit=limit)
            ]
        }

    def claim_task(
        self,
        agent_id: str,
        task_id: str = "",
        repo: str = "",
        role: str = "",
        lease_seconds: int = 3600,
    ) -> ClaimPayload:
        """Claim a task."""
        return self.claim(
            agent_id=agent_id,
            task_id=task_id,
            repo=repo,
            role=role,
            lease_seconds=lease_seconds,
        )

    def claim(
        self,
        agent_id: str,
        task_id: str = "",
        repo: str = "",
        role: str = "",
        lease_seconds: int = 3600,
    ) -> ClaimPayload:
        """Claim a task."""
        claim = self.coordinator.claim(
            agent_id=agent_id,
            task_id=task_id,
            repo=repo,
            role=role,
            lease_seconds=lease_seconds,
        )
        return _claim_payload(claim)

    def get_task_context(self, task_id: str) -> dict[str, Any]:
        """Return task context."""
        return state_to_dict(self.coordinator.get_task(task_id))

    def render_prompt(self, task_id: str, markdown: bool = False) -> PromptPayload:
        """Render task prompt text."""
        return {"prompt": self.coordinator.render_prompt(task_id, markdown=markdown)}

    def launch_worker_prompt(
        self,
        task_id: str,
        agent_id: str = "",
        markdown: bool = True,
        branch: str = "",
        base_ref: str = "",
        worktree_path: str = "",
    ) -> WorkerPromptPayload:
        """Return task context and prompt for an external worker host.

        This helper intentionally does not spawn a worker process. Tool hosts can
        use the returned prompt and task context to launch or resume work through
        their own execution adapter while the tracker remains the queue authority.
        """
        task = self.get_task_context(task_id)
        coordination = self.coordinator.worker_coordination_context(
            task_id=task_id,
            branch=branch,
            base_ref=base_ref,
            worktree_path=worktree_path,
        )
        prompt = self.coordinator.render_worker_prompt(
            task_id,
            markdown=markdown,
            branch=branch,
            base_ref=base_ref,
            worktree_path=worktree_path,
        )
        return {
            "project_id": self.coordinator.config.project_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "launch_mode": "prompt_only",
            "launched": False,
            "coordination_policy": coordination["policy"],
            "coordination": coordination,
            "prompt": prompt,
            "task": task,
        }

    def launch_worker(
        self,
        task_id: str,
        agent_id: str = "",
        markdown: bool = True,
        branch: str = "",
        base_ref: str = "",
        worktree_path: str = "",
    ) -> WorkerPromptPayload:
        """Return the prompt-only worker payload for launch-worker tool hosts."""
        return self.launch_worker_prompt(
            task_id=task_id,
            agent_id=agent_id,
            markdown=markdown,
            branch=branch,
            base_ref=base_ref,
            worktree_path=worktree_path,
        )

    def heartbeat_task(
        self,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 3600,
        agent_id: str = "",
    ) -> ClaimPayload:
        """Extend a lease."""
        return self.heartbeat(
            task_id=task_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            agent_id=agent_id,
        )

    def heartbeat(
        self,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 3600,
        agent_id: str = "",
    ) -> ClaimPayload:
        """Extend a lease."""
        claim = self.coordinator.heartbeat(
            task_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            agent_id=agent_id,
        )
        return _claim_payload(claim)

    def complete_task(
        self,
        task_id: str,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
        direct_merge: bool = False,
    ) -> OkPayload:
        """Complete a leased task.

        Args:
            task_id: Task identifier to complete.
            lease_token: Active lease token for the task.
            evidence: Optional evidence URIs to attach to the completion.
            agent_id: Actor completing the task.
            direct_merge: Whether to apply an explicit direct-merge completion
                override.

        Returns:
            A JSON-friendly success payload.
        """
        return self.complete(
            task_id=task_id,
            lease_token=lease_token,
            evidence=evidence,
            agent_id=agent_id,
            direct_merge=direct_merge,
        )

    def complete(
        self,
        task_id: str,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
        direct_merge: bool = False,
    ) -> OkPayload:
        """Complete a leased task."""
        self.coordinator.complete(
            task_id,
            lease_token=lease_token,
            evidence=evidence or [],
            agent_id=agent_id,
            direct_merge=direct_merge,
        )
        return {"ok": True}

    def release_task(
        self,
        task_id: str,
        lease_token: str,
        reason: str,
        agent_id: str = "",
        status: str = "pending",
    ) -> ReleasePayload:
        """Release an active leased task back to the queue."""
        return self.release(
            task_id=task_id,
            lease_token=lease_token,
            reason=reason,
            agent_id=agent_id,
            status=status,
        )

    def release(
        self,
        task_id: str,
        lease_token: str,
        reason: str,
        agent_id: str = "",
        status: str = "pending",
    ) -> ReleasePayload:
        """Release an active leased task back to pending queue state."""
        return cast(
            ReleasePayload,
            self.coordinator.release(
                task_id,
                lease_token=lease_token,
                reason=reason,
                agent_id=agent_id,
                status=status,
            ),
        )

    def pull_spool(self, dry_run: bool = False) -> PullSpoolPayload:
        """Pull complete remote spool files into the local spool inbox."""
        return cast(PullSpoolPayload, self.coordinator.pull_spool(dry_run=dry_run))

    def ingest_spool(self, actor: str = "system") -> IngestSpoolPayload:
        """Ingest configured local spool event files."""
        return cast(IngestSpoolPayload, self.coordinator.ingest_spool(actor=actor))

    def submit_review_task(
        self,
        task_id: str,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> dict[str, bool]:
        """Submit a leased task for review.

        Args:
            task_id: Task identifier to transition.
            lease_token: Current active lease token for the task.
            evidence: Optional evidence URIs to attach to the handoff.
            agent_id: Optional agent ID used to validate lease ownership.

        Returns:
            A JSON-friendly success payload.
        """
        self.coordinator.submit_review(
            task_id,
            lease_token=lease_token,
            evidence=evidence or [],
            agent_id=agent_id,
        )
        return {"ok": True}

    def await_integration_task(
        self,
        task_id: str,
        lease_token: str,
        status: str = "awaiting_integration",
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> dict[str, bool]:
        """Move a leased task to an integration wait state.

        Args:
            task_id: Task identifier to transition.
            lease_token: Current active lease token for the task.
            status: Integration wait status to set.
            evidence: Optional evidence URIs to attach to the handoff.
            agent_id: Optional agent ID used to validate lease ownership.

        Returns:
            A JSON-friendly success payload.
        """
        self.coordinator.await_integration(
            task_id,
            lease_token=lease_token,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
        )
        return {"ok": True}

    def resolve_review_task(
        self,
        task_id: str,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
        direct_merge: bool = False,
    ) -> dict[str, bool]:
        """Resolve a task waiting for review.

        Args:
            task_id: Task identifier to resolve.
            status: Terminal status to set. Must be `done` or `failed`.
            evidence: Optional evidence URIs to attach to the resolution.
            agent_id: Actor resolving the review.
            reason: Failure reason when resolving as `failed`.
            direct_merge: Whether to apply an explicit direct-merge completion
                override when resolving as `done`.

        Returns:
            A JSON-friendly success payload.
        """
        self.coordinator.resolve_review(
            task_id,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
            direct_merge=direct_merge,
        )
        return {"ok": True}

    def resolve_integration_task(
        self,
        task_id: str,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
        direct_merge: bool = False,
    ) -> dict[str, bool]:
        """Resolve a task waiting for integration.

        Args:
            task_id: Task identifier to resolve.
            status: Terminal status to set. Must be `done` or `failed`.
            evidence: Optional evidence URIs to attach to the resolution.
            agent_id: Actor resolving the integration wait.
            reason: Failure reason when resolving as `failed`.
            direct_merge: Whether to apply an explicit direct-merge completion
                override when resolving as `done`.

        Returns:
            A JSON-friendly success payload.
        """
        self.coordinator.resolve_integration(
            task_id,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
            direct_merge=direct_merge,
        )
        return {"ok": True}

    def fail_task(
        self,
        task_id: str,
        lease_token: str,
        reason: str,
        agent_id: str = "",
    ) -> dict[str, bool]:
        """Fail a leased task."""
        self.coordinator.fail(task_id, lease_token=lease_token, reason=reason, agent_id=agent_id)
        return {"ok": True}

    def record_event(self, payload: dict[str, Any], actor: str = "system") -> dict[str, bool]:
        """Record an event."""
        return {"inserted": self.coordinator.record_event(payload, actor=actor)}

    def record_evidence(self, task_id: str, uri: str, actor: str = "system") -> dict[str, bool]:
        """Record evidence."""
        return {"inserted": self.coordinator.record_evidence(task_id, uri, actor=actor)}
