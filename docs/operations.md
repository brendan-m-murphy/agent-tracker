# Operations Guide

This guide covers the normal local lifecycle for an `agent-tracker` project:
initialize, import, inspect, claim, heartbeat, complete or fail, ingest events,
and export audit state.

Every command takes `--config <project.json>`. Every command also accepts
`--db <path>` when you need to override the configured SQLite path temporarily.

## First Run Checklist

For a new local project:

```bash
mkdir -p tracking/spool/inbox tracking/spool/done tracking/spool/error tracking/exports
agent-tracker init --config tracking/project.json
agent-tracker import --config tracking/project.json
agent-tracker status --config tracking/project.json
```

Use `init` to create or refresh the project row and database schema. Use
`import` whenever the committed task plan changes. `import` also initializes the
schema if needed, so automation can run it as the first command in a pull
workflow.

## Initialize Storage

Create the project row and database schema:

```bash
agent-tracker init --config project.json
```

Run this once for a new project database. It is safe to run again after config
changes.

## Import Tasks

Load the committed task plan into SQLite:

```bash
agent-tracker import --config project.json
```

The importer is the bridge from durable project planning files into live queue
state. Re-run import whenever the task plan changes. Because imported task
statuses are authoritative, make sure completed tasks are marked `done` in the
source task plan before re-importing over live completed state.

## Check Status

Print counts:

```bash
agent-tracker status --config project.json
```

Print full JSON task state:

```bash
agent-tracker status --config project.json --json
```

The JSON payload contains:

- `tasks`: all evaluated task states;
- `ready`: ready task IDs;
- `active`: claimed, in-progress, or waiting-evidence task IDs;
- `blocked`: blocked task IDs;
- `db_path`: resolved SQLite path.

Status reads recover stale leases before reporting state.

## List Ready Tasks

Show the next ready tasks:

```bash
agent-tracker next --config project.json --limit 3
```

Filter by role or repo:

```bash
agent-tracker next --config project.json --role maintainer --repo demo-app --limit 1
```

Use JSON output for automation:

```bash
agent-tracker next --config project.json --role maintainer --json
```

Ready tasks are ordered by `priority`, then task ID.

## Claim Work

Claim the first matching ready task:

```bash
agent-tracker claim --config project.json \
  --agent agent-1 \
  --role maintainer \
  --lease-seconds 7200
```

Claim a specific task:

```bash
agent-tracker claim --config project.json \
  --agent agent-1 \
  --task-id write-readme \
  --lease-seconds 7200
```

The command prints JSON:

```json
{
  "project_id": "demo",
  "task_id": "write-readme",
  "lease_token": "6a0f...",
  "lease_expires_at": "2026-06-08T14:30:00+00:00",
  "agent_id": "agent-1"
}
```

Keep the `lease_token`. It is required for heartbeat, completion, and failure.

## Render Task Context

Render a human-readable prompt:

```bash
agent-tracker task --config project.json write-readme --markdown
```

Render JSON state:

```bash
agent-tracker task --config project.json write-readme --json
```

The default prompt is intentionally compact. Use a custom prompt renderer plugin
when a project needs richer context.

## Heartbeat A Lease

Extend the lease and mark the task `in_progress`:

```bash
agent-tracker heartbeat --config project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --lease-seconds 7200
```

Use heartbeats for longer tasks so stale-lease recovery does not return active
work to the ready queue.

## Log Work While Active

Use concise events or spool records for durable progress that another agent or
project manager should see. Keep raw logs in files, build systems, or external
artifacts; store only summaries and links in the tracker.

One direct event file:

```json
{
  "event_id": "worklog-write-readme-20260608T143000Z",
  "kind": "worklog",
  "task_id": "write-readme",
  "summary": "Drafted install and quickstart docs.",
  "files": ["README.md", "docs/operations.md"],
  "commands": ["agent-tracker task --config tracking/project.json write-readme --markdown"]
}
```

Ingest it:

```bash
agent-tracker ingest-event --config project.json worklog.json --actor agent-1
```

For asynchronous producers, write the same kind of JSON object into the
configured spool inbox and run `ingest-spool`.

## Complete Work

If the task changed tracked code, docs, config, tests, or task plans, complete
integration before marking the tracker task done. The work should be accessible
to other collaborators first:

- commit the scoped changes on a task branch;
- merge the task branch into `main` or open a PR when direct merge is not the
  intended workflow;
- push the branch or `main` when a remote is configured;
- include integrated evidence such as `git:<main-commit>` or `pr:<url>`.

Local validation output and file paths are useful supporting evidence, but they
are not enough by themselves for tasks that modify repository files. If
integration is blocked, keep the task active with heartbeats or fail it with an
actionable reason instead of marking it complete.

Mark the task done and attach evidence:

```bash
agent-tracker complete --config project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --evidence "git:<main-commit-or-merged-branch>" \
  --evidence "file:README.md" \
  --evidence "pr:https://github.com/org/repo/pull/123"
```

Evidence strings are intentionally URI-like and project-defined. Prefer links
or bounded summaries over large raw outputs.

Completing a task clears its lease. Downstream pending tasks become ready after
their dependencies are done.

For branch-backed local work, a typical direct-merge flow is:

```bash
git switch -c codex/write-readme main
# edit files and run validation
git add README.md docs/operations.md
git commit -m "Document agent-tracker quickstart"
git switch main
git merge --ff-only codex/write-readme
main_sha=$(git rev-parse HEAD)
agent-tracker complete --config tracking/project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --evidence "git:${main_sha}" \
  --evidence "file:README.md"
```

If `main` cannot be updated directly, open a PR and use `pr:<url>` evidence
instead of marking the task complete from an isolated worktree.

When the committed task plan is the authoritative source, include the terminal
task-plan status update in the integrated branch. Otherwise a future `import`
can reopen the completed live task from a stale `pending` entry.

## Fail Work

Mark the task failed with an actionable reason:

```bash
agent-tracker fail --config project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --reason "README validation failed: setup command is missing"
```

Failure is terminal for the task. Create or import follow-up tasks separately if
more work is needed.

## Stale Lease Recovery

If an agent claims a task and stops heartbeating, the task is recoverable after
the lease expires. `status`, `next`, `claim`, and task-state reads recover stale
leases before evaluating readiness.

To recover stuck work:

```bash
agent-tracker status --config project.json --json
agent-tracker next --config project.json --role maintainer --limit 5
```

If a claim fails unexpectedly:

```bash
agent-tracker status --config project.json --json
agent-tracker import --config project.json
agent-tracker next --config project.json --role maintainer --json
```

Then check:

- whether dependencies are still blocked;
- whether the role filter matches `metadata.roles` or `metadata.allowed_roles`;
- whether the task plan was imported;
- whether a stale lease has not expired yet;
- whether config paths point at the expected database and task plan.

## Ingest One Event

Record one event JSON file:

```bash
agent-tracker ingest-event --config project.json event.json --actor callback
```

Default event JSON:

```json
{
  "event_id": "run-123-finished",
  "kind": "validation.finished",
  "task_id": "write-readme",
  "status": "passed",
  "artifact": "file:reports/run-123.txt"
}
```

The default adapter requires `event_id` or `id`. `kind` defaults to `event`, and
`task_id` is optional. Duplicate `event_id` values are ignored and reported as
`duplicate`.

Use an `event_adapter` plugin when project callbacks need normalization.

## Ingest A Local Spool

Configure a local spool:

```json
{
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error"
  }
}
```

Write event files into the inbox, then ingest:

```bash
agent-tracker ingest-spool --config project.json --actor spool
```

The command returns counts:

```json
{
  "processed": 2,
  "inserted": 1,
  "errors": 1
}
```

For each `*.json` file in the inbox:

- valid new events are recorded and moved to `done`;
- duplicate events are moved to `done`;
- files that cannot be parsed or normalized are moved to `error`;
- non-JSON files are ignored.

Current spool support is local only. It does not yet copy from a remote inbox,
skip partial files, or run continuously as a daemon.

## Export Audit State

Write a snapshot:

```bash
agent-tracker export --config project.json
```

With the default exporter, the output path is `export_path` or
`agent-tracker-snapshot.json` beside the config file.

Snapshots include:

- evaluated task state;
- events;
- audit log entries;
- generated timestamp.

Treat SQLite as live state and snapshots as derived audit artifacts that can be
checked into git or shared with reviewers.

## Recommended Agent Flow

Agents should use this sequence:

```bash
agent-tracker import --config project.json
agent-tracker next --config project.json --role maintainer --limit 1
agent-tracker claim --config project.json --agent <agent-id> --role maintainer --lease-seconds 7200
agent-tracker task --config project.json <task-id> --markdown
```

Then, while working:

```bash
agent-tracker heartbeat --config project.json <task-id> --lease-token <lease-token> --agent <agent-id>
```

When done:

```bash
agent-tracker complete --config project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --evidence "git:<main-commit-or-merged-branch>" \
  --evidence "file:<path>"
```

If blocked after investigation:

```bash
agent-tracker fail --config project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --reason "<short actionable reason>"
```
