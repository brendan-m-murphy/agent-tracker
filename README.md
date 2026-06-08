# agent-tracker

`agent-tracker` is a generic local coordination queue for agent-managed
projects. It stores live state in SQLite and exposes a small CLI plus
MCP-friendly Python handlers. Project-specific behavior belongs in config files
and plugins.

The package intentionally does not know about any particular project, scheduler,
HPC environment, repository layout, or validation convention.

See [docs/planning.md](docs/planning.md) for the roadmap, missing pieces,
future architecture options, and recommendations from the transcript review.

## First Local Flow

```bash
agent-tracker init --config path/to/project.json
agent-tracker import --config path/to/project.json
agent-tracker status --config path/to/project.json
agent-tracker next --config path/to/project.json
agent-tracker claim --config path/to/project.json --agent codex
```

## Development

This package uses `uv` for local development:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

## Self-Dogfooding

This repository can track its own implementation work with `agent-tracker`.
See [tracking/README.md](tracking/README.md) for the workflow to import the
task plan, pull the next available task, log work, and add follow-up tasks.

The package also vendors a generic Codex skill named `project-manager`. After
installation, copy it into a Codex skills directory with:

```bash
agent-tracker-install-skill --name project-manager
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
