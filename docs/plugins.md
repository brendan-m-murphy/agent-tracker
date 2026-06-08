# Plugin Authoring

`agent-tracker` keeps project-specific behavior out of core by loading small
Python plugins from config. Plugin specs use `module:object` strings:

```json
{
  "importer": "plugins.tasks:ProjectImporter",
  "prompt_renderer": "plugins.prompts:ProjectPromptRenderer",
  "event_adapter": "plugins.events:ProjectEventAdapter",
  "exporter": "plugins.exports:ProjectExporter"
}
```

Before importing a plugin, `agent-tracker` adds the config directory to
`sys.path`. If your config is `tracking/project.json`, the module
`plugins.tasks` can live at `tracking/plugins/tasks.py`.

If the loaded object is a class, `agent-tracker` instantiates it with no
arguments. If it is already an object, that object is used directly.

## When To Write A Plugin

Start with the built-in JSON importer and default renderer when your project can
store tasks in `tasks.json`. Add a plugin when you need to:

- import tasks from an existing tracker, issue system, spreadsheet, or custom
  planning format;
- render richer task prompts with project notebooks, runbooks, or local context;
- normalize callback payloads into stable event IDs;
- export snapshots into a project-specific report format.

Keep plugins small and deterministic. They should adapt project data into core
records, not duplicate the SQLite queue or mutate unrelated files.

## Project-Local Layout

Plugin modules are resolved relative to the config directory. A common layout is:

```text
tracking/
  project.json
  tasks.json
  plugins/
    __init__.py
    tasks.py
    prompts.py
    events.py
    exports.py
```

With `tracking/project.json`, this config loads `tracking/plugins/tasks.py`:

```json
{
  "importer": "plugins.tasks:ProjectImporter"
}
```

## Shared Types

Plugins receive a `ProjectConfig` and return records from `agent_tracker.models`.

```python
from agent_tracker.config import ProjectConfig
from agent_tracker.models import DependencyRecord, EventRecord, TaskRecord, TaskState
```

`ProjectConfig` fields:

- `project_id`: stable project ID;
- `name`: human-readable name;
- `root`: directory containing the config file;
- `db_path`: resolved SQLite path;
- `raw`: original config dictionary.

Use `config.resolve_path("field_name")` for path-valued config fields.

## Task Importers

Config key: `importer`

Default: `agent_tracker.importers:JsonTaskImporter`

Protocol:

```python
class TaskImporter:
    def load_tasks(
        self, config: ProjectConfig
    ) -> tuple[list[TaskRecord], list[DependencyRecord]]:
        ...
```

Minimal project-local importer:

```python
from __future__ import annotations

import json

from agent_tracker.config import ProjectConfig
from agent_tracker.models import DependencyRecord, TaskRecord


class ProjectImporter:
    def load_tasks(
        self, config: ProjectConfig
    ) -> tuple[list[TaskRecord], list[DependencyRecord]]:
        path = config.resolve_path("task_plan_path")
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = [
            TaskRecord(
                task_id=str(item["id"]),
                title=str(item.get("title", item["id"])),
                repo=str(item.get("repo", "")),
                status=str(item.get("status", "pending")),
                priority=int(item.get("priority", 9999)),
                summary=str(item.get("summary", "")),
                validation_checks=[str(check) for check in item.get("validation_checks", [])],
                next_action=str(item.get("next_action", "")),
                metadata=dict(item.get("metadata", {})),
            )
            for item in data.get("tasks", [])
        ]
        dependencies: list[DependencyRecord] = []
        return tasks, dependencies
```

Importer responsibilities:

- produce stable, non-empty task IDs;
- set valid manual statuses;
- produce dependency records only for tasks that exist in the same import;
- keep project-specific formats out of core;
- avoid side effects beyond reading source planning data.

The store validates duplicate IDs, statuses, and dependency references before
mutating live state.

## Prompt Renderers

Config key: `prompt_renderer`

Default: `agent_tracker.rendering:DefaultPromptRenderer`

Protocol:

```python
class PromptRenderer:
    def render_prompt(
        self, config: ProjectConfig, state: TaskState, *, markdown: bool = False
    ) -> str:
        ...
```

Minimal renderer:

```python
from __future__ import annotations

from agent_tracker.config import ProjectConfig
from agent_tracker.models import TaskState


class ProjectPromptRenderer:
    def render_prompt(
        self, config: ProjectConfig, state: TaskState, *, markdown: bool = False
    ) -> str:
        task = state.task
        lines = [
            f"# {task.title}",
            "",
            f"Project: {config.name}",
            f"Task: {task.task_id}",
            f"State: {state.state}",
            "",
            task.summary,
            "",
            "Validation:",
            *(f"- {check}" for check in task.validation_checks),
        ]
        return "\n".join(lines).rstrip() + "\n"
```

Renderer responsibilities:

- include enough context for an agent to act without reading the task plan first;
- include dependency state when it matters;
- include validation checks and next action;
- keep rendered output deterministic and bounded.

## Event Adapters

Config key: `event_adapter`

Default: generic normalization from the event JSON object.

Protocol:

```python
class EventAdapter:
    def normalize_event(self, config: ProjectConfig, payload: dict) -> EventRecord:
        ...
```

Minimal adapter:

```python
from __future__ import annotations

from agent_tracker.config import ProjectConfig
from agent_tracker.models import EventRecord


class ProjectEventAdapter:
    def normalize_event(self, config: ProjectConfig, payload: dict) -> EventRecord:
        run_id = str(payload["run_id"])
        return EventRecord(
            event_id=f"run-{run_id}-{payload.get('status', 'unknown')}",
            kind="validation.run",
            task_id=str(payload.get("task_id", "")),
            payload=payload,
        )
```

Adapter responsibilities:

- return a non-empty `event_id`;
- make `event_id` stable so repeated ingestion is idempotent;
- set a useful `kind`;
- attach `task_id` when the event belongs to a task;
- preserve the original payload or a bounded normalized form.

Events are recorded idempotently by `event_id`. Duplicate event IDs are ignored.

## Exporters

Config key: `exporter`

Default: `agent_tracker.exporters:JsonSnapshotExporter`

Protocol:

```python
class Exporter:
    def export(self, config: ProjectConfig, snapshot: dict) -> list[str]:
        ...
```

Minimal exporter:

```python
from __future__ import annotations

import json

from agent_tracker.config import ProjectConfig


class ProjectExporter:
    def export(self, config: ProjectConfig, snapshot: dict) -> list[str]:
        path = config.resolve_path("export_path", "exports/snapshot.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        return [str(path)]
```

Exporter responsibilities:

- treat SQLite as canonical live state;
- write derived audit artifacts;
- return every created or updated path;
- link to large artifacts instead of copying raw logs into exports;
- keep output deterministic enough for review.

## Follow-Up Planners

Protocol:

```python
class FollowupPlanner:
    def propose_followups(self, config: ProjectConfig, event: EventRecord) -> list[TaskRecord]:
        ...
```

The protocol exists so projects can define deterministic follow-up proposals
from events or completed work. The current CLI does not call follow-up planners
yet; a planned task will wire proposal storage and review into the service.

When implementing a planner ahead of that wiring, keep it side-effect free:

- return task proposals rather than writing active tasks;
- include roles, write scopes, validation checks, and dependencies;
- preserve evidence links in task summaries or metadata;
- keep human approval separate from automatic task creation.

## Testing Plugins

A simple test can exercise project-local plugin loading without installing a
package:

```python
from agent_tracker.config import load_config
from agent_tracker.service import Coordinator


def test_project_importer(tmp_path):
    config = load_config(tmp_path / "project.json")
    coord = Coordinator(config)
    assert coord.import_tasks() == 1
```

Use temporary project directories with config files and plugin modules laid out
the same way users will run them.
