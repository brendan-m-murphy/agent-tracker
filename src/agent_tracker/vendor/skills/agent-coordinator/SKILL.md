---
name: agent-coordinator
description: Coordinate an agent-tracker-backed project end to end. Use when Codex is asked to act as project manager, work through a queue, plan and add tasks before execution, supervise implementation/review agents, check lease health, diagnose workflow friction, or close out code-changing work with commit, review, PR, merge, and tracker evidence.
---

# Agent Coordinator

Use this skill to run an `agent-tracker` project as the coordinator, not just as
one worker. Keep SQLite as live queue state, use task-plan or intake files only
as source definitions, and make friction visible instead of turning it into
hidden manual work.

## Locate The Project

Prefer repo-local wrappers when they exist. Otherwise use:

```bash
uv run agent-tracker overview --config tracking/project.json --limit 10
uv run agent-tracker status --config tracking/project.json --json
```

If `tracking/project.json` does not exist, look for `agent-tracker.config.json`.
If no config is discoverable, ask for the config path.

## Coordination Loop

1. Inspect `overview` before claiming anything. Note ready, active, review,
   integration, blocked, and recently completed work.
2. Confirm the queue is safe to coordinate:
   - no unexpected active task for the same work;
   - no stale lease that needs explicit `--recover-stale-leases`;
   - blocked tasks have understandable requirement details;
   - the next task's write scope does not collide with current local changes.
3. If the requested work is not represented, create or propose tasks before
   implementation. Use a durable intake/proposal mechanism when available. If
   the project has no task-ingest path yet, make the task-plan edit narrow,
   call it friction, import it, and record that fact as evidence.
4. Claim one task with a stable agent ID. Keep the lease token in your working
   notes until completion or failure.
5. Render the task prompt, then implement or coordinate bounded workers.
6. Use review/integration states when code, docs, config, tests, or task plans
   changed and final evidence is not available yet.
7. Complete only after evidence satisfies the task's completion policy.
8. Re-run `overview` and report ready work, active work, and any friction that
   remains.

## Lease Checks

Before trusting a queue, prove leases are working with live commands:

```bash
uv run agent-tracker claim --config tracking/project.json --agent <agent-id> --task-id <task-id> --lease-seconds 7200
uv run agent-tracker heartbeat --config tracking/project.json <task-id> --lease-token <lease-token> --agent <agent-id> --lease-seconds 7200
uv run agent-tracker overview --config tracking/project.json --limit 5
```

A healthy lease has a non-empty token, an expiry in the future, and an active
overview entry showing the same `lease_agent_id`. Invalid owner, missing token,
or expired lease errors are good signs: the queue is enforcing ownership. Use
`--recover-stale-leases` only when you intend to mutate SQLite recovery state.

## Planning And Creating Work

When the user gives rough goals, convert them into task contracts before
execution:

- stable task ID;
- summary and next action;
- repo and role;
- dependencies;
- explicit write scopes;
- validation checks;
- completion policy and direct-merge authority;
- human intervention or approval needs;
- notebook or documentation updates.

Do not make raw ideas claimable until they are triaged. If there is no intake or
task-ingest command, prefer a single small task-plan edit plus import over
untracked side notes, and state that this is remaining product friction.

## Delegating Work

Use subagents only when the current session and user request allow it. Give each
agent one bounded job and an explicit write scope. Tell workers they are not
alone in the codebase and must not revert unrelated changes.

Useful patterns:

- implementation worker for the claimed task's code/docs changes;
- read-only acceptance checklist before implementation;
- read-only reviewer after the concrete diff exists;
- focused test agent when behavior changed and risk is high.

Integrate results yourself. Do not let worker reports substitute for reviewing
the actual diff and live queue state.

## Closeout Policy

For changes to code, docs, config, tests, or task plans:

1. Run focused checks first, then the project's standard validation.
2. Commit on a task branch.
3. Prefer PR or review evidence. If explicitly authorized for local momentum,
   fast-forward/merge to `main`, push when configured, and use `--direct-merge`.
4. Complete the tracker task with cumulative evidence:
   - `git:<sha>`;
   - `pr:<url>`, `review:<id>`, `integration:<id>`, or `--direct-merge`;
   - `file:<path>` for touched files;
   - `check:<name>`;
   - subagent review/worker IDs when used.

If the task source still has to be edited manually to keep definitions aligned,
do it deliberately and record it as friction. The live completion command, not
the task-plan edit, is the terminal queue action.

## Friction Audit

Report these as product issues instead of quietly working around them:

- task creation or closeout requires hand-editing `tracking/tasks.json`;
- agents cannot update tracker state without git writes;
- normal validation requires sandbox escalation for cache or git access;
- active leases cannot be explained by `overview`;
- review or integration states strand work without resolver commands;
- completion policy accepts weak evidence or direct-merge authority implicitly.

When friction is small and safely fixable inside the claimed task, fix it. When
it is larger, add or propose a follow-up task with evidence.
