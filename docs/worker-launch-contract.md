# Worker Launch Contract

This page defines the shared worker launch contract for local workspaces and
SSH-backed workspaces. It is an operational contract for coordinators, launch
adapters, and workers; it does not change the queue state machine by itself.

Current implementation status:

- local workspace launches can prepare prompts and execute local commands;
- SSH workspace launches can upload prompts, execute configured remote
  commands, collect remote reports/stdout/stderr into local launch artifacts,
  and publish worker-launch events through the remote `spool_outbox`;
- SSH/SFTP event collection is available through `pull-spool`;
- SSH launch-time claims write task-ingest command files which must be
  processed by the canonical project config.

Remote workers must not open the canonical SQLite database directly.

## Authorities

The canonical tracker owns:

- the canonical config path and SQLite database;
- task state, leases, evidence, audit entries, and stale-lease recovery;
- processing of task-ingest command files;
- ingestion of event spool files pulled from remote outboxes.

The launcher owns:

- workspace resolution and validation;
- prompt rendering and prompt delivery;
- launch ID creation;
- launch artifact paths;
- process execution for supported adapters;
- timeout and startup-failure reporting;
- worker-launch event publication when a workspace has `spool_outbox`.

The worker owns:

- reading the handed-off prompt;
- doing the assigned task inside the assigned worktree;
- writing a final report when the adapter exposes a report path;
- emitting useful stdout, stderr, and spool events;
- heartbeating and closing out queue work through the allowed queue-mutation
  path for its location.

## Launch Inputs

Every launch is tied to a configured workspace. The workspace kind determines
which execution adapter may run:

| Workspace kind | Current `launch-worker` behavior | Queue mutation path |
| --- | --- | --- |
| `local` | prepare artifacts; optionally execute a local command | direct canonical CLI/service calls, or task-ingest if acting as a mediated worker |
| `ssh` | validate/list only; launch requests are rejected before artifact creation | task-ingest command files processed by canonical config |

The launch request may provide:

- `workspace`: configured workspace name;
- `task_id`: task whose prompt should be rendered;
- `prompt` or `prompt_file`: free-form prompt when no task prompt is used;
- `agent`: worker actor ID, defaulting to `worker-launch:<workspace>`;
- `role`: role passed to a claim when claiming during launch;
- `lease_seconds`: requested lease duration when `--claim-task` is used;
- `branch`, `base_ref`, and `worktree_path`: assignment context appended to the
  prompt and exposed to the command;
- `claim_task`: whether the launcher should claim the task before launch;
- `execute`: whether to run a command after preparing artifacts;
- `dry_run`: whether to report the planned launch without writing artifacts;
- `timeout_seconds`: local process timeout, where `0` means no timeout;
- `command`, `command_string`, or workspace `worker_command`: command argv for
  local execution.

For task launches, the target workspace or assigned worktree must not contain
the canonical tracker config. Implementation workers need isolated writable
worktrees so parallel workers do not share the same write target.

## Prompt Handoff

For `task_id` launches, the canonical tracker renders the task prompt and
appends coordination context:

- worktree policy;
- PR policy;
- assigned branch;
- base ref;
- assigned worktree;
- notes that implementation must happen in the assigned non-canonical worktree.

For free-form launches, the provided prompt text is used instead, then the same
coordination context is appended.

Local launches persist the final prompt at:

```text
<artifacts_path>/<launch_id>/prompt.md
```

When `--execute` is used, the prompt is passed to the local worker command on
stdin. The default local command is:

```bash
codex exec --cd {worktree_path} --output-last-message {report_path} -
```

Local command arguments may use these placeholders:

- `{agent_id}`
- `{base_ref}`
- `{branch}`
- `{launch_id}`
- `{project_id}`
- `{prompt_path}`
- `{report_path}`
- `{task_id}`
- `{workspace}`
- `{workspace_path}`
- `{worktree_path}`

The local process environment also receives:

- `AGENT_TRACKER_WORKER_AGENT_ID`
- `AGENT_TRACKER_WORKER_BASE_REF`
- `AGENT_TRACKER_WORKER_BRANCH`
- `AGENT_TRACKER_WORKER_LAUNCH_ID`
- `AGENT_TRACKER_WORKER_PROMPT`
- `AGENT_TRACKER_WORKER_REPORT`
- `AGENT_TRACKER_WORKER_TASK_ID`
- `AGENT_TRACKER_WORKER_WORKSPACE`
- `AGENT_TRACKER_WORKER_WORKTREE`

Prompt-only hosts, including MCP hosts, must treat the returned prompt and
coordination assignment as the launch handoff. They should not re-render a task
from a copied config or infer a different worktree.

## Lease Ownership

`launch-worker --claim-task` claims the task before launch artifacts are
written. The lease owner is the explicit `--agent` value, or
`worker-launch:<workspace>` when no agent is supplied. The launch result includes
the lease token and expiry.

If `--claim-task` is not used, launch preparation does not create a lease. A
worker that needs to mutate task state must claim through the appropriate queue
path before calling heartbeat, complete, fail, review, or integration commands.

Lease rules:

- heartbeats and lease-scoped mutations must use the active lease token;
- the actor must match the lease owner unless a future authority rule delegates
  that operation;
- workers should heartbeat before long work approaches lease expiry;
- a failed launch result does not automatically release or fail the task;
- stale recovery remains an explicit canonical maintenance operation.

`dry_run` cannot be combined with `claim_task`; dry runs do not mutate queue
state or create artifacts.

## Local Execution

A local launch has one of these statuses:

| Status | Meaning |
| --- | --- |
| `dry_run` | planned paths and command are returned; no artifacts are written |
| `prepared` | prompt, placeholder report, and launch JSON were written; no command ran |
| `succeeded` | local command ran and returned `0` |
| `failed` | local command returned non-zero, failed to start, or timed out |

Artifacts are written under:

```text
<artifacts_path>/<launch_id>/
  prompt.md
  report.md
  stdout.txt
  stderr.txt
  launch.json
```

Execution behavior:

- stdout is captured in `stdout.txt`;
- stderr is captured in `stderr.txt`;
- `AGENT_TRACKER_WORKER_REPORT` points at `report.md`;
- if the worker does not write `report.md`, the launcher writes stdout there;
- if there is no stdout, the report says that no report was produced;
- `launch.json` stores the launch result, command argv, artifacts, coordination
  assignment, and return code when a command ran.

When a task ID is supplied, the launcher records task evidence for:

- `worker-launch:<launch_id>`;
- `file:<launch.json>`.

That evidence records that a worker was launched. It is not completion evidence
by itself.

## Timeout And Cancellation

`--timeout-seconds` applies to the local process only. A timeout produces:

- launch status `failed`;
- return code `124`;
- any captured stdout and stderr;
- a stderr line describing the timeout;
- a launch JSON artifact with the failed result.

Timeout does not automatically complete, fail, or release the task lease. The
lease owner or a canonical operator must make an explicit queue decision after
inspection:

- heartbeat if the worker is still active and legitimately needs more time;
- release if the work is stopping early and should become claimable again;
- fail if the task should become terminally failed;
- complete, submit review, or await integration only when the task contract is
  actually satisfied.

The current local launcher has no separate interactive cancel command. If an
operator or future adapter cancels a running worker, the adapter must record the
launch outcome and artifacts, then use an explicit queue mutation for the task
state. Cancellation must not silently edit SQLite or clear a lease.

## Spool Outbox Reporting

If a workspace has `spool_outbox`, the launcher writes an atomic event file for
the launch:

```json
{
  "event_id": "worker-launch-<launch-id>",
  "kind": "agent_tracker.worker_launch",
  "task_id": "<task-id>",
  "workspace": "<workspace>",
  "status": "<launch-status>",
  "artifact": "file:<launch.json>",
  "report": "file:<report.md>",
  "created_at": "<utc timestamp>"
}
```

The canonical project can collect these events with:

```bash
agent-tracker pull-spool --config tracking/project.json
agent-tracker ingest-spool --config tracking/project.json --actor spool
```

Worker-launch events are observability records. They do not claim tasks,
heartbeat leases, complete tasks, fail tasks, or replace task-ingest responses.
Event spool files and task-ingest command files must use separate schemas and
separate inboxes.

## SSH Launch Boundary

SSH workspaces are part of the workspace registry. `launch-worker` connects to
the configured host, uploads the rendered prompt to the remote artifacts path,
runs the configured command in the assigned remote worktree, collects the
remote report/stdout/stderr into local launch artifacts, and writes a remote
worker-launch event when `spool_outbox` is configured.

The SSH adapter preserves this boundary:

- the canonical side renders or approves the exact prompt handoff;
- the remote side receives the assigned branch, base ref, worktree, prompt, and
  report/log paths;
- stdout, stderr, report, launch metadata, and cancellation or timeout outcomes
  are durable artifacts;
- launch-time claims and remote queue mutations return through task-ingest
  responses;
- remote workers never open canonical SQLite directly.

## Remote Queue Mutations

Remote workers mutate queue state by writing task-ingest command request files
and reading durable responses. The canonical project processes those files with:

```bash
agent-tracker process-task-ingest --config tracking/project.json --json
```

The command contract is defined in
`docs/task-ingest-command-contract.md`. Operationally:

- request files include `project_id`, `command_id`, `idempotency_key`,
  `command`, `actor.id`, and command-specific payload;
- claim responses return the lease token and expiry;
- heartbeat, complete, fail, review, and integration commands must include the
  current lease token when the command is lease-scoped;
- idempotent retries must reuse the same semantic command body;
- responses report `succeeded`, `rejected`, `failed`, or `pending`;
- the canonical processor owns all SQLite writes.

Remote workers may write command files and read response files. They may not:

- open the canonical SQLite file;
- write task status, leases, evidence, or audit rows directly;
- run mutating queue commands through a non-canonical config;
- mix event-spool payloads with task-ingest command payloads.

## Operational Sequences

Local prepared launch:

```bash
agent-tracker launch-worker --config tracking/project.json \
  --workspace hpc \
  --task-id write-readme \
  --branch codex/write-readme \
  --base-ref main \
  --worktree-path /path/to/worktrees/write-readme
```

Local executed launch with a launch-time claim:

```bash
agent-tracker launch-worker --config tracking/project.json \
  --workspace hpc \
  --task-id write-readme \
  --agent hpc-worker-1 \
  --claim-task \
  --lease-seconds 7200 \
  --execute
```

Remote queue claim through task-ingest:

1. Remote worker writes `commands/inbox/<command_id>.json` atomically.
2. Canonical operator or service runs `process-task-ingest` with canonical
   config.
3. Remote worker reads `commands/responses/<command_id>.json`.
4. Remote worker uses the returned lease token for heartbeat and closeout
   command files.

SSH executed launch:

1. Configure and validate the SSH workspace.
2. Use `list-workspaces` to inspect it.
3. Run `launch-worker --workspace <ssh-workspace> --execute`.
4. Inspect local launch artifacts for prompt, report, stdout, stderr, and
   `launch.json`.
5. Pull and ingest remote spool events when the workspace publishes an outbox.
6. Run `process-task-ingest` when the launch or worker writes queue mutation
   command files.
