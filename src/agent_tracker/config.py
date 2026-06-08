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
    config_path: Path | None = None
    state_root: Path | None = None
    task_source_root: Path | None = None
    canonical_config_path: Path | None = None

    def resolve_path(self, key: str, default: str = "") -> Path:
        """Resolve a path-valued config key relative to the config directory."""
        return self._resolve_from(self.root, key, default)

    def resolve_state_path(self, key: str, default: str = "") -> Path:
        """Resolve a runtime state path relative to the configured state root."""
        return self._resolve_from(self.effective_state_root, key, default)

    def resolve_task_source_path(self, key: str, default: str = "") -> Path:
        """Resolve a task-definition path relative to the task source root."""
        return self._resolve_from(self.effective_task_source_root, key, default)

    @property
    def effective_config_path(self) -> Path:
        """Return the resolved config path, falling back to the config directory."""
        return self.config_path or self.root

    @property
    def effective_state_root(self) -> Path:
        """Return the base directory for runtime state."""
        return self.state_root or self.root

    @property
    def effective_task_source_root(self) -> Path:
        """Return the base directory for task definition sources."""
        return self.task_source_root or self.root

    def mutation_refusal_reason(self, db_path: str | Path | None = None) -> str:
        """Return a non-empty reason when a mutating operation is not allowed."""
        if self.canonical_config_path is not None:
            current = self.effective_config_path.resolve()
            canonical = self.canonical_config_path.resolve()
            if current != canonical:
                return (
                    "mutating commands must use canonical config "
                    f"{canonical}; current config is {current}"
                )
            actual_db_path = Path(db_path or self.db_path).expanduser().resolve()
            configured_db_path = self.db_path.resolve()
            if actual_db_path != configured_db_path:
                return (
                    "mutating commands cannot override the configured canonical "
                    f"database {configured_db_path}; current database is {actual_db_path}"
                )
        return ""

    def path_summary(self, db_path: str | Path | None = None) -> dict[str, str]:
        """Return resolved paths useful for inspection and command reporting."""
        summary = {
            "config_path": str(self.effective_config_path),
            "state_root": str(self.effective_state_root),
            "task_source_root": str(self.effective_task_source_root),
            "db_path": str(Path(db_path or self.db_path).expanduser()),
        }
        if self.canonical_config_path is not None:
            summary["canonical_config_path"] = str(self.canonical_config_path)
        if "task_plan_path" in self.raw:
            summary["task_source_path"] = str(self.resolve_task_source_path("task_plan_path"))
        return summary

    def _resolve_from(self, root: Path, key: str, default: str = "") -> Path:
        value = str(self.raw.get(key, default)).strip()
        if not value:
            return root
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
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
    state_root = _resolve(root, str(data.get("state_root", "."))).resolve()
    task_source_root = _resolve(root, str(data.get("task_source_root", "."))).resolve()
    db_path = _resolve(state_root, str(data.get("db_path", ".agent-tracker/state.sqlite")))
    canonical_config_path = None
    canonical_value = str(data.get("canonical_config_path", "")).strip()
    if canonical_value:
        canonical_path = Path(canonical_value).expanduser()
        if not canonical_path.is_absolute():
            raise ValueError("canonical_config_path must be absolute or start with ~")
        canonical_config_path = canonical_path.resolve()
    return ProjectConfig(
        project_id=project_id,
        name=name,
        root=root,
        db_path=db_path,
        raw=data,
        config_path=config_path,
        state_root=state_root,
        task_source_root=task_source_root,
        canonical_config_path=canonical_config_path,
    )
