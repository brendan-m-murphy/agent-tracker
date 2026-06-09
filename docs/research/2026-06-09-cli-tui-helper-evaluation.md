# CLI And TUI Helper Evaluation

This note evaluates whether `agent-tracker` should keep improving its
human-readable CLI output with local helpers, adopt a richer terminal rendering
library, or migrate command parsing / interactive workflows to a more mature
framework.

## Current Requirements

The immediate UX problems are narrow:

- human `status`, `overview`, `next`, and intake output should wrap cleanly in a
  normal terminal;
- JSON output must remain stable and machine-readable;
- mutating commands must continue to print path/refusal diagnostics before
  touching SQLite;
- the package currently has no runtime dependencies, which keeps installation
  simple for local tracking repos and wrapper scripts.

The current `argparse` plus `textwrap` implementation is adequate for small
formatting fixes, but the number of human output surfaces is growing. A mature
renderer may become worthwhile when output needs tables, trees, color, status
badges, consistent terminal-width handling, or snapshot-style visual tests.

## Candidates

### Rich

Rich is the strongest candidate for human output rendering. Its official docs
describe it as a terminal formatting library for styled text, tables, Markdown,
syntax highlighting, and readable data display. Rich tables calculate terminal
column widths and wrap content when the terminal is too narrow.

Fit:

- good match for `overview`, `status`, `list-intake`, and future review or
  intervention summaries;
- can be introduced behind a small human-rendering boundary while leaving
  `argparse` and JSON paths alone;
- likely improves width handling, tables, dimmed metadata, and no-grid layouts.

Risks:

- adds the first runtime dependency;
- color/markup must stay conservative because terminal captures, logs, and
  accessibility matter more than decorative output;
- tests need to assert semantic text and stable plain output, not ANSI styling.

Recommended posture: add a separate Rich renderer spike only if another output
task needs table/tree/badge behavior that would otherwise grow more ad hoc
formatting helpers.

### Typer

Typer is a CLI application framework based on Python type hints. Its docs cover
commands, command groups, parameter types, environment variables, progress bars,
and user-friendly CLI behavior.

Fit:

- attractive if the command layer is redesigned around typed function
  signatures and generated help;
- could reduce boilerplate once command count and option validation grow.

Risks:

- parser migration is larger than the current output problem;
- existing command names, option spelling, help output, exit behavior, and
  traceback-free error handling would all need compatibility tests;
- Typer does not by itself solve overview row layout; it is primarily a command
  declaration/parsing decision.

Recommended posture: do not migrate now. Revisit after command semantics,
authority gates, and MCP/adapter surfaces stabilize.

### Click

Click is a mature command-line framework with composable commands, environment
variable support, prompts, terminal helpers, and POSIX-oriented parsing.

Fit:

- robust choice if `agent-tracker` needs command composition features that
  `argparse` makes awkward;
- has explicit support for environment-derived values, which is relevant to the
  recently added config/database defaults.

Risks:

- a Click migration has the same compatibility blast radius as Typer, with less
  benefit from type-hint-driven command signatures;
- the current parser is working and does not block the requested rendering
  improvements.

Recommended posture: treat Click as the underlying mature parser option to
compare if a Typer migration is proposed, not as an immediate output fix.

### Textual

Textual is a high-level TUI framework for Python applications that can run in
the terminal and provides widgets, CSS-like styling, cross-platform behavior,
SSH-friendly operation, and CLI integration.

Fit:

- good candidate for a future interactive project dashboard, intervention
  queue, task triage browser, or notebook-style coordination view;
- likely useful only when the product has workflows that benefit from
  navigation, selection, filters, panels, and live updates.

Risks:

- too heavy for current line-oriented CLI output;
- introduces an event-loop application model and a second UX surface to support;
- would need separate acceptance tests and terminal/browser rendering checks.

Recommended posture: defer until there is an explicit TUI task with concrete
interactive workflows.

### prompt_toolkit

prompt_toolkit supports complex full-screen terminal applications built from a
layout, key bindings, input/output abstractions, styles, and widgets.

Fit:

- useful for custom shells, interactive prompts, keybinding-heavy workflows, or
  full-screen apps where low-level control matters;
- mature option if an interactive task picker or command palette is needed.

Risks:

- lower-level than Textual for app-style layouts;
- not needed for static command output;
- would add complexity without solving simple wrapping and grouping bugs.

Recommended posture: defer unless a specific prompt, shell, or keybinding-heavy
workflow appears.

### Blessed

Blessed provides terminal capabilities for colors, keyboard input, screen
positioning, and basic full-screen terminal programs through a small `Terminal`
API.

Fit:

- useful for low-level terminal control while staying lighter than a full TUI
  framework;
- possible fit for simple keyboard-driven screens.

Risks:

- not a high-level formatter for grouped queue output;
- would make `agent-tracker` own more layout and rendering behavior itself.

Recommended posture: lower fit than Rich for output and lower fit than Textual
for full application screens.

## Recommendation

Keep `argparse` and the current stdlib wrapping helpers for immediate bug fixes.
Do not add Rich, Typer, Click, Textual, prompt_toolkit, or Blessed in this
research task.

If the next wave of human-output work needs reusable tables, trees, badges, or
consistent terminal-width rendering across several commands, create a focused
Rich renderer spike. The spike should:

- introduce a small `HumanRenderer` or output module rather than scattering
  Rich calls through command handlers;
- leave `--json` output untouched;
- preserve plain, copy-pasteable output when styling is disabled or output is
  captured;
- cover `overview`, `status`, and intake/proposal listings before changing the
  parser layer;
- include compatibility tests for terminal width, no-color/plain rendering, and
  snapshot-like text output.

Typer or Click should be treated as a later parser migration decision, not a
solution to the current formatting problems. Textual, prompt_toolkit, and
Blessed should wait for explicit interactive TUI requirements.

## Follow-Up Candidates

- `rich-human-output-renderer-spike`: Prototype a Rich-backed human renderer for
  `overview`, `status`, and intake/proposal listings behind a small abstraction.
- `cli-parser-migration-design`: Compare argparse, Typer, and Click only after
  command authority/error semantics stabilize.
- `interactive-tracker-tui-prototype`: Explore Textual or prompt_toolkit only
  when there is a concrete interactive queue/triage workflow.

## Sources

- Rich introduction: https://rich.readthedocs.io/en/stable/introduction.html
- Rich tables: https://rich.readthedocs.io/en/stable/tables.html
- Typer docs: https://typer.tiangolo.com/
- Typer features: https://typer.tiangolo.com/features/
- Click rationale: https://click.palletsprojects.com/en/stable/why/
- Textual docs: https://textual.textualize.io/
- prompt_toolkit full-screen apps:
  https://python-prompt-toolkit.readthedocs.io/en/3.0.40/pages/full_screen_apps.html
- Blessed introduction: https://blessed.readthedocs.io/en/latest/intro.html
