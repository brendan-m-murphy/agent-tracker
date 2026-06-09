"""High-level coordinator service."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from agent_tracker.config import ProjectConfig
from agent_tracker.db import Store, state_to_dict
from agent_tracker.models import (
    ACTIVE_STATES,
    INTEGRATION_STATES,
    REVIEW_STATES,
    Claim,
    EventRecord,
    RequirementState,
    TaskState,
)
from agent_tracker.plugins import load_plugin
from agent_tracker.rendering import DefaultPromptRenderer


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

    def record_event(self, payload: dict[str, Any], *, actor: str = "system") -> bool:
        """Normalize and record an event."""
        self._ensure_mutation_allowed()
        if not isinstance(payload, dict):
            raise ValueError("event payload must be a JSON object")
        adapter = load_plugin(self.config, "event_adapter")
        if adapter is None:
            event = EventRecord(
                event_id=_first_text(payload.get("event_id"), payload.get("id")),
                kind=_first_text(payload.get("kind")) or "event",
                task_id=_first_text(payload.get("task_id")),
                payload=payload,
            )
        else:
            event = adapter.normalize_event(self.config, payload)
        event_id = event.event_id.strip()
        if not event_id:
            raise ValueError("event payload must include event_id or id")
        kind = event.kind.strip() or "event"
        task_id = event.task_id.strip()
        event = EventRecord(event_id=event_id, kind=kind, task_id=task_id, payload=event.payload)
        return self.store.record_event(self.config.project_id, event, actor=actor)

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
        remote_inbox = _spool_path(self.config, spool.get("remote_inbox"))
        local_inbox, done, error = _local_spool_paths(self.config)
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


def _top_level_state_path(config: ProjectConfig, key: str) -> Path | None:
    """Resolve an optional top-level path config value."""
    if key not in config.raw:
        return None
    value = _first_text(config.raw.get(key))
    return config.resolve_state_path(key) if value else None


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


def _pull_spool_result(
    remote_inbox: Path,
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


def _is_spool_event_file(path: Path) -> bool:
    """Return whether a remote spool path is a complete JSON event file."""
    partial_suffixes = {".partial", ".part", ".tmp"}
    if any(path.name.endswith(suffix) for suffix in partial_suffixes):
        return False
    return path.suffix == ".json"


def _copy_spool_file_atomic(source: Path, target: Path) -> None:
    """Copy a spool file to a temporary name before publishing it as JSON."""
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
