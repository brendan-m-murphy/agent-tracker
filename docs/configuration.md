# Configuration Reference

An `agent-tracker` project is configured by a JSON file passed to every CLI
command with `--config`.

```bash
agent-tracker status --config path/to/project.json
```

All relative paths in the config are resolved relative to the directory
containing the config file. This keeps commands stable no matter where they are
run from.

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

## Full Local Config

```json
{
  "project_id": "demo",
  "name": "Demo Tracker",
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
    "error": "spool/error"
  }
}
```

## Fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `project_id` | Yes | None | Stable identifier for the project in SQLite, events, evidence, and snapshots. |
| `name` | No | `project_id` | Human-readable project name used in status and rendered prompts. |
| `db_path` | No | `.agent-tracker/state.sqlite` | SQLite database path for live state. Relative paths resolve beside the config file. |
| `task_plan_path` | For built-in importer | None | JSON task plan path used by `JsonTaskImporter`. |
| `importer` | No | `agent_tracker.importers:JsonTaskImporter` | Plugin that returns task and dependency records. |
| `prompt_renderer` | No | `agent_tracker.rendering:DefaultPromptRenderer` | Plugin that renders task context for agents. |
| `event_adapter` | No | Built-in generic event normalization | Plugin that converts incoming event JSON into an `EventRecord`. |
| `exporter` | No | `agent_tracker.exporters:JsonSnapshotExporter` | Plugin that writes audit snapshots. |
| `export_path` | No | `agent-tracker-snapshot.json` | Output path used by the default JSON exporter. |
| `spool` | No | None | Local spool paths for `ingest-spool`. |
| `spool_inbox` | No | None | Legacy top-level inbox path used when `spool` is absent. |
| `spool_done` | No | `<inbox>/done` | Legacy top-level done path used when `spool` is absent. |
| `spool_error` | No | `<inbox>/error` | Legacy top-level error path used when `spool` is absent. |

## Path Resolution

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

Absolute paths and `~` are also supported:

```json
{
  "project_id": "example",
  "db_path": "~/agent-tracker/example.sqlite"
}
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
    "error": "spool/error"
  }
}
```

`agent-tracker ingest-spool` reads `*.json` files from `inbox`. Valid event
files move to `done`; files that raise an error move to `error`.

If `done` or `error` is omitted, defaults are created below the inbox:

```json
{
  "spool": {
    "inbox": "spool/inbox"
  }
}
```

The current spool implementation is local-only. It does not copy files from a
remote machine or run a daemon.

## Current Validation Behavior

Current config loading is intentionally lightweight. Missing required keys,
invalid JSON, invalid plugin specs, and SQLite setup errors are reported by the
CLI as concise `error: ...` messages. A future schema-versioning task will add
stronger config validation and migrations.
