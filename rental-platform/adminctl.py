#!/usr/bin/env python3
"""First-run administrator bootstrap for the rental control plane.

Passwords are read from a hidden prompt and are never accepted as command-line
arguments or written to the systemd environment file.
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from core import Core


def default_state_root() -> str:
    """Resolve the same SQLite directory that server.py uses."""
    explicit = os.environ.get("RENTAL_STATE_ROOT")
    if explicit:
        return explicit
    data_root = os.environ.get("RENTAL_ROOT")
    if data_root:
        return str(Path(data_root) / "state")
    return "/var/lib/1cat-rental/state"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1Cat rental administrator setup")
    parser.add_argument("--root", default=default_state_root())
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="create a dedicated administrator account")
    create.add_argument("--name", required=True)
    price = sub.add_parser("set-price", help="set the CNY price per running GPU hour")
    price.add_argument("--admin", required=True)
    price.add_argument("--cny-per-hour", required=True, type=float)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    core = Core(Path(args.root))
    if args.command == "create-admin":
        try:
            if core.is_admin(args.name):
                raise SystemExit("administrator already exists; use its password or choose another name")
        except PermissionError:
            pass
        first = getpass.getpass("New administrator password: ")
        second = getpass.getpass("Repeat administrator password: ")
        if first != second:
            raise SystemExit("passwords do not match")
        core.ensure_admin(args.name, first)
        print(f"administrator created: {args.name}")
        return 0
    password = getpass.getpass("Administrator password: ")
    core.login(args.admin, password)
    cents = round(args.cny_per_hour * 100)
    core.set_price(args.admin, cents)
    print(f"hourly price set: {cents} cents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
