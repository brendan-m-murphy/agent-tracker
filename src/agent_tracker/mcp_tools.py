"""MCP-friendly tool handlers.

This module avoids a hard dependency on an MCP SDK. Hosts can expose these
methods as tool call handlers with the same input/output shapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_tracker.config import load_config
from agent_tracker.db import state_to_dict
from agent_tracker.service import Coordinator


class AgentTrackerTools:
    """Tool handlers for one project config."""

    def __init__(self, config_path: str | Path, db_path: str | Path | None = None):
        config = load_config(config_path)
        self.coordinator = Coordinator(config, db_path=db_path)

    def list_projects(self) -> dict[str, Any]:
        """Return the configured project."""
        config = self.coordinator.config
        return {"projects": [{"project_id": config.project_id, "name": config.name}]}

    def get_project_status(self) -> dict[str, Any]:
        """Return project status."""
        return self.coordinator.status_payload()

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
    ) -> dict[str, Any]:
        """Claim a task."""
        claim = self.coordinator.claim(
            agent_id=agent_id,
            task_id=task_id,
            repo=repo,
            role=role,
            lease_seconds=lease_seconds,
        )
        return claim.__dict__

    def get_task_context(self, task_id: str) -> dict[str, Any]:
        """Return task context."""
        return state_to_dict(self.coordinator.get_task(task_id))

    def render_prompt(self, task_id: str, markdown: bool = False) -> dict[str, str]:
        """Render task prompt text."""
        return {"prompt": self.coordinator.render_prompt(task_id, markdown=markdown)}

    def heartbeat_task(
        self,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 3600,
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Extend a lease."""
        claim = self.coordinator.heartbeat(
            task_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            agent_id=agent_id,
        )
        return claim.__dict__

    def complete_task(
        self,
        task_id: str,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> dict[str, bool]:
        """Complete a leased task."""
        self.coordinator.complete(
            task_id,
            lease_token=lease_token,
            evidence=evidence or [],
            agent_id=agent_id,
        )
        return {"ok": True}

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
    ) -> dict[str, bool]:
        """Resolve a task waiting for review.

        Args:
            task_id: Task identifier to resolve.
            status: Terminal status to set. Must be `done` or `failed`.
            evidence: Optional evidence URIs to attach to the resolution.
            agent_id: Actor resolving the review.
            reason: Failure reason when resolving as `failed`.

        Returns:
            A JSON-friendly success payload.
        """
        self.coordinator.resolve_review(
            task_id,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
        )
        return {"ok": True}

    def resolve_integration_task(
        self,
        task_id: str,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
    ) -> dict[str, bool]:
        """Resolve a task waiting for integration.

        Args:
            task_id: Task identifier to resolve.
            status: Terminal status to set. Must be `done` or `failed`.
            evidence: Optional evidence URIs to attach to the resolution.
            agent_id: Actor resolving the integration wait.
            reason: Failure reason when resolving as `failed`.

        Returns:
            A JSON-friendly success payload.
        """
        self.coordinator.resolve_integration(
            task_id,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
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
