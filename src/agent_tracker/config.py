"""Project configuration loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectConfig:
    """Generic project configuration."""

    project_id: str
    name: str
    root: Path
    db_path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    def resolve_path(self, key: str, default: str = "") -> Path:
        """Resolve a path-valued config key relative to the config directory."""
        value = str(self.raw.get(key, default)).strip()
        if not value:
            return self.root
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def load_config(path: str | Path) -> ProjectConfig:
    """Load a JSON project config."""
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent
    project_id = str(data["project_id"])
    name = str(data.get("name") or project_id)
    db_path = _resolve(root, str(data.get("db_path", ".agent-tracker/state.sqlite")))
    return ProjectConfig(project_id=project_id, name=name, root=root, db_path=db_path, raw=data)

