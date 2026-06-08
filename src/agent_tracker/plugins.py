"""Plugin protocols and loading helpers."""

from __future__ import annotations

import importlib
import sys
from typing import Any, Protocol

from agent_tracker.config import ProjectConfig
from agent_tracker.models import DependencyRecord, EventRecord, TaskRecord, TaskState


class TaskImporter(Protocol):
    """Import project-specific task data into generic records."""

    def load_tasks(self, config: ProjectConfig) -> tuple[list[TaskRecord], list[DependencyRecord]]:
        """Return task and dependency records."""


class PromptRenderer(Protocol):
    """Render task prompts for agents."""

    def render_prompt(
        self, config: ProjectConfig, state: TaskState, *, markdown: bool = False
    ) -> str:
        """Render prompt text for a task."""


class EventAdapter(Protocol):
    """Normalize incoming project events."""

    def normalize_event(self, config: ProjectConfig, payload: dict[str, Any]) -> EventRecord:
        """Return a generic event record."""


class FollowupPlanner(Protocol):
    """Propose follow-up tasks from events or completed work."""

    def propose_followups(self, config: ProjectConfig, event: EventRecord) -> list[TaskRecord]:
        """Return deterministic follow-up task proposals."""


class Exporter(Protocol):
    """Export audit snapshots to a project-specific location."""

    def export(self, config: ProjectConfig, snapshot: dict[str, Any]) -> list[str]:
        """Write an export and return created or updated paths."""


def load_object(spec: str) -> Any:
    """Load `module:object` from a plugin specification string."""
    if ":" not in spec:
        raise ValueError(f"plugin spec must be 'module:object', got {spec!r}")
    module_name, object_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in object_name.split("."):
        obj = getattr(obj, part)
    return obj


def load_plugin(config: ProjectConfig, key: str, default: str | None = None) -> Any | None:
    """Instantiate the plugin configured under `key`."""
    spec = str(config.raw.get(key, default or "")).strip()
    if not spec:
        return None
    root = str(config.root)
    if root not in sys.path:
        sys.path.insert(0, root)
    obj = load_object(spec)
    return obj() if isinstance(obj, type) else obj
