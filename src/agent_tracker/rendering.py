"""Prompt and human CLI rendering."""

from __future__ import annotations

import shlex
import sys
import textwrap
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from agent_tracker.config import ProjectConfig
from agent_tracker.models import TaskState

HUMAN_OUTPUT_WIDTH = 80
OVERVIEW_ID_INDENT = 2
OVERVIEW_TITLE_INDENT = 4
OVERVIEW_DETAIL_INDENT = 6
OVERVIEW_DETAIL_LIMIT = 2
OVERVIEW_HEADING_STYLE = "bold green"
OVERVIEW_TASK_ID_STYLE = "cyan"
OVERVIEW_TITLE_STYLE = "bold"
OVERVIEW_LABEL_STYLE = "bold blue"
OVERVIEW_DETAIL_STYLE = "dim"
OVERVIEW_GROUPS = (
    ("ready", "Ready"),
    ("active", "Active"),
    ("review", "Review"),
    ("integration", "Integration"),
    ("blocked", "Blocked"),
    ("recently_completed", "Recently completed"),
)


class HumanOutputRenderer:
    """Render copy-paste-safe human CLI output."""

    def __init__(self, output: TextIO | None = None, *, width: int = HUMAN_OUTPUT_WIDTH) -> None:
        """Bind the renderer to an output stream and fixed terminal width."""
        self._output = output or sys.stdout
        self._width = width
        self._console = Console(file=self._output, width=width, highlight=False)

    def line(
        self,
        text: str,
        *,
        initial_indent: str = "",
        subsequent_indent: str = "",
        break_long_words: bool = False,
        style: str | None = None,
    ) -> None:
        """Print a wrapped human-oriented line with stable indentation."""
        self._console.print(
            Text(
                textwrap.fill(
                    text,
                    width=self._width,
                    initial_indent=initial_indent,
                    subsequent_indent=subsequent_indent or initial_indent,
                    break_long_words=break_long_words,
                    break_on_hyphens=False,
                ),
                style=style,
            )
        )

    def field(self, label: str, value: object, *, indent: int = 2) -> None:
        """Print a labeled human field with wrapped continuation lines."""
        prefix = f"{' ' * indent}{label}: "
        self.line(str(value), initial_indent=prefix, subsequent_indent=" " * len(prefix))

    def kv_row(self, label: str, value: object, *, label_width: int = 12) -> None:
        """Print one compact aligned key/value row."""
        prefix = f"  {label:<{label_width}} "
        self.line(str(value), initial_indent=prefix, subsequent_indent=" " * len(prefix))

    def kv_table(self, rows: list[tuple[str, object]], *, label_width: int = 12) -> None:
        """Print aligned key/value rows using Rich text without borders."""
        for label, value in rows:
            line = Text(f"  {label:<{label_width}} ")
            line.append(str(value))
            self._console.print(line)

    def section(self, heading: str) -> None:
        """Print a plain section heading without box-drawing decoration."""
        self._console.print(Text(heading, style="bold"))

    def raw_line(self, text: str) -> None:
        """Print an unwrapped line for legacy plain-print output."""
        print(text, file=self._output)

    def status(self, payload: dict[str, Any]) -> None:
        """Render a project status summary."""
        self._console.print(Text(f"{payload['name']} ({payload['project_id']})", style="bold"))
        self.section("Paths")
        path_rows: list[tuple[str, object]] = [
            ("config", payload["config_path"]),
            ("db", payload["db_path"]),
        ]
        if payload.get("task_source_path"):
            path_rows.append(("task source", payload["task_source_path"]))
        self.kv_table(path_rows)
        self.section("Queue")
        self.kv_table(
            [
                ("ready", len(payload["ready"])),
                ("active", len(payload["active"])),
                ("review", len(payload["review"])),
                ("integration", len(payload["integration"])),
                ("blocked", len(payload["blocked"])),
            ]
        )

    def overview(self, payload: dict[str, Any]) -> None:
        """Render a grouped project overview."""
        self._console.print(Text(f"{payload['name']} ({payload['project_id']})", style="bold"))
        for key, heading in OVERVIEW_GROUPS:
            self._console.print()
            items = payload["groups"][key]
            total = payload["counts"][key]
            self._console.print(Text(f"{heading} ({total})", style=OVERVIEW_HEADING_STYLE))
            if not items:
                self._console.print(Text("  (none)", style=OVERVIEW_DETAIL_STYLE))
                continue
            for index, item in enumerate(items):
                if index:
                    self._console.print()
                self.overview_item(key, item)
            if len(items) < total:
                hidden_count = total - len(items)
                self._console.print()
                self._console.print(
                    Text(
                        f"  ... {hidden_count} more; use --limit 0 to show all",
                        style=OVERVIEW_DETAIL_STYLE,
                    )
                )

    def overview_item(self, group: str, item: dict[str, Any]) -> None:
        """Render one overview item."""
        qualifiers = []
        if group in {"active", "review", "integration"}:
            qualifiers.append(str(item["state"]))
        if item.get("lease_agent_id"):
            qualifiers.append(f"agent {self._truncate(item['lease_agent_id'], 24)}")
        id_width = max(1, self._width - OVERVIEW_ID_INDENT)
        task_id = self._truncate(str(item["id"]), id_width)
        id_indent = " " * OVERVIEW_ID_INDENT
        title_indent = " " * OVERVIEW_TITLE_INDENT
        self.line(
            task_id,
            initial_indent=id_indent,
            subsequent_indent=id_indent,
            style=OVERVIEW_TASK_ID_STYLE,
        )
        self.line(
            self._compact_text(item["title"]),
            initial_indent=title_indent,
            subsequent_indent=title_indent,
            break_long_words=True,
            style=OVERVIEW_TITLE_STYLE,
        )
        if qualifiers:
            self._overview_detail_line("state", "; ".join(qualifiers))

        detail_widths = {
            "blocker": self._detail_width("blocker"),
            "evidence": self._detail_width("evidence"),
            "next": self._detail_width("next"),
            "completed": self._detail_width("completed"),
        }
        for label, value in self._overview_details(group, item, detail_widths):
            self._overview_detail_line(label, value)

    def _overview_details(
        self,
        group: str,
        item: dict[str, Any],
        detail_widths: dict[str, int],
    ) -> list[tuple[str, str]]:
        """Return compact overview detail lines for one item."""
        details: list[tuple[str, str]] = []
        blockers = [self._compact_text(value) for value in item.get("blockers", []) if value]
        if blockers:
            blocker = blockers[0]
            if len(blockers) > 1:
                blocker = self._truncate_with_suffix(
                    blocker,
                    detail_widths["blocker"],
                    f" (+{len(blockers) - 1} more)",
                )
            details.append(("blocker", blocker))
        if item.get("latest_evidence"):
            details.append(("evidence", self._compact_text(item["latest_evidence"])))
        if (
            group != "recently_completed"
            and item.get("next_action")
            and len(details) < OVERVIEW_DETAIL_LIMIT
        ):
            details.append(("next", self._compact_text(item["next_action"])))
        if group == "recently_completed" and not details and item.get("completed_at"):
            details.append(
                (
                    "completed",
                    self._compact_text(
                        f"{item['completed_at']} by {item.get('completed_by') or 'system'}"
                    ),
                )
            )
        return details[:OVERVIEW_DETAIL_LIMIT]

    def _compact_text(self, value: object) -> str:
        """Collapse internal whitespace for one-line summaries."""
        return " ".join(str(value).split())

    def _truncate(self, value: object, width: int) -> str:
        """Return a single-line value capped to a display width."""
        text = self._compact_text(value)
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return text[: width - 3].rstrip() + "..."

    def _truncate_with_suffix(self, value: object, width: int, suffix: str) -> str:
        """Truncate a value while preserving an important trailing marker."""
        text = self._compact_text(value)
        if len(text) + len(suffix) <= width:
            return f"{text}{suffix}"
        available = width - len(suffix)
        if available <= 0:
            return suffix[-width:]
        return f"{self._truncate(text, available)}{suffix}"

    def _detail_width(self, label: str) -> int:
        """Return available display width for one overview detail value."""
        return max(1, self._width - len(f"{' ' * OVERVIEW_DETAIL_INDENT}{label}: "))

    def _overview_detail_line(self, label: str, value: object) -> None:
        """Render one compact overview detail with a styled label."""
        prefix = f"{' ' * OVERVIEW_DETAIL_INDENT}{label}: "
        width = max(1, self._width - len(prefix))
        line = Text(prefix, style=OVERVIEW_LABEL_STYLE)
        line.append(self._truncate(value, width), style=OVERVIEW_DETAIL_STYLE)
        self._console.print(line)

    def next_tasks(self, states: list[TaskState]) -> None:
        """Render ready task summaries."""
        if not states:
            self._console.print(Text("No ready tasks."))
            return
        for state in states:
            task = state.task
            self.line(f"{task.task_id}: {task.title}", subsequent_indent="  ")
            if task.repo:
                self.field("repo", task.repo)
            if task.next_action:
                self.field("next", task.next_action)

    def completion_integrity(self, payload: dict[str, Any]) -> None:
        """Render completion integrity issues."""
        self.section(f"Completion integrity issues ({payload['issue_count']})")
        for issue in payload["issues"]:
            self.line(
                f"- {issue['task_id']}: {issue['reason']}",
                initial_indent="  ",
                subsequent_indent="      ",
            )
            if issue.get("completion_action"):
                self.field("completion", issue["completion_action"], indent=4)
            if issue.get("direct_merge"):
                self.field("direct_merge", "true", indent=4)
            if issue.get("evidence"):
                self.field("evidence", ", ".join(issue["evidence"]), indent=4)

    def workspaces(self, payload: dict[str, Any]) -> None:
        """Render configured worker workspaces."""
        if not payload["workspaces"]:
            self._console.print(Text("No workspaces configured."))
            return
        for workspace in payload["workspaces"]:
            self.line(
                f"{workspace['name']}: {workspace['kind']} {workspace['path']}",
                subsequent_indent="  ",
            )
            if workspace.get("config_path"):
                self.field("config", workspace["config_path"])
            if workspace.get("spool_outbox"):
                self.field("spool", workspace["spool_outbox"])
            if workspace.get("capabilities"):
                self.field("capabilities", ", ".join(workspace["capabilities"]))

    def worker_launch(self, result: dict[str, Any]) -> None:
        """Render a worker launch summary."""
        self.section(f"Worker launch {result['launch_id']}")
        self.kv_table(
            [
                ("status", result["status"]),
                ("workspace", result["workspace"]["name"]),
                ("task", result.get("task_id") or "(prompt only)"),
                ("prompt", result["artifacts"]["prompt"]),
                ("report", result["artifacts"]["report"]),
                ("launch", result["artifacts"]["launch"]),
            ],
            label_width=9,
        )
        if result.get("command"):
            self.kv_table([("command", shlex.join(result["command"]))], label_width=9)
        if "returncode" in result:
            self.kv_table([("return", result["returncode"])], label_width=9)

    def intake(self, payload: dict[str, Any]) -> None:
        """Render raw intake records."""
        if not payload["intake"]:
            self._console.print(Text("No intake records."))
            return
        for item in payload["intake"]:
            rows: list[tuple[str, object]] = [
                ("status", item["status"]),
                ("kind", item["kind"]),
            ]
            if item["repo"]:
                rows.append(("repo", item["repo"]))
            if item["source"]:
                rows.append(("source", item["source"]))
            self.line(f"{item['id']}: {item['text']}", subsequent_indent="  ")
            self.kv_table(rows, label_width=8)
            if item["tags"]:
                self.kv_table([("tags", ", ".join(item["tags"]))], label_width=8)
            if item.get("created_at"):
                self.kv_table([("created", item["created_at"])], label_width=8)

    def proposals(self, payload: dict[str, Any]) -> None:
        """Render proposed task records."""
        if not payload["proposals"]:
            self.raw_line("No proposed tasks.")
            return
        for proposal in payload["proposals"]:
            task = proposal["task"]
            self.raw_line(f"{proposal['id']}: {task['id']} - {task['title']}")
            self.raw_line(f"  intake: {proposal['intake_id']}; status: {proposal['status']}")
            if task.get("repo"):
                self.raw_line(f"  repo: {task['repo']}")
            if task.get("next_action"):
                self.raw_line(f"  next: {task['next_action']}")


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
