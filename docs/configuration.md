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

## Current Validation Behavior

Current config loading is intentionally lightweight. Missing required keys,
invalid JSON, invalid plugin specs, and SQLite setup errors are reported by the
CLI as concise `error: ...` messages. A future schema-versioning task will add
stronger config validation and migrations.
