"""Command-line interface for agent-tracker."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from agent_tracker.config import load_config
from agent_tracker.db import state_to_dict
from agent_tracker.service import Coordinator


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
    print(f"  blocked: {len(payload['blocked'])}")
    return 0


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
    )
    print(f"Completed {args.task_id}")
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
    complete.set_defaults(func=command_complete)

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
