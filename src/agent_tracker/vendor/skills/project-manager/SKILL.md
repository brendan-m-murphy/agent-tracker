---
name: project-manager
description: Manage an agent-tracker-backed project. Use when the user asks to pull the next task, triage ideas/features/checks, organize follow-up work, review project status, update notebooks, or coordinate agents without manually scanning task files.
---

# Project Manager

Use this skill for project-manager work in repositories coordinated by
`agent-tracker` or by a repo-local wrapper around it.

## Locate The Queue

Prefer repo-local wrappers when they exist:

- If `tracking/README.md`, a project notebook, or another repo-local guide
  documents a tracker wrapper, use that wrapper's documented commands for
  listing, claiming, rendering, and logging work.
- Otherwise, look for `tracking/project.json`.
- If neither exists, look for `agent-tracker.config.json`.
- If no config is discoverable, ask for the tracker config path.

## Pull The Next Task

When the user asks to pull the next task:

1. Import/sync the task plan into live state.
2. List the next ready task with the requested role or the role implied by the
   project docs.
3. Claim the task with a clear `agent_id`.
4. Render the task prompt/context.
5. Keep the `lease_token` for heartbeat, complete, or fail.

For a plain `agent-tracker` project:

```bash
uv run agent-tracker import --config tracking/project.json
uv run agent-tracker next --config tracking/project.json --role maintainer --limit 1
uv run agent-tracker claim --config tracking/project.json --agent <agent-id> --role maintainer --lease-seconds 7200
uv run agent-tracker task --config tracking/project.json <task-id> --markdown
```

Before implementation, read the rendered task prompt and repo-local tracker
guide for write scope, validation, closeout, and authority rules. For tasks that
change tracked code, docs, config, tests, or task plans, expect to work on a
task branch, commit the scoped changes, and leave a PR or equivalent review
state before completing the tracker task. Only use a direct merge into `main`
when the repo-local policy or trusted manager explicitly authorizes that
override, and record the merged commit as evidence.

If claim fails, investigate before reporting a blocker:

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
5. If the current project has no intake feature yet, add a planning task or a
   repo-local note rather than silently changing active work.

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
When planning context has no clear home, create or update a notebook task
instead of burying it in a transient chat.
