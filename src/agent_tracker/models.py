"""Data models used by the generic coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MANUAL_STATES = {
    "pending",
    "claimed",
    "in_progress",
    "waiting_evidence",
    "awaiting_review",
    "awaiting_pr",
    "awaiting_merge",
    "awaiting_integration",
    "done",
    "failed",
    "deferred",
    "cancelled",
}

ACTIVE_STATES = {"claimed", "in_progress", "waiting_evidence"}
REVIEW_STATES = {"awaiting_review"}
INTEGRATION_STATES = {"awaiting_pr", "awaiting_merge", "awaiting_integration"}
AWAITING_STATES = REVIEW_STATES | INTEGRATION_STATES
TERMINAL_STATES = {"done", "failed", "cancelled"}


@dataclass(frozen=True)
class DependencyRecord:
    """A task dependency on another task in the same project."""

    task_id: str
    dependency_task_id: str
    description: str = ""


@dataclass
class TaskRecord:
    """A generic task record independent of any one project."""

    task_id: str
    title: str
    repo: str = ""
    status: str = "pending"
    priority: int = 9999
    prompt_key: str = ""
    prompt_path: str = ""
    summary: str = ""
    execution: dict[str, Any] = field(default_factory=dict)
    validation_checks: list[str] = field(default_factory=list)
    next_action: str = ""
    evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequirementState:
    """Evaluated dependency state."""

    description: str
    satisfied: bool
    detail: str


@dataclass
class TaskState:
    """Task plus computed queue state and dependency details."""

    task: TaskRecord
    state: str
    requirements: list[RequirementState] = field(default_factory=list)
    lease_agent_id: str = ""
    lease_token: str = ""
    lease_expires_at: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def outstanding_requirements(self) -> list[RequirementState]:
        """Return unsatisfied dependency states."""
        return [item for item in self.requirements if not item.satisfied]


@dataclass(frozen=True)
class Claim:
    """Successful task claim."""

    project_id: str
    task_id: str
    lease_token: str
    lease_expires_at: str
    agent_id: str


@dataclass(frozen=True)
class EventRecord:
    """Normalized event to ingest into the coordinator."""

    event_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""


@dataclass(frozen=True)
class IntakeRecord:
    """Raw project intake that is not a claimable task."""

    intake_id: str
    text: str
    kind: str = "idea"
    source: str = ""
    repo: str = ""
    status: str = "open"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ProposedTaskRecord:
    """A reviewed task proposal that is not yet live queue state."""

    proposal_id: str
    intake_id: str
    task: TaskRecord
    requirements: list[dict[str, str]] = field(default_factory=list)
    status: str = "proposed"
    created_at: str = ""
    updated_at: str = ""
