"""Approved synthetic seed for one private Linear Simplified demo instance."""
from __future__ import annotations

import json

from app.services.demo_instances import DemoSeed
from app.services.product_data_store import ISSUES_PATH, SEED_CYCLES, SEED_PROJECTS, SEED_TEAM
from app.workspace_config import WORKSPACE_SCOPES


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
    return DemoSeed.from_records("linear-simplified-v1", records)
