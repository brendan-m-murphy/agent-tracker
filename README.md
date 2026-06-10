# agent-tracker

`agent-tracker` is a local coordination queue for agent-managed projects. It
stores live task state in SQLite, imports a project task plan, lets agents claim
ready work with leases, records events and evidence, and exports audit snapshots.

The package is deliberately project-agnostic. Project-specific task formats,
prompt text, event normalization, and exports are provided by config and
plugins.

## How The Pieces Fit

An `agent-tracker` project has three durable inputs:

- a project config JSON file, passed to every CLI command with `--config`;
- a task plan, usually committed as JSON and imported into live state;
- optional project-local plugins for task import, prompt rendering, event
  normalization, and exports.

The live queue is a SQLite database. Treat it as runtime state: import from the
committed task plan, claim work with leases, record events and evidence while
agents work, and export snapshots when another system needs an audit artifact.
Git commits and GitHub PRs are evidence and review surfaces; they are not the
live coordination queue.

## What You Get

- A small CLI for initializing a project, importing tasks, claiming work,
  heartbeating leases, completing or failing tasks, ingesting events, and
  exporting snapshots.
- A JSON task-plan importer that is enough for a first local queue.
- SQLite-backed task state, dependencies, leases, evidence, events, and audit
  logs.
- Plugin protocols for custom importers, prompt renderers, event adapters,
  follow-up planners, and exporters.
- Vendored Codex skills for coordinator, project-manager, and one-task worker
  roles in repos that use `agent-tracker`.

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

### Preview Refs For Downstream Validation

When a downstream uv project needs an in-progress `agent-tracker` feature before
it reaches `main`, a maintainer can publish a temporary preview branch or share
a reachable commit SHA for validation. Use this only after the scoped change is
reviewable and has passed the maintainer's normal local checks. Preview refs are
temporary validation channels, not stable release policy.

In the downstream project, pin the preview through uv dependency sources:

```toml
[project]
dependencies = ["agent-tracker"]

[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", branch = "preview/<feature-or-task>" }
```

For an immutable validation run, pin the exact commit instead:

```toml
[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", rev = "<commit-sha>" }
```

Then refresh and validate the downstream lock:

```bash
uv lock --upgrade-package agent-tracker
uv sync
uv run <downstream-validation-command>
```

Record the downstream result on the tracker task as evidence, for example
`git:<preview-commit>`, `validation:<project>:<command-or-run-url>`, and any
review or PR URL. After the feature lands on `main` or is released, replace the
preview source with `branch = "main"`, `tag = "<release-tag>"`, or the normal
published dependency, then rerun `uv lock --upgrade-package agent-tracker`.
Avoid adjacent-checkout path dependencies as the default validation path; they
are harder to reproduce than a named git ref.

## Quickstart

The quickest way to try the queue is to scaffold a small plugin-free tracker
directory:

```bash
agent-tracker init-project demo-tracker --project-id demo --name "Demo Tracker"
```

This writes `project.json`, `tasks.json`, a local spool, an export directory, a
`.agent-tracker` runtime directory, and `.gitignore` entries for runtime files.
The generated config uses the built-in JSON task importer, default prompt
renderer, and JSON snapshot exporter without plugin fields:

```json
{
  "config_schema_version": 1,
  "project_id": "demo",
  "name": "Demo Tracker",
  "db_path": ".agent-tracker/state.sqlite",
  "task_plan_path": "tasks.json",
  "export_path": "exports/snapshot.json",
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error"
  }
}
```

Replace the starter task in `demo-tracker/tasks.json` with real work. For
example, one ready task and one dependent review task:

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

`import` is safe to re-run after task-plan edits. It creates the project row and
schema if needed, updates task definitions, preserves active leases for imported
active work, and recomputes which pending tasks are ready or blocked. The task
plan is authoritative: if you re-import a task plan that still says a completed
task is `pending`, the live task can become pending again.

Find and claim ready work:

```bash
agent-tracker next --config demo-tracker/project.json --role maintainer --limit 1
agent-tracker claim --config demo-tracker/project.json --agent agent-1 --role maintainer --lease-seconds 7200
```

Human `status`, `overview`, `next`, and intake output is rendered with Rich for
readable wrapping and alignment without decorative panels or box-drawing
characters by default. Human `overview` is a quick triage view: it shows count
summaries, an attention list for active/review/merge work, blocked tasks with
their current blockers, ready task titles, and a short recent-completion tail.
It intentionally omits full `next_action` prose and evidence paths; use
`agent-tracker task <task-id>` for full detail and add `--json` when you need
full task dictionaries. For automation, add `--json` to `next`, `status`,
`overview`, or intake list commands; JSON output is not wrapped or reformatted.

The claim command prints JSON containing the `task_id` and `lease_token`. Keep
the token; `heartbeat`, `complete`, and `fail` require it.

Render the task prompt:

```bash
agent-tracker task --config demo-tracker/project.json write-readme --markdown
```

The rendered prompt contains the summary, execution notes, dependency state,
validation checks, and next action. For task-level automation, add `--json` to
`task`.

Extend a lease while working:

```bash
agent-tracker heartbeat --config demo-tracker/project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --lease-seconds 7200
```

Complete the task with evidence:

If the task changed tracked code, docs, config, tests, or task plans, finish the
code-review closeout before completing it in the tracker. By default, that means
the scoped work is on a task branch, has a commit, and has a PR or equivalent
review surface. Local file paths, validation commands, or an unmerged worktree
are supporting context, not enough evidence by themselves.

Trusted project managers may use a direct-merge override for local workflows:
merge the task branch into `main`, push `main` when a remote is configured, and
record `git:<main-commit>` evidence. Use the override deliberately; ordinary
agent work should leave a PR or equivalent review state before tracker
completion.

```bash
agent-tracker complete --config demo-tracker/project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --evidence "git:<branch-or-main-commit>" \
  --evidence "pr:https://github.com/org/repo/pull/123" \
  --evidence "file:README.md"
```

Check what unblocked in live state:

```bash
agent-tracker next --config demo-tracker/project.json --limit 5
```

Before a future `import`, update the completed task's status in `tasks.json` to
`done` or keep terminal status in another authoritative importer source. This
keeps completed work from being reopened by the next sync.

Export an audit snapshot when you need a bounded artifact for review or another
system:

```bash
agent-tracker export --config demo-tracker/project.json
```

## CLI Reference

Every command accepts `--config <project.json>`. If `--config` is omitted, the
CLI uses `AGENT_TRACKER_CONFIG` as the default config path. Every command also
accepts `--db <path>` to override the SQLite database path from config; if
`--db` is omitted, the CLI uses `AGENT_TRACKER_DB` when set. Explicit CLI
arguments always take precedence over environment defaults. `init-project` is
the setup exception because it creates the project config.

For a local project shell:

```bash
export AGENT_TRACKER_CONFIG=demo-tracker/project.json
agent-tracker status
agent-tracker next --role maintainer
```

For wrapper scripts that need an isolated database:

```bash
AGENT_TRACKER_CONFIG=demo-tracker/project.json \
AGENT_TRACKER_DB=/tmp/demo-agent-tracker.sqlite \
agent-tracker status --json
```

| Command | Purpose | Example |
| --- | --- | --- |
| `init-project` | Create a plugin-free project layout with config, task plan, spool, exports, and runtime ignores. | `agent-tracker init-project demo-tracker --project-id demo` |
| `init` | Create or update the project row and database schema. | `agent-tracker init --config demo-tracker/project.json` |
| `import` | Import tasks and dependencies from the configured importer. | `agent-tracker import --config demo-tracker/project.json` |
| `status` | Show project counts; add `--json` for full task state. | `agent-tracker status --config demo-tracker/project.json --json` |
| `overview` | Show grouped ready, active, review, integration, blocked, and recent completion work; add `--json` for grouped task dictionaries. | `agent-tracker overview --config demo-tracker/project.json --limit 5` |
| `next` | List ready tasks, optionally filtered by repo or role. | `agent-tracker next --config demo-tracker/project.json --role maintainer --limit 1` |
| `task` | Show one task's prompt/context; add `--json` for stored state. | `agent-tracker task --config demo-tracker/project.json write-readme --markdown` |
| `claim` | Atomically claim a ready task and create a lease token. | `agent-tracker claim --config demo-tracker/project.json --agent agent-1 --role maintainer --lease-seconds 7200` |
| `heartbeat` | Extend a live lease and mark the task `in_progress`. | `agent-tracker heartbeat --config demo-tracker/project.json write-readme --lease-token <token> --agent agent-1` |
| `complete` | Mark a leased task `done` and record evidence URIs. | `agent-tracker complete --config demo-tracker/project.json write-readme --lease-token <token> --evidence "git:<branch-sha>" --evidence "pr:<url>"` |
| `submit-review` | Move a leased task into review wait state. | `agent-tracker submit-review --config demo-tracker/project.json write-readme --lease-token <token> --agent agent-1 --evidence "pr:<url>"` |
| `await-integration` | Move a leased task into PR, merge, or integration wait state. | `agent-tracker await-integration --config demo-tracker/project.json write-readme --lease-token <token> --agent agent-1 --status awaiting_merge` |
| `resolve-review` | Resolve a task waiting for review as `done` or `failed`. | `agent-tracker resolve-review --config demo-tracker/project.json write-readme --agent reviewer --evidence "review:approved"` |
| `resolve-integration` | Resolve a task waiting for integration as `done` or `failed`. | `agent-tracker resolve-integration --config demo-tracker/project.json write-readme --agent reviewer --evidence "git:<main-sha>"` |
| `fail` | Mark a leased task `failed` with a reason. | `agent-tracker fail --config demo-tracker/project.json write-readme --lease-token <token> --reason "validation failed"` |
| `ingest-event` | Ingest one JSON event file. | `agent-tracker ingest-event --config demo-tracker/project.json event.json --actor callback` |
| `pull-spool` | Copy complete JSON files from `spool.remote_inbox` to the local spool inbox; add `--dry-run` to preview. | `agent-tracker pull-spool --config demo-tracker/project.json --dry-run` |
| `ingest-spool` | Ingest all `*.json` files from the configured local spool inbox. | `agent-tracker ingest-spool --config demo-tracker/project.json --actor spool` |
| `list-workspaces` | List configured cross-project worker workspaces. | `agent-tracker list-workspaces --config demo-tracker/project.json` |
| `launch-worker` | Prepare or run a one-shot local worker in a configured workspace. | `agent-tracker launch-worker --config demo-tracker/project.json --workspace hpc --task-id write-readme` |
| `intake record` | Record raw ideas, features, checks, or planning notes without creating claimable tasks. | `agent-tracker intake --config demo-tracker/project.json record --kind feature --tag inbox "Add triage workflow"` |
| `intake list` | List raw intake records for later project-manager triage. | `agent-tracker intake --config demo-tracker/project.json list --json` |
| `intake update` | Mark intake as `triaged`, `closed`, `deferred`, or `open`. | `agent-tracker intake --config demo-tracker/project.json update <intake-id> --status closed` |
| `propose-task` | Create a reviewed proposed task contract from an intake record without importing it as a live task. | `agent-tracker propose-task --config demo-tracker/project.json <intake-id> --task-id add-triage --title "Add triage"` |
| `promote-proposal` | Promote a proposed task into live queue state so it appears in `next` and can be claimed. | `agent-tracker promote-proposal --config demo-tracker/project.json <proposal-id>` |
| `list-proposals` | List proposed task contracts awaiting review or promotion. | `agent-tracker list-proposals --config demo-tracker/project.json --json` |
| `export` | Write the configured audit snapshot through the exporter. | `agent-tracker export --config demo-tracker/project.json` |

See [docs/operations.md](docs/operations.md) for lifecycle details, stale lease
recovery, spool ingestion, and exports.

## Configuration And Task Plans

Config is JSON. Relative paths are resolved relative to the directory containing
the config file, not the shell's current working directory. The built-in config
fields and task-plan format are documented in:

- [docs/configuration.md](docs/configuration.md)
- [docs/task-plans.md](docs/task-plans.md)
- [docs/notebooks.md](docs/notebooks.md)

Commit config and task plans. Ignore local runtime files such as SQLite
databases, spool contents, virtual environments, and generated snapshots unless
a project explicitly asks for a bounded export.

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
objects with `event_id`, `id`, `run_id`, or `job_id` as stable identifiers;
explicit `event_id` and `id` values take precedence over HPC callback aliases.
It accepts optional `kind`, or `event_type` when `kind` is absent, optional
`task_id`, and any additional payload fields. The original payload is stored
unchanged with the normalized event.

Local spool ingestion is the simplest way to bridge asynchronous tools into the
queue. Write one event JSON object per file into the configured spool inbox and
run:

```bash
agent-tracker ingest-spool --config demo-tracker/project.json --actor ci
```

Valid or duplicate JSON files move to `done`; files that cannot be parsed or
normalized move to `error`.

When another process writes events to a shared filesystem, configure
`spool.remote_inbox` and run `pull-spool` before `ingest-spool`. `pull-spool`
also supports opt-in `ssh://` and `sftp://` remote inboxes when the optional
`ssh` extra is installed. It copies complete `*.json` files into the local
inbox, skips `.partial`, `.part`, and `.tmp` names, leaves remote files in
place, skips identical files already present in the local inbox/done/error
paths, and reports conflicts instead of overwriting different local files.

## Cross-Project Workspaces

Projects that coordinate work across local repositories can configure named
worker workspaces:

```json
{
  "workspaces": {
    "hpc": {
      "kind": "local",
      "path": "~/Documents/hpc-ci-project-tracker",
      "config_path": "agent-tracker.config.json",
      "spool_outbox": ".agent-tracker/spool/outbox",
      "artifacts_path": "results/worker-launches",
      "capabilities": ["local-worker", "summary-test"]
    }
  }
}
```

Inspect configured workspaces with:

```bash
agent-tracker list-workspaces --config tracking/project.json
```

`launch-worker` renders a task prompt or accepts a literal prompt, writes
prompt/report/launch artifacts under the configured workspace path, and can run either
the workspace's `worker_command` or a command supplied on the CLI:

```bash
agent-tracker launch-worker --config tracking/project.json \
  --workspace hpc \
  --task-id collect-status \
  --execute
```

The default command is a local `codex exec` one-shot. Current `launch-worker`
execution is local-only. SSH/SFTP support exists for pulling remote spool
events; remote queue mutation remains mediated by the task-ingest command
contract and processor work.

Evidence is stored as URI-like strings such as `git:<sha>`, `file:README.md`,
`pr:https://github.com/org/repo/pull/123`, or `artifact:s3://bucket/key`.
Large artifacts should be linked, not copied into the tracker.

Raw intake records capture ideas, feature requests, checks, and planning notes
without making them claimable tasks:

```bash
agent-tracker intake --config demo-tracker/project.json record \
  --kind feature --source user --tag triage \
  "Add an inbox for untriaged requests"
agent-tracker intake --config demo-tracker/project.json list --json
```

The grouped `intake` command is implemented with Typer. The flat aliases
`record-intake`, `list-intake`, and `update-intake` remain available for scripts
and produce the same JSON payloads. Without `--json`, `intake list` prints each
record with its current status, such as `status open`, `status triaged`,
`status closed`, or `status deferred`.

Intake records are included in snapshots for project-manager triage, but they do
not appear in ready-task listings and cannot be claimed.

Project-manager triage can turn an intake item into a proposed task contract
without adding it to the live queue:

```bash
agent-tracker propose-task --config demo-tracker/project.json <intake-id> \
  --task-id add-triage --title "Add triage workflow" \
  --role maintainer --write-scope src/agent_tracker/service.py \
  --validation-check "uv run pytest"
agent-tracker list-proposals --config demo-tracker/project.json --json
```

Proposals are durable review artifacts. A later promotion workflow can convert
approved proposals into task-plan entries or another authoritative importer.

Snapshots include evaluated task state, events, and audit log entries:

```bash
agent-tracker export --config demo-tracker/project.json
```

## Codex Skills

The package vendors reusable Codex skills:

- `project-manager`: triage intake, report status, tidy planning, and propose
  or promote tasks without taking a worker lease.
- `agent-coordinator`: run an agent-tracker project end to end, including queue
  health checks, lease checks, task planning, worker/review coordination, and
  closeout evidence. When the user asks for agent coordination or subagents, it
  normally delegates bounded implementation, review, test, or evidence work
  while keeping queue state, leases, integration decisions, and final evidence.
- `task-worker`: implement exactly one claimed task with scoped edits, focused
  checks, and handoff or closeout evidence.

After installing `agent-tracker`, install or refresh all vendored skills with:

```bash
agent-tracker-install-skill --all --overwrite
```

Install a subset by passing `--name` more than once:

```bash
agent-tracker-install-skill --name agent-coordinator --name task-worker
```

By default, this copies skills into `$CODEX_HOME/skills` or `~/.codex/skills`.
For backward compatibility, running the command with no `--name` or `--all`
installs `project-manager`. Use `--destination-root`, `--overwrite`, or
`--dry-run` when needed.

Project-specific trackers should consume these skills as generic workflows and
put local policy in project-owned files: `tracking/README.md`, project or repo
notebooks, plugins, or a small wrapper command documented by that repository.
For example, `hpc-ci-project-tracker` should install or vendor the generic
skills unchanged, then document any cluster-specific queues, validation suites,
or wrapper commands in its own tracker repo. Keep those details out of the
packaged skills so new projects can reuse them safely.

## Self-Dogfooding

This repository tracks its own implementation work with `agent-tracker`. The
committed task plan is [tracking/tasks.json](tracking/tasks.json), the project
config is [tracking/project.json](tracking/project.json), and the local SQLite
database is ignored runtime state.

Use [tracking/README.md](tracking/README.md) when asking an agent to pull the
next task, log work, complete work, or repair claim failures.

## Documentation Site

The browsable documentation is built with Sphinx and MyST from the Markdown
files in `docs/`, plus selected repository instructions such as the root
README, `tracking/README.md`, and the vendored skill docs.

Build the site locally with:

```bash
uv sync --group docs
uv run sphinx-build -W --keep-going -b html docs docs/_build/html
```

GitHub Actions builds the same HTML docs on pull requests and publishes them to
GitHub Pages on pushes to `main` or `master`.

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
