# CLI Parser Migration Design

This note defines the compatibility path for future parser or renderer changes
in `agent-tracker`. It exists to keep command parsing, structured output, and
plain human output stable while narrower UX tasks improve individual command
output.

## Decision

Keep the current hybrid CLI boundary for now:

- `argparse` remains the root parser and owner of existing flat commands.
- The grouped `intake` command may continue to delegate to the existing
  Typer/Click adapter because it is already isolated and has flat aliases.
- Rich stays behind `HumanOutputRenderer` for human output only.
- Do not migrate additional commands to Typer or Click inside small output
  fixes.

A broader parser migration should be a staged compatibility project with alias
tests, JSON-contract tests, help-output tests, and shell-completion policy
tests before command ownership moves.

## Current Compatibility Surface

The command surface is automation-facing, not only human-facing. Preserve these
constraints:

- command names stay flat and scriptable, such as `status`, `overview`, `next`,
  `task`, `claim`, `complete`, `record-intake`, and `list-proposals`;
- grouped intake commands are additive aliases for the flat intake commands,
  not replacements;
- `--config` and `--db` keep their explicit-argument precedence over
  `AGENT_TRACKER_CONFIG` and `AGENT_TRACKER_DB`;
- JSON payloads behind `--json` remain stable and do not inherit human
  formatting, color, wrapping, or labels;
- mutating commands keep resolved path reports and refusal diagnostics on
  stderr;
- help and default human output remain copy-paste-safe plain text without
  panels, box drawing, or decorative layout;
- shell-completion helper flags are not exposed unless a task deliberately
  designs and documents shell completion support.

## Candidate Comparison

### Argparse Grouping

`argparse` is the lowest-risk owner for existing commands. It already preserves
current option spelling, positional behavior, exit codes through `main()`, and
copy-paste-safe help text. Subparser grouping can support new command families
when needed, but nested groups should be additive and should retain flat aliases
for scripts.

The main drawback is boilerplate: every command needs explicit argument wiring,
and shared options have to be copied through helpers. That is acceptable while
the CLI remains an operational interface whose compatibility matters more than
declaration brevity.

Recommendation: keep as the default parser. Add grouped argparse commands only
when the grouping itself improves operator ergonomics, and keep flat aliases
through a documented deprecation window.

### Typer

Typer is already present for the grouped `intake` adapter. It is a reasonable
fit for new command families where typed function signatures, command grouping,
and generated help are useful. It also brings Click behavior underneath, so it
changes parsing details compared with `argparse`.

Risks to control before expanding Typer:

- generated help formatting can drift from existing plain text;
- shell-completion options can appear unless explicitly disabled;
- option ordering and group-level option rules differ from flat argparse
  commands;
- exceptions and validation errors must continue to return concise stderr
  messages without tracebacks.

Recommendation: allow Typer only behind a small adapter for new grouped command
families, with flat aliases and focused compatibility tests. Do not make Typer
the root parser until every existing command has golden compatibility coverage.

### Click

Click is the mature parser foundation underneath Typer and may be preferable if
the project wants explicit decorators instead of Typer's type-hint API. It has
strong command-composition behavior and environment-option support, but it has
the same migration blast radius for existing scripts.

Recommendation: do not migrate directly to Click unless Typer's type-hint model
becomes a poor fit. If Click is selected, treat it as a root-parser rewrite with
the same staged alias and contract tests as Typer.

### Optional Rich Rendering

Rich should remain a rendering dependency, not a parser dependency. It is useful
for width-aware human summaries, restrained color, and aligned text, but the
project's no-box plain output requirement rules out panels and decorative table
grids for default command output.

Recommendation: keep all Rich use behind `HumanOutputRenderer`. Parser code
should pass structured payloads to renderers instead of constructing Rich
objects directly. `--json` and plain modes must bypass Rich styling semantics.

## Staged Migration Path

1. Inventory command contracts.
   Record command names, aliases, positional arguments, options, JSON payload
   roots, stdout/stderr behavior, and expected exit codes.

2. Add compatibility tests before migration.
   Cover root help, grouped help, no shell-completion helper flags, flat alias
   visibility, JSON payload shape for representative read commands, concise
   error output for representative write commands, and `--config` / `--db`
   precedence.

3. Introduce grouped aliases without removing flat commands.
   New grouped commands may call existing command handlers through
   `argparse.Namespace` adapters, matching the current `intake` pattern.

4. Keep aliases through at least one minor release.
   Deprecation, if ever needed, should be documented in `docs/operations.md`
   with replacement commands and a removal date. The default should be no
   removal for coordination commands used by automation.

5. Migrate root ownership only after parity is boring.
   The root parser can move to Typer or Click only after command help, JSON,
   stderr, and exit-code tests pass for all command families.

## Test Requirements

Before any parser migration expands beyond grouped `intake`, add or preserve
tests for:

- root help includes flat commands and remains free of box-drawing characters;
- grouped help stays plain and does not expose completion installer flags;
- flat and grouped aliases produce identical JSON for intake list/update paths;
- representative `--json` payloads remain byte-for-byte stable except for
  documented new fields;
- common error paths return code `1`, write actionable `error: ...` messages to
  stderr, and avoid tracebacks;
- `--no-color` affects only human rendering, not JSON or plain output;
- `launch-worker --command ...` still preserves trailing arguments after the
  command boundary.

## Follow-Up Tasks

- `cli-command-contract-inventory`: Generate a maintained command/option
  inventory from the current parser and document stdout, stderr, and JSON
  contracts.
- `cli-parser-parity-tests`: Add alias and help parity tests across flat
  commands before any further Typer or Click migration.
- `intake-typer-adapter-hardening`: Keep the existing grouped intake adapter,
  but add JSON parity tests between grouped and flat intake commands.
