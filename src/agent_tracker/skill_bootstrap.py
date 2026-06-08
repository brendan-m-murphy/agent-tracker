"""Install vendored Codex skills for agent-tracker projects."""

from __future__ import annotations

import argparse
import os
import shutil
from importlib import resources
from pathlib import Path

DEFAULT_SKILL = "project-manager"


def vendored_skill_path(name: str = DEFAULT_SKILL) -> Path:
    """Return the path to a vendored skill directory."""
    path = resources.files("agent_tracker.vendor").joinpath("skills", name)
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


def build_parser() -> argparse.ArgumentParser:
    """Build the skill bootstrap parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_SKILL, help="Vendored skill name to install.")
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
    """Install a vendored skill."""
    args = build_parser().parse_args(argv)
    destination = install_skill(
        name=args.name,
        destination_root=args.destination_root or None,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    action = "Would install" if args.dry_run else "Installed"
    print(f"{action} {args.name} skill at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
