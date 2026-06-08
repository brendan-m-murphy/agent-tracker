"""Regression tests for the generic coordinator."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_tracker.config import load_config  # noqa: E402
from agent_tracker.mcp_tools import AgentTrackerTools  # noqa: E402
from agent_tracker.service import Coordinator  # noqa: E402


def write_project(tmpdir: str) -> Path:
    """Write a toy non-project-specific config and task plan."""
    root = Path(tmpdir)
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


class AgentTrackerTests(unittest.TestCase):
    """Cover generic queue behavior."""

    def test_import_evaluates_ready_blocked_and_deferred_tasks(self) -> None:
        """Imported task dependencies determine computed state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(write_project(tmpdir))
            coord = Coordinator(config)
            coord.import_tasks()

            states = {state.task.task_id: state for state in coord.task_states()}

        self.assertEqual(states["foundation"].state, "done")
        self.assertEqual(states["ready"].state, "ready")
        self.assertEqual(states["blocked"].state, "blocked")
        self.assertEqual(states["deferred"].state, "deferred")

    def test_claim_heartbeat_complete_and_unblock_downstream(self) -> None:
        """Claimed work can be heartbeated, completed, and unblock dependents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(write_project(tmpdir))
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

        self.assertEqual(claim.task_id, "ready")
        self.assertEqual(states["ready"].state, "done")
        self.assertEqual(states["ready"].evidence, ["evidence://ready"])
        self.assertEqual(states["blocked"].state, "ready")

    def test_stale_claim_is_recovered(self) -> None:
        """Expired task leases are returned to pending and computed as ready."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(write_project(tmpdir))
            coord = Coordinator(config)
            coord.import_tasks()

            coord.claim(agent_id="agent-1", task_id="ready", lease_seconds=-1)
            recovered = coord.get_task("ready")

        self.assertEqual(recovered.state, "ready")
        self.assertEqual(recovered.lease_token, "")

    def test_event_ingestion_is_idempotent_and_spool_moves_files(self) -> None:
        """Event IDs are unique and spool files move to done or error paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = write_project(tmpdir)
            config = load_config(config_path)
            coord = Coordinator(config)
            coord.import_tasks()
            inbox = Path(tmpdir) / "spool" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "event.json").write_text(
                json.dumps({"event_id": "evt-1", "kind": "sample"}),
                encoding="utf-8",
            )
            result = coord.ingest_spool()
            duplicate = coord.record_event({"event_id": "evt-1", "kind": "sample"})
            moved = (Path(tmpdir) / "spool" / "done" / "event.json").exists()

        self.assertEqual(result, {"processed": 1, "inserted": 1, "errors": 0})
        self.assertFalse(duplicate)
        self.assertTrue(moved)

    def test_mcp_handlers_claim_and_complete(self) -> None:
        """MCP-friendly handlers expose the same queue operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = write_project(tmpdir)
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
        self.assertEqual(ready_state["state"], "done")
        self.assertEqual(ready_state["evidence"], ["evidence://ready"])

    def test_export_writes_snapshot(self) -> None:
        """Configured exporters can write audit snapshots."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(write_project(tmpdir))
            coord = Coordinator(config)
            coord.import_tasks()
            paths = coord.export()
            snapshot = json.loads(Path(paths[0]).read_text(encoding="utf-8"))

        self.assertEqual(len(paths), 1)
        self.assertEqual(snapshot["project_id"], "toy")
        self.assertTrue(snapshot["tasks"])

    def test_core_contains_no_project_specific_terms(self) -> None:
        """The reusable package core must stay project-agnostic."""
        forbidden = ["hpc", "slurm", "test_inversions", "acrg"]
        offenders = []
        for path in (ROOT / "src" / "agent_tracker").glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for term in forbidden:
                if term in text:
                    offenders.append(f"{path.name}:{term}")

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
