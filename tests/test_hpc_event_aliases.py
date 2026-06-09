"""Regression tests for plugin-free HPC callback event aliases."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_tracker import service as service_module  # noqa: E402
from agent_tracker.config import load_config  # noqa: E402
from agent_tracker.models import EventRecord  # noqa: E402


def write_project(root: Path) -> Path:
    """Write a minimal project config for event ingestion."""
    config_path = root / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "project_id": "toy",
                "name": "Toy Project",
                "db_path": "state.sqlite",
            }
        ),
        encoding="utf-8",
    )
    return config_path


def coordinator(root: Path) -> service_module.Coordinator:
    """Return an initialized coordinator for the minimal test project."""
    coord = service_module.Coordinator(load_config(write_project(root)))
    coord.init()
    return coord


def only_event(coord: service_module.Coordinator) -> dict[str, object]:
    """Return the single event stored by a test."""
    snapshot = coord.store.snapshot(coord.config.project_id)
    assert len(snapshot["events"]) == 1
    return snapshot["events"][0]


@pytest.mark.parametrize(
    ("payload", "expected_event_id", "expected_kind"),
    [
        (
            {
                "run_id": "run-123",
                "event_type": "hpc.callback",
                "task_id": "hpc-ci-task",
                "status": "complete",
            },
            "run-123",
            "hpc.callback",
        ),
        (
            {
                "job_id": 123,
                "event_type": "hpc.job",
                "status": "started",
            },
            "123",
            "hpc.job",
        ),
        (
            {
                "id": "id-explicit",
                "run_id": "run-ignored",
                "job_id": "job-ignored",
                "event_type": "hpc.run",
            },
            "id-explicit",
            "hpc.run",
        ),
        (
            {
                "event_id": "event-explicit",
                "id": "id-ignored",
                "run_id": "run-ignored",
                "job_id": "job-ignored",
                "kind": "explicit.kind",
                "event_type": "hpc.event",
            },
            "event-explicit",
            "explicit.kind",
        ),
    ],
)
def test_default_event_normalization_accepts_hpc_aliases_and_preserves_payload(
    tmp_path: Path,
    payload: dict[str, object],
    expected_event_id: str,
    expected_kind: str,
) -> None:
    """Built-in event normalization accepts common HPC callback aliases."""
    coord = coordinator(tmp_path)

    inserted = coord.record_event(payload, actor="spool")
    event = only_event(coord)

    assert inserted is True
    assert event["event_id"] == expected_event_id
    assert event["kind"] == expected_kind
    assert event["payload"] == payload


def test_default_event_normalization_mentions_accepted_id_aliases(tmp_path: Path) -> None:
    """The default missing-ID error documents all accepted ID aliases."""
    coord = coordinator(tmp_path)

    with pytest.raises(ValueError, match="event_id, id, run_id, or job_id"):
        coord.record_event({"event_type": "hpc.callback"})


def test_event_adapter_takes_precedence_over_default_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured event adapters own normalization even when alias fields exist."""

    class Adapter:
        def normalize_event(
            self,
            config: object,
            payload: dict[str, object],
        ) -> EventRecord:
            return EventRecord(
                event_id="plugin-event",
                kind="plugin.kind",
                task_id="plugin-task",
                payload={"normalized_by": "plugin", "source_run_id": payload["run_id"]},
            )

    def fake_load_plugin(config: object, name: str) -> Adapter | None:
        return Adapter() if name == "event_adapter" else None

    monkeypatch.setattr(service_module, "load_plugin", fake_load_plugin)
    coord = coordinator(tmp_path)

    inserted = coord.record_event(
        {"run_id": "run-default", "event_type": "hpc.default"},
        actor="plugin",
    )
    event = only_event(coord)

    assert inserted is True
    assert event["event_id"] == "plugin-event"
    assert event["kind"] == "plugin.kind"
    assert event["task_id"] == "plugin-task"
    assert event["payload"] == {
        "normalized_by": "plugin",
        "source_run_id": "run-default",
    }
