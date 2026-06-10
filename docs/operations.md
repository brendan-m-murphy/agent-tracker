# Operations Guide

This guide covers the normal local lifecycle for an `agent-tracker` project:
initialize, import, inspect, claim, heartbeat, complete or fail, ingest events,
and export audit state.

Every command accepts `--config <project.json>`. If `--config` is omitted, the
CLI uses `AGENT_TRACKER_CONFIG` as the default project config path. Every command
also accepts `--db <path>` when you need to override the configured SQLite path
temporarily; if `--db` is omitted, the CLI uses `AGENT_TRACKER_DB` when set.
Explicit CLI arguments always take precedence over environment defaults. When
`canonical_config_path` is set, mutating commands refuse copied configs and
database overrides so live state stays attached to the canonical project.

## First Run Checklist

For a new local project:

```bash
agent-tracker init-project tracking --project-id demo --name "Demo Tracker"
agent-tracker init --config tracking/project.json
agent-tracker import --config tracking/project.json
agent-tracker status --config tracking/project.json
```

`init-project` creates a plugin-free JSON-task tracker layout with
`project.json`, `tasks.json`, local spool directories, an export directory, and
runtime `.gitignore` entries. Use `--canonical-config` when copied worktrees
should be refused for mutating commands.

In a project shell or wrapper script, set the config once and omit repeated
`--config` arguments:

```bash
export AGENT_TRACKER_CONFIG=tracking/project.json
agent-tracker init
agent-tracker import
agent-tracker status
```

For temporary read-only inspection of another SQLite file, wrappers can provide
a database default while still allowing callers to override it explicitly:

```bash
AGENT_TRACKER_CONFIG=tracking/project.json \
AGENT_TRACKER_DB=/tmp/agent-tracker.snapshot.sqlite \
agent-tracker status --json
```

Use `init` to create or refresh the project row and database schema. Use
`import` whenever the committed task plan changes. `import` also initializes the
schema if needed, so automation can run it as the first command in a pull
workflow. Mutating commands print the resolved config, task source, and database
paths to stderr before changing state.

## Sandboxed Codex Runs

In managed Codex worktrees, the project files, Git metadata, uv cache, and
canonical tracker state may live under different filesystem authorities. Treat
those boundaries as part of the coordination contract instead of working around
them with copied state.

Prefer the existing virtualenv entrypoints for read-only inspection and focused
validation when the environment has already been synced:

```bash
.venv/bin/agent-tracker overview --config tracking/project.json
.venv/bin/agent-tracker status --config tracking/project.json --json
.venv/bin/pytest
.venv/bin/ruff check .
```

These commands avoid `uv run` cache discovery, which can require approval in
sandboxed sessions even for read-only commands. Use `uv run` when dependencies
need to be resolved, extras or groups need to be installed, or a fresh
environment is being created. If the runner supports it, a writable uv cache
such as `uv --cache-dir /tmp/agent-tracker-uv-cache run ...` or
`UV_CACHE_DIR=/tmp/agent-tracker-uv-cache` can reduce home-directory cache
friction, but it does not replace approval for commands that need network,
system configuration, or out-of-worktree writes.

Mutating tracker commands must still use the canonical config when
`canonical_config_path` is configured:

```bash
agent-tracker claim --config /path/to/canonical/tracking/project.json \
  --agent agent-1 \
  --task-id write-readme
```

Those commands write the canonical SQLite database and may require approval when
the database is outside the Codex worktree. Do not point `AGENT_TRACKER_DB`,
`db_path`, or `state_root` at a temporary sandbox location just to avoid the
approval; that creates a second live queue authority and can lose leases,
evidence, and audit history.

Git branch, commit, and push operations can also require approval when the
worktree's `.git` file points to repository metadata outside the writable
worktree. That is expected for copied Codex worktrees. Record these approvals as
run evidence or friction only when they block normal coordinator progress.

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
state. The default import mode updates task definitions and dependencies while
preserving existing runtime status, leases, evidence, audit entries, and tasks
that are absent from the source. This keeps SQLite as the live queue authority.

When the task plan is intentionally being reconciled with runtime state, make
that explicit:

```bash
agent-tracker import --config project.json --reconcile-runtime-state
```

Runtime reconciliation applies imported statuses and removes tasks absent from
the task source. Use it only when the source task plan is known to be the desired
runtime policy.

## Check Status

Print counts:

```bash
agent-tracker status --config project.json
```

Human `status` output is rendered with Rich and grouped into `Paths` and
`Queue` sections with aligned labels. It avoids Rich panels or box-drawing
characters by default so output can be copied into logs and plain-text handoffs.

Print full JSON task state:

```bash
agent-tracker status --config project.json --json
```

The JSON payload contains:

- `tasks`: all evaluated task states;
- `ready`: ready task IDs;
- `active`: claimed, in-progress, or waiting-evidence task IDs;
- `review`: task IDs waiting for review evidence;
- `integration`: task IDs waiting for PR, merge, or integration evidence;
- `blocked`: blocked task IDs;
- `db_path`: resolved SQLite path.

By default, `status` is read-only and reports the effective state without
writing stale-lease recovery back to SQLite. To perform recovery during status
inspection, opt in:

```bash
agent-tracker status --config project.json --recover-stale-leases
```

## Project Overview

Use `overview` when you need a compact project-log view instead of raw status
IDs:

```bash
agent-tracker overview --config project.json
```

The default human output is no-box and copy-paste safe: plain section headings,
stable labels, no decorative panels, and readable wrapping when copied into
logs, chat, or pull request comments. It is a coordination summary, not a full
task dump.

The human output is grouped under stable headings:

- Ready;
- Active;
- Review;
- Integration;
- Blocked;
- Recently completed.

Blocked entries include unsatisfied requirement details from the evaluated
`requirements` data. Ready and waiting entries show `next_action` and latest
evidence when available. Recently completed entries are ordered from completion
audit records, not task priority.

Human overview output wraps long task titles, blockers, next actions, evidence,
and completion details at a standard terminal width. Wrapped task titles use a
distinct continuation indent so they do not read like `next`, `blocker`, or
other detail fields. Each group reports a count and is limited for readability;
pass `--limit 0` to show every grouped task.

Use JSON output for automation:

```bash
agent-tracker overview --config project.json --json
```

The JSON payload contains `counts` plus grouped task dictionaries under
`groups.ready`, `groups.active`, `groups.review`, `groups.integration`,
`groups.blocked`, and `groups.recently_completed`. By default, each group is
limited to five entries for readability; pass `--limit 0` for every grouped
task dictionary.

Like `status`, `overview` is read-only by default. It reports the effective
state without mutating stale leases. Pass `--recover-stale-leases` only when
inspection should also write stale-lease recovery to SQLite.

Raw intake and proposed tasks are planning records, not live queue tasks. They
stay out of overview's live task groups until promoted. If a future planning
section shows intake or proposals, it should be visibly distinct from Ready,
Active, Review, Integration, Blocked, and Recently completed.

Keep the default overview compact. Downstream work that needs more task
metadata should add an explicit human detail drilldown instead of expanding the
default summary. The detailed UX contract for future overview work is in
`docs/research/2026-06-10-overview-ux-contract.md`; it covers compact defaults,
copy-paste-safe output, JSON compatibility, summary/detail drilldown, output
modes, intake/proposal consistency, and the deferred Textual TUI posture. See
`docs/research/2026-06-09-cli-tui-helper-evaluation.md` before adding a runtime
dependency for richer human output or TUI behavior.

## Typed Tool Surface

`agent_tracker.mcp_tools.AgentTrackerTools` exposes a scoped, typed Python tool
surface for Codex, app, MCP, or local wrapper hosts that should not shell out to
the broad CLI for routine coordination operations.

Create one tool object per project config:

```python
from agent_tracker.mcp_tools import AgentTrackerTools

tools = AgentTrackerTools("tracking/project.json")
```

The routine wrappers mirror the coordinator operations and return JSON-friendly
payloads:

- `status(recover_stale_leases=False)`: full status payload with task state
  lists, resolved paths, and queue groups.
- `overview(limit=5, recover_stale_leases=False)`: grouped ready, active,
  review, integration, blocked, and recently completed task dictionaries.
- `claim(agent_id, task_id="", repo="", role="", lease_seconds=3600)`: claim
  payload containing `project_id`, `task_id`, `lease_token`,
  `lease_expires_at`, and `agent_id`.
- `heartbeat(task_id, lease_token, lease_seconds=3600, agent_id="")`: the same
  claim-shaped lease payload after extending the lease.
- `complete(task_id, lease_token, evidence=None, agent_id="",
  direct_merge=False)`: `{"ok": true}` after successful completion.
- `record_evidence(task_id, uri, actor="system")`: idempotently append one
  evidence URI without changing task state.
- `check_completion_integrity()`: deterministic diagnostic for completed tasks
  whose stored evidence no longer satisfies current completion policy.
- `pull_spool(dry_run=False)`: pull-spool counts and per-file actions.
- `ingest_spool(actor="system")`: processed, inserted, and error counts.
- `launch_worker_prompt(task_id, agent_id="", markdown=True)`: prompt-only
  worker handoff data with `launch_mode` set to `prompt_only`, `launched` set to
  `false`, the rendered prompt, and the current task context.

`launch_worker(...)` is an equivalent prompt-only alias for hosts that name the
tool after the launch-worker operation. Neither launch helper starts Codex, an
app server, or any external worker. Hosts that own execution can use the
returned prompt and task context as their launch input, then report progress
back through normal tracker state, evidence, and event APIs.

Existing method names remain supported as compatibility aliases:
`get_project_status()`, `claim_task(...)`, `heartbeat_task(...)`,
`complete_task(...)`, `list_ready_tasks(...)`, `get_task_context(...)`, and
`render_prompt(...)`.

These wrappers are adapters over `Coordinator`, not a second queue authority.
Canonical config and database override checks still run for mutating calls, and
lease tokens and optional `agent_id` ownership checks still apply to heartbeat,
completion, review, integration, and failure operations. `status` and
`overview` remain read-only unless their `recover_stale_leases` flag is set.

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
Human `next` output wraps long task titles and next actions with continuation
lines aligned under the wrapped value. JSON output is unchanged and should be
used for automation.
`next` is read-only by default. Use `claim` to recover stale leases and claim
work atomically, or pass `--recover-stale-leases` when inspection should also
write stale-lease recovery to SQLite.

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

## Launch Local Workers

Use `workspaces` in the project config when a coordinator needs to address other
local project checkouts by name. Inspect the registry first:

```bash
agent-tracker list-workspaces --config project.json
```

Prepare a worker launch for a task without executing a command:

```bash
agent-tracker launch-worker --config project.json \
  --workspace hpc \
  --task-id write-readme
```

This writes a rendered prompt, a placeholder report, and a launch JSON artifact
below the workspace's configured `artifacts_path`. `--markdown` is the default;
`--no-markdown` forwards `markdown=False` to custom prompt renderers. The
built-in renderer currently emits the same compact Markdown-style prompt for
both modes. The launcher records `worker-launch:<id>` and
`file:<launch.json>` evidence on the task when `--task-id` is supplied. If the
workspace has `spool_outbox`, it also writes a complete
`agent_tracker.worker_launch` event JSON file there for later collection.

Run the configured local worker command with `--execute`:

```bash
agent-tracker launch-worker --config project.json \
  --workspace hpc \
  --task-id write-readme \
  --agent hpc-worker-1 \
  --execute
```

The default command is:

```bash
codex exec --cd {workspace_path} --output-last-message {report_path} -
```

The rendered prompt is passed on stdin. The command runs in the workspace
directory, stdout and stderr are captured as launch artifacts, and the final
report path is exposed through `AGENT_TRACKER_WORKER_REPORT`.

For smoke tests or non-Codex workers, pass an explicit command:

```bash
agent-tracker launch-worker --config project.json \
  --workspace hpc \
  --prompt "Report installed agent-tracker capabilities." \
  --json \
  --execute \
  --command python -c "print('ok')"
```

Put `launch-worker` options such as `--json` before `--command`. Every token
after `--command` is treated as part of the worker command argv.

`launch-worker` execution is currently local-only. SSH workspaces can be
validated and listed, and SSH/SFTP `pull-spool` can collect remote event files,
but remote queue mutation should wait for the task-ingest command processor
instead of letting remote agents write canonical SQLite directly.

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

## Record Raw Intake

Use intake for ideas, feature requests, checks, and planning notes that should
not become claimable tasks yet:

```bash
agent-tracker intake --config project.json record \
  --kind feature \
  --source user \
  --repo agent-tracker \
  --tag triage \
  "Add an inbox for untriaged requests"
```

List intake for project-manager triage:

```bash
agent-tracker intake --config project.json list --json
```

The grouped `intake` command is implemented with Typer and accepts common
`--config` and `--db` options at the group level, before the leaf command.
Without `--json`, `intake list` uses Rich rendering and prints each record with
its current status, such as `status open`, `status triaged`, `status closed`, or
`status deferred`, plus its creation timestamp when available. The flat aliases
`record-intake`, `list-intake`, and `update-intake` remain supported for
existing scripts and produce the same JSON payloads.

Intake records are stored in SQLite, audited as `intake.record`, and included
in snapshots. They do not appear in `next`, `overview` ready groups, or `claim`
results until a later triage workflow promotes them into proposed task
contracts.

If an intake item needs no task, close or defer it explicitly:

```bash
agent-tracker intake --config project.json update <intake-id> --status closed
```

## Triage Intake

Project-manager triage turns a raw intake item into a proposed task contract.
The proposal is durable and reviewable, but it is still not live queue state.
Creating a proposal marks open intake as `triaged`:

```bash
agent-tracker propose-task --config project.json <intake-id> \
  --task-id add-triage \
  --title "Add triage workflow" \
  --repo agent-tracker \
  --role maintainer \
  --write-scope src/agent_tracker/service.py \
  --validation-check "uv run pytest" \
  --dependency foundation:"Base queue exists." \
  --authority "local code and docs"
```

List proposals:

```bash
agent-tracker list-proposals --config project.json --json
```

Proposed task records are stored in SQLite, audited as `proposal.record`, and
included in snapshots. They do not appear in ready-task listings and cannot be
claimed until promoted.

If a proposed contract has a typo, incomplete next action, wrong write scope, or
bad dependency, update the proposal before promotion instead of editing SQLite:

```bash
agent-tracker update-proposal --config project.json <proposal-id> \
  --title "Add triage workflow" \
  --next-action "Implement reviewed triage promotion." \
  --write-scope src/agent_tracker/service.py \
  --validation-check "uv run pytest"
```

If the intake should not become a task, withdraw the proposed contract before it
is promoted:

```bash
agent-tracker withdraw-proposal --config project.json <proposal-id> --actor pm
```

Updating or withdrawing a proposal is audited and only applies while the
proposal is still in the `proposed` state. Promoted proposals are live queue
history; fix them with normal task follow-up work instead of rewriting the
proposal record. Withdrawn proposals are retained with `rejected` status as
audit history, and their task IDs remain reserved; create a new proposal with a
new task ID if later work should replace the withdrawn contract.

After review, promote a proposal into live queue state:

```bash
agent-tracker promote-proposal --config project.json <proposal-id> --actor pm
```

Promotion audits `proposal.promote`, changes the proposal status to
`promoted`, creates a pending live task with its dependency records, and leaves
the task-plan JSON untouched. Normal definition imports preserve promoted
runtime tasks; use destructive runtime reconciliation only when you intend to
make the importer source authoritative again.

## Validate Preview Git Refs

Publish a preview git ref only when a downstream uv project is blocked on an
in-progress `agent-tracker` feature and needs to validate it before it reaches
`main` or a release. The ref should point at scoped, reviewable work that has
already passed the maintainer's normal local checks. Do not use preview refs as
a long-lived compatibility promise or as a substitute for review, merge, or
release policy.

Maintainer checklist:

- create or update a named preview branch such as `preview/<feature-or-task>`;
- keep the branch narrow enough that downstream results map to one tracker
  task or task group;
- record upstream evidence such as `git:<preview-commit>` and `pr:<url>` or an
  equivalent review surface;
- tell downstream validators which branch or commit SHA to pin and which checks
  to run.

Downstream uv projects should pin the preview in dependency config rather than
depending on an adjacent checkout:

```toml
[project]
dependencies = ["agent-tracker"]

[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", branch = "preview/<feature-or-task>" }
```

For a repeatable result, pin the exact preview commit:

```toml
[tool.uv.sources]
agent-tracker = { git = "<agent-tracker-git-url>", rev = "<commit-sha>" }
```

Validate from the downstream project:

```bash
uv lock --upgrade-package agent-tracker
uv sync
uv run <downstream-validation-command>
```

While validating, collect tracker evidence that another maintainer can inspect:

- `git:<preview-commit>` for the preview being tested;
- `validation:<project>:<command-or-run-url>` for the downstream check;
- `file:<path>` for a concise log, report, or config diff;
- `pr:<url>` or `review:<summary>` when validation happens through review.

When validation succeeds and the feature lands, remove the temporary preview
pin. If the downstream project intentionally tracks unreleased main, switch the
uv source to `branch = "main"`. Prefer `tag = "<release-tag>"` or the normal
published dependency for long-lived downstream configuration. Rerun
`uv lock --upgrade-package agent-tracker` after replacing the source.

## Complete Work

If the task changed tracked code, docs, config, tests, or task plans, complete
the closeout before marking the tracker task done. The default closeout is
branch-backed and reviewable:

- commit the scoped changes on a task branch;
- open a PR or equivalent review surface for that branch;
- push the branch when a remote is configured;
- include evidence such as `git:<branch-commit>` and `pr:<url>`.

Use the direct-merge override only for trusted manager workflows where review is
handled outside a PR:

- commit the scoped changes on a task branch;
- merge the task branch into `main`;
- push `main` when a remote is configured;
- include integrated evidence such as `git:<main-commit>`.

Local validation output and file paths are useful supporting evidence, but they
are not enough by themselves for tasks that modify repository files. If
review or integration evidence is pending, use `submit-review` or
`await-integration` instead of marking the task complete.

Projects can make this expectation machine-checkable per task with metadata:

```json
{
  "completion_policy": {
    "default": "pr_or_review_required",
    "direct_merge_override": true
  }
}
```

For tasks with `completion_policy.default` set to
`pr_or_review_required`, any transition to `done` requires cumulative evidence
with at least one `git:` URI and at least one `pr:`, `review:`, or
`integration:` URI. Evidence recorded before the final command, such as during
`record-evidence` or `submit-review`, counts alongside evidence supplied to
`complete`, `resolve-review`, or `resolve-integration`.

SQLite remains the canonical live queue state. Git commits and GitHub PRs are
evidence and review surfaces for closeout; do not use them as live coordination
state in place of leases, task status, evidence rows, or audit events.

Append evidence without changing task state:

```bash
agent-tracker record-evidence --config project.json write-readme \
  "git:<branch-or-main-commit>" \
  --actor agent-1
```

Mark the task done and attach evidence:

```bash
agent-tracker complete --config project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --evidence "git:<branch-or-main-commit>" \
  --evidence "file:README.md" \
  --evidence "pr:https://github.com/org/repo/pull/123"
```

Evidence strings are intentionally URI-like and project-defined. Prefer links
or bounded summaries over large raw outputs.

Completing a task clears its lease. Downstream pending tasks become ready after
their dependencies are done.

Check completed tasks for evidence that no longer satisfies the current policy:

```bash
agent-tracker check-completion-integrity --config project.json
agent-tracker check-completion-integrity --config project.json --json
```

The check is read-only and uses the same completion-policy validator as the
state transitions. It exits non-zero when it finds issues. Missing, malformed,
or unknown `completion_policy` metadata remains legacy behavior and is not
reported as a policy issue.

## Await Review Or Integration

When implementation work is finished but the task is not done yet, release the
active lease into an explicit non-terminal queue state. This prevents the task
from being reclaimed as active work while preserving that dependencies are not
satisfied until the task is `done`.

Submit work for review:

```bash
agent-tracker submit-review --config project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --evidence "git:<branch-commit>" \
  --evidence "pr:https://github.com/org/repo/pull/123"
```

Wait for PR, merge, or another integration step:

```bash
agent-tracker await-integration --config project.json write-readme \
  --lease-token <lease-token> \
  --agent agent-1 \
  --status awaiting_merge \
  --evidence "pr:https://github.com/org/repo/pull/123"
```

`await-integration` defaults to `awaiting_integration`; `--status` can be
`awaiting_pr`, `awaiting_merge`, or `awaiting_integration`. These transitions
record evidence, write audit entries, and clear lease fields. They do not unblock
dependents.

After review or integration is finished, a reviewer or trusted manager resolves
the waiting task without a lease token:

```bash
agent-tracker resolve-review --config project.json write-readme \
  --agent reviewer-1 \
  --evidence "review:approved"

agent-tracker resolve-integration --config project.json write-readme \
  --agent reviewer-1 \
  --evidence "git:<main-commit>"
```

Both resolver commands default to `--status done`. To resolve the waiting task
as failed, pass `--status failed --reason "<short reason>"`. A task is stranded
if it is moved to an awaiting state and never resolved, so reviewers and
managers should treat these resolver commands as the normal closeout path for
reviewable work.

For branch-backed local work with the trusted-manager direct-merge override, a
typical flow is:

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
  --evidence "file:README.md" \
  --direct-merge
```

If `main` cannot be updated directly, open a PR and use `pr:<url>` evidence
with `submit-review` or `await-integration` instead of marking the task complete
from an isolated worktree. Agents that do not have explicit direct-merge
authority should also open a PR or leave an equivalent review state before
completion.

`--direct-merge` is explicit and metadata-gated. It is accepted only when the
task allows `"direct_merge_override": true`, and it still requires `git:`
evidence. Without this flag, `git:` evidence alone is not enough for
`pr_or_review_required` tasks. The integrity check also reports direct-merge
completions whose cumulative evidence is only `git:` evidence and lacks
`pr:`, `review:`, or `integration:` evidence, so managers can find work that
still needs an integrated review or merge trail.

When the committed task plan is the authoritative source, include the terminal
task-plan status update in the integrated branch and use
`import --reconcile-runtime-state` deliberately. A normal import preserves the
live SQLite terminal status even if the source task entry still says `pending`.

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
    "error": "spool/error",
    "remote_inbox": "/shared/agent-tracker/spool/outbox"
  }
}
```

Write event files directly into the local inbox, then ingest:

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

When a shared filesystem or other outbox is available, configure
`spool.remote_inbox` and pull complete files into the local inbox first:

```bash
agent-tracker pull-spool --config project.json --dry-run
agent-tracker pull-spool --config project.json
agent-tracker ingest-spool --config project.json --actor spool
```

`pull-spool` copies complete `*.json` files from `remote_inbox` to `inbox`.
Remote files are left in place. Names ending in `.partial`, `.part`, or `.tmp`
are skipped so producers can write files atomically and rename them when
complete. Files are copied to a temporary non-JSON name in the local inbox and
then atomically renamed to the final `*.json` path. Existing identical local
files in `inbox`, `done`, or `error` are skipped; existing different files are
reported as conflicts and are not overwritten.

For SSH/SFTP sources, install the optional SSH extra and use an `ssh://` or
`sftp://` remote inbox:

```bash
uv run --extra ssh agent-tracker pull-spool --config project.json --dry-run
uv run --extra ssh agent-tracker pull-spool --config project.json
```

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

Set `spool.ssh.known_hosts` to `"none"` only for isolated loopback tests. Keep
passwords and private-key material out of committed project config.

`pull-spool` is a bounded copy step, not a daemon. Run it from an attendant,
cron, or supervisor when polling is needed.

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

## Agent Roles

Use distinct roles around the same tracker state:

- `agent-coordinator` owns project-wide orchestration: queue health, leases,
  task planning, worker supervision, review or integration evidence, and final
  tracker closeout. When the user asks for agent coordination or subagents, it
  normally delegates bounded implementation, review, test, or evidence work
  while keeping queue state, leases, integration decisions, and final evidence.
- `project-manager` owns planning and triage: intake, status reports, queue
  tidying, proposed tasks, promotions, and notebooks. It does not take a worker
  lease for one-task implementation.
- `task-worker` owns exactly one claimed task: scoped edits, focused checks,
  evidence, and handoff or closeout for that task only.

Install or refresh the vendored skills after installing `agent-tracker`:

```bash
agent-tracker-install-skill --all --overwrite
```

Use repeated `--name` flags to install or refresh a subset, for example
`agent-tracker-install-skill --name agent-coordinator --name task-worker
--overwrite`. With no `--name` or `--all`, the command keeps the historical
default and installs `project-manager`.

## Recommended Coordinator Flow

Coordinators or generic project agents that are allowed to choose work can use
this sequence:

```bash
agent-tracker import --config project.json
agent-tracker next --config project.json --role maintainer --limit 1
agent-tracker claim --config project.json --agent <agent-id> --role maintainer --lease-seconds 7200
agent-tracker task --config project.json <task-id> --markdown
```

Do not use this as `task-worker` guidance. A task worker should receive a
specific task ID or rendered prompt and may only claim that exact task when
project policy allows it. A project manager should report status, triage intake,
and propose or promote tasks without taking an implementation lease.

If the user requested agent coordination or subagents, the coordinator should
normally dispatch bounded implementation, review, test, or evidence work to
subagents after claiming/rendering the task. Tiny coordination mutations and
sessions where subagents are unavailable can stay local, but local work should
be explicit rather than silently replacing requested agent coordination.

Then, while a claimed task is active:

```bash
agent-tracker heartbeat --config project.json <task-id> --lease-token <lease-token> --agent <agent-id>
```

When done:

```bash
agent-tracker complete --config project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --evidence "git:<branch-or-main-commit>" \
  --evidence "file:<path>"
```

If implementation is done but review or integration evidence is still pending:

```bash
agent-tracker submit-review --config project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --evidence "git:<branch-commit>"

agent-tracker await-integration --config project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --status awaiting_pr \
  --evidence "git:<branch-commit>"
```

After review or integration is finished:

```bash
agent-tracker resolve-review --config project.json <task-id> \
  --agent <reviewer-or-manager> \
  --evidence "review:approved"

agent-tracker resolve-integration --config project.json <task-id> \
  --agent <reviewer-or-manager> \
  --evidence "git:<main-commit>"
```

If blocked after investigation:

```bash
agent-tracker fail --config project.json <task-id> \
  --lease-token <lease-token> \
  --agent <agent-id> \
  --reason "<short actionable reason>"
```
