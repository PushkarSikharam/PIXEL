"""Approved synthetic seed for one private Linear Simplified demo instance.

The seed is built from this package's synthetic records and then checked against the version and
checksum approved below. Changing any seed record changes the checksum, so a new demo refuses to
start until someone reviews the change, gives it a new version, and records the new checksum here.
A silently edited file can never become what a new visitor sees.
"""
from __future__ import annotations

import json

from app.services.demo_instances import DemoSeed, SeedNotApproved
from app.services.product_data_store import (
    ISSUES_PATH, SEED_CYCLES, SEED_PROJECTS, SEED_TEAM, dated,
)
from app.workspace_config import WORKSPACE_SCOPES

# v2 replaced each cycle's fixed start and end dates with a window of days either side of the day
# a demo starts, and made a cycle's progress the share of its planned work that is done. v1 had
# both stored independently, so the dashboard showed 68% beside 18 of 38 items done, and a cycle
# that had ended a fortnight earlier still called itself active with eight days left.
APPROVED_SEED_VERSION = "linear-simplified-v2"
APPROVED_SEED_CHECKSUM = "b8e4353de59359bd74d0487c43b4cb23cd2a917271a32eaa5757622a60546f00"


def approved_records() -> dict[str, dict[str, dict]]:
    """This package's synthetic records, checked against the approved checksum.

    A cycle is approved as a window of days rather than as a calendar, so the approved content is
    the same every day and review covers the seed itself.
    """
    records: dict[str, dict[str, dict]] = {
        "workspaceScopes": {
            scope.id: {
                "id": scope.id,
                "name": scope.name,
                "description": scope.description,
                "allowedProjectIds": sorted(scope.allowed_project_ids),
                "allowedIssueProjects": sorted(scope.allowed_issue_projects),
            }
            for scope in WORKSPACE_SCOPES
        },
        "projects": {row["id"]: dict(row) for row in SEED_PROJECTS},
        "team": {row["name"]: dict(row) for row in SEED_TEAM},
        "cycles": {row["id"]: dict(row) for row in SEED_CYCLES},
        "issues": {
            row["id"]: row
            for row in json.loads(ISSUES_PATH.read_text(encoding="utf-8"))
        },
    }
    if DemoSeed.from_records(APPROVED_SEED_VERSION, records).checksum != APPROVED_SEED_CHECKSUM:
        raise SeedNotApproved(
            f"{APPROVED_SEED_VERSION} does not match its approved checksum; review the seed "
            "change, give it a new version and record its checksum"
        )
    return records


def demo_seed() -> DemoSeed:
    """The approved seed, with each cycle's window turned into the dates a visitor sees.

    Dates are materialised only after approval, so a demo started today shows a cycle that is
    actually running today, and the approved content never depends on the current date.
    """
    records = approved_records()
    records["cycles"] = {key: dated(cycle) for key, cycle in records["cycles"].items()}
    return DemoSeed.from_records(APPROVED_SEED_VERSION, records)
