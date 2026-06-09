# Project And Repo Notebooks

Notebooks are durable, curated context for agents. Use them for information that
future agents should not have to rediscover from chat history, raw logs, or
closed task threads.

## Layout

Keep notebooks under the tracker task source root so they can be included in
rendered prompts with config-relative paths:

```text
tracking/
  project.json
  tasks.json
  notebooks/
    project.md
    repos/
      agent-tracker.md
```

Use these conventions:

- `notebooks/project.md`: project-wide coordination context, queue policy,
  canonical config/state paths, authority rules, and cross-repository decisions.
- `notebooks/repos/<repo-id>.md`: repository-specific architecture constraints,
  validation suites, known failure modes, sandbox notes, and local workflows.
- `docs/research/`: raw or exploratory notes that may feed notebooks but are
  not automatically included in task prompts.

For a standard `tracking/project.json`, prompt paths are relative to
`tracking/`. A task can point directly at a notebook:

```json
{
  "prompt_path": "notebooks/project.md"
}
```

For multiple notebooks, use opt-in task metadata:

```json
{
  "metadata": {
    "notebook_paths": [
      "notebooks/project.md",
      "notebooks/repos/agent-tracker.md"
    ]
  }
}
```

The default prompt renderer includes `prompt_path` first and then
`metadata.notebook_paths`. It uses the same safety rules for both: only readable
UTF-8 regular files below the config directory are included. Missing, absolute,
home-relative, directory, non-UTF-8, unreadable, or parent-traversal paths render
a stable note instead of raising.

Project-specific renderers can add richer selection rules, but they should keep
prompt context deterministic and bounded.

## Content

Good notebook entries are short, reviewed, and actionable. Prefer:

- operational conventions and queue closeout policy;
- architecture constraints and module boundaries;
- validation suites and expected runtime or sandbox requirements;
- known failure modes and recovery commands;
- authority, approval, and human-intervention rules;
- links to canonical config, state, task plans, research notes, and reports.

Do not paste large raw logs or entire chat exports into notebooks. Summarize the
decision or convention and link to the source artifact when it is useful.

## Provenance

Each notebook should include a small header:

```markdown
# Notebook Title

- Last reviewed: 2026-06-09
- Owner: project-manager
- Sources:
  - docs/research/example.md
```

Use review dates so context does not drift silently. When a task changes a
notebook, record that in the task contract with `metadata.notebook_updates` or
the proposed-task `--notebook-update` option.
