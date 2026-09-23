"""Operator commands, run on the server itself — never reachable through the public API.

Resetting everyone's demo data used to be one click on the public page, available to every
visitor because every visitor was signed in as the administrator. It now belongs to whoever can
open a shell on the server. From the repository root (or `/app` in the container):

    PYTHONPATH=apps/api python -m app.ops check-readiness
    PYTHONPATH=apps/api python -m app.ops reset-demo-data
    PYTHONPATH=apps/api python -m app.ops move-product-version --tenant <tenant> --product <product> --version <n>
    PYTHONPATH=apps/api python -m app.ops shadow-report [--days N]
    PYTHONPATH=apps/api python -m app.ops execution-preflight
    PYTHONPATH=apps/api python -m app.ops cutover-report [--days N]
    PYTHONPATH=apps/api python -m app.ops backup [--to DIR]
    PYTHONPATH=apps/api python -m app.ops verify-backup --path FILE
    PYTHONPATH=apps/api python -m app.ops acceptance-report --since YYYY-MM-DD [--deploy-at TS] [--product ID]

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

`backup` takes a consistent online copy of the database and then runs `verify-backup` on it.
`verify-backup` proves a copy restores: its integrity check passes and the application migrates
and starts against a scratch copy of it; the live database is never opened. Neither encrypts:
encrypt the copy with your own key before it leaves the host.

`acceptance-report` answers whether the live deployment meets the 5c acceptance gates (5d plan,
section 2.1): accepted turns, distinct sessions, the real/synthetic split, the window in days, the
workflows exercised, and whether a restart happened while conversations were open. It reads and
prints; it changes nothing, and exits non-zero when a gate is unmet.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from app.db import get_connection, migrate
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


def cutover_report(days: int) -> int:
    """Counts per authority over the last days (5c plan, section 10); metadata only."""
    from app.services import turn_telemetry

    print(json.dumps({"days": days, **turn_telemetry.report(days)}, indent=2))
    return 0


def execution_preflight() -> int:
    report = ExecutionLedger.preflight()
    print(json.dumps({"ready": not report["legacy_dispatched"], **report}, indent=2))
    return 1 if report["legacy_dispatched"] else 0


SYNTHETIC_PREFIX = "synthetic-visitor-check-"
# The 5c acceptance gates (5d plan, section 2.1, revision 6). Pixel has no visitors, so volume
# from strangers is replaced by coverage, durability and rehearsal. Sessions run by the operator
# are counted and reported as synthetic; nothing here ever calls them real.
GATES = {"days": 2}

# The workflows of the 5c matrix, written as the capabilities and outcomes a turn can be observed
# to have rather than as one product's nouns. The same gate therefore applies to any product the
# platform runs, and a workflow with no evidence is named rather than left to memory.
WORKFLOW_MATRIX: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "navigation": ("NAVIGATE_VIEW",),
    "open one record": ("OPEN_RECORD",),
    "filter records by a person": ("FILTER_RECORDS",),
    "point at a control": ("HIGHLIGHT_CONTROL",),
    "create a record": ("CREATE_RECORD",),
    "change a record": ("UPDATE_RECORD",),
})
# Outcomes proven by a turn's status rather than by an action it carried out.
OUTCOME_MATRIX: Mapping[str, str] = MappingProxyType({
    "refuse a request outside the product": "denied",
    "supersede a turn that lost its race": "stale",
})


def acceptance_report(since: str, deploy_at: list[str], product: str | None) -> int:
    """Does the live deployment meet the 5c acceptance gates? (5d plan, section 2.1a.)

    Read-only, and honest about what it can and cannot see: per-turn rows carry no authority, so
    everything counted here is "since <date>", which the operator sets to the cutover. Counts that
    do carry an authority come from the telemetry table and are labelled as such.
    """
    from app.services import turn_telemetry

    with get_connection() as connection:
        scope = (since, product) if product else (since,)
        product_clause = "and s.product_id = ?" if product else ""
        turns = connection.execute(
            f"""
            select m.session_id as session_id, count(*) as turns,
                   min(m.created_at) as first_at, max(m.created_at) as last_at
            from messages m join sessions s on s.id = m.session_id
            where m.role = 'user' and m.created_at >= ? {product_clause}
            group by m.session_id
            """,
            scope,
        ).fetchall()
        executions = connection.execute(
            f"""
            select e.action_key as action_key, e.capability as capability, e.state as state,
                   count(*) as total
            from action_executions e join sessions s on s.id = e.session_id
            where e.created_at >= ? {product_clause}
            group by e.action_key, e.capability, e.state
            """,
            scope,
        ).fetchall()
        moved = connection.execute(
            "select max(updated_at) as moved_at from product_bindings"
        ).fetchone()

    sessions = [dict(row) for row in turns]
    synthetic = [row for row in sessions if row["session_id"].startswith(SYNTHETIC_PREFIX)]
    real = [row for row in sessions if not row["session_id"].startswith(SYNTHETIC_PREFIX)]
    counts = sorted(row["turns"] for row in sessions)
    first_at = min((row["first_at"] for row in sessions), default=None)
    last_at = max((row["last_at"] for row in sessions), default=None)
    days = _days_between(first_at, last_at)

    telemetry = turn_telemetry.report(days=max(days, 1)).get("authorities", {}).get("definition", {})
    status = telemetry.get("status", {})
    stages = telemetry.get("stage", {})
    seen_actions = {name for name, total in telemetry.get("action", {}).items() if total and name != "none"}

    report = {
        "since": since,
        "product": product or "all",
        "window": {"first_turn_at": first_at, "last_turn_at": last_at, "days": days},
        "turns": {
            "counted": sum(counts),
            "real": sum(row["turns"] for row in real),
            "synthetic": sum(row["turns"] for row in synthetic),
            "by_session_p50": _percentile(counts, 0.5),
            "by_session_max": counts[-1] if counts else 0,
            "accepted_definition_authority": status.get("completed", 0),
            "denied_definition_authority": status.get("denied", 0),
            "stale_definition_authority": status.get("stale", 0),
        },
        "sessions": {"total": len(sessions), "real": len(real), "synthetic": len(synthetic)},
        "workflows": {
            **_workflow_coverage([dict(row) for row in executions], status),
            "actions_seen": sorted(seen_actions),
            "stages_seen": sorted(name for name, total in stages.items() if total),
            "executions": [dict(row) for row in executions],
        },
        "restarts": [_restart(sessions, moment) for moment in deploy_at],
        "failures": {
            "stale": status.get("stale", 0),
            "failed_executions": sum(row["total"] for row in executions if row["state"] == "failed"),
        },
        "resets": {"last_definition_version_move": moved["moved_at"] if moved else None},
        "note": (
            "Per-turn rows carry no authority, so counted turns and sessions are everything since "
            "--since; set it to the cutover. Counts named *_definition_authority come from "
            "telemetry, which does record the authority. Sessions whose id begins "
            f"'{SYNTHETIC_PREFIX}' were run by the operator and are reported as synthetic; this "
            "command never describes a session as a real visitor. Two gates it cannot decide are "
            "the rollback rehearsal and the owner's recorded decision to stop preserving rollback."
        ),
    }
    report["gates"] = _gates(report)
    report["ready"] = all(gate["met"] for gate in report["gates"])
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


def _gates(report: dict) -> list[dict]:
    """Every gate of section 2.1 that a command can decide. Coverage, durability and quality are
    decided here; the rollback rehearsal and the owner's recorded decision are not, and the
    report says so rather than implying it checked them."""
    turns, window = report["turns"], report["window"]
    accepted = turns["accepted_definition_authority"]
    gates = [
        {"gate": "days covered", "have": window["days"], "need": GATES["days"],
         "met": window["days"] >= GATES["days"]},
        {"gate": "accepted turns under definition authority", "have": accepted, "need": 1,
         "met": accepted >= 1},
    ]
    missing = report["workflows"]["missing"]
    gates.append({"gate": "every workflow of the matrix has evidence",
                  "have": report["workflows"]["covered"],
                  "need": "no workflow missing", "met": not missing})
    denied = turns["denied_definition_authority"]
    stale = turns["stale_definition_authority"]
    total = accepted + denied + stale
    # Proving supersession requires deliberately losing one turn, so that turn is not counted
    # against the rate. Without this the coverage run would fail a gate its own evidence created.
    unexplained = max(0, stale - 1)
    stale_rate = (unexplained / total) if total else 0.0
    gates.append({"gate": "unexplained stale rate at or below 1%", "have": round(stale_rate, 4),
                  "need": 0.01, "met": stale_rate <= 0.01})
    gates.append({"gate": "no failed execution", "have": report["failures"]["failed_executions"],
                  "need": 0, "met": report["failures"]["failed_executions"] == 0})
    gates.append({"gate": "a restart happened while conversations were open",
                  "have": sum(entry["sessions_open"] for entry in report["restarts"]), "need": 1,
                  "met": any(entry["sessions_open"] for entry in report["restarts"])})
    return gates


def _workflow_coverage(executions: list[dict], status: Mapping[str, int]) -> dict:
    """Which workflows of the matrix have evidence, and which have none."""
    carried_out = {row["capability"] for row in executions if row["state"] != "failed"}
    seen, missing = [], []
    for workflow, capabilities in WORKFLOW_MATRIX.items():
        (seen if carried_out.intersection(capabilities) else missing).append(workflow)
    for workflow, outcome in OUTCOME_MATRIX.items():
        (seen if status.get(outcome, 0) else missing).append(workflow)
    return {"covered": sorted(seen), "missing": sorted(missing)}


def _restart(sessions: list[dict], moment: str) -> dict:
    open_then = [
        row for row in sessions
        if row["first_at"] and row["last_at"] and row["first_at"] <= moment <= row["last_at"]
    ]
    return {"deploy_at": moment, "sessions_open": len(open_then)}


def _percentile(ordered: list[int], fraction: float) -> int:
    if not ordered:
        return 0
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _days_between(first: str | None, last: str | None) -> int:
    if not first or not last:
        return 0
    from datetime import datetime

    try:
        start, end = datetime.fromisoformat(first), datetime.fromisoformat(last)
    except ValueError:
        return 0
    return max(1, (end.date() - start.date()).days + 1)


def backup(destination: str | None) -> int:
    """A consistent copy of the live database (SQLite online backup), then its restore check.

    The copy is not encrypted here: encrypt it with the operator's own key before it leaves the
    host (5c plan, section 11.2, step 1).
    """
    from app import db

    path = db.backup_database() if destination is None else _backup_to(Path(destination))
    if path is None:
        print(json.dumps({"backed_up": False, "reason": "no database to back up"}, indent=2))
        return 1
    return verify_backup(str(path))


def _backup_to(directory: Path) -> Path:
    import sqlite3
    import time

    from app import db

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{db.DB_PATH.stem}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    source, target = sqlite3.connect(db.DB_PATH), sqlite3.connect(path)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()
    return path


def verify_backup(backup_path: str) -> int:
    """Prove a backup restores: it is intact, and the application starts against a copy of it.

    The live database is never touched: the copy is migrated in a scratch directory.
    """
    import hashlib
    import shutil
    import sqlite3
    import tempfile
    from unittest.mock import patch

    from app import db

    path = Path(backup_path)
    if not path.is_file():
        print(json.dumps({"restorable": False, "reason": f"{path} does not exist"}, indent=2))
        return 1
    started, problem, counts = False, None, {}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        tables = [row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]
        counts = {table: connection.execute(f'select count(*) from "{table}"').fetchone()[0] for table in tables}
    except sqlite3.DatabaseError as error:
        integrity, problem = "unreadable", f"{type(error).__name__}: {error}"
    finally:
        connection.close()
    if integrity == "ok":
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "restore-check.sqlite3"
            shutil.copyfile(path, copy)
            try:
                with patch.object(db, "DB_PATH", copy):
                    db.migrate()
                started = True
            except Exception as error:  # the report says why the restore would not start
                problem = f"{type(error).__name__}: {error}"
    restorable = integrity == "ok" and started
    print(json.dumps({
        "restorable": restorable,
        "backup": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "integrity": integrity,
        "application_starts_on_copy": started,
        "problem": problem,
        "rows": counts,
    }, indent=2))
    return 0 if restorable else 1


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
    cutover = commands.add_parser("cutover-report")
    cutover.add_argument("--days", type=int, default=1)
    backup_command = commands.add_parser("backup")
    backup_command.add_argument("--to", help="directory for the copy (default: backups/ beside the database)")
    verify = commands.add_parser("verify-backup")
    verify.add_argument("--path", required=True)
    acceptance = commands.add_parser("acceptance-report")
    acceptance.add_argument("--since", required=True, help="ISO date the cutover happened, for example 2026-09-22")
    acceptance.add_argument("--deploy-at", action="append", default=[],
                            help="ISO timestamp of a restart or redeploy; repeatable")
    acceptance.add_argument("--product", help="limit the report to one installed product")
    arguments = parser.parse_args(argv)
    if arguments.command == "verify-backup":
        # Checking a copy never migrates, or even opens, the live database.
        return verify_backup(arguments.path)
    if arguments.command == "backup":
        # The copy is taken before this command migrates anything.
        return backup(arguments.to)
    migrate()
    if arguments.command == "acceptance-report":
        return acceptance_report(arguments.since, arguments.deploy_at, arguments.product)
    if arguments.command == "move-product-version":
        return move_product_version(arguments.tenant, arguments.product, arguments.version)
    if arguments.command == "shadow-report":
        return shadow_report(arguments.days)
    if arguments.command == "cutover-report":
        return cutover_report(arguments.days)
    if arguments.command == "execution-preflight":
        return execution_preflight()
    return {"check-readiness": check_readiness, "reset-demo-data": reset_demo_data}[arguments.command]()


if __name__ == "__main__":
    sys.exit(main())
