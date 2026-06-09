"""Command-line interface for agent-tracker."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from agent_tracker.config import load_config
from agent_tracker.db import intake_to_dict, proposed_task_to_dict, state_to_dict
from agent_tracker.models import INTEGRATION_STATES
from agent_tracker.service import Coordinator

OVERVIEW_GROUPS = (
    ("ready", "Ready"),
    ("active", "Active"),
    ("review", "Review"),
    ("integration", "Integration"),
    ("blocked", "Blocked"),
    ("recently_completed", "Recently completed"),
)


def coordinator(args: argparse.Namespace) -> Coordinator:
    """Build a coordinator from CLI args."""
    config = load_config(args.config)
    db_path = Path(args.db).expanduser() if getattr(args, "db", "") else None
    return Coordinator(config, db_path=db_path)


def print_json(payload: Any) -> None:
    """Print JSON."""
    print(json.dumps(payload, indent=2))


def print_path_report(coord: Coordinator) -> None:
    """Report resolved paths before a mutating command."""
    paths = coord.path_summary()
    print("agent-tracker paths:", file=sys.stderr)
    for key in (
        "config_path",
        "canonical_config_path",
        "state_root",
        "task_source_root",
        "task_source_path",
        "db_path",
    ):
        if key in paths:
            print(f"  {key}: {paths[key]}", file=sys.stderr)


def command_init(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.init()
    print(f"Initialized {coord.config.project_id} at {coord.store.path}")
    return 0


def command_import(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    count = coord.import_tasks(reconcile_runtime_state=args.reconcile_runtime_state)
    policy = (
        "with runtime-state reconciliation" if args.reconcile_runtime_state else "definitions only"
    )
    print(f"Imported {count} task definitions for {coord.config.project_id} ({policy})")
    return 0


def command_status(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    payload = coord.status_payload(recover_stale_leases=args.recover_stale_leases)
    if args.json:
        print_json(payload)
        return 0
    print(f"{payload['name']} ({payload['project_id']})")
    print(f"  config: {payload['config_path']}")
    print(f"  db: {payload['db_path']}")
    if payload.get("task_source_path"):
        print(f"  task source: {payload['task_source_path']}")
    print(f"  ready: {len(payload['ready'])}")
    print(f"  active: {len(payload['active'])}")
    print(f"  review: {len(payload['review'])}")
    print(f"  integration: {len(payload['integration'])}")
    print(f"  blocked: {len(payload['blocked'])}")
    return 0


def command_overview(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    payload = coord.overview_payload(
        recover_stale_leases=args.recover_stale_leases,
        limit=args.limit,
    )
    if args.json:
        print_json(payload)
        return 0
    print_overview(payload)
    return 0


def print_overview(payload: dict[str, Any]) -> None:
    """Print a grouped project overview."""
    print(f"{payload['name']} ({payload['project_id']})")
    for key, heading in OVERVIEW_GROUPS:
        items = payload["groups"][key]
        total = payload["counts"][key]
        print(f"{heading} ({total})")
        if not items:
            print("  (none)")
            continue
        for item in items:
            print_overview_item(key, item)
        if len(items) < total:
            print(f"  ... {total - len(items)} more")


def print_overview_item(group: str, item: dict[str, Any]) -> None:
    """Print one overview item."""
    qualifiers = []
    if group in {"active", "review", "integration"}:
        qualifiers.append(str(item["state"]))
    if item.get("lease_agent_id"):
        qualifiers.append(f"agent {item['lease_agent_id']}")
    qualifier = f" [{'; '.join(qualifiers)}]" if qualifiers else ""
    print(f"  - {item['id']}: {item['title']}{qualifier}")

    for blocker in item.get("blockers", [])[:2]:
        print(f"    blocker: {blocker}")
    if len(item.get("blockers", [])) > 2:
        print(f"    blocker: +{len(item['blockers']) - 2} more")
    if item.get("next_action"):
        print(f"    next: {item['next_action']}")
    if item.get("latest_evidence"):
        print(f"    evidence: {item['latest_evidence']}")
    if group == "recently_completed" and item.get("completed_at"):
        print(f"    completed: {item['completed_at']} by {item.get('completed_by') or 'system'}")


def command_next(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    ready = coord.ready_tasks(
        limit=args.limit,
        repo=args.repo,
        role=args.role,
        recover_stale_leases=args.recover_stale_leases,
    )
    if args.json:
        print_json([state_to_dict(state) for state in ready])
        return 0
    if not ready:
        print("No ready tasks.")
        return 0
    for state in ready:
        task = state.task
        print(f"{task.task_id}: {task.title}")
        if task.repo:
            print(f"  repo: {task.repo}")
        if task.next_action:
            print(f"  next: {task.next_action}")
    return 0


def command_task(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    state = coord.get_task(args.task_id, recover_stale_leases=args.recover_stale_leases)
    if args.json:
        print_json(state_to_dict(state))
        return 0
    print(
        coord.render_prompt(
            args.task_id,
            markdown=args.markdown,
            recover_stale_leases=args.recover_stale_leases,
        ),
        end="",
    )
    return 0


def command_claim(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    claim = coord.claim(
        agent_id=args.agent,
        task_id=args.task_id,
        repo=args.repo,
        role=args.role,
        lease_seconds=args.lease_seconds,
    )
    print_json(claim.__dict__)
    return 0


def command_heartbeat(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    claim = coord.heartbeat(
        args.task_id,
        lease_token=args.lease_token,
        lease_seconds=args.lease_seconds,
        agent_id=args.agent,
    )
    print_json(claim.__dict__)
    return 0


def command_complete(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.complete(
        args.task_id,
        lease_token=args.lease_token,
        evidence=args.evidence,
        agent_id=args.agent,
        direct_merge=args.direct_merge,
    )
    print(f"Completed {args.task_id}")
    return 0


def command_submit_review(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.submit_review(
        args.task_id,
        lease_token=args.lease_token,
        evidence=args.evidence,
        agent_id=args.agent,
    )
    print(f"Submitted {args.task_id} for review")
    return 0


def command_await_integration(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.await_integration(
        args.task_id,
        lease_token=args.lease_token,
        status=args.status,
        evidence=args.evidence,
        agent_id=args.agent,
    )
    print(f"Set {args.task_id} to {args.status}")
    return 0


def command_resolve_review(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.resolve_review(
        args.task_id,
        status=args.status,
        evidence=args.evidence,
        agent_id=args.agent,
        reason=args.reason,
        direct_merge=args.direct_merge,
    )
    print(f"Resolved review for {args.task_id} as {args.status}")
    return 0


def command_resolve_integration(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.resolve_integration(
        args.task_id,
        status=args.status,
        evidence=args.evidence,
        agent_id=args.agent,
        reason=args.reason,
        direct_merge=args.direct_merge,
    )
    print(f"Resolved integration for {args.task_id} as {args.status}")
    return 0


def command_fail(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    coord.fail(args.task_id, lease_token=args.lease_token, reason=args.reason, agent_id=args.agent)
    print(f"Failed {args.task_id}")
    return 0


def command_ingest_event(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    inserted = coord.ingest_event_file(args.event_json, actor=args.actor)
    print("inserted" if inserted else "duplicate")
    return 0


def command_ingest_spool(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    print_json(coord.ingest_spool(actor=args.actor))
    return 0


def command_pull_spool(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    print_json(coord.pull_spool(dry_run=args.dry_run))
    return 0


def command_record_intake(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    metadata = parse_json_object(args.metadata_json, "metadata-json")
    intake = coord.record_intake(
        args.text,
        kind=args.kind,
        source=args.source,
        repo=args.repo,
        tags=args.tag,
        metadata=metadata,
        intake_id=args.id,
        actor=args.actor,
    )
    print_json(intake_to_dict(intake))
    return 0


def command_list_intake(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    payload = coord.intake_payload(
        status=args.status,
        kind=args.kind,
        repo=args.repo,
        limit=args.limit,
    )
    if args.json:
        print_json(payload)
        return 0
    if not payload["intake"]:
        print("No intake records.")
        return 0
    for item in payload["intake"]:
        qualifiers = [item["kind"]]
        if item["repo"]:
            qualifiers.append(f"repo {item['repo']}")
        if item["source"]:
            qualifiers.append(f"source {item['source']}")
        print(f"{item['id']}: {item['text']}")
        print(f"  {'; '.join(qualifiers)}")
        if item["tags"]:
            print(f"  tags: {', '.join(item['tags'])}")
    return 0


def command_propose_task(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    metadata = parse_json_object(args.metadata_json, "metadata-json")
    proposal = coord.propose_task_from_intake(
        args.intake_id,
        task_id=args.task_id,
        title=args.title,
        repo=args.repo,
        summary=args.summary,
        next_action=args.next_action,
        role=args.role,
        write_scopes=args.write_scope,
        validation_checks=args.validation_check,
        requirements=[parse_dependency(value) for value in args.dependency],
        authority=args.authority,
        intervention_needs=args.intervention_need,
        notebook_updates=args.notebook_update,
        metadata=metadata,
        proposal_id=args.proposal_id,
        actor=args.actor,
    )
    print_json(proposed_task_to_dict(proposal))
    return 0


def command_list_proposals(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    payload = coord.proposed_tasks_payload(
        status=args.status,
        intake_id=args.intake_id,
        limit=args.limit,
    )
    if args.json:
        print_json(payload)
        return 0
    if not payload["proposals"]:
        print("No proposed tasks.")
        return 0
    for proposal in payload["proposals"]:
        task = proposal["task"]
        print(f"{proposal['id']}: {task['id']} - {task['title']}")
        print(f"  intake: {proposal['intake_id']}; status: {proposal['status']}")
        if task.get("repo"):
            print(f"  repo: {task['repo']}")
        if task.get("next_action"):
            print(f"  next: {task['next_action']}")
    return 0


def command_export(args: argparse.Namespace) -> int:
    coord = coordinator(args)
    print_path_report(coord)
    for path in coord.export():
        print(path)
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    """Add common project args."""
    parser.add_argument("--config", required=True, help="Project config JSON path.")
    parser.add_argument("--db", default="", help="Override SQLite DB path.")


def parse_json_object(value: str, label: str) -> dict[str, Any]:
    """Parse a JSON object CLI argument."""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def parse_dependency(value: str) -> dict[str, str]:
    """Parse a proposed task dependency argument."""
    task_id, separator, description = value.partition(":")
    task_id = task_id.strip()
    if not task_id:
        raise ValueError("dependency must start with a task id")
    return {
        "kind": "task",
        "task": task_id,
        "description": description.strip() if separator else "",
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize project database.")
    add_common(init)
    init.set_defaults(func=command_init)

    import_cmd = sub.add_parser("import", help="Import configured tasks.")
    add_common(import_cmd)
    import_cmd.add_argument(
        "--reconcile-runtime-state",
        action="store_true",
        help=(
            "Apply imported statuses and removals to runtime state. Without this, "
            "import updates definitions and dependencies while preserving existing "
            "runtime task status."
        ),
    )
    import_cmd.set_defaults(func=command_import)

    status = sub.add_parser("status", help="Show project status.")
    add_common(status)
    status.add_argument("--json", action="store_true")
    status.add_argument("--recover-stale-leases", action="store_true")
    status.set_defaults(func=command_status)

    overview = sub.add_parser("overview", help="Show grouped project overview.")
    add_common(overview)
    overview.add_argument("--json", action="store_true")
    overview.add_argument("--recover-stale-leases", action="store_true")
    overview.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum items to show per group; use 0 for all.",
    )
    overview.set_defaults(func=command_overview)

    next_cmd = sub.add_parser("next", help="Show ready tasks.")
    add_common(next_cmd)
    next_cmd.add_argument("--json", action="store_true")
    next_cmd.add_argument("--limit", type=int, default=3)
    next_cmd.add_argument("--repo", default="")
    next_cmd.add_argument("--role", default="")
    next_cmd.add_argument("--recover-stale-leases", action="store_true")
    next_cmd.set_defaults(func=command_next)

    task = sub.add_parser("task", help="Show one task prompt/context.")
    add_common(task)
    task.add_argument("task_id")
    task.add_argument("--json", action="store_true")
    task.add_argument("--markdown", action="store_true")
    task.add_argument("--recover-stale-leases", action="store_true")
    task.set_defaults(func=command_task)

    claim = sub.add_parser("claim", help="Claim a ready task.")
    add_common(claim)
    claim.add_argument("--agent", required=True)
    claim.add_argument("--task-id", default="")
    claim.add_argument("--repo", default="")
    claim.add_argument("--role", default="")
    claim.add_argument("--lease-seconds", type=int, default=3600)
    claim.set_defaults(func=command_claim)

    heartbeat = sub.add_parser("heartbeat", help="Extend a task lease.")
    add_common(heartbeat)
    heartbeat.add_argument("task_id")
    heartbeat.add_argument("--lease-token", required=True)
    heartbeat.add_argument("--agent", default="")
    heartbeat.add_argument("--lease-seconds", type=int, default=3600)
    heartbeat.set_defaults(func=command_heartbeat)

    complete = sub.add_parser("complete", help="Complete a task.")
    add_common(complete)
    complete.add_argument("task_id")
    complete.add_argument("--lease-token", required=True)
    complete.add_argument("--agent", default="")
    complete.add_argument("--evidence", action="append", default=[])
    complete.add_argument(
        "--direct-merge",
        action="store_true",
        help="Apply an explicit direct-merge completion override when task metadata allows it.",
    )
    complete.set_defaults(func=command_complete)

    submit_review = sub.add_parser("submit-review", help="Submit a leased task for review.")
    add_common(submit_review)
    submit_review.add_argument("task_id")
    submit_review.add_argument("--lease-token", required=True)
    submit_review.add_argument("--agent", default="")
    submit_review.add_argument("--evidence", action="append", default=[])
    submit_review.set_defaults(func=command_submit_review)

    await_integration = sub.add_parser(
        "await-integration",
        help="Move a leased task to an integration wait state.",
    )
    add_common(await_integration)
    await_integration.add_argument("task_id")
    await_integration.add_argument("--lease-token", required=True)
    await_integration.add_argument("--agent", default="")
    await_integration.add_argument(
        "--status",
        choices=sorted(INTEGRATION_STATES),
        default="awaiting_integration",
    )
    await_integration.add_argument("--evidence", action="append", default=[])
    await_integration.set_defaults(func=command_await_integration)

    resolve_review = sub.add_parser(
        "resolve-review",
        help="Resolve a task waiting for review.",
    )
    add_common(resolve_review)
    resolve_review.add_argument("task_id")
    resolve_review.add_argument("--agent", required=True)
    resolve_review.add_argument("--status", choices=["done", "failed"], default="done")
    resolve_review.add_argument("--reason", default="")
    resolve_review.add_argument("--evidence", action="append", default=[])
    resolve_review.add_argument(
        "--direct-merge",
        action="store_true",
        help="Apply an explicit direct-merge completion override when task metadata allows it.",
    )
    resolve_review.set_defaults(func=command_resolve_review)

    resolve_integration = sub.add_parser(
        "resolve-integration",
        help="Resolve a task waiting for integration.",
    )
    add_common(resolve_integration)
    resolve_integration.add_argument("task_id")
    resolve_integration.add_argument("--agent", required=True)
    resolve_integration.add_argument("--status", choices=["done", "failed"], default="done")
    resolve_integration.add_argument("--reason", default="")
    resolve_integration.add_argument("--evidence", action="append", default=[])
    resolve_integration.add_argument(
        "--direct-merge",
        action="store_true",
        help="Apply an explicit direct-merge completion override when task metadata allows it.",
    )
    resolve_integration.set_defaults(func=command_resolve_integration)

    fail = sub.add_parser("fail", help="Fail a task.")
    add_common(fail)
    fail.add_argument("task_id")
    fail.add_argument("--lease-token", required=True)
    fail.add_argument("--agent", default="")
    fail.add_argument("--reason", required=True)
    fail.set_defaults(func=command_fail)

    event = sub.add_parser("ingest-event", help="Ingest one event JSON file.")
    add_common(event)
    event.add_argument("event_json")
    event.add_argument("--actor", default="system")
    event.set_defaults(func=command_ingest_event)

    spool = sub.add_parser("ingest-spool", help="Ingest configured spool JSON files.")
    add_common(spool)
    spool.add_argument("--actor", default="system")
    spool.set_defaults(func=command_ingest_spool)

    pull_spool = sub.add_parser(
        "pull-spool",
        help="Copy complete remote spool JSON files into the configured local inbox.",
    )
    add_common(pull_spool)
    pull_spool.add_argument("--dry-run", action="store_true")
    pull_spool.set_defaults(func=command_pull_spool)

    record_intake = sub.add_parser(
        "record-intake",
        help="Record raw project intake without creating a task.",
    )
    add_common(record_intake)
    record_intake.add_argument("text")
    record_intake.add_argument("--id", default="")
    record_intake.add_argument("--kind", default="idea")
    record_intake.add_argument("--source", default="")
    record_intake.add_argument("--repo", default="")
    record_intake.add_argument("--tag", action="append", default=[])
    record_intake.add_argument("--metadata-json", default="{}")
    record_intake.add_argument("--actor", default="system")
    record_intake.set_defaults(func=command_record_intake)

    list_intake = sub.add_parser("list-intake", help="List raw project intake.")
    add_common(list_intake)
    list_intake.add_argument("--json", action="store_true")
    list_intake.add_argument("--limit", type=int, default=0)
    list_intake.add_argument("--status", default="")
    list_intake.add_argument("--kind", default="")
    list_intake.add_argument("--repo", default="")
    list_intake.set_defaults(func=command_list_intake)

    propose_task = sub.add_parser(
        "propose-task",
        help="Create a proposed task contract from an intake record.",
    )
    add_common(propose_task)
    propose_task.add_argument("intake_id")
    propose_task.add_argument("--proposal-id", default="")
    propose_task.add_argument("--task-id", required=True)
    propose_task.add_argument("--title", required=True)
    propose_task.add_argument("--repo", default="")
    propose_task.add_argument("--summary", default="")
    propose_task.add_argument("--next-action", default="")
    propose_task.add_argument("--role", default="")
    propose_task.add_argument("--write-scope", action="append", default=[])
    propose_task.add_argument("--validation-check", action="append", default=[])
    propose_task.add_argument("--dependency", action="append", default=[])
    propose_task.add_argument("--authority", default="")
    propose_task.add_argument("--intervention-need", action="append", default=[])
    propose_task.add_argument("--notebook-update", action="append", default=[])
    propose_task.add_argument("--metadata-json", default="{}")
    propose_task.add_argument("--actor", default="system")
    propose_task.set_defaults(func=command_propose_task)

    list_proposals = sub.add_parser("list-proposals", help="List proposed task contracts.")
    add_common(list_proposals)
    list_proposals.add_argument("--json", action="store_true")
    list_proposals.add_argument("--limit", type=int, default=0)
    list_proposals.add_argument("--status", default="")
    list_proposals.add_argument("--intake-id", default="")
    list_proposals.set_defaults(func=command_list_proposals)

    export = sub.add_parser("export", help="Export project audit snapshot.")
    add_common(export)
    export.set_defaults(func=command_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        FileNotFoundError,
        ImportError,
        json.JSONDecodeError,
        KeyError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
