---
name: task-worker
description: Implement exactly one claimed agent-tracker task. Use when Codex is given a task ID, rendered task prompt, lease token, or task-specific write scope and asked to make scoped code/docs changes, run focused checks, report evidence, or hand work back without triaging the project-wide queue.
---

# Task Worker

Use this skill when you are the bounded implementer for one already-selected
`agent-tracker` task. You are not the project coordinator or project manager:
do not choose from the queue, triage intake, create or promote tasks, resolve
unrelated leases, or reorganize planning beyond the claimed task.

## Required Inputs

Before editing, identify:

- tracker config or repo-local tracker guide;
- task ID or rendered task prompt;
- lease token when you are expected to heartbeat, complete, fail, or submit the
  task;
- explicit write scope, validation checks, completion policy, and authority.
- assigned branch, base ref, and worktree path when the coordinator is managing
  worktree isolation.

If the user gives a specific task ID but no lease, you may claim that exact task
when project policy allows it. Do not run `next` to select your own work. If the
write scope, lease ownership, or completion policy is unclear, ask the
coordinator or report the missing input instead of widening the job.

When a coordinator supplies branch/worktree context, work only in that assigned
non-canonical worktree unless the coordinator updates the assignment. Do not
edit the canonical repository checkout for implementation. If the project uses a
shared worktree policy, treat it as serial-only unless the coordinator gives a
different explicit policy; parallel agents should not write to the same
worktree.

## Working Loop

1. Read the task prompt and repo-local tracker instructions.
2. Inspect only the code, docs, config, and tests needed for the task.
3. Preserve other agents' work. Do not revert unrelated edits.
4. Confirm the current branch/worktree matches the assigned implementation
   context before editing.
5. Make the smallest scoped changes that satisfy the task.
6. Run focused validation first; run broader checks only when risk or policy
   calls for them.
7. Keep the lease alive if you own one.
8. Stop after this task. Report follow-up work instead of expanding scope.

Useful plain `agent-tracker` commands:

```bash
uv run agent-tracker task --config tracking/project.json <task-id> --markdown
uv run agent-tracker claim --config tracking/project.json --task-id <task-id> --agent <agent-id> --lease-seconds 7200
uv run agent-tracker heartbeat --config tracking/project.json <task-id> --lease-token <lease-token> --agent <agent-id>
```

## Closeout

Close out through the tracker only when you own the lease and the task's policy
allows it:

```bash
uv run agent-tracker complete --config tracking/project.json <task-id> --lease-token <lease-token> --evidence "file:<path>" --evidence "check:<name>"
uv run agent-tracker submit-review --config tracking/project.json <task-id> --lease-token <lease-token> --agent <agent-id> --evidence "pr:<url>"
uv run agent-tracker fail --config tracking/project.json <task-id> --lease-token <lease-token> --reason "<reason>"
```

When a coordinator owns closeout, hand back concise evidence instead:

- files changed;
- checks run and results;
- task evidence URIs such as `file:<path>`, `check:<name>`, `git:<sha>`,
  `pr:<url>`, or artifact paths;
- assigned branch/worktree and diff or commit range reviewed;
- blockers, skipped checks, and proposed follow-up tasks.
