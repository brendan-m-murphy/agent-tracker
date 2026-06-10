"""SQLite persistence for the generic coordinator."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_tracker.config import ProjectConfig
from agent_tracker.models import (
    ACTIVE_STATES,
    INTAKE_STATES,
    INTEGRATION_STATES,
    INTERVENTION_REASONS,
    MANUAL_STATES,
    PROPOSAL_STATES,
    REVIEW_STATES,
    Claim,
    DependencyRecord,
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

DB_SCHEMA_VERSION = 1
DB_SCHEMA_VERSION_KEY = "db_schema_version"


def utcnow() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """Return an ISO timestamp."""
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime | None:
    """Parse an ISO timestamp, returning None for empty or invalid values."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def dumps(value: Any) -> str:
    """Serialize JSON consistently."""
    return json.dumps(value, sort_keys=True)


def loads(value: str | None, default: Any) -> Any:
    """Deserialize JSON, returning default for blank values."""
    if not value:
        return default
    return json.loads(value)


class Store:
    """SQLite-backed project coordination store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        """Open a configured SQLite connection."""
        if read_only:
            uri = f"{self.path.expanduser().resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = False,
        read_only: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Open a transaction."""
        if immediate and read_only:
            raise ValueError("read-only transactions cannot be immediate")
        conn = self.connect(read_only=read_only)
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create database tables."""
        with self.transaction(immediate=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    repo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 9999,
                    prompt_key TEXT NOT NULL DEFAULT '',
                    prompt_path TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    execution_json TEXT NOT NULL DEFAULT '{}',
                    validation_json TEXT NOT NULL DEFAULT '[]',
                    next_action TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    lease_agent_id TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, task_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS dependencies (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    dependency_task_id TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, task_id, dependency_task_id),
                    FOREIGN KEY(project_id, task_id) REFERENCES tasks(project_id, task_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, task_id, uri),
                    FOREIGN KEY(project_id, task_id) REFERENCES tasks(project_id, task_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    project_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, event_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS intake (
                    project_id TEXT NOT NULL,
                    intake_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    repo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    text TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, intake_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS proposed_tasks (
                    project_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    intake_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    contract_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, proposal_id),
                    UNIQUE (project_id, task_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id, intake_id) REFERENCES intake(project_id, intake_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interventions (
                    project_id TEXT NOT NULL,
                    intervention_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    summary TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    resolution TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    resolved_by TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, intervention_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    project_id TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target_json TEXT NOT NULL DEFAULT '{}',
                    comment_id TEXT NOT NULL DEFAULT '',
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_posted_at TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, target_key, channel),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    project_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    uri TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, artifact_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
                );
                """
            )
            self._record_schema_metadata(conn)

    def upsert_project(self, config: ProjectConfig) -> None:
        """Create or update a project row."""
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO projects (project_id, name, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    name=excluded.name,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                (config.project_id, config.name, dumps(config.raw), now, now),
            )
            self._audit(
                conn,
                config.project_id,
                "",
                "project.upsert",
                "system",
                {"name": config.name},
            )

    def import_tasks(
        self,
        config: ProjectConfig,
        tasks: list[TaskRecord],
        dependencies: list[DependencyRecord],
        *,
        reconcile_runtime_state: bool = False,
    ) -> None:
        """Import tasks and dependencies into the store."""
        self._validate_import(tasks, dependencies)
        self.upsert_project(config)
        now = iso()
        with self.transaction(immediate=True) as conn:
            existing_rows = {
                str(row["task_id"]): row
                for row in conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ?",
                    (config.project_id,),
                )
            }
            if _table_exists(conn, "proposed_tasks"):
                proposal_rows = list(
                    conn.execute(
                        """
                        SELECT task_id, proposal_id
                        FROM proposed_tasks
                        WHERE project_id = ? AND status = 'proposed'
                        """,
                        (config.project_id,),
                    )
                )
                proposed_task_ids = {
                    str(row["task_id"]): str(row["proposal_id"]) for row in proposal_rows
                }
                for task in tasks:
                    proposal_id = proposed_task_ids.get(task.task_id)
                    if proposal_id and task.task_id not in existing_rows:
                        raise ValueError(
                            f"task {task.task_id} is still proposed "
                            f"({proposal_id}); promote it before importing"
                        )
            imported_task_ids = [task.task_id for task in tasks]
            for task in tasks:
                existing = existing_rows.get(task.task_id)
                if existing is None:
                    status = task.status
                    lease_agent_id = ""
                    lease_token = ""
                    lease_expires_at = ""
                    claimed_at = ""
                elif reconcile_runtime_state:
                    preserve_lease = _should_preserve_lease(existing, task.status)
                    if preserve_lease:
                        status = (
                            str(existing["status"]) if task.status == "pending" else task.status
                        )
                        lease_agent_id = str(existing["lease_agent_id"])
                        lease_token = str(existing["lease_token"])
                        lease_expires_at = str(existing["lease_expires_at"])
                        claimed_at = str(existing["claimed_at"])
                    else:
                        status = task.status
                        lease_agent_id = ""
                        lease_token = ""
                        lease_expires_at = ""
                        claimed_at = ""
                else:
                    status = str(existing["status"])
                    lease_agent_id = str(existing["lease_agent_id"])
                    lease_token = str(existing["lease_token"])
                    lease_expires_at = str(existing["lease_expires_at"])
                    claimed_at = str(existing["claimed_at"])
                conn.execute(
                    """
                    INSERT INTO tasks (
                        project_id, task_id, title, repo, status, priority, prompt_key,
                        prompt_path, summary, execution_json, validation_json, next_action,
                        metadata_json, lease_agent_id, lease_token, lease_expires_at,
                        claimed_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, task_id) DO UPDATE SET
                        title=excluded.title,
                        repo=excluded.repo,
                        status=excluded.status,
                        priority=excluded.priority,
                        prompt_key=excluded.prompt_key,
                        prompt_path=excluded.prompt_path,
                        summary=excluded.summary,
                        execution_json=excluded.execution_json,
                        validation_json=excluded.validation_json,
                        next_action=excluded.next_action,
                        metadata_json=excluded.metadata_json,
                        lease_agent_id=excluded.lease_agent_id,
                        lease_token=excluded.lease_token,
                        lease_expires_at=excluded.lease_expires_at,
                        claimed_at=excluded.claimed_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        config.project_id,
                        task.task_id,
                        task.title,
                        task.repo,
                        status,
                        task.priority,
                        task.prompt_key,
                        task.prompt_path,
                        task.summary,
                        dumps(task.execution),
                        dumps(task.validation_checks),
                        task.next_action,
                        dumps(task.metadata),
                        lease_agent_id,
                        lease_token,
                        lease_expires_at,
                        claimed_at,
                        now,
                        now,
                    ),
                )
                for uri in task.evidence:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO evidence (project_id, task_id, uri, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (config.project_id, task.task_id, uri, now),
                    )
            if reconcile_runtime_state:
                if imported_task_ids:
                    placeholders = ", ".join("?" for _ in imported_task_ids)
                    conn.execute(
                        f"""
                        DELETE FROM tasks
                        WHERE project_id = ? AND task_id NOT IN ({placeholders})
                        """,
                        (config.project_id, *imported_task_ids),
                    )
                else:
                    conn.execute("DELETE FROM tasks WHERE project_id = ?", (config.project_id,))
                conn.execute("DELETE FROM dependencies WHERE project_id = ?", (config.project_id,))
            elif imported_task_ids:
                placeholders = ", ".join("?" for _ in imported_task_ids)
                conn.execute(
                    f"""
                    DELETE FROM dependencies
                    WHERE project_id = ? AND task_id IN ({placeholders})
                    """,
                    (config.project_id, *imported_task_ids),
                )
            for dependency in dependencies:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dependencies (
                        project_id, task_id, dependency_task_id, description
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        config.project_id,
                        dependency.task_id,
                        dependency.dependency_task_id,
                        dependency.description,
                    ),
                )
            self._audit(
                conn,
                config.project_id,
                "",
                "tasks.import",
                "system",
                {
                    "tasks": len(tasks),
                    "dependencies": len(dependencies),
                    "reconcile_runtime_state": reconcile_runtime_state,
                },
            )

    def _validate_import(
        self,
        tasks: list[TaskRecord],
        dependencies: list[DependencyRecord],
    ) -> None:
        """Validate task import payloads before mutating storage."""
        task_ids: set[str] = set()
        for task in tasks:
            if not task.task_id:
                raise ValueError("task id is required")
            if task.task_id in task_ids:
                raise ValueError(f"duplicate task id: {task.task_id}")
            if task.status not in MANUAL_STATES:
                raise ValueError(f"invalid task status for {task.task_id}: {task.status}")
            task_ids.add(task.task_id)
        for dependency in dependencies:
            if not dependency.task_id:
                raise ValueError("dependency task id is required")
            if not dependency.dependency_task_id:
                raise ValueError(f"dependency for {dependency.task_id} is missing a task id")
            if dependency.task_id not in task_ids:
                raise ValueError(
                    f"dependency references unknown task {dependency.task_id}: "
                    f"{dependency.dependency_task_id}"
                )
            if dependency.dependency_task_id not in task_ids:
                raise ValueError(
                    f"task {dependency.task_id} references unknown dependency "
                    f"{dependency.dependency_task_id}"
                )

    def task_states(
        self,
        project_id: str,
        *,
        recover_stale_leases: bool = False,
    ) -> list[TaskState]:
        """Return evaluated task states ordered by priority."""
        now = utcnow()
        with self.transaction(
            immediate=recover_stale_leases,
            read_only=not recover_stale_leases,
        ) as conn:
            if recover_stale_leases:
                self._recover_stale_leases(conn, project_id, now=now)
            rows = list(
                conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority, task_id",
                    (project_id,),
                )
            )
            dep_rows = list(
                conn.execute(
                    "SELECT * FROM dependencies WHERE project_id = ?",
                    (project_id,),
                )
            )
            evidence_rows = list(
                conn.execute(
                    """
                    SELECT task_id, uri
                    FROM evidence
                    WHERE project_id = ?
                    ORDER BY created_at, uri
                    """,
                    (project_id,),
                )
            )
        tasks_by_id = {
            str(row["task_id"]): self._effective_task_from_row(
                row,
                now=now,
                recover_stale_leases=recover_stale_leases,
            )
            for row in rows
        }
        status_by_id = {task_id: task.status for task_id, task in tasks_by_id.items()}
        dependencies_by_task: dict[str, list[sqlite3.Row]] = {}
        for dep_row in dep_rows:
            dependencies_by_task.setdefault(str(dep_row["task_id"]), []).append(dep_row)
        evidence_by_task: dict[str, list[str]] = {}
        for evidence_row in evidence_rows:
            evidence_by_task.setdefault(str(evidence_row["task_id"]), []).append(
                str(evidence_row["uri"])
            )

        states: list[TaskState] = []
        rows_by_id = {str(row["task_id"]): row for row in rows}
        for task_id, task in tasks_by_id.items():
            requirements = []
            for dep in dependencies_by_task.get(task_id, []):
                dependency_id = str(dep["dependency_task_id"])
                dependency_status = status_by_id.get(dependency_id, "missing")
                requirements.append(
                    RequirementState(
                        description=str(dep["description"] or f"Depends on {dependency_id}"),
                        satisfied=dependency_status == "done",
                        detail=f"{dependency_id}: {dependency_status}",
                    )
                )
            state = self._computed_state(task.status, requirements)
            row = rows_by_id[task_id]
            stale_lease = (
                not recover_stale_leases
                and _row_has_stale_lease(row, now=now)
                and str(row["status"]) in ACTIVE_STATES
            )
            states.append(
                TaskState(
                    task=task,
                    state=state,
                    requirements=requirements,
                    lease_agent_id="" if stale_lease else str(row["lease_agent_id"]),
                    lease_token="" if stale_lease else str(row["lease_token"]),
                    lease_expires_at="" if stale_lease else str(row["lease_expires_at"]),
                    evidence=evidence_by_task.get(task_id, []),
                )
            )
        return states

    def get_task_state(
        self,
        project_id: str,
        task_id: str,
        *,
        recover_stale_leases: bool = False,
    ) -> TaskState:
        """Return one evaluated task state."""
        for state in self.task_states(project_id, recover_stale_leases=recover_stale_leases):
            if state.task.task_id == task_id:
                return state
        raise KeyError(f"unknown task: {task_id}")

    def claim_task(
        self,
        project_id: str,
        *,
        agent_id: str,
        task_id: str = "",
        repo: str = "",
        role: str = "",
        lease_seconds: int = 3600,
    ) -> Claim:
        """Claim a ready task atomically."""
        _validate_lease_seconds(lease_seconds)
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        token = uuid.uuid4().hex
        with self.transaction(immediate=True) as conn:
            self._recover_stale_leases(conn, project_id, now=now)
            rows = list(
                conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority, task_id",
                    (project_id,),
                )
            )
            candidates = [
                row
                for row in rows
                if (not task_id or row["task_id"] == task_id)
                and (not repo or row["repo"] == repo)
                and self._row_is_ready(conn, project_id, row)
                and self._role_matches(row, role)
            ]
            if not candidates:
                raise ValueError("no matching ready task")
            row = candidates[0]
            conn.execute(
                """
                UPDATE tasks SET
                    status = 'claimed',
                    lease_agent_id = ?,
                    lease_token = ?,
                    lease_expires_at = ?,
                    claimed_at = ?,
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (
                    agent_id,
                    token,
                    iso(expires),
                    iso(now),
                    iso(now),
                    project_id,
                    row["task_id"],
                ),
            )
            self._audit(
                conn,
                project_id,
                str(row["task_id"]),
                "task.claim",
                agent_id,
                {"lease_expires_at": iso(expires), "role": role},
            )
            return Claim(
                project_id=project_id,
                task_id=str(row["task_id"]),
                lease_token=token,
                lease_expires_at=iso(expires),
                agent_id=agent_id,
            )

    def heartbeat(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        lease_seconds: int = 3600,
        agent_id: str = "",
    ) -> Claim:
        """Extend a task lease and mark it in progress."""
        _validate_lease_seconds(lease_seconds)
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        with self.transaction(immediate=True) as conn:
            row = self._locked_task(
                conn,
                project_id,
                task_id,
                lease_token,
                agent_id=agent_id,
                now=now,
            )
            conn.execute(
                """
                UPDATE tasks SET
                    status = 'in_progress',
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (iso(expires), iso(now), project_id, task_id),
            )
            actor = agent_id or str(row["lease_agent_id"])
            self._audit(
                conn,
                project_id,
                task_id,
                "task.heartbeat",
                actor,
                {"lease_expires_at": iso(expires)},
            )
            return Claim(project_id, task_id, lease_token, iso(expires), actor)

    def complete_task(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
        direct_merge: bool = False,
    ) -> None:
        """Mark a leased task done.

        Args:
            project_id: Project that owns the task.
            task_id: Task identifier to complete.
            lease_token: Active lease token for the task.
            evidence: Optional evidence URIs for the completion.
            agent_id: Actor completing the task.
            direct_merge: Whether to apply an explicit direct-merge completion
                override.

        Raises:
            ValueError: If the lease is invalid or completion evidence does not
                satisfy task policy.
            KeyError: If the task does not exist.
        """
        self._finish_task(
            project_id,
            task_id,
            lease_token=lease_token,
            status="done",
            action="task.complete",
            evidence=evidence or [],
            agent_id=agent_id,
            direct_merge=direct_merge,
        )

    def fail_task(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        reason: str,
        agent_id: str = "",
    ) -> None:
        """Mark a leased task failed."""
        self._finish_task(
            project_id,
            task_id,
            lease_token=lease_token,
            status="failed",
            action="task.fail",
            evidence=[],
            agent_id=agent_id,
            payload={"reason": reason},
        )

    def release_task(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        reason: str,
        agent_id: str = "",
        status: str = "pending",
    ) -> dict[str, str]:
        """Release an active leased task back to the queue."""
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("release reason is required")
        if status != "pending":
            raise ValueError("release status must be pending")
        now_dt = utcnow()
        with self.transaction(immediate=True) as conn:
            row = self._locked_task(
                conn,
                project_id,
                task_id,
                lease_token,
                agent_id=agent_id,
                now=now_dt,
            )
            actor = agent_id or str(row["lease_agent_id"])
            if not actor.strip():
                raise ValueError("agent is required when releasing a lease without recorded owner")
            from_status = str(row["status"])
            now = iso(now_dt)
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    lease_agent_id = '',
                    lease_token = '',
                    lease_expires_at = '',
                    claimed_at = '',
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (status, now, project_id, task_id),
            )
            payload = {
                "from_status": from_status,
                "status": status,
                "reason": clean_reason,
            }
            self._audit(conn, project_id, task_id, "task.release", actor, payload)
        return {
            "project_id": project_id,
            "task_id": task_id,
            "from_status": from_status,
            "status": status,
            "agent_id": actor,
            "reason": clean_reason,
        }

    def submit_review_task(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> None:
        """Move a leased task to review and clear its lease.

        Args:
            project_id: Project that owns the task.
            task_id: Task identifier to transition.
            lease_token: Current active lease token for the task.
            evidence: Optional evidence URIs to attach to the handoff.
            agent_id: Optional agent ID used to validate lease ownership.

        Raises:
            KeyError: If the task does not exist.
            ValueError: If the lease token is missing, expired, invalid, or
                belongs to a different agent.
        """
        self._finish_task(
            project_id,
            task_id,
            lease_token=lease_token,
            status="awaiting_review",
            action="task.submit_review",
            evidence=evidence or [],
            agent_id=agent_id,
        )

    def await_integration_task(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        status: str = "awaiting_integration",
        evidence: list[str] | None = None,
        agent_id: str = "",
    ) -> None:
        """Move a leased task to an integration wait state and clear its lease.

        Args:
            project_id: Project that owns the task.
            task_id: Task identifier to transition.
            lease_token: Current active lease token for the task.
            status: Integration wait status to set.
            evidence: Optional evidence URIs to attach to the handoff.
            agent_id: Optional agent ID used to validate lease ownership.

        Raises:
            KeyError: If the task does not exist.
            ValueError: If `status` is not an integration wait state, or if the
                lease token is missing, expired, invalid, or belongs to a
                different agent.
        """
        if status not in INTEGRATION_STATES:
            allowed = ", ".join(sorted(INTEGRATION_STATES))
            raise ValueError(f"integration status must be one of: {allowed}")
        self._finish_task(
            project_id,
            task_id,
            lease_token=lease_token,
            status=status,
            action="task.await_integration",
            evidence=evidence or [],
            agent_id=agent_id,
            payload={"status": status},
        )

    def resolve_review_task(
        self,
        project_id: str,
        task_id: str,
        *,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
        direct_merge: bool = False,
    ) -> None:
        """Resolve a review-waiting task without requiring an active lease."""
        self._resolve_awaiting_task(
            project_id,
            task_id,
            allowed_current_statuses=REVIEW_STATES,
            status=status,
            action="task.resolve_review",
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
            direct_merge=direct_merge,
        )

    def resolve_integration_task(
        self,
        project_id: str,
        task_id: str,
        *,
        status: str = "done",
        evidence: list[str] | None = None,
        agent_id: str = "",
        reason: str = "",
        direct_merge: bool = False,
    ) -> None:
        """Resolve an integration-waiting task without requiring an active lease."""
        self._resolve_awaiting_task(
            project_id,
            task_id,
            allowed_current_statuses=INTEGRATION_STATES,
            status=status,
            action="task.resolve_integration",
            evidence=evidence or [],
            agent_id=agent_id,
            reason=reason,
            direct_merge=direct_merge,
        )

    def record_event(self, project_id: str, event: EventRecord, *, actor: str = "system") -> bool:
        """Record an event idempotently. Return True if inserted."""
        with self.transaction(immediate=True) as conn:
            now = iso()
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    project_id, event_id, kind, task_id, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    event.event_id,
                    event.kind,
                    event.task_id,
                    dumps(event.payload),
                    now,
                ),
            )
            inserted = cur.rowcount > 0
            if inserted:
                self._audit(
                    conn,
                    project_id,
                    event.task_id,
                    "event.record",
                    actor,
                    {"event_id": event.event_id, "kind": event.kind},
                )
            return inserted

    def record_intake(
        self,
        project_id: str,
        intake: IntakeRecord,
        *,
        actor: str = "system",
    ) -> IntakeRecord:
        """Record a raw intake item."""
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO intake (
                    project_id, intake_id, kind, source, repo, status, text,
                    tags_json, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    intake.intake_id,
                    intake.kind,
                    intake.source,
                    intake.repo,
                    intake.status,
                    intake.text,
                    dumps(intake.tags),
                    dumps(intake.metadata),
                    now,
                    now,
                ),
            )
            self._audit(
                conn,
                project_id,
                "",
                "intake.record",
                actor,
                {"intake_id": intake.intake_id, "kind": intake.kind, "repo": intake.repo},
            )
        return replace(intake, created_at=now, updated_at=now)

    def intake_records(
        self,
        project_id: str,
        *,
        status: str = "",
        kind: str = "",
        repo: str = "",
        limit: int = 0,
    ) -> list[IntakeRecord]:
        """Return raw intake records newest first."""
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if status:
            clauses.append("status = ?")
            values.append(status)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        if repo:
            clauses.append("repo = ?")
            values.append(repo)
        sql = f"""
            SELECT *
            FROM intake
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, intake_id DESC
        """
        if limit:
            sql += " LIMIT ?"
            values.append(limit)
        with self.transaction(read_only=True) as conn:
            if not _table_exists(conn, "intake"):
                return []
            rows = list(conn.execute(sql, values))
        return [_intake_from_row(row) for row in rows]

    def update_intake_status(
        self,
        project_id: str,
        intake_id: str,
        status: str,
        *,
        actor: str = "system",
    ) -> IntakeRecord:
        """Update the triage status for one intake record."""
        self.init_schema()
        if status not in INTAKE_STATES:
            raise ValueError(f"invalid intake status: {status}")
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM intake WHERE project_id = ? AND intake_id = ?",
                (project_id, intake_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown intake record: {intake_id}")
            previous = str(row["status"])
            conn.execute(
                """
                UPDATE intake
                SET status = ?, updated_at = ?
                WHERE project_id = ? AND intake_id = ?
                """,
                (status, now, project_id, intake_id),
            )
            self._audit(
                conn,
                project_id,
                "",
                "intake.status",
                actor,
                {"intake_id": intake_id, "previous": previous, "status": status},
            )
            updated = conn.execute(
                "SELECT * FROM intake WHERE project_id = ? AND intake_id = ?",
                (project_id, intake_id),
            ).fetchone()
        if updated is None:
            raise ValueError(f"unknown intake record: {intake_id}")
        return _intake_from_row(updated)

    def record_proposed_task(
        self,
        project_id: str,
        proposal: ProposedTaskRecord,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Record a proposed task contract."""
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            if not _intake_exists(conn, project_id, proposal.intake_id):
                raise ValueError(f"unknown intake record: {proposal.intake_id}")
            if _task_exists(conn, project_id, proposal.task.task_id):
                raise ValueError(f"task already exists: {proposal.task.task_id}")
            existing_proposal = conn.execute(
                """
                SELECT proposal_id, status
                FROM proposed_tasks
                WHERE project_id = ? AND task_id = ?
                """,
                (project_id, proposal.task.task_id),
            ).fetchone()
            if existing_proposal is not None:
                status = str(existing_proposal["status"])
                if status == "rejected":
                    raise ValueError(
                        "proposal task id conflicts with an existing rejected proposal: "
                        f"{proposal.task.task_id}"
                    )
                raise ValueError(f"proposal task id already exists: {proposal.task.task_id}")
            if proposal.status not in PROPOSAL_STATES:
                raise ValueError(f"invalid proposal status: {proposal.status}")
            contract = proposed_task_to_dict(proposal)
            conn.execute(
                """
                INSERT INTO proposed_tasks (
                    project_id, proposal_id, intake_id, task_id, status,
                    contract_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    proposal.proposal_id,
                    proposal.intake_id,
                    proposal.task.task_id,
                    proposal.status,
                    dumps(contract),
                    now,
                    now,
                ),
            )
            cur = conn.execute(
                """
                UPDATE intake
                SET status = 'triaged', updated_at = ?
                WHERE project_id = ? AND intake_id = ? AND status = 'open'
                """,
                (now, project_id, proposal.intake_id),
            )
            if cur.rowcount:
                self._audit(
                    conn,
                    project_id,
                    "",
                    "intake.status",
                    actor,
                    {
                        "intake_id": proposal.intake_id,
                        "previous": "open",
                        "status": "triaged",
                    },
                )
            self._audit(
                conn,
                project_id,
                proposal.task.task_id,
                "proposal.record",
                actor,
                {"proposal_id": proposal.proposal_id, "intake_id": proposal.intake_id},
            )
        return replace(proposal, created_at=now, updated_at=now)

    def record_planned_task(
        self,
        project_id: str,
        intake: IntakeRecord,
        proposal: ProposedTaskRecord,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Atomically record planning intake and its proposed task contract."""
        if proposal.intake_id != intake.intake_id:
            raise ValueError("proposal intake id must match the recorded intake id")
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            if _task_exists(conn, project_id, proposal.task.task_id):
                raise ValueError(f"task already exists: {proposal.task.task_id}")
            existing_id = conn.execute(
                """
                SELECT 1
                FROM proposed_tasks
                WHERE project_id = ? AND proposal_id = ?
                """,
                (project_id, proposal.proposal_id),
            ).fetchone()
            if existing_id is not None:
                raise ValueError(f"proposal already exists: {proposal.proposal_id}")
            existing_task = conn.execute(
                """
                SELECT proposal_id, status
                FROM proposed_tasks
                WHERE project_id = ? AND task_id = ?
                """,
                (project_id, proposal.task.task_id),
            ).fetchone()
            if existing_task is not None:
                status = str(existing_task["status"])
                if status == "rejected":
                    raise ValueError(
                        "proposal task id conflicts with an existing rejected proposal: "
                        f"{proposal.task.task_id}"
                    )
                raise ValueError(f"proposal task id already exists: {proposal.task.task_id}")
            if proposal.status not in PROPOSAL_STATES:
                raise ValueError(f"invalid proposal status: {proposal.status}")
            conn.execute(
                """
                INSERT INTO intake (
                    project_id, intake_id, kind, source, repo, status, text,
                    tags_json, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    intake.intake_id,
                    intake.kind,
                    intake.source,
                    intake.repo,
                    intake.status,
                    intake.text,
                    dumps(intake.tags),
                    dumps(intake.metadata),
                    now,
                    now,
                ),
            )
            self._audit(
                conn,
                project_id,
                "",
                "intake.record",
                actor,
                {"intake_id": intake.intake_id, "kind": intake.kind, "repo": intake.repo},
            )
            contract = proposed_task_to_dict(proposal)
            conn.execute(
                """
                INSERT INTO proposed_tasks (
                    project_id, proposal_id, intake_id, task_id, status,
                    contract_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    proposal.proposal_id,
                    proposal.intake_id,
                    proposal.task.task_id,
                    proposal.status,
                    dumps(contract),
                    now,
                    now,
                ),
            )
            cur = conn.execute(
                """
                UPDATE intake
                SET status = 'triaged', updated_at = ?
                WHERE project_id = ? AND intake_id = ? AND status = 'open'
                """,
                (now, project_id, proposal.intake_id),
            )
            if cur.rowcount:
                self._audit(
                    conn,
                    project_id,
                    "",
                    "intake.status",
                    actor,
                    {
                        "intake_id": proposal.intake_id,
                        "previous": "open",
                        "status": "triaged",
                    },
                )
            self._audit(
                conn,
                project_id,
                proposal.task.task_id,
                "proposal.record",
                actor,
                {"proposal_id": proposal.proposal_id, "intake_id": proposal.intake_id},
            )
        return replace(proposal, created_at=now, updated_at=now)

    def proposed_task_records(
        self,
        project_id: str,
        *,
        status: str = "",
        intake_id: str = "",
        limit: int = 0,
    ) -> list[ProposedTaskRecord]:
        """Return proposed task records newest first."""
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if status:
            clauses.append("status = ?")
            values.append(status)
        if intake_id:
            clauses.append("intake_id = ?")
            values.append(intake_id)
        sql = f"""
            SELECT *
            FROM proposed_tasks
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, proposal_id DESC
        """
        if limit:
            sql += " LIMIT ?"
            values.append(limit)
        with self.transaction(read_only=True) as conn:
            if not _table_exists(conn, "proposed_tasks"):
                return []
            rows = list(conn.execute(sql, values))
        return [_proposed_task_from_row(row) for row in rows]

    def proposed_task_record(
        self,
        project_id: str,
        proposal_id: str,
    ) -> ProposedTaskRecord | None:
        """Return one proposed task record, if present."""
        with self.transaction(read_only=True) as conn:
            if not _table_exists(conn, "proposed_tasks"):
                return None
            row = conn.execute(
                """
                SELECT *
                FROM proposed_tasks
                WHERE project_id = ? AND proposal_id = ?
                """,
                (project_id, proposal_id),
            ).fetchone()
        return _proposed_task_from_row(row) if row is not None else None

    def update_proposed_task(
        self,
        project_id: str,
        proposal_id: str,
        proposal: ProposedTaskRecord,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Update a proposed task contract before promotion."""
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM proposed_tasks
                WHERE project_id = ? AND proposal_id = ?
                """,
                (project_id, proposal_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown proposed task: {proposal_id}")
            current = _proposed_task_from_row(row)
            if current.status != "proposed":
                raise ValueError(
                    f"cannot update proposal {proposal_id} from status {current.status}"
                )
            task = proposal.task
            if proposal.status not in PROPOSAL_STATES:
                raise ValueError(f"invalid proposal status: {proposal.status}")
            if not task.task_id:
                raise ValueError("proposed task id is required")
            if not task.title:
                raise ValueError("proposed task title is required")
            if _task_exists(conn, project_id, task.task_id):
                raise ValueError(f"task already exists: {task.task_id}")
            proposal_row = conn.execute(
                """
                SELECT proposal_id, status
                FROM proposed_tasks
                WHERE project_id = ?
                  AND task_id = ?
                  AND proposal_id != ?
                """,
                (project_id, task.task_id, proposal_id),
            ).fetchone()
            if proposal_row is not None:
                status = str(proposal_row["status"])
                if status in {"proposed", "promoted"}:
                    raise ValueError(f"proposal task id already exists: {task.task_id}")
                raise ValueError(
                    f"proposal task id conflicts with an existing rejected proposal: {task.task_id}"
                )
            updated = replace(
                proposal,
                proposal_id=proposal_id,
                intake_id=current.intake_id,
                status="proposed",
                created_at=current.created_at,
                updated_at=now,
            )
            changes = _proposal_contract_changes(current, updated)
            conn.execute(
                """
                UPDATE proposed_tasks
                SET task_id = ?, status = 'proposed', contract_json = ?, updated_at = ?
                WHERE project_id = ? AND proposal_id = ?
                """,
                (
                    updated.task.task_id,
                    dumps(proposed_task_to_dict(updated)),
                    now,
                    project_id,
                    proposal_id,
                ),
            )
            self._audit(
                conn,
                project_id,
                updated.task.task_id,
                "proposal.update",
                actor,
                {
                    "proposal_id": proposal_id,
                    "intake_id": current.intake_id,
                    "previous_task_id": current.task.task_id,
                    "task_id": updated.task.task_id,
                    "changed_fields": list(changes),
                    "changes": changes,
                },
            )
        return updated

    def promote_proposed_task(
        self,
        project_id: str,
        proposal_id: str,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Promote a proposed task into live pending task state."""
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM proposed_tasks
                WHERE project_id = ? AND proposal_id = ?
                """,
                (project_id, proposal_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown proposed task: {proposal_id}")
            proposal = _proposed_task_from_row(row)
            task = replace(proposal.task, status="pending")
            if proposal.status == "promoted":
                if not _task_exists(conn, project_id, task.task_id):
                    raise ValueError(f"promoted task is missing: {task.task_id}")
                return proposal
            if proposal.status != "proposed":
                raise ValueError(
                    f"cannot promote proposal {proposal_id} from status {proposal.status}"
                )
            if _task_exists(conn, project_id, task.task_id):
                raise ValueError(f"task already exists: {task.task_id}")
            if not task.task_id:
                raise ValueError("proposed task id is required")
            if not task.title:
                raise ValueError("proposed task title is required")
            for requirement in proposal.requirements:
                dependency_task_id = str(requirement.get("task") or "").strip()
                if dependency_task_id and not _task_exists(conn, project_id, dependency_task_id):
                    raise ValueError(
                        f"proposal dependency is not a live task: {dependency_task_id}"
                    )
            conn.execute(
                """
                INSERT INTO tasks (
                    project_id, task_id, title, repo, status, priority, prompt_key,
                    prompt_path, summary, execution_json, validation_json, next_action,
                    metadata_json, lease_agent_id, lease_token, lease_expires_at,
                    claimed_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?)
                """,
                (
                    project_id,
                    task.task_id,
                    task.title,
                    task.repo,
                    task.status,
                    task.priority,
                    task.prompt_key,
                    task.prompt_path,
                    task.summary,
                    dumps(task.execution),
                    dumps(task.validation_checks),
                    task.next_action,
                    dumps(task.metadata),
                    now,
                    now,
                ),
            )
            for requirement in proposal.requirements:
                dependency_task_id = str(requirement.get("task") or "").strip()
                if not dependency_task_id:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dependencies (
                        project_id, task_id, dependency_task_id, description
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        task.task_id,
                        dependency_task_id,
                        str(requirement.get("description") or ""),
                    ),
                )
            for uri in task.evidence:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence (project_id, task_id, uri, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_id, task.task_id, uri, now),
                )
            conn.execute(
                """
                UPDATE proposed_tasks
                SET status = 'promoted', updated_at = ?
                WHERE project_id = ? AND proposal_id = ?
                """,
                (now, project_id, proposal_id),
            )
            cur = conn.execute(
                """
                UPDATE intake
                SET status = 'triaged', updated_at = ?
                WHERE project_id = ? AND intake_id = ? AND status = 'open'
                """,
                (now, project_id, proposal.intake_id),
            )
            if cur.rowcount:
                self._audit(
                    conn,
                    project_id,
                    "",
                    "intake.status",
                    actor,
                    {
                        "intake_id": proposal.intake_id,
                        "previous": "open",
                        "status": "triaged",
                    },
                )
            self._audit(
                conn,
                project_id,
                task.task_id,
                "proposal.promote",
                actor,
                {"proposal_id": proposal_id, "intake_id": proposal.intake_id},
            )
        return replace(proposal, task=task, status="promoted", updated_at=now)

    def withdraw_proposed_task(
        self,
        project_id: str,
        proposal_id: str,
        *,
        actor: str = "system",
    ) -> ProposedTaskRecord:
        """Withdraw a proposed task contract before promotion."""
        self.init_schema()
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM proposed_tasks
                WHERE project_id = ? AND proposal_id = ?
                """,
                (project_id, proposal_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown proposed task: {proposal_id}")
            proposal = _proposed_task_from_row(row)
            if proposal.status != "proposed":
                raise ValueError(
                    f"cannot withdraw proposal {proposal_id} from status {proposal.status}"
                )
            withdrawn = replace(proposal, status="rejected", updated_at=now)
            conn.execute(
                """
                UPDATE proposed_tasks
                SET status = 'rejected', contract_json = ?, updated_at = ?
                WHERE project_id = ? AND proposal_id = ?
                """,
                (dumps(proposed_task_to_dict(withdrawn)), now, project_id, proposal_id),
            )
            self._audit(
                conn,
                project_id,
                proposal.task.task_id,
                "proposal.withdraw",
                actor,
                {"proposal_id": proposal_id, "intake_id": proposal.intake_id},
            )
        return withdrawn

    def record_intervention(
        self,
        project_id: str,
        intervention: InterventionRecord,
        *,
        actor: str = "system",
    ) -> InterventionRecord:
        """Record an unresolved human intervention need."""
        self.init_schema()
        if intervention.reason not in INTERVENTION_REASONS:
            raise ValueError(f"invalid intervention reason: {intervention.reason}")
        if intervention.status != "open":
            raise ValueError("new interventions must start open")
        if (
            intervention.resolution
            or intervention.evidence
            or intervention.resolved_by
            or intervention.resolved_at
        ):
            raise ValueError("new interventions cannot include resolution state")
        now = iso()
        with self.transaction(immediate=True) as conn:
            if intervention.task_id and not _task_exists(conn, project_id, intervention.task_id):
                raise ValueError(f"unknown task: {intervention.task_id}")
            conn.execute(
                """
                INSERT INTO interventions (
                    project_id, intervention_id, task_id, reason, status, summary,
                    metadata_json, resolution, evidence_json, resolved_by,
                    resolved_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'open', ?, ?, '', '[]', '', '', ?, ?)
                """,
                (
                    project_id,
                    intervention.intervention_id,
                    intervention.task_id,
                    intervention.reason,
                    intervention.summary,
                    dumps(intervention.metadata),
                    now,
                    now,
                ),
            )
            self._audit(
                conn,
                project_id,
                intervention.task_id,
                "intervention.record",
                actor,
                {
                    "intervention_id": intervention.intervention_id,
                    "reason": intervention.reason,
                    "summary": intervention.summary,
                },
            )
        return replace(intervention, created_at=now, updated_at=now)

    def intervention_records(
        self,
        project_id: str,
        *,
        status: str = "",
        reason: str = "",
        task_id: str = "",
        limit: int = 0,
    ) -> list[InterventionRecord]:
        """Return durable intervention records newest first."""
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if status:
            clauses.append("status = ?")
            values.append(status)
        if reason:
            clauses.append("reason = ?")
            values.append(reason)
        if task_id:
            clauses.append("task_id = ?")
            values.append(task_id)
        sql = f"""
            SELECT *
            FROM interventions
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, intervention_id DESC
        """
        if limit:
            sql += " LIMIT ?"
            values.append(limit)
        with self.transaction(read_only=True) as conn:
            if not _table_exists(conn, "interventions"):
                return []
            rows = list(conn.execute(sql, values))
        return [_intervention_from_row(row) for row in rows]

    def resolve_intervention(
        self,
        project_id: str,
        intervention_id: str,
        *,
        resolution: str = "",
        evidence: list[str] | None = None,
        actor: str = "system",
    ) -> InterventionRecord:
        """Resolve one intervention with evidence or a resolution reason."""
        self.init_schema()
        clean_evidence = [str(uri).strip() for uri in evidence or [] if str(uri).strip()]
        if not resolution.strip() and not clean_evidence:
            raise ValueError("resolution requires evidence or reason")
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM interventions
                WHERE project_id = ? AND intervention_id = ?
                """,
                (project_id, intervention_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown intervention: {intervention_id}")
            if str(row["status"]) != "open":
                raise ValueError(f"intervention is already {row['status']}")
            conn.execute(
                """
                UPDATE interventions
                SET status = 'resolved',
                    resolution = ?,
                    evidence_json = ?,
                    resolved_by = ?,
                    resolved_at = ?,
                    updated_at = ?
                WHERE project_id = ? AND intervention_id = ?
                """,
                (
                    resolution.strip(),
                    dumps(clean_evidence),
                    actor,
                    now,
                    now,
                    project_id,
                    intervention_id,
                ),
            )
            self._audit(
                conn,
                project_id,
                str(row["task_id"]),
                "intervention.resolve",
                actor,
                {
                    "intervention_id": intervention_id,
                    "reason": str(row["reason"]),
                    "resolution": resolution.strip(),
                    "evidence": clean_evidence,
                },
            )
            updated = conn.execute(
                """
                SELECT *
                FROM interventions
                WHERE project_id = ? AND intervention_id = ?
                """,
                (project_id, intervention_id),
            ).fetchone()
        if updated is None:
            raise ValueError(f"unknown intervention: {intervention_id}")
        return _intervention_from_row(updated)

    def notification_delivery(
        self,
        project_id: str,
        target_key: str,
        *,
        channel: str = "pull_request_comment",
    ) -> NotificationDeliveryRecord | None:
        """Return one notification delivery record when present."""
        self.init_schema()
        with self.transaction(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM notification_deliveries
                WHERE project_id = ? AND target_key = ? AND channel = ?
                """,
                (project_id, target_key, channel),
            ).fetchone()
        return _notification_delivery_from_row(row) if row is not None else None

    def upsert_notification_delivery(
        self,
        project_id: str,
        *,
        target_key: str,
        channel: str,
        target: dict[str, Any],
        payload_hash: str,
        status: str,
        comment_id: str = "",
        last_posted_at: str = "",
        metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> NotificationDeliveryRecord:
        """Create or update durable notification delivery state."""
        self.init_schema()
        cleaned_target_key = str(target_key).strip()
        cleaned_channel = str(channel).strip()
        cleaned_payload_hash = str(payload_hash).strip()
        cleaned_status = str(status).strip()
        if not cleaned_target_key:
            raise ValueError("notification target key is required")
        if not cleaned_channel:
            raise ValueError("notification channel is required")
        if not cleaned_payload_hash:
            raise ValueError("notification payload hash is required")
        if not cleaned_status:
            raise ValueError("notification status is required")
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM notification_deliveries
                WHERE project_id = ? AND target_key = ? AND channel = ?
                """,
                (project_id, cleaned_target_key, cleaned_channel),
            ).fetchone()
            created_at = str(row["created_at"]) if row is not None else now
            conn.execute(
                """
                INSERT INTO notification_deliveries (
                    project_id, target_key, channel, target_json, comment_id,
                    payload_hash, status, last_posted_at, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, target_key, channel) DO UPDATE SET
                    target_json = excluded.target_json,
                    comment_id = excluded.comment_id,
                    payload_hash = excluded.payload_hash,
                    status = excluded.status,
                    last_posted_at = excluded.last_posted_at,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    cleaned_target_key,
                    cleaned_channel,
                    dumps(target),
                    str(comment_id).strip(),
                    cleaned_payload_hash,
                    cleaned_status,
                    str(last_posted_at).strip(),
                    dumps(metadata or {}),
                    created_at,
                    now,
                ),
            )
            self._audit(
                conn,
                project_id,
                "",
                "notification.delivery",
                actor,
                {
                    "target_key": cleaned_target_key,
                    "channel": cleaned_channel,
                    "status": cleaned_status,
                    "comment_id": str(comment_id).strip(),
                    "payload_hash": cleaned_payload_hash,
                },
            )
            updated = conn.execute(
                """
                SELECT *
                FROM notification_deliveries
                WHERE project_id = ? AND target_key = ? AND channel = ?
                """,
                (project_id, cleaned_target_key, cleaned_channel),
            ).fetchone()
        if updated is None:
            raise ValueError(f"unknown notification target: {cleaned_target_key}")
        return _notification_delivery_from_row(updated)

    def notification_deliveries(self, project_id: str) -> list[NotificationDeliveryRecord]:
        """Return all notification delivery records for a project."""
        self.init_schema()
        with self.transaction(read_only=True) as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT *
                    FROM notification_deliveries
                    WHERE project_id = ?
                    ORDER BY updated_at, target_key, channel
                    """,
                    (project_id,),
                )
            )
        return [_notification_delivery_from_row(row) for row in rows]

    def record_evidence(
        self,
        project_id: str,
        task_id: str,
        uri: str,
        *,
        actor: str = "system",
    ) -> bool:
        """Record an evidence URI idempotently."""
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO evidence (project_id, task_id, uri, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, task_id, uri, iso()),
            )
            inserted = cur.rowcount > 0
            if inserted:
                self._audit(conn, project_id, task_id, "evidence.record", actor, {"uri": uri})
            return inserted

    def completion_integrity_issues(self, project_id: str) -> list[dict[str, Any]]:
        """Return done tasks whose evidence no longer satisfies current policy."""
        with self.transaction(read_only=True) as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT *
                    FROM tasks
                    WHERE project_id = ? AND status = 'done'
                    ORDER BY priority, task_id
                    """,
                    (project_id,),
                )
            )
            completion_records = _done_completion_records_by_task(conn, project_id)
            issues: list[dict[str, Any]] = []
            for row in rows:
                task_id = str(row["task_id"])
                try:
                    _validate_completion_policy(
                        conn,
                        row,
                        project_id=project_id,
                        task_id=task_id,
                        evidence=[],
                        direct_merge=False,
                    )
                except ValueError as exc:
                    completion_record = completion_records.get(task_id, {})
                    issues.append(
                        {
                            "task_id": task_id,
                            "title": str(row["title"]),
                            "status": str(row["status"]),
                            "kind": "completion_policy_unsatisfied",
                            "reason": str(exc),
                            "evidence": _completion_evidence(conn, project_id, task_id, []),
                            "completion_action": completion_record.get("action", ""),
                            "completed_by": completion_record.get("actor", ""),
                            "completed_at": completion_record.get("completed_at", ""),
                            "direct_merge": completion_record.get("direct_merge", False),
                        }
                    )
            return issues

    def recent_completion_records(self, project_id: str) -> list[dict[str, str]]:
        """Return audit-backed completion records newest first."""
        with self.transaction(read_only=True) as conn:
            return [
                {
                    "task_id": str(record["task_id"]),
                    "action": str(record["action"]),
                    "actor": str(record["actor"]),
                    "completed_at": str(record["completed_at"]),
                }
                for record in _done_completion_records_by_task(conn, project_id).values()
            ]

    def snapshot(self, project_id: str) -> dict[str, Any]:
        """Return a JSON-friendly project snapshot."""
        states = self.task_states(project_id)
        with self.transaction(read_only=True) as conn:
            if _table_exists(conn, "intake"):
                intake = [
                    intake_to_dict(_intake_from_row(row))
                    for row in conn.execute(
                        "SELECT * FROM intake WHERE project_id = ? ORDER BY created_at, intake_id",
                        (project_id,),
                    )
                ]
            else:
                intake = []
            if _table_exists(conn, "proposed_tasks"):
                proposed_tasks = [
                    proposed_task_to_dict(_proposed_task_from_row(row))
                    for row in conn.execute(
                        """
                        SELECT *
                        FROM proposed_tasks
                        WHERE project_id = ?
                        ORDER BY created_at, proposal_id
                        """,
                        (project_id,),
                    )
                ]
            else:
                proposed_tasks = []
            if _table_exists(conn, "interventions"):
                interventions = [
                    intervention_to_dict(_intervention_from_row(row))
                    for row in conn.execute(
                        """
                        SELECT *
                        FROM interventions
                        WHERE project_id = ?
                        ORDER BY created_at, intervention_id
                        """,
                        (project_id,),
                    )
                ]
            else:
                interventions = []
            if _table_exists(conn, "notification_deliveries"):
                notification_deliveries = [
                    notification_delivery_to_dict(_notification_delivery_from_row(row))
                    for row in conn.execute(
                        """
                        SELECT *
                        FROM notification_deliveries
                        WHERE project_id = ?
                        ORDER BY updated_at, target_key, channel
                        """,
                        (project_id,),
                    )
                ]
            else:
                notification_deliveries = []
            events = [
                {
                    "event_id": row["event_id"],
                    "kind": row["kind"],
                    "task_id": row["task_id"],
                    "payload": loads(row["payload_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in conn.execute(
                    "SELECT * FROM events WHERE project_id = ? ORDER BY created_at, event_id",
                    (project_id,),
                )
            ]
            audit = [
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "payload": loads(row["payload_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in conn.execute(
                    "SELECT * FROM audit_log WHERE project_id = ? ORDER BY id",
                    (project_id,),
                )
            ]
        return {
            "project_id": project_id,
            "generated_at": iso(),
            "tasks": [state_to_dict(state) for state in states],
            "intake": intake,
            "proposed_tasks": proposed_tasks,
            "interventions": interventions,
            "notification_deliveries": notification_deliveries,
            "events": events,
            "audit": audit,
        }

    def _finish_task(
        self,
        project_id: str,
        task_id: str,
        *,
        lease_token: str,
        status: str,
        action: str,
        evidence: list[str],
        agent_id: str,
        direct_merge: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now_dt = utcnow()
        with self.transaction(immediate=True) as conn:
            row = self._locked_task(
                conn,
                project_id,
                task_id,
                lease_token,
                agent_id=agent_id,
                now=now_dt,
            )
            if status == "done":
                _validate_completion_policy(
                    conn,
                    row,
                    project_id=project_id,
                    task_id=task_id,
                    evidence=evidence,
                    direct_merge=direct_merge,
                )
            actor = agent_id or str(row["lease_agent_id"])
            now = iso(now_dt)
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    lease_agent_id = '',
                    lease_token = '',
                    lease_expires_at = '',
                    claimed_at = '',
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (status, now, project_id, task_id),
            )
            for uri in evidence:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence (project_id, task_id, uri, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_id, task_id, uri, now),
                )
            self._audit(
                conn,
                project_id,
                task_id,
                action,
                actor,
                {
                    "evidence": evidence,
                    **({"direct_merge": True} if direct_merge else {}),
                    **(payload or {}),
                },
            )

    def _resolve_awaiting_task(
        self,
        project_id: str,
        task_id: str,
        *,
        allowed_current_statuses: set[str],
        status: str,
        action: str,
        evidence: list[str],
        agent_id: str,
        reason: str = "",
        direct_merge: bool = False,
    ) -> None:
        if status not in {"done", "failed"}:
            raise ValueError("resolution status must be one of: done, failed")
        if direct_merge and status != "done":
            raise ValueError("direct-merge override only applies to done resolution")
        if status == "failed" and not reason:
            raise ValueError("reason is required when resolving a task as failed")
        now = iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND task_id = ?",
                (project_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            current_status = str(row["status"])
            if current_status not in allowed_current_statuses:
                allowed = ", ".join(sorted(allowed_current_statuses))
                raise ValueError(
                    f"task must be in one of {allowed} to resolve; "
                    f"current status is {current_status}"
                )
            if status == "done":
                _validate_completion_policy(
                    conn,
                    row,
                    project_id=project_id,
                    task_id=task_id,
                    evidence=evidence,
                    direct_merge=direct_merge,
                )
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    lease_agent_id = '',
                    lease_token = '',
                    lease_expires_at = '',
                    claimed_at = '',
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (status, now, project_id, task_id),
            )
            for uri in evidence:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence (project_id, task_id, uri, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_id, task_id, uri, now),
                )
            self._audit(
                conn,
                project_id,
                task_id,
                action,
                agent_id or "system",
                {
                    "from_status": current_status,
                    "status": status,
                    "evidence": evidence,
                    **({"direct_merge": True} if direct_merge else {}),
                    **({"reason": reason} if reason else {}),
                },
            )

    def _locked_task(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        task_id: str,
        lease_token: str,
        *,
        agent_id: str = "",
        now: datetime | None = None,
    ) -> sqlite3.Row:
        if not lease_token:
            raise ValueError("lease token is required")
        row = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND task_id = ?",
            (project_id, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        stored_agent_id = str(row["lease_agent_id"])
        stored_token = str(row["lease_token"])
        if agent_id and stored_agent_id and agent_id != stored_agent_id:
            raise ValueError("task lease belongs to a different agent")
        if (
            not stored_token
            or stored_token != lease_token
            or str(row["status"]) not in ACTIVE_STATES
        ):
            raise ValueError("task lease token is invalid or task is not active")
        expires_at = parse_time(str(row["lease_expires_at"]))
        if expires_at is None or expires_at <= (now or utcnow()):
            raise ValueError("task lease is expired")
        return row

    def _row_is_ready(self, conn: sqlite3.Connection, project_id: str, row: sqlite3.Row) -> bool:
        if str(row["status"]) != "pending":
            return False
        deps = list(
            conn.execute(
                """
                SELECT dependency_task_id
                FROM dependencies
                WHERE project_id = ? AND task_id = ?
                """,
                (project_id, row["task_id"]),
            )
        )
        for dep in deps:
            dep_row = conn.execute(
                """
                SELECT status FROM tasks
                WHERE project_id = ? AND task_id = ?
                """,
                (project_id, dep["dependency_task_id"]),
            ).fetchone()
            if dep_row is None or str(dep_row["status"]) != "done":
                return False
        return True

    def _recover_stale_leases(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utcnow()
        rows = list(
            conn.execute(
                """
                SELECT task_id, lease_token, lease_expires_at
                FROM tasks
                WHERE project_id = ? AND status IN ('claimed', 'in_progress', 'waiting_evidence')
                """,
                (project_id,),
            )
        )
        for row in rows:
            expires_at = parse_time(str(row["lease_expires_at"]))
            has_lease = bool(str(row["lease_token"]))
            if has_lease and (expires_at is None or expires_at <= current):
                conn.execute(
                    """
                    UPDATE tasks SET
                        status = 'pending',
                        lease_agent_id = '',
                        lease_token = '',
                        lease_expires_at = '',
                        claimed_at = '',
                        updated_at = ?
                    WHERE project_id = ? AND task_id = ?
                    """,
                    (iso(current), project_id, row["task_id"]),
                )
                self._audit(
                    conn,
                    project_id,
                    str(row["task_id"]),
                    "task.lease_recovered",
                    "system",
                    {"expired_at": row["lease_expires_at"]},
                )

    def _role_matches(self, row: sqlite3.Row, role: str) -> bool:
        if not role:
            return True
        metadata = loads(str(row["metadata_json"]), {})
        roles = metadata.get("roles") or metadata.get("allowed_roles") or []
        if isinstance(roles, str):
            roles = [roles]
        if not isinstance(roles, (list, tuple, set)):
            return False
        return role in roles

    def _task_from_row(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            title=str(row["title"]),
            repo=str(row["repo"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            prompt_key=str(row["prompt_key"]),
            prompt_path=str(row["prompt_path"]),
            summary=str(row["summary"]),
            execution=loads(str(row["execution_json"]), {}),
            validation_checks=list(loads(str(row["validation_json"]), [])),
            next_action=str(row["next_action"]),
            metadata=loads(str(row["metadata_json"]), {}),
        )

    def _effective_task_from_row(
        self,
        row: sqlite3.Row,
        *,
        now: datetime,
        recover_stale_leases: bool,
    ) -> TaskRecord:
        task = self._task_from_row(row)
        if recover_stale_leases:
            return task
        if str(row["status"]) in ACTIVE_STATES and _row_has_stale_lease(row, now=now):
            return replace(task, status="pending")
        return task

    def _computed_state(self, status: str, requirements: list[RequirementState]) -> str:
        if status == "pending":
            return "blocked" if any(not item.satisfied for item in requirements) else "ready"
        return status

    def _audit(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        task_id: str,
        action: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_log (project_id, task_id, action, actor, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, task_id, action, actor, dumps(payload), iso()),
        )

    def _record_schema_metadata(self, conn: sqlite3.Connection) -> None:
        """Record and verify the database schema version."""
        row = conn.execute(
            "SELECT value FROM schema_metadata WHERE key = ?",
            (DB_SCHEMA_VERSION_KEY,),
        ).fetchone()
        if row is not None and str(row["value"]) != str(DB_SCHEMA_VERSION):
            raise ValueError(
                "unsupported database schema version "
                f"{row['value']}; supported version is {DB_SCHEMA_VERSION}"
            )
        now = iso()
        conn.execute(
            """
            INSERT INTO schema_metadata (key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (DB_SCHEMA_VERSION_KEY, str(DB_SCHEMA_VERSION), now, now),
        )


def _should_preserve_lease(row: sqlite3.Row | None, imported_status: str) -> bool:
    """Return whether an import should preserve an existing live lease."""
    if row is None:
        return False
    if imported_status not in ACTIVE_STATES and imported_status != "pending":
        return False
    return str(row["status"]) in ACTIVE_STATES and bool(str(row["lease_token"]))


def _proposal_contract_changes(
    previous: ProposedTaskRecord,
    updated: ProposedTaskRecord,
) -> dict[str, dict[str, Any]]:
    """Return field-level changes between two proposed task contracts."""
    previous_task = previous.task
    updated_task = updated.task
    candidates: list[tuple[str, Any, Any]] = [
        ("task.id", previous_task.task_id, updated_task.task_id),
        ("task.title", previous_task.title, updated_task.title),
        ("task.repo", previous_task.repo, updated_task.repo),
        ("task.summary", previous_task.summary, updated_task.summary),
        ("task.next_action", previous_task.next_action, updated_task.next_action),
        ("task.execution", previous_task.execution, updated_task.execution),
        (
            "task.validation_checks",
            previous_task.validation_checks,
            updated_task.validation_checks,
        ),
        ("task.metadata", previous_task.metadata, updated_task.metadata),
        ("requirements", previous.requirements, updated.requirements),
    ]
    return {
        name: {"previous": before, "updated": after}
        for name, before, after in candidates
        if before != after
    }


def _done_completion_records_by_task(
    conn: sqlite3.Connection,
    project_id: str,
) -> dict[str, dict[str, Any]]:
    """Return latest done-completion audit records by task in newest-first order."""
    rows = list(
        conn.execute(
            """
            SELECT task_id, action, actor, payload_json, created_at
            FROM audit_log
            WHERE project_id = ?
              AND task_id != ''
              AND action IN (
                'task.complete',
                'task.resolve_review',
                'task.resolve_integration'
              )
            ORDER BY id DESC
            """,
            (project_id,),
        )
    )
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row["task_id"])
        if task_id in records:
            continue
        action = str(row["action"])
        payload = loads(str(row["payload_json"]), {})
        if action != "task.complete" and payload.get("status") != "done":
            continue
        records[task_id] = {
            "task_id": task_id,
            "action": action,
            "actor": str(row["actor"]),
            "completed_at": str(row["created_at"]),
            "direct_merge": payload.get("direct_merge") is True,
        }
    return records


def _row_has_stale_lease(row: sqlite3.Row, *, now: datetime) -> bool:
    """Return whether a row has an expired or invalid live lease."""
    if not str(row["lease_token"]):
        return False
    expires_at = parse_time(str(row["lease_expires_at"]))
    return expires_at is None or expires_at <= now


def _validate_lease_seconds(lease_seconds: int) -> None:
    """Reject leases that would be immediately stale."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be greater than zero")


def _validate_completion_policy(
    conn: sqlite3.Connection,
    task_row: sqlite3.Row,
    *,
    project_id: str,
    task_id: str,
    evidence: list[str],
    direct_merge: bool,
) -> None:
    """Reject done transitions that do not satisfy task completion policy."""
    metadata = loads(str(task_row["metadata_json"]), {})
    policy = metadata.get("completion_policy") if isinstance(metadata, dict) else None
    policy_applies = isinstance(policy, dict) and policy.get("default") == "pr_or_review_required"
    if not policy_applies:
        if direct_merge:
            raise ValueError("direct-merge override is not allowed for this task")
        return

    all_evidence = _completion_evidence(conn, project_id, task_id, evidence)
    has_git = _has_evidence_prefix(all_evidence, "git:")
    has_integration = any(
        _has_evidence_prefix(all_evidence, prefix) for prefix in ("pr:", "review:", "integration:")
    )

    if direct_merge:
        if policy.get("direct_merge_override") is not True:
            raise ValueError("direct-merge override is not allowed for this task")
        if not has_git:
            raise ValueError("direct-merge completion requires git: evidence")
        return

    if not has_git and not has_integration:
        raise ValueError("completion requires git: and pr:/review:/integration: evidence")
    if not has_git:
        raise ValueError("completion requires git: evidence")
    if not has_integration:
        raise ValueError("completion requires pr:/review:/integration: evidence")


def _completion_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    task_id: str,
    new_evidence: list[str],
) -> list[str]:
    """Return existing and new completion evidence without duplicate URIs."""
    rows = conn.execute(
        """
        SELECT uri
        FROM evidence
        WHERE project_id = ? AND task_id = ?
        ORDER BY created_at, uri
        """,
        (project_id, task_id),
    )
    seen: set[str] = set()
    combined: list[str] = []
    for uri in [str(row["uri"]) for row in rows] + new_evidence:
        if uri not in seen:
            seen.add(uri)
            combined.append(uri)
    return combined


def _has_evidence_prefix(evidence: list[str], prefix: str) -> bool:
    """Return whether any evidence URI starts with the exact prefix."""
    return any(uri.startswith(prefix) for uri in evidence)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return whether a SQLite table exists."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _intake_exists(conn: sqlite3.Connection, project_id: str, intake_id: str) -> bool:
    """Return whether an intake record exists."""
    if not _table_exists(conn, "intake"):
        return False
    row = conn.execute(
        "SELECT 1 FROM intake WHERE project_id = ? AND intake_id = ?",
        (project_id, intake_id),
    ).fetchone()
    return row is not None


def _task_exists(conn: sqlite3.Connection, project_id: str, task_id: str) -> bool:
    """Return whether a live task exists."""
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE project_id = ? AND task_id = ?",
        (project_id, task_id),
    ).fetchone()
    return row is not None


def _intake_from_row(row: sqlite3.Row) -> IntakeRecord:
    """Convert a SQLite row to an intake record."""
    return IntakeRecord(
        intake_id=str(row["intake_id"]),
        text=str(row["text"]),
        kind=str(row["kind"]),
        source=str(row["source"]),
        repo=str(row["repo"]),
        status=str(row["status"]),
        tags=list(loads(str(row["tags_json"]), [])),
        metadata=loads(str(row["metadata_json"]), {}),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def intake_to_dict(intake: IntakeRecord) -> dict[str, Any]:
    """Convert an intake record to a JSON-friendly dictionary."""
    return {
        "id": intake.intake_id,
        "kind": intake.kind,
        "source": intake.source,
        "repo": intake.repo,
        "status": intake.status,
        "text": intake.text,
        "tags": intake.tags,
        "metadata": intake.metadata,
        "created_at": intake.created_at,
        "updated_at": intake.updated_at,
    }


def _intervention_from_row(row: sqlite3.Row) -> InterventionRecord:
    """Convert a SQLite row to an intervention record."""
    return InterventionRecord(
        intervention_id=str(row["intervention_id"]),
        task_id=str(row["task_id"]),
        reason=str(row["reason"]),
        status=str(row["status"]),
        summary=str(row["summary"]),
        metadata=loads(str(row["metadata_json"]), {}),
        resolution=str(row["resolution"]),
        evidence=list(loads(str(row["evidence_json"]), [])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        resolved_at=str(row["resolved_at"]),
        resolved_by=str(row["resolved_by"]),
    )


def intervention_to_dict(intervention: InterventionRecord) -> dict[str, Any]:
    """Convert an intervention record to a JSON-friendly dictionary."""
    return {
        "id": intervention.intervention_id,
        "task_id": intervention.task_id,
        "reason": intervention.reason,
        "status": intervention.status,
        "summary": intervention.summary,
        "metadata": intervention.metadata,
        "resolution": intervention.resolution,
        "evidence": intervention.evidence,
        "created_at": intervention.created_at,
        "updated_at": intervention.updated_at,
        "resolved_at": intervention.resolved_at,
        "resolved_by": intervention.resolved_by,
    }


def _notification_delivery_from_row(row: sqlite3.Row) -> NotificationDeliveryRecord:
    """Convert a SQLite row to a notification delivery record."""
    return NotificationDeliveryRecord(
        target_key=str(row["target_key"]),
        channel=str(row["channel"]),
        target=loads(str(row["target_json"]), {}),
        comment_id=str(row["comment_id"]),
        payload_hash=str(row["payload_hash"]),
        status=str(row["status"]),
        last_posted_at=str(row["last_posted_at"]),
        metadata=loads(str(row["metadata_json"]), {}),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def notification_delivery_to_dict(
    delivery: NotificationDeliveryRecord,
) -> dict[str, Any]:
    """Convert notification delivery state to a JSON-friendly dictionary."""
    return {
        "target_key": delivery.target_key,
        "channel": delivery.channel,
        "target": delivery.target,
        "comment_id": delivery.comment_id,
        "payload_hash": delivery.payload_hash,
        "status": delivery.status,
        "last_posted_at": delivery.last_posted_at,
        "metadata": delivery.metadata,
        "created_at": delivery.created_at,
        "updated_at": delivery.updated_at,
    }


def notebook_to_dict(record: NotebookRecord) -> dict[str, Any]:
    """Return a JSON-friendly notebook record."""
    return {
        "id": record.notebook_id,
        "kind": record.kind,
        "path": record.path,
        "title": record.title,
        "exists": record.exists,
        "size_bytes": record.size_bytes,
        "updated_at": record.updated_at,
    }


def _proposed_task_from_row(row: sqlite3.Row) -> ProposedTaskRecord:
    """Convert a SQLite row to a proposed task record."""
    contract = loads(str(row["contract_json"]), {})
    if not isinstance(contract, dict):
        contract = {}
    task_payload = contract.get("task", {})
    if not isinstance(task_payload, dict):
        task_payload = {}
    requirements = contract.get("requirements", [])
    if not isinstance(requirements, list):
        requirements = []
    return ProposedTaskRecord(
        proposal_id=str(row["proposal_id"]),
        intake_id=str(row["intake_id"]),
        status=str(row["status"]),
        task=_task_record_from_dict(task_payload),
        requirements=[
            {
                "kind": str(item.get("kind") or "task"),
                "task": str(item.get("task") or ""),
                "description": str(item.get("description") or ""),
            }
            for item in requirements
            if isinstance(item, dict)
        ],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _task_record_from_dict(payload: dict[str, Any]) -> TaskRecord:
    """Convert a task contract dictionary to a task record."""
    return TaskRecord(
        task_id=str(payload.get("id") or payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        repo=str(payload.get("repo") or ""),
        status=str(payload.get("status") or "pending"),
        priority=int(payload.get("priority") or 9999),
        prompt_key=str(payload.get("prompt_key") or ""),
        prompt_path=str(payload.get("prompt_path") or ""),
        summary=str(payload.get("summary") or ""),
        execution=dict(payload.get("execution") or {}),
        validation_checks=list(payload.get("validation_checks") or []),
        next_action=str(payload.get("next_action") or ""),
        evidence=list(payload.get("evidence") or []),
        metadata=dict(payload.get("metadata") or {}),
    )


def proposed_task_to_dict(proposal: ProposedTaskRecord) -> dict[str, Any]:
    """Convert a proposed task record to a JSON-friendly dictionary."""
    return {
        "id": proposal.proposal_id,
        "intake_id": proposal.intake_id,
        "status": proposal.status,
        "task": task_record_to_dict(proposal.task),
        "requirements": proposal.requirements,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
    }


def task_record_to_dict(task: TaskRecord) -> dict[str, Any]:
    """Convert a task record to a JSON-friendly dictionary."""
    return {
        "id": task.task_id,
        "title": task.title,
        "repo": task.repo,
        "status": task.status,
        "priority": task.priority,
        "prompt_key": task.prompt_key,
        "prompt_path": task.prompt_path,
        "summary": task.summary,
        "execution": task.execution,
        "validation_checks": task.validation_checks,
        "next_action": task.next_action,
        "evidence": task.evidence,
        "metadata": task.metadata,
    }


def state_to_dict(state: TaskState) -> dict[str, Any]:
    """Convert a task state to a JSON-friendly dictionary."""
    task = state.task
    return {
        "id": task.task_id,
        "title": task.title,
        "repo": task.repo,
        "manual_status": task.status,
        "state": state.state,
        "priority": task.priority,
        "prompt_key": task.prompt_key,
        "prompt_path": task.prompt_path,
        "summary": task.summary,
        "execution": task.execution,
        "validation_checks": task.validation_checks,
        "next_action": task.next_action,
        "metadata": task.metadata,
        "requirements": [
            {
                "description": item.description,
                "satisfied": item.satisfied,
                "detail": item.detail,
            }
            for item in state.requirements
        ],
        "lease_agent_id": state.lease_agent_id,
        "lease_expires_at": state.lease_expires_at,
        "evidence": state.evidence,
    }
