"""Cutover telemetry (5c plan, section 10): daily counts of what each authority did, metadata only.

A count is keyed by day, deployment, tenant, product, definition version, authority, metric and a
bounded value (a status, a stage, a receipt code or a latency band). No message, field value,
record title, prompt, provider output, execution key, session ID or user ID is ever written here.
Rows older than 30 days are pruned by the same maintenance that prunes the execution ledger.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import re

from app.db import get_connection

RETENTION_DAYS = 30
# Latency bands, in milliseconds: coarse enough to be metadata, fine enough to compare authorities.
LATENCY_BANDS = ((250, "lt_250ms"), (1_000, "lt_1s"), (3_000, "lt_3s"))
_VALUE = re.compile(r"^[a-z0-9_]{1,40}$")
logger = logging.getLogger("pixel.telemetry")


def create_schema(connection) -> None:
    connection.executescript(
        """
        create table if not exists turn_telemetry_daily(
          day text not null,
          deployment_id text not null,
          tenant_id text not null,
          product_id text not null,
          definition_version integer not null,
          authority text not null,
          metric text not null,
          value text not null,
          count integer not null check (count >= 0),
          primary key (day, deployment_id, tenant_id, product_id, definition_version, authority, metric, value)
        );
        """
    )


def action_name(action_type: str | None) -> str:
    """An action's own name as a telemetry value, or "none" for a turn that acted on nothing."""
    if not action_type:
        return "none"
    value = re.sub(r"[^a-z0-9_]", "_", action_type.lower())[:40]
    return value if _VALUE.match(value) else "other"


def latency_band(milliseconds: float) -> str:
    return next((name for limit, name in LATENCY_BANDS if milliseconds < limit), "ge_3s")


def record(*, deployment_id: str, tenant_id: str, product_id: str, definition_version: int | None,
           authority: str, counts: dict[str, str]) -> None:
    """Add one to each (metric, value). A failure is logged and never reaches the visitor."""
    rows = [(metric, value) for metric, value in counts.items() if _VALUE.match(metric) and _VALUE.match(value)]
    if not rows:
        return
    day = datetime.now(UTC).date().isoformat()
    try:
        with get_connection() as connection:
            connection.executemany(
                """
                insert into turn_telemetry_daily(day, deployment_id, tenant_id, product_id,
                  definition_version, authority, metric, value, count)
                values (?, ?, ?, ?, ?, ?, ?, ?, 1)
                on conflict(day, deployment_id, tenant_id, product_id, definition_version, authority, metric, value)
                do update set count = count + 1
                """,
                [(day, deployment_id, tenant_id, product_id, definition_version or 0, authority, metric, value)
                 for metric, value in rows],
            )
    except Exception as error:  # noqa: BLE001 - telemetry never fails a turn
        logger.warning("telemetry_write_failed", extra={"error": type(error).__name__})


def prune(now: datetime | None = None) -> int:
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)).date().isoformat()
    with get_connection() as connection:
        return connection.execute("delete from turn_telemetry_daily where day < ?", (cutoff,)).rowcount


def report(days: int = 1, now: datetime | None = None) -> dict:
    """Counts per authority and metric over the last `days` days, with a few derived rates."""
    since = ((now or datetime.now(UTC)) - timedelta(days=days - 1)).date().isoformat()
    with get_connection() as connection:
        rows = connection.execute(
            """
            select authority, metric, value, sum(count) as total from turn_telemetry_daily
            where day >= ? group by authority, metric, value order by authority, metric, value
            """,
            (since,),
        ).fetchall()
    authorities: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        authorities.setdefault(row["authority"], {}).setdefault(row["metric"], {})[row["value"]] = row["total"]
    for metrics in authorities.values():
        statuses = metrics.get("status", {})
        turns = sum(statuses.values())
        metrics["rates"] = {
            "turns": turns,
            "denied": round(statuses.get("denied", 0) / turns, 4) if turns else 0.0,
            "stale": round(statuses.get("stale", 0) / turns, 4) if turns else 0.0,
            "fallback": round(metrics.get("stage", {}).get("fallback", 0) / turns, 4) if turns else 0.0,
        }
    return {"since": since, "authorities": authorities}
