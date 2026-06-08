# Agent Tracker Self-Dogfooding

This directory lets `agent-tracker` manage implementation work for this
repository.

- Project config: `tracking/project.json`
- Committed task plan: `tracking/tasks.json`
- Local live database: `tracking/state.sqlite`
- Local spool inbox: `tracking/spool/inbox`
- Local spool done/error folders: `tracking/spool/done`, `tracking/spool/error`
- Snapshot export: `tracking/exports/snapshot.json`

The SQLite database, spool files, and exports are runtime state unless a task
explicitly asks you to commit a bounded export.

## Pull The Next Available Task

Use this workflow when a user asks an agent to pull the next available task.
The default role for this repo is `maintainer`.

```bash
uv run agent-tracker import --config tracking/project.json
uv run agent-tracker next --config tracking/project.json --role maintainer --limit 1
uv run agent-tracker claim --config tracking/project.json --agent <agent-id> --role maintainer --lease-seconds 7200
uv run agent-tracker task --config tracking/project.json <task-id> --markdown
```

Use a clear `agent-id`, such as `codex-<worktree-name>` or a short thread ID.
The claim command prints a `lease_token`. Keep that token for `heartbeat`,
`complete`, or `fail`.

## While Working

Heartbeat during longer tasks:

```bash
uv run agent-tracker heartbeat --config tracking/project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --lease-seconds 7200
```

Record concise work logs through the local spool when useful. Write a JSON file
to `tracking/spool/inbox/`:

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

Then ingest it:

```bash
uv run agent-tracker ingest-spool --config tracking/project.json --actor <agent-id>
```

Do not store large raw command outputs in the tracker. Link to commits, PRs,
reports, screenshots, or bounded summaries instead.

## Complete A Task

Run the validation checks listed in the rendered task prompt. For this
repository, most implementation tasks use:

```bash
uv run pytest
uv run ruff check .
```

Documentation tasks may also require manual review. For example:

- new-user review: a user can create a minimal config and task plan from the
  docs alone;
- dogfood review: an agent can pull the next task from this runbook alone.

### Integration Gate

For tasks that change tracked code, docs, config, tests, or task plans, do not
mark the tracker task complete while the work exists only in an unmerged
worktree. First make the work accessible to others:

- commit the scoped changes on a task branch;
- merge the task branch into `main` or open a PR when direct merge is not the
  intended workflow;
- push the branch or `main` when a remote is configured;
- use integrated evidence, such as `git:<main-commit>` or `pr:<url>`, in the
  completion command.

If integration is blocked, keep the task active with heartbeats or fail it with
an actionable reason. Local validation evidence is necessary, but it is not
sufficient for completion when the task changed repository files.

Complete the task with concise evidence:

```bash
uv run agent-tracker complete --config tracking/project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --evidence "git:<main-commit-or-merged-branch>" \
  --evidence "file:<path>"
```

Use evidence that future reviewers can inspect. Good examples:

- `git:<main-commit>`
- `file:README.md`
- `file:docs/operations.md`
- `pr:https://github.com/<owner>/<repo>/pull/<number>`

## Fail A Task

Only fail a claimed task after investigating whether the blocker is repairable.
Use a short, actionable reason:

```bash
uv run agent-tracker fail --config tracking/project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --reason "Short actionable reason."
```

## Repair Claim Problems

If pulling or claiming the next task fails, investigate before reporting a
blocker.

Check queue state:

```bash
uv run agent-tracker status --config tracking/project.json --json
```

Sync from the committed task plan:

```bash
uv run agent-tracker import --config tracking/project.json
```

List ready tasks without filters:

```bash
uv run agent-tracker next --config tracking/project.json --limit 10 --json
```

List ready tasks with the intended role:

```bash
uv run agent-tracker next --config tracking/project.json --role maintainer --limit 10 --json
```

Then check:

- whether all ready work is filtered out by role metadata;
- whether the task has dependencies that are not `done`;
- whether a stale claim exists and the lease has not expired;
- whether the config path resolves to the expected `tracking/state.sqlite`;
- whether `tracking/tasks.json` is valid JSON and imports cleanly;
- whether the CLI, importer, or store has a repairable bug that should be fixed
  with focused tests.

Only report a blocker after confirming the queue state is valid and no
repairable repo issue is preventing the claim.

## Add Or Update Tasks

New deterministic work items currently live in `tracking/tasks.json`.

When adding a task:

- use a stable lowercase ID, for example `pull-spool-command`;
- set `status` to `pending` unless the work is already complete;
- pick a priority that preserves the intended sequence;
- add `requirements` for real dependencies;
- add `metadata.roles` so agents can claim it by role;
- add `metadata.write_scopes` to describe expected files or modules;
- add validation checks that a future agent can run.

After editing `tracking/tasks.json`, run:

```bash
uv run agent-tracker import --config tracking/project.json
uv run agent-tracker status --config tracking/project.json
```

Do not remove task entries casually. Importing synchronizes the database to the
task plan, so deleted tasks are removed from live state.

## User Documentation

User-facing docs live in:

- `README.md`
- `docs/configuration.md`
- `docs/task-plans.md`
- `docs/operations.md`
- `docs/plugins.md`
- `docs/planning.md`

When changing user-facing behavior, update the relevant docs in the same task.
