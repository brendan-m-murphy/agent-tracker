# Task-Ingest Command Contract

Task ingest is the mediated command path for queue mutations from agents that
cannot or should not open the canonical SQLite database directly. It is distinct
from event ingestion, raw intake, and proposed-task review:

- events report facts that already happened and are idempotent by `event_id`;
- raw intake stores untriaged ideas or checks and does not create claimable
  work;
- proposed tasks are draft task contracts waiting for promotion;
- task-ingest commands request state changes against the canonical queue and
  must receive a durable response.

The initial transport is filesystem based so it works for local worktrees,
shared filesystems, and SSH-backed Codex projects. Future adapters can expose
the same request and response payloads through MCP or HTTP, but the service
layer remains the owner of SQLite writes.

## Directory Layout

A command spool has separate request, processing, archive, error, and response
paths. Relative paths resolve below the project `state_root`.

```text
commands/
  inbox/        # complete request JSON files ready to process
  processing/   # request files claimed by the local processor
  done/         # accepted request files after a response is written
  error/        # malformed or rejected request files
  responses/    # durable response JSON files, one per command_id
```

Remote writers publish requests by writing to a temporary name ending in
`.partial`, `.part`, or `.tmp`, then atomically renaming to `<command_id>.json`.
Processors ignore temporary names. Processors write the response before moving a
valid request to `done` so a remote agent can observe the result even if archive
movement fails.

## Request Payload

Every request file contains one JSON object.

```json
{
  "schema_version": 1,
  "command_id": "cmd-20260609-claim-docs",
  "idempotency_key": "remote-agent-1:claim:docs-task:1",
  "project_id": "agent-tracker-self",
  "command": "claim",
  "actor": {
    "id": "remote-agent-1",
    "role": "worker",
    "authority": ["claim", "heartbeat"]
  },
  "task_id": "docs-task",
  "lease_token": "",
  "payload": {
    "lease_seconds": 7200
  },
  "reply_to": "commands/responses/cmd-20260609-claim-docs.json"
}
```

Required fields:

- `schema_version`: integer contract version. Version `1` is this contract.
- `command_id`: stable unique identifier for the request file and response.
- `idempotency_key`: stable key for retrying the same command.
- `project_id`: target project. Mismatches are rejected before mutation.
- `command`: one of the supported commands below.
- `actor.id`: identity recorded in audit and used for lease ownership.

Optional common fields:

- `actor.role` and `actor.authority`: authorization hints for future approval
  gates. They are audit metadata until authority enforcement exists.
- `task_id`: required for task-scoped commands.
- `lease_token`: required for commands that mutate a claimed task.
- `payload`: command-specific inputs.
- `reply_to`: explicit response path. When omitted, processors write
  `responses/<command_id>.json`.

## Response Payload

Every processable request receives one response JSON object.

```json
{
  "schema_version": 1,
  "command_id": "cmd-20260609-claim-docs",
  "idempotency_key": "remote-agent-1:claim:docs-task:1",
  "project_id": "agent-tracker-self",
  "command": "claim",
  "status": "succeeded",
  "duplicate": false,
  "actor_id": "remote-agent-1",
  "task_id": "docs-task",
  "result": {
    "lease_token": "0f3d13cf60b443b6bb18012cbe5509b3",
    "lease_expires_at": "2026-06-09T15:30:03+00:00",
    "state": "claimed"
  },
  "error": null,
  "audit_id": 42
}
```

`status` values are:

- `succeeded`: the mutation was applied or an idempotent duplicate matched the
  original command.
- `rejected`: the request was valid JSON but cannot be applied, for example a
  project mismatch, missing lease token, unmet dependency, or unauthorized
  command.
- `failed`: processing encountered an internal error after validation.

`duplicate` is `true` when the same `idempotency_key` is replayed with the same
command body and the stored response is returned. A replay with the same
`idempotency_key` but different command body is rejected as an idempotency
conflict and must not mutate state.

## Supported Commands

The command surface mirrors existing service and CLI mutations. A processor
must call the same service methods used by local CLI commands.

| Command | Required fields | Result |
| --- | --- | --- |
| `claim` | `task_id`, `actor.id`, `payload.lease_seconds` | lease token, expiry, task state |
| `heartbeat` | `task_id`, `actor.id`, `lease_token`, `payload.lease_seconds` | renewed expiry |
| `complete` | `task_id`, `actor.id`, `lease_token`, `payload.evidence` | final state and evidence |
| `fail` | `task_id`, `actor.id`, `lease_token`, `payload.reason` | error state and reason |
| `submit_review` | `task_id`, `actor.id`, `lease_token`, `payload.evidence` | review state |
| `await_integration` | `task_id`, `actor.id`, `lease_token`, `payload.status`, `payload.evidence` | integration state |
| `resolve_review` | `task_id`, `actor.id`, `payload.evidence` | active or terminal state |
| `resolve_integration` | `task_id`, `actor.id`, `payload.evidence` | terminal state |
| `record_intake` | `actor.id`, `payload.kind`, `payload.source`, `payload.description` | intake id |
| `propose_task` | `actor.id`, `payload.intake_id`, `payload.task` | proposal id |
| `promote_proposal` | `actor.id`, `payload.proposal_id` | promoted task id |

Completion evidence rules are unchanged. Code-changing tasks still need the
configured evidence such as `git:`, `pr:`, review, integration, or an explicit
direct-merge override.

## Lease Handling

`claim` is the only command that creates a lease token. The response must return
the token and expiry. `heartbeat`, `complete`, `fail`, `submit_review`, and
`await_integration` must include the current token and the same `actor.id` that
owns the lease, unless a future authority rule explicitly delegates that power.

Missing, expired, or wrong-owner tokens are rejected and archived to `error`
with a response explaining the refusal. A rejected command does not recover
stale leases implicitly; stale recovery remains an explicit local maintenance
operation.

## Error Handling

Malformed JSON files move to `error`. If a `command_id` can be read, the
processor also writes a response with `status: "rejected"` and an error object:

```json
{
  "code": "invalid_payload",
  "message": "command request must contain a JSON object"
}
```

Recommended error codes are:

- `invalid_payload`
- `unsupported_schema_version`
- `project_mismatch`
- `unknown_command`
- `idempotency_conflict`
- `missing_lease_token`
- `invalid_lease_owner`
- `lease_expired`
- `dependency_blocked`
- `completion_evidence_missing`
- `authority_required`
- `internal_error`

Error responses should be concise and safe to show in remote logs. Detailed
tracebacks belong in local logs or audit artifacts, not in the command response.

## Processing Rules

Processors should use an exclusive local claim by moving a request from `inbox`
to `processing`. If the move fails because another processor already claimed
the file, skip it. After processing:

1. validate schema, project, command, actor, lease, and idempotency;
2. run the corresponding `Coordinator` method against canonical config/state;
3. write or reuse `responses/<command_id>.json`;
4. move the request file to `done` for successful commands or `error` for
   rejected/failed commands;
5. record audit with the actor, command, request digest, response path, and
   final archive path.

The processor owns all SQLite writes. Remote agents only write request files and
read response files.

