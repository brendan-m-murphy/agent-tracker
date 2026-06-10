"""High-level coordinator service."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from agent_tracker.config import ProjectConfig
from agent_tracker.db import (
    Store,
    intake_to_dict,
    intervention_to_dict,
    notebook_to_dict,
    notification_delivery_to_dict,
    proposed_task_to_dict,
    state_to_dict,
)
from agent_tracker.exporters import PreparedPrNotificationExporter
from agent_tracker.models import (
    ACTIVE_STATES,
    INTAKE_STATES,
    INTEGRATION_STATES,
    INTERVENTION_REASONS,
    INTERVENTION_STATES,
    PROPOSAL_STATES,
    REVIEW_STATES,
    Claim,
    EventRecord,
    IntakeRecord,
    InterventionRecord,
    NotebookRecord,
    NotificationDeliveryRecord,
    ProposedTaskRecord,
    RequirementState,
    TaskRecord,
    TaskState,
)
from agent_tracker.plugins import load_plugin
from agent_tracker.rendering import DefaultPromptRenderer

_SSH_SPOOL_SCHEMES = {"ssh", "sftp"}
_DISABLED_KNOWN_HOSTS = {"none", "off", "false", "disabled"}
STRUCTURED_INTAKE_KINDS = ("idea", "feature", "check", "concern", "note")
TASK_INGEST_SCHEMA_VERSION = 1


class _GitBranchCheck(TypedDict):
    ok: bool
    branch: str
    detail: str


class Coordinator:
    """Coordinate tasks for one configured project."""

    def __init__(self, config: ProjectConfig, db_path: str | Path | None = None):
        self.config = config
        self.store = Store(db_path or config.db_path)

    def init(self) -> None:
        """Initialize storage for the configured project."""
        self._ensure_mutation_allowed()
        self.store.upsert_project(self.config)

    def import_tasks(self, *, reconcile_runtime_state: bool = False) -> int:
        """Import configured tasks through the configured importer."""
        self._ensure_mutation_allowed()
        importer = load_plugin(self.config, "importer", "agent_tracker.importers:JsonTaskImporter")
        if importer is None:
            raise RuntimeError("task importer is not configured")
        tasks, dependencies = importer.load_tasks(self.config)
        self.store.import_tasks(
            self.config,
            tasks,
            dependencies,
            reconcile_runtime_state=reconcile_runtime_state,
        )
        return len(tasks)

    def task_states(self, *, recover_stale_leases: bool = False) -> list[TaskState]:
        """Return all evaluated task states."""
        return self.store.task_states(
            self.config.project_id,
            recover_stale_leases=recover_stale_leases,
        )

    def ready_tasks(
        self,
        *,
        limit: int = 0,
        repo: str = "",
        role: str = "",
        recover_stale_leases: bool = False,
    ) -> list[TaskState]:
        """Return ready tasks matching optional filters."""
        if limit < 0:
            raise ValueError("limit must be greater than or equal to zero")
        states = [
            state
            for state in self.task_states(recover_stale_leases=recover_stale_leases)
            if state.state == "ready"
        ]
        if repo:
            states = [state for state in states if state.task.repo == repo]
        if role:
            states = [state for state in states if _role_matches(state, role)]
        return states[:limit] if limit else states

    def get_task(self, task_id: str, *, recover_stale_leases: bool = False) -> TaskState:
        """Return one evaluated task state."""
        return self.store.get_task_state(
            self.config.project_id,
            task_id,
            recover_stale_leases=recover_stale_leases,
        )

    def claim(
        self,
        *,
        agent_id: str,
        task_id: str = "",
        repo: str = "",
        role: str = "",
        lease_seconds: int = 3600,
    ) -> Claim:
        """Claim a ready task."""
        self._ensure_mutation_allowed()
        return self.store.claim_task(
            self.config.project_id,
            agent_id=agent_id,
            task_id=task_id,
            repo=repo,
            role=role,
            lease_seconds=lease_seconds,
        )

    def heartbeat(
        self,
        task_id: str,
        *,
        lease_token: str,
        lease_seconds: int = 3600,
        agent_id: str = "",
    ) -> Claim:
        """Extend a task lease."""
        self._ensure_mutation_allowed()
        return self.store.heartbeat(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            agent_id=agent_id,
        )

    def complete(
        self,
        task_id: str,
        *,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
        direct_merge: bool = False,
    ) -> None:
        """Complete a leased task.

        Args:
            task_id: Task identifier to complete.
            lease_token: Active lease token for the task.
            evidence: Optional evidence URIs for the completion.
            agent_id: Actor completing the task.
            direct_merge: Whether to apply an explicit direct-merge completion
                override.

        Raises:
            ValueError: If mutation is refused, the lease is invalid, or the
                completion evidence does not satisfy task policy.
            KeyError: If the task does not exist.
        """
        self._ensure_mutation_allowed()
        self.store.complete_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            evidence=evidence or [],
            agent_id=agent_id,
            direct_merge=direct_merge,
        )

    def fail(self, task_id: str, *, lease_token: str, reason: str, agent_id: str = "") -> None:
        """Fail a leased task."""
        self._ensure_mutation_allowed()
        self.store.fail_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            reason=reason,
            agent_id=agent_id,
        )

    def release(
        self,
        task_id: str,
        *,
        lease_token: str,
        reason: str,
        agent_id: str = "",
        status: str = "pending",
    ) -> dict[str, str]:
        """Release an active leased task back to the queue.

        Args:
            task_id: Task identifier to release.
            lease_token: Current active lease token for the task.
            reason: Non-empty audit reason for returning the work to the queue.
            agent_id: Optional agent ID used to validate lease ownership.
            status: Target queue status. Only `pending` is currently supported.

        Raises:
            ValueError: If mutation is refused, the release reason is empty, the
                lease is invalid, or the requested target status is unsupported.
            KeyError: If the task does not exist.
        """
        self._ensure_mutation_allowed()
        return self.store.release_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            reason=reason,
            agent_id=agent_id,
            status=status,
        )

    def submit_review(
        self,
        task_id: str,
        *,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> None:
        """Submit a leased task for review without marking it done.

        Args:
            task_id: Task identifier to transition.
            lease_token: Current active lease token for the task.
            evidence: Optional evidence URIs for the review submission.
            agent_id: Optional agent ID used to validate lease ownership.

        Raises:
            ValueError: If mutation is refused or the lease is invalid.
            KeyError: If the task does not exist.
        """
        self._ensure_mutation_allowed()
        self.store.submit_review_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            evidence=evidence or [],
            agent_id=agent_id,
        )

    def await_integration(
        self,
        task_id: str,
        *,
        lease_token: str,
        status: str = "awaiting_integration",
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> None:
        """Move a leased task to an integration wait state.

        Args:
            task_id: Task identifier to transition.
            lease_token: Current active lease token for the task.
            status: Integration status to set. Must be `awaiting_pr`,
                `awaiting_merge`, or `awaiting_integration`.
            evidence: Optional evidence URIs for the integration handoff.
            agent_id: Optional agent ID used to validate lease ownership.

        Raises:
            ValueError: If mutation is refused, the lease is invalid, or the
                requested status is not an integration wait state.
            KeyError: If the task does not exist.
        """
        self._ensure_mutation_allowed()
        self.store.await_integration_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
        )

    def resolve_review(
        self,
        task_id: str,
        *,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
        direct_merge: bool = False,
    ) -> None:
        """Resolve a task waiting for review.

        Args:
            task_id: Task identifier to resolve.
            status: Terminal status to set. Must be `done` or `failed`.
            evidence: Optional evidence URIs for the resolution.
            agent_id: Actor resolving the review.
            reason: Failure reason when resolving as `failed`.
            direct_merge: Whether to apply an explicit direct-merge completion
                override when resolving as `done`.

        Raises:
            ValueError: If mutation is refused, the task is not awaiting review,
                or the resolution status is invalid.
            KeyError: If the task does not exist.
        """
        self._ensure_mutation_allowed()
        if not agent_id.strip():
            raise ValueError("agent is required when resolving review state")
        self.store.resolve_review_task(
            self.config.project_id,
            task_id,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
            direct_merge=direct_merge,
        )

    def resolve_integration(
        self,
        task_id: str,
        *,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
        direct_merge: bool = False,
    ) -> None:
        """Resolve a task waiting for integration.

        Args:
            task_id: Task identifier to resolve.
            status: Terminal status to set. Must be `done` or `failed`.
            evidence: Optional evidence URIs for the resolution.
            agent_id: Actor resolving the integration wait.
            reason: Failure reason when resolving as `failed`.
            direct_merge: Whether to apply an explicit direct-merge completion
                override when resolving as `done`.

        Raises:
            ValueError: If mutation is refused, the task is not awaiting
                integration, or the resolution status is invalid.
            KeyError: If the task does not exist.
        """
        self._ensure_mutation_allowed()
        if not agent_id.strip():
            raise ValueError("agent is required when resolving integration state")
        self.store.resolve_integration_task(
            self.config.project_id,
            task_id,
            status=status,
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
            direct_merge=direct_merge,
        )

    def record_evidence(self, task_id: str, uri: str, *, actor: str = "system") -> bool:
        """Record evidence for a task."""
        self._ensure_mutation_allowed()
        return self.store.record_evidence(self.config.project_id, task_id, uri, actor=actor)

    def completion_integrity_payload(self) -> dict[str, Any]:
        """Return a deterministic diagnostic for completed task evidence."""
        issues = self.store.completion_integrity_issues(self.config.project_id)
        issues.extend(_completion_file_evidence_git_issues(self.config, self.task_states()))
        return {
            "project_id": self.config.project_id,
            "ok": not issues,
            "issue_count": len(issues),
            "issues": issues,
        }

    def record_event(self, payload: dict[str, Any], *, actor: str = "system") -> bool:
        """Normalize and record an event."""
        self._ensure_mutation_allowed()
        if not isinstance(payload, dict):
            raise ValueError("event payload must be a JSON object")
        adapter = load_plugin(self.config, "event_adapter")
        if adapter is None:
            event = _default_event_record(payload)
        else:
            event = adapter.normalize_event(self.config, payload)
        event_id = event.event_id.strip()
        if not event_id:
            raise ValueError("event payload must include event_id, id, run_id, or job_id")
        kind = event.kind.strip() or "event"
        task_id = event.task_id.strip()
        event = EventRecord(event_id=event_id, kind=kind, task_id=task_id, payload=event.payload)
        return self.store.record_event(self.config.project_id, event, actor=actor)

    def record_intake(
        self,
        text: str,
        *,
        kind: str = "idea",
        source: str = "",
        repo: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        intake_id: str = "",
        actor: str = "system",
    ) -> IntakeRecord:
        """Record raw project intake without creating a task."""
        self._ensure_mutation_allowed()
        cleaned_text = text.strip()
        if not cleaned_text:
            raise ValueError("intake text is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("intake metadata must be a JSON object")
        record = IntakeRecord(
            intake_id=_first_text(intake_id) or uuid.uuid4().hex,
            text=cleaned_text,
            kind=_first_text(kind) or "idea",
            source=_first_text(source),
            repo=_first_text(repo),
            tags=_clean_tags(tags or []),
            metadata=metadata or {},
        )
        return self.store.record_intake(self.config.project_id, record, actor=actor)

    def record_structured_intake(
        self,
        text: str,
        *,
        kind: str,
        source: str,
        repo: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        intake_id: str = "",
        actor: str = "system",
    ) -> IntakeRecord:
        """Record raw intake through the guided helper path."""
        self._ensure_mutation_allowed()
        raw_text = str(text)
        if not raw_text.strip():
            raise ValueError("intake text is required")
        cleaned_kind = _first_text(kind)
        cleaned_source = _first_text(source)
        cleaned_repo = _first_text(repo)
        if not cleaned_kind:
            raise ValueError("intake kind is required")
        if cleaned_kind not in STRUCTURED_INTAKE_KINDS:
            allowed = ", ".join(STRUCTURED_INTAKE_KINDS)
            raise ValueError(f"intake kind must be one of: {allowed}")
        if not cleaned_source:
            raise ValueError("intake source is required")
        if not cleaned_repo:
            raise ValueError("intake repo is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("intake metadata must be a JSON object")
        record = IntakeRecord(
            intake_id=_first_text(intake_id) or uuid.uuid4().hex,
            text=raw_text,
            kind=cleaned_kind,
            source=cleaned_source,
            repo=cleaned_repo,
            tags=_clean_tags(tags or []),
            metadata=metadata or {},
        )
        return self.store.record_intake(self.config.project_id, record, actor=actor)

    def intake_records(
        self,
        *,
        status: str = "",
        kind: str = "",
        repo: str = "",
        limit: int = 0,
    ) -> list[IntakeRecord]:
        """Return raw project intake records."""
        if limit < 0:
            raise ValueError("limit must be greater than or equal to zero")
        return self.store.intake_records(
            self.config.project_id,
            status=_first_text(status),
            kind=_first_text(kind),
            repo=_first_text(repo),
            limit=limit,
        )

    def update_intake_status(
        self,
        intake_id: str,
        *,
        status: str,
        actor: str = "system",
    ) -> IntakeRecord:
        """Update the triage status for a raw intake record."""
        self._ensure_mutation_allowed()
        cleaned_intake_id = _first_text(intake_id)
        cleaned_status = _first_text(status)
        if not cleaned_intake_id:
            raise ValueError("intake id is required")
        if cleaned_status not in INTAKE_STATES:
            raise ValueError(f"invalid intake status: {cleaned_status}")
        return self.store.update_intake_status(
            self.config.project_id,
            cleaned_intake_id,
            cleaned_status,
            actor=actor,
        )

    def intake_payload(
        self,
        *,
        status: str = "",
        kind: str = "",
        repo: str = "",
        limit: int = 0,
    ) -> dict[str, Any]:
        """Return JSON-friendly intake records."""
        records = self.intake_records(status=status, kind=kind, repo=repo, limit=limit)
        return {
            "project_id": self.config.project_id,
            "intake": [intake_to_dict(record) for record in records],
        }

    def propose_task_from_intake(
        self,
        intake_id: str,
        *,
        task_id: str,
        title: str,
        repo: str = "",
        summary: str = "",
        next_action: str = "",
        role: str = "",
        write_scopes: list[str] | None = None,
        validation_checks: list[str] | None = None,
        requirements: list[dict[str, str]] | None = None,
        authority: str = "",
        intervention_needs: list[str] | None = None,
        notebook_paths: list[str] | None = None,
        notebook_updates: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        proposal_id: str = "",
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Create a proposed task contract from raw intake."""
        self._ensure_mutation_allowed()
        cleaned_intake_id = _first_text(intake_id)
        cleaned_task_id = _first_text(task_id)
        cleaned_title = _first_text(title)
        if not cleaned_intake_id:
            raise ValueError("intake id is required")
        if not cleaned_task_id:
            raise ValueError("proposed task id is required")
        if not cleaned_title:
            raise ValueError("proposed task title is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("proposal metadata must be a JSON object")
        scopes = _clean_strings(write_scopes or [])
        task_metadata = dict(metadata or {})
        if role:
            task_metadata["roles"] = _clean_strings([role])
        if scopes:
            task_metadata["write_scopes"] = scopes
        if authority:
            task_metadata["authority"] = authority.strip()
        needs = _clean_strings(intervention_needs or [])
        if needs:
            task_metadata["intervention_needs"] = needs
        notebook_references = _clean_notebook_paths(notebook_paths or [])
        if notebook_references:
            task_metadata["notebook_paths"] = notebook_references
        notebooks = _clean_strings(notebook_updates or [])
        if notebooks:
            task_metadata["notebook_updates"] = notebooks
        task = TaskRecord(
            task_id=cleaned_task_id,
            title=cleaned_title,
            repo=_first_text(repo),
            status="pending",
            summary=_first_text(summary),
            execution={"primary_files": scopes} if scopes else {},
            validation_checks=_clean_strings(validation_checks or []),
            next_action=_first_text(next_action),
            metadata=task_metadata,
        )
        proposal = ProposedTaskRecord(
            proposal_id=_first_text(proposal_id) or uuid.uuid4().hex,
            intake_id=cleaned_intake_id,
            task=task,
            requirements=_clean_requirements(requirements or []),
        )
        return self.store.record_proposed_task(
            self.config.project_id,
            proposal,
            actor=actor,
        )

    def plan_task_from_text(
        self,
        text: str,
        *,
        task_id: str,
        title: str,
        kind: str = "idea",
        source: str = "agent-tracker plan task",
        intake_repo: str = "",
        tags: list[str] | None = None,
        intake_metadata: dict[str, Any] | None = None,
        repo: str = "",
        summary: str = "",
        next_action: str = "",
        role: str = "",
        write_scopes: list[str] | None = None,
        validation_checks: list[str] | None = None,
        requirements: list[dict[str, str]] | None = None,
        authority: str = "",
        intervention_needs: list[str] | None = None,
        notebook_paths: list[str] | None = None,
        notebook_updates: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        proposal_id: str = "",
        intake_id: str = "",
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Record planning intake and create its proposed task contract.

        Args:
            text: Raw planning request or note to preserve as intake.
            task_id: Stable task identifier for the proposed task.
            title: Human-readable proposed task title.
            kind: Intake kind, such as `idea`, `feature`, or `check`.
            source: Origin of the planning request.
            intake_repo: Repository or component for the intake record. When
                omitted, `repo` is reused.
            tags: Optional intake triage tags.
            intake_metadata: Optional structured intake metadata.
            repo: Repository or component for the proposed task.
            summary: Proposed task summary.
            next_action: Immediate proposed task next action.
            role: Optional role filter for the proposed task.
            write_scopes: Optional files or directories expected to change.
            validation_checks: Optional checks needed before completion.
            requirements: Optional task dependency records.
            authority: Optional authority note stored in task metadata.
            intervention_needs: Optional human-intervention notes.
            notebook_paths: Optional config-relative notebook paths to include
                when rendering the task prompt.
            notebook_updates: Optional notebook update notes.
            metadata: Optional proposed task metadata.
            proposal_id: Optional stable proposal identifier.
            intake_id: Optional stable intake identifier.
            actor: Actor recorded in audit history.

        Returns:
            The durable proposed task contract created from the new intake.

        Raises:
            ValueError: If intake text, proposed task fields, or metadata are
                invalid, or if the proposed task collides with existing state.
        """
        self._ensure_mutation_allowed()
        cleaned_text = text.strip()
        cleaned_task_id = _first_text(task_id)
        cleaned_title = _first_text(title)
        if not cleaned_text:
            raise ValueError("intake text is required")
        if not cleaned_task_id:
            raise ValueError("proposed task id is required")
        if not cleaned_title:
            raise ValueError("proposed task title is required")
        if intake_metadata is not None and not isinstance(intake_metadata, dict):
            raise ValueError("intake metadata must be a JSON object")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("proposal metadata must be a JSON object")

        scopes = _clean_strings(write_scopes or [])
        task_metadata = dict(metadata or {})
        if role:
            task_metadata["roles"] = _clean_strings([role])
        if scopes:
            task_metadata["write_scopes"] = scopes
        if authority:
            task_metadata["authority"] = authority.strip()
        needs = _clean_strings(intervention_needs or [])
        if needs:
            task_metadata["intervention_needs"] = needs
        notebook_references = _clean_notebook_paths(notebook_paths or [])
        if notebook_references:
            task_metadata["notebook_paths"] = notebook_references
        notebooks = _clean_strings(notebook_updates or [])
        if notebooks:
            task_metadata["notebook_updates"] = notebooks
        cleaned_repo = _first_text(repo)
        intake = IntakeRecord(
            intake_id=_first_text(intake_id) or uuid.uuid4().hex,
            text=cleaned_text,
            kind=_first_text(kind) or "idea",
            source=_first_text(source),
            repo=_first_text(intake_repo) or cleaned_repo,
            tags=_clean_tags(tags or []),
            metadata=intake_metadata or {},
        )
        task = TaskRecord(
            task_id=cleaned_task_id,
            title=cleaned_title,
            repo=cleaned_repo,
            status="pending",
            summary=_first_text(summary),
            execution={"primary_files": scopes} if scopes else {},
            validation_checks=_clean_strings(validation_checks or []),
            next_action=_first_text(next_action),
            metadata=task_metadata,
        )
        proposal = ProposedTaskRecord(
            proposal_id=_first_text(proposal_id) or uuid.uuid4().hex,
            intake_id=intake.intake_id,
            task=task,
            requirements=_clean_requirements(requirements or []),
        )
        return self.store.record_planned_task(
            self.config.project_id,
            intake,
            proposal,
            actor=actor,
        )

    def proposed_task_records(
        self,
        *,
        status: str = "",
        intake_id: str = "",
        limit: int = 0,
    ) -> list[ProposedTaskRecord]:
        """Return proposed task contracts."""
        if limit < 0:
            raise ValueError("limit must be greater than or equal to zero")
        return self.store.proposed_task_records(
            self.config.project_id,
            status=_first_text(status),
            intake_id=_first_text(intake_id),
            limit=limit,
        )

    def update_proposed_task(
        self,
        proposal_id: str,
        *,
        task_id: str | None = None,
        title: str | None = None,
        repo: str | None = None,
        summary: str | None = None,
        next_action: str | None = None,
        role: str | None = None,
        write_scopes: list[str] | None = None,
        validation_checks: list[str] | None = None,
        requirements: list[dict[str, str]] | None = None,
        authority: str | None = None,
        notebook_paths: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Update a proposed task contract before promotion."""
        self._ensure_mutation_allowed()
        cleaned_proposal_id = _first_text(proposal_id)
        if not cleaned_proposal_id:
            raise ValueError("proposal id is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("proposal metadata must be a JSON object")
        current = self.store.proposed_task_record(self.config.project_id, cleaned_proposal_id)
        if current is None:
            raise ValueError(f"unknown proposed task: {cleaned_proposal_id}")

        cleaned_task_id = current.task.task_id if task_id is None else _first_text(task_id)
        cleaned_title = current.task.title if title is None else _first_text(title)
        if not cleaned_task_id:
            raise ValueError("proposed task id is required")
        if not cleaned_title:
            raise ValueError("proposed task title is required")

        task_metadata = dict(current.task.metadata if metadata is None else metadata)
        execution = dict(current.task.execution)
        if role is not None:
            roles = _clean_strings([role])
            if roles:
                task_metadata["roles"] = roles
            else:
                task_metadata.pop("roles", None)
        if write_scopes is not None:
            scopes = _clean_strings(write_scopes)
            if scopes:
                task_metadata["write_scopes"] = scopes
                execution["primary_files"] = scopes
            else:
                task_metadata.pop("write_scopes", None)
                execution.pop("primary_files", None)
        if authority is not None:
            cleaned_authority = _first_text(authority)
            if cleaned_authority:
                task_metadata["authority"] = cleaned_authority
            else:
                task_metadata.pop("authority", None)
        if notebook_paths is not None:
            notebook_references = _clean_notebook_paths(notebook_paths)
            if notebook_references:
                task_metadata["notebook_paths"] = notebook_references
            else:
                task_metadata.pop("notebook_paths", None)

        task = replace(
            current.task,
            task_id=cleaned_task_id,
            title=cleaned_title,
            repo=current.task.repo if repo is None else _first_text(repo),
            summary=current.task.summary if summary is None else _first_text(summary),
            next_action=(
                current.task.next_action if next_action is None else _first_text(next_action)
            ),
            execution=execution,
            validation_checks=(
                list(current.task.validation_checks)
                if validation_checks is None
                else _clean_strings(validation_checks)
            ),
            metadata=task_metadata,
        )
        proposal = replace(
            current,
            task=task,
            requirements=(
                list(current.requirements)
                if requirements is None
                else _clean_requirements(requirements)
            ),
        )
        return self.store.update_proposed_task(
            self.config.project_id,
            cleaned_proposal_id,
            proposal,
            actor=actor,
        )

    def notebook_records(self) -> list[NotebookRecord]:
        """Return discovered project and repository notebooks.

        Notebook discovery is file-backed and scoped to `notebooks/` below the
        configured task source root. Returned paths are config-relative when
        possible, which keeps them copy-paste ready for `--notebook-path` and
        the default prompt renderer.

        Returns:
            Existing conventional notebook files, sorted with the project
            notebook before repo notebooks.
        """
        notebooks_root = self.config.effective_task_source_root / "notebooks"
        candidates: list[tuple[str, str, Path]] = [
            ("project", "project", notebooks_root / "project.md")
        ]
        repos_root = notebooks_root / "repos"
        if repos_root.exists():
            candidates.extend(
                ("repo", path.stem, path)
                for path in sorted(repos_root.glob("*.md"))
                if path.is_file()
            )
        records = [
            _notebook_record(self.config, kind=kind, name=name, path=path)
            for kind, name, path in candidates
            if path.exists()
        ]
        return sorted(records, key=lambda record: (record.kind != "project", record.path))

    def notebook_payload(self) -> dict[str, Any]:
        """Return JSON-friendly discovered notebook records.

        Returns:
            A payload containing the project id and discovered notebook records.
        """
        return {
            "project_id": self.config.project_id,
            "notebooks": [notebook_to_dict(record) for record in self.notebook_records()],
        }

    def read_notebook(self, notebook_path: str) -> str:
        """Read one notebook by safe relative path.

        Args:
            notebook_path: Path below `notebooks/`, for example
                `notebooks/project.md`.

        Returns:
            UTF-8 notebook text.

        Raises:
            ValueError: If the path is absolute, home-relative, escapes the
                notebook root, or points at a directory.
            FileNotFoundError: If the notebook file does not exist.
        """
        path = _resolve_notebook_file(self.config, notebook_path, must_exist=True)
        return path.read_text(encoding="utf-8")

    def append_notebook(
        self,
        notebook_path: str,
        text: str,
        *,
        actor: str = "system",
    ) -> NotebookRecord:
        """Append a short durable note to a notebook.

        Args:
            notebook_path: Path below `notebooks/`, for example
                `notebooks/repos/agent-tracker.md`.
            text: Markdown note body to append.
            actor: Actor written in the appended entry heading.

        Returns:
            The updated notebook record.

        Raises:
            ValueError: If mutation is refused, the notebook path is unsafe, or
                the note text is blank.
        """
        self._ensure_mutation_allowed()
        cleaned_text = text.strip()
        if not cleaned_text:
            raise ValueError("notebook note text is required")
        path = _resolve_notebook_file(self.config, notebook_path, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "\n" if existing.strip() else ""
        timestamp = datetime.now(timezone.utc).date().isoformat()
        cleaned_actor = _first_text(actor) or "system"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}\n## {timestamp} - {cleaned_actor}\n\n{cleaned_text}\n")
        return _notebook_record(
            self.config,
            kind=_notebook_kind_from_path(path),
            name=path.stem,
            path=path,
        )

    def promote_proposed_task(
        self,
        proposal_id: str,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Promote a proposed task into live queue state."""
        self._ensure_mutation_allowed()
        cleaned_proposal_id = _first_text(proposal_id)
        if not cleaned_proposal_id:
            raise ValueError("proposal id is required")
        proposal = self.store.promote_proposed_task(
            self.config.project_id,
            cleaned_proposal_id,
            actor=actor,
        )
        if proposal.status not in PROPOSAL_STATES:
            raise ValueError(f"invalid proposal status: {proposal.status}")
        return proposal

    def withdraw_proposed_task(
        self,
        proposal_id: str,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Withdraw a proposed task contract before promotion."""
        self._ensure_mutation_allowed()
        cleaned_proposal_id = _first_text(proposal_id)
        if not cleaned_proposal_id:
            raise ValueError("proposal id is required")
        proposal = self.store.withdraw_proposed_task(
            self.config.project_id,
            cleaned_proposal_id,
            actor=actor,
        )
        if proposal.status not in PROPOSAL_STATES:
            raise ValueError(f"invalid proposal status: {proposal.status}")
        return proposal

    def proposed_tasks_payload(
        self,
        *,
        status: str = "",
        intake_id: str = "",
        limit: int = 0,
    ) -> dict[str, Any]:
        """Return JSON-friendly proposed task contracts."""
        records = self.proposed_task_records(status=status, intake_id=intake_id, limit=limit)
        return {
            "project_id": self.config.project_id,
            "proposals": [proposed_task_to_dict(record) for record in records],
        }

    def record_intervention(
        self,
        *,
        reason: str,
        task_id: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
        intervention_id: str = "",
        actor: str = "system",
    ) -> InterventionRecord:
        """Record a durable human intervention need without notifying anyone."""
        self._ensure_mutation_allowed()
        cleaned_reason = _first_text(reason)
        if cleaned_reason not in INTERVENTION_REASONS:
            allowed = ", ".join(sorted(INTERVENTION_REASONS))
            raise ValueError(f"intervention reason must be one of: {allowed}")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("intervention metadata must be a JSON object")
        record = InterventionRecord(
            intervention_id=_first_text(intervention_id) or uuid.uuid4().hex,
            task_id=_first_text(task_id),
            reason=cleaned_reason,
            summary=_first_text(summary),
            metadata=metadata or {},
        )
        return self.store.record_intervention(
            self.config.project_id,
            record,
            actor=actor,
        )

    def intervention_records(
        self,
        *,
        status: str = "",
        reason: str = "",
        task_id: str = "",
        limit: int = 0,
    ) -> list[InterventionRecord]:
        """Return durable intervention records."""
        if limit < 0:
            raise ValueError("limit must be greater than or equal to zero")
        cleaned_status = _first_text(status)
        if cleaned_status and cleaned_status not in INTERVENTION_STATES:
            raise ValueError(f"invalid intervention status: {cleaned_status}")
        cleaned_reason = _first_text(reason)
        if cleaned_reason and cleaned_reason not in INTERVENTION_REASONS:
            raise ValueError(f"invalid intervention reason: {cleaned_reason}")
        return self.store.intervention_records(
            self.config.project_id,
            status=cleaned_status,
            reason=cleaned_reason,
            task_id=_first_text(task_id),
            limit=limit,
        )

    def resolve_intervention(
        self,
        intervention_id: str,
        *,
        resolution: str = "",
        evidence: list[str] | None = None,
        actor: str = "system",
    ) -> InterventionRecord:
        """Resolve one intervention with evidence or a resolution reason."""
        self._ensure_mutation_allowed()
        cleaned_intervention_id = _first_text(intervention_id)
        if not cleaned_intervention_id:
            raise ValueError("intervention id is required")
        return self.store.resolve_intervention(
            self.config.project_id,
            cleaned_intervention_id,
            resolution=_first_text(resolution),
            evidence=_clean_strings(evidence or []),
            actor=actor,
        )

    def interventions_payload(
        self,
        *,
        status: str = "",
        reason: str = "",
        task_id: str = "",
        limit: int = 0,
    ) -> dict[str, Any]:
        """Return JSON-friendly intervention records."""
        records = self.intervention_records(
            status=status,
            reason=reason,
            task_id=task_id,
            limit=limit,
        )
        return {
            "project_id": self.config.project_id,
            "interventions": [intervention_to_dict(record) for record in records],
        }

    def pr_notification_setup_payload(
        self,
        *,
        workspace: str = "",
        repo_path: str | Path | None = None,
        remote: str = "origin",
        timeout_seconds: int = 5,
    ) -> dict[str, Any]:
        """Return read-only diagnostics for PR-based intervention notifications."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be greater than or equal to zero")
        checker = load_plugin(self.config, "pr_notification_setup_checker")
        if checker is not None:
            return checker.check_pr_notification_setup(
                self.config,
                workspace=workspace,
                repo_path=repo_path,
                remote=remote,
                timeout_seconds=timeout_seconds,
            )
        notification_workspace = _notification_workspace(
            self.config,
            workspace=workspace,
            repo_path=repo_path,
        )
        return _default_pr_notification_setup_payload(
            self.config,
            notification_workspace,
            interventions=[
                intervention_to_dict(record) for record in self.intervention_records(status="open")
            ],
            remote=remote,
            timeout_seconds=timeout_seconds,
        )

    def export_pr_notifications(
        self,
        *,
        workspace: str = "",
        repo_path: str | Path | None = None,
        remote: str = "origin",
        timeout_seconds: int = 5,
        prepared_payload_path: str | Path | None = None,
        dry_run: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Export open intervention notifications to a PR or prepared payload."""
        self._ensure_mutation_allowed()
        setup = self.pr_notification_setup_payload(
            workspace=workspace,
            repo_path=repo_path,
            remote=remote,
            timeout_seconds=timeout_seconds,
        )
        prepared_payload = setup["prepared_payload"]
        intervention_ids = list(prepared_payload.get("intervention_ids") or [])
        target = setup.get("target")
        target_payload = target if isinstance(target, dict) else {}
        target_key = _notification_target_key(target_payload)
        payload_hash = _notification_payload_hash(prepared_payload)
        existing = (
            self.store.notification_delivery(
                self.config.project_id,
                target_key,
                channel="pull_request_comment",
            )
            if target_key
            else None
        )
        result: dict[str, Any] = {
            "project_id": self.config.project_id,
            "ok": False,
            "action": "refused",
            "status": setup["status"],
            "target_key": target_key,
            "payload_hash": payload_hash,
            "setup": setup,
            "prepared_payload": prepared_payload,
            "prepared_payload_path": "",
            "delivery": notification_delivery_to_dict(existing) if existing else None,
            "issues": list(setup.get("issues") or []),
        }
        if not intervention_ids:
            result["ok"] = True
            result["action"] = "skipped"
            result["status"] = "no_open_interventions"
            return result
        if not target_key:
            return result
        posting_mode = str(setup.get("posting", {}).get("mode") or "")
        if setup.get("ok") and posting_mode == "live_comment":
            if _live_export_satisfied(existing, payload_hash):
                result["ok"] = True
                result["action"] = "suppressed"
                result["status"] = "unchanged"
                return result
            if dry_run:
                result["ok"] = True
                result["action"] = "dry_run"
                result["status"] = "pending"
                return result
            delivery = _post_pr_notification(
                target_payload,
                prepared_payload,
                existing=existing,
                timeout_seconds=timeout_seconds,
                path=Path(setup["repo"]["path"]),
            )
            record = self.store.upsert_notification_delivery(
                self.config.project_id,
                target_key=target_key,
                channel="pull_request_comment",
                target=target_payload,
                comment_id=delivery["comment_id"],
                payload_hash=payload_hash,
                status=delivery["status"],
                last_posted_at=_utc_timestamp(),
                metadata={
                    "intervention_ids": intervention_ids,
                    "body": prepared_payload["body"],
                },
                actor=actor,
            )
            result["ok"] = True
            result["action"] = delivery["action"]
            result["status"] = delivery["status"]
            result["delivery"] = notification_delivery_to_dict(record)
            return result

        path = _prepared_pr_notification_path(self.config, prepared_payload_path)
        if _prepared_export_satisfied(existing, payload_hash, path):
            result["ok"] = True
            result["action"] = "suppressed"
            result["status"] = "unchanged"
            result["prepared_payload_path"] = str(path)
            return result
        if dry_run:
            result["ok"] = True
            result["action"] = "dry_run"
            result["status"] = "pending"
            result["prepared_payload_path"] = str(path)
            return result
        payload_to_write = {
            "project_id": self.config.project_id,
            "target_key": target_key,
            "payload_hash": payload_hash,
            "target": target_payload,
            "payload": prepared_payload,
            "setup_status": setup["status"],
            "setup_issues": setup.get("issues", []),
        }
        written_path = PreparedPrNotificationExporter().export(path, payload_to_write)
        record = self.store.upsert_notification_delivery(
            self.config.project_id,
            target_key=target_key,
            channel="pull_request_comment",
            target=target_payload,
            comment_id=existing.comment_id if existing else "",
            payload_hash=payload_hash,
            status="prepared",
            metadata={
                "intervention_ids": intervention_ids,
                "prepared_payload_path": str(path),
            },
            actor=actor,
        )
        result["ok"] = True
        result["action"] = "prepared"
        result["status"] = "prepared"
        result["prepared_payload_path"] = written_path
        result["delivery"] = notification_delivery_to_dict(record)
        return result

    def ingest_event_file(self, path: str | Path, *, actor: str = "system") -> bool:
        """Record an event from a JSON file."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("event file must contain a JSON object")
        return self.record_event(payload, actor=actor)

    def ingest_spool(self, *, actor: str = "system") -> dict[str, int]:
        """Ingest configured spool JSON files."""
        self._ensure_mutation_allowed()
        inbox, done, error = _local_spool_paths(self.config)
        if inbox is None or not inbox.exists():
            return {"processed": 0, "inserted": 0, "errors": 0}
        if done is None:
            done = inbox / "done"
        if error is None:
            error = inbox / "error"
        done.mkdir(parents=True, exist_ok=True)
        error.mkdir(parents=True, exist_ok=True)
        processed = inserted = errors = 0
        for event_path in sorted(inbox.glob("*.json")):
            if event_path.is_dir():
                continue
            processed += 1
            try:
                if self.ingest_event_file(event_path, actor=actor):
                    inserted += 1
                shutil.move(str(event_path), str(done / event_path.name))
            except Exception:
                errors += 1
                shutil.move(str(event_path), str(error / event_path.name))
        return {"processed": processed, "inserted": inserted, "errors": errors}

    def pull_spool(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Copy complete remote spool files into the local spool inbox."""
        self._ensure_mutation_allowed()
        spool = self.config.raw.get("spool", {})
        if not isinstance(spool, dict):
            spool = {}
        remote_inbox_value = spool.get("remote_inbox")
        local_inbox, done, error = _local_spool_paths(self.config)
        if _is_ssh_spool_value(remote_inbox_value):
            if local_inbox is None:
                raise ValueError("spool.inbox or spool_inbox is required for pull-spool")
            if done is None:
                done = local_inbox / "done"
            if error is None:
                error = local_inbox / "error"
            if not dry_run:
                local_inbox.mkdir(parents=True, exist_ok=True)
            return asyncio.run(
                _pull_spool_sftp(
                    self.config,
                    str(remote_inbox_value),
                    local_inbox,
                    done,
                    error,
                    dry_run=dry_run,
                )
            )
        remote_inbox = _spool_path(self.config, remote_inbox_value)
        if remote_inbox is None:
            raise ValueError("spool.remote_inbox is required for pull-spool")
        if local_inbox is None:
            raise ValueError("spool.inbox or spool_inbox is required for pull-spool")
        if done is None:
            done = local_inbox / "done"
        if error is None:
            error = local_inbox / "error"
        if not remote_inbox.exists():
            return _pull_spool_result(remote_inbox, local_inbox, dry_run=dry_run)
        if not remote_inbox.is_dir():
            raise ValueError(f"spool.remote_inbox is not a directory: {remote_inbox}")
        if not dry_run:
            local_inbox.mkdir(parents=True, exist_ok=True)

        result = _pull_spool_result(remote_inbox, local_inbox, dry_run=dry_run)
        for source in sorted(remote_inbox.iterdir()):
            if source.is_dir() or not _is_spool_event_file(source):
                result["skipped"] += 1
                continue
            target = local_inbox / source.name
            item = {"source": str(source), "target": str(target)}
            existing_candidates = [
                (target, "skip_existing", "conflict"),
                (done / source.name, "skip_done", "conflict_done"),
                (error / source.name, "skip_error", "conflict_error"),
            ]
            handled = False
            for existing, skip_action, conflict_action in existing_candidates:
                if not existing.exists():
                    continue
                if existing.is_file() and existing.read_bytes() == source.read_bytes():
                    result["skipped"] += 1
                    result["files"].append(
                        {**item, "existing": str(existing), "action": skip_action}
                    )
                else:
                    result["conflicts"] += 1
                    result["files"].append(
                        {**item, "existing": str(existing), "action": conflict_action}
                    )
                handled = True
                break
            if handled:
                continue
            result["processed"] += 1
            result["files"].append({**item, "action": "copy"})
            if not dry_run:
                _copy_spool_file_atomic(source, target)
                result["copied"] += 1
        return result

    def process_task_ingest_commands(
        self,
        *,
        actor: str = "task-ingest",
        limit: int = 0,
    ) -> dict[str, Any]:
        """Process filesystem task-ingest command request files.

        The processor owns canonical SQLite mutations. Remote or non-canonical
        agents publish command request files and read durable response files.
        """
        self._ensure_mutation_allowed()
        if limit < 0:
            raise ValueError("limit must be greater than or equal to zero")
        paths = _task_command_paths(self.config)
        if not paths.inbox.exists():
            return _task_command_result(paths)
        paths.processing.mkdir(parents=True, exist_ok=True)
        paths.done.mkdir(parents=True, exist_ok=True)
        paths.error.mkdir(parents=True, exist_ok=True)
        paths.responses.mkdir(parents=True, exist_ok=True)

        result = _task_command_result(paths)
        request_paths = [
            path
            for path in sorted(paths.inbox.iterdir())
            if path.is_file() and _is_spool_event_file(path)
        ]
        for request_path in request_paths[:limit] if limit else request_paths:
            result["processed"] += 1
            processing_path = paths.processing / request_path.name
            if processing_path.exists():
                result["skipped"] += 1
                result["files"].append(
                    {
                        "request": str(request_path),
                        "action": "skip_processing_exists",
                        "archive": str(processing_path),
                    }
                )
                continue
            try:
                request_path.rename(processing_path)
            except FileNotFoundError:
                result["skipped"] += 1
                result["files"].append({"request": str(request_path), "action": "skip_missing"})
                continue
            item = self._process_task_command_file(processing_path, paths, actor=actor)
            result["files"].append(item)
            status = item.get("status", "")
            if item.get("duplicate"):
                result["duplicates"] += 1
            if status == "succeeded":
                result["succeeded"] += 1
            elif status == "rejected":
                result["rejected"] += 1
            elif status == "failed":
                result["failed"] += 1
        return result

    def _process_task_command_file(
        self,
        processing_path: Path,
        paths: _TaskCommandPaths,
        *,
        actor: str,
    ) -> dict[str, Any]:
        """Process one claimed task-ingest command request file."""
        response: dict[str, Any] | None = None
        archive_dir = paths.error
        request: dict[str, Any] | None = None
        command_id = processing_path.stem
        try:
            raw_text = processing_path.read_text(encoding="utf-8")
            loaded = json.loads(raw_text)
            if not isinstance(loaded, dict):
                raise _TaskCommandRejected(
                    "invalid_payload",
                    "command request must be a JSON object",
                )
            request = loaded
            command_id = _first_text(request.get("command_id"), command_id)
            response_path = _task_command_response_path(self.config, paths, request, command_id)
            response_path.parent.mkdir(parents=True, exist_ok=True)
            request_digest = _task_command_request_digest(request)
            response = self._task_command_response_for_request(
                request,
                request_digest=request_digest,
                actor=actor,
            )
            archive_dir = paths.done if response["status"] == "succeeded" else paths.error
        except _TaskCommandRejected as exc:
            response = _task_command_response(
                self.config.project_id,
                request or {},
                command_id=command_id,
                status="rejected",
                error={"code": exc.code, "message": exc.message},
            )
            response_path = _default_task_command_response_path(paths, command_id)
        except Exception as exc:
            response = _task_command_response(
                self.config.project_id,
                request or {},
                command_id=command_id,
                status="failed",
                error={"code": "internal_error", "message": str(exc)},
            )
            response_path = _default_task_command_response_path(paths, command_id)
        if response is not None and response.get("command_id"):
            _write_spool_file_atomic(
                (json.dumps(response, indent=2, sort_keys=True) + "\n").encode(),
                response_path,
            )
        archive_path = archive_dir / processing_path.name
        shutil.move(str(processing_path), str(archive_path))
        return {
            "request": str(processing_path),
            "archive": str(archive_path),
            "response": (
                str(response_path) if response is not None and response.get("command_id") else ""
            ),
            "status": response.get("status", "failed") if response else "failed",
            "duplicate": bool(response.get("duplicate")) if response else False,
            "command": response.get("command", "") if response else "",
            "task_id": response.get("task_id", "") if response else "",
        }

    def _task_command_response_for_request(
        self,
        request: dict[str, Any],
        *,
        request_digest: str,
        actor: str,
    ) -> dict[str, Any]:
        """Return the response for a valid task-ingest request."""
        _validate_task_command_envelope(self.config.project_id, request)
        idempotency_key = _first_text(request.get("idempotency_key"))
        pending_response = _task_command_response(
            self.config.project_id,
            request,
            status="pending",
            error={
                "code": "command_pending",
                "message": ("command was accepted but no final response has been recorded yet"),
            },
        )
        try:
            existing = self.store.begin_task_ingest_idempotency(
                self.config.project_id,
                idempotency_key,
                request_digest=request_digest,
                response=pending_response,
                actor=actor,
            )
        except ValueError as exc:
            if "idempotency conflict" in str(exc).lower():
                raise _TaskCommandRejected(
                    "idempotency_conflict",
                    "idempotency key was already used for a different command body",
                ) from exc
            raise
        if existing is not None:
            duplicate = dict(existing.response)
            duplicate["command_id"] = _first_text(request.get("command_id"))
            duplicate["duplicate"] = True
            return duplicate
        try:
            applied_result = self._apply_task_command(request)
        except _TaskCommandRejected:
            self.store.discard_task_ingest_idempotency(
                self.config.project_id,
                idempotency_key,
                request_digest=request_digest,
            )
            raise
        except KeyError as exc:
            self.store.discard_task_ingest_idempotency(
                self.config.project_id,
                idempotency_key,
                request_digest=request_digest,
            )
            raise _TaskCommandRejected("invalid_payload", str(exc)) from exc
        except ValueError as exc:
            self.store.discard_task_ingest_idempotency(
                self.config.project_id,
                idempotency_key,
                request_digest=request_digest,
            )
            raise _TaskCommandRejected(_task_command_error_code(str(exc)), str(exc)) from exc
        response = _task_command_response(
            self.config.project_id,
            request,
            status="succeeded",
            result=applied_result,
        )
        self.store.finish_task_ingest_idempotency(
            self.config.project_id,
            idempotency_key,
            request_digest=request_digest,
            response=response,
            actor=actor,
        )
        return response

    def _apply_task_command(self, request: dict[str, Any]) -> dict[str, Any]:
        """Apply one validated task-ingest command through service methods."""
        command = _first_text(request.get("command"))
        actor_id = _task_command_actor_id(request)
        actor_role = _task_command_actor_role(request)
        task_id = _first_text(request.get("task_id"))
        lease_token = _first_text(request.get("lease_token"))
        payload = _task_command_payload(request)
        if command == "claim":
            claim = self.claim(
                agent_id=actor_id,
                task_id=_required_task_command_field(task_id, "task_id"),
                role=actor_role,
                lease_seconds=_task_command_int(payload, "lease_seconds", default=3600),
            )
            return {
                "lease_token": claim.lease_token,
                "lease_expires_at": claim.lease_expires_at,
                "state": "claimed",
            }
        if command == "heartbeat":
            claim = self.heartbeat(
                _required_task_command_field(task_id, "task_id"),
                lease_token=_required_task_command_field(lease_token, "lease_token"),
                lease_seconds=_task_command_int(payload, "lease_seconds", default=3600),
                agent_id=actor_id,
            )
            return {"lease_expires_at": claim.lease_expires_at, "state": "in_progress"}
        if command == "complete":
            self.complete(
                _required_task_command_field(task_id, "task_id"),
                lease_token=_required_task_command_field(lease_token, "lease_token"),
                evidence=_task_command_string_list(payload.get("evidence")),
                agent_id=actor_id,
                direct_merge=payload.get("direct_merge") is True,
            )
            return {"state": "done"}
        if command == "fail":
            self.fail(
                _required_task_command_field(task_id, "task_id"),
                lease_token=_required_task_command_field(lease_token, "lease_token"),
                reason=_required_task_command_field(
                    _first_text(payload.get("reason")),
                    "payload.reason",
                ),
                agent_id=actor_id,
            )
            return {"state": "failed"}
        if command == "submit_review":
            self.submit_review(
                _required_task_command_field(task_id, "task_id"),
                lease_token=_required_task_command_field(lease_token, "lease_token"),
                evidence=_task_command_string_list(payload.get("evidence")),
                agent_id=actor_id,
            )
            return {"state": "awaiting_review"}
        if command == "await_integration":
            status = _first_text(payload.get("status")) or "awaiting_integration"
            self.await_integration(
                _required_task_command_field(task_id, "task_id"),
                lease_token=_required_task_command_field(lease_token, "lease_token"),
                status=status,
                evidence=_task_command_string_list(payload.get("evidence")),
                agent_id=actor_id,
            )
            return {"state": status}
        if command == "resolve_review":
            status = _first_text(payload.get("status")) or "done"
            self.resolve_review(
                _required_task_command_field(task_id, "task_id"),
                status=status,
                evidence=_task_command_string_list(payload.get("evidence")),
                agent_id=actor_id,
                reason=_first_text(payload.get("reason")),
                direct_merge=payload.get("direct_merge") is True,
            )
            return {"state": status}
        if command == "resolve_integration":
            status = _first_text(payload.get("status")) or "done"
            self.resolve_integration(
                _required_task_command_field(task_id, "task_id"),
                status=status,
                evidence=_task_command_string_list(payload.get("evidence")),
                agent_id=actor_id,
                reason=_first_text(payload.get("reason")),
                direct_merge=payload.get("direct_merge") is True,
            )
            return {"state": status}
        if command == "record_intake":
            intake = self.record_intake(
                _required_task_command_field(
                    _first_text(payload.get("description")) or _first_text(payload.get("text")),
                    "payload.description",
                ),
                kind=_first_text(payload.get("kind")) or "idea",
                source=_first_text(payload.get("source")),
                repo=_first_text(payload.get("repo")),
                tags=_task_command_string_list(payload.get("tags")),
                metadata=_task_command_dict(payload.get("metadata")),
                intake_id=_first_text(payload.get("intake_id")),
                actor=actor_id,
            )
            return {"intake_id": intake.intake_id, "status": intake.status}
        if command == "propose_task":
            task_payload = _task_command_dict(payload.get("task"))
            proposal = self.propose_task_from_intake(
                _required_task_command_field(
                    _first_text(payload.get("intake_id")),
                    "payload.intake_id",
                ),
                task_id=_required_task_command_field(
                    _first_text(task_payload.get("id")) or _first_text(task_payload.get("task_id")),
                    "payload.task.id",
                ),
                title=_required_task_command_field(
                    _first_text(task_payload.get("title")),
                    "payload.task.title",
                ),
                repo=_first_text(task_payload.get("repo")),
                summary=_first_text(task_payload.get("summary")),
                next_action=_first_text(task_payload.get("next_action")),
                write_scopes=_task_command_string_list(
                    _task_command_dict(task_payload.get("metadata")).get("write_scopes")
                    or _task_command_dict(task_payload.get("execution")).get("primary_files")
                ),
                validation_checks=_task_command_string_list(task_payload.get("validation_checks")),
                requirements=_task_command_requirements(payload.get("requirements")),
                metadata=_task_command_dict(task_payload.get("metadata")),
                proposal_id=_first_text(payload.get("proposal_id")),
                actor=actor_id,
            )
            return {"proposal_id": proposal.proposal_id, "task_id": proposal.task.task_id}
        if command == "promote_proposal":
            proposal = self.promote_proposed_task(
                _required_task_command_field(
                    _first_text(payload.get("proposal_id")),
                    "payload.proposal_id",
                ),
                actor=actor_id,
            )
            return {"proposal_id": proposal.proposal_id, "task_id": proposal.task.task_id}
        raise _TaskCommandRejected("unknown_command", f"unknown task-ingest command: {command}")

    def workspace_payload(self) -> dict[str, Any]:
        """Return configured cross-project workspaces."""
        return {
            "project_id": self.config.project_id,
            "workspaces": [
                _workspace_to_dict(workspace)
                for workspace in sorted(
                    _workspace_records(self.config),
                    key=lambda item: item.name,
                )
            ],
        }

    def launch_worker(
        self,
        workspace_name: str,
        *,
        task_id: str = "",
        prompt_text: str = "",
        agent_id: str = "",
        role: str = "",
        lease_seconds: int = 3600,
        claim_task: bool = False,
        markdown: bool = True,
        execute: bool = False,
        command: list[str] | None = None,
        dry_run: bool = False,
        timeout_seconds: int = 0,
        branch: str = "",
        base_ref: str = "",
        worktree_path: str = "",
    ) -> dict[str, Any]:
        """Prepare or run a one-shot local worker in a configured workspace."""
        self._ensure_mutation_allowed()
        workspace = _workspace_by_name(self.config, workspace_name)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be greater than or equal to zero")
        if workspace.kind == "local" and not workspace.path.is_dir():
            raise ValueError(f"workspace path is not a directory: {workspace.path}")
        if workspace.kind not in {"local", "ssh"}:
            raise ValueError(f"unsupported workspace kind: {workspace.kind}")
        if dry_run and claim_task:
            raise ValueError("claim_task cannot be used with dry_run")
        if workspace.kind == "ssh" and claim_task and execute:
            raise ValueError(
                "SSH launch-worker cannot combine claim_task with execute until "
                "the task-ingest claim response is processed"
            )
        cleaned_task_id = _first_text(task_id)
        if (
            workspace.kind == "local"
            and cleaned_task_id
            and _workspace_contains_config(workspace.path, self.config)
        ):
            raise ValueError(
                "launch-worker task workspaces must not contain the canonical tracker "
                "config; use an isolated task worktree or explicit non-canonical workspace"
            )
        coordination = _worker_coordination_context(
            self.config,
            workspace=workspace,
            task_id=cleaned_task_id,
            branch=branch,
            base_ref=base_ref,
            worktree_path=worktree_path,
        )

        launch_id = (
            f"{_launch_id_component(self.config.project_id, fallback='project')}-"
            f"{_launch_id_component(workspace.name, fallback='workspace')}-"
            f"{uuid.uuid4().hex[:12]}"
        )
        actor = _first_text(agent_id, f"worker-launch:{workspace.name}")
        claim: Claim | None = None
        if claim_task:
            if not cleaned_task_id:
                raise ValueError("task_id is required when claim_task is true")
            if workspace.kind == "local":
                claim = self.claim(
                    agent_id=actor,
                    task_id=cleaned_task_id,
                    role=role,
                    lease_seconds=lease_seconds,
                )
        if cleaned_task_id:
            prompt = self.render_prompt(cleaned_task_id, markdown=markdown)
        else:
            prompt = prompt_text.strip()
        if not prompt:
            raise ValueError("prompt text or task_id is required")
        prompt = _append_worker_coordination_prompt(prompt, coordination)

        launch_dir = _worker_launch_dir(self.config, workspace, launch_id)
        prompt_path = launch_dir / "prompt.md"
        report_path = launch_dir / "report.md"
        stdout_path = launch_dir / "stdout.txt"
        stderr_path = launch_dir / "stderr.txt"
        launch_path = launch_dir / "launch.json"
        assignment = coordination["assignment"]
        placeholders = {
            "agent_id": actor,
            "base_ref": assignment["base_ref"],
            "branch": assignment["branch"],
            "launch_id": launch_id,
            "project_id": self.config.project_id,
            "prompt_path": str(prompt_path),
            "report_path": str(report_path),
            "task_id": cleaned_task_id,
            "workspace": workspace.name,
            "workspace_path": str(workspace.path),
            "worktree_path": assignment["worktree_path"],
        }
        remote_artifacts = _ssh_worker_remote_artifacts(workspace, launch_id)
        command_placeholders = (
            {
                **placeholders,
                "prompt_path": remote_artifacts["prompt"],
                "report_path": remote_artifacts["report"],
            }
            if workspace.kind == "ssh"
            else placeholders
        )
        command_argv = (
            _worker_command_argv(workspace, command, command_placeholders) if execute else []
        )
        result: dict[str, Any] = {
            "project_id": self.config.project_id,
            "workspace": _workspace_to_dict(workspace),
            "launch_id": launch_id,
            "status": "dry_run" if dry_run else "prepared",
            "dry_run": dry_run,
            "execute": execute,
            "task_id": cleaned_task_id,
            "agent_id": actor,
            "role": _first_text(role),
            "lease": claim.__dict__ if claim else None,
            "coordination": coordination,
            "artifacts": {
                "directory": str(launch_dir),
                "prompt": str(prompt_path),
                "report": str(report_path),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "launch": str(launch_path),
            },
            "command": command_argv,
        }
        if workspace.kind == "ssh":
            result["remote_artifacts"] = remote_artifacts
        if dry_run:
            return result

        launch_dir.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        if claim_task and workspace.kind == "ssh":
            result["task_ingest"] = _write_task_ingest_claim_request(
                self.config,
                launch_id=launch_id,
                actor=actor,
                role=_first_text(role),
                task_id=cleaned_task_id,
                lease_seconds=lease_seconds,
            )
        if execute:
            if workspace.kind == "ssh":
                completed = _run_ssh_worker_command(
                    self.config,
                    workspace,
                    command_argv,
                    prompt=prompt,
                    report_path=report_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    env={
                        **placeholders,
                        "prompt_path": remote_artifacts["prompt"],
                        "report_path": remote_artifacts["report"],
                    },
                    timeout_seconds=timeout_seconds,
                )
            else:
                completed = _run_worker_command(
                    command_argv,
                    workspace=workspace,
                    prompt=prompt,
                    report_path=report_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    env=placeholders,
                    timeout_seconds=timeout_seconds,
                )
            result["status"] = "succeeded" if completed.returncode == 0 else "failed"
            result["returncode"] = completed.returncode
        else:
            report_path.write_text(
                "Worker launch prepared; no command was executed.\n",
                encoding="utf-8",
            )
        launch_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if workspace.spool_outbox is not None:
            if workspace.kind == "ssh":
                _write_ssh_worker_launch_event(self.config, workspace, result)
            else:
                _write_worker_launch_event(workspace, result)
        if cleaned_task_id and workspace.kind == "local":
            self.record_evidence(
                cleaned_task_id,
                f"worker-launch:{launch_id}",
                actor=actor,
            )
            self.record_evidence(
                cleaned_task_id,
                f"file:{launch_path}",
                actor=actor,
            )
        return result

    def render_prompt(
        self,
        task_id: str,
        *,
        markdown: bool = False,
        recover_stale_leases: bool = False,
    ) -> str:
        """Render a prompt for a task."""
        state = self.get_task(task_id, recover_stale_leases=recover_stale_leases)
        renderer = load_plugin(
            self.config,
            "prompt_renderer",
            "agent_tracker.rendering:DefaultPromptRenderer",
        )
        if renderer is None:
            renderer = DefaultPromptRenderer()
        return renderer.render_prompt(self.config, state, markdown=markdown)

    def worker_coordination_context(
        self,
        *,
        task_id: str = "",
        branch: str = "",
        base_ref: str = "",
        worktree_path: str = "",
    ) -> dict[str, Any]:
        """Return default worker coordination policy context for a task."""
        return _worker_coordination_context(
            self.config,
            task_id=task_id,
            branch=branch,
            base_ref=base_ref,
            worktree_path=worktree_path,
        )

    def render_worker_prompt(
        self,
        task_id: str,
        *,
        markdown: bool = False,
        branch: str = "",
        base_ref: str = "",
        worktree_path: str = "",
    ) -> str:
        """Render a task prompt with worker coordination context appended."""
        prompt = self.render_prompt(task_id, markdown=markdown)
        coordination = self.worker_coordination_context(
            task_id=task_id,
            branch=branch,
            base_ref=base_ref,
            worktree_path=worktree_path,
        )
        return _append_worker_coordination_prompt(prompt, coordination)

    def export(self) -> list[str]:
        """Export an audit snapshot through the configured exporter."""
        self._ensure_mutation_allowed()
        exporter = load_plugin(
            self.config, "exporter", "agent_tracker.exporters:JsonSnapshotExporter"
        )
        if exporter is None:
            raise RuntimeError("exporter is not configured")
        snapshot = self.store.snapshot(self.config.project_id)
        return exporter.export(self.config, snapshot)

    def status_payload(self, *, recover_stale_leases: bool = False) -> dict[str, Any]:
        """Return a JSON-friendly status payload."""
        states = self.task_states(recover_stale_leases=recover_stale_leases)
        return {
            "project_id": self.config.project_id,
            "name": self.config.name,
            "db_path": str(self.store.path),
            **self.config.path_summary(db_path=self.store.path),
            "tasks": [state_to_dict(state) for state in states],
            "ready": [state.task.task_id for state in states if state.state == "ready"],
            "active": [state.task.task_id for state in states if state.state in ACTIVE_STATES],
            "review": [state.task.task_id for state in states if state.state in REVIEW_STATES],
            "integration": [
                state.task.task_id for state in states if state.state in INTEGRATION_STATES
            ],
            "blocked": [state.task.task_id for state in states if state.state == "blocked"],
        }

    def overview_payload(
        self,
        *,
        recover_stale_leases: bool = False,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return a grouped project overview payload."""
        if limit < 0:
            raise ValueError("limit must be greater than or equal to zero")
        states = self.task_states(recover_stale_leases=recover_stale_leases)
        states_by_id = {state.task.task_id: state for state in states}
        completion_records = self.store.recent_completion_records(self.config.project_id)
        completion_records_by_task = {record["task_id"]: record for record in completion_records}
        recently_completed = [
            state
            for record in completion_records
            if (state := states_by_id.get(record["task_id"])) is not None and state.state == "done"
        ]
        grouped_states: dict[str, list[TaskState]] = {
            "ready": [state for state in states if state.state == "ready"],
            "active": [state for state in states if state.state in ACTIVE_STATES],
            "review": [state for state in states if state.state in REVIEW_STATES],
            "integration": [state for state in states if state.state in INTEGRATION_STATES],
            "blocked": [state for state in states if state.state == "blocked"],
            "recently_completed": recently_completed,
        }
        return {
            "project_id": self.config.project_id,
            "name": self.config.name,
            "db_path": str(self.store.path),
            **self.config.path_summary(db_path=self.store.path),
            "limit": limit,
            "counts": {key: len(value) for key, value in grouped_states.items()},
            "groups": {
                key: [
                    _overview_state_to_dict(
                        state,
                        completion_record=(
                            completion_records_by_task.get(state.task.task_id)
                            if key == "recently_completed"
                            else None
                        ),
                    )
                    for state in (value[:limit] if limit else value)
                ]
                for key, value in grouped_states.items()
            },
        }

    def task_detail_payload(
        self,
        task_id: str,
        *,
        recover_stale_leases: bool = False,
    ) -> dict[str, Any]:
        """Return a JSON-friendly payload for one human task detail view.

        Args:
            task_id: Identifier of the task to inspect.
            recover_stale_leases: Whether stale leases should be recovered while
                evaluating task state.

        Returns:
            Full task state plus computed blocker and completion fields used by
            the human detail renderer.

        Raises:
            KeyError: If the task does not exist.
        """
        state = self.get_task(task_id, recover_stale_leases=recover_stale_leases)
        completion_record = None
        if state.state == "done":
            completion_records = {
                record["task_id"]: record
                for record in self.store.recent_completion_records(self.config.project_id)
            }
            completion_record = completion_records.get(state.task.task_id)
        return _overview_state_to_dict(
            state,
            completion_record=completion_record,
        )

    def path_summary(self) -> dict[str, str]:
        """Return resolved paths used by this coordinator."""
        return self.config.path_summary(db_path=self.store.path)

    def _ensure_mutation_allowed(self) -> None:
        reason = self.config.mutation_refusal_reason(self.store.path)
        if reason:
            raise ValueError(reason)


def _role_matches(state: TaskState, role: str) -> bool:
    roles = state.task.metadata.get("roles") or state.task.metadata.get("allowed_roles") or []
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, (list, tuple, set)):
        return False
    return role in roles


def _overview_state_to_dict(
    state: TaskState,
    *,
    completion_record: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a task state into the overview task shape."""
    payload = state_to_dict(state)
    payload["blockers"] = [
        _requirement_summary(requirement) for requirement in state.outstanding_requirements
    ]
    payload["latest_evidence"] = state.evidence[-1] if state.evidence else ""
    if completion_record is not None:
        payload["completed_at"] = completion_record["completed_at"]
        payload["completed_by"] = completion_record["actor"]
        payload["completion_action"] = completion_record["action"]
    return payload


def _requirement_summary(requirement: RequirementState) -> str:
    """Return a compact blocker summary for human and JSON overview output."""
    description = requirement.description.strip()
    detail = requirement.detail.strip()
    if description and detail:
        return f"{description} ({detail})"
    return description or detail


def _first_text(*values: Any) -> str:
    """Return the first non-empty text value."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _default_event_record(payload: dict[str, Any]) -> EventRecord:
    """Normalize a plugin-free event payload."""
    return EventRecord(
        event_id=_first_text(
            payload.get("event_id"),
            payload.get("id"),
            payload.get("run_id"),
            payload.get("job_id"),
        ),
        kind=_first_text(payload.get("kind"), payload.get("event_type")) or "event",
        task_id=_first_text(payload.get("task_id")),
        payload=payload,
    )


def _clean_tags(tags: list[str]) -> list[str]:
    """Normalize tag values while preserving order."""
    return _clean_strings(tags)


def _clean_strings(values: list[str]) -> list[str]:
    """Normalize string values while preserving order."""
    cleaned = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            cleaned.append(text)
            seen.add(text)
    return cleaned


def _clean_notebook_paths(values: list[str]) -> list[str]:
    """Normalize and validate notebook include paths for task metadata."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        notebook_path = _normalize_notebook_path(value)
        if notebook_path and notebook_path not in seen:
            cleaned.append(notebook_path)
            seen.add(notebook_path)
    return cleaned


def _normalize_notebook_path(value: str) -> str:
    """Return a safe config-relative notebook path."""
    text = _first_text(value)
    if not text:
        return ""
    requested = Path(text)
    if text.startswith("~") or requested.is_absolute():
        raise ValueError("notebook path must be relative and cannot start with ~")
    normalized = Path(*requested.parts)
    if ".." in normalized.parts:
        raise ValueError("notebook path cannot contain parent traversal")
    if normalized.parts[:1] != ("notebooks",):
        raise ValueError("notebook path must be below notebooks/")
    return normalized.as_posix()


def _resolve_notebook_file(
    config: ProjectConfig,
    notebook_path: str,
    *,
    must_exist: bool,
) -> Path:
    """Resolve a notebook path below the task source notebook root."""
    normalized = _normalize_notebook_path(notebook_path)
    if not normalized:
        raise ValueError("notebook path is required")
    task_source_root = config.effective_task_source_root.resolve(strict=False)
    path = (task_source_root / normalized).resolve(strict=False)
    notebooks_root = (task_source_root / "notebooks").resolve(strict=False)
    if not path.is_relative_to(notebooks_root):
        raise ValueError("notebook path resolves outside notebooks/")
    if must_exist and not path.exists():
        raise FileNotFoundError(f"notebook does not exist: {normalized}")
    if path.exists() and not path.is_file():
        raise ValueError(f"notebook path is not a file: {normalized}")
    return path


def _notebook_record(
    config: ProjectConfig,
    *,
    kind: str,
    name: str,
    path: Path,
) -> NotebookRecord:
    """Build a notebook record from filesystem metadata."""
    stat = path.stat()
    return NotebookRecord(
        notebook_id=name if kind == "project" else f"repo:{name}",
        kind=kind,
        path=_display_notebook_path(config, path),
        title=_notebook_title(path),
        exists=True,
        size_bytes=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        ),
    )


def _display_notebook_path(config: ProjectConfig, path: Path) -> str:
    """Return a task-source-relative notebook path when possible."""
    resolved = path.resolve(strict=False)
    task_source_root = config.effective_task_source_root.resolve(strict=False)
    try:
        return resolved.relative_to(task_source_root).as_posix()
    except ValueError:
        pass
    config_root = config.root.resolve(strict=False)
    try:
        return resolved.relative_to(config_root).as_posix()
    except ValueError:
        pass
    return str(resolved)


def _notebook_title(path: Path) -> str:
    """Return the first Markdown heading from a notebook file."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text.startswith("#"):
                return text.lstrip("#").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return ""


def _notebook_kind_from_path(path: Path) -> str:
    """Infer the notebook kind from a conventional notebook path."""
    return "repo" if path.parent.name == "repos" else "project"


def _clean_requirements(requirements: list[dict[str, str]]) -> list[dict[str, str]]:
    """Normalize proposed task dependency records."""
    cleaned = []
    for requirement in requirements:
        task_id = _first_text(requirement.get("task"))
        if not task_id:
            raise ValueError("proposed task dependency is missing a task id")
        cleaned.append(
            {
                "kind": _first_text(requirement.get("kind")) or "task",
                "task": task_id,
                "description": _first_text(requirement.get("description")),
            }
        )
    return cleaned


def _top_level_state_path(config: ProjectConfig, key: str) -> Path | None:
    """Resolve an optional top-level path config value."""
    if key not in config.raw:
        return None
    value = _first_text(config.raw.get(key))
    return config.resolve_state_path(key) if value else None


def _completion_file_evidence_git_issues(
    config: ProjectConfig,
    states: list[TaskState],
) -> list[dict[str, Any]]:
    """Return completed-task file evidence pointing at untracked or unstaged files."""
    git_root = _git_worktree_root(config.effective_config_path)
    if git_root is None:
        return []
    issues: list[dict[str, Any]] = []
    for state in states:
        if state.task.status != "done":
            continue
        for uri in state.evidence:
            evidence_path = _file_evidence_path(uri)
            if not evidence_path:
                continue
            relative_path = _git_relative_evidence_path(git_root, evidence_path)
            if relative_path is None:
                continue
            evidence_state = _git_file_evidence_state(git_root, relative_path)
            if evidence_state == "clean":
                continue
            if evidence_state == "untracked":
                reason = "file evidence points at an untracked workspace file"
                kind = "file_evidence_untracked"
            else:
                reason = "file evidence points at an unstaged workspace file"
                kind = "file_evidence_unstaged"
            issues.append(
                {
                    "task_id": state.task.task_id,
                    "title": state.task.title,
                    "status": state.task.status,
                    "kind": kind,
                    "reason": reason,
                    "evidence": [uri],
                    "completion_action": "",
                    "completed_by": "",
                    "completed_at": "",
                    "direct_merge": False,
                }
            )
    return issues


def _git_worktree_root(path: Path) -> Path | None:
    """Return the containing Git worktree root, or ``None`` outside Git."""
    start = path if path.is_dir() else path.parent
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    root = completed.stdout.strip().splitlines()
    if not root:
        return None
    return Path(root[0]).resolve()


def _file_evidence_path(uri: str) -> str:
    """Return a local path from a file evidence URI, or an empty string."""
    if not uri.startswith("file:"):
        return ""
    text = uri.removeprefix("file:")
    if text.startswith("//"):
        parsed = urlsplit(uri)
        if parsed.netloc and parsed.netloc != "localhost":
            return ""
        text = parsed.path
    return unquote(text).strip()


def _git_relative_evidence_path(git_root: Path, evidence_path: str) -> str | None:
    """Resolve evidence below the Git root and return a Git pathspec."""
    path = Path(evidence_path).expanduser()
    candidate = path if path.is_absolute() else git_root / path
    git_root_text = os.path.abspath(os.path.normpath(str(git_root)))
    candidate_text = os.path.abspath(os.path.normpath(str(candidate)))
    try:
        common_path = os.path.commonpath([git_root_text, candidate_text])
    except ValueError:
        return None
    if common_path != git_root_text:
        return None
    pathspec = Path(os.path.relpath(candidate_text, git_root_text)).as_posix()
    if pathspec == ".":
        return None
    return pathspec or None


def _git_file_evidence_state(git_root: Path, relative_path: str) -> str:
    """Return whether a Git pathspec is clean, untracked, or unstaged."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(git_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
                "--",
                relative_path,
            ],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "clean"
    if completed.returncode != 0:
        return "clean"
    status_lines = [line for line in completed.stdout.splitlines() if len(line) >= 2]
    if any(line[:2] in {"??", "!!"} for line in status_lines):
        return "untracked"
    if any(line[1] != " " for line in status_lines if line[:2] != "??"):
        return "unstaged"
    return "clean"


def _spool_path(config: ProjectConfig, value: Any) -> Path | None:
    """Resolve an optional path from the `spool` config block."""
    text = _first_text(value)
    if not text:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else config.effective_state_root / path


def _local_spool_paths(
    config: ProjectConfig,
) -> tuple[Path | None, Path | None, Path | None]:
    """Return local inbox, done, and error paths for spool operations."""
    top_level_paths = (
        _top_level_state_path(config, "spool_inbox"),
        _top_level_state_path(config, "spool_done"),
        _top_level_state_path(config, "spool_error"),
    )
    spool = config.raw.get("spool", {})
    if not isinstance(spool, dict) or not spool:
        return top_level_paths
    inbox = _spool_path(config, spool.get("inbox"))
    if inbox is None:
        return top_level_paths
    return (
        inbox,
        _spool_path(config, spool.get("done")),
        _spool_path(config, spool.get("error")),
    )


@dataclass(frozen=True)
class _TaskCommandPaths:
    """Resolved local task-ingest command spool paths."""

    inbox: Path
    processing: Path
    done: Path
    error: Path
    responses: Path


class _TaskCommandRejected(ValueError):
    """A valid task-ingest request that cannot be applied."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _task_command_paths(config: ProjectConfig) -> _TaskCommandPaths:
    """Return configured task-ingest command spool paths."""
    commands = config.raw.get("commands", {})
    if not isinstance(commands, dict):
        commands = {}
    return _TaskCommandPaths(
        inbox=_command_path(config, commands.get("inbox"), "commands/inbox"),
        processing=_command_path(config, commands.get("processing"), "commands/processing"),
        done=_command_path(config, commands.get("done"), "commands/done"),
        error=_command_path(config, commands.get("error"), "commands/error"),
        responses=_command_path(config, commands.get("responses"), "commands/responses"),
    )


def _command_path(config: ProjectConfig, value: Any, default: str) -> Path:
    """Resolve one task-ingest command spool path below state root by default."""
    text = _first_text(value) or default
    state_root = config.effective_state_root.resolve(strict=False)
    path = Path(text).expanduser()
    resolved = (path if path.is_absolute() else state_root / path).resolve(strict=False)
    try:
        resolved.relative_to(state_root)
    except ValueError as exc:
        raise ValueError(
            "task-ingest command paths must resolve below the configured state root"
        ) from exc
    return resolved


def _task_command_result(paths: _TaskCommandPaths) -> dict[str, Any]:
    """Return the initial task-ingest processor result payload."""
    return {
        "inbox": str(paths.inbox),
        "processing": str(paths.processing),
        "done": str(paths.done),
        "error": str(paths.error),
        "responses": str(paths.responses),
        "processed": 0,
        "succeeded": 0,
        "rejected": 0,
        "failed": 0,
        "duplicates": 0,
        "skipped": 0,
        "files": [],
    }


def _task_command_response(
    project_id: str,
    request: dict[str, Any],
    *,
    command_id: str = "",
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a task-ingest command response payload."""
    return {
        "schema_version": TASK_INGEST_SCHEMA_VERSION,
        "command_id": _first_text(command_id, request.get("command_id")),
        "idempotency_key": _first_text(request.get("idempotency_key")),
        "project_id": project_id,
        "command": _first_text(request.get("command")),
        "status": status,
        "duplicate": False,
        "actor_id": _task_command_actor_id(request, required=False),
        "task_id": _first_text(request.get("task_id")),
        "result": result or {},
        "error": error,
        "audit_id": None,
    }


def _validate_task_command_envelope(project_id: str, request: dict[str, Any]) -> None:
    """Validate common task-ingest request fields."""
    schema_version = request.get("schema_version")
    if schema_version != TASK_INGEST_SCHEMA_VERSION:
        raise _TaskCommandRejected(
            "unsupported_schema_version",
            f"schema_version must be {TASK_INGEST_SCHEMA_VERSION}",
        )
    if not _first_text(request.get("command_id")):
        raise _TaskCommandRejected("invalid_payload", "command_id is required")
    if not _first_text(request.get("idempotency_key")):
        raise _TaskCommandRejected("invalid_payload", "idempotency_key is required")
    if _first_text(request.get("project_id")) != project_id:
        raise _TaskCommandRejected("project_mismatch", "request project_id does not match")
    if not _first_text(request.get("command")):
        raise _TaskCommandRejected("unknown_command", "command is required")
    _task_command_actor_id(request)


def _task_command_actor_id(request: dict[str, Any], *, required: bool = True) -> str:
    """Return a task-ingest actor ID."""
    actor = request.get("actor", {})
    actor_id = ""
    if isinstance(actor, dict):
        actor_id = _first_text(actor.get("id"))
    if required and not actor_id:
        raise _TaskCommandRejected("invalid_payload", "actor.id is required")
    return actor_id


def _task_command_actor_role(request: dict[str, Any]) -> str:
    """Return an optional task-ingest actor role."""
    actor = request.get("actor", {})
    if isinstance(actor, dict):
        return _first_text(actor.get("role"))
    return ""


def _task_command_payload(request: dict[str, Any]) -> dict[str, Any]:
    """Return the command-specific payload object."""
    payload = request.get("payload", {})
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise _TaskCommandRejected("invalid_payload", "payload must be a JSON object")
    return payload


def _task_command_request_digest(request: dict[str, Any]) -> str:
    """Return a semantic digest for idempotent command replay."""
    semantic_request = {
        key: value for key, value in request.items() if key not in {"command_id", "reply_to"}
    }
    data = json.dumps(semantic_request, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _task_command_response_path(
    config: ProjectConfig,
    paths: _TaskCommandPaths,
    request: dict[str, Any],
    command_id: str,
) -> Path:
    """Return the response path for a task-ingest request."""
    reply_to = _first_text(request.get("reply_to"))
    if not reply_to:
        return _default_task_command_response_path(paths, command_id)
    path = Path(reply_to).expanduser()
    if not path.is_absolute():
        path = config.effective_state_root / path
    state_root = config.effective_state_root.resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(state_root)
    except ValueError as exc:
        raise _TaskCommandRejected(
            "invalid_payload",
            "reply_to must resolve below the configured state root",
        ) from exc
    return path


def _default_task_command_response_path(paths: _TaskCommandPaths, command_id: str) -> Path:
    """Return the default response path for a command ID."""
    component = _launch_id_component(command_id, fallback="command")
    return paths.responses / f"{component}.json"


def _required_task_command_field(value: str, field_name: str) -> str:
    """Return a required command field or raise a task-ingest rejection."""
    cleaned = _first_text(value)
    if not cleaned:
        raise _TaskCommandRejected("invalid_payload", f"{field_name} is required")
    return cleaned


def _task_command_int(payload: dict[str, Any], key: str, *, default: int) -> int:
    """Return a positive integer command payload field."""
    value = payload.get(key, default)
    if not isinstance(value, int):
        raise _TaskCommandRejected("invalid_payload", f"payload.{key} must be an integer")
    if value <= 0:
        raise _TaskCommandRejected(
            "invalid_payload",
            f"payload.{key} must be greater than zero",
        )
    return value


def _task_command_string_list(value: Any) -> list[str]:
    """Return a normalized string list from a command payload value."""
    if value is None:
        return []
    if isinstance(value, str):
        return _clean_strings([value])
    if isinstance(value, list):
        return _clean_strings([str(item) for item in value])
    raise _TaskCommandRejected("invalid_payload", "expected a string list")


def _task_command_dict(value: Any) -> dict[str, Any]:
    """Return a JSON object from a command payload value."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _TaskCommandRejected("invalid_payload", "expected a JSON object")
    return value


def _task_command_requirements(value: Any) -> list[dict[str, str]]:
    """Return normalized proposal dependency records from command payload."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _TaskCommandRejected("invalid_payload", "payload.requirements must be a list")
    requirements: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _TaskCommandRejected(
                "invalid_payload",
                "payload.requirements items must be JSON objects",
            )
        dependency = _first_text(item.get("task"), item.get("dependency_task_id"))
        if dependency:
            requirements.append(
                {
                    "kind": "task",
                    "task": dependency,
                    "description": _first_text(item.get("description")),
                }
            )
    return requirements


def _task_command_error_code(message: str) -> str:
    """Map service exceptions to stable task-ingest error codes."""
    text = message.lower()
    if "lease token" in text:
        return "missing_lease_token" if "required" in text else "invalid_lease_owner"
    if "lease" in text and "expired" in text:
        return "lease_expired"
    if "not owned" in text or "different agent" in text:
        return "invalid_lease_owner"
    if "no matching ready task" in text or "depends" in text:
        return "dependency_blocked"
    if "completion evidence" in text or "evidence" in text:
        return "completion_evidence_missing"
    if "unknown task" in text:
        return "invalid_payload"
    return "invalid_payload"


def _pull_spool_result(
    remote_inbox: str | Path,
    local_inbox: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Return the initial pull-spool result payload."""
    return {
        "dry_run": dry_run,
        "remote_inbox": str(remote_inbox),
        "local_inbox": str(local_inbox),
        "processed": 0,
        "copied": 0,
        "skipped": 0,
        "conflicts": 0,
        "files": [],
    }


@dataclass(frozen=True)
class _SftpSpoolConfig:
    """Connection details for an SSH/SFTP remote spool."""

    host: str
    port: int
    remote_path: str
    display_inbox: str
    username: str
    password: str | None
    known_hosts: str | None
    client_keys: list[str] | None


@dataclass(frozen=True)
class _WorkerWorkspace:
    """Resolved cross-project workspace configuration."""

    name: str
    kind: str
    path: Path
    config_path: Path | None
    spool_outbox: Path | None
    artifacts_path: Path
    roles: list[str]
    capabilities: list[str]
    worker_command: list[str]
    host: str
    port: int
    username: str
    password: str | None
    known_hosts: str | None
    client_keys: list[str] | None
    remote_path: str


@dataclass(frozen=True)
class _NotificationWorkspace:
    """Resolved repository context for PR notification diagnostics."""

    name: str
    kind: str
    path: Path
    host: str = ""
    remote_path: str = ""


@dataclass(frozen=True)
class _ParsedSshHost:
    """Normalized SSH host target."""

    host: str
    port: int
    username: str


def _workspace_records(config: ProjectConfig) -> list[_WorkerWorkspace]:
    """Return resolved workspace registry entries."""
    raw_workspaces = config.raw.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        return []
    return [
        _parse_workspace(config, name, value)
        for name, value in raw_workspaces.items()
        if isinstance(value, dict)
    ]


def _workspace_by_name(config: ProjectConfig, name: str) -> _WorkerWorkspace:
    """Return one configured workspace by name."""
    cleaned = _first_text(name)
    if not cleaned:
        raise ValueError("workspace is required")
    for workspace in _workspace_records(config):
        if workspace.name == cleaned:
            return workspace
    raise KeyError(f"unknown workspace: {cleaned}")


def _parse_workspace_ssh_host(value: str, *, port: Any = None) -> _ParsedSshHost:
    """Parse a workspace SSH host string into host, port, and username."""
    text = _first_text(value)
    if not text:
        raise ValueError("SSH workspace host is required")
    parsed = urlsplit(text if "://" in text else f"ssh://{text}")
    if parsed.scheme and parsed.scheme != "ssh":
        raise ValueError("SSH workspace host must use the ssh scheme")
    if parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("SSH workspace host must not include password, path, query, or fragment")
    if not parsed.hostname:
        raise ValueError("SSH workspace host must include a hostname")
    try:
        parsed_port = parsed.port or 22
    except ValueError as exc:
        raise ValueError("SSH workspace host has an invalid port") from exc
    port_text = _first_text(port)
    if port_text:
        try:
            parsed_port = int(port_text)
        except ValueError as exc:
            raise ValueError("SSH workspace port must be an integer") from exc
    if parsed_port <= 0:
        raise ValueError("SSH workspace port must be greater than zero")
    return _ParsedSshHost(
        host=parsed.hostname,
        port=parsed_port,
        username=unquote(parsed.username or ""),
    )


def _parse_workspace(config: ProjectConfig, name: str, raw: dict[str, Any]) -> _WorkerWorkspace:
    """Resolve one workspace registry entry."""
    cleaned_name = _first_text(name)
    kind = _first_text(raw.get("kind")) or "local"
    if kind == "local":
        path = _resolve_config_path(config, _first_text(raw.get("path")))
        ssh_host = _ParsedSshHost(host="", port=22, username="")
    else:
        remote_path = _first_text(raw.get("remote_path")) or "."
        if not remote_path.startswith("/"):
            raise ValueError(f"workspaces.{cleaned_name}.remote_path must be absolute")
        path = Path(remote_path)
        ssh_host = _parse_workspace_ssh_host(
            _first_text(raw.get("host")),
            port=raw.get("port"),
        )
    config_path = _workspace_relative_path(
        path,
        raw.get("config_path"),
        field_name=f"workspaces.{cleaned_name}.config_path",
        require_child=kind == "local",
    )
    spool_outbox = _workspace_relative_path(
        path,
        raw.get("spool_outbox"),
        field_name=f"workspaces.{cleaned_name}.spool_outbox",
        require_child=kind == "local",
    )
    artifacts_path = (
        _workspace_relative_path(
            path,
            raw.get("artifacts_path"),
            field_name=f"workspaces.{cleaned_name}.artifacts_path",
            require_child=kind == "local",
        )
        or path / ".agent-tracker" / "workers"
    )
    return _WorkerWorkspace(
        name=cleaned_name,
        kind=kind,
        path=path,
        config_path=config_path,
        spool_outbox=spool_outbox,
        artifacts_path=artifacts_path,
        roles=_string_list(raw.get("roles")),
        capabilities=_string_list(raw.get("capabilities")),
        worker_command=_string_list(raw.get("worker_command")),
        host=ssh_host.host,
        port=ssh_host.port,
        username=_first_text(raw.get("username"), ssh_host.username),
        password=_first_text(raw.get("password")) or None,
        known_hosts=_known_hosts_option(config, raw.get("known_hosts")),
        client_keys=_client_key_options(config, raw.get("client_keys")),
        remote_path=_first_text(raw.get("remote_path")),
    )


def _workspace_relative_path(
    root: Path,
    value: Any,
    *,
    field_name: str,
    require_child: bool,
) -> Path | None:
    """Resolve an optional path configured below a workspace root.

    Args:
        root: Workspace root used as the base for relative paths.
        value: Raw config value to resolve. Empty values are treated as absent.
        field_name: Human-readable config field name for validation errors.
        require_child: Whether absolute paths and parent-directory escapes are
            rejected.

    Returns:
        The resolved path joined to ``root``, an absolute path when allowed for
        non-local workspaces, or ``None`` when ``value`` is empty.

    Raises:
        ValueError: If ``require_child`` is true and ``value`` is absolute or
            resolves outside ``root``.
    """
    text = _first_text(value)
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        if require_child:
            raise ValueError(f"{field_name} must be a workspace-relative path")
        return path
    candidate = root / path
    if require_child:
        try:
            candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be contained by the workspace path") from exc
    return candidate


def _string_list(value: Any) -> list[str]:
    """Return a normalized string list from a string or list value."""
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value) if value.strip() else []
    if isinstance(value, list):
        return _clean_strings(value)
    return []


def _workspace_to_dict(workspace: _WorkerWorkspace) -> dict[str, Any]:
    """Return a JSON-friendly workspace payload."""
    payload: dict[str, Any] = {
        "name": workspace.name,
        "kind": workspace.kind,
        "path": str(workspace.path),
        "config_path": str(workspace.config_path) if workspace.config_path else "",
        "spool_outbox": str(workspace.spool_outbox) if workspace.spool_outbox else "",
        "artifacts_path": str(workspace.artifacts_path),
        "roles": workspace.roles,
        "capabilities": workspace.capabilities,
    }
    if workspace.kind == "ssh":
        payload["host"] = workspace.host
        payload["port"] = workspace.port
        payload["username"] = workspace.username
        payload["remote_path"] = workspace.remote_path
    if workspace.worker_command:
        payload["worker_command"] = workspace.worker_command
    return payload


def _worker_coordination_context(
    config: ProjectConfig,
    *,
    workspace: _WorkerWorkspace | None = None,
    task_id: str = "",
    branch: str = "",
    base_ref: str = "",
    worktree_path: str = "",
) -> dict[str, Any]:
    """Return worker launch context for task worktree and PR policy."""
    cleaned_task_id = _first_text(task_id)
    assigned_worktree = _first_text(worktree_path)
    if (
        cleaned_task_id
        and assigned_worktree
        and _workspace_contains_config(
            Path(assigned_worktree).expanduser(),
            config,
        )
    ):
        raise ValueError(
            "launch-worker assigned worktree must not contain the canonical tracker "
            "config; use an isolated task worktree or explicit non-canonical workspace"
        )
    assignment: dict[str, str] = {
        "branch": _first_text(branch) or (f"codex/{cleaned_task_id}" if cleaned_task_id else ""),
        "base_ref": _first_text(base_ref) or ("main" if cleaned_task_id else ""),
        "worktree_path": assigned_worktree
        or (str(workspace.path) if workspace is not None else ""),
    }
    return {
        "policy": dict(config.coordination_policy),
        "assignment": assignment,
        "notes": [
            "Use the assigned non-canonical worktree for implementation.",
            "Parallel implementation agents must not write to the same worktree.",
            "Reviewers should inspect this branch, worktree, or explicit patch content.",
        ],
    }


def _append_worker_coordination_prompt(prompt: str, coordination: dict[str, Any]) -> str:
    """Append worker coordination policy to a rendered task prompt."""
    policy = coordination["policy"]
    assignment = coordination["assignment"]
    lines = [
        "",
        "## Coordination Context",
        "",
        f"- Worktree policy: `{policy['worktree_mode']}`",
        f"- PR policy: `{policy['pr_mode']}`",
    ]
    if assignment["branch"]:
        lines.append(f"- Assigned branch: `{assignment['branch']}`")
    if assignment["base_ref"]:
        lines.append(f"- Base ref: `{assignment['base_ref']}`")
    if assignment["worktree_path"]:
        lines.append(f"- Assigned worktree: `{assignment['worktree_path']}`")
    lines.extend(
        [
            "- Use the assigned non-canonical worktree for implementation.",
            "- Parallel implementation agents must not write to the same worktree.",
            "- Reviewers should inspect this branch, worktree, or explicit patch content.",
        ]
    )
    return prompt.rstrip() + "\n" + "\n".join(lines) + "\n"


def _workspace_contains_config(workspace_path: Path, config: ProjectConfig) -> bool:
    """Return true when a workspace contains the canonical/effective config file."""
    config_path = (config.canonical_config_path or config.effective_config_path).resolve(
        strict=False
    )
    workspace = workspace_path.resolve(strict=False)
    if config_path == workspace:
        return True
    try:
        config_path.relative_to(workspace)
    except ValueError:
        return False
    return True


def _notification_workspace(
    config: ProjectConfig,
    *,
    workspace: str,
    repo_path: str | Path | None,
) -> _NotificationWorkspace:
    """Resolve the workspace/repository path to diagnose."""
    explicit_repo_path = _first_text(str(repo_path) if repo_path is not None else "")
    if explicit_repo_path:
        path = Path(explicit_repo_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return _NotificationWorkspace(
            name=_first_text(workspace),
            kind="local",
            path=path.resolve(strict=False),
        )
    cleaned_workspace = _first_text(workspace)
    if cleaned_workspace:
        worker_workspace = _workspace_by_name(config, cleaned_workspace)
        return _NotificationWorkspace(
            name=worker_workspace.name,
            kind=worker_workspace.kind,
            path=worker_workspace.path,
            host=worker_workspace.host,
            remote_path=worker_workspace.remote_path,
        )
    return _NotificationWorkspace(name="", kind="local", path=config.root)


def _default_pr_notification_setup_payload(
    config: ProjectConfig,
    workspace: _NotificationWorkspace,
    *,
    interventions: list[dict[str, Any]],
    remote: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run default PR notification setup diagnostics without side effects."""
    remote_name = _first_text(remote) or "origin"
    payload: dict[str, Any] = {
        "project_id": config.project_id,
        "ok": False,
        "status": "checking",
        "workspace": _notification_workspace_to_dict(workspace),
        "repo": {
            "path": str(workspace.path),
            "remote": None,
            "branch": "",
        },
        "target": None,
        "auth": {
            "method": "gh",
            "checked": False,
            "authenticated": False,
            "error": "",
        },
        "posting": {
            "live_supported": False,
            "prepared_payload_supported": True,
            "mode": "prepared_payload",
        },
        "issues": [],
        "prepared_payload": _prepared_pr_notification_payload(
            interventions,
            target=None,
        ),
    }
    issues: list[dict[str, str]] = payload["issues"]
    if workspace.kind != "local":
        _add_notification_issue(
            issues,
            code="unsupported_sandbox",
            severity="error",
            message=(
                "PR notification setup checks currently require a local workspace path; "
                f"workspace {workspace.name or '(unnamed)'} is {workspace.kind!r}."
            ),
            remediation="Run the setup check from a local checkout or use prepared payload output.",
        )
        return _finalize_pr_notification_payload(payload)

    remote_check = _git_remote_check(workspace.path, remote_name, timeout_seconds)
    payload["repo"]["remote"] = remote_check["remote"] if remote_check["ok"] else None
    if not remote_check["ok"]:
        _add_notification_issue(
            issues,
            code="missing_remote",
            severity="error",
            message=remote_check["detail"],
            remediation=(
                f"Add a Git remote named {remote_name!r} that points at the GitHub "
                "repository for this worktree."
            ),
        )
        return _finalize_pr_notification_payload(payload)

    branch_check = _git_branch_check(workspace.path, timeout_seconds)
    payload["repo"]["branch"] = branch_check["branch"]
    if not branch_check["ok"]:
        _add_notification_issue(
            issues,
            code="missing_pr_association",
            severity="error",
            message=branch_check["detail"],
            remediation="Check out the task branch that has the associated pull request.",
        )
        return _finalize_pr_notification_payload(payload)

    pr_check = _gh_pr_check(
        workspace.path,
        timeout_seconds,
        remote_info=remote_check["remote"],
        branch=branch_check["branch"],
    )
    if not pr_check["ok"]:
        issue_code = (
            "missing_auth" if pr_check["reason"] == "missing_auth" else "missing_pr_association"
        )
        remediation = (
            "Authenticate the GitHub CLI for this checkout or use prepared payload output."
            if issue_code == "missing_auth"
            else "Open a pull request for the current branch or run the check from that branch."
        )
        _add_notification_issue(
            issues,
            code=issue_code,
            severity="error",
            message=pr_check["detail"],
            remediation=remediation,
        )
        return _finalize_pr_notification_payload(payload)

    payload["target"] = pr_check["target"]
    payload["prepared_payload"] = _prepared_pr_notification_payload(
        interventions,
        target=pr_check["target"],
    )

    auth_check = _gh_auth_check(workspace.path, timeout_seconds)
    payload["auth"] = auth_check
    if not auth_check["authenticated"]:
        _add_notification_issue(
            issues,
            code="missing_auth",
            severity="error",
            message=auth_check["error"] or "GitHub CLI authentication is unavailable.",
            remediation="Run `gh auth login` outside the sandbox or use prepared payload output.",
        )
        return _finalize_pr_notification_payload(payload)

    if _live_pr_notifications_enabled(config):
        payload["posting"]["live_supported"] = True
        payload["posting"]["mode"] = "live_comment"
    else:
        _add_notification_issue(
            issues,
            code="unsupported_sandbox",
            severity="warning",
            message=(
                "Live PR comments are not enabled for this environment; prepared "
                "payload output is available."
            ),
            remediation=(
                "Set notifications.github.allow_live to true only in an environment "
                "where posting PR comments is safe."
            ),
        )
    return _finalize_pr_notification_payload(payload)


def _notification_workspace_to_dict(workspace: _NotificationWorkspace) -> dict[str, str]:
    """Return a JSON-friendly notification workspace payload."""
    payload = {
        "name": workspace.name,
        "kind": workspace.kind,
        "path": str(workspace.path),
    }
    if workspace.host:
        payload["host"] = workspace.host
    if workspace.remote_path:
        payload["remote_path"] = workspace.remote_path
    return payload


def _finalize_pr_notification_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Set final status and ok fields on a PR notification diagnostic payload."""
    error_issues = [issue for issue in payload["issues"] if issue.get("severity") == "error"]
    payload["ok"] = not error_issues
    if error_issues:
        payload["status"] = str(error_issues[0]["code"])
    elif payload["issues"]:
        payload["status"] = str(payload["issues"][0]["code"])
    else:
        payload["status"] = "ok"
    return payload


def _add_notification_issue(
    issues: list[dict[str, str]],
    *,
    code: str,
    severity: str,
    message: str,
    remediation: str,
) -> None:
    """Append one normalized notification setup issue."""
    issues.append(
        {
            "code": code,
            "severity": severity,
            "message": message,
            "remediation": remediation,
        }
    )


def _git_remote_check(path: Path, remote: str, timeout_seconds: int) -> dict[str, Any]:
    """Return diagnostic information for a configured Git remote."""
    command = ["git", "-C", str(path), "remote", "get-url", remote]
    completed = _run_setup_check_command(command, path=path, timeout_seconds=timeout_seconds)
    remote_url = completed.stdout.strip()
    if completed.returncode != 0 or not remote_url:
        return {
            "ok": False,
            "detail": _command_failure_detail(completed, f"Git remote {remote!r} is missing."),
        }
    parsed = _parse_git_remote(remote_url)
    if parsed is None:
        return {
            "ok": False,
            "detail": f"Git remote {remote!r} is not a supported owner/repo URL: {remote_url}",
        }
    return {
        "ok": True,
        "remote": {
            "name": remote,
            "url": remote_url,
            **parsed,
        },
    }


def _git_branch_check(path: Path, timeout_seconds: int) -> _GitBranchCheck:
    """Return diagnostic information for the current Git branch."""
    command = ["git", "-C", str(path), "branch", "--show-current"]
    completed = _run_setup_check_command(command, path=path, timeout_seconds=timeout_seconds)
    branch = completed.stdout.strip()
    if completed.returncode != 0 or not branch:
        return {
            "ok": False,
            "branch": "",
            "detail": _command_failure_detail(
                completed,
                "Current checkout is detached or no branch is available.",
            ),
        }
    return {"ok": True, "branch": branch, "detail": ""}


def _gh_pr_check(
    path: Path,
    timeout_seconds: int,
    *,
    remote_info: dict[str, str],
    branch: str,
) -> dict[str, Any]:
    """Return diagnostic information for the PR associated with the branch."""
    command = [
        "gh",
        "pr",
        "view",
        branch,
        "--repo",
        _gh_repo_argument(remote_info),
        "--json",
        "number,url,headRefName,baseRefName,state",
    ]
    completed = _run_setup_check_command(command, path=path, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        detail = _command_failure_detail(
            completed,
            "No pull request is associated with the current branch.",
        )
        return {
            "ok": False,
            "reason": (
                "missing_auth"
                if completed.returncode in {126, 127} or _looks_like_auth_failure(detail)
                else "missing_pr"
            ),
            "detail": detail,
        }
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {
            "ok": False,
            "reason": "missing_pr",
            "detail": "GitHub CLI returned non-JSON PR data.",
        }
    number = data.get("number")
    url = _first_text(data.get("url"))
    if not isinstance(number, int) or not url:
        return {
            "ok": False,
            "reason": "missing_pr",
            "detail": "GitHub CLI did not return a PR number and URL.",
        }
    remote = _parse_git_remote(url)
    if remote is None:
        return {
            "ok": False,
            "reason": "missing_pr",
            "detail": "GitHub CLI returned a PR URL that does not identify a repository.",
        }
    mismatch = _remote_target_mismatch(remote_info, remote)
    if mismatch:
        return {
            "ok": False,
            "reason": "missing_pr",
            "detail": (
                "Resolved PR does not match selected remote "
                f"{remote_info['owner']}/{remote_info['repo']}: {mismatch}."
            ),
        }
    target: dict[str, Any] = {
        "kind": "pull_request",
        "number": number,
        "url": url,
        "head": _first_text(data.get("headRefName")),
        "base": _first_text(data.get("baseRefName")),
        "state": _first_text(data.get("state")),
        **remote,
    }
    return {"ok": True, "target": target, "detail": ""}


def _gh_auth_check(path: Path, timeout_seconds: int) -> dict[str, Any]:
    """Return GitHub CLI authentication diagnostics."""
    command = ["gh", "auth", "status"]
    completed = _run_setup_check_command(command, path=path, timeout_seconds=timeout_seconds)
    if completed.returncode == 0:
        return {
            "method": "gh",
            "checked": True,
            "authenticated": True,
            "error": "",
        }
    return {
        "method": "gh",
        "checked": True,
        "authenticated": False,
        "error": _command_failure_detail(
            completed,
            "GitHub CLI authentication is unavailable.",
        ),
    }


def _run_setup_check_command(
    command: list[str],
    *,
    path: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run a read-only setup diagnostic command."""
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            cwd=path,
            timeout=timeout_seconds or None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output_text(exc.stdout)
        stderr = _timeout_output_text(exc.stderr)
        if stderr:
            stderr += "\n"
        stderr += f"setup check command timed out after {exc.timeout} seconds\n"
        return subprocess.CompletedProcess(command, returncode=124, stdout=stdout, stderr=stderr)
    except FileNotFoundError as exc:
        return _setup_check_start_failed(command, returncode=127, exc=exc)
    except PermissionError as exc:
        return _setup_check_start_failed(command, returncode=126, exc=exc)
    except OSError as exc:
        return _setup_check_start_failed(command, returncode=1, exc=exc)


def _setup_check_start_failed(
    command: list[str],
    *,
    returncode: int,
    exc: OSError,
) -> subprocess.CompletedProcess[str]:
    """Return a failed completed process for setup command startup errors."""
    return subprocess.CompletedProcess(
        command,
        returncode=returncode,
        stdout="",
        stderr=f"setup check command failed to start: {exc}\n",
    )


def _command_failure_detail(
    completed: subprocess.CompletedProcess[str],
    fallback: str,
) -> str:
    """Return a concise command failure detail."""
    output = (completed.stderr or completed.stdout or "").strip()
    if output:
        return output.splitlines()[-1]
    return fallback


def _parse_git_remote(url: str) -> dict[str, str] | None:
    """Parse common GitHub remote/PR URLs into host, owner, and repo fields."""
    text = _first_text(url)
    if not text:
        return None
    host = ""
    path = ""
    if text.startswith("git@") and ":" in text:
        host_path = text.removeprefix("git@")
        host, path = host_path.split(":", 1)
    else:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    if not host or not path:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return {
        "host": host,
        "owner": parts[0],
        "repo": parts[1],
    }


def _gh_repo_argument(remote_info: dict[str, str]) -> str:
    """Return a GitHub CLI --repo argument for a parsed remote."""
    owner_repo = f"{remote_info['owner']}/{remote_info['repo']}"
    host = remote_info.get("host", "")
    return owner_repo if host in {"", "github.com"} else f"{host}/{owner_repo}"


def _remote_target_mismatch(
    remote_info: dict[str, str],
    target_info: dict[str, str],
) -> str:
    """Return a concise mismatch detail when a PR target differs from remote."""
    for key in ("host", "owner", "repo"):
        if remote_info.get(key) != target_info.get(key):
            return (
                f"{key} is {target_info.get(key) or '(missing)'} "
                f"instead of {remote_info.get(key) or '(missing)'}"
            )
    return ""


def _looks_like_auth_failure(text: str) -> bool:
    """Return whether command output looks like an auth/tooling failure."""
    lowered = text.lower()
    auth_needles = (
        "not logged",
        "authenticate",
        "authentication",
        "gh auth login",
        "oauth",
        "token",
    )
    return any(needle in lowered for needle in auth_needles)


def _prepared_pr_notification_payload(
    interventions: list[dict[str, Any]],
    *,
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a prepared PR comment payload for open intervention records."""
    lines = ["Agent tracker intervention notification"]
    if target:
        lines.append(f"Target PR: {target.get('url', '')}")
    if interventions:
        lines.append("")
        lines.append("Open interventions:")
        for intervention in interventions:
            summary = _first_text(intervention.get("summary")) or "(no summary)"
            task_id = _first_text(intervention.get("task_id"))
            suffix = f" for task {task_id}" if task_id else ""
            lines.append(f"- {intervention['id']}{suffix}: {summary}")
    else:
        lines.append("")
        lines.append("No open interventions are currently recorded.")
    lines.append("")
    lines.append("Managed by agent-tracker intervention notifications.")
    return {
        "surface": "pull_request_comment",
        "body": "\n".join(lines),
        "intervention_ids": [str(item["id"]) for item in interventions],
        "interventions": interventions,
    }


def _notification_target_key(target: Any) -> str:
    """Return a stable delivery key for a notification target."""
    if not isinstance(target, dict):
        return ""
    host = _first_text(target.get("host")) or "github.com"
    owner = _first_text(target.get("owner"))
    repo = _first_text(target.get("repo"))
    number = target.get("number")
    if not owner or not repo or not isinstance(number, int):
        return ""
    return f"github:{host}/{owner}/{repo}#pull/{number}"


def _notification_payload_hash(payload: dict[str, Any]) -> str:
    """Return a deterministic content hash for one notification payload."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _prepared_pr_notification_path(
    config: ProjectConfig,
    override: str | Path | None,
) -> Path:
    """Return the configured prepared PR notification payload path."""
    value = _first_text(str(override) if override is not None else "")
    if not value:
        notifications = config.raw.get("notifications", {})
        github = notifications.get("github", {}) if isinstance(notifications, dict) else {}
        if isinstance(github, dict):
            value = _first_text(github.get("prepared_payload_path"))
    path = Path(value or "exports/pr-notification.json").expanduser()
    if not path.is_absolute():
        path = config.effective_state_root / path
    return path


def _live_export_satisfied(
    existing: NotificationDeliveryRecord | None,
    payload_hash: str,
) -> bool:
    """Return whether an existing live PR comment already has this payload."""
    if existing is None:
        return False
    return (
        existing.payload_hash == payload_hash
        and existing.status in {"posted", "updated"}
        and bool(existing.comment_id)
    )


def _prepared_export_satisfied(
    existing: NotificationDeliveryRecord | None,
    payload_hash: str,
    path: Path,
) -> bool:
    """Return whether an existing prepared payload file already has this payload."""
    if existing is None or existing.status != "prepared":
        return False
    return (
        existing.payload_hash == payload_hash
        and existing.metadata.get("prepared_payload_path") == str(path)
        and path.exists()
    )


def _post_pr_notification(
    target: dict[str, Any],
    prepared_payload: dict[str, Any],
    *,
    existing: NotificationDeliveryRecord | None,
    timeout_seconds: int,
    path: Path,
) -> dict[str, str]:
    """Create or update the PR comment for a prepared notification payload."""
    comment_id = _first_text(existing.comment_id if existing else "")
    body = _first_text(prepared_payload.get("body"))
    if not body:
        raise RuntimeError("notification payload body is empty")
    repo_path = _gh_issue_comments_path(target)
    command = _gh_api_command(target, repo_path, body=body)
    status = "posted"
    action = "created"
    if comment_id:
        command = _gh_api_command(
            target,
            f"repos/{target['owner']}/{target['repo']}/issues/comments/{quote(comment_id)}",
            method="PATCH",
            body=body,
        )
        status = "updated"
        action = "updated"
    completed = _run_setup_check_command(command, path=path, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        raise RuntimeError(
            _command_failure_detail(completed, "GitHub CLI failed to post PR notification.")
        )
    if not comment_id:
        try:
            response = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub CLI returned non-JSON comment data.") from exc
        raw_comment_id = response.get("id")
        comment_id = str(raw_comment_id) if raw_comment_id else ""
        if not comment_id:
            raise RuntimeError("GitHub CLI did not return a PR comment id.")
    return {"action": action, "status": status, "comment_id": comment_id}


def _gh_api_command(
    target: dict[str, Any],
    path: str,
    *,
    body: str,
    method: str = "POST",
) -> list[str]:
    """Return a GitHub CLI API command for an issue comment operation."""
    command = ["gh", "api", path.lstrip("/")]
    host = _first_text(target.get("host"))
    if host and host != "github.com":
        command.extend(["--hostname", host])
    if method != "POST":
        command.extend(["-X", method])
    command.extend(["-f", f"body={body}"])
    return command


def _gh_issue_comments_path(target: dict[str, Any]) -> str:
    """Return the GitHub API path for PR issue comments."""
    return f"repos/{target['owner']}/{target['repo']}/issues/{target['number']}/comments"


def _utc_timestamp() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _live_pr_notifications_enabled(config: ProjectConfig) -> bool:
    """Return whether config explicitly permits live PR comments."""
    notifications = config.raw.get("notifications", {})
    if not isinstance(notifications, dict):
        return False
    github = notifications.get("github", {})
    if not isinstance(github, dict):
        return False
    return github.get("allow_live") is True


def _worker_command_argv(
    workspace: _WorkerWorkspace,
    command: list[str] | None,
    placeholders: dict[str, str],
) -> list[str]:
    """Return the command argv for a worker launch."""
    raw_command = command or workspace.worker_command or _default_worker_command()
    rendered = [_render_template_arg(arg, placeholders) for arg in raw_command]
    if not rendered:
        raise ValueError("worker command is empty")
    return rendered


def _worker_launch_dir(
    config: ProjectConfig,
    workspace: _WorkerWorkspace,
    launch_id: str,
) -> Path:
    """Return the local artifact directory for a worker launch."""
    if workspace.kind == "ssh":
        return (
            config.effective_state_root
            / "workers"
            / _launch_id_component(workspace.name, fallback="workspace")
            / launch_id
        )
    return workspace.artifacts_path / launch_id


def _ssh_worker_remote_artifacts(
    workspace: _WorkerWorkspace,
    launch_id: str,
) -> dict[str, str]:
    """Return remote artifact paths for an SSH worker launch."""
    if workspace.kind != "ssh":
        return {}
    launch_dir = _join_remote_path(workspace.artifacts_path.as_posix(), launch_id)
    return {
        "directory": launch_dir,
        "prompt": _join_remote_path(launch_dir, "prompt.md"),
        "report": _join_remote_path(launch_dir, "report.md"),
        "stdout": _join_remote_path(launch_dir, "stdout.txt"),
        "stderr": _join_remote_path(launch_dir, "stderr.txt"),
    }


def _join_remote_path(base: str, *parts: str) -> str:
    """Join POSIX-style remote path components."""
    return posixpath.normpath(posixpath.join(base, *parts))


def _write_task_ingest_claim_request(
    config: ProjectConfig,
    *,
    launch_id: str,
    actor: str,
    role: str,
    task_id: str,
    lease_seconds: int,
) -> dict[str, str]:
    """Write a task-ingest claim request for an SSH worker launch."""
    paths = _task_command_paths(config)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)
    command_id = f"{_launch_id_component(launch_id, fallback='launch')}-claim"
    request_path = paths.inbox / f"{command_id}.json"
    response_path = _default_task_command_response_path(paths, command_id)
    request = {
        "schema_version": TASK_INGEST_SCHEMA_VERSION,
        "command_id": command_id,
        "idempotency_key": f"{actor}:claim:{task_id}:{launch_id}",
        "project_id": config.project_id,
        "command": "claim",
        "actor": {"id": actor, "role": role or "worker"},
        "task_id": task_id,
        "lease_token": "",
        "payload": {"lease_seconds": lease_seconds},
        "reply_to": str(response_path),
    }
    _write_spool_file_atomic(
        (json.dumps(request, indent=2, sort_keys=True) + "\n").encode(),
        request_path,
    )
    return {
        "claim_request": str(request_path),
        "claim_response": str(response_path),
        "command_id": command_id,
    }


def _default_worker_command() -> list[str]:
    """Return the default local Codex one-shot command."""
    return [
        "codex",
        "exec",
        "--cd",
        "{worktree_path}",
        "--output-last-message",
        "{report_path}",
        "-",
    ]


def _render_template_arg(arg: str, placeholders: dict[str, str]) -> str:
    """Render one command argument placeholder."""
    rendered = arg
    for key, value in placeholders.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _run_worker_command(
    command: list[str],
    *,
    workspace: _WorkerWorkspace,
    prompt: str,
    report_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run a worker command in a local workspace."""
    process_env = _worker_process_env(env)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=Path(env["worktree_path"]).expanduser(),
            env={**os.environ, **process_env},
            timeout=timeout_seconds or None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output_text(exc.stdout)
        stderr = _timeout_output_text(exc.stderr)
        if stderr:
            stderr += "\n"
        stderr += f"worker command timed out after {exc.timeout} seconds\n"
        completed = subprocess.CompletedProcess(
            command,
            returncode=124,
            stdout=stdout,
            stderr=stderr,
        )
    except FileNotFoundError as exc:
        completed = _worker_start_failed(command, returncode=127, exc=exc)
    except PermissionError as exc:
        completed = _worker_start_failed(command, returncode=126, exc=exc)
    except OSError as exc:
        completed = _worker_start_failed(command, returncode=1, exc=exc)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if not report_path.exists():
        report_text = completed.stdout or "Worker command produced no report.\n"
        report_path.write_text(report_text, encoding="utf-8")
    return completed


def _worker_process_env(env: dict[str, str]) -> dict[str, str]:
    """Return environment variables exposed to worker commands."""
    return {
        "AGENT_TRACKER_WORKER_AGENT_ID": env["agent_id"],
        "AGENT_TRACKER_WORKER_BASE_REF": env["base_ref"],
        "AGENT_TRACKER_WORKER_BRANCH": env["branch"],
        "AGENT_TRACKER_WORKER_LAUNCH_ID": env["launch_id"],
        "AGENT_TRACKER_WORKER_PROMPT": env["prompt_path"],
        "AGENT_TRACKER_WORKER_REPORT": env["report_path"],
        "AGENT_TRACKER_WORKER_TASK_ID": env["task_id"],
        "AGENT_TRACKER_WORKER_WORKSPACE": env["workspace"],
        "AGENT_TRACKER_WORKER_WORKTREE": env["worktree_path"],
    }


def _run_ssh_worker_command(
    config: ProjectConfig,
    workspace: _WorkerWorkspace,
    command: list[str],
    *,
    prompt: str,
    report_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run a worker command in an SSH workspace and collect local artifacts."""
    try:
        return asyncio.run(
            _run_ssh_worker_command_async(
                config,
                workspace,
                command,
                prompt=prompt,
                report_path=report_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        )
    except asyncio.TimeoutError as exc:
        stdout = _timeout_output_text(getattr(exc, "stdout", None))
        stderr = _timeout_output_text(getattr(exc, "stderr", None))
        if stderr:
            stderr += "\n"
        stderr += f"worker command timed out after {timeout_seconds} seconds\n"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if not report_path.exists():
            report_path.write_text(
                stdout or "Worker command produced no report.\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, returncode=124, stdout=stdout, stderr=stderr)


async def _run_ssh_worker_command_async(
    config: ProjectConfig,
    workspace: _WorkerWorkspace,
    command: list[str],
    *,
    prompt: str,
    report_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run one SSH worker command and collect its local report/log artifacts."""
    asyncssh = _load_asyncssh()
    remote_artifacts = _ssh_worker_remote_artifacts(workspace, env["launch_id"])
    command_text = _ssh_worker_command_text(command, env)
    connect_kwargs = _ssh_workspace_connect_kwargs(workspace)
    async with asyncssh.connect(
        workspace.host,
        port=workspace.port,
        **connect_kwargs,
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(remote_artifacts["directory"], exist_ok=True)
            await _write_sftp_text(sftp, remote_artifacts["prompt"], prompt)
    async with asyncssh.connect(
        workspace.host,
        port=workspace.port,
        **connect_kwargs,
    ) as conn:
        completed = await conn.run(
            command_text,
            input=prompt,
            check=False,
            timeout=timeout_seconds or None,
        )
    stdout = _ssh_output_text(completed.stdout)
    stderr = _ssh_output_text(completed.stderr)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    async with asyncssh.connect(
        workspace.host,
        port=workspace.port,
        **connect_kwargs,
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            report_text = await _read_sftp_text_or_none(
                asyncssh,
                sftp,
                remote_artifacts["report"],
            )
    report_path.write_text(
        (
            report_text
            if report_text is not None
            else stdout or "Worker command produced no report.\n"
        ),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(
        command,
        returncode=int(completed.returncode if completed.returncode is not None else 1),
        stdout=stdout,
        stderr=stderr,
    )


def _ssh_worker_command_text(command: list[str], env: dict[str, str]) -> str:
    """Return an SSH shell command with explicit cwd and worker environment."""
    exports = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in _worker_process_env(env).items()
    )
    argv = " ".join(shlex.quote(item) for item in command)
    worktree = shlex.quote(env["worktree_path"])
    return f"cd {worktree} && env {exports} {argv}"


def _ssh_workspace_connect_kwargs(workspace: _WorkerWorkspace) -> dict[str, Any]:
    """Return AsyncSSH connection kwargs for a worker workspace."""
    kwargs: dict[str, Any] = {}
    if workspace.username:
        kwargs["username"] = workspace.username
    if workspace.password is not None:
        kwargs["password"] = workspace.password
    if workspace.known_hosts is None or workspace.known_hosts:
        kwargs["known_hosts"] = workspace.known_hosts
    if workspace.client_keys is not None:
        kwargs["client_keys"] = workspace.client_keys
    return kwargs


async def _write_sftp_text(sftp: Any, remote_path: str, text: str) -> None:
    """Write one text file through an AsyncSSH SFTP client."""
    async with sftp.open(remote_path, "w") as remote_file:
        await remote_file.write(text)


async def _read_sftp_text_or_none(
    asyncssh: Any,
    sftp: Any,
    remote_path: str,
) -> str | None:
    """Read one SFTP text file, returning ``None`` when absent."""
    try:
        async with sftp.open(remote_path, "r") as remote_file:
            return await remote_file.read()
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
        return None


def _ssh_output_text(value: str | bytes | None) -> str:
    """Return AsyncSSH command output as text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _worker_start_failed(
    command: list[str],
    *,
    returncode: int,
    exc: OSError,
) -> subprocess.CompletedProcess[str]:
    """Return a failed completed process for command startup errors."""
    return subprocess.CompletedProcess(
        command,
        returncode=returncode,
        stdout="",
        stderr=f"worker command failed to start: {exc}\n",
    )


def _timeout_output_text(value: str | bytes | None) -> str:
    """Return captured timeout output as text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _launch_id_component(value: str, *, fallback: str) -> str:
    """Return a path-safe launch ID component."""
    cleaned = _first_text(value)
    chars = [char.lower() if char.isalnum() else "-" for char in cleaned]
    component = "-".join(part for part in "".join(chars).split("-") if part)
    return component or fallback


def _write_worker_launch_event(workspace: _WorkerWorkspace, result: dict[str, Any]) -> None:
    """Publish a worker launch event into a workspace outbox."""
    if workspace.spool_outbox is None:
        return
    workspace.spool_outbox.mkdir(parents=True, exist_ok=True)
    launch_id = _launch_id_component(str(result["launch_id"]), fallback="launch")
    event_path = workspace.spool_outbox / f"{launch_id}.json"
    payload = {
        "event_id": f"worker-launch-{launch_id}",
        "kind": "agent_tracker.worker_launch",
        "task_id": result.get("task_id", ""),
        "workspace": workspace.name,
        "status": result.get("status", ""),
        "artifact": f"file:{result['artifacts']['launch']}",
        "report": f"file:{result['artifacts']['report']}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_spool_file_atomic(json.dumps(payload, indent=2).encode(), event_path)


def _write_ssh_worker_launch_event(
    config: ProjectConfig,
    workspace: _WorkerWorkspace,
    result: dict[str, Any],
) -> None:
    """Publish a worker launch event into an SSH workspace outbox."""
    try:
        asyncio.run(_write_ssh_worker_launch_event_async(config, workspace, result))
    except asyncio.TimeoutError as exc:
        raise TimeoutError("timed out writing SSH worker launch event") from exc


async def _write_ssh_worker_launch_event_async(
    config: ProjectConfig,
    workspace: _WorkerWorkspace,
    result: dict[str, Any],
) -> None:
    """Write one SSH worker launch event through SFTP."""
    if workspace.spool_outbox is None:
        return
    asyncssh = _load_asyncssh()
    launch_id = _launch_id_component(str(result["launch_id"]), fallback="launch")
    event_path = _join_remote_path(workspace.spool_outbox.as_posix(), f"{launch_id}.json")
    payload = {
        "event_id": f"worker-launch-{launch_id}",
        "kind": "agent_tracker.worker_launch",
        "task_id": result.get("task_id", ""),
        "workspace": workspace.name,
        "status": result.get("status", ""),
        "artifact": f"file:{result['artifacts']['launch']}",
        "report": f"file:{result['artifacts']['report']}",
        "remote_report": f"file:{result.get('remote_artifacts', {}).get('report', '')}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with asyncssh.connect(
        workspace.host,
        port=workspace.port,
        **_ssh_workspace_connect_kwargs(workspace),
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(workspace.spool_outbox.as_posix(), exist_ok=True)
            temp_path = f"{event_path}.tmp"
            await _write_sftp_text(sftp, temp_path, json.dumps(payload, indent=2) + "\n")
            await sftp.rename(temp_path, event_path)


def _is_spool_event_file(path: Path) -> bool:
    """Return whether a remote spool path is a complete JSON event file."""
    return _is_spool_event_name(path.name)


def _is_spool_event_name(name: str) -> bool:
    """Return whether a remote spool filename is a complete JSON event file."""
    partial_suffixes = {".partial", ".part", ".tmp"}
    if any(name.endswith(suffix) for suffix in partial_suffixes):
        return False
    return Path(name).suffix == ".json"


def _copy_spool_file_atomic(source: Path, target: Path) -> None:
    """Copy a spool file to a temporary name before publishing it as JSON."""
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_spool_file_atomic(data: bytes, target: Path) -> None:
    """Write bytes to a temporary non-JSON name before publishing them."""
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _is_ssh_spool_value(value: Any) -> bool:
    """Return whether a spool remote value names an SSH/SFTP transport."""
    text = _first_text(value)
    if not text:
        return False
    return urlsplit(text).scheme.lower() in _SSH_SPOOL_SCHEMES


def _parse_sftp_spool_config(config: ProjectConfig, remote_value: str) -> _SftpSpoolConfig:
    """Parse an SSH/SFTP remote spool config value and optional SSH settings."""
    parsed = urlsplit(remote_value)
    scheme = parsed.scheme.lower()
    if scheme not in _SSH_SPOOL_SCHEMES:
        raise ValueError("spool.remote_inbox must start with ssh:// or sftp://")
    if not parsed.hostname:
        raise ValueError("spool.remote_inbox SSH URI must include a host")
    try:
        port = parsed.port or 22
    except ValueError as exc:
        raise ValueError("spool.remote_inbox SSH URI has an invalid port") from exc
    remote_path = unquote(parsed.path or "")
    if not remote_path:
        raise ValueError("spool.remote_inbox SSH URI must include an absolute path")
    if not remote_path.startswith("/"):
        raise ValueError("spool.remote_inbox SSH URI path must be absolute")

    spool = config.raw.get("spool", {})
    ssh_options = spool.get("ssh", {}) if isinstance(spool, dict) else {}
    if not isinstance(ssh_options, dict):
        ssh_options = {}

    username = unquote(parsed.username or "") or _first_text(ssh_options.get("username"))
    password = unquote(parsed.password or "") or _first_text(ssh_options.get("password")) or None
    known_hosts = _known_hosts_option(config, ssh_options.get("known_hosts"))
    client_keys = _client_key_options(config, ssh_options.get("client_keys"))
    return _SftpSpoolConfig(
        host=parsed.hostname,
        port=port,
        remote_path=remote_path,
        display_inbox=_redacted_uri(parsed),
        username=username,
        password=password,
        known_hosts=known_hosts,
        client_keys=client_keys,
    )


def _known_hosts_option(config: ProjectConfig, value: Any) -> str | None:
    """Return an AsyncSSH known_hosts option from config."""
    text = _first_text(value)
    if not text:
        return ""
    if text.lower() in _DISABLED_KNOWN_HOSTS:
        return None
    return str(_resolve_config_path(config, text))


def _client_key_options(config: ProjectConfig, value: Any) -> list[str] | None:
    """Return AsyncSSH client key paths from config."""
    if value is None:
        return None
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return None
    paths = [str(_resolve_config_path(config, item)) for item in values if _first_text(item)]
    return paths or None


def _resolve_config_path(config: ProjectConfig, value: str) -> Path:
    """Resolve a path-like SSH option relative to the config directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else config.root / path


def _redacted_uri(parsed: Any) -> str:
    """Return a URI suitable for result payloads without leaking passwords."""
    username = unquote(parsed.username or "")
    netloc = parsed.hostname or ""
    if username:
        netloc = f"{quote(username)}@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _join_sftp_display(base: str, name: str) -> str:
    """Join an SFTP display URI and filename."""
    parsed = urlsplit(base)
    joined = posixpath.join(parsed.path, name)
    return urlunsplit((parsed.scheme, parsed.netloc, joined, "", ""))


async def _pull_spool_sftp(
    config: ProjectConfig,
    remote_value: str,
    local_inbox: Path,
    done: Path,
    error: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Copy complete remote spool files from an SSH/SFTP source."""
    asyncssh = _load_asyncssh()
    sftp_config = _parse_sftp_spool_config(config, remote_value)
    result = _pull_spool_result(sftp_config.display_inbox, local_inbox, dry_run=dry_run)
    connect_kwargs: dict[str, Any] = {}
    if sftp_config.username:
        connect_kwargs["username"] = sftp_config.username
    if sftp_config.password is not None:
        connect_kwargs["password"] = sftp_config.password
    if sftp_config.known_hosts is None or sftp_config.known_hosts:
        connect_kwargs["known_hosts"] = sftp_config.known_hosts
    if sftp_config.client_keys is not None:
        connect_kwargs["client_keys"] = sftp_config.client_keys
    async with asyncssh.connect(
        sftp_config.host,
        port=sftp_config.port,
        **connect_kwargs,
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            try:
                if not await sftp.isdir(sftp_config.remote_path):
                    raise ValueError(
                        f"spool.remote_inbox is not a directory: {sftp_config.display_inbox}"
                    )
            except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
                return result

            for name in sorted(await sftp.listdir(sftp_config.remote_path)):
                if name in {".", ".."}:
                    continue
                if not _is_safe_spool_entry_name(name):
                    result["skipped"] += 1
                    continue
                remote_path = posixpath.join(sftp_config.remote_path, name)
                if await sftp.isdir(remote_path) or not _is_spool_event_name(name):
                    result["skipped"] += 1
                    continue
                target = local_inbox / name
                item = {
                    "source": _join_sftp_display(sftp_config.display_inbox, name),
                    "target": str(target),
                }
                remote_bytes: bytes | None = None
                existing_candidates = [
                    (target, "skip_existing", "conflict"),
                    (done / name, "skip_done", "conflict_done"),
                    (error / name, "skip_error", "conflict_error"),
                ]
                handled = False
                for existing, skip_action, conflict_action in existing_candidates:
                    if not existing.exists():
                        continue
                    remote_bytes = await _read_sftp_file(sftp, remote_path)
                    if existing.is_file() and existing.read_bytes() == remote_bytes:
                        result["skipped"] += 1
                        result["files"].append(
                            {**item, "existing": str(existing), "action": skip_action}
                        )
                    else:
                        result["conflicts"] += 1
                        result["files"].append(
                            {**item, "existing": str(existing), "action": conflict_action}
                        )
                    handled = True
                    break
                if handled:
                    continue
                result["processed"] += 1
                result["files"].append({**item, "action": "copy"})
                if not dry_run:
                    if remote_bytes is None:
                        remote_bytes = await _read_sftp_file(sftp, remote_path)
                    _write_spool_file_atomic(remote_bytes, target)
                    result["copied"] += 1
    return result


async def _read_sftp_file(sftp: Any, remote_path: str) -> bytes:
    """Read one remote SFTP file as bytes."""
    async with sftp.open(remote_path, "rb") as remote_file:
        return await remote_file.read()


def _is_safe_spool_entry_name(name: str) -> bool:
    """Return whether a remote entry name is safe as a local filename."""
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    if posixpath.isabs(name) or Path(name).is_absolute():
        return False
    return posixpath.basename(name) == name


def _load_asyncssh() -> Any:
    """Import AsyncSSH lazily for optional SSH spool support."""
    try:
        return importlib.import_module("asyncssh")
    except ModuleNotFoundError as exc:
        raise ImportError(
            "SSH spool transport requires the optional 'ssh' extra; "
            "install it with `agent-tracker[ssh]` or run `uv run --extra ssh ...`"
        ) from exc
