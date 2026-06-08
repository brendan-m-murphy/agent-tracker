# agent-tracker

`agent-tracker` is a generic local coordination queue for agent-managed
projects. It stores live state in SQLite and exposes a small CLI plus
MCP-friendly Python handlers. Project-specific behavior belongs in config files
and plugins.

The package intentionally does not know about any particular project, scheduler,
HPC environment, repository layout, or validation convention.

## First Local Flow

```bash
agent-tracker init --config path/to/project.json
agent-tracker import --config path/to/project.json
agent-tracker status --config path/to/project.json
agent-tracker next --config path/to/project.json
agent-tracker claim --config path/to/project.json --agent codex
```

## Project Config

Project config is JSON. Paths are resolved relative to the config file unless
absolute.

```json
{
  "project_id": "example",
  "name": "Example Project",
  "db_path": ".agent-tracker/state.sqlite",
  "task_plan_path": "tasks.json",
  "importer": "agent_tracker.importers:JsonTaskImporter",
  "prompt_renderer": "agent_tracker.rendering:DefaultPromptRenderer",
  "exporter": "agent_tracker.exporters:JsonSnapshotExporter",
  "export_path": "exports/agent-tracker-snapshot.json"
}
```

