"""Default prompt rendering."""

from __future__ import annotations

from pathlib import Path

from agent_tracker.config import ProjectConfig
from agent_tracker.models import TaskState


class DefaultPromptRenderer:
    """Render a generic task prompt from stored task context."""

    def render_prompt(
        self, config: ProjectConfig, state: TaskState, *, markdown: bool = False
    ) -> str:
        """Render a compact handoff prompt."""
        task = state.task
        lines = [
            f"# {task.title}",
            "",
            f"Project: {config.name}",
            f"Task: {task.task_id}",
            f"State: {state.state}",
        ]
        if task.repo:
            lines.append(f"Repo: {task.repo}")
        if task.summary:
            lines.extend(["", "## Summary", task.summary])
        if task.execution:
            lines.extend(["", "## Execution"])
            for key, value in task.execution.items():
                lines.append(f"- {key}: {value}")
        if state.requirements:
            lines.extend(["", "## Requirements"])
            for requirement in state.requirements:
                marker = "OK" if requirement.satisfied else "BLOCKED"
                lines.append(f"- [{marker}] {requirement.description} ({requirement.detail})")
        if task.validation_checks:
            lines.extend(["", "## Validation"])
            lines.extend(f"- {check}" for check in task.validation_checks)
        if task.next_action:
            lines.extend(["", "## Next Action", task.next_action])
        if task.prompt_path:
            lines.extend(["", "## Prompt Path"])
            lines.extend(_render_text_include(config, task.prompt_path, label="prompt_path"))
        notebook_paths = _metadata_notebook_paths(task.metadata)
        if notebook_paths:
            lines.extend(["", "## Notebooks"])
            for notebook_path in notebook_paths:
                lines.extend(_render_text_include(config, notebook_path, label="notebook"))
        return "\n".join(lines).rstrip() + "\n"


def _metadata_notebook_paths(metadata: dict) -> list[str]:
    """Return opt-in notebook paths from task metadata."""
    value = metadata.get("notebook_paths")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _render_text_include(config: ProjectConfig, source_path: str, *, label: str) -> list[str]:
    """Return config-relative text content or a deterministic unreadable note."""
    source = source_path.strip()
    lines = [f"Source: {source}", ""]
    requested_path = Path(source)
    if source.startswith("~") or requested_path.is_absolute():
        lines.append(f"[{label} not included: absolute or home-relative paths are not allowed]")
        return lines

    try:
        config_root = config.root.resolve()
        path = (config_root / requested_path).resolve(strict=False)
    except OSError:
        lines.append(f"[{label} not included: file could not be read]")
        return lines
    if not path.is_relative_to(config_root):
        lines.append(f"[{label} not included: path resolves outside the config directory]")
        return lines

    try:
        if not path.exists():
            lines.append(f"[{label} not included: file does not exist]")
            return lines
        if not path.is_file():
            lines.append(f"[{label} not included: path is not a file]")
            return lines
        lines.append(path.read_text(encoding="utf-8").rstrip())
    except OSError:
        lines.append(f"[{label} not included: file could not be read]")
    except UnicodeDecodeError:
        lines.append(f"[{label} not included: file is not valid UTF-8 text]")
    return lines
