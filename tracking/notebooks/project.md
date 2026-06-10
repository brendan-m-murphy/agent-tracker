# Agent Tracker Self-Dogfood Project Notebook

- Last reviewed: 2026-06-09
- Owner: project-manager
- Sources:
  - docs/research/2026-06-08-coordination-intake-dispatch.md
  - /Users/bm13805/Downloads/ChatGPT-Codex_Chat_Communication.md

## Purpose

This notebook is the durable project-level context for the
`agent-tracker-self` queue. Keep it curated and bounded so task prompts can
include it without pulling raw chat history into every agent run.

## Canonical State

- Project config: `tracking/project.json`
- Task source: `tracking/tasks.json`
- Live SQLite state: `tracking/state.sqlite`
- Spool inbox: `tracking/spool/inbox`
- Snapshot export: `tracking/exports/snapshot.json`

SQLite is the canonical live queue state. Commits, PRs, Markdown exports,
mailbox messages, and issue comments are evidence or notification surfaces, not
the live coordination bus.

## Notebook Convention

- Project context lives in `tracking/notebooks/project.md`.
- Repo-specific context lives in `tracking/notebooks/repos/<repo>.md`.
- Task `prompt_path` values are relative to `tracking/project.json`.
  `metadata.notebook_paths` checks that same directory first, then safely
  falls back to the configured task source root for paths such as
  `notebooks/project.md`.
- Use `agent-tracker notebook list/show/append --config tracking/project.json`
  for routine notebook discovery and concise reviewed updates.
- Use repeatable `--notebook-path` on proposed-task commands when a future
  worker prompt should include project or repo notebook context. Keep
  `--notebook-update` for notebooks the worker should review or change.
- Keep raw research notes in `docs/research/` and link to them from notebooks
  after summarizing durable decisions.

## Coordination Rules

- Do not make raw ideas claimable until a project-manager has triaged them into
  proposed task contracts.
- Include write scopes, validation checks, authority, intervention needs, and
  notebook updates in proposed tasks.
- Model human approval and intervention separately from notification mechanics.
- Use PR comments, issue comments, and exports as notification surfaces only
  after durable intervention states exist.

## Execution Adapter Notes

The long-term execution adapter can invoke Codex through an SDK or App Server
control plane, but the queue remains the source of truth. Store thread IDs,
prompt paths, report paths, and review summaries as evidence.

Agents should not wait on long-running Slurm jobs, notebook cells, slow tests,
or nested worker chains. Schedulers, callbacks, daemons, or attendants wait.
Agents should wake only when new evidence exists.

## Open Design Follow-Ups

- Human intervention queue: define reasons, state transitions, audit records,
  and resolution evidence.
- PR notification setup and exporter: depend on durable intervention states.
- Worker adapter: claim a task, render a prompt, run a Codex thread, and record
  thread/report evidence without owning coordination state.


## 2026-06-10 - codex-remote-agent-coordinator

## 2026-06-10 bp1 SSH Project-Manager Smoke Test

- Ran the new SSH launch-worker path with a temporary workspace for bm13805@bp1:/group/chem/acrg/brendan_for_codex/hpc-ci-project-tracker.
- Launch succeeded with return code 0. Local report artifact: /private/tmp/agent-tracker-bp1-smoke-state/workers/bp1-hpc-ci/agent-tracker-bp1-smoke-bp1-hpc-ci-cdd20fcc6750/report.md. Remote report artifact: /group/chem/acrg/brendan_for_codex/hpc-ci-project-tracker/.agent-tracker/workers/agent-tracker-bp1-smoke-bp1-hpc-ci-cdd20fcc6750/report.md.
- Remote status report: tracker is on main at b20f965, clean, tracking origin/main. Ready work: acrg-data-validation-profiles, hpc-ci-collect-hpc-validation, hpc-ci-compare-design. No active or review/integration work was visible from committed tracker files. Blocked follow-ups include hpc-ci-pr-collection-automation, hpc-ci-compare-implementation, hpc-ci-worktrees, and hpc-ci-report-sync-cleanup.
- Structural limitations reported by the remote worker: project-manager skill was not available in that Codex session; no local .venv exists; uv status/overview was blocked by cache/environment constraints; default python3 is 3.6.8 so scripts/hpc_tracker.py needs python3.11 or an environment wrapper. The report was derived directly from committed tracker files and recent notes/results.
