---
orphan: true
---

# Overview UX Contract

Status: implementation contract for downstream `overview`, intake, and proposal
output tasks.

This note defines the UX contract for `agent-tracker overview` and adjacent
planning lists. The goal is a compact default command that stays useful in a
terminal, copied handoff, pull request comment, chat update, or coordinator log.
It is intentionally narrower than a full terminal UI design.

## Scope

This contract covers:

- human `overview` output;
- `overview --json` stability;
- summary versus detail drilldown behavior;
- output-mode expectations;
- intake and proposal listing consistency;
- the deferred posture for a later Textual TUI.

It does not cover parser migration, storage changes, task claiming semantics,
or the design of a full-screen dashboard.

## Product Contract

`overview` answers the coordination question: what can move now, what is in
flight, what needs review or integration, what is blocked, and what recently
finished.

It is a project-log summary, not a raw task dump. Downstream work should add
drilldown controls for more detail instead of making the default output longer.

## Compact Default

`agent-tracker overview` is the compact human summary. The default output must:

- keep stable groups for Ready, Active, Review, Integration, Blocked, and
  Recently completed;
- show group counts even when the visible item list is limited;
- render one concise primary row per visible task;
- prefer task ID, title, current state or agent, short blocker, short next
  action, and latest decisive evidence over nested records;
- reduce recently completed noise by showing only the configured visible list;
- preserve the existing display-limit behavior, including `--limit 0` for every
  grouped task;
- remain readable when captured in logs or copied into plain text.

Default rows should not show validation checks, full metadata, full evidence
lists, all requirements, notebook paths, write scopes, raw lease tokens, or
every completion detail. Those belong in detail drilldown or JSON.

## No-Box Output

The default human output must remain no-box and copy-paste safe:

- use plain text with ASCII headings and labels;
- do not use box-drawing characters, decorative panels, or nested table grids;
- do not require ANSI color for meaning;
- keep output intelligible when color is stripped or stdout is captured;
- wrap long titles, blockers, next actions, evidence, and completion notes with
  stable continuation indentation;
- avoid trailing whitespace and terminal-width-dependent semantic changes.

Rich rendering is acceptable only when the captured default remains plain,
borderless, and readable.

## Summary And Detail Drilldown

The summary/detail split is the main guard against overview bloat.

Summary mode is the default:

- one primary row per listed task;
- one or two detail lines only for next action, blocker, or decisive evidence;
- empty fields omitted;
- truncated groups called out with a plain note that explains how to request
  more rows.

Detail mode is a planned human drilldown, not a replacement for `--json`.
Downstream implementation should choose one CLI shape and use it consistently
across overview, intake, and proposal lists. Prefer a simple verbosity flag such
as `--detail` unless a broader `--mode compact|detail` design is already being
implemented for multiple commands.

Detail mode may include:

- full `next_action` text;
- unsatisfied requirement details;
- dependency summaries;
- latest lease holder and expiry, excluding raw lease tokens unless explicitly
  needed;
- latest evidence and completion audit records;
- write scopes, validation checks, authority notes, notebook updates, and lane
  metadata;
- proposal or intake provenance when a planning section is intentionally shown.

Acceptance for detail work should compare default and detail output on the same
fixture: default stays compact, detail shows the extra fields.

## Output Modes

The supported output modes are:

- compact human output: default, no-box, copy-paste safe;
- detailed human output: planned drilldown for humans;
- JSON output: existing machine contract behind `--json`;
- future TUI: deferred and separate from the line-oriented command.

`--json` is a serialization choice, not a verbosity flag. JSON output should not
inherit human wrapping, truncation markers, ANSI styling, Rich markup, or
copy-edited labels.

If a future command accepts both detail and JSON options, the behavior must be
documented before implementation. The safe default is that `--json` returns the
stable structured payload, with detail-like additions allowed only as additive
fields or explicit documented options.

## JSON Stability

`overview --json` is the automation contract. Downstream tasks must treat the
current payload shape as stable:

- `project_id`, `name`, `db_path`, and path-summary fields remain present;
- `limit` remains the requested visible-row limit;
- `counts` remains an object of integer counts;
- `groups.ready`, `groups.active`, `groups.review`, `groups.integration`,
  `groups.blocked`, and `groups.recently_completed` remain present;
- group values remain ordered lists of task dictionaries;
- task IDs and status-like values remain strings;
- recently completed ordering remains based on completion audit records;
- `--limit` semantics remain stable, including `--limit 0` for all grouped
  tasks.

JSON changes should be additive unless a versioned migration is explicitly
designed. Do not rename or remove existing keys for human-output cleanup. Do
not put wrapped strings, ellipsized human text, ANSI escape sequences, Rich
markup, or section labels into JSON fields.

Acceptance for JSON work:

- existing JSON consumers can still read `counts` and every `groups.*` key;
- fixture output remains deterministic for ordering and data types;
- adding a human detail mode does not change default `--json` output;
- any new JSON fields are documented and covered by compatibility tests.

## Intake And Proposal Consistency

Raw intake and proposed tasks are planning records, not live queue tasks.
`overview` must not imply they are claimable.

The consistency rules are:

- raw intake does not appear in Ready, Active, Review, Integration, Blocked, or
  Recently completed task groups;
- proposed tasks do not appear in live overview groups until promoted;
- if a future overview planning section includes intake or proposals, it must be
  visually and textually distinct from live task groups;
- intake and proposal list commands should follow the same compact/detail/no-box
  posture even when they use a richer renderer internally;
- proposal rows should preserve the task-shaped contract fields that matter for
  review: proposed task ID, title, source intake, dependencies, authority,
  write scopes, validation, and next action;
- promotion remains the boundary that turns a proposal into live queue state.

This keeps project-manager triage, follow-up proposal automation, and live
worker coordination on one mental model without mixing their authority levels.

## Deferred Textual TUI

Textual remains deferred for overview work. The line-oriented CLI should not
adopt a full-screen event-loop dependency just to improve grouped output.

A future Textual task is appropriate only when there are concrete interactive
workflows such as filtering, selecting tasks, reviewing proposals, resolving
interventions, or watching live queue changes. That task should:

- introduce a separate command or explicit TUI mode;
- consume the same service/query layer as the CLI;
- leave default `overview` output unchanged;
- preserve `overview --json` compatibility;
- include terminal rendering checks and workflow acceptance tests;
- define fallback behavior for non-interactive environments.

Do not treat a TUI as the acceptance path for compact overview output. The
plain command must remain useful by itself.

## Downstream Acceptance Criteria

Use these criteria when implementing overview UX tasks:

- Default `agent-tracker overview` output is compact, no-box, ASCII-safe, and
  readable after copy/paste into Markdown, chat, or logs.
- Major group order is stable and each group exposes a count.
- Long titles, blockers, next actions, evidence, and completion notes wrap
  without being confused with neighboring task rows.
- The default output limit is documented, and truncated groups explain how to
  request all rows.
- `overview --json` keeps the existing `counts` and `groups.*` contract.
- Human formatting changes do not alter JSON payload shape or data types.
- Detail drilldown is opt-in and tested against the same fixture as compact
  output.
- Intake and proposed-task records remain non-claimable and visually distinct
  from live overview task groups.
- Intake/proposal listing output follows the same compact/detail terminology
  and copy-paste-safe posture.
- No Textual dependency or full-screen TUI behavior is required for compact
  overview work.
