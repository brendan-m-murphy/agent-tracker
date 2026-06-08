"""Regression tests for the generic coordinator."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_tracker import cli  # noqa: E402
from agent_tracker.config import load_config  # noqa: E402
from agent_tracker.mcp_tools import AgentTrackerTools  # noqa: E402
from agent_tracker.service import Coordinator  # noqa: E402
from agent_tracker.skill_bootstrap import install_skill, vendored_skill_path  # noqa: E402


def write_project(root: Path) -> Path:
    """Write a toy non-project-specific config and task plan."""
    (root / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "foundation",
                        "title": "Foundation",
                        "repo": "library",
                        "status": "done",
                        "priority": 1,
                    },
                    {
                        "id": "ready",
                        "title": "Ready",
                        "repo": "library",
                        "status": "pending",
                        "priority": 2,
                        "summary": "Ready to run.",
                        "requirements": [
                            {
                                "kind": "task",
                                "task": "foundation",
                                "description": "Needs foundation.",
                            }
                        ],
                        "metadata": {"roles": ["worker"]},
                    },
                    {
                        "id": "blocked",
                        "title": "Blocked",
                        "repo": "library",
                        "status": "pending",
                        "priority": 3,
                        "requirements": [{"kind": "task", "task": "ready"}],
                    },
                    {
                        "id": "deferred",
                        "title": "Deferred",
                        "status": "deferred",
                        "priority": 4,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path = root / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "project_id": "toy",
                "name": "Toy Project",
                "db_path": "state.sqlite",
                "task_plan_path": "tasks.json",
                "importer": "agent_tracker.importers:JsonTaskImporter",
                "prompt_renderer": "agent_tracker.rendering:DefaultPromptRenderer",
                "exporter": "agent_tracker.exporters:JsonSnapshotExporter",
                "export_path": "snapshot.json",
                "spool": {
                    "inbox": "spool/inbox",
                    "done": "spool/done",
                    "error": "spool/error",
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_import_evaluates_ready_blocked_and_deferred_tasks(tmp_path: Path) -> None:
    """Imported task dependencies determine computed state."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    states = {state.task.task_id: state for state in coord.task_states()}

    assert states["foundation"].state == "done"
    assert states["ready"].state == "ready"
    assert states["blocked"].state == "blocked"
    assert states["deferred"].state == "deferred"


def test_claim_heartbeat_complete_and_unblock_downstream(tmp_path: Path) -> None:
    """Claimed work can be heartbeated, completed, and unblock dependents."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    claim = coord.claim(agent_id="agent-1", role="worker")
    heartbeat = coord.heartbeat("ready", lease_token=claim.lease_token)
    coord.complete(
        "ready",
        lease_token=heartbeat.lease_token,
        evidence=["evidence://ready"],
    )
    states = {state.task.task_id: state for state in coord.task_states()}

    assert claim.task_id == "ready"
    assert states["ready"].state == "done"
    assert states["ready"].evidence == ["evidence://ready"]
    assert states["blocked"].state == "ready"


def test_stale_claim_is_recovered(tmp_path: Path) -> None:
    """Expired task leases are returned to pending and computed as ready."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    coord.claim(agent_id="agent-1", task_id="ready")
    with coord.store.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            ("2000-01-01T00:00:00+00:00", config.project_id, "ready"),
        )
    recovered = coord.get_task("ready")

    assert recovered.state == "ready"
    assert recovered.lease_token == ""


def test_lease_validation_rejects_nonpositive_duration_and_wrong_agent(tmp_path: Path) -> None:
    """Lease operations reject unusable durations and mismatched agent IDs."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with pytest.raises(ValueError, match="greater than zero"):
        coord.claim(agent_id="agent-1", task_id="ready", lease_seconds=0)
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    with pytest.raises(ValueError, match="different agent"):
        coord.heartbeat("ready", lease_token=claim.lease_token, agent_id="agent-2")
    with coord.store.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            ("not-a-timestamp", config.project_id, "ready"),
        )
    with pytest.raises(ValueError, match="expired"):
        coord.complete("ready", lease_token=claim.lease_token, agent_id="agent-1")


def test_reimport_preserves_live_lease_but_clears_when_source_is_terminal(
    tmp_path: Path,
) -> None:
    """Runtime reconciliation must be explicit before source statuses clear leases."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()

    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.import_tasks()
    claimed = coord.get_task("ready")

    task_plan = tmp_path / "tasks.json"
    data = json.loads(task_plan.read_text(encoding="utf-8"))
    for raw_task in data["tasks"]:
        if raw_task["id"] == "ready":
            raw_task["status"] = "done"
    task_plan.write_text(json.dumps(data), encoding="utf-8")
    coord.import_tasks()
    preserved = coord.get_task("ready")
    coord.import_tasks(reconcile_runtime_state=True)
    completed = coord.get_task("ready")

    assert claimed.state == "claimed"
    assert claimed.lease_token == claim.lease_token
    assert preserved.state == "claimed"
    assert preserved.lease_token == claim.lease_token
    assert completed.state == "done"
    assert completed.lease_token == ""
    assert completed.lease_agent_id == ""


def test_import_removes_tasks_deleted_from_source_only_when_reconciling(tmp_path: Path) -> None:
    """Definition imports preserve live tasks unless reconciliation is explicit."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()

    task_plan = tmp_path / "tasks.json"
    data = json.loads(task_plan.read_text(encoding="utf-8"))
    data["tasks"] = [task for task in data["tasks"] if task["id"] != "deferred"]
    task_plan.write_text(json.dumps(data), encoding="utf-8")
    coord.import_tasks()
    preserved_task_ids = {state.task.task_id for state in coord.task_states()}
    coord.import_tasks(reconcile_runtime_state=True)
    reconciled_task_ids = {state.task.task_id for state in coord.task_states()}

    assert "deferred" in preserved_task_ids
    assert "deferred" not in reconciled_task_ids


def test_import_rejects_unknown_task_dependency(tmp_path: Path) -> None:
    """Task dependencies must point to tasks in the same import payload."""
    config_path = write_project(tmp_path)
    task_plan = tmp_path / "tasks.json"
    data = json.loads(task_plan.read_text(encoding="utf-8"))
    data["tasks"].append(
        {
            "id": "bad",
            "status": "pending",
            "requirements": [{"kind": "task", "task": "missing"}],
        }
    )
    task_plan.write_text(json.dumps(data), encoding="utf-8")
    config = load_config(config_path)
    coord = Coordinator(config)

    with pytest.raises(ValueError, match="unknown dependency missing"):
        coord.import_tasks()


def test_event_ingestion_is_idempotent_and_spool_moves_files(tmp_path: Path) -> None:
    """Event IDs are unique and spool files move to done or error paths."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    inbox = tmp_path / "spool" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "event.json").write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    result = coord.ingest_spool()
    duplicate = coord.record_event({"event_id": "evt-1", "kind": "sample"})
    moved = (tmp_path / "spool" / "done" / "event.json").exists()

    assert result == {"processed": 1, "inserted": 1, "errors": 0}
    assert duplicate is False
    assert moved is True


def test_event_ingestion_rejects_missing_event_id(tmp_path: Path) -> None:
    """Events without an ID are invalid instead of being stored as None."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with pytest.raises(ValueError, match="event_id or id"):
        coord.record_event({"kind": "sample"})


def test_project_root_plugin_loads_from_config_directory(tmp_path: Path) -> None:
    """Configured project plugins can live below the project config directory."""
    old_path = list(sys.path)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "sample_plugin.py").write_text(
        "\n".join(
            [
                "from agent_tracker.models import TaskRecord",
                "",
                "class SampleImporter:",
                "    def load_tasks(self, config):",
                "        return [TaskRecord(task_id='sample', title='Sample')], []",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "project_id": "plugin-toy",
                "db_path": "state.sqlite",
                "importer": "plugins.sample_plugin:SampleImporter",
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    coord = Coordinator(config)

    try:
        imported = coord.import_tasks()
    finally:
        sys.path[:] = old_path

    assert imported == 1


def test_state_and_task_source_roots_are_scoped_independently(tmp_path: Path) -> None:
    """Runtime state can live outside the task-definition source tree."""
    definition_root = tmp_path / "definitions"
    definition_root.mkdir()
    config_path = write_project(definition_root)
    state_root = tmp_path / "runtime"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["state_root"] = str(state_root)
    data["task_source_root"] = str(definition_root)
    config_path.write_text(json.dumps(data), encoding="utf-8")
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()

    assert config.db_path == state_root / "state.sqlite"
    assert (state_root / "state.sqlite").exists()
    assert not (definition_root / "state.sqlite").exists()
    assert coord.get_task("ready").state == "ready"


def test_noncanonical_config_refuses_mutating_commands(tmp_path: Path) -> None:
    """Copied configs cannot silently mutate an independent project database."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    config_path = write_project(canonical_root)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["canonical_config_path"] = str(config_path)
    data["state_root"] = str(canonical_root)
    data["task_source_root"] = str(canonical_root)
    config_path.write_text(json.dumps(data), encoding="utf-8")
    Coordinator(load_config(config_path)).import_tasks()
    copied_root = tmp_path / "copied"
    copied_root.mkdir()
    copied_config = copied_root / "project.json"
    copied_config.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "claim",
                "--config",
                str(copied_config),
                "--agent",
                "agent-1",
                "--role",
                "worker",
            ]
        )

    assert code == 1
    assert "canonical config" in stderr.getvalue()
    assert not (copied_root / "state.sqlite").exists()


def test_relative_canonical_config_path_is_rejected(tmp_path: Path) -> None:
    """Relative canonical paths cannot make copied configs self-authoritative."""
    config_path = write_project(tmp_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["canonical_config_path"] = "project.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(["status", "--config", str(config_path)])

    assert code == 1
    assert "canonical_config_path must be absolute" in stderr.getvalue()
    assert not (tmp_path / "state.sqlite").exists()


def test_status_inspection_does_not_recover_stale_leases_without_flag(tmp_path: Path) -> None:
    """Read-only status can inspect stale leases without mutating storage."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    with coord.store.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            ("2000-01-01T00:00:00+00:00", config.project_id, "ready"),
        )

    assert cli.main(["status", "--config", str(config_path)]) == 0
    with coord.store.transaction() as conn:
        inspected = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()
    assert inspected["status"] == "claimed"
    assert inspected["lease_token"] == claim.lease_token

    assert cli.main(["status", "--config", str(config_path), "--recover-stale-leases"]) == 0
    with coord.store.transaction() as conn:
        recovered = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()
    assert recovered["status"] == "pending"
    assert recovered["lease_token"] == ""


def test_mcp_handlers_claim_and_complete(tmp_path: Path) -> None:
    """MCP-friendly handlers expose the same queue operations."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)

    claim = tools.claim_task(agent_id="agent-1", task_id="ready")
    tools.complete_task(
        task_id="ready",
        lease_token=claim["lease_token"],
        evidence=["evidence://ready"],
    )
    status = tools.get_project_status()

    ready_state = next(task for task in status["tasks"] if task["id"] == "ready")
    assert ready_state["state"] == "done"
    assert ready_state["evidence"] == ["evidence://ready"]


def test_mcp_ready_task_limit_rejects_negative_values(tmp_path: Path) -> None:
    """MCP handlers expose service validation for invalid limits."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)

    with pytest.raises(ValueError, match="greater than or equal to zero"):
        tools.list_ready_tasks(limit=-1)


def test_cli_reports_validation_errors_without_traceback(tmp_path: Path) -> None:
    """CLI validation failures are concise command errors."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "claim",
                "--config",
                str(config_path),
                "--agent",
                "agent-1",
                "--role",
                "missing-role",
            ]
        )

    assert code == 1
    assert "no matching ready task" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_read_only_cli_missing_database_does_not_create_state(tmp_path: Path) -> None:
    """Read-only CLI storage failures are concise and do not create SQLite files."""
    config_path = write_project(tmp_path)
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(["status", "--config", str(config_path)])

    assert code == 1
    assert "unable to open database file" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
    assert not (tmp_path / "state.sqlite").exists()


def test_export_writes_snapshot(tmp_path: Path) -> None:
    """Configured exporters can write audit snapshots."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    paths = coord.export()
    snapshot = json.loads(Path(paths[0]).read_text(encoding="utf-8"))

    assert len(paths) == 1
    assert snapshot["project_id"] == "toy"
    assert snapshot["tasks"]


def test_core_contains_no_project_specific_terms() -> None:
    """The reusable package core must stay project-agnostic."""
    forbidden = ["hpc", "slurm", "test_inversions", "acrg"]
    offenders = []
    for path in (ROOT / "src" / "agent_tracker").glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            if term in text:
                offenders.append(f"{path.name}:{term}")

    assert offenders == []


def test_project_manager_skill_is_vendored_and_installable(tmp_path: Path) -> None:
    """The reusable project-manager skill can be bootstrapped for new installs."""
    source = vendored_skill_path("project-manager")
    installed = install_skill(destination_root=tmp_path, dry_run=False)

    assert (source / "SKILL.md").exists()
    assert installed == tmp_path / "project-manager"
    assert (installed / "SKILL.md").exists()
    assert (installed / "agents" / "openai.yaml").exists()
    assert "name: project-manager" in (installed / "SKILL.md").read_text(encoding="utf-8")


def test_project_manager_skill_has_no_project_specific_terms() -> None:
    """The vendored skill must stay generic across agent-tracker projects."""
    source = vendored_skill_path("project-manager")
    forbidden = ["hpc", "slurm", "test_inversions", "acrg"]
    offenders = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            if term in text:
                offenders.append(f"{path.relative_to(source)}:{term}")

    assert offenders == []
