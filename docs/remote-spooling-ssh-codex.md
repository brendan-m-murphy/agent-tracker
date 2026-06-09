# Remote Spooling For SSH Codex Projects

This runbook verifies the pull-based spool path when a Codex session is opened
against an SSH-backed project workspace. The automated test suite models the
same topology locally, so CI can catch event-spool regressions without requiring
an SSH server.

## Topology

- Canonical project: the local checkout that owns `tracking/project.json` and
  `tracking/state.sqlite`.
- SSH remote project: the project directory opened by Codex over SSH.
- Remote outbox: a directory in or near the SSH project, for example
  `.agent-tracker/spool/outbox`.
- Local inbox: the canonical project's configured `spool.inbox`.
- Pull command: `agent-tracker pull-spool --config tracking/project.json`.
- Ingest command: `agent-tracker ingest-spool --config tracking/project.json
  --actor ssh-codex-spool`.

The canonical project pulls from the remote outbox. Remote agents do not write
the canonical SQLite database.

## Canonical Config

Configure the canonical project with a remote inbox path that is visible from
the local machine. For a mounted SSH filesystem or shared path:

```json
{
  "spool": {
    "inbox": "spool/inbox",
    "done": "spool/done",
    "error": "spool/error",
    "remote_inbox": "/Volumes/ssh-project/.agent-tracker/spool/outbox"
  }
}
```

For an SSH-only host, use an explicit sync step such as `rsync` or `scp` to copy
complete remote files into the configured `remote_inbox`, then run
`pull-spool`. Keep that sync outside `agent-tracker` until a dedicated transport
adapter exists.

## Remote Agent Publication

The remote Codex agent writes one JSON object per event. It should publish
atomically by writing a temporary file first:

```bash
mkdir -p .agent-tracker/spool/outbox
cat > .agent-tracker/spool/outbox/remote-event.json.partial <<'JSON'
{
  "event_id": "ssh-codex-20260609-001",
  "kind": "codex.remote_spool",
  "task_id": "remote-spooling-ssh-codex-test",
  "artifact": "file:.agent-tracker/spool/outbox/remote-event.json"
}
JSON
mv .agent-tracker/spool/outbox/remote-event.json.partial \
  .agent-tracker/spool/outbox/remote-event.json
```

Files ending in `.partial`, `.part`, or `.tmp` are ignored by `pull-spool`.

## Local Verification

From the canonical project:

```bash
agent-tracker pull-spool --config tracking/project.json --dry-run
agent-tracker pull-spool --config tracking/project.json
agent-tracker ingest-spool --config tracking/project.json --actor ssh-codex-spool
agent-tracker pull-spool --config tracking/project.json
```

Expected evidence:

- the dry run lists the complete remote `*.json` event and does not create a
  local inbox file;
- the real pull copies the complete file into the canonical local inbox and
  leaves the remote outbox file in place;
- ingest records the event and moves the local file to `spool.done`;
- the repeat pull skips the already ingested identical file with `skip_done`;
- malformed local files move to `spool.error` during ingest and remain visible
  for diagnosis.

## Queue-Mutation Boundary

Event spool is available today and is covered by automated tests. Queue
mutation from remote agents uses the task-ingest command contract in
`docs/task-ingest-command-contract.md`:

- remote agents write command request files instead of opening SQLite;
- the local processor applies `claim`, `heartbeat`, `complete`, `fail`,
  review, integration, intake, proposal, and promotion commands;
- responses are written to a durable `commands/responses` path so the remote
  agent can read lease tokens, completion results, or error details;
- command requests and event files must not share a schema or inbox.

Until a task-ingest processor is implemented, SSH/Codex end-to-end testing
should cover event-spool pull/ingest behavior and manually inspect command
request/response examples against the contract. That keeps remote evidence
collection usable without pretending remote queue mutation is already
implemented.

