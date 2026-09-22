"""Operator commands, run on the server itself — never reachable through the public API.

Resetting everyone's demo data used to be one click on the public page, available to every
visitor because every visitor was signed in as the administrator. It now belongs to whoever can
open a shell on the server. From the repository root (or `/app` in the container):

    PYTHONPATH=apps/api python -m app.ops check-readiness
    PYTHONPATH=apps/api python -m app.ops reset-demo-data
    PYTHONPATH=apps/api python -m app.ops move-product-version --tenant <tenant> --product <product> --version <n>
    PYTHONPATH=apps/api python -m app.ops shadow-report [--days N]
    PYTHONPATH=apps/api python -m app.ops execution-preflight

`check-readiness` exits non-zero when any active product cannot start a conversation, and prints
which ones and why — the detail the public health endpoint deliberately withholds.

`move-product-version` moves one product binding to another published definition version. Open
sessions keep the version they were pinned to; only new sessions use the new one. Running it again
with the previous version moves back.

`shadow-report` prints the shadow engine's parity counts (5a) for the last N days: per product,
definition version, response field and class. Counts only; nothing a visitor said is stored.

`execution-preflight` reports execution keys by state and exits non-zero if a key issued before
5b (with no workspace) is still dispatched. Run it before deploying 5b; the application runs the
same check at startup and refuses to serve if it fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from app.db import migrate
from app.definitions.bootstrap import publish_lineage
from app.definitions.integrity import session_start_problems
from app.definitions.loader import DefinitionError
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import RegistryError
from app.engine.execution import ExecutionLedger
from app.services import shadow_parity
from app.services.product_data_store import ProductDataStore


def check_readiness() -> int:
    problems = session_start_problems()
    print(json.dumps({"ready": not problems, "problems": [asdict(problem) for problem in problems]},
                     indent=2))
    return 1 if problems else 0


def reset_demo_data() -> int:
    data = ProductDataStore().reset()
    print(json.dumps({"reset": True, **{name: len(rows) for name, rows in data.items()}}, indent=2))
    return 0


def move_product_version(tenant: str, product: str, version: int) -> int:
    """Publish `version` (and any never-registered earlier one), move the binding, prove readiness.

    Nothing moves unless the version publishes: a missing file, a failed validation or a breaking
    entity change is refused with the binding untouched. If readiness fails after the move, the
    binding is moved back, so the command never leaves a product unable to start conversations.
    """
    directory = OrganizationDirectory()
    binding = directory.product(tenant, product)
    if binding is None:
        return _refused(f"{tenant} has no product {product}")
    previous = binding.definition_version
    try:
        publish_lineage(directory.definitions, binding.definition_id, version)
        moved = directory.move_product_version(tenant, product, version)
    except (DefinitionError, RegistryError, OSError) as error:
        return _refused(str(error))
    problems = session_start_problems()
    if problems:
        directory.move_product_version(tenant, product, previous)
    print(json.dumps({
        "moved": not problems,
        "tenant": tenant,
        "product": product,
        "definition_id": moved.definition_id,
        "from_version": previous,
        "to_version": version if not problems else previous,
        "definition_checksum": moved.definition_checksum if not problems else binding.definition_checksum,
        "problems": [asdict(problem) for problem in problems],
    }, indent=2))
    return 1 if problems else 0


def shadow_report(days: int) -> int:
    print(json.dumps({"days": days, "rows": shadow_parity.report(days)}, indent=2))
    return 0


def execution_preflight() -> int:
    report = ExecutionLedger.preflight()
    print(json.dumps({"ready": not report["legacy_dispatched"], **report}, indent=2))
    return 1 if report["legacy_dispatched"] else 0


def _refused(reason: str) -> int:
    print(json.dumps({"moved": False, "reason": reason}, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ops", description="Pixel operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-readiness")
    commands.add_parser("reset-demo-data")
    move = commands.add_parser("move-product-version")
    move.add_argument("--tenant", required=True)
    move.add_argument("--product", required=True)
    move.add_argument("--version", required=True, type=int)
    commands.add_parser("execution-preflight")
    report = commands.add_parser("shadow-report")
    report.add_argument("--days", type=int, default=7)
    arguments = parser.parse_args(argv)
    migrate()
    if arguments.command == "move-product-version":
        return move_product_version(arguments.tenant, arguments.product, arguments.version)
    if arguments.command == "shadow-report":
        return shadow_report(arguments.days)
    if arguments.command == "execution-preflight":
        return execution_preflight()
    return {"check-readiness": check_readiness, "reset-demo-data": reset_demo_data}[arguments.command]()


if __name__ == "__main__":
    sys.exit(main())
