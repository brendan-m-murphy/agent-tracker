# Coordination Intake And Dispatch Context

This note preserves planning context that should be organized into durable
project or repo notebooks when the notebook feature is designed.

## User Needs

- Capture rough "ideas", "features", "checks", concerns, and planning notes
  without immediately converting them into claimable implementation tasks.
- Let a project-manager agent review that intake later, ask follow-up planning
  questions, and promote selected items into task contracts.
- Notify the user when intervention is needed. The simplest initial external
  notification surface is a PR or PR comment in a remote GitHub repository.
- Check whether associated repositories have enough GitHub setup for PR-based
  notification before relying on that path.
- Provide project-level and repo-level notebooks where agents can record
  operational issues, design conventions, architecture constraints, known
  failure modes, and validation rules.
- Avoid manually repeating the prompt to read tracker docs and pull the next
  available task. A Codex skill should provide that workflow.
- Eventually submit tasks to Codex without manually creating chats.

## Architecture Notes

- Keep SQLite/task state as canonical. GitHub PRs, issue comments, Markdown
  exports, and mailbox messages are notification or review surfaces.
- Treat Codex App Server/SDK as an execution adapter: a worker may claim a task,
  render a prompt, start or resume a Codex thread, and record thread/report
  metadata, but it should not own coordination state.
- Do not expose local Codex/App Server control over network or HPC boundaries
  without a security model.
- Callbacks should enqueue events. A controlled attendant or worker should decide
  whether to invoke Codex.
- High-value agents should not wait on long-running Slurm jobs, notebook cells,
  slow tests, or subagents. Cheap schedulers, callbacks, daemons, or sentinels
  should wait; agents should wake only when evidence exists.

## Feature Candidates

- Intake inbox for raw user ideas/features/checks.
- Project-manager triage workflow that promotes intake into proposed tasks.
- Intervention queue with a small set of reasons and explicit resolution
  evidence.
- PR notification setup check and idempotent PR notifier/exporter.
- Project/repo notebook conventions and prompt inclusion rules.
- Vendored `project-manager` skill that can be installed from this package.
- Codex App Server/SDK worker adapter for task dispatch without manual chats.
- HPC validation request contract that stays separate from callback events,
  suite verdicts, and Codex review reports.
