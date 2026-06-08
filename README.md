# agent-tracker

`agent-tracker` is a local coordination queue for agent-managed projects. It
stores live task state in SQLite, imports a project task plan, lets agents claim
ready work with leases, records events and evidence, and exports audit snapshots.

The package is deliberately project-agnostic. Project-specific task formats,
prompt text, event normalization, and exports are provided by config and
plugins.

## What You Get

- A small CLI for initializing a project, importing tasks, claiming work,
  heartbeating leases, completing or failing tasks, ingesting events, and
  exporting snapshots.
- A JSON task-plan importer that is enough for a first local queue.
- SQLite-backed task state, dependencies, leases, evidence, events, and audit
  logs.
- Plugin protocols for custom importers, prompt renderers, event adapters,
  follow-up planners, and exporters.
- A vendored Codex skill named `project-manager` for repos that want agents to
  pull and log work through `agent-tracker`.

## Install

From a checkout:

```bash
git clone <agent-tracker-repo-url>
cd agent-tracker
uv sync
uv run agent-tracker --help
```

To install the CLI into another environment from a local checkout:

```bash
python -m pip install /path/to/agent-tracker
agent-tracker --help
```

If you use `uv` tools:

```bash
uv tool install /path/to/agent-tracker
agent-tracker --help
```

## Quickstart

Create a minimal project directory:

```bash
mkdir -p demo-tracker/spool/inbox demo-tracker/spool/done demo-tracker/spool/error demo-tracker/exports
```

Create `demo-tracker/project.json`:

```json
{
  "project_id": "demo",
  "name": "Demo Tracker",
  "db_path": ".agent-tracker/state.sqlite",
  "task_plan_path": "tasks.json",
  "importer": "agent_tracker.importers:JsonTaskImporter",
  "prompt_renderer": "agent_tracker.rendering:DefaultPromptRenderer",
  "exporter": "agent_tracker.exporters:JsonSnapshotExporter",
  "export_path": "exports/snapshot.json",
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error"
  }
}
```

Create `demo-tracker/tasks.json`:

```json
{
  "tasks": [
    {
      "id": "write-readme",
      "title": "Write the README",
      "repo": "demo-app",
      "status": "pending",
      "priority": 10,
      "summary": "Document the first local workflow.",
      "execution": {
        "primary_files": ["README.md"],
        "notes": "Keep examples copy-pasteable."
      },
      "validation_checks": ["Manual review: README explains setup and usage."],
      "next_action": "Draft the setup and usage sections.",
      "metadata": {
        "roles": ["maintainer"],
        "write_scopes": ["README.md"]
      }
    },
    {
      "id": "review-readme",
      "title": "Review the README",
      "repo": "demo-app",
      "status": "pending",
      "priority": 20,
      "summary": "Check the README from a new user's perspective.",
      "requirements": [
        {
          "kind": "task",
          "task": "write-readme",
          "description": "README draft is complete."
        }
      ],
      "validation_checks": ["Manual review: commands can be copied into a shell."],
      "next_action": "Review the README after the drafting task is done.",
      "metadata": {
        "roles": ["reviewer"],
        "write_scopes": ["README.md"]
      }
    }
  ]
}
```

Initialize and import:

```bash
agent-tracker init --config demo-tracker/project.json
agent-tracker import --config demo-tracker/project.json
agent-tracker status --config demo-tracker/project.json
```

Find and claim ready work:

```bash
agent-tracker next --config demo-tracker/project.json --role maintainer --limit 1
agent-tracker claim --config demo-tracker/project.json --agent agent-1 --role maintainer --lease-seconds 7200
```

The claim command prints JSON containing the `task_id` and `lease_token`. Keep
the token; `heartbeat`, `complete`, and `fail` require it.

Render the task prompt:

```bash
agent-tracker task --config demo-tracker/project.json write-readme --markdown
```

Extend a lease while working:

```bash
agent-tracker heartbeat --config demo-tracker/project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --lease-seconds 7200
```

Complete the task with evidence:

If the task changed tracked repository files, commit and integrate the work
before completing it in the tracker. Use a merged commit, pushed branch, or PR
URL as evidence, not only local file paths or validation commands.

```bash
agent-tracker complete --config demo-tracker/project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --evidence "git:main-commit-or-merged-branch" \
  --evidence "file:README.md"
```

Re-import after editing the task plan, then check what unblocked:

```bash
agent-tracker import --config demo-tracker/project.json
agent-tracker next --config demo-tracker/project.json --limit 5
```

## CLI Reference

Every command requires `--config <project.json>`. Every command also accepts
`--db <path>` to override the SQLite database path from config.

| Command | Purpose | Example |
| --- | --- | --- |
| `init` | Create or update the project row and database schema. | `agent-tracker init --config demo-tracker/project.json` |
| `import` | Import tasks and dependencies from the configured importer. | `agent-tracker import --config demo-tracker/project.json` |
| `status` | Show project counts; add `--json` for full task state. | `agent-tracker status --config demo-tracker/project.json --json` |
| `next` | List ready tasks, optionally filtered by repo or role. | `agent-tracker next --config demo-tracker/project.json --role maintainer --limit 1` |
| `task` | Show one task's prompt/context; add `--json` for stored state. | `agent-tracker task --config demo-tracker/project.json write-readme --markdown` |
| `claim` | Atomically claim a ready task and create a lease token. | `agent-tracker claim --config demo-tracker/project.json --agent agent-1 --role maintainer --lease-seconds 7200` |
| `heartbeat` | Extend a live lease and mark the task `in_progress`. | `agent-tracker heartbeat --config demo-tracker/project.json write-readme --lease-token <token> --agent agent-1` |
| `complete` | Mark a leased task `done` and record evidence URIs. | `agent-tracker complete --config demo-tracker/project.json write-readme --lease-token <token> --evidence "git:<main-sha>"` |
| `fail` | Mark a leased task `failed` with a reason. | `agent-tracker fail --config demo-tracker/project.json write-readme --lease-token <token> --reason "validation failed"` |
| `ingest-event` | Ingest one JSON event file. | `agent-tracker ingest-event --config demo-tracker/project.json event.json --actor callback` |
| `ingest-spool` | Ingest all `*.json` files from the configured local spool inbox. | `agent-tracker ingest-spool --config demo-tracker/project.json --actor spool` |
| `export` | Write the configured audit snapshot through the exporter. | `agent-tracker export --config demo-tracker/project.json` |

See [docs/operations.md](docs/operations.md) for lifecycle details, stale lease
recovery, spool ingestion, and exports.

## Configuration And Task Plans

Config is JSON. Relative paths are resolved relative to the directory containing
the config file, not the shell's current working directory. The built-in config
fields and task-plan format are documented in:

- [docs/configuration.md](docs/configuration.md)
- [docs/task-plans.md](docs/task-plans.md)

## Plugins

Plugin specs use `module:object` strings. Before loading project plugins,
`agent-tracker` adds the config directory to `sys.path`, so project-local
modules can live beside the config file.

Built-in defaults:

- `importer`: `agent_tracker.importers:JsonTaskImporter`
- `prompt_renderer`: `agent_tracker.rendering:DefaultPromptRenderer`
- `exporter`: `agent_tracker.exporters:JsonSnapshotExporter`

See [docs/plugins.md](docs/plugins.md) for importer, prompt renderer, event
adapter, follow-up planner, and exporter contracts.

## Events, Evidence, And Audit Snapshots

Events are idempotent by `event_id`. The default event adapter accepts JSON
objects with `event_id` or `id`, optional `kind`, optional `task_id`, and any
additional payload fields.

Evidence is stored as URI-like strings such as `git:<sha>`, `file:README.md`,
`pr:https://github.com/org/repo/pull/123`, or `artifact:s3://bucket/key`.
Large artifacts should be linked, not copied into the tracker.

Snapshots include evaluated task state, events, and audit log entries:

```bash
agent-tracker export --config demo-tracker/project.json
```

## Codex Project-Manager Skill

The package vendors a reusable Codex skill named `project-manager`. Install it
with:

```bash
agent-tracker-install-skill --name project-manager
```

By default, this copies the skill into `$CODEX_HOME/skills` or
`~/.codex/skills`. Use `--destination-root`, `--overwrite`, or `--dry-run` when
needed.

## Self-Dogfooding

This repository tracks its own implementation work with `agent-tracker`. The
committed task plan is [tracking/tasks.json](tracking/tasks.json), the project
config is [tracking/project.json](tracking/project.json), and the local SQLite
database is ignored runtime state.

Use [tracking/README.md](tracking/README.md) when asking an agent to pull the
next task, log work, complete work, or repair claim failures.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Planning notes and future architecture options live in
[docs/planning.md](docs/planning.md).
