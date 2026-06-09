"""Regression tests for default prompt_path rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_tracker.config import ProjectConfig, load_config  # noqa: E402
from agent_tracker.models import RequirementState, TaskRecord, TaskState  # noqa: E402
from agent_tracker.rendering import DefaultPromptRenderer  # noqa: E402


def _write_config(root: Path) -> ProjectConfig:
    """Write and load a minimal project config rooted at `root`."""
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "project.json"
    config_path.write_text(
        json.dumps(
            {
                "project_id": "demo",
                "name": "Demo Project",
                "db_path": "state.sqlite",
            }
        ),
        encoding="utf-8",
    )
    return load_config(config_path)


def _state(prompt_path: str) -> TaskState:
    """Build a task state with every default renderer section populated."""
    return TaskState(
        task=TaskRecord(
            task_id="render-task",
            title="Render Task",
            repo="agent-tracker",
            prompt_path=prompt_path,
            summary="Use the summary.",
            execution={"primary_files": ["src/agent_tracker/rendering.py"]},
            validation_checks=["uv run pytest tests/test_prompt_path_rendering.py"],
            next_action="Implement it.",
        ),
        state="ready",
        requirements=[RequirementState("Foundation complete.", True, "done")],
    )


def _render(config: ProjectConfig, prompt_path: str) -> str:
    """Render a task prompt through the default renderer."""
    return DefaultPromptRenderer().render_prompt(config, _state(prompt_path), markdown=True)


def test_default_renderer_includes_config_relative_prompt_path(tmp_path: Path) -> None:
    """Readable config-relative prompt_path files are included in the prompt."""
    config = _write_config(tmp_path)
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "handoff.md").write_text(
        "Concrete handoff prompt.\n\nCloseout instructions.",
        encoding="utf-8",
    )

    prompt = _render(config, "prompts/handoff.md")

    assert "# Render Task" in prompt
    assert "## Summary\nUse the summary." in prompt
    assert "- primary_files: ['src/agent_tracker/rendering.py']" in prompt
    assert "- [OK] Foundation complete. (done)" in prompt
    assert "- uv run pytest tests/test_prompt_path_rendering.py" in prompt
    assert "## Next Action\nImplement it." in prompt
    assert (
        "## Prompt Path\n"
        "Source: prompts/handoff.md\n\n"
        "Concrete handoff prompt.\n\n"
        "Closeout instructions."
    ) in prompt


def test_default_renderer_notes_missing_prompt_path(tmp_path: Path) -> None:
    """Missing prompt_path files render a stable note instead of raising."""
    config = _write_config(tmp_path)

    prompt = _render(config, "prompts/missing.md")

    assert "## Prompt Path\nSource: prompts/missing.md" in prompt
    assert "[prompt_path not included: file does not exist]" in prompt


def test_default_renderer_does_not_read_directory_prompt_path(tmp_path: Path) -> None:
    """Directory prompt_path values are reported and not read as files."""
    config = _write_config(tmp_path)
    (tmp_path / "prompts").mkdir()

    prompt = _render(config, "prompts")

    assert "## Prompt Path\nSource: prompts" in prompt
    assert "[prompt_path not included: path is not a file]" in prompt


def test_default_renderer_rejects_prompt_path_parent_traversal(tmp_path: Path) -> None:
    """Parent traversal cannot include files outside the config directory."""
    project_root = tmp_path / "project"
    config = _write_config(project_root)
    (tmp_path / "secret.md").write_text("outside secret", encoding="utf-8")

    prompt = _render(config, "../secret.md")

    assert "outside secret" not in prompt
    assert "## Prompt Path\nSource: ../secret.md" in prompt
    assert "[prompt_path not included: path resolves outside the config directory]" in prompt


def test_default_renderer_rejects_absolute_prompt_path(tmp_path: Path) -> None:
    """Absolute prompt_path values are not read, even when they point into the project."""
    config = _write_config(tmp_path)
    path = tmp_path / "handoff.md"
    path.write_text("absolute handoff", encoding="utf-8")

    prompt = _render(config, str(path))

    assert "absolute handoff" not in prompt
    assert "[prompt_path not included: absolute or home-relative paths are not allowed]" in prompt


def test_default_renderer_rejects_home_relative_prompt_path(tmp_path: Path) -> None:
    """Home-relative prompt_path values render a stable note."""
    config = _write_config(tmp_path)

    prompt = _render(config, "~/handoff.md")

    assert "## Prompt Path\nSource: ~/handoff.md" in prompt
    assert "[prompt_path not included: absolute or home-relative paths are not allowed]" in prompt


def test_default_renderer_notes_non_utf8_prompt_path(tmp_path: Path) -> None:
    """Non-UTF-8 prompt_path files render a stable unreadable-content note."""
    config = _write_config(tmp_path)
    (tmp_path / "handoff.md").write_bytes(b"\xff")

    prompt = _render(config, "handoff.md")

    assert "## Prompt Path\nSource: handoff.md" in prompt
    assert "[prompt_path not included: file is not valid UTF-8 text]" in prompt


def test_default_renderer_notes_unreadable_prompt_path_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission errors while probing prompt_path render a stable note."""
    config = _write_config(tmp_path)
    original_exists = Path.exists

    def raise_for_unreadable(path: Path) -> bool:
        if path.name == "unreadable.md":
            raise PermissionError("not searchable")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", raise_for_unreadable)

    prompt = _render(config, "unreadable.md")

    assert "## Prompt Path\nSource: unreadable.md" in prompt
    assert "[prompt_path not included: file could not be read]" in prompt
