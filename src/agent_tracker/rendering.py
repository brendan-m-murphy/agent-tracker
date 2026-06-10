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

OVERVIEW_HANDLE_WIDTH = 24
OVERVIEW_STATUS_WIDTH = 8
OVERVIEW_HEADER_STYLE = "bold"
OVERVIEW_MUTED_STYLE = "dim"
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

    def __init__(self, output: TextIO | None = None, *, width: int | None = None) -> None:
        """Bind the renderer to an output stream."""
        self._output = output or sys.stdout
        console_options: dict[str, Any] = {"file": self._output, "highlight": False}
        if width is not None:
            console_options["width"] = width
        self._console = Console(**console_options)

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
                    width=self._terminal_width,
                    initial_indent=initial_indent,
                    subsequent_indent=subsequent_indent or initial_indent,
                    break_long_words=break_long_words,
                    break_on_hyphens=False,
                ),
                style=style or "",
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
        self.line(self._overview_summary(payload), style=OVERVIEW_MUTED_STYLE)
        self._console.print()
        self._overview_attention(payload)
        self._overview_blocked(payload)
        self._overview_ready(payload)
        self._overview_recent(payload)

    def overview_item(self, group: str, item: dict[str, Any]) -> None:
        """Render one overview item."""
        if group == "blocked":
            self._overview_blocked_row(item)
        elif group == "recently_completed":
            self._overview_recent_row(item)
        elif group == "ready":
            self._overview_ready_row(item)
        else:
            self._overview_attention_row(group, item)

    def _overview_summary(self, payload: dict[str, Any]) -> str:
        """Return the one-line overview count summary."""
        counts = payload["counts"]
        open_count = sum(
            counts[key] for key in ("ready", "active", "review", "integration", "blocked")
        )
        return (
            f"Open {open_count} | Ready {counts['ready']} | Active {counts['active']} | "
            f"Review {counts['review']} | Merge {counts['integration']} | "
            f"Blocked {counts['blocked']} | Done {counts['recently_completed']}"
        )

    def _overview_attention(self, payload: dict[str, Any]) -> None:
        """Render active, review, and integration work as one attention list."""
        groups = payload["groups"]
        counts = payload["counts"]
        keys = ("active", "review", "integration")
        items = [(key, item) for key in keys for item in groups[key]]
        total = sum(counts[key] for key in keys)
        if not items and total == 0:
            return
        self._console.print(Text("ATTENTION", style=OVERVIEW_HEADER_STYLE))
        if not items:
            self._console.print(Text("  (none)", style=OVERVIEW_MUTED_STYLE))
        for group, item in items:
            self._overview_attention_row(group, item)
        self._overview_hidden_count(total - len(items), "attention items")
        self._console.print()

    def _overview_blocked(self, payload: dict[str, Any]) -> None:
        """Render blocked work with its blocker because it cannot be acted on directly."""
        items = payload["groups"]["blocked"]
        total = payload["counts"]["blocked"]
        if not items and total == 0:
            return
        self._console.print(Text(f"BLOCKED ({total})", style=OVERVIEW_HEADER_STYLE))
        if not items:
            self._console.print(Text("  (none)", style=OVERVIEW_MUTED_STYLE))
        for item in items:
            self._overview_blocked_row(item)
        self._overview_hidden_count(total - len(items), "blocked tasks")
        self._console.print()

    def _overview_ready(self, payload: dict[str, Any]) -> None:
        """Render ready work as a title-first task index."""
        items = payload["groups"]["ready"]
        total = payload["counts"]["ready"]
        self._console.print(Text(f"READY ({total})", style=OVERVIEW_HEADER_STYLE))
        if not items:
            self._console.print(Text("  (none)", style=OVERVIEW_MUTED_STYLE))
        for item in items:
            self._overview_ready_row(item)
        self._overview_hidden_count(total - len(items), "ready tasks")
        self._console.print()

    def _overview_recent(self, payload: dict[str, Any]) -> None:
        """Render recent completions as a short history tail."""
        items = payload["groups"]["recently_completed"]
        total = payload["counts"]["recently_completed"]
        self._console.print(Text(f"RECENT ({total})", style=OVERVIEW_HEADER_STYLE))
        if not items:
            self._console.print(Text("  (none)", style=OVERVIEW_MUTED_STYLE))
        for item in items:
            self._overview_recent_row(item)
        self._overview_hidden_count(total - len(items), "completed tasks")

    def _overview_attention_row(self, group: str, item: dict[str, Any]) -> None:
        """Render one attention row."""
        status = self._overview_status(group)
        handle = self._overview_handle(item)
        prefix = f"  {status:<{OVERVIEW_STATUS_WIDTH}} {handle:<{self._handle_width}} "
        self.line(
            self._compact_text(item["title"]),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=True,
        )

    def _overview_blocked_row(self, item: dict[str, Any]) -> None:
        """Render one blocked row with the concise blocker summary."""
        prefix = f"  {self._overview_handle(item):<{self._handle_width}} "
        self.line(
            self._compact_text(item["title"]),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=True,
        )
        blocker = self._overview_blocker(item)
        if blocker:
            self.field("blocker", blocker, indent=4)

    def _overview_ready_row(self, item: dict[str, Any]) -> None:
        """Render one ready row."""
        prefix = f"  {self._overview_handle(item):<{self._handle_width}} "
        self.line(
            self._compact_text(item["title"]),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=True,
        )

    def _overview_recent_row(self, item: dict[str, Any]) -> None:
        """Render one recent completion row."""
        completed = self._compact_completed_time(item.get("completed_at") or "")
        prefix = f"  {completed:<5} "
        self.line(
            self._compact_text(item["title"]),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=True,
            style=OVERVIEW_MUTED_STYLE,
        )

    def _overview_hidden_count(self, hidden_count: int, noun: str) -> None:
        """Render a hidden-count hint for a section."""
        if hidden_count <= 0:
            return
        self._console.print(
            Text(
                f"  ... {hidden_count} more {noun}; use --limit 0 to show all",
                style=OVERVIEW_MUTED_STYLE,
            )
        )

    def _overview_status(self, group: str) -> str:
        """Return a short status label for an overview group."""
        return {"active": "ACTIVE", "review": "REVIEW", "integration": "MERGE"}.get(
            group, group.upper()
        )

    def _overview_handle(self, item: dict[str, Any]) -> str:
        """Return a compact command-oriented task handle."""
        return self._truncate(item["id"], self._handle_width)

    def _overview_blocker(self, item: dict[str, Any]) -> str:
        """Return the concise blocker summary for a blocked task."""
        blockers = [self._compact_text(value) for value in item.get("blockers", []) if value]
        if not blockers:
            return ""
        if len(blockers) == 1:
            return blockers[0]
        return f"{blockers[0]} (+{len(blockers) - 1} more)"

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

    def _compact_completed_time(self, value: object) -> str:
        """Return a compact completed time for the recent-completion tail."""
        text = self._compact_text(value)
        if not text:
            return ""
        text = text.replace("T", " ")
        if len(text) >= 16 and text[10] == " ":
            return text[11:16]
        return self._truncate(text, 5)

    @property
    def _terminal_width(self) -> int:
        """Return the width Rich detected for this output stream."""
        return max(20, self._console.size.width)

    @property
    def _handle_width(self) -> int:
        """Return a width-aware cap for overview task handles."""
        return min(OVERVIEW_HANDLE_WIDTH, max(12, self._terminal_width // 4))

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
        assignment = result.get("coordination", {}).get("assignment", {})
        rows: list[tuple[str, object]] = [
            ("status", result["status"]),
            ("workspace", result["workspace"]["name"]),
            ("task", result.get("task_id") or "(prompt only)"),
        ]
        if assignment:
            rows.extend(
                [
                    ("branch", assignment.get("branch") or "(none)"),
                    ("worktree", assignment.get("worktree_path") or "(none)"),
                ]
            )
        rows.extend(
            [
                ("prompt", result["artifacts"]["prompt"]),
                ("report", result["artifacts"]["report"]),
                ("launch", result["artifacts"]["launch"]),
            ]
        )
        self.kv_table(
            rows,
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
