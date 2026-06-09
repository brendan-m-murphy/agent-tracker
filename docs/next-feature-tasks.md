# Agent Tracker Next Feature Tasks

This file converts the roadmap in `docs/planning.md` into concrete next tasks.
It is intentionally implementation-facing and can be mirrored into an
`agent-tracker` task plan for dogfooding.

## Priority Tasks

### 1. Create User Documentation

Goal:

- Give a new user enough context to install, configure, run, and extend
  `agent-tracker` without reading the source first.

Scope:

- Expand the README with a complete local quickstart.
- Add a config reference covering required fields, optional plugin keys, spool
  paths, and path resolution.
- Document the task plan JSON format and lifecycle commands.
- Document plugin contracts for importers, prompt renderers, event adapters,
  follow-up planners, and exporters.
- Add an operations guide for local spool ingestion, leases, stale recovery,
  exports, and audit snapshots.

Acceptance criteria:

- A user can create a minimal project config and task plan from docs alone.
- Every public CLI command has a documented purpose and example.
- Plugin authors can implement a project-local importer or exporter from the
  documented protocol.

### Safety Task: Define Canonical State And Task Source Contract

Goal:

- Prevent copied worktrees and imported task plans from acting as competing live
  state authorities.

Scope:

- Define how a project identifies canonical SQLite state when commands run from
  copied worktrees or generated agent worktrees.
- Split task definition import from runtime state reconciliation.
- Prevent imported task descriptions from casually overwriting leases, evidence,
  audit records, or terminal task status.
- Make mutating commands report or refuse ambiguous resolved config, database,
  task-source, and spool paths.
- Split read-only inspection from stale-lease recovery where needed.

Acceptance criteria:

- A copied worktree config cannot silently mutate an independent SQLite database
  for the same project.
- Task definitions can be human/agent-readable without making git the live
  queue state source.
- Reconciliation of imported statuses is explicit and tested.

### Safety Task: Define Task-Ingest Command Contract

Goal:

- Define mediated request/response ingestion for queue mutations so remote or
  worktree agents do not write SQLite directly.

Scope:

- Define command messages for claim, heartbeat, complete, fail, task
  creation/promotion, and any state-changing follow-up operations.
- Keep command ingest distinct from raw intake and event spool ingestion.
- Include response payloads, idempotency keys, actor identity, errors, and lease
  token handling.
- Decide how command files move through inbox, processing, done, error, and
  response paths.

Acceptance criteria:

- Agents can request state mutations and receive results without direct SQLite
  writes.
- Events, raw intake, task proposals, and queue mutation commands have separate
  schemas and responsibilities.
- The design works for local worktrees and the planned HPC pull-spool transport.

### 2. Add Config Validation And Schema Versioning

Goal:

- Fail early with actionable errors for malformed project configs and establish
  a compatibility path for future config and database changes.

Scope:

- Add `config_schema_version` support.
- Validate required and optional config fields in `agent_tracker.config`.
- Add a database schema metadata/version table in `Store.init_schema()`.
- Add migration-oriented tests without introducing a heavy migration framework.

Acceptance criteria:

- Missing or invalid config fields produce concise CLI errors.
- New databases record the current schema version.
- Tests cover valid config, malformed config, and schema metadata creation.

### 3. Implement Pull-Based Spool Support

Goal:

- Support a safe cross-directory spool bridge before adding network transports.

Scope:

- Add a `pull-spool` CLI command.
- Support local or shared-filesystem `remote_inbox` to `local_inbox` copies.
- Add `--dry-run` output that lists files and target paths without mutation.
- Skip partial files using a documented convention.
- Preserve remote evidence files by copying rather than deleting them.

Acceptance criteria:

- Dry-run is side-effect free.
- Repeated pulls are idempotent.
- Pulled files can then be processed by the existing `ingest-spool` flow.

### Safety Task: Test Remote Spooling For SSH/Codex Projects

Goal:

- Prove the spool path works for remote agents, including Codex app sessions
  connected to an SSH-backed project workspace.

Scope:

- Start with a local/shared-filesystem test harness that models remote outbox
  and canonical local inbox paths without requiring a real SSH service in CI.
- Add a documented manual test for a Codex app SSH project where a remote agent
  writes spool files and the canonical project pulls and ingests them.
- Cover partial-file publication, repeated pulls, done/error idempotency, and
  response/error visibility for remote agents.
- Keep this separate from raw intake and queue mutation semantics until
  `task-ingest` defines command request/response files.

Acceptance criteria:

- The test harness catches regressions in remote-style pull behavior before an
  actual SSH environment is needed.
- The manual runbook identifies the canonical project, remote project, outbox,
  local inbox, pull command, ingest command, and expected evidence.
- The task clearly states which parts are blocked on task-ingest command
  semantics versus already-covered event-spool behavior.

### 4. Add Queue Lanes And Conflict Risk Planning

Goal:

- Let agent-coordinators telegraph non-blocking plans and choose parallel-safe
  work across multiple objectives.

Scope:

- Define durable lane metadata for task groups such as coordination plumbing,
  planning/intake, human/review authority, exports/reporting, and feature work.
- Add a conflict-risk field or convention covering likely write-scope overlap,
  sequencing pressure, and whether a task can run beside other active work.
- Decide how `overview` should display lanes and plan notes without becoming
  noisy.
- Document how coordinators should add or update planning signals while the
  full intake/task-ingest path is still missing.

Acceptance criteria:

- The queue can show multiple objectives without implying a single linear path.
- A coordinator can identify at least one low-conflict feature task and one
  coordination-plumbing task from the overview/planning docs.
- The design avoids requiring agents to edit runtime tracker state through git.

Dependency note:

- Raw intake should not wait for follow-up proposal/export plumbing. It can
  capture untriaged ideas as non-claimable records first.
- Triage should wait for raw intake records and then promote selected items into
  proposed task contracts.
- Follow-up proposals remain a later automation path for deriving tasks from
  completed work or events.

### 5. Add A Repository Boundary For In-Memory Tests

Goal:

- Make service tests cheaper and less coupled to SQLite file setup by placing a
  repository boundary between coordinator logic and persistence.

Scope:

- Define the smallest protocol or interface that `Coordinator` needs from the
  current SQLite `Store`.
- Keep the SQLite implementation as the production repository.
- Add an in-memory repository option for focused unit tests where database
  behavior is not the subject under test.
- Preserve existing integration tests that exercise real SQLite migrations,
  constraints, and path handling.

Acceptance criteria:

- Service-level behavior can be tested without creating temporary SQLite files
  when persistence semantics are not relevant.
- SQLite-backed tests still cover schema, constraints, audit persistence, and
  import/claim/complete flows.
- The abstraction does not force plugin or CLI callers to know about test-only
  repository types.

### 6. Add An MCP Server Entrypoint

Goal:

- Package the existing MCP-friendly handlers as a real MCP server adapter.

Scope:

- Add a thin server entrypoint around `AgentTrackerTools`.
- Expose schemas for ready-task listing, claim, heartbeat, complete, fail,
  record evidence, record event, render prompt, and status.
- Keep all state transitions in `Coordinator` and `Store`.

Acceptance criteria:

- The MCP adapter can be tested without duplicating service logic.
- Tool names and payloads match the existing handler shapes where practical.

### 5. Add Approval Gates And Authority Metadata

Goal:

- Prevent automatic claims or completions for tasks that need human approval or
  stronger authority checks.

Scope:

- Define task metadata for allowed roles, write scopes, environments, and
  approval requirements.
- Add approval-related task states or metadata transitions.
- Audit approval and denial actions.

Acceptance criteria:

- Tasks requiring approval are not returned as automatically claimable.
- Approval decisions are visible in snapshots and audit logs.

### 6. Move Markdown Note/Result Export Into Plugins

Goal:

- Keep SQLite as live state while making git-backed summaries derived exports
  instead of manually edited coordination state.

Scope:

- Define exporter expectations for readable Markdown summaries.
- Keep project-specific HPC output in the project adapter, not core.
- Retain links to large external artifacts rather than copying them.

Acceptance criteria:

- Generic core remains project-agnostic.
- Markdown exports can be regenerated from canonical state.

### 7. Add Deterministic Follow-Up Task Proposals

Goal:

- Let project plugins propose follow-up tasks from completed work or events
  without giving callbacks direct authority to launch agents.

Scope:

- Wire the existing `FollowupPlanner` protocol into service and CLI commands.
- Persist proposed tasks separately from imported tasks until approved.
- Support a review mode before promotion into active work.

Acceptance criteria:

- Follow-up proposals are deterministic and auditable.
- Proposed tasks can be reviewed before becoming claimable.

### 8. Add A Cheap Attendant Command

Goal:

- Provide a scheduled command that handles routine coordination without keeping
  high-intelligence agents running as pollers.

Scope:

- Add a command that ingests spool files, recovers stale leases, exports
  snapshots, and proposes follow-ups.
- Keep it idempotent so it can run safely from cron or launchd.
- Keep canonical state connection policy and task-ingest/request-response
  semantics out of this task; those need their own design so the attendant stays
  a scheduled maintenance command rather than the general queue mutation API.

Acceptance criteria:

- Repeated attendant runs do not duplicate events or proposals.
- The command reports concise counts for each action.
- The command refuses ambiguous project state paths or reports resolved config,
  database, and spool paths before mutation.

## Additional Coordination Tasks

These tasks came from the coordination/intake research note in
`docs/research/2026-06-08-coordination-intake-dispatch.md`.

### 9. Vendor A Generic Project-Manager Skill

Goal:

- Provide a reusable Codex skill named `project-manager`, not
  `hpc-ci-project-manager`, that can be installed from this package.

Scope:

- Vendor the skill under the package source.
- Add a bootstrap command for copying the skill into a Codex skills directory.
- Include workflows for pulling the next task, repairing claim failures,
  capturing intake, logging work, and updating notebooks.
- Document how project-specific trackers can extend the generic skill.

Acceptance criteria:

- The packaged skill is named `project-manager`.
- A new environment can install or copy the vendored skill without manually
  finding the source tree.
- The skill does not hard-code HPC-specific assumptions.

### 10. Add Repo And Project Notebook Conventions

Goal:

- Give agents durable, curated project context without forcing every chat to
  rediscover operational issues and design conventions.

Scope:

- Define project-level and repo-level notebook locations.
- Define expected notebook content: operational conventions, architecture
  constraints, validation suites, known failure modes, sandbox/authority rules,
  and links to canonical config/state.
- Decide how prompt renderers should include or reference notebooks.
- Move or link stashed planning context into its long-term home.

Acceptance criteria:

- The research note in `docs/research/` has a deliberate destination.
- Notebook entries have provenance or review-date guidance so they do not drift
  silently.

### 11. Add An Intake Inbox

Goal:

- Capture raw user ideas, features, checks, and planning notes without making
  them immediately claimable.

Scope:

- Add intake records to storage and snapshots.
- Add CLI/service methods for recording intake.
- Include source, date, repo/project context, free-form text, and optional tags.
- Keep intake separate from active tasks and proposals.

Acceptance criteria:

- Intake records are visible to project-manager workflows.
- Intake records are never returned by ready-task listing or claim operations.

### 12. Add Project-Manager Triage

Goal:

- Let a project-manager agent organize intake, ask planning questions, and
  promote selected items into proposed task contracts.

Scope:

- Add deterministic triage commands.
- Generate proposed tasks with repo, role, authority, dependencies, validation
  checks, intervention needs, and notebook updates.
- Define the minimal proposed-task contract and persistence needed for
  human-reviewed intake promotion.
- Keep human approval between proposal and active queue state.

Acceptance criteria:

- Triage produces proposed tasks, not immediately claimable tasks.
- Later follow-up proposal automation can reuse the same proposed-task contract
  instead of creating a parallel model.
- The project-manager skill documents the triage workflow.

### 13. Add A Human Intervention Queue

Goal:

- Model "the user needs to intervene" as durable state before sending external
  notifications.

Scope:

- Define a small intervention reason set: approval required, failed verdict,
  ambiguous diagnosis, stale claim, missing evidence, unsafe operation, PR
  review needed, or setup missing.
- Add resolution evidence and audit records.
- Keep intervention state distinct from notification delivery.

Acceptance criteria:

- Interventions can be listed and resolved.
- Resolution requires evidence or a reason.

### 14. Add PR Notification Setup Checks

Goal:

- Determine whether associated repositories can support PR-based notification
  before relying on that path.

Scope:

- Check remote URLs, branch/PR association, available auth path, and fallback
  behavior.
- Account for agent sandboxes where `gh` auth may fail.
- Prefer diagnostics and prepared payloads before attempting live comments.

Acceptance criteria:

- Setup diagnostics distinguish missing remote, missing PR, missing auth, and
  unsupported sandbox.
- Notification exporters can refuse unsafe posting with actionable errors.

### 15. Add An Idempotent PR Notification Exporter

Goal:

- Notify the user about intervention states through PR comments or prepared PR
  notification payloads while keeping SQLite as canonical state.

Scope:

- Store notification target, comment ID if available, last payload hash, and
  last posted time.
- Prefer updating/suppressing duplicate notifications over adding repeated
  comments.
- Support dry-run or prepared-payload output when live GitHub auth is not
  available.

Acceptance criteria:

- Repeated exports do not spam PR comments.
- Notification state is auditable and reproducible.

### 16. Define An HPC Validation Request Contract

Goal:

- Let feature agents request remote/HPC validation without waiting on Slurm or
  owning the run lifecycle.

Scope:

- Define `validation_request` separately from callback events, suite verdicts,
  and Codex review reports.
- Include branch/commit, suite/case, repo, authority, expected evidence, and
  requested-by metadata.
- Keep callbacks as event producers only.

Acceptance criteria:

- Feature agents can hand off validation and stop.
- A later attendant or runner can process the request without reinterpreting the
  original feature chat.

### 17. Add A Codex SDK/App Server Worker Adapter

Goal:

- Submit queued tasks to Codex programmatically without manually creating chats.

Scope:

- Claim a task.
- Render its prompt.
- Start or resume a Codex SDK/App Server thread.
- Record thread ID, prompt path, final response/report path, and evidence.
- Keep SQLite as canonical task state.

Acceptance criteria:

- The worker is a separate execution adapter beside CLI/MCP.
- It does not expose local App Server control over unsafe network boundaries.

## Dogfooding Plan

Yes, this project can use `agent-tracker` to manage its own implementation.
The current package already supports enough of the workflow: a project config,
a JSON task plan, import, status, next-task listing, claims, heartbeats,
completion, evidence, and snapshot export.

Implemented self-tracking layout:

- `tracking/project.json`: project config for this repository.
- `tracking/tasks.json`: task plan generated from the priority tasks above.
- `tracking/exports/snapshot.json`: derived status export.
- `tracking/spool/inbox`, `tracking/spool/done`, and `tracking/spool/error`:
  local event spool for dogfooding ingestion.
- `tracking/README.md`: agent workflow for pulling work, repairing failed
  claims, logging progress, completing tasks, and creating follow-up tasks.

Suggested bootstrap commands:

```bash
agent-tracker init --config tracking/project.json
agent-tracker import --config tracking/project.json
agent-tracker status --config tracking/project.json
agent-tracker next --config tracking/project.json
agent-tracker claim --config tracking/project.json --agent codex --role maintainer
```

Dogfooding requirement:

- A user should be able to tell an agent to pull the next available task.
- The agent should import the task plan, list the next ready task, claim it,
  render the task prompt, and then start work.
- If the pull or claim fails, the agent should inspect status, dependencies,
  roles, stale leases, config, task-plan validity, and CLI/store behavior before
  reporting a blocker.
- Agents should log meaningful work as completion evidence or spool-ingested
  worklog events.
- New follow-up tasks should be added to `tracking/tasks.json` until the planned
  proposal workflow exists.

Dogfooding caveats:

- The self-tracking config should live in a normal committed directory rather
  than inside the package source.
- Live SQLite state should stay uncommitted; exported snapshots can be committed
  only when they are useful for review.
- Until follow-up proposals and approval gates exist, humans should still decide
  which task to claim next.
- A dedicated Codex skill may be useful later, but the first version should stay
  in repo-local documentation until the workflow stabilizes.
