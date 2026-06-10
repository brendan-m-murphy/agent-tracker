# Configuration Reference

An `agent-tracker` project is configured by a JSON file passed to CLI commands
with `--config` or provided as an environment default.

```bash
agent-tracker status --config path/to/project.json
```

If `--config` is omitted, the CLI reads `AGENT_TRACKER_CONFIG`. If `--db` is
omitted, the CLI reads `AGENT_TRACKER_DB` as a SQLite database override.
Explicit `--config` and `--db` arguments always take precedence over these
environment defaults.

All relative paths in the config are resolved relative to the directory
containing the config file. This keeps commands stable no matter where they are
run from.

Projects that have multiple copied worktrees can separate task-definition files
from live runtime state. `task_source_root` controls where task plans are read
from. `state_root` controls where SQLite, spool, and exports are written.
`canonical_config_path` can require mutating commands to use one specific config
file.

## Create A Project Layout

For the common JSON-task case, create the standard layout with one command:

```bash
agent-tracker init-project tracking --project-id demo --name "Demo Tracker"
```

This creates:

- `tracking/project.json`
- `tracking/tasks.json`
- `tracking/.agent-tracker/`
- `tracking/spool/inbox`, `tracking/spool/done`, and `tracking/spool/error`
- `tracking/exports/`
- `tracking/notebooks/repos/`
- `tracking/.gitignore` entries for runtime state

Commit the config and task plan. Leave runtime files out of git unless a
project explicitly asks for an exported artifact:

- `tracking/.agent-tracker/state.sqlite` for the minimal config below, or
  `tracking/state.sqlite` when a project chooses that `db_path`;
- `tracking/spool/inbox/*.json`
- `tracking/spool/done/*.json`
- `tracking/spool/error/*.json`
- `tracking/exports/*.json`

If the project will be managed from copied worktrees, add
`--canonical-config`. The generated config then records absolute
`canonical_config_path`, `state_root`, and `task_source_root` values so mutating
commands must use the canonical config.

## Minimal Config

```json
{
  "project_id": "demo",
  "name": "Demo Tracker",
  "db_path": ".agent-tracker/state.sqlite",
  "task_plan_path": "tasks.json"
}
```

With this config, `agent-tracker` uses the built-in JSON task importer, default
prompt renderer, and JSON snapshot exporter.

Run it with:

```bash
agent-tracker import --config tracking/project.json
agent-tracker status --config tracking/project.json
```

For an interactive local shell, export the config once:

```bash
export AGENT_TRACKER_CONFIG=tracking/project.json
agent-tracker import
agent-tracker status
```

## Full Local Config

Plugin fields are optional. Include them only when you want to be explicit or
when replacing a built-in default with project-specific behavior.

```json
{
  "project_id": "demo",
  "name": "Demo Tracker",
  "canonical_config_path": "~/Documents/demo/tracking/project.json",
  "state_root": "~/Documents/demo/tracking",
  "task_source_root": "~/Documents/demo/tracking",
  "db_path": ".agent-tracker/state.sqlite",
  "task_plan_path": "tasks.json",
  "importer": "agent_tracker.importers:JsonTaskImporter",
  "prompt_renderer": "agent_tracker.rendering:DefaultPromptRenderer",
  "event_adapter": "plugins.events:DemoEventAdapter",
  "exporter": "agent_tracker.exporters:JsonSnapshotExporter",
  "export_path": "exports/snapshot.json",
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error",
    "remote_inbox": "/shared/demo/spool/outbox"
  },
  "coordination_policy": {
    "worktree_mode": "one_task_per_worktree",
    "pr_mode": "one_task_per_pr"
  },
  "workspaces": {
    "hpc": {
      "kind": "local",
      "path": "~/Documents/hpc-ci-project-tracker",
      "config_path": "agent-tracker.config.json",
      "spool_outbox": ".agent-tracker/spool/outbox",
      "artifacts_path": "results/worker-launches",
      "capabilities": ["local-worker"]
    }
  }
}
```

## Fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `project_id` | Yes | None | Stable identifier for the project in SQLite, events, evidence, and snapshots. |
| `name` | No | `project_id` | Human-readable project name used in status and rendered prompts. |
| `canonical_config_path` | No | None | Absolute or `~`-based config path required for mutating commands. Copied configs can still be used for read-only inspection. |
| `state_root` | No | Config directory | Base directory for runtime state paths such as SQLite, spool, and exports. |
| `task_source_root` | No | Config directory | Base directory for task-definition paths such as `task_plan_path`. |
| `db_path` | No | `.agent-tracker/state.sqlite` | SQLite database path for live state. Relative paths resolve below `state_root`. |
| `task_plan_path` | For built-in importer | None | JSON task plan path used by `JsonTaskImporter`. |
| `importer` | No | `agent_tracker.importers:JsonTaskImporter` | Plugin that returns task and dependency records. |
| `prompt_renderer` | No | `agent_tracker.rendering:DefaultPromptRenderer` | Plugin that renders task context for agents. |
| `event_adapter` | No | Built-in generic event normalization | Plugin that converts incoming event JSON into an `EventRecord`. |
| `exporter` | No | `agent_tracker.exporters:JsonSnapshotExporter` | Plugin that writes audit snapshots. |
| `export_path` | No | `agent-tracker-snapshot.json` | Output path used by the default JSON exporter. Relative paths resolve below `state_root`. |
| `spool` | No | None | Spool paths for `pull-spool` and `ingest-spool`. Relative paths resolve below `state_root`. |
| `spool_inbox` | No | None | Legacy top-level inbox path used when `spool` is absent. Relative paths resolve below `state_root`. |
| `spool_done` | No | `<inbox>/done` | Legacy top-level done path used when `spool` is absent. |
| `spool_error` | No | `<inbox>/error` | Legacy top-level error path used when `spool` is absent. |
| `coordination_policy` | No | Conservative defaults | Coordinator-managed implementation policy for task worktrees and PR mapping. |
| `workspaces` | No | None | Named local or SSH workspace registry for cross-project worker launch and diagnostics. |

Use stable `project_id` values. Evidence, events, audit entries, and snapshots
are all tied to that identifier.

## Path Resolution And Authority

If `tracking/project.json` contains:

```json
{
  "project_id": "example",
  "db_path": "state.sqlite",
  "task_plan_path": "tasks.json",
  "export_path": "exports/snapshot.json"
}
```

Then the resolved paths are:

- `tracking/state.sqlite`
- `tracking/tasks.json`
- `tracking/exports/snapshot.json`

If the same config also contains:

```json
{
  "state_root": "~/Documents/demo/tracking",
  "task_source_root": "~/Documents/demo/tracking",
  "canonical_config_path": "~/Documents/demo/tracking/project.json"
}
```

then runtime state and task definitions resolve through that canonical tree even
when an agent reads a copied config from another worktree. Mutating commands run
through the copied config fail with a concise error naming the canonical config.
Read-only commands such as `status`, `next`, and `task` can still inspect the
resolved database path.

Environment defaults do not bypass this authority model. A config path from
`AGENT_TRACKER_CONFIG` is still loaded through the same validation path as
`--config`, and mutating commands still refuse copied configs or database
overrides when `canonical_config_path` is set.

Sandbox and cache settings do not change state authority either. In Codex or
other managed runners, `uv run` may need approval to read or populate its cache
under the user's home directory. Prefer direct virtualenv commands such as
`.venv/bin/agent-tracker status --config tracking/project.json` for read-only
inspection after the environment has already been synced. A writable uv cache,
for example `uv --cache-dir /tmp/agent-tracker-uv-cache run ...`, can reduce
cache-path friction in some runners, but it does not grant permission to write a
canonical SQLite database or Git metadata outside the worktree.

Do not use `AGENT_TRACKER_DB` or a copied config to redirect mutating commands
to a sandbox-local database unless you are deliberately inspecting or testing a
separate throwaway project. Production coordination should keep leases,
evidence, and audit rows in the configured canonical database.

## Coordination Policy

`coordination_policy` lets a project choose how coordinator-managed
implementation maps tasks onto writable worktrees and PRs. When omitted, the
defaults are conservative:

```json
{
  "coordination_policy": {
    "worktree_mode": "one_task_per_worktree",
    "pr_mode": "one_task_per_pr"
  }
}
```

Allowed `worktree_mode` values:

- `one_task_per_worktree`: create or assign one non-canonical task worktree per
  implementation task.
- `shared_worktree_serial`: allow a shared non-canonical worktree only for
  serially related tasks with non-conflicting write scopes. Parallel agents
  must still use separate writable worktrees.

Allowed `pr_mode` values:

- `one_task_per_pr`: open one PR or equivalent review surface per task.
- `batch_pr_allowed`: allow an explicit batch or epic PR. The PR must list the
  covered task IDs, the batching rationale, and closeout evidence for each
  task.

These settings guide coordinators, project managers, and worker prompts. They
do not replace task leases, write scopes, completion policy metadata, or final
tracker evidence.

Absolute paths and `~` are also supported:

```json
{
  "project_id": "example",
  "db_path": "~/agent-tracker/example.sqlite"
}
```

Wrapper scripts can set a default database path without changing the committed
config:

```bash
AGENT_TRACKER_CONFIG=tracking/project.json \
AGENT_TRACKER_DB=/tmp/example-agent-tracker.sqlite \
agent-tracker status --json
```

## Downstream uv Preview Pins

Downstream uv projects can validate in-progress `agent-tracker` work by pinning
a temporary git ref in `pyproject.toml`. This is the recommended preview path
for uv projects because the ref is named, lockable, and reproducible. Avoid
adjacent-checkout path dependencies as the default workflow; reserve them for
local package development that is not meant to produce tracker validation
evidence.

Branch preview:

```toml
[project]
dependencies = ["agent-tracker"]

[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", branch = "preview/<feature-or-task>" }
```

Immutable preview:

```toml
[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", rev = "<commit-sha>" }
```

Refresh the downstream lock and run the downstream checks:

```bash
uv lock --upgrade-package agent-tracker
uv sync
uv run <downstream-validation-command>
```

After validation succeeds and the feature lands on `main` or a release is
available, replace the preview pin. For unreleased `main` validation:

```toml
[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", branch = "main" }
```

For a release:

```toml
[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", tag = "<release-tag>" }
```

If `agent-tracker` is consumed from a package index, remove the
`tool.uv.sources.agent-tracker` entry and keep only the normal dependency
specifier, then rerun `uv lock --upgrade-package agent-tracker`.

Preview refs are temporary validation channels. They do not define release
support, compatibility guarantees, or long-lived downstream policy.

## Plugin Specs

Plugin fields use `module:object` strings:

```json
{
  "importer": "plugins.tasks:ProjectImporter",
  "exporter": "plugins.exports:MarkdownExporter"
}
```

Before loading a plugin, `agent-tracker` adds the config directory to
`sys.path`. For example, if the config is `tracking/project.json`, a plugin spec
of `plugins.tasks:ProjectImporter` can load `tracking/plugins/tasks.py`.

See [plugins.md](plugins.md) for the Python protocols.

## Spool Config

Prefer the nested `spool` block:

```json
{
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error",
    "remote_inbox": "/shared/agent-tracker/spool/outbox"
  }
}
```

`agent-tracker ingest-spool` reads `*.json` files from `inbox`. Valid event
files move to `done`; files that raise an error move to `error`.
`agent-tracker pull-spool` copies complete `*.json` files from
`remote_inbox` into `inbox` before ingestion. It skips names ending in
`.partial`, `.part`, or `.tmp`, publishes local files through a temporary
non-JSON name, leaves remote files in place, skips identical local files already
present in `inbox`, `done`, or `error`, and reports conflicting local files
without overwriting them.

If `done` or `error` is omitted, defaults are created below the inbox:

```json
{
  "spool": {
    "inbox": "spool/inbox"
  }
}
```

`pull-spool` copies between configured filesystem paths by default. It also
accepts `ssh://` or `sftp://` `spool.remote_inbox` values when the optional
`ssh` extra is installed:

```json
{
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error",
    "remote_inbox": "sftp://agent@example.internal/var/spool/agent-tracker/outbox",
    "ssh": {
      "username": "agent",
      "client_keys": "~/.ssh/agent_tracker_ed25519",
      "known_hosts": "~/.ssh/known_hosts"
    }
  }
}
```

Use `uv run --extra ssh agent-tracker pull-spool ...` when operating from a
source checkout. `spool.ssh.known_hosts` may be set to `"none"` for isolated
loopback tests only. Production SSH/SFTP pulls should use host-key verification
and should not store secrets in committed config files.

`pull-spool` is a bounded copy step. It does not run as a daemon.

## Workspace Registry

The optional `workspaces` object gives coordinators stable names for local or
remote project checkouts. `list-workspaces` reports the resolved registry.
`launch-worker` currently executes only `kind: "local"` entries.

```json
{
  "workspaces": {
    "hpc": {
      "kind": "local",
      "path": "~/Documents/hpc-ci-project-tracker",
      "config_path": "agent-tracker.config.json",
      "spool_outbox": ".agent-tracker/spool/outbox",
      "artifacts_path": "results/worker-launches",
      "roles": ["agent-coordinator"],
      "capabilities": ["local-worker", "summary-test"],
      "worker_command": [
        "codex",
        "exec",
        "--cd",
        "{workspace_path}",
        "--output-last-message",
        "{report_path}",
        "-"
      ]
    }
  }
}
```

Local workspace paths are resolved relative to the tracker config unless they
are absolute or start with `~`. `config_path`, `spool_outbox`, and
`artifacts_path` are resolved relative to the workspace path. If
`artifacts_path` is omitted, worker artifacts are written below
`.agent-tracker/workers` inside the workspace.

`worker_command` can be a list of argv items or a shell-like string parsed with
`shlex`. It is not run through a shell. The launcher replaces these placeholders
inside argv items:

- `{agent_id}`
- `{launch_id}`
- `{project_id}`
- `{prompt_path}`
- `{report_path}`
- `{task_id}`
- `{workspace}`
- `{workspace_path}`

SSH workspace entries are validated and listed, but not launched yet:

```json
{
  "workspaces": {
    "remote-hpc": {
      "kind": "ssh",
      "host": "hpc-login",
      "remote_path": "/work/project",
      "spool_outbox": ".agent-tracker/spool/outbox"
    }
  }
}
```

Use SSH/SFTP `pull-spool` for remote event collection today. Remote queue
mutation should wait for the task-ingest command processor rather than letting
remote workers open canonical SQLite directly.

## Current Validation Behavior

Config loading validates the schema version, known string fields, spool fields,
SSH spool options, and workspace registry shapes. Invalid JSON, invalid plugin
specs, and SQLite setup errors are reported by the CLI as concise `error: ...`
messages.
