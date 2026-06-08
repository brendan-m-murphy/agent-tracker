"""SQLite persistence for the generic coordinator."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from agent_tracker.config import ProjectConfig
from agent_tracker.models import (
    ACTIVE_STATES,
    DependencyRecord,
    EventRecord,
    MANUAL_STATES,
    Claim,
    RequirementState,
    TaskRecord,
    TaskState,
)


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
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a transaction."""
        conn = self.connect()
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
    ) -> None:
        """Import tasks and dependencies into the store."""
        self.upsert_project(config)
        now = iso()
        with self.transaction(immediate=True) as conn:
            for task in tasks:
                if task.status not in MANUAL_STATES:
                    raise ValueError(f"invalid task status for {task.task_id}: {task.status}")
                conn.execute(
                    """
                    INSERT INTO tasks (
                        project_id, task_id, title, repo, status, priority, prompt_key,
                        prompt_path, summary, execution_json, validation_json, next_action,
                        metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        updated_at=excluded.updated_at
                    """,
                    (
                        config.project_id,
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
                for uri in task.evidence:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO evidence (project_id, task_id, uri, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (config.project_id, task.task_id, uri, now),
                    )
            conn.execute("DELETE FROM dependencies WHERE project_id = ?", (config.project_id,))
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
                {"tasks": len(tasks), "dependencies": len(dependencies)},
            )

    def task_states(self, project_id: str) -> list[TaskState]:
        """Return evaluated task states ordered by priority."""
        with self.transaction() as conn:
            self._recover_stale_leases(conn, project_id)
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
                    "SELECT task_id, uri FROM evidence WHERE project_id = ? ORDER BY created_at, uri",
                    (project_id,),
                )
            )
        tasks_by_id = {str(row["task_id"]): self._task_from_row(row) for row in rows}
        status_by_id = {task_id: task.status for task_id, task in tasks_by_id.items()}
        dependencies_by_task: dict[str, list[sqlite3.Row]] = {}
        for dep_row in dep_rows:
            dependencies_by_task.setdefault(str(dep_row["task_id"]), []).append(dep_row)
        evidence_by_task: dict[str, list[str]] = {}
        for evidence_row in evidence_rows:
            evidence_by_task.setdefault(str(evidence_row["task_id"]), []).append(str(evidence_row["uri"]))

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
            states.append(
                TaskState(
                    task=task,
                    state=state,
                    requirements=requirements,
                    lease_agent_id=str(row["lease_agent_id"]),
                    lease_token=str(row["lease_token"]),
                    lease_expires_at=str(row["lease_expires_at"]),
                    evidence=evidence_by_task.get(task_id, []),
                )
            )
        return states

    def get_task_state(self, project_id: str, task_id: str) -> TaskState:
        """Return one evaluated task state."""
        for state in self.task_states(project_id):
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
        expires = utcnow() + timedelta(seconds=lease_seconds)
        with self.transaction(immediate=True) as conn:
            row = self._locked_task(conn, project_id, task_id, lease_token)
            conn.execute(
                """
                UPDATE tasks SET
                    status = 'in_progress',
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (iso(expires), iso(), project_id, task_id),
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
    ) -> None:
        """Mark a leased task done."""
        self._finish_task(
            project_id,
            task_id,
            lease_token=lease_token,
            status="done",
            action="task.complete",
            evidence=evidence or [],
            agent_id=agent_id,
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

    def snapshot(self, project_id: str) -> dict[str, Any]:
        """Return a JSON-friendly project snapshot."""
        states = self.task_states(project_id)
        with self.transaction() as conn:
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
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction(immediate=True) as conn:
            row = self._locked_task(conn, project_id, task_id, lease_token)
            actor = agent_id or str(row["lease_agent_id"])
            now = iso()
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    lease_agent_id = '',
                    lease_token = '',
                    lease_expires_at = '',
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
                {"evidence": evidence, **(payload or {})},
            )

    def _locked_task(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        task_id: str,
        lease_token: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND task_id = ?",
            (project_id, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        if str(row["lease_token"]) != lease_token or str(row["status"]) not in ACTIVE_STATES:
            raise ValueError("task lease token is invalid or task is not active")
        expires_at = parse_time(str(row["lease_expires_at"]))
        if expires_at is not None and expires_at <= utcnow():
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
                SELECT task_id, lease_expires_at
                FROM tasks
                WHERE project_id = ? AND status IN ('claimed', 'in_progress', 'waiting_evidence')
                """,
                (project_id,),
            )
        )
        for row in rows:
            expires_at = parse_time(str(row["lease_expires_at"]))
            if expires_at is not None and expires_at <= current:
                conn.execute(
                    """
                    UPDATE tasks SET
                        status = 'pending',
                        lease_agent_id = '',
                        lease_token = '',
                        lease_expires_at = '',
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
