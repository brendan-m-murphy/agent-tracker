# Agent Tracker Repo Notebook

- Last reviewed: 2026-06-10
- Owner: maintainer
- Sources:
  - tracking/README.md
  - docs/operations.md
  - docs/task-plans.md
  - docs/notebooks.md
  - docs/research/2026-06-10-local-ssh-server-harness.md

## Repo Workflow

Work from `/Users/bm13805/Documents/agent-tracker` when mutating tracker state.
The self-dogfood config declares canonical state paths, so mutating tracker
commands from copied worktrees are refused.

For code, docs, config, tests, or task-plan changes:

- claim the task and heartbeat with the live lease token;
- create a task branch from `main`;
- run focused checks before broad validation;
- commit scoped changes;
- complete with commit evidence plus review, PR, integration, or explicit
  direct-merge evidence.

## Validation

Standard checks for implementation tasks:

- `uv run pytest`
- `uv run ruff check .`

Common focused checks:

- `uv run pytest tests/test_prompt_path_rendering.py`
- `uv run pytest tests/test_agent_tracker.py -k init_project`
- `uv run --extra ssh pytest tests/test_agent_tracker.py -k "sftp or pull_spool"`
- `uv run ty check src/agent_tracker/cli.py`
- `uv run --group docs sphinx-build -b html docs /tmp/agent-tracker-docs-build`

The sandbox may require escalation for `uv` cache access and Git metadata
writes. Record that as friction when it blocks normal coordinator validation.

## Prompt Context

The default renderer reads `prompt_path` and opt-in
`metadata.notebook_paths` as config-directory-relative UTF-8 files. It refuses
absolute paths, home-relative paths, and parent traversal outside the config
directory. For this repo, use `tracking/notebooks/` as the prompt-includable
notebook root and `docs/research/` as raw source material.

The CLI now exposes `agent-tracker notebook list/show/append` for project and
repo notebooks. Proposed-task commands accept repeatable `--notebook-path`
values, validate that they stay under `notebooks/`, and store them in
`metadata.notebook_paths` without raw SQLite or task-plan edits.

## Known Failure Modes

- `prompt_path` values like `docs/research/...` do not render in this
  self-dogfood project because the config root is `tracking/`.
- Direct SQLite or task-plan edits are friction unless a task explicitly asks
  for source alignment or no command exists.
- Local validation evidence alone is not sufficient closeout for tasks that
  changed tracked files.

## SSH Spool Testing

The optional `ssh` extra installs AsyncSSH for SSH/SFTP spool transport and
loopback tests. Keep AsyncSSH optional unless the dependency and license are
accepted for default installs. Use `known_hosts: "none"` only for loopback tests
or deliberately isolated environments; real hosts should use a known_hosts file.

Event-spool SSH coverage is separate from queue-mutation command-spool coverage.
Remote queue mutation still belongs to the task-ingest command contract.
