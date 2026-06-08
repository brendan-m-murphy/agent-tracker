"""Built-in generic task importers."""

from __future__ import annotations

import json
from typing import Any

from agent_tracker.config import ProjectConfig
from agent_tracker.models import DependencyRecord, TaskRecord


class JsonTaskImporter:
    """Import tasks from a generic JSON task plan."""

    def load_tasks(self, config: ProjectConfig) -> tuple[list[TaskRecord], list[DependencyRecord]]:
        """Load generic task records from `task_plan_path`."""
        path = config.resolve_task_source_path("task_plan_path")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        tasks: list[TaskRecord] = []
        dependencies: list[DependencyRecord] = []
        for raw_task in data.get("tasks", []):
            task_id = str(raw_task["id"])
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    title=str(raw_task.get("title", task_id)),
                    repo=str(raw_task.get("repo", "")),
                    status=str(raw_task.get("status", "pending")),
                    priority=int(raw_task.get("priority", 9999)),
                    prompt_key=str(raw_task.get("prompt_key", "")),
                    prompt_path=str(raw_task.get("prompt_path", "")),
                    summary=str(raw_task.get("summary", "")),
                    execution=dict(raw_task.get("execution", {})),
                    validation_checks=[str(item) for item in raw_task.get("validation_checks", [])],
                    next_action=str(raw_task.get("next_action", "")),
                    evidence=[str(item) for item in raw_task.get("evidence", [])],
                    metadata=dict(raw_task.get("metadata", {})),
                )
            )
            for requirement in raw_task.get("requirements", []):
                if requirement.get("kind") == "task":
                    dependencies.append(
                        DependencyRecord(
                            task_id=task_id,
                            dependency_task_id=str(requirement.get("task", "")),
                            description=str(requirement.get("description", "")),
                        )
                    )
        return tasks, dependencies
