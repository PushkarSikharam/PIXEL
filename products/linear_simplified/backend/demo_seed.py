"""Approved synthetic seed for one private Linear Simplified demo instance.

The seed is built from this package's synthetic records and then checked against the version and
checksum approved below. Changing any seed record changes the checksum, so a new demo refuses to
start until someone reviews the change, gives it a new version, and records the new checksum here.
A silently edited file can never become what a new visitor sees.
"""
from __future__ import annotations

import json

from app.services.demo_instances import DemoSeed, SeedNotApproved
from app.services.product_data_store import ISSUES_PATH, SEED_CYCLES, SEED_PROJECTS, SEED_TEAM
from app.workspace_config import WORKSPACE_SCOPES

APPROVED_SEED_VERSION = "linear-simplified-v1"
APPROVED_SEED_CHECKSUM = "1d5a15a1ff6c97fc11ee280c96015465a40a2e12b2f761ed2360274fb035d6e7"


def demo_seed() -> DemoSeed:
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
    seed = DemoSeed.from_records(APPROVED_SEED_VERSION, records)
    if seed.checksum != APPROVED_SEED_CHECKSUM:
        raise SeedNotApproved(
            f"{APPROVED_SEED_VERSION} does not match its approved checksum; review the seed "
            "change, give it a new version and record its checksum"
        )
    return seed
