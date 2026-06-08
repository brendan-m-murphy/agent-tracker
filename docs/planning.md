# Agent Tracker Planning Notes

This document records the intended direction for `agent-tracker` beyond the
first implementation pass. It is deliberately project-agnostic. HPC CI details
belong in the project config and adapter plugin in the tracker repo, not in
`agent_tracker` core.

## Current Implementation

The first pass provides a local, SQLite-backed coordination package with:

- generic project/task/dependency state;
- computed `ready` and `blocked` states;
- task claiming with lease tokens;
- heartbeat, complete, fail, evidence, event, audit, and JSON export support;
- a CLI;
- MCP-friendly Python handler methods;
- plugin interfaces for task import, prompt rendering, event adaptation, and
  export;
- a project-specific adapter in `hpc-ci-project-tracker`.

The current spool support is local only. `agent-tracker` can ingest JSON files
from a configured local inbox directory and move them to done/error folders, but
it does not yet provide a cross-network spool bridge, polling daemon, SSH/rsync
puller, HTTP receiver, or shared-filesystem transport.

## Subagent Recommendations To Preserve

The transcript review recommended this overall shape:

- Replace git as the live coordination bus with a small API/server backed by
  durable state.
- Keep git/tracker repos as the audit/archive layer for docs, decisions,
  summaries, and durable handoffs.
- Use formal task states such as `pending`, `ready`, `claimed`, `in_progress`,
  `done`, `error`, and `deferred`, plus dependencies, owner/agent role, repo,
  sandbox, validation requirements, and evidence links.
- Separate responsibilities:
  - feature agents make scoped code changes in implementation repos;
  - a coordinator decomposes goals into task contracts and routes follow-ups;
  - a run manager handles canonical suite/job submission;
  - an attendant/reviewer wakes on events, inspects evidence, and writes reports;
  - the API/server owns claims, locks, retries, state, and audit logs.
- Prefer one agent thread per validation run or task for auditability, with a
  durable ledger mapping `task_id` or `run_id` to thread/report/evidence.
- Treat callbacks as event producers only. A controlled worker should decide
  whether to invoke agents or create follow-up tasks.
- Avoid long-running LLM agents as pollers. Cheap daemons should wait; agents
  should wake only when evidence is available.
- Add suite-level aggregation separately from per-job callback review.
- Preserve human-auditable artifacts: prompt, event JSON, inspected files,
  thread ID, review report, follow-up task, and final decision.

Important risks from the transcript:

- Local app-server or Codex-control APIs should not be exposed directly over an
  HPC/network boundary without a security model.
- MCP tool handlers are useful but should not be treated as durable state by
  themselves.
- Queue semantics must include idempotency, leases, stale-claim recovery,
  retries, cancellation, rate limits, priority, dependency resolution, and human
  approval gates.
- Event evidence, canonical run state, agent diagnosis, and final CI/human
  policy are distinct records and should not be collapsed into one status flag.
- Auth, secrets, and network boundaries need explicit design before direct
  remote writes are allowed.

## Missing Pieces Needed For A Useful V1

These items are not complete in the current implementation and should be
addressed before treating `agent-tracker` as the operational coordination
system.

### Cross-Network Spool Bridge

Current state:

- Local file ingestion exists.
- No process moves remote event files into the local inbox.
- No daemon continuously ingests events.

Needed:

- A configured spool layout with `remote_inbox`, `local_inbox`, `done`, and
  `error` locations.
- A pull command such as `agent-tracker pull-spool` that can copy event files
  from a configured source into the local inbox without deleting remote evidence.
- A safe idempotency convention, for example stable event IDs plus content hash.
- Optional lock files or atomic rename rules so partially written event files
  are not ingested.
- A dry-run mode that lists pending event files and expected moves.

Future transport options:

- shared filesystem path;
- `rsync` or `scp` pull over SSH;
- object storage bucket;
- GitHub artifact/issue/comment bridge;
- authenticated HTTP event receiver;
- message broker only after the event model is stable.

Recommendation:

Start with a pull-based file spool. It is safer than exposing a local service to
the network and fits HPC environments where outbound/inbound networking may be
restricted.

### Actual MCP Server Packaging

Current state:

- `agent_tracker.mcp_tools.AgentTrackerTools` contains MCP-friendly handlers.
- There is no registered MCP server process or SDK integration yet.

Needed:

- A thin MCP server entrypoint that exposes the existing handler methods as
  tools.
- Tool schemas for claim, heartbeat, complete, fail, record evidence, record
  event, render prompt, and list ready tasks.
- A clear mapping between actor identity and MCP caller identity.
- Tests that exercise the tool layer without requiring the full app runtime.

Recommendation:

Keep business logic in the service layer. Treat MCP as one adapter beside CLI
and future HTTP.

### Config Validation And Schema Versioning

Current state:

- Config is JSON and loaded directly.
- SQLite schema is created opportunistically.
- There is no config schema version or DB migration mechanism.

Needed:

- `config_schema_version`.
- Required/optional field validation with actionable errors.
- DB schema version table.
- Migration commands for future schema changes.
- Documented compatibility policy for plugins.

Recommendation:

Add lightweight dataclass validation before adding new dependencies. Move to a
validation library only if config shape becomes complex.

### Agent Authority And Approval Gates

Current state:

- Role filtering exists only as optional task metadata.
- Approval gates are planned but not enforced.

Needed:

- Configurable roles and allowed operations.
- Task-level authority: repos, write scopes, allowed environments, and whether a
  task can be claimed automatically.
- Approval-required state for risky actions such as real job submission,
  threshold changes, destructive cleanup, or publishing.
- Audit entries for approvals and denials.

Recommendation:

Model approval as task state/metadata, not as comments in prompt text.

### Follow-Up Planning

Current state:

- Dependencies can make existing tasks ready.
- No deterministic follow-up task creation is implemented.
- No coordinator/attendant worker exists.

Needed:

- A `propose-task` or `create-task` flow.
- Follow-up planner plugins that can create project-specific tasks from
  completed tasks or events.
- A review mode where proposed tasks wait for human approval.
- A scheduled or event-driven attendant command that ingests events, exports
  snapshots, and proposes follow-ups.

Recommendation:

Start with deterministic plugin-generated proposals. Add LLM-generated proposals
only behind human approval.

### Export And Audit Policy

Current state:

- JSON snapshot export exists.
- Existing Markdown notes/results are still produced by the legacy tracker
  commands, not by generic export plugins.

Needed:

- Decide which records are canonical in SQLite and which are derived exports.
- Export notes/results/task snapshots without requiring agents to edit git.
- Retain links to large artifacts without copying them.
- Define retention for events, stale leases, and audit logs.

Recommendation:

SQLite should own live coordination state. Git exports should be readable,
reviewable summaries and snapshots.

## Future Architecture Options

### Stay Local-First With File Spool

Best for:

- personal or small-team use;
- HPC environments with uncertain networking;
- high auditability and low operational burden.

Tradeoffs:

- no live remote API;
- spool sync must be configured per environment;
- not ideal for many simultaneous writers.

Recommendation:

This is the best next step.

### Add HTTP API

Best for:

- multiple machines that can safely reach a central service;
- dashboards or non-agent clients;
- direct callback/event submission.

Required first:

- authentication;
- TLS or trusted network boundary;
- request idempotency;
- rate limits;
- write authorization per project/role.

Recommendation:

Delay until the local service and event model are stable.

### Switch SQLite To Postgres

Best for:

- multiple concurrent writers;
- central service deployment;
- richer querying and dashboards.

Tradeoffs:

- operational setup;
- migrations become mandatory;
- harder local bootstrap.

Recommendation:

Keep SQLite for v1. Add a storage abstraction only when a real multi-host write
need appears.

### Use Temporal, Prefect, Or A Task Queue

Temporal:

- strong durable workflow and retry semantics;
- useful if task execution becomes long-running and production-critical;
- heavier than the current coordinator needs.

Prefect:

- useful for scheduled workflows and worker pools;
- less natural as the project knowledge/audit source.

Celery/RQ/Dramatiq:

- useful worker backends;
- not replacements for planner state, audit, or task contracts.

Recommendation:

Do not adopt these until `agent-tracker` has a stable task/event contract and a
clear need for external execution workers.

### Use GitHub Issues Or Linear As Queue Backend

Best for:

- avoiding custom infrastructure;
- human-visible task tracking;
- using existing hosted APIs and permissions.

Tradeoffs:

- weaker lease semantics;
- awkward dependency/state computation;
- project-specific automation glue still required.

Recommendation:

Consider as an export/sync target, not as the first source of truth.

## Recommended Next Milestones

1. Create user documentation for installation, configuration, core workflows,
   plugin authoring, spool ingestion, and self-hosted project operation.
2. Add config validation, DB schema versioning, and migration tests.
3. Implement pull-based cross-network spool support with dry-run and idempotent
   ingestion.
4. Add an actual MCP server entrypoint around the existing handler methods.
5. Add approval gates and role/authority enforcement.
6. Move HPC tracker Markdown note/result export into the plugin so agents do not
   need to write tracker files.
7. Add deterministic follow-up task proposal support.
8. Add an attendant command that can run cheaply on a schedule: ingest spool,
   recover stale leases, export snapshots, and propose follow-ups.

## Non-Goals For Now

- Exposing a local Codex/app control API over the network.
- Letting callbacks launch high-intelligence agents directly.
- Replacing project-specific plugins with generic heuristics.
- Copying large artifacts into the tracker or package repository.
- Making git the live queue again under a different abstraction.
