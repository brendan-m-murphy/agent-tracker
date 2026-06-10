"""Regression tests for the generic coordinator."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_tracker import cli  # noqa: E402
from agent_tracker import service as service_module  # noqa: E402
from agent_tracker import skill_bootstrap as skill_bootstrap_module  # noqa: E402
from agent_tracker.config import (  # noqa: E402
    PROJECT_CONFIG_ENV_VAR,
    PROJECT_DB_ENV_VAR,
    SUPPORTED_CONFIG_SCHEMA_VERSION,
    load_config,
)
from agent_tracker.db import DB_SCHEMA_VERSION, DB_SCHEMA_VERSION_KEY  # noqa: E402
from agent_tracker.mcp_tools import AgentTrackerTools  # noqa: E402
from agent_tracker.models import INTEGRATION_STATES, REVIEW_STATES, InterventionRecord  # noqa: E402
from agent_tracker.service import Coordinator  # noqa: E402
from agent_tracker.skill_bootstrap import (  # noqa: E402
    available_skill_names,
    install_skill,
    install_skills,
    vendored_skill_path,
)


def assert_no_box_drawing(text: str) -> None:
    """Assert text output is free of Rich-style box-drawing characters."""
    assert not any(0x2500 <= ord(char) <= 0x257F for char in text)


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


def write_overview_project(root: Path) -> Path:
    """Write a project with enough tasks to exercise overview groups."""
    config_path = write_project(root)
    task_path = root / "tasks.json"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload["tasks"].extend(
        [
            {
                "id": "other-ready",
                "title": "Other Ready",
                "status": "pending",
                "priority": 4,
                "next_action": "Pick up the remaining ready task.",
                "requirements": [{"kind": "task", "task": "foundation"}],
            },
            {
                "id": "review-task",
                "title": "Review Task",
                "status": "pending",
                "priority": 5,
                "requirements": [{"kind": "task", "task": "foundation"}],
            },
            {
                "id": "integration-task",
                "title": "Integration Task",
                "status": "pending",
                "priority": 6,
                "requirements": [{"kind": "task", "task": "foundation"}],
            },
            {
                "id": "done-a",
                "title": "Done A",
                "status": "pending",
                "priority": 7,
                "requirements": [{"kind": "task", "task": "foundation"}],
            },
            {
                "id": "done-b",
                "title": "Done B",
                "status": "pending",
                "priority": 8,
                "requirements": [{"kind": "task", "task": "foundation"}],
            },
        ]
    )
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


class LoopbackSFTPServer:
    """Run an AsyncSSH SFTP server in a background thread for sync tests."""

    def __init__(self, root: Path, asyncssh_module: Any):
        self.root = root
        self.asyncssh = asyncssh_module
        self.host_key = asyncssh_module.generate_private_key("ssh-rsa")
        self.port = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> LoopbackSFTPServer:
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise TimeoutError("timed out starting loopback SFTP server")
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def serve() -> None:
            asyncssh = self.asyncssh

            class NoAuthServer(asyncssh.SSHServer):  # type: ignore[name-defined]
                def begin_auth(self, username: str) -> bool:
                    return False

            server = await asyncssh.listen(
                "127.0.0.1",
                0,
                server_factory=NoAuthServer,
                server_host_keys=[self.host_key],
                sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(self.root)),
            )
            self.port = server.get_port()
            self._ready.set()
            try:
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)
            finally:
                server.close()
                await server.wait_closed()

        try:
            loop.run_until_complete(serve())
        except BaseException as exc:  # pragma: no cover - surfaced through __enter__
            self._error = exc
            self._ready.set()
        finally:
            loop.close()


def configure_sftp_spool(config_path: Path, port: int, remote_path: str = "/outbox") -> None:
    """Point a test project at a loopback SFTP remote inbox."""
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = f"sftp://127.0.0.1:{port}{remote_path}"
    config_payload["spool"]["ssh"] = {
        "username": "agent",
        "known_hosts": "none",
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")


def prepare_overview_state(root: Path) -> tuple[Path, Coordinator]:
    """Create a mixed queue state for overview tests."""
    config_path = write_overview_project(root)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()

    coord.claim(agent_id="agent-1", task_id="ready")
    review_claim = coord.claim(agent_id="agent-2", task_id="review-task")
    coord.submit_review(
        "review-task",
        lease_token=review_claim.lease_token,
        agent_id="agent-2",
        evidence=["pr:review-task"],
    )
    integration_claim = coord.claim(agent_id="agent-3", task_id="integration-task")
    coord.await_integration(
        "integration-task",
        lease_token=integration_claim.lease_token,
        agent_id="agent-3",
        status="awaiting_merge",
        evidence=["git:integration-task"],
    )
    done_a_claim = coord.claim(agent_id="agent-4", task_id="done-a")
    coord.complete("done-a", lease_token=done_a_claim.lease_token, evidence=["git:done-a"])
    done_b_claim = coord.claim(agent_id="agent-5", task_id="done-b")
    coord.complete("done-b", lease_token=done_b_claim.lease_token, evidence=["git:done-b"])
    return config_path, coord


def write_project_with_completion_policy(root: Path, policy: object) -> Path:
    """Write a toy project whose ready task has completion policy metadata."""
    root.mkdir(parents=True, exist_ok=True)
    config_path = write_project(root)
    task_path = root / "tasks.json"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    for task in payload["tasks"]:
        if task["id"] == "ready":
            task.setdefault("metadata", {})["completion_policy"] = policy
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def write_project_with_workspace(root: Path, workspace: Path) -> Path:
    """Write a toy project with one local worker workspace."""
    root.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = write_project(root)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["workspaces"] = {
        "hpc": {
            "kind": "local",
            "path": str(workspace),
            "config_path": "agent-tracker.config.json",
            "spool_outbox": ".agent-tracker/spool/outbox",
            "artifacts_path": "results/worker-launches",
            "roles": ["agent-coordinator"],
            "capabilities": ["local-worker", "summary-test"],
        }
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    return config_path


PR_OR_REVIEW_POLICY = {
    "default": "pr_or_review_required",
    "direct_merge_override": True,
}


@pytest.fixture(autouse=True)
def clear_agent_tracker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent agent-tracker env defaults from leaking into unrelated tests."""
    monkeypatch.delenv(PROJECT_CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv(PROJECT_DB_ENV_VAR, raising=False)


def test_config_schema_version_defaults_to_current_version(tmp_path: Path) -> None:
    """Configs without an explicit schema version use the current schema."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)

    assert config.config_schema_version == SUPPORTED_CONFIG_SCHEMA_VERSION
    assert config.raw["config_schema_version"] == SUPPORTED_CONFIG_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], "project config must be a JSON object"),
        ({}, "config field 'project_id' is required"),
        ({"project_id": ""}, "config field 'project_id' must be non-empty"),
        ({"project_id": 123}, "config field 'project_id' must be a string"),
        (
            {"project_id": "toy", "config_schema_version": "1"},
            "config_schema_version must be an integer",
        ),
        (
            {"project_id": "toy", "config_schema_version": 2},
            "unsupported config_schema_version 2",
        ),
        (
            {"project_id": "toy", "state_root": {}},
            "config field 'state_root' must be a string",
        ),
        ({"project_id": "toy", "spool": []}, "config field 'spool' must be an object"),
        (
            {"project_id": "toy", "spool": {"inbox": 1}},
            "config field 'spool.inbox' must be a string",
        ),
        (
            {"project_id": "toy", "spool": {"remote_inbox": 1}},
            "config field 'spool.remote_inbox' must be a string",
        ),
        (
            {"project_id": "toy", "spool": {"ssh": []}},
            "config field 'spool.ssh' must be an object",
        ),
        (
            {"project_id": "toy", "spool": {"ssh": {"known_hosts": False}}},
            "config field 'spool.ssh.known_hosts' must be a string",
        ),
        (
            {"project_id": "toy", "spool": {"ssh": {"client_keys": [1]}}},
            "config field 'spool.ssh.client_keys' must be a string or list of strings",
        ),
        (
            {"project_id": "toy", "workspaces": []},
            "config field 'workspaces' must be an object",
        ),
        (
            {"project_id": "toy", "workspaces": {"hpc": []}},
            "config field 'workspaces.hpc' must be an object",
        ),
        (
            {"project_id": "toy", "workspaces": {"hpc": {"kind": "local"}}},
            "config field 'workspaces.hpc.path' is required",
        ),
        (
            {
                "project_id": "toy",
                "workspaces": {"remote": {"kind": "ssh", "host": "host"}},
            },
            "config field 'workspaces.remote.remote_path' is required",
        ),
        (
            {
                "project_id": "toy",
                "workspaces": {"hpc": {"path": ".", "capabilities": [1]}},
            },
            "config field 'workspaces.hpc.capabilities' must be a string or list of strings",
        ),
        (
            {"project_id": "toy", "pr_notification_setup_checker": []},
            "config field 'pr_notification_setup_checker' must be a string",
        ),
        (
            {"project_id": "toy", "notifications": []},
            "config field 'notifications' must be an object",
        ),
        (
            {"project_id": "toy", "notifications": {"github": {"allow_live": "yes"}}},
            "config field 'notifications.github.allow_live' must be a boolean",
        ),
    ],
)
def test_cli_reports_malformed_config_errors_without_traceback(
    tmp_path: Path,
    payload: object,
    expected: str,
) -> None:
    """Malformed project configs fail before touching SQLite state."""
    config_path = tmp_path / "project.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(["status", "--config", str(config_path)])

    assert code == 1
    assert expected in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
    assert not (tmp_path / ".agent-tracker" / "state.sqlite").exists()
    assert not (tmp_path / "state.sqlite").exists()


def test_cli_uses_config_env_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI commands use the config env var when --config is omitted."""
    config_path = write_project(tmp_path)
    monkeypatch.setenv(PROJECT_CONFIG_ENV_VAR, str(config_path))

    code = cli.main(["import"])

    assert code == 0
    assert Coordinator(load_config(config_path)).get_task("ready").state == "ready"


def test_cli_uses_db_env_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI commands use the database env var when --db is omitted."""
    config_path = write_project(tmp_path)
    env_db_path = tmp_path / "env-state.sqlite"
    monkeypatch.setenv(PROJECT_CONFIG_ENV_VAR, str(config_path))
    monkeypatch.setenv(PROJECT_DB_ENV_VAR, str(env_db_path))

    code = cli.main(["import"])

    assert code == 0
    assert env_db_path.exists()
    assert not (tmp_path / "state.sqlite").exists()
    assert (
        Coordinator(load_config(config_path), db_path=env_db_path).get_task("ready").state
        == "ready"
    )


def test_cli_explicit_config_and_db_override_env_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit --config and --db values take precedence over env defaults."""
    env_root = tmp_path / "env"
    env_root.mkdir()
    env_config = write_project(env_root)
    env_data = json.loads(env_config.read_text(encoding="utf-8"))
    env_data["project_id"] = "env-toy"
    env_config.write_text(json.dumps(env_data), encoding="utf-8")
    Coordinator(load_config(env_config)).import_tasks()

    explicit_root = tmp_path / "explicit"
    explicit_root.mkdir()
    explicit_config = write_project(explicit_root)
    explicit_data = json.loads(explicit_config.read_text(encoding="utf-8"))
    explicit_data["project_id"] = "explicit-toy"
    explicit_config.write_text(json.dumps(explicit_data), encoding="utf-8")
    explicit_db_path = tmp_path / "explicit-db.sqlite"
    Coordinator(load_config(explicit_config), db_path=explicit_db_path).import_tasks()

    env_db_path = tmp_path / "env-db.sqlite"
    monkeypatch.setenv(PROJECT_CONFIG_ENV_VAR, str(env_config))
    monkeypatch.setenv(PROJECT_DB_ENV_VAR, str(env_db_path))

    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "status",
                "--config",
                str(explicit_config),
                "--db",
                str(explicit_db_path),
                "--json",
            ]
        )

    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert payload["project_id"] == "explicit-toy"
    assert payload["db_path"] == str(explicit_db_path)
    assert not env_db_path.exists()


def test_cli_init_project_bootstraps_plugin_free_layout(tmp_path: Path) -> None:
    """A new tracker can be created and operated with built-in defaults."""
    project_root = tmp_path / "tracking"
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "init-project",
                str(project_root),
                "--project-id",
                "demo",
                "--name",
                "Demo Tracker",
            ]
        )

    config_path = project_root / "project.json"
    task_plan_path = project_root / "tasks.json"
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    task_payload = json.loads(task_plan_path.read_text(encoding="utf-8"))

    assert code == 0
    assert "agent-tracker init --config" in stdout.getvalue()
    assert config_payload == {
        "config_schema_version": SUPPORTED_CONFIG_SCHEMA_VERSION,
        "project_id": "demo",
        "name": "Demo Tracker",
        "db_path": ".agent-tracker/state.sqlite",
        "task_plan_path": "tasks.json",
        "export_path": "exports/snapshot.json",
        "spool": {
            "inbox": "spool/inbox",
            "done": "spool/done",
            "error": "spool/error",
        },
    }
    assert "importer" not in config_payload
    assert "prompt_renderer" not in config_payload
    assert "exporter" not in config_payload
    assert task_payload["tasks"][0]["id"] == "first-task"
    assert (project_root / ".agent-tracker").is_dir()
    assert (project_root / "spool" / "inbox").is_dir()
    assert (project_root / "spool" / "done").is_dir()
    assert (project_root / "spool" / "error").is_dir()
    assert (project_root / "exports").is_dir()
    assert (project_root / "notebooks" / "repos").is_dir()
    assert ".agent-tracker/" in (project_root / ".gitignore").read_text(encoding="utf-8")

    assert cli.main(["init", "--config", str(config_path)]) == 0
    assert cli.main(["import", "--config", str(config_path)]) == 0
    coord = Coordinator(load_config(config_path))
    state = coord.get_task("first-task")

    assert state.state == "ready"
    assert state.task.title == "Write the first task"
    assert "first-task" in coord.render_prompt("first-task", markdown=True)


def test_cli_init_project_can_write_canonical_config(tmp_path: Path) -> None:
    """Bootstrap can opt into copied-worktree mutation safety from the start."""
    project_root = tmp_path / "tracking"

    assert (
        cli.main(
            [
                "init-project",
                str(project_root),
                "--project-id",
                "demo",
                "--canonical-config",
                "--no-gitignore",
            ]
        )
        == 0
    )

    config_path = project_root / "project.json"
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config = load_config(config_path)

    assert config_payload["canonical_config_path"] == str(config_path.resolve())
    assert config_payload["state_root"] == str(project_root.resolve())
    assert config_payload["task_source_root"] == str(project_root.resolve())
    assert config.canonical_config_path == config_path.resolve()
    assert not (project_root / ".gitignore").exists()


def test_cli_init_project_refuses_to_overwrite_files(tmp_path: Path) -> None:
    """Existing project definitions are protected unless --force is explicit."""
    project_root = tmp_path / "tracking"
    project_root.mkdir()
    (project_root / "project.json").write_text("{}", encoding="utf-8")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(["init-project", str(project_root), "--project-id", "demo"])

    assert code == 1
    assert "refusing to overwrite existing file" in stderr.getvalue()
    assert json.loads((project_root / "project.json").read_text(encoding="utf-8")) == {}
    assert cli.main(["init-project", str(project_root), "--project-id", "demo", "--force"]) == 0
    assert (
        json.loads((project_root / "project.json").read_text(encoding="utf-8"))["project_id"]
        == "demo"
    )


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


@pytest.mark.parametrize(
    "status",
    [
        "waiting_evidence",
        "awaiting_review",
        "awaiting_pr",
        "awaiting_merge",
        "awaiting_integration",
    ],
)
def test_imported_non_ready_statuses_compute_to_themselves(
    tmp_path: Path,
    status: str,
) -> None:
    """Imported active and awaiting statuses remain explicit queue states."""
    config_path = write_project(tmp_path)
    task_plan = tmp_path / "tasks.json"
    data = json.loads(task_plan.read_text(encoding="utf-8"))
    for raw_task in data["tasks"]:
        if raw_task["id"] == "ready":
            raw_task["status"] = status
    task_plan.write_text(json.dumps(data), encoding="utf-8")
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()

    payload = coord.status_payload()
    ready_state = next(task for task in payload["tasks"] if task["id"] == "ready")

    assert ready_state["state"] == status
    assert ready_state["manual_status"] == status
    assert "ready" not in payload["ready"]
    assert "ready" not in payload["blocked"]
    if status == "waiting_evidence":
        assert "ready" in payload["active"]
    else:
        assert "ready" not in payload["active"]


def test_overview_payload_groups_tasks_with_audit_backed_recent_completion(
    tmp_path: Path,
) -> None:
    """Overview payload groups queue work and orders completion history from audit."""
    _, coord = prepare_overview_state(tmp_path)

    payload = coord.overview_payload(limit=0)

    assert list(payload["groups"]) == [
        "ready",
        "active",
        "review",
        "integration",
        "blocked",
        "recently_completed",
    ]
    assert payload["counts"] == {
        "ready": 1,
        "active": 1,
        "review": 1,
        "integration": 1,
        "blocked": 1,
        "recently_completed": 2,
    }
    assert [task["id"] for task in payload["groups"]["ready"]] == ["other-ready"]
    assert payload["groups"]["ready"][0]["next_action"] == "Pick up the remaining ready task."
    assert [task["id"] for task in payload["groups"]["active"]] == ["ready"]
    assert payload["groups"]["active"][0]["lease_agent_id"] == "agent-1"
    assert payload["groups"]["review"][0]["latest_evidence"] == "pr:review-task"
    assert payload["groups"]["integration"][0]["state"] == "awaiting_merge"
    assert payload["groups"]["blocked"][0]["blockers"] == ["Depends on ready (ready: claimed)"]
    assert [task["id"] for task in payload["groups"]["recently_completed"]] == [
        "done-b",
        "done-a",
    ]
    assert payload["groups"]["recently_completed"][0]["latest_evidence"] == "git:done-b"
    assert payload["groups"]["recently_completed"][0]["completion_action"] == "task.complete"


def test_overview_recent_completion_includes_successful_resolvers_only(
    tmp_path: Path,
) -> None:
    """Recent completion includes done resolvers and excludes failed resolutions."""
    config_path = write_overview_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()

    review_claim = coord.claim(agent_id="agent-1", task_id="review-task")
    coord.submit_review(
        "review-task",
        lease_token=review_claim.lease_token,
        agent_id="agent-1",
        evidence=["git:review"],
    )
    coord.resolve_review(
        "review-task",
        status="done",
        agent_id="reviewer-1",
        evidence=["review:approved"],
    )

    done_claim = coord.claim(agent_id="agent-2", task_id="integration-task")
    coord.await_integration(
        "integration-task",
        lease_token=done_claim.lease_token,
        agent_id="agent-2",
        status="awaiting_merge",
        evidence=["git:integration"],
    )
    coord.resolve_integration(
        "integration-task",
        status="done",
        agent_id="integrator-1",
        evidence=["integration:merged"],
    )

    failed_claim = coord.claim(agent_id="agent-3", task_id="done-a")
    coord.await_integration(
        "done-a",
        lease_token=failed_claim.lease_token,
        agent_id="agent-3",
        status="awaiting_merge",
        evidence=["git:failed"],
    )
    coord.resolve_integration(
        "done-a",
        status="failed",
        reason="merge failed",
        agent_id="integrator-1",
        evidence=["integration:failed"],
    )

    payload = coord.overview_payload(limit=0)

    assert [task["id"] for task in payload["groups"]["recently_completed"]] == [
        "integration-task",
        "review-task",
    ]
    assert payload["groups"]["recently_completed"][0]["completion_action"] == (
        "task.resolve_integration"
    )
    assert payload["groups"]["recently_completed"][1]["completion_action"] == "task.resolve_review"


def test_cli_overview_json_groups_tasks_and_counts_limited_items(tmp_path: Path) -> None:
    """CLI JSON overview exposes grouped task dictionaries with full counts."""
    config_path, _ = prepare_overview_state(tmp_path)
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "overview",
                "--config",
                str(config_path),
                "--json",
                "--limit",
                "1",
            ]
        )
    payload = json.loads(stdout.getvalue())

    assert code == 0
    assert payload["counts"]["recently_completed"] == 2
    assert [task["id"] for task in payload["groups"]["recently_completed"]] == ["done-b"]
    assert payload["groups"]["blocked"][0]["blockers"] == ["Depends on ready (ready: claimed)"]


def test_cli_overview_human_output_includes_blockers_evidence_and_completion(
    tmp_path: Path,
) -> None:
    """Human overview reads like a grouped queue log."""
    config_path, _ = prepare_overview_state(tmp_path)
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["overview", "--config", str(config_path), "--limit", "10"])
    output = stdout.getvalue()

    assert code == 0
    assert "Toy Project (toy)" in output
    assert (
        "Ready (1)\n"
        "  - other-ready: Other Ready\n"
        "    next: Pick up the remaining ready task." in output
    )
    assert "Active (1)\n  - ready: Ready [claimed; agent agent-1]" in output
    assert (
        "Review (1)\n"
        "  - review-task: Review Task [awaiting_review]\n"
        "    evidence: pr:review-task" in output
    )
    assert (
        "Integration (1)\n"
        "  - integration-task: Integration Task [awaiting_merge]\n"
        "    evidence: git:integration-task"
    ) in output
    assert (
        "Blocked (1)\n"
        "  - blocked: Blocked\n"
        "    blocker: Depends on ready (ready: claimed)" in output
    )
    assert "Recently completed (2)\n  - done-b: Done B\n    evidence: git:done-b" in output
    assert "    completed: " in output
    assert "foundation: Foundation" not in output


def test_cli_overview_human_compatibility_helpers_delegate_to_renderer(
    tmp_path: Path,
) -> None:
    """Importable overview helpers remain as renderer-backed compatibility APIs."""
    _, coord = prepare_overview_state(tmp_path)
    payload = coord.overview_payload(limit=1)

    stdout = StringIO()
    with redirect_stdout(stdout):
        cli.print_overview(payload)
    overview_output = stdout.getvalue()

    stdout = StringIO()
    with redirect_stdout(stdout):
        cli.print_overview_item("ready", payload["groups"]["ready"][0])
    item_output = stdout.getvalue()

    assert "Toy Project (toy)" in overview_output
    assert "Ready (1)" in overview_output
    assert "  - other-ready: Other Ready" in item_output
    assert_no_box_drawing(overview_output)
    assert_no_box_drawing(item_output)


def test_cli_overview_human_output_wraps_long_fields(tmp_path: Path) -> None:
    """Human overview wraps long next-action and blocker lines with hanging indents."""
    config_path = write_project(tmp_path)
    task_path = tmp_path / "tasks.json"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    for raw_task in payload["tasks"]:
        if raw_task["id"] == "ready":
            raw_task["next_action"] = (
                "Coordinate the implementation notes, review evidence, and handoff "
                "details before asking another worker to continue the queue."
            )
        if raw_task["id"] == "blocked":
            raw_task["requirements"] = [
                {
                    "kind": "task",
                    "task": "ready",
                    "description": (
                        "Wait until the ready task publishes a detailed operational "
                        "handoff with enough evidence for a reviewer to resume safely."
                    ),
                }
            ]
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    coord.claim(agent_id="agent-1", task_id="ready")
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["overview", "--config", str(config_path), "--limit", "10"])
    lines = stdout.getvalue().splitlines()

    assert code == 0
    assert all(len(line) <= 80 for line in lines)
    assert any(line.startswith("    next: Coordinate the implementation") for line in lines)
    assert any(line.startswith("          details before asking another worker") for line in lines)
    assert any(line.startswith("    blocker: Wait until the ready task") for line in lines)
    assert any(line.startswith("             with enough evidence") for line in lines)


def test_cli_overview_human_output_distinguishes_wrapped_titles(
    tmp_path: Path,
) -> None:
    """Wrapped overview titles use a distinct indent from detail fields."""
    config_path = write_project(tmp_path)
    task_path = tmp_path / "tasks.json"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    for raw_task in payload["tasks"]:
        if raw_task["id"] == "ready":
            raw_task["title"] = (
                "Add repository boundary for in-memory tests with title continuation "
                "that should not look like a field"
            )
            raw_task["next_action"] = (
                "Keep detail fields visually separate from wrapped task titles."
            )
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    Coordinator(load_config(config_path)).import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["overview", "--config", str(config_path), "--limit", "10"])
    lines = stdout.getvalue().splitlines()

    assert code == 0
    assert all(len(line) <= 80 for line in lines)
    assert any(line.startswith("      that should not look like a field") for line in lines)
    assert not any(line.startswith("    that should not look like a field") for line in lines)
    assert any(line.startswith("    next: Keep detail fields") for line in lines)


def test_cli_next_human_output_wraps_long_next_action(tmp_path: Path) -> None:
    """Human next output wraps long next-action lines under the field value."""
    config_path = write_project(tmp_path)
    task_path = tmp_path / "tasks.json"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    for raw_task in payload["tasks"]:
        if raw_task["id"] == "ready":
            raw_task["next_action"] = (
                "Prepare the branch, run the focused checks, capture the evidence, "
                "and leave a short handoff that another maintainer can act on."
            )
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["next", "--config", str(config_path), "--limit", "1"])
    lines = stdout.getvalue().splitlines()

    assert code == 0
    assert all(len(line) <= 80 for line in lines)
    assert any(line.startswith("  next: Prepare the branch") for line in lines)
    assert any(line.startswith("        leave a short handoff") for line in lines)


def test_overview_inspection_does_not_recover_stale_leases_without_flag(
    tmp_path: Path,
) -> None:
    """Overview follows status stale-lease recovery semantics."""
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

    assert cli.main(["overview", "--config", str(config_path)]) == 0
    with coord.store.transaction() as conn:
        inspected = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()
    assert inspected["status"] == "claimed"
    assert inspected["lease_token"] == claim.lease_token

    assert cli.main(["overview", "--config", str(config_path), "--recover-stale-leases"]) == 0
    with coord.store.transaction() as conn:
        recovered = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()
    assert recovered["status"] == "pending"
    assert recovered["lease_token"] == ""


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
        evidence=["file:src/ready.py"],
    )
    states = {state.task.task_id: state for state in coord.task_states()}

    assert claim.task_id == "ready"
    assert states["ready"].state == "done"
    assert states["ready"].evidence == ["file:src/ready.py"]
    assert states["blocked"].state == "ready"


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (["file:src/ready.py", "validation:pytest"], "git: and pr:/review:/integration:"),
        (["git:abc123"], "pr:/review:/integration:"),
        (["pr:https://example.invalid/pr/1"], "git: evidence"),
        (["review:approved"], "git: evidence"),
        (["integration:deployed"], "git: evidence"),
        (["file:git:abc123", "note:pr:1"], "git: and pr:/review:/integration:"),
    ],
)
def test_completion_policy_rejects_incomplete_evidence(
    tmp_path: Path,
    evidence: list[str],
    expected: str,
) -> None:
    """Policy tasks reject file, git-only, integration-only, and false-prefix evidence."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    with pytest.raises(ValueError, match=expected):
        coord.complete(
            "ready",
            lease_token=claim.lease_token,
            agent_id="agent-1",
            evidence=evidence,
        )


@pytest.mark.parametrize(
    "integration_evidence",
    ["pr:https://example.invalid/pr/1", "review:approved", "integration:deployed"],
)
def test_completion_policy_accepts_git_with_integration_evidence(
    tmp_path: Path,
    integration_evidence: str,
) -> None:
    """Policy tasks complete with git evidence plus PR, review, or integration evidence."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    coord.complete(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["git:abc123", integration_evidence],
    )

    assert coord.get_task("ready").state == "done"
    assert coord.completion_integrity_payload() == {
        "project_id": "toy",
        "ok": True,
        "issue_count": 0,
        "issues": [],
    }


def test_completion_policy_complete_uses_recorded_and_new_evidence(
    tmp_path: Path,
) -> None:
    """Direct completion counts evidence recorded before the final command."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.record_evidence("ready", "git:branch-abc123", actor="agent-1")

    coord.complete(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["pr:https://example.invalid/pr/1"],
    )

    state = coord.get_task("ready")
    assert state.state == "done"
    assert state.evidence == ["git:branch-abc123", "pr:https://example.invalid/pr/1"]


def test_completion_policy_direct_merge_requires_permission_and_git_evidence(
    tmp_path: Path,
) -> None:
    """Direct-merge completion is explicit, metadata-gated, and audited."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    coord.complete(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["git:main-abc123"],
        direct_merge=True,
    )
    snapshot = coord.store.snapshot(config.project_id)

    assert coord.get_task("ready").state == "done"
    assert any(
        audit["action"] == "task.complete" and audit["payload"]["direct_merge"] is True
        for audit in snapshot["audit"]
    )

    for dirname, policy in {
        "forbidden-omitted": {"default": "pr_or_review_required"},
        "forbidden-false": {
            "default": "pr_or_review_required",
            "direct_merge_override": False,
        },
    }.items():
        forbidden_config = load_config(
            write_project_with_completion_policy(tmp_path / dirname, policy)
        )
        forbidden_coord = Coordinator(forbidden_config)
        forbidden_coord.import_tasks()
        forbidden_claim = forbidden_coord.claim(agent_id="agent-1", task_id="ready")
        with pytest.raises(ValueError, match="not allowed"):
            forbidden_coord.complete(
                "ready",
                lease_token=forbidden_claim.lease_token,
                agent_id="agent-1",
                evidence=["git:main-abc123"],
                direct_merge=True,
            )

    missing_git_config = load_config(
        write_project_with_completion_policy(tmp_path / "missing-git", PR_OR_REVIEW_POLICY)
    )
    missing_git_coord = Coordinator(missing_git_config)
    missing_git_coord.import_tasks()
    missing_git_claim = missing_git_coord.claim(agent_id="agent-1", task_id="ready")
    with pytest.raises(ValueError, match="requires git: evidence"):
        missing_git_coord.complete(
            "ready",
            lease_token=missing_git_claim.lease_token,
            agent_id="agent-1",
            evidence=["file:src/ready.py"],
            direct_merge=True,
        )


def test_completion_policy_direct_merge_rejects_missing_policy(
    tmp_path: Path,
) -> None:
    """Direct-merge completion requires an explicit completion policy."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    with pytest.raises(ValueError, match="not allowed"):
        coord.complete(
            "ready",
            lease_token=claim.lease_token,
            agent_id="agent-1",
            evidence=["git:main-abc123"],
            direct_merge=True,
        )


@pytest.mark.parametrize("policy", [[], {}, {"default": "unknown"}])
def test_completion_policy_direct_merge_rejects_legacy_metadata(
    tmp_path: Path,
    policy: object,
) -> None:
    """Direct-merge completion rejects malformed or unknown policy metadata."""
    config = load_config(write_project_with_completion_policy(tmp_path, policy))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    with pytest.raises(ValueError, match="not allowed"):
        coord.complete(
            "ready",
            lease_token=claim.lease_token,
            agent_id="agent-1",
            evidence=["git:main-abc123"],
            direct_merge=True,
        )


@pytest.mark.parametrize("policy", [[], {}, {"default": "unknown"}])
def test_completion_policy_malformed_or_unknown_metadata_is_legacy(
    tmp_path: Path,
    policy: object,
) -> None:
    """Malformed or unknown completion policy metadata does not break legacy completion."""
    config = load_config(write_project_with_completion_policy(tmp_path, policy))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    coord.complete(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["file:src/ready.py"],
    )

    assert coord.get_task("ready").state == "done"


def test_completion_integrity_check_flags_corruption_and_ignores_legacy_metadata(
    tmp_path: Path,
) -> None:
    """Integrity check flags corrupted policy tasks but preserves legacy metadata."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    with coord.store.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'done', lease_agent_id = '', lease_token = '', lease_expires_at = ''
            WHERE project_id = ? AND task_id = ?
            """,
            (config.project_id, "ready"),
        )
        conn.execute(
            """
            UPDATE tasks
            SET metadata_json = ?
            WHERE project_id = ? AND task_id = ?
            """,
            (
                json.dumps({"completion_policy": {"default": "unknown"}}),
                config.project_id,
                "foundation",
            ),
        )
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["check-completion-integrity", "--config", str(config_path), "--json"])
    payload = json.loads(stdout.getvalue())

    assert code == 1
    assert payload["ok"] is False
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["task_id"] == "ready"
    assert payload["issues"][0]["reason"] == (
        "completion requires git: and pr:/review:/integration: evidence"
    )
    assert payload["issues"][0]["direct_merge"] is False


def test_completion_integrity_check_reports_direct_merge_git_only_completion(
    tmp_path: Path,
) -> None:
    """Integrity check reports direct-merge completions without integrated evidence."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.complete(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["git:main-abc123"],
        direct_merge=True,
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["check-completion-integrity", "--config", str(config_path), "--json"])
    payload = json.loads(stdout.getvalue())
    tool_payload = AgentTrackerTools(config_path).check_completion_integrity()

    assert code == 1
    assert payload["issues"][0]["task_id"] == "ready"
    assert payload["issues"][0]["direct_merge"] is True
    assert payload["issues"][0]["completion_action"] == "task.complete"
    assert payload["issues"][0]["evidence"] == ["git:main-abc123"]
    assert "pr:/review:/integration:" in payload["issues"][0]["reason"]
    assert tool_payload == payload


def test_submit_review_clears_lease_records_evidence_and_preserves_blocking(
    tmp_path: Path,
) -> None:
    """Review submission is lease-gated, audited, and non-terminal."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    coord.submit_review(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["evidence://review", "evidence://review"],
    )
    states = {state.task.task_id: state for state in coord.task_states()}
    payload = coord.status_payload()
    snapshot = coord.store.snapshot(config.project_id)

    assert states["ready"].state == "awaiting_review"
    assert states["ready"].lease_token == ""
    assert states["ready"].lease_agent_id == ""
    assert states["ready"].lease_expires_at == ""
    assert states["ready"].evidence == ["evidence://review"]
    assert states["blocked"].state == "blocked"
    assert payload["review"] == ["ready"]
    assert payload["integration"] == []
    assert "ready" not in payload["active"]
    assert any(audit["action"] == "task.submit_review" for audit in snapshot["audit"])
    assert "evidence://review" in {
        evidence for task in snapshot["tasks"] for evidence in task["evidence"]
    }
    with pytest.raises(ValueError, match="not active"):
        coord.heartbeat("ready", lease_token=claim.lease_token, agent_id="agent-1")
    with pytest.raises(ValueError, match="not active"):
        coord.complete("ready", lease_token=claim.lease_token, agent_id="agent-1")
    with pytest.raises(ValueError, match="not active"):
        coord.fail(
            "ready",
            lease_token=claim.lease_token,
            agent_id="agent-1",
            reason="old token",
        )


@pytest.mark.parametrize("status", sorted(INTEGRATION_STATES))
def test_await_integration_statuses_clear_lease_and_do_not_unblock(
    tmp_path: Path,
    status: str,
) -> None:
    """Integration wait states are non-terminal and keep dependents blocked."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    coord.await_integration(
        "ready",
        lease_token=claim.lease_token,
        status=status,
        agent_id="agent-1",
        evidence=[f"evidence://{status}"],
    )
    states = {state.task.task_id: state for state in coord.task_states()}
    payload = coord.status_payload()
    snapshot = coord.store.snapshot(config.project_id)

    assert states["ready"].state == status
    assert states["ready"].lease_token == ""
    assert states["ready"].evidence == [f"evidence://{status}"]
    assert states["blocked"].state == "blocked"
    assert payload["integration"] == ["ready"]
    assert payload["review"] == []
    assert "ready" not in payload["active"]
    assert any(
        audit["action"] == "task.await_integration" and audit["payload"]["status"] == status
        for audit in snapshot["audit"]
    )


def test_await_integration_defaults_to_generic_status(tmp_path: Path) -> None:
    """The integration handoff defaults to awaiting_integration."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    coord.await_integration("ready", lease_token=claim.lease_token, agent_id="agent-1")

    assert coord.get_task("ready").state == "awaiting_integration"


def test_resolve_review_completes_waiting_task_and_unblocks_dependents(
    tmp_path: Path,
) -> None:
    """Review resolution finalizes an awaiting task without a lease token."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["evidence://review"],
    )

    coord.resolve_review(
        "ready",
        status="done",
        agent_id="reviewer-1",
        evidence=["evidence://approval"],
    )
    states = {state.task.task_id: state for state in coord.task_states()}
    snapshot = coord.store.snapshot(config.project_id)

    assert states["ready"].state == "done"
    assert set(states["ready"].evidence) == {"evidence://review", "evidence://approval"}
    assert states["blocked"].state == "ready"
    assert any(
        audit["action"] == "task.resolve_review"
        and audit["actor"] == "reviewer-1"
        and audit["payload"]["from_status"] == "awaiting_review"
        and audit["payload"]["status"] == "done"
        for audit in snapshot["audit"]
    )


def test_resolve_review_completion_policy_uses_cumulative_evidence(
    tmp_path: Path,
) -> None:
    """Resolver completion counts evidence recorded before and during resolution."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["git:branch-abc123", "git:branch-abc123"],
    )

    with pytest.raises(ValueError, match="pr:/review:/integration:"):
        coord.resolve_review("ready", status="done", agent_id="reviewer-1")

    coord.resolve_review(
        "ready",
        status="done",
        agent_id="reviewer-1",
        evidence=["review:approved", "review:approved"],
    )

    state = coord.get_task("ready")
    assert state.state == "done"
    assert state.evidence == ["git:branch-abc123", "review:approved"]


def test_resolve_integration_accepts_direct_merge_override(
    tmp_path: Path,
) -> None:
    """Integration resolution can finalize direct-merge policy tasks."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.await_integration(
        "ready",
        lease_token=claim.lease_token,
        status="awaiting_merge",
        agent_id="agent-1",
        evidence=["git:main-abc123"],
    )

    coord.resolve_integration(
        "ready",
        status="done",
        agent_id="integrator-1",
        direct_merge=True,
    )
    snapshot = coord.store.snapshot(config.project_id)

    assert coord.get_task("ready").state == "done"
    assert any(
        audit["action"] == "task.resolve_integration"
        and audit["actor"] == "integrator-1"
        and audit["payload"]["direct_merge"] is True
        for audit in snapshot["audit"]
    )


def test_resolve_integration_failed_ignores_completion_policy(
    tmp_path: Path,
) -> None:
    """Failed resolver paths require a reason but not completion evidence."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.await_integration(
        "ready",
        lease_token=claim.lease_token,
        status="awaiting_merge",
        agent_id="agent-1",
        evidence=["file:src/ready.py"],
    )

    coord.resolve_integration(
        "ready",
        status="failed",
        reason="merge failed",
        agent_id="reviewer-1",
        evidence=["file:report.txt"],
    )

    assert coord.get_task("ready").state == "failed"


def test_resolve_integration_failed_rejects_direct_merge_override(
    tmp_path: Path,
) -> None:
    """Direct-merge resolution is only valid when a resolver marks a task done."""
    config = load_config(write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.await_integration(
        "ready",
        lease_token=claim.lease_token,
        status="awaiting_merge",
        agent_id="agent-1",
        evidence=["file:src/ready.py"],
    )

    with pytest.raises(ValueError, match="only applies to done"):
        coord.resolve_integration(
            "ready",
            status="failed",
            reason="merge failed",
            agent_id="reviewer-1",
            evidence=["file:report.txt"],
            direct_merge=True,
        )

    assert coord.get_task("ready").state == "awaiting_merge"


def test_resolve_integration_can_fail_waiting_task_with_reason(tmp_path: Path) -> None:
    """Integration resolution can fail an awaiting task with a durable reason."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.await_integration(
        "ready",
        lease_token=claim.lease_token,
        status="awaiting_merge",
        agent_id="agent-1",
        evidence=["evidence://pr"],
    )

    coord.resolve_integration(
        "ready",
        status="failed",
        reason="merge conflict",
        agent_id="reviewer-1",
        evidence=["evidence://conflict"],
    )
    states = {state.task.task_id: state for state in coord.task_states()}
    snapshot = coord.store.snapshot(config.project_id)

    assert states["ready"].state == "failed"
    assert states["blocked"].state == "blocked"
    assert any(
        audit["action"] == "task.resolve_integration"
        and audit["payload"]["reason"] == "merge conflict"
        and audit["payload"]["status"] == "failed"
        for audit in snapshot["audit"]
    )


def test_resolve_awaiting_requires_matching_state_and_failure_reason(
    tmp_path: Path,
) -> None:
    """Awaiting resolvers reject wrong source states and incomplete failures."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with pytest.raises(ValueError, match="awaiting_review"):
        coord.resolve_review("ready", agent_id="reviewer-1")

    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review("ready", lease_token=claim.lease_token, agent_id="agent-1")

    with pytest.raises(ValueError, match="agent is required"):
        coord.resolve_review("ready")
    with pytest.raises(ValueError, match="reason is required"):
        coord.resolve_review("ready", status="failed", agent_id="reviewer-1")


def test_database_schema_metadata_is_created(tmp_path: Path) -> None:
    """Schema initialization records the current database schema version."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with coord.store.transaction(read_only=True) as conn:
        row = conn.execute(
            "SELECT value, created_at, updated_at FROM schema_metadata WHERE key = ?",
            (DB_SCHEMA_VERSION_KEY,),
        ).fetchone()

    assert row is not None
    assert row["value"] == str(DB_SCHEMA_VERSION)
    assert row["created_at"]
    assert row["updated_at"]


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
    inspected = coord.get_task("ready")
    recovered = coord.get_task("ready", recover_stale_leases=True)

    assert inspected.state == "ready"
    assert inspected.lease_token == ""
    assert recovered.state == "ready"
    assert recovered.lease_token == ""


def test_stale_waiting_evidence_lease_is_recovered(tmp_path: Path) -> None:
    """Waiting-evidence remains an active lease state for stale recovery."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    with coord.store.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, lease_expires_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            ("waiting_evidence", "2000-01-01T00:00:00+00:00", config.project_id, "ready"),
        )

    inspected = coord.get_task("ready")
    recovered = coord.get_task("ready", recover_stale_leases=True)
    with coord.store.transaction(read_only=True) as conn:
        row = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()

    assert claim.lease_token
    assert inspected.state == "ready"
    assert inspected.lease_token == ""
    assert recovered.state == "ready"
    assert recovered.lease_token == ""
    assert row["status"] == "pending"
    assert row["lease_token"] == ""


def test_awaiting_states_are_not_recovered_as_stale_leases(tmp_path: Path) -> None:
    """Lease-free awaiting states are unaffected by stale lease recovery."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review("ready", lease_token=claim.lease_token, agent_id="agent-1")

    state = coord.get_task("ready", recover_stale_leases=True)

    assert state.state == "awaiting_review"
    assert state.lease_token == ""


def test_awaiting_and_waiting_evidence_states_are_not_claimable(tmp_path: Path) -> None:
    """Explicit active and awaiting statuses are excluded from claim selection."""
    for status in sorted(REVIEW_STATES | INTEGRATION_STATES | {"waiting_evidence"}):
        project_root = tmp_path / status
        project_root.mkdir()
        config_path = write_project(project_root)
        task_plan = project_root / "tasks.json"
        data = json.loads(task_plan.read_text(encoding="utf-8"))
        data["tasks"] = [
            {**raw_task, "status": status} if raw_task["id"] == "ready" else raw_task
            for raw_task in data["tasks"]
            if raw_task["id"] in {"foundation", "ready"}
        ]
        task_plan.write_text(json.dumps(data), encoding="utf-8")
        coord = Coordinator(load_config(config_path))
        coord.import_tasks()

        with pytest.raises(ValueError, match="no matching ready task"):
            coord.claim(agent_id="agent-1", task_id="ready")


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


def test_reimport_reconciles_new_nonterminal_statuses_explicitly(tmp_path: Path) -> None:
    """Definition imports preserve live awaiting states unless reconciliation is explicit."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review("ready", lease_token=claim.lease_token, agent_id="agent-1")

    task_plan = tmp_path / "tasks.json"
    data = json.loads(task_plan.read_text(encoding="utf-8"))
    for raw_task in data["tasks"]:
        if raw_task["id"] == "ready":
            raw_task["status"] = "awaiting_pr"
    task_plan.write_text(json.dumps(data), encoding="utf-8")
    coord.import_tasks()
    preserved = coord.get_task("ready")
    coord.import_tasks(reconcile_runtime_state=True)
    reconciled = coord.get_task("ready")

    assert preserved.state == "awaiting_review"
    assert reconciled.state == "awaiting_pr"
    assert reconciled.lease_token == ""


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


def test_workspace_payload_resolves_local_registry_paths(tmp_path: Path) -> None:
    """Workspace registry entries expose resolved local paths and capabilities."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    payload = Coordinator(load_config(config_path)).workspace_payload()

    assert payload["workspaces"] == [
        {
            "name": "hpc",
            "kind": "local",
            "path": str(workspace),
            "config_path": str(workspace / "agent-tracker.config.json"),
            "spool_outbox": str(workspace / ".agent-tracker" / "spool" / "outbox"),
            "artifacts_path": str(workspace / "results" / "worker-launches"),
            "roles": ["agent-coordinator"],
            "capabilities": ["local-worker", "summary-test"],
        }
    ]


@pytest.mark.parametrize("field", ["config_path", "spool_outbox", "artifacts_path"])
@pytest.mark.parametrize("escape_kind", ["absolute", "parent"])
def test_workspace_payload_rejects_non_relative_local_paths(
    tmp_path: Path,
    field: str,
    escape_kind: str,
) -> None:
    """Local workspace child path settings cannot be absolute or escape upward."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["workspaces"]["hpc"][field] = (
        str(workspace / "absolute-child") if escape_kind == "absolute" else "../outside"
    )
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    with pytest.raises(ValueError, match=f"workspaces.hpc.{field}"):
        Coordinator(load_config(config_path)).workspace_payload()


def test_launch_worker_dry_run_does_not_create_artifacts(tmp_path: Path) -> None:
    """Dry-run worker launches report intended paths without writing files."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    coord = Coordinator(load_config(config_path))

    result = coord.launch_worker("hpc", prompt_text="Report capabilities.", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["workspace"]["name"] == "hpc"
    assert result["command"] == []
    assert not Path(result["artifacts"]["directory"]).exists()


def test_launch_worker_dry_run_rejects_claim_without_mutation(tmp_path: Path) -> None:
    """Dry-run worker launches cannot claim tasks as a side effect."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()

    with pytest.raises(ValueError, match="claim_task cannot be used with dry_run"):
        coord.launch_worker(
            "hpc",
            task_id="ready",
            agent_id="agent-1",
            claim_task=True,
            dry_run=True,
        )

    state = coord.get_task("ready")
    assert state.task.status == "pending"
    assert state.lease_token == ""
    assert state.lease_agent_id == ""
    assert not (workspace / "results" / "worker-launches").exists()


def test_launch_worker_prepares_prompt_report_spool_and_task_evidence(
    tmp_path: Path,
) -> None:
    """Prepared local launches write durable artifacts and task evidence."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()

    result = coord.launch_worker("hpc", task_id="ready", agent_id="agent-1")
    artifacts = result["artifacts"]
    state = coord.get_task("ready")
    outbox = workspace / ".agent-tracker" / "spool" / "outbox"

    assert result["status"] == "prepared"
    assert Path(artifacts["prompt"]).read_text(encoding="utf-8").startswith("# Ready")
    assert Path(artifacts["report"]).read_text(encoding="utf-8") == (
        "Worker launch prepared; no command was executed.\n"
    )
    assert Path(artifacts["launch"]).exists()
    assert f"worker-launch:{result['launch_id']}" in state.evidence
    assert f"file:{artifacts['launch']}" in state.evidence
    event_files = sorted(outbox.glob("*.json"))
    assert len(event_files) == 1
    event = json.loads(event_files[0].read_text(encoding="utf-8"))
    assert event["kind"] == "agent_tracker.worker_launch"
    assert event["task_id"] == "ready"
    assert event["status"] == "prepared"


def test_cli_launch_worker_executes_local_command_and_writes_report(
    tmp_path: Path,
) -> None:
    """The launch-worker CLI can execute a harmless local worker command."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    script = (
        "from pathlib import Path; "
        "import os; "
        "Path(os.environ['AGENT_TRACKER_WORKER_REPORT']).write_text("
        "'capabilities available\\n', encoding='utf-8')"
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "launch-worker",
                "--config",
                str(config_path),
                "--workspace",
                "hpc",
                "--prompt",
                "Report capabilities.",
                "--agent",
                "agent-1",
                "--execute",
                "--json",
                "--command",
                sys.executable,
                "-c",
                script,
            ]
        )

    assert code == 0
    result = json.loads(stdout.getvalue())
    assert result["status"] == "succeeded"
    assert result["returncode"] == 0
    assert Path(result["artifacts"]["report"]).read_text(encoding="utf-8") == (
        "capabilities available\n"
    )
    assert Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8") == ""
    assert Path(result["artifacts"]["stderr"]).read_text(encoding="utf-8") == ""


def test_cli_launch_worker_ssh_workspace_reports_error_without_traceback(
    tmp_path: Path,
) -> None:
    """The CLI reports unsupported SSH worker launches without a traceback."""
    root = tmp_path / "tracker"
    root.mkdir()
    config_path = write_project(root)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["workspaces"] = {
        "remote": {
            "kind": "ssh",
            "host": "example.internal",
            "remote_path": "/srv/project",
        }
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "launch-worker",
                "--config",
                str(config_path),
                "--workspace",
                "remote",
                "--prompt",
                "Report capabilities.",
            ]
        )

    assert code == 1
    assert "error: launch-worker currently supports only local workspaces" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_cli_launch_worker_command_remainder_keeps_later_options_in_command() -> None:
    """Tokens after --command belong to the worker command argv."""
    args = cli.build_parser().parse_args(
        [
            "launch-worker",
            "--workspace",
            "hpc",
            "--execute",
            "--json",
            "--command",
            "python",
            "-c",
            "print(1)",
        ]
    )
    trailing_args = cli.build_parser().parse_args(
        [
            "launch-worker",
            "--workspace",
            "hpc",
            "--execute",
            "--command",
            "python",
            "-c",
            "print(1)",
            "--json",
        ]
    )

    assert args.json is True
    assert args.command == ["python", "-c", "print(1)"]
    assert trailing_args.json is False
    assert trailing_args.command == ["python", "-c", "print(1)", "--json"]


@pytest.mark.parametrize(
    ("exc", "returncode"),
    [
        (FileNotFoundError(2, "No such file or directory", "missing-worker"), 127),
        (PermissionError(13, "Permission denied", "denied-worker"), 126),
    ],
)
def test_launch_worker_start_errors_write_failed_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: OSError,
    returncode: int,
) -> None:
    """Worker process startup failures produce durable failed launch artifacts."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    coord = Coordinator(load_config(config_path))

    def fail_run(*args: object, **kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(service_module.subprocess, "run", fail_run)

    result = coord.launch_worker(
        "hpc",
        prompt_text="Report capabilities.",
        execute=True,
        command=["worker-command"],
    )
    artifacts = result["artifacts"]
    launch = json.loads(Path(artifacts["launch"]).read_text(encoding="utf-8"))

    assert result["status"] == "failed"
    assert result["returncode"] == returncode
    assert launch["status"] == "failed"
    assert launch["returncode"] == returncode
    assert Path(artifacts["stdout"]).read_text(encoding="utf-8") == ""
    assert "worker command failed to start" in Path(artifacts["stderr"]).read_text(encoding="utf-8")
    assert Path(artifacts["report"]).read_text(encoding="utf-8") == (
        "Worker command produced no report.\n"
    )


def test_launch_worker_timeout_writes_failed_artifacts(tmp_path: Path) -> None:
    """Timed-out worker commands produce a failed launch result and logs."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    coord = Coordinator(load_config(config_path))

    result = coord.launch_worker(
        "hpc",
        prompt_text="Report capabilities.",
        execute=True,
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=1,
    )

    assert result["status"] == "failed"
    assert result["returncode"] == 124
    assert "timed out" in Path(result["artifacts"]["stderr"]).read_text(encoding="utf-8")
    launch = json.loads(Path(result["artifacts"]["launch"]).read_text(encoding="utf-8"))
    assert launch["status"] == "failed"
    assert launch["returncode"] == 124


def test_launch_worker_sanitizes_launch_id_path_components(tmp_path: Path) -> None:
    """Launch artifact and event filenames do not use raw path-like IDs."""
    workspace = tmp_path / "hpc-ci-project-tracker"
    config_path = write_project_with_workspace(tmp_path / "tracker", workspace)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["project_id"] = "../toy.project"
    hpc_workspace = config_payload["workspaces"].pop("hpc")
    config_payload["workspaces"] = {"../hpc.workspace": hpc_workspace}
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    coord = Coordinator(load_config(config_path))

    result = coord.launch_worker("../hpc.workspace", prompt_text="Report capabilities.")
    launch_id = result["launch_id"]
    launch_path = Path(result["artifacts"]["launch"])
    outbox = workspace / ".agent-tracker" / "spool" / "outbox"
    event_files = sorted(outbox.glob("*.json"))

    assert "/" not in launch_id
    assert "\\" not in launch_id
    assert ".." not in launch_id
    assert launch_path.parent == workspace / "results" / "worker-launches" / launch_id
    assert launch_path.exists()
    assert len(event_files) == 1
    assert event_files[0].parent == outbox
    assert event_files[0].name == f"{launch_id}.json"


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


def test_pull_spool_dry_run_lists_complete_files_without_copying(
    tmp_path: Path,
) -> None:
    """Dry-run reports complete remote events without mutating the local inbox."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "remote-spool"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    local = tmp_path / "spool" / "inbox"
    remote.mkdir()
    (remote / "event.json").write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    (remote / "event.json.partial").write_text("{}", encoding="utf-8")
    (remote / "other.tmp").write_text("{}", encoding="utf-8")
    (remote / "notes.txt").write_text("not json", encoding="utf-8")

    result = Coordinator(load_config(config_path)).pull_spool(dry_run=True)

    assert result["dry_run"] is True
    assert result["processed"] == 1
    assert result["copied"] == 0
    assert result["skipped"] == 3
    assert result["conflicts"] == 0
    assert result["files"] == [
        {
            "source": str(remote / "event.json"),
            "target": str(local / "event.json"),
            "action": "copy",
        }
    ]
    assert not (local / "event.json").exists()


def test_pull_spool_copies_complete_files_idempotently_and_ingests(
    tmp_path: Path,
) -> None:
    """Pulled remote events can be processed by the existing spool ingest flow."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "remote-spool"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    local = tmp_path / "spool" / "inbox"
    remote.mkdir()
    remote_event = remote / "event.json"
    remote_event.write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()

    first_pull = coord.pull_spool()
    second_pull = coord.pull_spool()
    ingest = coord.ingest_spool(actor="spool")

    assert first_pull["processed"] == 1
    assert first_pull["copied"] == 1
    assert first_pull["conflicts"] == 0
    assert second_pull["processed"] == 0
    assert second_pull["copied"] == 0
    assert second_pull["skipped"] == 1
    assert second_pull["files"][0]["action"] == "skip_existing"
    assert second_pull["files"][0]["existing"] == str(local / "event.json")
    assert remote_event.exists()
    assert not (local / "event.json").exists()
    assert (tmp_path / "spool" / "done" / "event.json").exists()
    assert ingest == {"processed": 1, "inserted": 1, "errors": 0}
    third_pull = coord.pull_spool()
    assert third_pull["processed"] == 0
    assert third_pull["copied"] == 0
    assert third_pull["skipped"] == 1
    assert third_pull["files"][0]["action"] == "skip_done"
    assert third_pull["files"][0]["existing"] == str(tmp_path / "spool" / "done" / "event.json")
    assert not (local / "event.json").exists()


def test_remote_spooling_harness_models_ssh_codex_project_outbox(
    tmp_path: Path,
) -> None:
    """A separate SSH-style project outbox can feed the canonical event spool."""
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config_path = write_project(canonical)
    remote_project = tmp_path / "ssh-codex-project"
    remote_outbox = remote_project / ".agent-tracker" / "spool" / "outbox"
    remote_outbox.mkdir(parents=True)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = str(remote_outbox)
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    partial = remote_outbox / "remote-event.json.partial"
    partial.write_text(
        json.dumps({"event_id": "ssh-partial", "kind": "codex.remote_spool"}),
        encoding="utf-8",
    )
    remote_event = remote_outbox / "remote-event.json"
    remote_artifact = "file:ssh-codex-project/.agent-tracker/spool/outbox/remote-event.json"
    remote_event.write_text(
        json.dumps(
            {
                "event_id": "ssh-codex-evt-1",
                "kind": "codex.remote_spool",
                "task_id": "ready",
                "artifact": remote_artifact,
            }
        ),
        encoding="utf-8",
    )
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()

    dry_run = coord.pull_spool(dry_run=True)
    first_pull = coord.pull_spool()
    ingest = coord.ingest_spool(actor="ssh-codex-spool")
    repeat_pull = coord.pull_spool()
    snapshot = coord.store.snapshot(coord.config.project_id)

    local_event = canonical / "spool" / "inbox" / "remote-event.json"
    done_event = canonical / "spool" / "done" / "remote-event.json"
    assert dry_run["dry_run"] is True
    assert dry_run["processed"] == 1
    assert dry_run["skipped"] == 1
    assert not local_event.exists()
    assert first_pull["processed"] == 1
    assert first_pull["copied"] == 1
    assert remote_event.exists()
    assert partial.exists()
    assert ingest == {"processed": 1, "inserted": 1, "errors": 0}
    assert done_event.exists()
    assert repeat_pull["processed"] == 0
    assert repeat_pull["copied"] == 0
    assert repeat_pull["skipped"] == 2
    assert repeat_pull["files"] == [
        {
            "source": str(remote_event),
            "target": str(local_event),
            "existing": str(done_event),
            "action": "skip_done",
        }
    ]
    assert snapshot["events"][0]["event_id"] == "ssh-codex-evt-1"
    assert snapshot["events"][0]["payload"]["artifact"] == remote_artifact


def test_pull_spool_over_loopback_sftp_server(tmp_path: Path) -> None:
    """The optional SSH transport pulls and ingests remote SFTP event files."""
    asyncssh = pytest.importorskip("asyncssh")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config_path = write_project(canonical)
    remote_root = tmp_path / "remote-root"
    remote_outbox = remote_root / "outbox"
    remote_outbox.mkdir(parents=True)
    partial = remote_outbox / "remote-event.json.partial"
    partial.write_text(
        json.dumps({"event_id": "ssh-partial", "kind": "codex.remote_spool"}),
        encoding="utf-8",
    )
    remote_event = remote_outbox / "remote-event.json"
    remote_event.write_text(
        json.dumps(
            {
                "event_id": "ssh-codex-evt-1",
                "kind": "codex.remote_spool",
                "task_id": "ready",
            }
        ),
        encoding="utf-8",
    )

    with LoopbackSFTPServer(remote_root, asyncssh) as server:
        configure_sftp_spool(config_path, server.port)
        coord = Coordinator(load_config(config_path))
        coord.import_tasks()

        dry_run = coord.pull_spool(dry_run=True)
        first_pull = coord.pull_spool()
        ingest = coord.ingest_spool(actor="ssh-codex-spool")
        repeat_pull = coord.pull_spool()

    local_event = canonical / "spool" / "inbox" / "remote-event.json"
    done_event = canonical / "spool" / "done" / "remote-event.json"
    remote_source = f"sftp://127.0.0.1:{server.port}/outbox/remote-event.json"
    assert dry_run["dry_run"] is True
    assert dry_run["processed"] == 1
    assert dry_run["copied"] == 0
    assert dry_run["skipped"] == 1
    assert dry_run["files"] == [
        {
            "source": remote_source,
            "target": str(local_event),
            "action": "copy",
        }
    ]
    assert not local_event.exists()
    assert first_pull["processed"] == 1
    assert first_pull["copied"] == 1
    assert remote_event.exists()
    assert partial.exists()
    assert ingest == {"processed": 1, "inserted": 1, "errors": 0}
    assert done_event.exists()
    assert repeat_pull["processed"] == 0
    assert repeat_pull["copied"] == 0
    assert repeat_pull["skipped"] == 2
    assert repeat_pull["files"] == [
        {
            "source": remote_source,
            "target": str(local_event),
            "existing": str(done_event),
            "action": "skip_done",
        }
    ]


def test_pull_spool_over_sftp_repeats_malformed_event_as_skip_error(tmp_path: Path) -> None:
    """SFTP pulls preserve malformed events in error and skip them on repeat."""
    asyncssh = pytest.importorskip("asyncssh")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config_path = write_project(canonical)
    remote_root = tmp_path / "remote-root"
    remote_outbox = remote_root / "outbox"
    remote_outbox.mkdir(parents=True)
    remote_event = remote_outbox / "bad-event.json"
    remote_event.write_text("not json", encoding="utf-8")

    with LoopbackSFTPServer(remote_root, asyncssh) as server:
        configure_sftp_spool(config_path, server.port)
        coord = Coordinator(load_config(config_path))
        coord.import_tasks()

        first_pull = coord.pull_spool()
        ingest = coord.ingest_spool(actor="ssh-codex-spool")
        repeat_pull = coord.pull_spool()

    local_event = canonical / "spool" / "inbox" / "bad-event.json"
    error_event = canonical / "spool" / "error" / "bad-event.json"
    remote_source = f"sftp://127.0.0.1:{server.port}/outbox/bad-event.json"
    assert first_pull["processed"] == 1
    assert first_pull["copied"] == 1
    assert ingest == {"processed": 1, "inserted": 0, "errors": 1}
    assert error_event.exists()
    assert not local_event.exists()
    assert repeat_pull["processed"] == 0
    assert repeat_pull["copied"] == 0
    assert repeat_pull["skipped"] == 1
    assert repeat_pull["files"] == [
        {
            "source": remote_source,
            "target": str(local_event),
            "existing": str(error_event),
            "action": "skip_error",
        }
    ]


def test_pull_spool_over_sftp_reports_done_conflict(tmp_path: Path) -> None:
    """SFTP pulls report different done files as conflicts without overwrite."""
    asyncssh = pytest.importorskip("asyncssh")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config_path = write_project(canonical)
    remote_root = tmp_path / "remote-root"
    remote_outbox = remote_root / "outbox"
    remote_outbox.mkdir(parents=True)
    remote_event = remote_outbox / "event.json"
    remote_event.write_text(
        json.dumps({"event_id": "remote", "kind": "sample"}),
        encoding="utf-8",
    )
    done = canonical / "spool" / "done"
    done.mkdir(parents=True)
    done_event = done / "event.json"
    done_event.write_text(
        json.dumps({"event_id": "local", "kind": "sample"}),
        encoding="utf-8",
    )

    with LoopbackSFTPServer(remote_root, asyncssh) as server:
        configure_sftp_spool(config_path, server.port)
        result = Coordinator(load_config(config_path)).pull_spool()

    local_event = canonical / "spool" / "inbox" / "event.json"
    assert result["processed"] == 0
    assert result["copied"] == 0
    assert result["conflicts"] == 1
    assert result["files"] == [
        {
            "source": f"sftp://127.0.0.1:{server.port}/outbox/event.json",
            "target": str(local_event),
            "existing": str(done_event),
            "action": "conflict_done",
        }
    ]
    assert json.loads(done_event.read_text(encoding="utf-8"))["event_id"] == "local"


def test_pull_spool_over_sftp_uses_atomic_publish_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SFTP pulls publish local files through the atomic write helper."""
    asyncssh = pytest.importorskip("asyncssh")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config_path = write_project(canonical)
    remote_root = tmp_path / "remote-root"
    remote_outbox = remote_root / "outbox"
    remote_outbox.mkdir(parents=True)
    remote_payload = json.dumps({"event_id": "remote", "kind": "sample"}).encode()
    (remote_outbox / "event.json").write_bytes(remote_payload)
    writes: list[tuple[bytes, Path]] = []

    def fake_write_spool_file_atomic(data: bytes, target: Path) -> None:
        writes.append((data, target))
        target.write_bytes(data)

    monkeypatch.setattr(service_module, "_write_spool_file_atomic", fake_write_spool_file_atomic)

    with LoopbackSFTPServer(remote_root, asyncssh) as server:
        configure_sftp_spool(config_path, server.port)
        result = Coordinator(load_config(config_path)).pull_spool()

    target = canonical / "spool" / "inbox" / "event.json"
    assert result["copied"] == 1
    assert writes == [(remote_payload, target)]
    assert target.read_bytes() == remote_payload


def test_pull_spool_over_sftp_accepts_known_hosts_file(tmp_path: Path) -> None:
    """SFTP pulls can verify the loopback host key through known_hosts."""
    asyncssh = pytest.importorskip("asyncssh")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config_path = write_project(canonical)
    remote_root = tmp_path / "remote-root"
    remote_outbox = remote_root / "outbox"
    remote_outbox.mkdir(parents=True)
    (remote_outbox / "event.json").write_text(
        json.dumps({"event_id": "remote", "kind": "sample"}),
        encoding="utf-8",
    )

    with LoopbackSFTPServer(remote_root, asyncssh) as server:
        known_hosts = canonical / "known_hosts"
        known_hosts.write_bytes(
            f"[127.0.0.1]:{server.port} ".encode() + server.host_key.export_public_key() + b"\n"
        )
        configure_sftp_spool(config_path, server.port)
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
        config_payload["spool"]["ssh"]["known_hosts"] = str(known_hosts)
        config_path.write_text(json.dumps(config_payload), encoding="utf-8")

        result = Coordinator(load_config(config_path)).pull_spool(dry_run=True)

    assert result["processed"] == 1
    assert result["files"][0]["action"] == "copy"


@pytest.mark.parametrize(
    ("remote_inbox", "expected"),
    [
        ("sftp:///outbox", "must include a host"),
        ("sftp://example.internal:not-a-port/outbox", "invalid port"),
        ("sftp://example.internal", "must include an absolute path"),
    ],
)
def test_pull_spool_ssh_uri_validation_errors_are_actionable(
    tmp_path: Path,
    remote_inbox: str,
    expected: str,
) -> None:
    """Malformed SSH remote inbox URIs fail before connecting."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = remote_inbox
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
        Coordinator(load_config(config_path)).pull_spool()


@pytest.mark.parametrize(
    "name",
    ["../event.json", "/tmp/event.json", "nested/event.json", r"nested\\event.json"],
)
def test_sftp_spool_entry_names_must_be_safe_basenames(name: str) -> None:
    """Remote SFTP names are rejected before becoming local paths."""
    assert service_module._is_safe_spool_entry_name(name) is False


def test_pull_spool_ssh_transport_reports_missing_optional_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH transport errors explain how to install the optional dependency."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "sftp://example.internal/outbox"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    def missing_asyncssh() -> object:
        raise ImportError(
            "SSH spool transport requires the optional 'ssh' extra; "
            "install it with `agent-tracker[ssh]` or run `uv run --extra ssh ...`"
        )

    monkeypatch.setattr(service_module, "_load_asyncssh", missing_asyncssh)

    with pytest.raises(ImportError, match="optional 'ssh' extra"):
        Coordinator(load_config(config_path)).pull_spool()


def test_cli_pull_spool_ssh_transport_reports_missing_optional_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI missing-extra errors are concise and do not traceback."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "sftp://example.internal/outbox"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    def missing_asyncssh() -> object:
        raise ImportError(
            "SSH spool transport requires the optional 'ssh' extra; "
            "install it with `agent-tracker[ssh]` or run `uv run --extra ssh ...`"
        )

    monkeypatch.setattr(service_module, "_load_asyncssh", missing_asyncssh)
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(["pull-spool", "--config", str(config_path)])

    assert code == 1
    assert "error: SSH spool transport requires the optional 'ssh' extra" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_pull_spool_uses_temporary_path_before_publishing_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pulled files are copied to a non-JSON temp name before final publish."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "remote-spool"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    remote.mkdir()
    source = remote / "event.json"
    source.write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    copied_to: list[Path] = []

    def fake_copy2(source_path: Path, target_path: Path) -> None:
        copied_to.append(target_path)
        target_path.write_bytes(source_path.read_bytes())

    monkeypatch.setattr(service_module.shutil, "copy2", fake_copy2)

    result = Coordinator(load_config(config_path)).pull_spool()

    target = tmp_path / "spool" / "inbox" / "event.json"
    assert result["copied"] == 1
    assert target.exists()
    assert len(copied_to) == 1
    assert copied_to[0].parent == target.parent
    assert copied_to[0].name != target.name
    assert copied_to[0].name.endswith(".tmp")
    assert not copied_to[0].exists()


def test_pull_spool_and_ingest_share_legacy_local_spool_paths(
    tmp_path: Path,
) -> None:
    """A legacy local spool plus nested remote inbox uses one local path set."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"] = {"remote_inbox": "remote-spool"}
    config_payload["spool_inbox"] = "legacy/inbox"
    config_payload["spool_done"] = "legacy/done"
    config_payload["spool_error"] = "legacy/error"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    remote.mkdir()
    (remote / "event.json").write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()

    pull = coord.pull_spool()
    ingest = coord.ingest_spool(actor="spool")

    assert pull["processed"] == 1
    assert pull["copied"] == 1
    assert not (tmp_path / "legacy" / "inbox" / "event.json").exists()
    assert (tmp_path / "legacy" / "done" / "event.json").exists()
    assert ingest == {"processed": 1, "inserted": 1, "errors": 0}


def test_pull_spool_reports_conflicting_existing_file(tmp_path: Path) -> None:
    """Different local files are reported as conflicts instead of overwritten."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "remote-spool"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    local = tmp_path / "spool" / "inbox"
    remote.mkdir()
    local.mkdir(parents=True)
    (remote / "event.json").write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    local_file = local / "event.json"
    local_file.write_text(
        json.dumps({"event_id": "evt-local", "kind": "sample"}),
        encoding="utf-8",
    )

    result = Coordinator(load_config(config_path)).pull_spool()

    assert result["processed"] == 0
    assert result["copied"] == 0
    assert result["conflicts"] == 1
    assert result["files"] == [
        {
            "source": str(remote / "event.json"),
            "target": str(local_file),
            "existing": str(local_file),
            "action": "conflict",
        }
    ]
    assert json.loads(local_file.read_text(encoding="utf-8"))["event_id"] == "evt-local"


def test_cli_pull_spool_dry_run_outputs_json_without_mutation(tmp_path: Path) -> None:
    """The pull-spool CLI exposes dry-run counts as JSON."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "remote-spool"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    remote.mkdir()
    (remote / "event.json").write_text(
        json.dumps({"event_id": "evt-1", "kind": "sample"}),
        encoding="utf-8",
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["pull-spool", "--config", str(config_path), "--dry-run"])

    result = json.loads(stdout.getvalue())
    assert code == 0
    assert result["dry_run"] is True
    assert result["processed"] == 1
    assert result["copied"] == 0
    assert not (tmp_path / "spool" / "inbox" / "event.json").exists()


def test_event_ingestion_rejects_missing_event_id(tmp_path: Path) -> None:
    """Events without an ID are invalid instead of being stored as None."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with pytest.raises(ValueError, match="event_id, id, run_id, or job_id"):
        coord.record_event({"kind": "sample"})


def test_intake_records_are_persisted_listed_and_exported(tmp_path: Path) -> None:
    """Raw intake is durable project context, not a task."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    intake = coord.record_intake(
        "Add an inbox for rough ideas.",
        kind="feature",
        source="user",
        repo="agent-tracker",
        tags=["planning", "planning", "inbox"],
        metadata={"priority": "soon"},
        actor="tester",
    )
    listed = coord.intake_records(repo="agent-tracker", kind="feature")
    snapshot = coord.store.snapshot(config.project_id)
    ready_ids = {state.task.task_id for state in coord.ready_tasks()}

    assert intake.intake_id
    assert intake.status == "open"
    assert intake.tags == ["planning", "inbox"]
    assert listed == [intake]
    assert snapshot["intake"][0]["id"] == intake.intake_id
    assert snapshot["intake"][0]["text"] == "Add an inbox for rough ideas."
    assert any(
        audit["action"] == "intake.record"
        and audit["actor"] == "tester"
        and audit["payload"]["intake_id"] == intake.intake_id
        for audit in snapshot["audit"]
    )
    assert intake.intake_id not in ready_ids
    with pytest.raises(ValueError, match="no matching ready task"):
        coord.claim(agent_id="agent-1", task_id=intake.intake_id)


def test_intake_requires_text_and_object_metadata(tmp_path: Path) -> None:
    """Intake records require meaningful text and object metadata."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with pytest.raises(ValueError, match="intake text is required"):
        coord.record_intake("   ")
    with pytest.raises(ValueError, match="metadata"):
        coord.record_intake("Valid text", metadata=json.loads("[]"))


def test_intake_status_can_be_updated_after_triage(tmp_path: Path) -> None:
    """Project managers can close or defer intake without editing SQLite."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Capture a planning note.", actor="tester")

    updated = coord.update_intake_status(
        intake.intake_id,
        status="closed",
        actor="pm",
    )
    snapshot = coord.store.snapshot(config.project_id)

    assert updated.status == "closed"
    assert coord.intake_records(status="closed") == [updated]
    assert coord.intake_records(status="open") == []
    assert any(
        audit["action"] == "intake.status"
        and audit["actor"] == "pm"
        and audit["payload"]["intake_id"] == intake.intake_id
        and audit["payload"]["status"] == "closed"
        for audit in snapshot["audit"]
    )
    with pytest.raises(ValueError, match="invalid intake status"):
        coord.update_intake_status(intake.intake_id, status="unknown")
    with pytest.raises(ValueError, match="unknown intake"):
        coord.update_intake_status("missing", status="closed")


def test_intake_is_compatible_with_pre_intake_database(tmp_path: Path) -> None:
    """Old databases without the intake table stay readable and can self-upgrade."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    with coord.store.transaction(immediate=True) as conn:
        conn.execute("DROP TABLE intake")

    listed_before = coord.intake_records()
    snapshot_before = coord.store.snapshot(config.project_id)
    intake = coord.record_intake("Capture later planning.", actor="tester")
    listed_after = coord.intake_records()

    assert listed_before == []
    assert snapshot_before["intake"] == []
    assert listed_after == [intake]
    assert intake.text == "Capture later planning."


def test_cli_record_and_list_intake_json(tmp_path: Path) -> None:
    """CLI commands can record and list raw intake without creating tasks."""
    config_path = write_project(tmp_path)
    Coordinator(load_config(config_path)).import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "record-intake",
                "--config",
                str(config_path),
                "--kind",
                "check",
                "--source",
                "user",
                "--repo",
                "agent-tracker",
                "--tag",
                "triage",
                "--metadata-json",
                '{"needs": "planning"}',
                "Check whether intake is visible.",
            ]
        )

    recorded = json.loads(stdout.getvalue())
    assert code == 0
    assert recorded["kind"] == "check"
    assert recorded["repo"] == "agent-tracker"
    assert recorded["tags"] == ["triage"]
    assert recorded["metadata"] == {"needs": "planning"}

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "list-intake",
                "--config",
                str(config_path),
                "--json",
                "--kind",
                "check",
            ]
        )

    listed = json.loads(stdout.getvalue())
    assert code == 0
    assert [item["id"] for item in listed["intake"]] == [recorded["id"]]
    assert listed["intake"][0]["status"] == "open"
    assert listed["intake"][0]["text"] == "Check whether intake is visible."


def test_cli_grouped_intake_commands_match_flat_json_behavior(tmp_path: Path) -> None:
    """Grouped intake commands preserve the flat command JSON contracts."""
    config_path = write_project(tmp_path)
    Coordinator(load_config(config_path)).import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "intake",
                "--config",
                str(config_path),
                "record",
                "--kind",
                "feature",
                "--repo",
                "agent-tracker",
                "--tag",
                "ux",
                "Group intake commands.",
            ]
        )

    recorded = json.loads(stdout.getvalue())
    assert code == 0
    assert recorded["kind"] == "feature"
    assert recorded["repo"] == "agent-tracker"
    assert recorded["tags"] == ["ux"]
    assert recorded["text"] == "Group intake commands."

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "intake",
                "--config",
                str(config_path),
                "list",
                "--json",
                "--kind",
                "feature",
            ]
        )

    listed = json.loads(stdout.getvalue())
    assert code == 0
    assert [item["id"] for item in listed["intake"]] == [recorded["id"]]

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "intake",
                "update",
                "--config",
                str(config_path),
                recorded["id"],
                "--status",
                "closed",
                "--actor",
                "pm",
            ]
        )

    updated = json.loads(stdout.getvalue())
    assert code == 0
    assert updated["id"] == recorded["id"]
    assert updated["status"] == "closed"


def test_cli_list_intake_human_output_shows_status(tmp_path: Path) -> None:
    """Human intake listings show status for all intake closeout states."""
    config_path = write_project(tmp_path)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    records = {
        "open": coord.record_intake("Open intake.", kind="idea"),
        "triaged": coord.record_intake("Triaged intake.", kind="feature"),
        "closed": coord.record_intake("Closed intake.", kind="check"),
        "deferred": coord.record_intake("Deferred intake.", kind="note"),
    }
    for status in ("triaged", "closed", "deferred"):
        coord.update_intake_status(records[status].intake_id, status=status)

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(["list-intake", "--config", str(config_path)])

    output = stdout.getvalue()
    assert code == 0
    for status, record in records.items():
        assert f"{record.intake_id}: {status.title()} intake." in output
        assert f"  status   {status}" in output
        assert f"  kind     {record.kind}" in output


def test_cli_human_status_and_intake_output_are_plain_text(tmp_path: Path) -> None:
    """Human status and intake output use readable plain text without boxes."""
    config_path = write_project(tmp_path)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    coord.record_intake("Review the CLI output.", kind="check", repo="agent-tracker")

    stdout = StringIO()
    with redirect_stdout(stdout):
        status_code = cli.main(["status", "--config", str(config_path)])
    status_output = stdout.getvalue()

    stdout = StringIO()
    with redirect_stdout(stdout):
        intake_code = cli.main(["intake", "list", "--config", str(config_path)])
    intake_output = stdout.getvalue()

    assert status_code == 0
    assert intake_code == 0
    assert "Paths\n" in status_output
    assert "Queue\n" in status_output
    assert "  config       " in status_output
    assert "  ready        " in status_output
    assert "Review the CLI output." in intake_output
    assert "  status   open" in intake_output
    assert "  kind     check" in intake_output
    assert "  created  " in intake_output
    assert_no_box_drawing(status_output)
    assert_no_box_drawing(intake_output)


def test_cli_empty_human_lists_use_renderer_boundary(tmp_path: Path) -> None:
    """Empty human intake and proposal listings stay plain and unchanged."""
    config_path = write_project(tmp_path)
    Coordinator(load_config(config_path)).import_tasks()

    stdout = StringIO()
    with redirect_stdout(stdout):
        intake_code = cli.main(["list-intake", "--config", str(config_path)])
    intake_output = stdout.getvalue()

    stdout = StringIO()
    with redirect_stdout(stdout):
        proposal_code = cli.main(["list-proposals", "--config", str(config_path)])
    proposal_output = stdout.getvalue()

    assert intake_code == 0
    assert proposal_code == 0
    assert intake_output == "No intake records.\n"
    assert proposal_output == "No proposed tasks.\n"
    assert_no_box_drawing(intake_output)
    assert_no_box_drawing(proposal_output)


def test_cli_task_human_output_stays_prompt_renderer(tmp_path: Path) -> None:
    """Task human output remains the prompt renderer output."""
    config_path = write_project(tmp_path)
    Coordinator(load_config(config_path)).import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["task", "--config", str(config_path), "ready", "--markdown"])

    output = stdout.getvalue()
    assert code == 0
    assert output.startswith("# Ready\n")
    assert "## Summary\nReady to run." in output
    assert "Paths\n" not in output
    assert_no_box_drawing(output)


def test_interventions_are_recorded_resolved_audited_and_exported(tmp_path: Path) -> None:
    """Interventions are durable state separate from notification delivery."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    intervention = coord.record_intervention(
        reason="setup_missing",
        task_id="ready",
        summary="PR notification setup is missing.",
        metadata={"target": "pull-request"},
        actor="coordinator",
    )
    listed = coord.intervention_records(
        status="open",
        reason="setup_missing",
        task_id="ready",
    )
    snapshot = coord.store.snapshot(config.project_id)

    assert intervention.intervention_id
    assert intervention.status == "open"
    assert listed == [intervention]
    assert snapshot["interventions"][0]["id"] == intervention.intervention_id
    assert snapshot["interventions"][0]["reason"] == "setup_missing"
    assert snapshot["interventions"][0]["metadata"] == {"target": "pull-request"}
    assert any(
        audit["action"] == "intervention.record"
        and audit["actor"] == "coordinator"
        and audit["task_id"] == "ready"
        and audit["payload"]["intervention_id"] == intervention.intervention_id
        for audit in snapshot["audit"]
    )

    with pytest.raises(ValueError, match="resolution requires evidence or reason"):
        coord.resolve_intervention(intervention.intervention_id, actor="pm")

    resolved = coord.resolve_intervention(
        intervention.intervention_id,
        evidence=["evidence://setup-check"],
        actor="pm",
    )
    resolved_snapshot = coord.store.snapshot(config.project_id)

    assert resolved.status == "resolved"
    assert resolved.evidence == ["evidence://setup-check"]
    assert resolved.resolved_by == "pm"
    assert coord.intervention_records(status="open") == []
    assert coord.intervention_records(status="resolved") == [resolved]
    assert any(
        audit["action"] == "intervention.resolve"
        and audit["actor"] == "pm"
        and audit["payload"]["evidence"] == ["evidence://setup-check"]
        for audit in resolved_snapshot["audit"]
    )
    with pytest.raises(ValueError, match="already resolved"):
        coord.resolve_intervention(
            intervention.intervention_id,
            resolution="Already handled.",
            actor="pm",
        )


def test_interventions_validate_reason_task_and_reason_only_resolution(
    tmp_path: Path,
) -> None:
    """Intervention records use the documented reason set and resolution rules."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()

    with pytest.raises(ValueError, match="intervention reason"):
        coord.record_intervention(reason="unknown", summary="Bad reason.")
    with pytest.raises(ValueError, match="unknown task"):
        coord.record_intervention(
            reason="setup_missing",
            task_id="missing",
            summary="Missing task.",
        )
    with pytest.raises(ValueError, match="resolution state"):
        coord.store.record_intervention(
            config.project_id,
            InterventionRecord(
                intervention_id="prefilled",
                reason="setup_missing",
                resolution="Already done.",
            ),
        )

    intervention = coord.record_intervention(
        reason="approval_required",
        summary="Need a human decision.",
    )
    resolved = coord.resolve_intervention(
        intervention.intervention_id,
        resolution="Approved by project owner.",
        actor="pm",
    )

    assert resolved.status == "resolved"
    assert resolved.resolution == "Approved by project owner."
    assert resolved.evidence == []


def test_interventions_are_compatible_with_pre_intervention_database(
    tmp_path: Path,
) -> None:
    """Old databases without intervention tables stay readable and self-upgrade."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    with coord.store.transaction(immediate=True) as conn:
        conn.execute("DROP TABLE interventions")

    assert coord.intervention_records() == []
    assert coord.store.snapshot(config.project_id)["interventions"] == []

    intervention = coord.record_intervention(
        reason="missing_evidence",
        summary="Need final evidence.",
    )

    assert coord.intervention_records() == [intervention]


def test_cli_record_list_and_resolve_interventions(tmp_path: Path) -> None:
    """CLI commands expose intervention state as JSON and readable text."""
    config_path = write_project(tmp_path)
    Coordinator(load_config(config_path)).import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "record-intervention",
                "--config",
                str(config_path),
                "--task-id",
                "ready",
                "--reason",
                "pr_review_needed",
                "--metadata-json",
                '{"surface": "pull-request"}',
                "--actor",
                "coordinator",
                "PR review is needed.",
            ]
        )

    recorded = json.loads(stdout.getvalue())
    assert code == 0
    assert recorded["task_id"] == "ready"
    assert recorded["reason"] == "pr_review_needed"
    assert recorded["metadata"] == {"surface": "pull-request"}

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(["list-interventions", "--config", str(config_path), "--json"])

    listed = json.loads(stdout.getvalue())
    assert code == 0
    assert [item["id"] for item in listed["interventions"]] == [recorded["id"]]

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "resolve-intervention",
                "--config",
                str(config_path),
                recorded["id"],
                "--reason",
                "Review completed.",
                "--actor",
                "pm",
            ]
        )

    resolved = json.loads(stdout.getvalue())
    assert code == 0
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "Review completed."

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(["list-interventions", "--config", str(config_path)])

    output = stdout.getvalue()
    assert code == 0
    assert "PR review is needed." in output
    assert "  status     resolved" in output
    assert "  reason     pr_review_needed" in output
    assert "  task       ready" in output
    assert "  resolution Review completed." in output
    assert_no_box_drawing(output)


def _setup_command_key(command: list[str]) -> tuple[str, ...]:
    """Normalize setup-check commands for test stubs."""
    if len(command) >= 4 and command[0] == "git" and command[1] == "-C":
        return ("git", *command[3:])
    return tuple(command)


def _completed_process(
    command: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> Any:
    """Return a subprocess result for setup-check tests."""
    return service_module.subprocess.CompletedProcess(
        command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _stub_setup_commands(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], tuple[int, str, str]],
) -> list[tuple[str, ...]]:
    """Stub service setup commands and record normalized invocations."""
    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], **_: object) -> Any:
        key = _setup_command_key(command)
        calls.append(key)
        if key not in responses:
            raise AssertionError(f"unexpected setup command: {key}")
        returncode, stdout, stderr = responses[key]
        return _completed_process(command, returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)
    return calls


def test_pr_notification_setup_flags_missing_remote_without_gh_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Git remotes stop setup checks before any GitHub CLI probe."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intervention = coord.record_intervention(
        reason="setup_missing",
        task_id="ready",
        summary="Need notification setup.",
    )
    calls = _stub_setup_commands(
        monkeypatch,
        {
            ("git", "remote", "get-url", "origin"): (
                2,
                "",
                "error: No such remote 'origin'\n",
            ),
        },
    )

    payload = coord.pr_notification_setup_payload(repo_path=tmp_path)

    assert payload["ok"] is False
    assert payload["status"] == "missing_remote"
    assert [issue["code"] for issue in payload["issues"]] == ["missing_remote"]
    assert payload["posting"]["live_supported"] is False
    assert payload["posting"]["prepared_payload_supported"] is True
    assert payload["prepared_payload"]["intervention_ids"] == [intervention.intervention_id]
    assert calls == [("git", "remote", "get-url", "origin")]


def test_pr_notification_setup_flags_missing_pr_association(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GitHub remote without an associated branch PR is reported distinctly."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    calls = _stub_setup_commands(
        monkeypatch,
        {
            ("git", "remote", "get-url", "origin"): (
                0,
                "https://github.com/example/library.git\n",
                "",
            ),
            ("git", "branch", "--show-current"): (
                0,
                "feature/pr-notifications\n",
                "",
            ),
            (
                "gh",
                "pr",
                "view",
                "--repo",
                "example/library",
                "--json",
                "number,url,headRefName,baseRefName,state",
            ): (1, "", "no pull requests found for branch\n"),
        },
    )

    payload = coord.pr_notification_setup_payload(repo_path=tmp_path)

    assert payload["ok"] is False
    assert payload["status"] == "missing_pr_association"
    assert [issue["code"] for issue in payload["issues"]] == ["missing_pr_association"]
    assert payload["repo"]["remote"]["owner"] == "example"
    assert payload["repo"]["remote"]["repo"] == "library"
    assert payload["repo"]["branch"] == "feature/pr-notifications"
    assert payload["target"] is None
    assert ("gh", "auth", "status") not in calls


def test_pr_notification_setup_rejects_pr_target_from_different_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selected Git remote constrains PR lookup and target validation."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    calls = _stub_setup_commands(
        monkeypatch,
        {
            ("git", "remote", "get-url", "upstream"): (
                0,
                "https://github.com/example/selected.git\n",
                "",
            ),
            ("git", "branch", "--show-current"): (0, "feature/setup\n", ""),
            (
                "gh",
                "pr",
                "view",
                "--repo",
                "example/selected",
                "--json",
                "number,url,headRefName,baseRefName,state",
            ): (
                0,
                json.dumps(
                    {
                        "number": 123,
                        "url": "https://github.com/other/selected/pull/123",
                        "headRefName": "feature/setup",
                        "baseRefName": "main",
                        "state": "OPEN",
                    }
                ),
                "",
            ),
        },
    )

    payload = coord.pr_notification_setup_payload(repo_path=tmp_path, remote="upstream")

    assert payload["ok"] is False
    assert payload["status"] == "missing_pr_association"
    assert [issue["code"] for issue in payload["issues"]] == ["missing_pr_association"]
    assert "does not match selected remote" in payload["issues"][0]["message"]
    assert payload["target"] is None
    assert (
        "gh",
        "pr",
        "view",
        "--repo",
        "example/selected",
        "--json",
        "number,url,headRefName,baseRefName,state",
    ) in calls
    assert ("gh", "auth", "status") not in calls


def test_pr_notification_setup_missing_gh_is_missing_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing GitHub CLI is classified as an auth/tooling setup gap."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], **_: object) -> Any:
        key = _setup_command_key(command)
        calls.append(key)
        if command[0] == "gh":
            raise FileNotFoundError(2, "No such file or directory", "gh")
        responses = {
            ("git", "remote", "get-url", "origin"): (
                0,
                "https://github.com/example/library.git\n",
                "",
            ),
            ("git", "branch", "--show-current"): (0, "feature/setup\n", ""),
        }
        returncode, stdout, stderr = responses[key]
        return _completed_process(command, returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)

    payload = coord.pr_notification_setup_payload(repo_path=tmp_path)

    assert payload["ok"] is False
    assert payload["status"] == "missing_auth"
    assert [issue["code"] for issue in payload["issues"]] == ["missing_auth"]
    assert "failed to start" in payload["issues"][0]["message"]
    assert ("gh", "auth", "status") not in calls


def test_pr_notification_setup_flags_missing_auth_after_pr_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live posting is refused when a PR is known but gh auth is unavailable."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    calls = _stub_setup_commands(
        monkeypatch,
        {
            ("git", "remote", "get-url", "origin"): (
                0,
                "git@github.com:example/library.git\n",
                "",
            ),
            ("git", "branch", "--show-current"): (0, "feature/setup\n", ""),
            (
                "gh",
                "pr",
                "view",
                "--repo",
                "example/library",
                "--json",
                "number,url,headRefName,baseRefName,state",
            ): (
                0,
                json.dumps(
                    {
                        "number": 123,
                        "url": "https://github.com/example/library/pull/123",
                        "headRefName": "feature/setup",
                        "baseRefName": "main",
                        "state": "OPEN",
                    }
                ),
                "",
            ),
            ("gh", "auth", "status"): (
                1,
                "",
                "You are not logged into any GitHub hosts\n",
            ),
        },
    )

    payload = coord.pr_notification_setup_payload(repo_path=tmp_path)

    assert payload["ok"] is False
    assert payload["status"] == "missing_auth"
    assert [issue["code"] for issue in payload["issues"]] == ["missing_auth"]
    assert payload["target"]["number"] == 123
    assert payload["target"]["owner"] == "example"
    assert payload["auth"]["checked"] is True
    assert payload["auth"]["authenticated"] is False
    assert ("gh", "auth", "status") in calls


def test_pr_notification_setup_uses_prepared_payload_when_live_posting_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated PR checks still default to prepared payloads in safe mode."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intervention = coord.record_intervention(
        reason="pr_review_needed",
        task_id="ready",
        summary="Human review is needed.",
    )
    _stub_setup_commands(
        monkeypatch,
        {
            ("git", "remote", "get-url", "origin"): (
                0,
                "https://github.com/example/library.git\n",
                "",
            ),
            ("git", "branch", "--show-current"): (0, "feature/setup\n", ""),
            (
                "gh",
                "pr",
                "view",
                "--repo",
                "example/library",
                "--json",
                "number,url,headRefName,baseRefName,state",
            ): (
                0,
                json.dumps(
                    {
                        "number": 123,
                        "url": "https://github.com/example/library/pull/123",
                        "headRefName": "feature/setup",
                        "baseRefName": "main",
                        "state": "OPEN",
                    }
                ),
                "",
            ),
            ("gh", "auth", "status"): (0, "Logged in to github.com\n", ""),
        },
    )

    payload = coord.pr_notification_setup_payload(repo_path=tmp_path)

    assert payload["ok"] is True
    assert payload["status"] == "unsupported_sandbox"
    assert [issue["code"] for issue in payload["issues"]] == ["unsupported_sandbox"]
    assert payload["issues"][0]["severity"] == "warning"
    assert payload["posting"] == {
        "live_supported": False,
        "prepared_payload_supported": True,
        "mode": "prepared_payload",
    }
    assert payload["prepared_payload"]["surface"] == "pull_request_comment"
    assert payload["prepared_payload"]["intervention_ids"] == [intervention.intervention_id]
    assert "Human review is needed." in payload["prepared_payload"]["body"]


def test_cli_check_pr_notification_setup_outputs_json_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setup-check CLI exposes the service diagnostic as JSON."""
    config_path = write_project(tmp_path)
    expected = {
        "project_id": "toy",
        "ok": False,
        "status": "missing_remote",
        "workspace": {"name": "", "kind": "local", "path": str(tmp_path)},
        "repo": {"path": str(tmp_path), "remote": None, "branch": ""},
        "target": None,
        "auth": {"method": "gh", "checked": False, "authenticated": False, "error": ""},
        "posting": {
            "live_supported": False,
            "prepared_payload_supported": True,
            "mode": "prepared_payload",
        },
        "issues": [
            {
                "code": "missing_remote",
                "severity": "error",
                "message": "missing",
                "remediation": "add remote",
            }
        ],
        "prepared_payload": {
            "surface": "pull_request_comment",
            "body": "body",
            "intervention_ids": [],
            "interventions": [],
        },
    }

    def fake_payload(self: Coordinator, **_: object) -> dict[str, Any]:
        return expected

    monkeypatch.setattr(Coordinator, "pr_notification_setup_payload", fake_payload)
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "check-pr-notification-setup",
                "--config",
                str(config_path),
                "--repo-path",
                str(tmp_path),
                "--json",
            ]
        )

    assert code == 0
    assert json.loads(stdout.getvalue()) == expected


def test_cli_help_output_is_plain_text_without_rich_boxes() -> None:
    """Root and grouped help output stay copy-paste-safe plain text."""
    root_help = cli.build_parser().format_help()
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["intake", "--help"])

    intake_help = stdout.getvalue()
    assert code == 0
    assert "record-intake" in root_help
    assert "intake" in root_help
    assert "Usage: agent-tracker intake" in intake_help
    assert "--config TEXT" in intake_help
    assert "record" in intake_help
    assert "list" in intake_help
    assert "update" in intake_help
    assert_no_box_drawing(root_help)
    assert_no_box_drawing(intake_help)


def test_triage_proposes_task_from_intake_without_claiming(tmp_path: Path) -> None:
    """Triage creates proposed task contracts outside live queue state."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage for intake.", kind="feature")

    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
        repo="agent-tracker",
        summary="Promote intake into reviewed proposed tasks.",
        next_action="Define the proposal contract.",
        role="maintainer",
        write_scopes=["src/agent_tracker/service.py", "tests/test_agent_tracker.py"],
        validation_checks=["uv run pytest"],
        requirements=[{"task": "foundation", "description": "Base queue exists."}],
        authority="local code and docs",
        intervention_needs=["approval"],
        notebook_updates=["project-notebook"],
        metadata={"lane": "planning/intake"},
        actor="pm",
    )
    listed = coord.proposed_task_records(intake_id=intake.intake_id)
    snapshot = coord.store.snapshot(config.project_id)
    ready_ids = {state.task.task_id for state in coord.ready_tasks()}
    triaged_intake = coord.intake_records(status="triaged")

    assert proposal.task.task_id == "add-triage"
    assert proposal.task.metadata["roles"] == ["maintainer"]
    assert proposal.task.metadata["write_scopes"] == [
        "src/agent_tracker/service.py",
        "tests/test_agent_tracker.py",
    ]
    assert proposal.task.metadata["authority"] == "local code and docs"
    assert proposal.task.metadata["intervention_needs"] == ["approval"]
    assert proposal.task.metadata["notebook_updates"] == ["project-notebook"]
    assert proposal.requirements == [
        {"kind": "task", "task": "foundation", "description": "Base queue exists."}
    ]
    assert [item.intake_id for item in triaged_intake] == [intake.intake_id]
    assert listed == [proposal]
    assert snapshot["proposed_tasks"][0]["id"] == proposal.proposal_id
    assert snapshot["proposed_tasks"][0]["task"]["id"] == "add-triage"
    assert any(
        audit["action"] == "proposal.record"
        and audit["actor"] == "pm"
        and audit["payload"]["proposal_id"] == proposal.proposal_id
        for audit in snapshot["audit"]
    )
    assert "add-triage" not in ready_ids
    with pytest.raises(ValueError, match="no matching ready task"):
        coord.claim(agent_id="agent-1", task_id="add-triage")


def test_promote_proposed_task_creates_claimable_live_task(tmp_path: Path) -> None:
    """Reviewed proposals can become live queue tasks without task-plan edits."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
        repo="agent-tracker",
        summary="Promote reviewed intake into live task state.",
        next_action="Implement the triage promotion.",
        validation_checks=["uv run pytest"],
        requirements=[{"task": "foundation", "description": "Base queue exists."}],
        actor="pm",
    )

    promoted = coord.promote_proposed_task(proposal.proposal_id, actor="pm")
    promoted_again = coord.promote_proposed_task(proposal.proposal_id, actor="pm")
    coord.import_tasks()
    state = coord.get_task("add-triage")
    ready_ids = {item.task.task_id for item in coord.ready_tasks()}
    overview_ready_ids = {item["id"] for item in coord.overview_payload()["groups"]["ready"]}
    claim = coord.claim(agent_id="agent-1", task_id="add-triage")
    snapshot = coord.store.snapshot(config.project_id)

    assert promoted.status == "promoted"
    assert promoted_again.status == "promoted"
    assert coord.proposed_task_records(status="promoted") == [promoted]
    assert coord.intake_records(status="triaged")[0].intake_id == intake.intake_id
    assert state.state == "ready"
    assert state.task.status == "pending"
    assert state.task.summary == "Promote reviewed intake into live task state."
    assert state.task.validation_checks == ["uv run pytest"]
    assert state.requirements[0].detail == "foundation: done"
    assert "add-triage" in ready_ids
    assert "add-triage" in overview_ready_ids
    assert claim.task_id == "add-triage"
    assert any(
        audit["action"] == "proposal.promote"
        and audit["actor"] == "pm"
        and audit["task_id"] == "add-triage"
        for audit in snapshot["audit"]
    )


def test_update_proposed_task_edits_contract_before_promotion(tmp_path: Path) -> None:
    """Proposed task contracts can be corrected before they become live tasks."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
        repo="agent-tracker",
        summary="Promote reviewed intake into live task state.",
        next_action="Draft the change.",
        validation_checks=["uv run pytest"],
        metadata={"lane": "planning"},
        actor="pm",
    )

    updated = coord.update_proposed_task(
        proposal.proposal_id,
        task_id="add-triage-v2",
        title="Add proposal edit workflow",
        repo="agent-tracker",
        summary="Allow proposals to be corrected before promotion.",
        next_action="Implement update and withdraw commands.",
        role="maintainer",
        write_scopes=["src/agent_tracker/service.py", "tests/test_agent_tracker.py"],
        validation_checks=[".venv/bin/pytest tests/test_agent_tracker.py"],
        requirements=[{"task": "foundation", "description": "Base queue exists."}],
        authority="local code and tests",
        metadata={"lane": "triage"},
        actor="pm",
    )
    listed = coord.proposed_task_records()
    snapshot = coord.store.snapshot(config.project_id)

    assert updated.status == "proposed"
    assert updated.created_at == proposal.created_at
    assert updated.task.task_id == "add-triage-v2"
    assert updated.task.title == "Add proposal edit workflow"
    assert updated.task.next_action == "Implement update and withdraw commands."
    assert updated.task.validation_checks == [".venv/bin/pytest tests/test_agent_tracker.py"]
    assert updated.task.execution["primary_files"] == [
        "src/agent_tracker/service.py",
        "tests/test_agent_tracker.py",
    ]
    assert updated.task.metadata == {
        "lane": "triage",
        "roles": ["maintainer"],
        "write_scopes": [
            "src/agent_tracker/service.py",
            "tests/test_agent_tracker.py",
        ],
        "authority": "local code and tests",
    }
    assert updated.requirements == [
        {"kind": "task", "task": "foundation", "description": "Base queue exists."}
    ]
    assert listed == [updated]
    update_audit = next(
        audit for audit in snapshot["audit"] if audit["action"] == "proposal.update"
    )
    assert update_audit["actor"] == "pm"
    assert update_audit["payload"]["proposal_id"] == proposal.proposal_id
    assert update_audit["payload"]["previous_task_id"] == "add-triage"
    assert update_audit["payload"]["task_id"] == "add-triage-v2"
    assert update_audit["payload"]["changed_fields"] == [
        "task.id",
        "task.title",
        "task.summary",
        "task.next_action",
        "task.execution",
        "task.validation_checks",
        "task.metadata",
        "requirements",
    ]
    assert update_audit["payload"]["changes"]["task.title"] == {
        "previous": "Add triage workflow",
        "updated": "Add proposal edit workflow",
    }
    with pytest.raises(ValueError, match="task already exists"):
        coord.update_proposed_task(proposal.proposal_id, task_id="ready")

    other_intake = coord.record_intake("Add another proposal.", kind="feature")
    other = coord.propose_task_from_intake(
        other_intake.intake_id,
        task_id="other-triage",
        title="Other triage workflow",
    )
    with pytest.raises(ValueError, match="proposal task id already exists"):
        coord.update_proposed_task(proposal.proposal_id, task_id=other.task.task_id)

    promoted = coord.promote_proposed_task(proposal.proposal_id, actor="pm")
    state = coord.get_task("add-triage-v2")

    assert promoted.task.task_id == "add-triage-v2"
    assert state.task.title == "Add proposal edit workflow"
    assert state.task.next_action == "Implement update and withdraw commands."
    assert state.task.validation_checks == [".venv/bin/pytest tests/test_agent_tracker.py"]
    assert state.task.execution["primary_files"] == [
        "src/agent_tracker/service.py",
        "tests/test_agent_tracker.py",
    ]
    assert state.task.metadata == {
        "lane": "triage",
        "roles": ["maintainer"],
        "write_scopes": [
            "src/agent_tracker/service.py",
            "tests/test_agent_tracker.py",
        ],
        "authority": "local code and tests",
    }
    assert state.requirements[0].detail == "foundation: done"


def test_withdraw_proposed_task_prevents_promotion_and_audits(tmp_path: Path) -> None:
    """Withdrawn proposals are retained as rejected records and cannot promote."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
        actor="pm",
    )

    withdrawn = coord.withdraw_proposed_task(proposal.proposal_id, actor="pm")
    snapshot = coord.store.snapshot(config.project_id)

    assert withdrawn.status == "rejected"
    assert coord.proposed_task_records(status="rejected") == [withdrawn]
    with pytest.raises(ValueError, match="cannot promote proposal .* from status rejected"):
        coord.promote_proposed_task(proposal.proposal_id, actor="pm")
    with pytest.raises(ValueError, match="cannot update proposal .* from status rejected"):
        coord.update_proposed_task(proposal.proposal_id, title="Edited after withdrawal")
    with pytest.raises(ValueError, match="cannot withdraw proposal .* from status rejected"):
        coord.withdraw_proposed_task(proposal.proposal_id, actor="pm")
    followup_intake = coord.record_intake("Re-propose the same ID.", kind="feature")
    with pytest.raises(ValueError, match="existing rejected proposal"):
        coord.propose_task_from_intake(
            followup_intake.intake_id,
            task_id=proposal.task.task_id,
            title="Re-propose triage workflow",
        )
    assert any(
        audit["action"] == "proposal.withdraw"
        and audit["actor"] == "pm"
        and audit["payload"]
        == {
            "proposal_id": proposal.proposal_id,
            "intake_id": intake.intake_id,
        }
        for audit in snapshot["audit"]
    )


def test_promoted_proposal_cannot_be_updated_or_withdrawn(tmp_path: Path) -> None:
    """Proposal edits and withdrawal stop once a task is live."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
        actor="pm",
    )
    coord.promote_proposed_task(proposal.proposal_id, actor="pm")

    with pytest.raises(ValueError, match="cannot update proposal .* from status promoted"):
        coord.update_proposed_task(proposal.proposal_id, title="Edited after promotion")
    with pytest.raises(ValueError, match="cannot withdraw proposal .* from status promoted"):
        coord.withdraw_proposed_task(proposal.proposal_id, actor="pm")


def test_triage_rejects_missing_intake_and_existing_task_id(tmp_path: Path) -> None:
    """Proposed tasks must come from intake and not collide with live tasks."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add a task.", kind="feature")

    with pytest.raises(ValueError, match="unknown intake"):
        coord.propose_task_from_intake("missing", task_id="new-task", title="New Task")
    with pytest.raises(ValueError, match="task already exists"):
        coord.propose_task_from_intake(
            intake.intake_id,
            task_id="ready",
            title="Duplicate Ready",
        )


def test_import_rejects_task_id_that_is_still_proposed(tmp_path: Path) -> None:
    """Plain task imports cannot silently promote proposed task contracts."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
    )
    task_plan = tmp_path / "tasks.json"
    payload = json.loads(task_plan.read_text(encoding="utf-8"))
    payload["tasks"].append(
        {
            "id": proposal.task.task_id,
            "title": proposal.task.title,
            "status": "pending",
            "requirements": [{"kind": "task", "task": "foundation"}],
        }
    )
    task_plan.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="still proposed"):
        coord.import_tasks()
    with pytest.raises(ValueError, match="no matching ready task"):
        coord.claim(agent_id="agent-1", task_id=proposal.task.task_id)


def test_proposed_tasks_are_compatible_with_pre_triage_database(tmp_path: Path) -> None:
    """Old databases without proposal tables stay readable and can self-upgrade."""
    config = load_config(write_project(tmp_path))
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    with coord.store.transaction(immediate=True) as conn:
        conn.execute("DROP TABLE proposed_tasks")

    listed_before = coord.proposed_task_records()
    snapshot_before = coord.store.snapshot(config.project_id)
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
    )
    listed_after = coord.proposed_task_records()

    assert listed_before == []
    assert snapshot_before["proposed_tasks"] == []
    assert listed_after == [proposal]


def test_cli_propose_and_list_proposals_json(tmp_path: Path) -> None:
    """CLI triage records proposed tasks without importing them as live tasks."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "propose-task",
                "--config",
                str(config_path),
                intake.intake_id,
                "--task-id",
                "add-triage",
                "--title",
                "Add triage workflow",
                "--repo",
                "agent-tracker",
                "--role",
                "maintainer",
                "--write-scope",
                "src/agent_tracker/service.py",
                "--validation-check",
                "uv run pytest",
                "--dependency",
                "foundation:Base queue exists.",
                "--authority",
                "local code and docs",
            ]
        )

    proposed = json.loads(stdout.getvalue())
    assert code == 0
    assert proposed["task"]["id"] == "add-triage"
    assert proposed["requirements"] == [
        {"kind": "task", "task": "foundation", "description": "Base queue exists."}
    ]

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(["list-proposals", "--config", str(config_path), "--json"])

    listed = json.loads(stdout.getvalue())
    assert code == 0
    assert [proposal["id"] for proposal in listed["proposals"]] == [proposed["id"]]
    with pytest.raises(ValueError, match="no matching ready task"):
        Coordinator(load_config(config_path)).claim(agent_id="agent-1", task_id="add-triage")

    close_intake = Coordinator(load_config(config_path)).record_intake("Close me.")
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "update-intake",
                "--config",
                str(config_path),
                close_intake.intake_id,
                "--status",
                "closed",
                "--actor",
                "pm",
            ]
        )

    updated_intake = json.loads(stdout.getvalue())
    assert code == 0
    assert updated_intake["status"] == "closed"

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "promote-proposal",
                "--config",
                str(config_path),
                proposed["id"],
                "--actor",
                "pm",
            ]
        )

    promoted = json.loads(stdout.getvalue())
    state = Coordinator(load_config(config_path)).get_task("add-triage")
    assert code == 0
    assert promoted["status"] == "promoted"
    assert state.state == "ready"
    assert state.task.task_id == "add-triage"


def test_cli_list_proposals_human_output_uses_renderer_boundary(tmp_path: Path) -> None:
    """Human proposal listings stay readable and no-box through the renderer boundary."""
    config_path = write_project(tmp_path)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
        repo="agent-tracker",
        next_action="Review and promote the proposed task.",
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(["list-proposals", "--config", str(config_path)])

    output = stdout.getvalue()
    assert code == 0
    assert f"{proposal.proposal_id}: add-triage - Add triage workflow" in output
    assert f"  intake: {intake.intake_id}; status: proposed" in output
    assert "  repo: agent-tracker" in output
    assert "  next: Review and promote the proposed task." in output
    assert_no_box_drawing(output)


def test_cli_update_and_withdraw_proposal_json(tmp_path: Path) -> None:
    """CLI proposal edits and withdrawal return proposal JSON."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    intake = coord.record_intake("Add triage.", kind="feature")
    proposal = coord.propose_task_from_intake(
        intake.intake_id,
        task_id="add-triage",
        title="Add triage workflow",
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "update-proposal",
                "--config",
                str(config_path),
                proposal.proposal_id,
                "--title",
                "Add proposal edit workflow",
                "--next-action",
                "Implement update and withdraw commands.",
                "--validation-check",
                ".venv/bin/pytest tests/test_agent_tracker.py",
                "--dependency",
                "foundation:Base queue exists.",
                "--metadata-json",
                '{"lane": "triage"}',
                "--actor",
                "pm",
            ]
        )

    updated = json.loads(stdout.getvalue())
    assert code == 0
    assert updated["status"] == "proposed"
    assert updated["task"]["title"] == "Add proposal edit workflow"
    assert updated["task"]["next_action"] == "Implement update and withdraw commands."
    assert updated["task"]["validation_checks"] == [".venv/bin/pytest tests/test_agent_tracker.py"]
    assert updated["task"]["metadata"] == {"lane": "triage"}
    assert updated["requirements"] == [
        {"kind": "task", "task": "foundation", "description": "Base queue exists."}
    ]

    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli.main(
            [
                "withdraw-proposal",
                "--config",
                str(config_path),
                proposal.proposal_id,
                "--actor",
                "pm",
            ]
        )

    withdrawn = json.loads(stdout.getvalue())
    assert code == 0
    assert withdrawn["status"] == "rejected"

    stderr = StringIO()
    with redirect_stderr(stderr):
        code = cli.main(
            [
                "promote-proposal",
                "--config",
                str(config_path),
                proposal.proposal_id,
            ]
        )

    assert code == 1
    assert "from status rejected" in stderr.getvalue()


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


def test_env_config_preserves_canonical_mutation_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-derived config paths still enforce canonical mutation safety."""
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
    monkeypatch.setenv(PROJECT_CONFIG_ENV_VAR, str(copied_config))
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(["claim", "--agent", "agent-1", "--role", "worker"])

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


def test_mcp_complete_task_supports_direct_merge_override(tmp_path: Path) -> None:
    """MCP completion exposes the explicit direct-merge override."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)
    claim = tools.claim_task(agent_id="agent-1", task_id="ready")

    tools.complete_task(
        task_id="ready",
        lease_token=claim["lease_token"],
        evidence=["git:main-abc123"],
        agent_id="agent-1",
        direct_merge=True,
    )

    assert tools.get_task_context("ready")["state"] == "done"


def test_mcp_resolver_supports_direct_merge_override(tmp_path: Path) -> None:
    """MCP resolver completion exposes the explicit direct-merge override."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)
    claim = tools.claim_task(agent_id="agent-1", task_id="ready")
    tools.submit_review_task(
        task_id="ready",
        lease_token=claim["lease_token"],
        evidence=["git:main-abc123"],
        agent_id="agent-1",
    )

    tools.resolve_review_task(
        task_id="ready",
        agent_id="reviewer-1",
        direct_merge=True,
    )

    assert tools.get_task_context("ready")["state"] == "done"


def test_mcp_handlers_submit_review_and_await_integration(tmp_path: Path) -> None:
    """MCP-friendly handlers expose review and integration handoff operations."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)

    review_claim = tools.claim_task(agent_id="agent-1", task_id="ready")
    tools.submit_review_task(
        task_id="ready",
        lease_token=review_claim["lease_token"],
        evidence=["evidence://review"],
        agent_id="agent-1",
    )
    tools.resolve_review_task(
        task_id="ready",
        evidence=["evidence://approval"],
        agent_id="reviewer-1",
    )
    review_status = tools.get_task_context("ready")

    with coord.store.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending'
            WHERE project_id = ? AND task_id = ?
            """,
            (config.project_id, "ready"),
        )
    integration_claim = tools.claim_task(agent_id="agent-1", task_id="ready")
    tools.await_integration_task(
        task_id="ready",
        lease_token=integration_claim["lease_token"],
        status="awaiting_merge",
        evidence=["evidence://merge"],
        agent_id="agent-1",
    )
    tools.resolve_integration_task(
        task_id="ready",
        status="failed",
        reason="merge failed",
        evidence=["evidence://failure"],
        agent_id="reviewer-1",
    )
    integration_status = tools.get_task_context("ready")

    assert review_status["state"] == "done"
    assert integration_status["state"] == "failed"
    assert set(integration_status["evidence"]) == {
        "evidence://review",
        "evidence://approval",
        "evidence://merge",
        "evidence://failure",
    }


def test_mcp_typed_wrappers_expose_scoped_operations_and_aliases(
    tmp_path: Path,
) -> None:
    """Typed MCP wrappers expose stable payloads and preserve older names."""
    config_path = write_overview_project(tmp_path)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)

    status = tools.status()
    alias_status = tools.get_project_status()
    overview = tools.overview(limit=1)
    worker_prompt = tools.launch_worker_prompt("ready", agent_id="agent-1")
    launch_worker = tools.launch_worker("ready", agent_id="agent-1")

    claim_keys = {"project_id", "task_id", "lease_token", "lease_expires_at", "agent_id"}
    claim = tools.claim(agent_id="agent-1", task_id="ready")
    alias_claim = tools.claim_task(agent_id="agent-2", task_id="other-ready")
    heartbeat = tools.heartbeat(
        task_id="ready",
        lease_token=claim["lease_token"],
        agent_id="agent-1",
    )
    alias_heartbeat = tools.heartbeat_task(
        task_id="other-ready",
        lease_token=alias_claim["lease_token"],
        agent_id="agent-2",
    )

    assert {"project_id", "name", "db_path", "tasks", "ready", "active", "blocked"} <= set(status)
    assert alias_status["project_id"] == status["project_id"] == "toy"
    assert list(overview["groups"]) == [
        "ready",
        "active",
        "review",
        "integration",
        "blocked",
        "recently_completed",
    ]
    assert overview["limit"] == 1
    assert set(worker_prompt) == {
        "project_id",
        "task_id",
        "agent_id",
        "launch_mode",
        "launched",
        "prompt",
        "task",
    }
    assert worker_prompt["project_id"] == "toy"
    assert worker_prompt["task_id"] == "ready"
    assert worker_prompt["agent_id"] == "agent-1"
    assert worker_prompt["launch_mode"] == "prompt_only"
    assert worker_prompt["launched"] is False
    assert launch_worker["launch_mode"] == "prompt_only"
    assert launch_worker["launched"] is False
    assert worker_prompt["task"]["id"] == "ready"
    assert "# Ready" in worker_prompt["prompt"]
    assert set(claim) == claim_keys
    assert set(alias_claim) == claim_keys
    assert set(heartbeat) == claim_keys
    assert set(alias_heartbeat) == claim_keys
    assert heartbeat["task_id"] == "ready"
    assert alias_heartbeat["task_id"] == "other-ready"
    assert tools.complete(
        task_id="ready",
        lease_token=heartbeat["lease_token"],
        evidence=["evidence://typed-ready"],
        agent_id="agent-1",
    ) == {"ok": True}
    assert tools.complete_task(
        task_id="other-ready",
        lease_token=alias_heartbeat["lease_token"],
        evidence=["evidence://alias-ready"],
        agent_id="agent-2",
    ) == {"ok": True}


def test_mcp_typed_wrappers_preserve_lease_enforcement(tmp_path: Path) -> None:
    """Typed mutating wrappers still enforce lease token and owner checks."""
    config_path = write_project(tmp_path)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)
    claim = tools.claim(agent_id="agent-1", task_id="ready")

    with pytest.raises(ValueError, match="different agent"):
        tools.heartbeat(
            task_id="ready",
            lease_token=claim["lease_token"],
            agent_id="agent-2",
        )
    with pytest.raises(ValueError, match="lease token is invalid"):
        tools.complete(
            task_id="ready",
            lease_token="wrong-token",
            evidence=["evidence://ready"],
            agent_id="agent-1",
        )

    assert coord.get_task("ready").state == "claimed"


def test_mcp_typed_status_and_overview_keep_stale_lease_recovery_explicit(
    tmp_path: Path,
) -> None:
    """Typed read wrappers inspect stale leases without mutating unless asked."""
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
    tools = AgentTrackerTools(config_path)

    status = tools.status()
    overview = tools.overview()
    with coord.store.transaction() as conn:
        inspected = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()

    assert "ready" in status["ready"]
    assert overview["groups"]["ready"][0]["id"] == "ready"
    assert inspected["status"] == "claimed"
    assert inspected["lease_token"] == claim.lease_token

    recovered_status = tools.status(recover_stale_leases=True)
    with coord.store.transaction() as conn:
        recovered = conn.execute(
            "SELECT status, lease_token FROM tasks WHERE project_id = ? AND task_id = ?",
            (config.project_id, "ready"),
        ).fetchone()
    assert "ready" in recovered_status["ready"]
    assert recovered["status"] == "pending"
    assert recovered["lease_token"] == ""


def test_mcp_typed_mutations_preserve_canonical_config_refusal(tmp_path: Path) -> None:
    """Typed mutating wrappers cannot bypass canonical config authority."""
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
    tools = AgentTrackerTools(copied_config)

    assert tools.status()["project_id"] == "toy"
    with pytest.raises(ValueError, match="canonical config"):
        tools.claim(agent_id="agent-1", role="worker")
    assert not (copied_root / "state.sqlite").exists()


def test_mcp_typed_spool_wrappers_expose_payload_shapes(tmp_path: Path) -> None:
    """Typed MCP wrappers expose pull-spool and ingest-spool payloads."""
    config_path = write_project(tmp_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config_payload["spool"]["remote_inbox"] = "remote-spool"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    remote = tmp_path / "remote-spool"
    local = tmp_path / "spool" / "inbox"
    remote.mkdir()
    (remote / "event.json").write_text(
        json.dumps({"event_id": "evt-typed", "kind": "sample"}),
        encoding="utf-8",
    )
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)

    dry_run = tools.pull_spool(dry_run=True)
    pulled = tools.pull_spool()
    ingested = tools.ingest_spool(actor="mcp")

    assert set(dry_run) == {
        "dry_run",
        "remote_inbox",
        "local_inbox",
        "processed",
        "copied",
        "skipped",
        "conflicts",
        "files",
    }
    assert dry_run["dry_run"] is True
    assert dry_run["processed"] == 1
    assert dry_run["copied"] == 0
    assert dry_run["files"] == [
        {
            "source": str(remote / "event.json"),
            "target": str(local / "event.json"),
            "action": "copy",
        }
    ]
    assert pulled["dry_run"] is False
    assert pulled["processed"] == 1
    assert pulled["copied"] == 1
    assert ingested == {"processed": 1, "inserted": 1, "errors": 0}


def test_mcp_ready_task_limit_rejects_negative_values(tmp_path: Path) -> None:
    """MCP handlers expose service validation for invalid limits."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)

    with pytest.raises(ValueError, match="greater than or equal to zero"):
        tools.list_ready_tasks(limit=-1)


def test_cli_record_evidence_appends_idempotently_and_audits(
    tmp_path: Path,
) -> None:
    """CLI record-evidence appends once and audits only inserted evidence."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    stdout = StringIO()

    with redirect_stdout(stdout):
        first_code = cli.main(
            [
                "record-evidence",
                "--config",
                str(config_path),
                "ready",
                "evidence://one",
                "--actor",
                "agent-1",
            ]
        )
        second_code = cli.main(
            [
                "record-evidence",
                "--config",
                str(config_path),
                "ready",
                "evidence://one",
                "--actor",
                "agent-1",
            ]
        )
    snapshot = coord.store.snapshot(config.project_id)

    assert first_code == 0
    assert second_code == 0
    assert coord.get_task("ready").evidence == ["evidence://one"]
    assert "Recorded for ready: evidence://one" in stdout.getvalue()
    assert "Evidence already recorded for ready: evidence://one" in stdout.getvalue()
    evidence_audits = [audit for audit in snapshot["audit"] if audit["action"] == "evidence.record"]
    assert len(evidence_audits) == 1
    assert evidence_audits[0]["task_id"] == "ready"
    assert evidence_audits[0]["actor"] == "agent-1"
    assert evidence_audits[0]["payload"] == {"uri": "evidence://one"}


def test_cli_submit_review_updates_state_and_records_repeated_evidence(
    tmp_path: Path,
) -> None:
    """CLI submit-review parses lease, agent, task ID, and repeatable evidence."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "submit-review",
                "--config",
                str(config_path),
                "ready",
                "--lease-token",
                claim.lease_token,
                "--agent",
                "agent-1",
                "--evidence",
                "evidence://one",
                "--evidence",
                "evidence://two",
            ]
        )
    state = coord.get_task("ready")

    assert code == 0
    assert "Submitted ready for review" in stdout.getvalue()
    assert state.state == "awaiting_review"
    assert state.evidence == ["evidence://one", "evidence://two"]


def test_cli_submit_review_reports_validation_errors_without_traceback(
    tmp_path: Path,
) -> None:
    """CLI submit-review reports invalid lease ownership concisely."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "submit-review",
                "--config",
                str(config_path),
                "ready",
                "--lease-token",
                claim.lease_token,
                "--agent",
                "agent-2",
                "--evidence",
                "evidence://review",
            ]
        )

    assert code == 1
    assert "different agent" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_cli_complete_reports_completion_policy_errors_without_traceback(
    tmp_path: Path,
) -> None:
    """CLI complete reports completion policy failures concisely."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "complete",
                "--config",
                str(config_path),
                "ready",
                "--lease-token",
                claim.lease_token,
                "--agent",
                "agent-1",
                "--evidence",
                "git:abc123",
            ]
        )

    assert code == 1
    assert "pr:/review:/integration:" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_cli_complete_accepts_direct_merge_override(tmp_path: Path) -> None:
    """CLI complete passes the explicit direct-merge override."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")

    code = cli.main(
        [
            "complete",
            "--config",
            str(config_path),
            "ready",
            "--lease-token",
            claim.lease_token,
            "--agent",
            "agent-1",
            "--evidence",
            "git:main-abc123",
            "--direct-merge",
        ]
    )

    assert code == 0
    assert coord.get_task("ready").state == "done"


def test_cli_resolve_review_completes_waiting_task(tmp_path: Path) -> None:
    """CLI resolve-review finalizes a task waiting for review."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review("ready", lease_token=claim.lease_token, agent_id="agent-1")
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = cli.main(
            [
                "resolve-review",
                "--config",
                str(config_path),
                "ready",
                "--agent",
                "reviewer-1",
                "--evidence",
                "evidence://approval",
            ]
        )
    state = coord.get_task("ready")

    assert code == 0
    assert "Resolved review for ready as done" in stdout.getvalue()
    assert state.state == "done"
    assert state.evidence == ["evidence://approval"]


def test_cli_resolve_review_reports_completion_policy_errors_without_traceback(
    tmp_path: Path,
) -> None:
    """CLI resolve-review reports completion policy failures concisely."""
    config_path = write_project_with_completion_policy(tmp_path, PR_OR_REVIEW_POLICY)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.submit_review(
        "ready",
        lease_token=claim.lease_token,
        agent_id="agent-1",
        evidence=["git:abc123"],
    )
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "resolve-review",
                "--config",
                str(config_path),
                "ready",
                "--agent",
                "reviewer-1",
            ]
        )

    assert code == 1
    assert "pr:/review:/integration:" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_cli_resolve_integration_requires_failure_reason_without_traceback(
    tmp_path: Path,
) -> None:
    """CLI resolve-integration reports missing failure reasons concisely."""
    config_path = write_project(tmp_path)
    config = load_config(config_path)
    coord = Coordinator(config)
    coord.import_tasks()
    claim = coord.claim(agent_id="agent-1", task_id="ready")
    coord.await_integration("ready", lease_token=claim.lease_token, agent_id="agent-1")
    stderr = StringIO()

    with redirect_stderr(stderr):
        code = cli.main(
            [
                "resolve-integration",
                "--config",
                str(config_path),
                "ready",
                "--agent",
                "reviewer-1",
                "--status",
                "failed",
            ]
        )

    assert code == 1
    assert "reason is required" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_mcp_resolvers_require_actor_identity(tmp_path: Path) -> None:
    """Lease-free MCP resolver paths require durable actor identity."""
    config_path = write_project(tmp_path)
    coord = Coordinator(load_config(config_path))
    coord.import_tasks()
    tools = AgentTrackerTools(config_path)
    claim = tools.claim_task(agent_id="agent-1", task_id="ready")
    tools.submit_review_task("ready", lease_token=claim["lease_token"], agent_id="agent-1")

    with pytest.raises(ValueError, match="agent is required"):
        tools.resolve_review_task("ready")


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


def test_agent_coordinator_skill_is_vendored_and_installable(tmp_path: Path) -> None:
    """The reusable agent-coordinator skill can be bootstrapped for new installs."""
    source = vendored_skill_path("agent-coordinator")
    installed = install_skill(
        name="agent-coordinator",
        destination_root=tmp_path,
        dry_run=False,
    )

    assert (source / "SKILL.md").exists()
    assert installed == tmp_path / "agent-coordinator"
    assert (installed / "SKILL.md").exists()
    assert (installed / "agents" / "openai.yaml").exists()
    assert "name: agent-coordinator" in (installed / "SKILL.md").read_text(encoding="utf-8")


def test_task_worker_skill_is_vendored_and_installable(tmp_path: Path) -> None:
    """The reusable task-worker skill can be bootstrapped for new installs."""
    source = vendored_skill_path("task-worker")
    installed = install_skill(
        name="task-worker",
        destination_root=tmp_path,
        dry_run=False,
    )

    assert (source / "SKILL.md").exists()
    assert installed == tmp_path / "task-worker"
    assert (installed / "SKILL.md").exists()
    assert (installed / "agents" / "openai.yaml").exists()
    skill_text = (installed / "SKILL.md").read_text(encoding="utf-8")
    assert "name: task-worker" in skill_text
    assert "Do not run `next` to select your own work." in skill_text
    assert "claim that exact task" in skill_text
    assert "triage intake" in skill_text


def test_available_skill_names_lists_vendored_skills() -> None:
    """Vendored skill discovery lists the installable skill directories."""
    assert available_skill_names() == [
        "agent-coordinator",
        "project-manager",
        "task-worker",
    ]


def test_install_skills_can_install_all_vendored_skills(tmp_path: Path) -> None:
    """The bootstrap API can install every vendored skill in one call."""
    installed = install_skills(destination_root=tmp_path, all_skills=True)

    assert [path.name for path in installed] == available_skill_names()
    for skill_name in available_skill_names():
        assert (tmp_path / skill_name / "SKILL.md").exists()


def test_install_skills_can_install_a_subset(tmp_path: Path) -> None:
    """The bootstrap API can install an explicit subset of vendored skills."""
    installed = install_skills(
        names=["agent-coordinator", "task-worker"],
        destination_root=tmp_path,
    )

    assert [path.name for path in installed] == ["agent-coordinator", "task-worker"]
    assert (tmp_path / "agent-coordinator" / "SKILL.md").exists()
    assert (tmp_path / "task-worker" / "SKILL.md").exists()
    assert not (tmp_path / "project-manager").exists()


def test_skill_bootstrap_cli_can_dry_run_all_skills(tmp_path: Path) -> None:
    """The bootstrap CLI can target every vendored skill without copying files."""
    stdout = StringIO()

    with redirect_stdout(stdout):
        code = skill_bootstrap_module.main(
            ["--all", "--destination-root", str(tmp_path), "--dry-run"]
        )

    assert code == 0
    output = stdout.getvalue()
    for skill_name in available_skill_names():
        assert f"Would install {skill_name} skill at {tmp_path / skill_name}" in output
        assert not (tmp_path / skill_name).exists()


def test_project_manager_skill_has_no_project_specific_terms() -> None:
    """Vendored skills must stay generic across agent-tracker projects."""
    forbidden = ["hpc", "slurm", "test_inversions", "acrg"]
    offenders = []
    for skill_name in ("project-manager", "agent-coordinator", "task-worker"):
        source = vendored_skill_path(skill_name)
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").lower()
            for term in forbidden:
                if term in text:
                    offenders.append(f"{skill_name}/{path.relative_to(source)}:{term}")

    assert offenders == []
