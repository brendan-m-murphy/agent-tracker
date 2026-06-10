"""Install vendored Codex skills for agent-tracker projects."""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

DEFAULT_SKILL = "project-manager"


def available_skill_names() -> list[str]:
    """Return vendored skill names available for installation."""
    skills_root = resources.files("agent_tracker.vendor").joinpath("skills")
    return sorted(
        child.name
        for child in skills_root.iterdir()
        if child.is_dir() and child.joinpath("SKILL.md").is_file()
    )


def vendored_skill_path(name: str = DEFAULT_SKILL) -> Path:
    """Return the path to a vendored skill directory."""
    path = resources.files("agent_tracker.vendor").joinpath("skills").joinpath(name)
    if not path.is_dir():
        raise ValueError(f"unknown vendored skill: {name}")
    return Path(str(path))


def default_skill_root() -> Path:
    """Return the default Codex skills directory."""
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "skills"


def install_skill(
    *,
    name: str = DEFAULT_SKILL,
    destination_root: str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> Path:
    """Copy a vendored skill into a Codex skills directory."""
    source = vendored_skill_path(name)
    root = (
        Path(destination_root).expanduser()
        if destination_root is not None
        else default_skill_root()
    )
    destination = root / name
    if destination.exists():
        if not overwrite:
            raise FileExistsError(f"skill already exists: {destination}")
        if not dry_run:
            shutil.rmtree(destination)
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    return destination


def install_skills(
    *,
    names: Sequence[str] | None = None,
    all_skills: bool = False,
    destination_root: str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[Path]:
    """Copy selected vendored skills into a Codex skills directory."""
    selected_names = _selected_skill_names(names, all_skills=all_skills)
    return [
        install_skill(
            name=name,
            destination_root=destination_root,
            overwrite=overwrite,
            dry_run=dry_run,
        )
        for name in selected_names
    ]


def build_parser() -> argparse.ArgumentParser:
    """Build the skill bootstrap parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Vendored skill name to install. Pass multiple times to install a "
            f"subset. Defaults to {DEFAULT_SKILL!r} when no selection is given."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Install every vendored skill.",
    )
    parser.add_argument(
        "--destination-root",
        default="",
        help=(
            "Directory that contains Codex skill folders. Defaults to "
            "$CODEX_HOME/skills or ~/.codex/skills."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing skill folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the target path without copying.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Install vendored skills."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        selected_names = _selected_skill_names(args.name, all_skills=args.all)
    except ValueError as exc:
        parser.error(str(exc))
    destinations = [
        install_skill(
            name=name,
            destination_root=args.destination_root or None,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        for name in selected_names
    ]
    action = "Would install" if args.dry_run else "Installed"
    for name, destination in zip(selected_names, destinations):
        print(f"{action} {name} skill at {destination}")
    return 0


def _selected_skill_names(names: Sequence[str] | None, *, all_skills: bool) -> list[str]:
    """Return the skill names selected by CLI or API options."""
    requested_names = [name for name in names or [] if name]
    if all_skills:
        if requested_names:
            raise ValueError("--all cannot be combined with --name")
        return available_skill_names()
    if not requested_names:
        requested_names = [DEFAULT_SKILL]
    return _deduplicate(requested_names)


def _deduplicate(names: Sequence[str]) -> list[str]:
    """Return names in first-seen order without duplicates."""
    selected: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            selected.append(name)
            seen.add(name)
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
