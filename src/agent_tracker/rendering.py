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
            lines.extend(_render_prompt_path(config, task.prompt_path))
        return "\n".join(lines).rstrip() + "\n"


def _render_prompt_path(config: ProjectConfig, prompt_path: str) -> list[str]:
    """Return prompt_path content or a deterministic note when it cannot be read."""
    source = prompt_path.strip()
    lines = [f"Source: {source}", ""]
    requested_path = Path(source)
    if source.startswith("~") or requested_path.is_absolute():
        lines.append("[prompt_path not included: absolute or home-relative paths are not allowed]")
        return lines

    try:
        config_root = config.root.resolve()
        path = (config_root / requested_path).resolve(strict=False)
    except OSError:
        lines.append("[prompt_path not included: file could not be read]")
        return lines
    if not path.is_relative_to(config_root):
        lines.append("[prompt_path not included: path resolves outside the config directory]")
        return lines

    try:
        if not path.exists():
            lines.append("[prompt_path not included: file does not exist]")
            return lines
        if not path.is_file():
            lines.append("[prompt_path not included: path is not a file]")
            return lines
        lines.append(path.read_text(encoding="utf-8").rstrip())
    except OSError:
        lines.append("[prompt_path not included: file could not be read]")
    except UnicodeDecodeError:
        lines.append("[prompt_path not included: file is not valid UTF-8 text]")
    return lines
