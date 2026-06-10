"""Prompt and human CLI rendering."""

from __future__ import annotations

import json
import os
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
OVERVIEW_HEADER_STYLE = "bold cyan"
OVERVIEW_MUTED_STYLE = "dim"
OVERVIEW_ATTENTION_STYLE = "yellow"
OVERVIEW_BLOCKED_STYLE = "bold red"
OVERVIEW_READY_STYLE = "green"
OVERVIEW_RECENT_STYLE = "dim"
STATUS_STYLES = {
    "active": "yellow",
    "awaiting_integration": "cyan",
    "awaiting_merge": "cyan",
    "awaiting_pr": "cyan",
    "awaiting_review": "magenta",
    "blocked": "bold red",
    "claimed": "yellow",
    "closed": "dim",
    "deferred": "dim",
    "done": "green",
    "failed": "bold red",
    "in_progress": "yellow",
    "integration": "cyan",
    "open": "green",
    "pending": "green",
    "promoted": "green",
    "proposed": "magenta",
    "ready": "green",
    "rejected": "red",
    "review": "magenta",
    "triaged": "cyan",
    "waiting_evidence": "yellow",
}
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

    def __init__(
        self,
        output: TextIO | None = None,
        *,
        width: int | None = None,
        color: bool | None = None,
    ) -> None:
        """Bind the renderer to an output stream.

        Args:
            output: Destination stream. Defaults to stdout.
            width: Optional deterministic terminal width for wrapping.
            color: Optional colour policy. ``True`` enables colour for human
                output, ``False`` disables it, and ``None`` enables colour only
                for interactive terminals while respecting ``NO_COLOR``.
        """
        self._output = output or sys.stdout
        console_options: dict[str, Any] = {"file": self._output, "highlight": False}
        if width is not None:
            console_options["width"] = width
        no_color_requested = color is False or "NO_COLOR" in os.environ
        default_non_tty = color is None and not self._output_is_tty()
        self._styles_enabled = not (no_color_requested or default_non_tty)
        if color is True:
            console_options["force_terminal"] = True
            console_options["color_system"] = "standard"
        if no_color_requested or default_non_tty:
            console_options["no_color"] = True
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
                style=self._style(style),
            )
        )

    def field(self, label: str, value: object, *, indent: int = 2) -> None:
        """Print a labeled human field with wrapped continuation lines."""
        prefix = f"{' ' * indent}{label}: "
        self.line(str(value), initial_indent=prefix, subsequent_indent=" " * len(prefix))

    def kv_row(
        self,
        label: str,
        value: object,
        *,
        label_width: int = 12,
        break_long_words: bool = False,
    ) -> None:
        """Print one compact aligned key/value row."""
        prefix = f"  {label:<{label_width}} "
        self.line(
            str(value),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=break_long_words,
            style=self._style(self._value_style(label, value)),
        )

    def kv_table(self, rows: list[tuple[str, object]], *, label_width: int = 12) -> None:
        """Print aligned key/value rows using Rich text without borders."""
        for label, value in rows:
            line = Text(f"  {label:<{label_width}} ", style=self._style(OVERVIEW_MUTED_STYLE))
            line.append(str(value), style=self._style(self._value_style(label, value)))
            self._console.print(line)

    def section(self, heading: str) -> None:
        """Print a plain section heading without box-drawing decoration."""
        self._console.print(Text(heading, style=self._style(OVERVIEW_HEADER_STYLE)))

    def raw_line(self, text: str) -> None:
        """Print an unwrapped line for legacy plain-print output."""
        print(text, file=self._output)

    def status(self, payload: dict[str, Any]) -> None:
        """Render a project status summary."""
        self._console.print(
            Text(f"{payload['name']} ({payload['project_id']})", style=self._style("bold"))
        )
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
        self._console.print(
            Text(f"{payload['name']} ({payload['project_id']})", style=self._style("bold"))
        )
        self.line(self._overview_summary(payload), style=OVERVIEW_MUTED_STYLE)
        self._console.print()
        self._overview_attention(payload)
        self._overview_blocked(payload)
        self._overview_ready(payload)
        self._overview_recent(payload)

    def overview_plain(self, payload: dict[str, Any]) -> None:
        """Render a grep-friendly overview without Rich-shaped output.

        Args:
            payload: Overview payload produced by `Coordinator.overview_payload`.

        Side Effects:
            Writes deterministic line-oriented text to the renderer stream. Long
            values are not wrapped or truncated, so the output remains suitable
            for grep, awk, and copy/paste into scripts.
        """
        self.raw_line(f"{payload['name']} ({payload['project_id']})")
        counts = payload["counts"]
        open_count = sum(
            counts[key] for key in ("ready", "active", "review", "integration", "blocked")
        )
        self.raw_line(
            "counts "
            f"open={open_count} ready={counts['ready']} active={counts['active']} "
            f"review={counts['review']} integration={counts['integration']} "
            f"blocked={counts['blocked']} recent={counts['recently_completed']}"
        )
        for group, heading in OVERVIEW_GROUPS:
            self.raw_line(f"{heading.lower().replace(' ', '_')} count={counts[group]}")
            for item in payload["groups"][group]:
                self._overview_plain_row(group, item)

    def overview_wide(self, payload: dict[str, Any]) -> None:
        """Render a width-aware overview with targeted context columns.

        Args:
            payload: Overview payload produced by `Coordinator.overview_payload`.

        Side Effects:
            Writes wrapped human text to the renderer stream using the detected
            terminal width. The method intentionally keeps one task per logical
            row and includes only the extra context useful for wide terminals.
        """
        self._console.print(
            Text(f"{payload['name']} ({payload['project_id']})", style=self._style("bold"))
        )
        self.line(self._overview_summary(payload), style=OVERVIEW_MUTED_STYLE)
        self._console.print()
        self._overview_attention(payload, wide=True)
        self._overview_blocked(payload, wide=True)
        self._overview_ready(payload, wide=True)
        self._overview_recent(payload, wide=True)

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

    def task_detail(self, payload: dict[str, Any]) -> None:
        """Render full human detail for one task.

        Args:
            payload: Task detail dictionary produced by
                `Coordinator.task_detail_payload`.

        Side Effects:
            Writes wrapped, copy-paste-safe human output to the renderer stream.
        """
        self.section(f"{payload['id']}: {payload['title']}")
        rows: list[tuple[str, object]] = [
            ("state", payload["state"]),
            ("manual", payload["manual_status"]),
            ("priority", payload["priority"]),
        ]
        if payload.get("repo"):
            rows.append(("repo", payload["repo"]))
        if payload.get("prompt_key"):
            rows.append(("prompt key", payload["prompt_key"]))
        if payload.get("prompt_path"):
            rows.append(("prompt path", payload["prompt_path"]))
        self._detail_rows(rows, label_width=11)

        if payload.get("summary"):
            self._detail_section("SUMMARY")
            self.line(payload["summary"], initial_indent="  ", subsequent_indent="  ")

        if payload.get("blockers"):
            self._detail_section("BLOCKERS")
            for blocker in payload["blockers"]:
                self.line(f"- {blocker}", initial_indent="  ", subsequent_indent="    ")

        if payload.get("requirements"):
            self._detail_section("REQUIREMENTS")
            for requirement in payload["requirements"]:
                marker = "OK" if requirement["satisfied"] else "BLOCKED"
                detail = requirement.get("detail") or ""
                suffix = f" ({detail})" if detail else ""
                self.line(
                    f"- {marker} {requirement['description']}{suffix}",
                    initial_indent="  ",
                    subsequent_indent="    ",
                )

        if payload.get("execution"):
            self._detail_section("EXECUTION")
            self._detail_mapping(payload["execution"])

        if payload.get("next_action"):
            self._detail_section("NEXT ACTION")
            self.line(payload["next_action"], initial_indent="  ", subsequent_indent="  ")

        if payload.get("validation_checks"):
            self._detail_section("VALIDATION")
            for check in payload["validation_checks"]:
                self.line(f"- {check}", initial_indent="  ", subsequent_indent="    ")

        self._detail_section("EVIDENCE")
        if payload.get("evidence"):
            for evidence in payload["evidence"]:
                self.line(f"- {evidence}", initial_indent="  ", subsequent_indent="    ")
        else:
            self.line(
                "(none)",
                initial_indent="  ",
                subsequent_indent="  ",
                style=OVERVIEW_MUTED_STYLE,
            )

        if payload.get("metadata"):
            self._detail_section("METADATA")
            self._detail_mapping(payload["metadata"])

        lease_rows = [
            ("agent", payload.get("lease_agent_id") or "(none)"),
            ("expires", payload.get("lease_expires_at") or "(none)"),
        ]
        self._detail_section("LEASE")
        self._detail_rows(lease_rows, label_width=9)

        completion_rows = [
            ("action", payload.get("completion_action") or "(none)"),
            ("by", payload.get("completed_by") or "(none)"),
            ("at", payload.get("completed_at") or "(none)"),
        ]
        self._detail_section("COMPLETION")
        self._detail_rows(completion_rows, label_width=9)

    def _detail_rows(
        self,
        rows: list[tuple[str, object]],
        *,
        label_width: int,
    ) -> None:
        """Render wrapped key/value rows for task detail sections."""
        for label, value in rows:
            self.kv_row(label, value, label_width=label_width)

    def _detail_section(self, heading: str) -> None:
        """Render a visually separated task detail section."""
        self._console.print()
        self.section(heading)

    def _detail_mapping(self, payload: dict[str, Any]) -> None:
        """Render a dictionary as stable detail rows."""
        for key in sorted(payload):
            self.field(str(key), _detail_value(payload[key]), indent=2)

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

    def _overview_attention(self, payload: dict[str, Any], *, wide: bool = False) -> None:
        """Render active, review, and integration work as one attention list."""
        groups = payload["groups"]
        counts = payload["counts"]
        keys = ("active", "review", "integration")
        items = [(key, item) for key in keys for item in groups[key]]
        total = sum(counts[key] for key in keys)
        if not items and total == 0:
            return
        self._console.print(Text("ATTENTION", style=self._style(OVERVIEW_HEADER_STYLE)))
        if not items:
            self._console.print(Text("  (none)", style=self._style(OVERVIEW_MUTED_STYLE)))
        for group, item in items:
            if wide:
                self._overview_wide_row(group, item)
            else:
                self._overview_attention_row(group, item)
        self._overview_hidden_count(total - len(items), "attention items")
        self._console.print()

    def _overview_blocked(self, payload: dict[str, Any], *, wide: bool = False) -> None:
        """Render blocked work with its blocker because it cannot be acted on directly."""
        items = payload["groups"]["blocked"]
        total = payload["counts"]["blocked"]
        if not items and total == 0:
            return
        self._console.print(Text(f"BLOCKED ({total})", style=self._style(OVERVIEW_BLOCKED_STYLE)))
        if not items:
            self._console.print(Text("  (none)", style=self._style(OVERVIEW_MUTED_STYLE)))
        for item in items:
            if wide:
                self._overview_wide_row("blocked", item)
            else:
                self._overview_blocked_row(item)
        self._overview_hidden_count(total - len(items), "blocked tasks")
        self._console.print()

    def _overview_ready(self, payload: dict[str, Any], *, wide: bool = False) -> None:
        """Render ready work as a title-first task index."""
        items = payload["groups"]["ready"]
        total = payload["counts"]["ready"]
        self._console.print(Text(f"READY ({total})", style=self._style(OVERVIEW_READY_STYLE)))
        if not items:
            self._console.print(Text("  (none)", style=self._style(OVERVIEW_MUTED_STYLE)))
        for item in items:
            if wide:
                self._overview_wide_row("ready", item)
            else:
                self._overview_ready_row(item)
        self._overview_hidden_count(total - len(items), "ready tasks")
        self._console.print()

    def _overview_recent(self, payload: dict[str, Any], *, wide: bool = False) -> None:
        """Render recent completions as a short history tail."""
        items = payload["groups"]["recently_completed"]
        total = payload["counts"]["recently_completed"]
        self._console.print(Text(f"RECENT ({total})", style=self._style(OVERVIEW_RECENT_STYLE)))
        if not items:
            self._console.print(Text("  (none)", style=self._style(OVERVIEW_MUTED_STYLE)))
        for item in items:
            if wide:
                self._overview_wide_row("recently_completed", item)
            else:
                self._overview_recent_row(item)
        self._overview_hidden_count(total - len(items), "completed tasks")

    def _overview_plain_row(self, group: str, item: dict[str, Any]) -> None:
        """Render one plain overview task as stable key/value fragments."""
        fragments = [
            f"status={self._plain_value(self._overview_status(group).lower())}",
            f"id={self._plain_value(item['id'])}",
            f"title={self._plain_value(item['title'])}",
        ]
        if group in {"ready", "active", "review", "integration"} and item.get("next_action"):
            fragments.append(f"next={self._plain_value(item['next_action'])}")
        if group == "blocked" and self._overview_blocker(item):
            fragments.append(f"blocker={self._plain_value(self._overview_blocker(item))}")
        if group in {"review", "integration", "recently_completed"} and item.get("latest_evidence"):
            fragments.append(f"evidence={self._plain_value(item['latest_evidence'])}")
        if group == "recently_completed":
            if item.get("completed_at"):
                fragments.append(f"completed_at={self._plain_value(item['completed_at'])}")
            if item.get("completion_action"):
                fragments.append(f"completion={self._plain_value(item['completion_action'])}")
        self.raw_line(" ".join(fragments))

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
            style=self._overview_group_style(group),
        )

    def _overview_blocked_row(self, item: dict[str, Any]) -> None:
        """Render one blocked row with the concise blocker summary."""
        prefix = f"  {self._overview_handle(item):<{self._handle_width}} "
        self.line(
            self._compact_text(item["title"]),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=True,
            style=OVERVIEW_BLOCKED_STYLE,
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
            style=OVERVIEW_READY_STYLE,
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
            style=OVERVIEW_RECENT_STYLE,
        )

    def _overview_wide_row(self, group: str, item: dict[str, Any]) -> None:
        """Render one wide-mode overview row with concise detail fragments."""
        status = self._overview_status(group)
        suffix = self._overview_wide_suffix(group, item)
        title = self._compact_text(item["title"])
        prefix, text_prefix = self._overview_wide_prefix(status, item)
        core = f"{text_prefix}{title}"
        text = f"{core} | {suffix}" if suffix else core
        self.line(
            text,
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=True,
            style=self._overview_group_style(group),
        )

    def _overview_wide_prefix(self, status: str, item: dict[str, Any]) -> tuple[str, str]:
        """Return prefixes for a wide row that still work in narrow terminals."""
        handle = self._overview_handle(item)
        full_prefix = f"  {status:<{OVERVIEW_STATUS_WIDTH}} {handle:<{self._handle_width}} "
        if self._terminal_width - len(full_prefix) >= 20:
            return full_prefix, ""
        return f"  {status} ", f"{handle} "

    def _overview_wide_suffix(self, group: str, item: dict[str, Any]) -> str:
        """Return targeted detail fragments for one wide-mode row."""
        fragments: list[str] = []
        if group in {"ready", "active", "review", "integration"} and item.get("next_action"):
            fragments.append(f"next: {self._compact_text(item['next_action'])}")
        if group == "blocked" and self._overview_blocker(item):
            fragments.append(f"blocker: {self._overview_blocker(item)}")
        if group in {"review", "integration", "recently_completed"} and item.get("latest_evidence"):
            fragments.append(f"evidence: {self._compact_text(item['latest_evidence'])}")
        if group == "recently_completed":
            completed = self._compact_text(item.get("completed_at") or "")
            if completed:
                fragments.append(f"completed: {completed}")
            action = self._compact_text(item.get("completion_action") or "")
            if action:
                fragments.append(f"action: {action}")
        return " | ".join(fragments)

    def _overview_hidden_count(self, hidden_count: int, noun: str) -> None:
        """Render a hidden-count hint for a section."""
        if hidden_count <= 0:
            return
        self._console.print(
            Text(
                f"  ... {hidden_count} more {noun}; use --limit 0 to show all",
                style=self._style(OVERVIEW_MUTED_STYLE),
            )
        )

    def _overview_status(self, group: str) -> str:
        """Return a short status label for an overview group."""
        return {
            "active": "ACTIVE",
            "review": "REVIEW",
            "integration": "MERGE",
            "recently_completed": "RECENT",
        }.get(group, group.upper())

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

    def _overview_group_style(self, group: str) -> str | None:
        """Return the restrained colour style for an overview group."""
        return {
            "active": OVERVIEW_ATTENTION_STYLE,
            "review": "magenta",
            "integration": "cyan",
            "blocked": OVERVIEW_BLOCKED_STYLE,
            "ready": OVERVIEW_READY_STYLE,
            "recently_completed": OVERVIEW_RECENT_STYLE,
        }.get(group)

    def _value_style(self, label: str, value: object) -> str:
        """Return the style for a table value without changing its text."""
        if label in {"status", "state", "manual"}:
            return STATUS_STYLES.get(str(value).lower(), "")
        return ""

    def _style(self, style: str | None) -> str:
        """Return an enabled Rich style or an empty style when styles are disabled."""
        if not self._styles_enabled:
            return ""
        return style or ""

    def _output_is_tty(self) -> bool:
        """Return whether the output stream is an interactive terminal."""
        isatty = getattr(self._output, "isatty", None)
        if not callable(isatty):
            return False
        return bool(isatty())

    def _compact_text(self, value: object) -> str:
        """Collapse internal whitespace for one-line summaries."""
        return " ".join(str(value).split())

    def _plain_value(self, value: object) -> str:
        """Return one shell-token-safe plain output value."""
        return shlex.quote(self._compact_text(value))

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
            self._console.print(Text("No proposed tasks."))
            return
        for proposal in payload["proposals"]:
            task = proposal["task"]
            self.line(
                f"{proposal['id']}: {task['id']} - {task['title']}",
                subsequent_indent="  ",
                break_long_words=True,
            )
            self.kv_row("intake", proposal["intake_id"], label_width=8)
            self.kv_row("status", proposal["status"], label_width=8)
            if task.get("repo"):
                self.kv_row("repo", task["repo"], label_width=8)
            if task.get("next_action"):
                self.kv_row(
                    "next",
                    task["next_action"],
                    label_width=8,
                    break_long_words=True,
                )


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
                lines.extend(
                    _render_text_include(
                        config,
                        notebook_path,
                        label="notebook",
                        allow_task_source_root=True,
                    )
                )
        return "\n".join(lines).rstrip() + "\n"


def _metadata_notebook_paths(metadata: dict) -> list[str]:
    """Return opt-in notebook paths from task metadata."""
    value = metadata.get("notebook_paths")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _detail_value(value: Any) -> str:
    """Return a stable text representation for detail field values."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _render_text_include(
    config: ProjectConfig,
    source_path: str,
    *,
    label: str,
    allow_task_source_root: bool = False,
) -> list[str]:
    """Return config-relative text content or a deterministic unreadable note."""
    source = source_path.strip()
    lines = [f"Source: {source}", ""]
    requested_path = Path(source)
    if source.startswith("~") or requested_path.is_absolute():
        lines.append(f"[{label} not included: absolute or home-relative paths are not allowed]")
        return lines
    if allow_task_source_root and (
        requested_path.parts[:1] != ("notebooks",) or ".." in requested_path.parts
    ):
        lines.append(f"[{label} not included: path must be below notebooks/]")
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
    if allow_task_source_root and not path.exists():
        fallback = _task_source_notebook_path(config, requested_path)
        if fallback is not None:
            path = fallback

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


def _task_source_notebook_path(config: ProjectConfig, requested_path: Path) -> Path | None:
    """Return a safe task-source-root notebook include fallback path."""
    try:
        task_source_root = config.effective_task_source_root.resolve()
        path = (task_source_root / requested_path).resolve(strict=False)
        notebooks_root = (task_source_root / "notebooks").resolve(strict=False)
    except OSError:
        return None
    return path if path.is_relative_to(notebooks_root) else None
