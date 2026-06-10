---
name: project-manager
description: Manage planning and triage for an agent-tracker-backed project. Use when the user asks to triage ideas/features/checks, report queue status, tidy planning or queue metadata, organize follow-up work, propose or promote tasks, update notebooks, or summarize coordination state without taking a worker lease.
---

# Project Manager

Use this skill for planning, status, and triage work in repositories coordinated
by `agent-tracker` or by a repo-local wrapper around it. Do not use it as the
one-task implementation worker. Use `task-worker` for one claimed task and
`agent-coordinator` for project-wide orchestration.

## Locate The Queue

Prefer repo-local wrappers when they exist:

- If `tracking/README.md`, a project notebook, or another repo-local guide
  documents a tracker wrapper, use that wrapper's documented commands for
  status, listing, task rendering, intake, proposals, and promotions.
- Otherwise, look for `tracking/project.json`.
- If neither exists, look for `agent-tracker.config.json`.
- If no config is discoverable, ask for the tracker config path.

## Report Queue Status

When the user asks what should happen next, report the queue without claiming
work:

1. Import or sync the task plan only when the project guide says that is part
   of normal status reporting.
2. List ready, active, review, integration, blocked, and recently completed
   work.
3. Identify candidate next tasks by role, dependency state, write scope, and
   risk.
4. Summarize blockers, stale leases, missing task definitions, and follow-up
   planning needs.
5. Hand implementation to `task-worker` or project-wide execution to
   `agent-coordinator`, including any configured `coordination_policy` that
   controls task worktrees and PR mapping.

For a plain `agent-tracker` project:

```bash
uv run agent-tracker import --config tracking/project.json
uv run agent-tracker overview --config tracking/project.json --limit 10
uv run agent-tracker next --config tracking/project.json --role maintainer --limit 1
uv run agent-tracker task --config tracking/project.json <task-id> --markdown
```

Render a task prompt only to inspect readiness, write scope, validation,
closeout, and authority rules. Do not take the lease or make implementation
edits as project-manager unless the user explicitly switches you into a
different role.

When proposing or handing off implementation, preserve the project's worktree
and PR policy. The conservative default is one non-canonical worktree per task
and one PR per task. Only plan a shared worktree for serially related,
non-conflicting tasks when project policy allows `shared_worktree_serial`.
Only plan a batch or epic PR when policy allows `batch_pr_allowed`; the PR must
list task IDs, batching rationale, and closeout evidence for each task.

If the queue does not show expected ready work, investigate before reporting a
blocker:

- status JSON and ready/blocked counts;
- whether the task plan has been imported;
- dependency status;
- role filters and task metadata;
- stale leases;
- config path and path resolution;
- importer, CLI, or store failures that can be repaired with focused tests.

## Capture Ideas, Features, And Checks

When the user gives an idea, feature request, check, concern, or planning note:

1. Preserve the raw intake text with source, date, and project context.
2. Do not make it claimable until it has been triaged.
3. Convert it into proposed task contracts only after project-manager review.
4. Include repo, role, authority, dependencies, validation checks, intervention
   needs, and notebook updates in each proposed task.
5. Include branch/worktree and PR expectations when they differ from the
   project default.
6. If the current project has no intake feature yet, add a planning task or a
   repo-local note rather than silently changing active work.

For a plain `agent-tracker` project with intake support:

```bash
uv run agent-tracker intake --config tracking/project.json capture \
  --kind feature --source user --repo <repo-or-component> --tag triage \
  --metadata source_date=<YYYY-MM-DD> \
  --metadata thread=<thread-or-context> \
  "Raw request or idea text"
uv run agent-tracker intake --config tracking/project.json list --json
```

Use `--metadata KEY=VALUE` for small structured context such as source date,
thread, project, owner, or priority. `intake capture` requires kind, source, and
repo, accepts guided kinds `idea`, `feature`, `check`, `concern`, and `note`,
and preserves the user's raw wording as the positional intake text, not as
metadata. Use `--metadata-json` only when the metadata is already a JSON object.
The flat `capture-intake` alias is available for scripts, while looser
`record-intake` remains compatibility-only for intake that lacks full context.

After reviewing an intake item, create a proposed task contract rather than a
claimable task. For ordinary planning, prefer the readable `plan task` command;
it records raw intake and creates the proposed task contract in one audited
operation:

```bash
uv run agent-tracker plan task --config tracking/project.json \
  --task-id <stable-task-id> \
  --title "Task title" \
  --repo <repo-or-component> \
  --kind feature \
  --source <source-or-thread> \
  --role maintainer \
  --write-scope docs/ \
  --validation-check "uv run pytest" \
  --authority "local code and docs" \
  "Raw request or idea text"
uv run agent-tracker plan list --config tracking/project.json --json
```

Use `--intake-metadata KEY=VALUE` for intake context such as source date,
thread, project, owner, or priority. Use `--metadata-json` for proposed task
metadata only. The flat `propose-task` command remains available when intake
already exists and scripts need the old JSON contract.

Proposals are review artifacts. When the user or project workflow approves a
proposal, promote it into live queue state through SQLite rather than editing
the task plan by hand:

```bash
uv run agent-tracker plan promote --config tracking/project.json <proposal-id> \
  --actor <project-manager-id>
```

The flat `list-proposals` and `promote-proposal` aliases remain supported for
existing automation.

If an intake item should not become a task, close or defer it explicitly:

```bash
uv run agent-tracker update-intake --config tracking/project.json <intake-id> \
  --status closed --actor <project-manager-id>
```

Promoted tasks appear in `next` and can be claimed. Definition-only imports
preserve promoted runtime tasks that are absent from the task-plan source; do
not use runtime reconciliation unless you intend the importer source to replace
live queue state.

## Logging And Follow-Up

Agents should log useful work as task evidence, notes, or spool-ingested events.
Completion evidence should point to commits, PRs, notes, reports, or bounded
artifact summaries. Do not store large raw outputs in coordination repos.

SQLite is the canonical live queue state; commits and PRs are evidence and
review surfaces, not live coordination state.

When creating follow-up tasks:

- use stable task IDs;
- keep write scopes explicit;
- add validation checks;
- model human approval/intervention separately from notification;
- preserve links to the evidence that motivated the follow-up.

## Notebooks

Project and repo notebooks should capture durable context that future agents
need: operational conventions, design constraints, validation suites, known
failure modes, sandbox/authority rules, and links to canonical config/state.
Prefer config-root paths such as `tracking/notebooks/project.md` and
`tracking/notebooks/repos/<repo>.md` in projects that use the default
`agent-tracker` prompt renderer, because `prompt_path` and
`metadata.notebook_paths` are resolved relative to the tracker config directory.
Keep raw research notes and large chat exports out of the notebook body; link to
them as sources and summarize only durable decisions.
When planning context has no clear home, create or update a notebook task
instead of burying it in a transient chat.
