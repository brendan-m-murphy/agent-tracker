# Task Plan Format

The built-in `JsonTaskImporter` reads a project task plan from the config field
`task_plan_path`. The file contains a top-level `tasks` array.

```json
{
  "tasks": [
    {
      "id": "write-readme",
      "title": "Write the README",
      "repo": "demo-app",
      "status": "pending",
      "priority": 10,
      "summary": "Document the local setup workflow.",
      "execution": {
        "primary_files": ["README.md"],
        "notes": "Keep examples copy-pasteable."
      },
      "validation_checks": ["Manual review: setup commands are complete."],
      "next_action": "Draft the setup and usage sections.",
      "metadata": {
        "roles": ["maintainer"],
        "write_scopes": ["README.md"]
      }
    }
  ]
}
```

## Design A Claimable Task

A useful task should answer five questions before an agent claims it:

- what stable `id` identifies the work;
- which `repo` or component owns the change;
- what concrete result is required in `summary` and `next_action`;
- which files or directories are expected in `execution.primary_files` or
  `metadata.write_scopes`;
- which validation checks prove the work is complete.

Keep task entries small and deterministic. Put durable planning context in docs
or notebook files and link to it with `prompt_path`; avoid pasting large logs or
raw artifacts into the task plan.

## Task Fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `id` | Yes | None | Stable task identifier. |
| `title` | No | `id` | Human-readable task title. |
| `repo` | No | Empty string | Repository or component the task applies to. Used by `next --repo` and `claim --repo`. |
| `status` | No | `pending` | Imported manual status. See [Statuses](#statuses). |
| `priority` | No | `9999` | Lower numbers are returned first by `next` and `claim`. |
| `prompt_key` | No | Empty string | Project-defined prompt lookup key. Stored for plugins. |
| `prompt_path` | No | Empty string | Project-defined path to source context. Stored for plugins. |
| `summary` | No | Empty string | Short task purpose included in the default prompt. |
| `execution` | No | `{}` | Free-form object for work instructions. Included in the default prompt. |
| `validation_checks` | No | `[]` | Commands or manual checks needed before completion. |
| `next_action` | No | Empty string | Immediate action shown by `next` and the default prompt. |
| `evidence` | No | `[]` | Initial evidence URIs imported with the task. |
| `metadata` | No | `{}` | Free-form object for role filters, write scopes, authority notes, or plugin-specific data. |
| `requirements` | No | `[]` | Dependencies that must be satisfied before the task is ready. |

## Statuses

Task plans store manual statuses. `ready` and `blocked` are computed by the
tracker and should not be written into the task plan.

Valid imported statuses:

- `pending`: Eligible to become `ready` when dependencies are done.
- `claimed`: Active work with a lease.
- `in_progress`: Active work after a heartbeat.
- `waiting_evidence`: Active work waiting on external evidence.
- `done`: Completed task.
- `failed`: Failed task.
- `deferred`: Not ready for automatic claim.
- `cancelled`: Terminal task that will not run.

Computed states:

- `ready`: A `pending` task with no unsatisfied dependencies.
- `blocked`: A `pending` task with at least one dependency that is not `done`.

## Dependencies

Only task dependencies are supported by the built-in JSON importer today:

```json
{
  "id": "review-readme",
  "title": "Review the README",
  "status": "pending",
  "priority": 20,
  "requirements": [
    {
      "kind": "task",
      "task": "write-readme",
      "description": "README draft is complete."
    }
  ]
}
```

A dependency is satisfied only when the dependency task's stored status is
`done`. When a completed dependency unblocks downstream work, the downstream
task appears in `next`.

Imports validate that:

- every task has a non-empty `id`;
- task IDs are unique;
- every status is valid;
- every dependency points to a task that exists in the same imported plan.

## Role And Repo Filtering

`next` and `claim` can filter by repo and role:

```bash
agent-tracker next --config project.json --repo demo-app --role maintainer
agent-tracker claim --config project.json --agent agent-1 --repo demo-app --role maintainer
```

Role filtering checks `metadata.roles` first, then `metadata.allowed_roles`.
Either can be a string or a list:

```json
{
  "metadata": {
    "roles": ["maintainer", "docs"]
  }
}
```

If no role is provided, all ready tasks can match. If a role is provided and the
task metadata does not include it, the task is skipped.

Use role filters to keep specialized queues clear. For example, documentation
work can include `["maintainer", "docs"]`, while implementation work can
include `["maintainer", "python"]`.

## Suggested Metadata

The core package does not enforce authority or write scopes yet, but including
them makes task prompts and reviews clearer:

```json
{
  "metadata": {
    "roles": ["maintainer"],
    "write_scopes": ["src/agent_tracker/config.py", "tests/test_agent_tracker.py"],
    "authority": "local code and docs only",
    "requires_human_approval": false
  }
}
```

Recommended metadata keys:

- `roles`: Agent roles allowed to claim the task.
- `write_scopes`: Files or directories the task is expected to touch.
- `authority`: Short description of what the assignee may do.
- `validation`: Extra project-specific validation notes.
- `dogfood`: Boolean marker for tasks used to validate `agent-tracker` itself.

Metadata is advisory in the current core package, except for role filtering.
It is still worth keeping accurate because rendered prompts, reviews, and
project-local plugins can use it to enforce local conventions.

## Import Semantics

Importing synchronizes the live SQLite state with the task plan:

```bash
agent-tracker import --config project.json
```

During import:

- task rows are inserted or updated;
- dependencies are replaced with the imported dependency set;
- tasks removed from the task plan are removed from live state;
- evidence listed on a task is inserted if not already present;
- live leases are preserved when the imported task remains active or `pending`;
- terminal imported statuses such as `done`, `failed`, or `cancelled` clear any
  live lease.

Do not delete task entries casually. The import is authoritative for the active
task set and for imported manual statuses. If live SQLite state says a task is
`done` but the imported task plan still says `pending`, the next import can
reopen that task as pending.

When a task changes tracked repository files, do not mark it complete until the
work has been integrated or made reviewable. Store that evidence on completion,
for example `git:<main-sha>` or `pr:https://github.com/org/repo/pull/123`.
If the task plan is the authoritative source for your project, update the
completed task's imported status to `done` in the same integrated change.

## Prompt Rendering

The default prompt includes:

- project name;
- task ID and computed state;
- repo;
- summary;
- key/value entries from `execution`;
- dependency status;
- validation checks;
- next action.

Render a prompt with:

```bash
agent-tracker task --config project.json write-readme --markdown
```

For project-specific prompt text, configure a custom `prompt_renderer`. See
[plugins.md](plugins.md).
