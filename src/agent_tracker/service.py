"""High-level coordinator service."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from agent_tracker.config import ProjectConfig
from agent_tracker.db import Store, state_to_dict
from agent_tracker.models import Claim, EventRecord, TaskState
from agent_tracker.plugins import load_plugin
from agent_tracker.rendering import DefaultPromptRenderer


class Coordinator:
    """Coordinate tasks for one configured project."""

    def __init__(self, config: ProjectConfig, db_path: str | Path | None = None):
        self.config = config
        self.store = Store(db_path or config.db_path)

    def init(self) -> None:
        """Initialize storage for the configured project."""
        self.store.upsert_project(self.config)

    def import_tasks(self) -> int:
        """Import configured tasks through the configured importer."""
        importer = load_plugin(self.config, "importer", "agent_tracker.importers:JsonTaskImporter")
        tasks, dependencies = importer.load_tasks(self.config)
        self.store.import_tasks(self.config, tasks, dependencies)
        return len(tasks)

    def task_states(self) -> list[TaskState]:
        """Return all evaluated task states."""
        return self.store.task_states(self.config.project_id)

    def ready_tasks(self, *, limit: int = 0, repo: str = "", role: str = "") -> list[TaskState]:
        """Return ready tasks matching optional filters."""
        states = [state for state in self.task_states() if state.state == "ready"]
        if repo:
            states = [state for state in states if state.task.repo == repo]
        if role:
            states = [state for state in states if _role_matches(state, role)]
        return states[:limit] if limit else states

    def get_task(self, task_id: str) -> TaskState:
        """Return one evaluated task state."""
        return self.store.get_task_state(self.config.project_id, task_id)

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
    ) -> None:
        """Complete a leased task."""
        self.store.complete_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            evidence=evidence or [],
            agent_id=agent_id,
        )

    def fail(self, task_id: str, *, lease_token: str, reason: str, agent_id: str = "") -> None:
        """Fail a leased task."""
        self.store.fail_task(
            self.config.project_id,
            task_id,
            lease_token=lease_token,
            reason=reason,
            agent_id=agent_id,
        )

    def record_evidence(self, task_id: str, uri: str, *, actor: str = "system") -> bool:
        """Record evidence for a task."""
        return self.store.record_evidence(self.config.project_id, task_id, uri, actor=actor)

    def record_event(self, payload: dict[str, Any], *, actor: str = "system") -> bool:
        """Normalize and record an event."""
        adapter = load_plugin(self.config, "event_adapter")
        if adapter is None:
            event = EventRecord(
                event_id=str(payload.get("event_id") or payload.get("id")),
                kind=str(payload.get("kind", "event")),
                task_id=str(payload.get("task_id", "")),
                payload=payload,
            )
        else:
            event = adapter.normalize_event(self.config, payload)
        if not event.event_id:
            raise ValueError("event payload must include event_id or id")
        return self.store.record_event(self.config.project_id, event, actor=actor)

    def ingest_event_file(self, path: str | Path, *, actor: str = "system") -> bool:
        """Record an event from a JSON file."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.record_event(payload, actor=actor)

    def ingest_spool(self, *, actor: str = "system") -> dict[str, int]:
        """Ingest configured spool JSON files."""
        spool = self.config.raw.get("spool", {})
        inbox = self.config.resolve_path("spool_inbox", "") if "spool_inbox" in self.config.raw else None
        done = self.config.resolve_path("spool_done", "") if "spool_done" in self.config.raw else None
        error = self.config.resolve_path("spool_error", "") if "spool_error" in self.config.raw else None
        if isinstance(spool, dict) and spool:
            inbox = self.config.root / spool.get("inbox", "") if not Path(spool.get("inbox", "")).is_absolute() else Path(spool.get("inbox", ""))
            done = self.config.root / spool.get("done", "") if not Path(spool.get("done", "")).is_absolute() else Path(spool.get("done", ""))
            error = self.config.root / spool.get("error", "") if not Path(spool.get("error", "")).is_absolute() else Path(spool.get("error", ""))
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

    def render_prompt(self, task_id: str, *, markdown: bool = False) -> str:
        """Render a prompt for a task."""
        state = self.get_task(task_id)
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
        exporter = load_plugin(self.config, "exporter", "agent_tracker.exporters:JsonSnapshotExporter")
        snapshot = self.store.snapshot(self.config.project_id)
        return exporter.export(self.config, snapshot)

    def status_payload(self) -> dict[str, Any]:
        """Return a JSON-friendly status payload."""
        states = self.task_states()
        return {
            "project_id": self.config.project_id,
            "name": self.config.name,
            "db_path": str(self.store.path),
            "tasks": [state_to_dict(state) for state in states],
            "ready": [state.task.task_id for state in states if state.state == "ready"],
            "active": [
                state.task.task_id
                for state in states
                if state.state in {"claimed", "in_progress", "waiting_evidence"}
            ],
            "blocked": [state.task.task_id for state in states if state.state == "blocked"],
        }


def _role_matches(state: TaskState, role: str) -> bool:
    roles = state.task.metadata.get("roles") or state.task.metadata.get("allowed_roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return role in roles

