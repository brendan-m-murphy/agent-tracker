"""Project configuration loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_CONFIG_SCHEMA_VERSION = 1

_TEXT_FIELDS = {
    "project_id",
    "name",
    "canonical_config_path",
    "state_root",
    "task_source_root",
    "db_path",
    "task_plan_path",
    "importer",
    "prompt_renderer",
    "event_adapter",
    "exporter",
    "export_path",
    "spool_inbox",
    "spool_done",
    "spool_error",
}
_SPOOL_PATH_FIELDS = {"inbox", "done", "error", "remote_inbox"}


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
    config_schema_version: int = SUPPORTED_CONFIG_SCHEMA_VERSION

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
    if not isinstance(data, dict):
        raise ValueError("project config must be a JSON object")
    config_schema_version = _config_schema_version(data)
    _validate_text_fields(data)
    _validate_spool(data)
    raw = dict(data)
    raw["config_schema_version"] = config_schema_version
    root = config_path.parent
    project_id = _required_text(data, "project_id")
    name = _optional_text(data, "name") or project_id
    state_root = _resolve(root, _optional_text(data, "state_root", ".") or ".").resolve()
    task_source_root = _resolve(
        root,
        _optional_text(data, "task_source_root", ".") or ".",
    ).resolve()
    db_path = _resolve(
        state_root,
        _optional_text(data, "db_path", ".agent-tracker/state.sqlite")
        or ".agent-tracker/state.sqlite",
    )
    canonical_config_path = None
    canonical_value = _optional_text(data, "canonical_config_path")
    if canonical_value:
        canonical_path = Path(canonical_value).expanduser()
        if not canonical_path.is_absolute():
            raise ValueError("canonical_config_path must be absolute or start with ~")
        canonical_config_path = canonical_path.resolve()
    return ProjectConfig(
        config_schema_version=config_schema_version,
        project_id=project_id,
        name=name,
        root=root,
        db_path=db_path,
        raw=raw,
        config_path=config_path,
        state_root=state_root,
        task_source_root=task_source_root,
        canonical_config_path=canonical_config_path,
    )


def _config_schema_version(data: dict[str, Any]) -> int:
    """Return the validated config schema version."""
    value = data.get("config_schema_version", SUPPORTED_CONFIG_SCHEMA_VERSION)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("config_schema_version must be an integer")
    if value != SUPPORTED_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            "unsupported config_schema_version "
            f"{value}; supported version is {SUPPORTED_CONFIG_SCHEMA_VERSION}"
        )
    return value


def _required_text(data: dict[str, Any], key: str) -> str:
    """Return a required non-empty string config field."""
    if key not in data:
        raise ValueError(f"config field {key!r} is required")
    text = _optional_text(data, key)
    if not text:
        raise ValueError(f"config field {key!r} must be non-empty")
    return text


def _optional_text(data: dict[str, Any], key: str, default: str = "") -> str:
    """Return a string config field, treating missing or null as a default."""
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"config field {key!r} must be a string")
    return value.strip()


def _validate_text_fields(data: dict[str, Any]) -> None:
    """Validate known string-valued top-level config fields."""
    for key in _TEXT_FIELDS:
        if key in data:
            _optional_text(data, key)


def _validate_spool(data: dict[str, Any]) -> None:
    """Validate the optional local spool config block."""
    if "spool" not in data or data["spool"] is None:
        return
    spool = data["spool"]
    if not isinstance(spool, dict):
        raise ValueError("config field 'spool' must be an object")
    for key in _SPOOL_PATH_FIELDS:
        if key not in spool or spool[key] is None:
            continue
        if not isinstance(spool[key], str):
            raise ValueError(f"config field 'spool.{key}' must be a string")
