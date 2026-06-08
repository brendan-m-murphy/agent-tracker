# Agent Tracker Self-Dogfooding

This directory lets `agent-tracker` manage implementation work for this
repository. The committed source of truth is `tracking/tasks.json`; the live
SQLite database is local runtime state and is ignored by git.

## Pull The Next Available Task

Use this workflow when a user asks an agent to pull the next available task:

```bash
uv run agent-tracker import --config tracking/project.json
uv run agent-tracker next --config tracking/project.json --role maintainer --limit 1
uv run agent-tracker claim --config tracking/project.json --agent <agent-id> --role maintainer --lease-seconds 7200
uv run agent-tracker task --config tracking/project.json <task-id> --markdown
```

The claim command prints a `lease_token`. Keep that token for heartbeat,
complete, or fail commands.

If pulling the next task fails, investigate before handing the problem back:

- Run `uv run agent-tracker status --config tracking/project.json --json`.
- Re-run `uv run agent-tracker import --config tracking/project.json` to sync
  from the committed task plan.
- Check whether no task is ready because dependencies are still blocked.
- Check whether the requested `--role` is absent from task metadata.
- Check whether a stale claim exists; status and task-state reads recover stale
  leases automatically.
- If the config, task plan, CLI, importer, or store behavior is broken, repair
  the problem with focused tests.

Only report a blocker after confirming that the queue state is valid and no
repairable repo issue is preventing the claim.

## Keep Work Logged

Heartbeat during longer tasks:

```bash
uv run agent-tracker heartbeat --config tracking/project.json <task-id> --lease-token <lease-token> --agent <agent-id>
```

Record work by adding concise evidence when completing a task:

```bash
uv run agent-tracker complete --config tracking/project.json <task-id> --lease-token <lease-token> --agent <agent-id> --evidence "git:<commit-or-branch>" --evidence "file:docs/example.md"
```

For intermediate work logs, write an event JSON file into
`tracking/spool/inbox/` and ingest the spool:

```json
{
  "event_id": "worklog-<task-id>-<timestamp>",
  "kind": "worklog",
  "task_id": "<task-id>",
  "payload": {
    "summary": "What changed or what was learned.",
    "files": ["path/to/file"],
    "commands": ["uv run pytest"],
    "next": "Useful next step or unresolved question."
  }
}
```

```bash
uv run agent-tracker ingest-spool --config tracking/project.json --actor <agent-id>
```

If a task cannot be completed, fail it with a concrete reason:

```bash
uv run agent-tracker fail --config tracking/project.json <task-id> --lease-token <lease-token> --agent <agent-id> --reason "Short actionable reason."
```

## Create Or Update Tasks

New deterministic work items currently live in `tracking/tasks.json`.

When adding a task:

- Use a stable lowercase id, for example `pull-spool-command`.
- Set `status` to `pending` unless the work is already complete.
- Pick a priority that preserves the intended sequence.
- Add `requirements` for real dependencies.
- Add `metadata.roles` so agents can claim it by role.
- Add `metadata.write_scopes` to describe expected files or modules.
- Add validation checks that a future agent can run.

After editing `tracking/tasks.json`, run:

```bash
uv run agent-tracker import --config tracking/project.json
uv run agent-tracker status --config tracking/project.json
```

Do not remove task entries casually. Importing synchronizes the database to the
task plan, so deleted tasks are removed from live state.

## Skill Note

This file is the repo-local workflow guide for now. A Codex skill may be useful
after the command flow stabilizes, but keeping the first version in-repo makes
the dogfooding rules reviewable with the code they coordinate.
