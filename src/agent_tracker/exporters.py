"""Built-in generic exporters."""

from __future__ import annotations

import json

from agent_tracker.config import ProjectConfig


class JsonSnapshotExporter:
    """Export a project snapshot as JSON."""

    def export(self, config: ProjectConfig, snapshot: dict) -> list[str]:
        """Write the configured export file."""
        path = config.resolve_path("export_path", "agent-tracker-snapshot.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        return [str(path)]

