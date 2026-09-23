from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from datetime import date, timedelta
import re
from pathlib import Path
from typing import Any

from app.db import get_connection, use_connection
from app.workspace_config import WORKSPACE_SCOPES, WorkspaceScope
from app.services.demo_instances import (
    DemoContext,
    DemoInstanceStore,
    DemoSeed,
    InstanceCapacity,
    InstanceConflict,
    InstanceUnavailable,
)


API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[1]
ISSUES_PATH = REPO_ROOT / "packages" / "shared" / "demo-data" / "issues.json"


class RecordConflict(ValueError):
    pass


class RecordNotFound(ValueError):
    pass


class InvalidReference(ValueError):
    pass


class ScopeViolation(ValueError):
    pass


SEED_PROJECTS: tuple[dict[str, Any], ...] = (
    {
        "id": "PRJ-101",
        "name": "GitHub Integration Hardening",
        "description": "Improve PR sync reliability, webhook recovery, and commit-to-issue visibility.",
        "progress": 68,
        "status": "Active",
        "lead": "Maya Chen",
        "team": "Product Engineering",
        "targetDate": "2026-09-28",
    },
    {
        "id": "PRJ-102",
        "name": "Issue Triage Workflow",
        "description": "Reduce duplicate reports and speed up assignment, priority, and status updates.",
        "progress": 54,
        "status": "Active",
        "lead": "Noah Patel",
        "team": "Product Engineering",
        "targetDate": "2026-10-12",
    },
    {
        "id": "PRJ-103",
        "name": "Cycle Planning Insights",
        "description": "Forecast sprint capacity, burndown risks, and planning accuracy.",
        "progress": 42,
        "status": "At risk",
        "lead": "Avery Brooks",
        "team": "Platform",
        "targetDate": "2026-09-18",
    },
    {
        "id": "PRJ-104",
        "name": "Workspace Migration",
        "description": "Move archived project history into the new operating model.",
        "progress": 31,
        "status": "Planned",
        "lead": "Iris Morgan",
        "team": "Platform",
        "targetDate": "2026-11-04",
    },
)

SEED_TEAM: tuple[dict[str, Any], ...] = (
    {
        "name": "Maya Chen",
        "initials": "MC",
        "role": "Frontend Lead",
        "load": 84,
        "projectIds": ["PRJ-101"],
    },
    {
        "name": "Noah Patel",
        "initials": "NP",
        "role": "Product Engineer",
        "load": 71,
        "projectIds": ["PRJ-102"],
    },
    {
        "name": "Avery Brooks",
        "initials": "AB",
        "role": "Engineering Manager",
        "load": 63,
        "projectIds": ["PRJ-103"],
    },
    {
        "name": "Iris Morgan",
        "initials": "IM",
        "role": "Platform Engineer",
        "load": 77,
        "projectIds": ["PRJ-104"],
    },
)

def _window(starts_in: int, ends_in: int) -> dict[str, int]:
    """A cycle's window, in days either side of the day a demo is seeded.

    Calendar dates written into the seed went stale: a cycle that had ended a fortnight earlier
    still called itself active with eight days left. The window is what is reviewed and approved;
    `dated` turns it into dates when a demo actually starts.
    """
    return {"startsInDays": starts_in, "endsInDays": ends_in}


def dated(cycle: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """The cycle with its window materialised as calendar dates and days remaining."""
    if "startsInDays" not in cycle:
        return dict(cycle)
    anchor = today or date.today()
    materialised = {name: value for name, value in cycle.items()
                    if name not in ("startsInDays", "endsInDays")}
    materialised["startDate"] = str(anchor + timedelta(days=cycle["startsInDays"]))
    materialised["endDate"] = str(anchor + timedelta(days=cycle["endsInDays"]))
    materialised["daysLeft"] = max(0, cycle["endsInDays"])
    return materialised


def _counted(completed: int, in_progress: int, remaining: int) -> dict[str, int]:
    """Progress is the share of planned work that is done; it is never typed in separately."""
    planned = completed + in_progress + remaining
    return {"completed": completed, "inProgress": in_progress, "remaining": remaining,
            "progress": round(completed * 100 / planned) if planned else 0}


SEED_CYCLES: tuple[dict[str, Any], ...] = (
    {
        "id": "CYC-14",
        "name": "Product Engineering Cycle 14",
        "projectId": "PRJ-101",
        **_counted(18, 9, 11),
        "focus": ["Bug triage", "Cycle planning", "GitHub sync", "Assignment flow"],
        "status": "Active",
        "team": "Product Engineering",
        **_window(-7, 8),
    },
    {
        "id": "CYC-21",
        "name": "Platform Cycle 21",
        "projectId": "PRJ-103",
        **_counted(7, 6, 9),
        "focus": ["Capacity forecast", "Migration readiness", "Planning accuracy"],
        "status": "Active",
        "team": "Platform",
        **_window(-9, 6),
    },
)


class ProductDataStore:
    def __init__(self, demo_context: DemoContext | None = None,
                 instances: DemoInstanceStore | None = None) -> None:
        self.demo_context = demo_context
        self.instances = instances or DemoInstanceStore()

    def load(self, scope_ids: frozenset[str] | None = None,
             connection=None) -> dict[str, list[dict[str, Any]]]:
        """Load records; with scope_ids, only records inside those workspaces.

        With `connection`, every table is read through the caller's open transaction, so one call
        gives a single consistent view (used by the turn snapshot).
        """
        data = self._load_private(connection) if self.demo_context else self._load_all(connection)
        return data if scope_ids is None else filter_to_scopes(data, scope_ids)

    def workspace_scope(self, scope_id: str) -> WorkspaceScope | None:
        if self.demo_context:
            row = next((item for item in self.load()["workspaceScopes"] if item["id"] == scope_id), None)
            if row is None:
                return None
            project_ids = frozenset(row["allowedProjectIds"])
            members = self.load()["team"]
            return WorkspaceScope(
                row["id"], row["name"], row["description"], project_ids,
                frozenset(row["allowedIssueProjects"]),
                frozenset(member["name"] for member in members
                          if project_ids.intersection(member.get("projectIds", []))),
            )
        self.seed_if_empty()
        with get_connection() as connection:
            row = connection.execute(
                "select * from demo_workspace_scopes where id = ?", (scope_id,)
            ).fetchone()
            if row is None:
                return None
            members = connection.execute("select name, project_ids from demo_team_members").fetchall()
        project_ids = frozenset(json.loads(row["allowed_project_ids"]))
        return WorkspaceScope(
            row["id"],
            row["name"],
            row["description"],
            project_ids,
            frozenset(json.loads(row["allowed_issue_projects"])),
            frozenset(member["name"] for member in members
                      if project_ids.intersection(json.loads(member["project_ids"]))),
        )

    def get_issue(self, issue_id: str, connection=None) -> dict[str, Any] | None:
        if self.demo_context:
            records = self.instances.read(self.demo_context, connection)
            row = records.get("issues", {}).get(issue_id)
            return _with_revision(row) if row else None
        if connection is None:
            self.seed_if_empty()
        with use_connection(connection) as connection:
            row = connection.execute("select * from demo_issues where id = ?", (issue_id,)).fetchone()
        return _issue_from_row(row) if row else None

    def scopes_for_record(self, project_id: str | None, issue_project: str | None = None,
                          connection=None) -> set[str]:
        """Workspaces that contain a record, by project ID or, failing that, issue project label."""
        if self.demo_context:
            rows = self.load(connection=connection)["workspaceScopes"]
            if project_id:
                return {row["id"] for row in rows if project_id in row["allowedProjectIds"]}
            return {row["id"] for row in rows
                    if issue_project and issue_project in row["allowedIssueProjects"]}
        if connection is None:
            self.seed_if_empty()
        with use_connection(connection) as connection:
            rows = connection.execute("select * from demo_workspace_scopes").fetchall()
        if project_id:
            return {row["id"] for row in rows if project_id in json.loads(row["allowed_project_ids"])}
        return {row["id"] for row in rows
                if issue_project and issue_project in json.loads(row["allowed_issue_projects"])}

    def _load_all(self, connection=None) -> dict[str, list[dict[str, Any]]]:
        if connection is None:
            self.seed_if_empty()
        with use_connection(connection) as connection:
            return {
                "workspaceScopes": [
                    _scope_from_row(row)
                    for row in connection.execute(
                        "select * from demo_workspace_scopes order by id"
                    ).fetchall()
                ],
                "projects": [
                    _project_from_row(row)
                    for row in connection.execute(
                        "select * from demo_projects order by id"
                    ).fetchall()
                ],
                "team": [
                    _member_from_row(row)
                    for row in connection.execute(
                        "select * from demo_team_members order by name"
                    ).fetchall()
                ],
                "cycles": [
                    _cycle_from_row(row)
                    for row in connection.execute(
                        "select * from demo_cycles order by id"
                    ).fetchall()
                ],
                "issues": [
                    _issue_from_row(row)
                    for row in connection.execute(
                        "select * from demo_issues order by id"
                    ).fetchall()
                ],
            }

    def seed_if_empty(self) -> None:
        if self.demo_context:
            self.instances.assert_available(self.demo_context)
            return
        with get_connection() as connection:
            row = connection.execute("select count(*) as count from demo_projects").fetchone()
            if row and row["count"] > 0:
                return
        self.reset()

    def reset(self) -> dict[str, list[dict[str, Any]]]:
        if self.demo_context:
            raise RuntimeError("private demos reset through reset_private with their pinned seed")
        issues = json.loads(ISSUES_PATH.read_text(encoding="utf-8"))
        with get_connection() as connection:
            connection.executescript(
                """
                delete from demo_issues;
                delete from demo_cycles;
                delete from demo_team_members;
                delete from demo_projects;
                delete from demo_workspace_scopes;
                delete from mutation_receipts;
                """
            )

            for scope in WORKSPACE_SCOPES:
                connection.execute(
                    """
                    insert into demo_workspace_scopes(
                      id, name, description, allowed_project_ids, allowed_issue_projects
                    )
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        scope.id,
                        scope.name,
                        scope.description,
                        json.dumps(sorted(scope.allowed_project_ids)),
                        json.dumps(sorted(scope.allowed_issue_projects)),
                    ),
                )
            for project in SEED_PROJECTS:
                self._upsert_project(connection, project)
            for member in SEED_TEAM:
                self._upsert_member(connection, member)
            for cycle in SEED_CYCLES:
                self._upsert_cycle(connection, dated(cycle))
            for issue in issues:
                self._upsert_issue(connection, issue)

        return self.load()

    def reset_private(self, seed: DemoSeed, connection=None) -> tuple[DemoContext, dict[str, list[dict[str, Any]]]]:
        if self.demo_context is None:
            raise RuntimeError("a private demo context is required")
        updated = self.instances.reset(self.demo_context, seed, connection=connection)
        return updated, ProductDataStore(updated, self.instances).load(connection=connection)

    def save_issue(self, issue: dict[str, Any], request_key: str | None = None,
                   connection=None) -> dict[str, Any]:
        return self._create_record("issue", issue, request_key=request_key, connection=connection)

    def update_issue(self, issue_id: str, issue: dict[str, Any], connection=None) -> dict[str, Any]:
        """Change one ticket. With `connection`, the caller owns the transaction (see 3.2 slice 3)."""
        if self.demo_context:
            return self._private_update_issue(issue_id, issue, connection)
        if connection is None:
            self.seed_if_empty()
        issue = {**issue, "id": issue_id}
        with self._transaction(connection) as open_connection:
            existing = open_connection.execute(
                "select * from demo_issues where id = ?", (issue_id,)
            ).fetchone()
            if existing is None:
                raise RecordNotFound("This ticket no longer exists.")
            _check_references(open_connection, "issue", issue, previous_issue=_issue_from_row(existing))
            self._upsert_issue(open_connection, issue)
        return issue

    def save_project(self, project: dict[str, Any], workspace_scope_id: str,
                     request_key: str | None = None, connection=None) -> dict[str, Any]:
        return self._create_record("project", project, workspace_scope_id, request_key, connection)

    def save_cycle(self, cycle: dict[str, Any], request_key: str | None = None,
                   connection=None) -> dict[str, Any]:
        return self._create_record("cycle", cycle, request_key=request_key, connection=connection)

    def save_team_member(
        self,
        member: dict[str, Any],
        workspace_scope_id: str,
        request_key: str | None = None,
        connection=None,
    ) -> dict[str, Any]:
        return self._create_record("member", member, workspace_scope_id, request_key, connection)

    @contextmanager
    def _transaction(self, connection):
        """The caller's transaction when one is given, otherwise our own write transaction.

        A supplied connection must already hold `begin immediate`, so the record change and
        whatever else the caller writes (an execution outcome) commit together.
        """
        if connection is not None:
            if not connection.in_transaction:
                raise RuntimeError("a caller-owned connection must already hold a write transaction")
            yield connection
            return
        with get_connection() as own_connection:
            own_connection.execute("begin immediate")
            yield own_connection

    def _create_record(self, kind: str, record: dict[str, Any],
                       scope_id: str | None = None, request_key: str | None = None,
                       connection=None) -> dict[str, Any]:
        if self.demo_context:
            return self._private_create_record(kind, record, scope_id, request_key, connection)
        if connection is None:
            self.seed_if_empty()
        table, key_column, prefix, writer = {
            "issue": ("demo_issues", "id", "PIX", self._upsert_issue),
            "project": ("demo_projects", "id", "PRJ", self._upsert_project),
            "cycle": ("demo_cycles", "id", "CYC", self._upsert_cycle),
            "member": ("demo_team_members", "name", None, self._upsert_member),
        }[kind]
        request_body = json.dumps([kind, scope_id, record], sort_keys=True)
        record = dict(record)
        with self._transaction(connection) as connection:
            # Serialize allocation, duplicate checks, record writes and retry receipts.
            if request_key:
                receipt = connection.execute(
                    "select * from mutation_receipts where request_key = ?", (request_key,)
                ).fetchone()
                if receipt:
                    if receipt["request_body"] != request_body:
                        raise RecordConflict("This request key was already used for a different change.")
                    return json.loads(receipt["response_body"])
            if scope_id:
                scope = connection.execute(
                    "select * from demo_workspace_scopes where id = ?", (scope_id,)
                ).fetchone()
                if scope is None:
                    raise RecordNotFound("This workspace no longer exists.")
                if kind == "member":
                    scope_project_ids = json.loads(scope["allowed_project_ids"])
                    if not record.get("projectIds"):
                        record["projectIds"] = scope_project_ids
                    elif not set(record["projectIds"]) <= set(scope_project_ids):
                        raise ScopeViolation(
                            "Team members can only join projects in this workspace. No records were changed."
                        )
            if prefix and not record.get("id"):
                ids = connection.execute(f"select id from {table}").fetchall()
                numbers = [int(match.group(1)) for row in ids
                           if (match := re.search(r"-(\d+)$", row["id"]))]
                record["id"] = f"{prefix}-{max(numbers, default=0) + 1}"
            if connection.execute(
                f"select 1 from {table} where {key_column} = ?", (record[key_column],)
            ).fetchone():
                raise RecordConflict(f"A {kind} with this identifier already exists. No records were changed.")
            _check_references(connection, kind, record)
            writer(connection, record)
            if kind == "project":
                _add_project_to_scope(connection, scope_id, record)
            if request_key:
                connection.execute(
                    "insert into mutation_receipts values (?, ?, ?)",
                    (request_key, request_body, json.dumps(record)),
                )
        return record

    def _load_private(self, connection=None) -> dict[str, list[dict[str, Any]]]:
        records = self.instances.read(self.demo_context, connection)
        return {
            name: [_with_revision(row) for _, row in sorted(records.get(name, {}).items())]
            for name in ("workspaceScopes", "projects", "team", "cycles", "issues")
        }

    @contextmanager
    def _private_transaction(self, connection):
        if connection is not None:
            if not connection.in_transaction:
                raise RuntimeError("a caller-owned connection must already hold a write transaction")
            self.instances.assert_available(self.demo_context, connection)
            yield connection
            return
        with self.instances.transaction(self.demo_context) as own:
            yield own

    def _private_create_record(self, kind: str, record: dict[str, Any], scope_id: str | None,
                               request_key: str | None, connection=None) -> dict[str, Any]:
        source, key_name, prefix = {
            "issue": ("issues", "id", "PIX"),
            "project": ("projects", "id", "PRJ"),
            "cycle": ("cycles", "id", "CYC"),
            "member": ("team", "name", None),
        }[kind]
        record = {name: value for name, value in dict(record).items() if name != "revision"}
        request_body = json.dumps([kind, scope_id, record], sort_keys=True, separators=(",", ":"))
        request_digest = hashlib.sha256(request_body.encode()).hexdigest()
        with self._private_transaction(connection) as open_connection:
            if request_key:
                receipt = open_connection.execute(
                    "select request_digest, entity, record_id from demo_instance_receipts "
                    "where tenant_id=? and product_id=? and instance_id=? and generation=? and request_key=?",
                    (*self.instances.key(self.demo_context), self.demo_context.generation, request_key),
                ).fetchone()
                if receipt:
                    if receipt["request_digest"] != request_digest:
                        raise RecordConflict("This request key was already used for a different change.")
                    existing = self.instances.read(self.demo_context, open_connection).get(
                        receipt["entity"], {}
                    ).get(receipt["record_id"])
                    if existing is None:
                        raise RecordConflict("The earlier result is no longer available.")
                    return _with_revision(existing)
            data = self.instances.read(self.demo_context, open_connection)
            if scope_id:
                scope_row = data.get("workspaceScopes", {}).get(scope_id)
                if scope_row is None:
                    raise RecordNotFound("This workspace no longer exists.")
                scope = scope_row["data"]
                if kind == "member":
                    allowed = set(scope["allowedProjectIds"])
                    if not record.get("projectIds"):
                        record["projectIds"] = sorted(allowed)
                    elif not set(record["projectIds"]) <= allowed:
                        raise ScopeViolation(
                            "Team members can only join projects in this workspace. No records were changed."
                        )
            if prefix and not record.get("id"):
                numbers = [int(match.group(1)) for identifier in data.get(source, {})
                           if (match := re.search(r"-(\d+)$", identifier))]
                record["id"] = f"{prefix}-{max(numbers, default=0) + 1}"
            record_id = record[key_name]
            if record_id in data.get(source, {}):
                raise RecordConflict(f"A {kind} with this identifier already exists. No records were changed.")
            references = self._private_references(kind, record, data, previous_issue=None)
            revision = self.instances.put(
                self.demo_context, source, record_id, record, expected_revision=None,
                references=references, connection=open_connection,
            )
            if kind == "project" and scope_id:
                scope_row = data["workspaceScopes"][scope_id]
                scope = dict(scope_row["data"])
                scope["allowedProjectIds"] = sorted(set(scope["allowedProjectIds"]) | {record_id})
                scope["allowedIssueProjects"] = sorted(
                    set(scope["allowedIssueProjects"]) | {record["name"]}
                )
                self.instances.put(
                    self.demo_context, "workspaceScopes", scope_id, scope,
                    expected_revision=scope_row["revision"], connection=open_connection,
                )
            if request_key:
                open_connection.execute(
                    "insert into demo_instance_receipts values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*self.instances.key(self.demo_context), self.demo_context.generation,
                     request_key, request_digest, source, record_id),
                )
        return {**record, "revision": revision}

    def _private_update_issue(self, issue_id: str, issue: dict[str, Any], connection=None) -> dict[str, Any]:
        expected = issue.get("revision")
        if type(expected) is not int or expected < 1:
            raise RecordConflict("Reload this ticket before saving it.")
        clean = {name: value for name, value in dict(issue).items() if name != "revision"}
        clean["id"] = issue_id
        with self._private_transaction(connection) as open_connection:
            data = self.instances.read(self.demo_context, open_connection)
            existing = data.get("issues", {}).get(issue_id)
            if existing is None:
                raise RecordNotFound("This ticket no longer exists.")
            references = self._private_references(
                "issue", clean, data, previous_issue=existing["data"]
            )
            revision = self.instances.put(
                self.demo_context, "issues", issue_id, clean,
                expected_revision=expected, references=references, connection=open_connection,
            )
        return {**clean, "revision": revision}

    def _private_references(self, kind: str, record: dict[str, Any], data,
                            previous_issue: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        references: list[tuple[str, str]] = []
        project_id = record.get("projectId")
        if kind in ("issue", "cycle") and project_id:
            if project_id not in data.get("projects", {}):
                raise InvalidReference(f"Project {project_id} does not exist.")
            references.append(("projects", project_id))
        if kind != "issue":
            return tuple(references)
        assignee = record["assignee"]
        member = data.get("team", {}).get(assignee)
        if member is None:
            raise InvalidReference(f"{assignee} is not a member of this team.")
        scopes = [row["data"] for row in data.get("workspaceScopes", {}).values()]
        workspace_projects = {
            project
            for scope in scopes
            if (project_id in scope["allowedProjectIds"] if project_id
                else record["project"] in scope["allowedIssueProjects"])
            for project in scope["allowedProjectIds"]
        }
        unchanged = previous_issue and assignee == previous_issue.get("assignee") and (
            previous_issue.get("projectId"), previous_issue.get("project")
        ) == (project_id, record.get("project"))
        if not unchanged and not workspace_projects.intersection(member["data"].get("projectIds", [])):
            raise InvalidReference(f"{assignee} does not work in this ticket's workspace.")
        references.append(("team", assignee))
        return tuple(references)

    def _upsert_issue(self, connection, issue: dict[str, Any]) -> None:
        connection.execute(
            """
            insert into demo_issues(
              id, title, priority, assignee, project, project_id, status, cycle,
              estimate, label, description
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
              title = excluded.title,
              priority = excluded.priority,
              assignee = excluded.assignee,
              project = excluded.project,
              project_id = excluded.project_id,
              status = excluded.status,
              cycle = excluded.cycle,
              estimate = excluded.estimate,
              label = excluded.label,
              description = excluded.description
            """,
            (
                issue["id"],
                issue["title"],
                issue["priority"],
                issue["assignee"],
                issue["project"],
                issue.get("projectId"),
                issue["status"],
                issue.get("cycle"),
                issue.get("estimate"),
                issue.get("label"),
                issue.get("description"),
            ),
        )

    def _upsert_project(self, connection, project: dict[str, Any]) -> None:
        connection.execute(
            """
            insert into demo_projects(
              id, name, description, progress, status, lead, team, target_date
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
              name = excluded.name,
              description = excluded.description,
              progress = excluded.progress,
              status = excluded.status,
              lead = excluded.lead,
              team = excluded.team,
              target_date = excluded.target_date
            """,
            (
                project["id"],
                project["name"],
                project["description"],
                int(project["progress"]),
                project["status"],
                project["lead"],
                project["team"],
                project["targetDate"],
            ),
        )

    def _upsert_member(self, connection, member: dict[str, Any]) -> None:
        connection.execute(
            """
            insert into demo_team_members(name, initials, role, load, email, project_ids)
            values (?, ?, ?, ?, ?, ?)
            on conflict(name) do update set
              initials = excluded.initials,
              role = excluded.role,
              load = excluded.load,
              email = excluded.email,
              project_ids = excluded.project_ids
            """,
            (
                member["name"],
                member["initials"],
                member["role"],
                int(member["load"]),
                member.get("email"),
                json.dumps(member.get("projectIds", [])),
            ),
        )

    def _upsert_cycle(self, connection, cycle: dict[str, Any]) -> None:
        connection.execute(
            """
            insert into demo_cycles(
              id, name, project_id, days_left, progress, completed, in_progress,
              remaining, focus, status, team, start_date, end_date
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
              name = excluded.name,
              project_id = excluded.project_id,
              days_left = excluded.days_left,
              progress = excluded.progress,
              completed = excluded.completed,
              in_progress = excluded.in_progress,
              remaining = excluded.remaining,
              focus = excluded.focus,
              status = excluded.status,
              team = excluded.team,
              start_date = excluded.start_date,
              end_date = excluded.end_date
            """,
            (
                cycle["id"],
                cycle["name"],
                cycle.get("projectId"),
                int(cycle["daysLeft"]),
                int(cycle["progress"]),
                int(cycle["completed"]),
                int(cycle["inProgress"]),
                int(cycle["remaining"]),
                json.dumps(cycle.get("focus", [])),
                cycle["status"],
                cycle["team"],
                cycle["startDate"],
                cycle["endDate"],
            ),
        )


def _scope_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "allowedProjectIds": json.loads(row["allowed_project_ids"]),
        "allowedIssueProjects": json.loads(row["allowed_issue_projects"]),
    }


def _project_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "progress": row["progress"],
        "status": row["status"],
        "lead": row["lead"],
        "team": row["team"],
        "targetDate": row["target_date"],
    }


def _member_from_row(row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "initials": row["initials"],
        "role": row["role"],
        "load": row["load"],
        "email": row["email"],
        "projectIds": json.loads(row["project_ids"]),
    }


def _cycle_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "projectId": row["project_id"],
        "daysLeft": row["days_left"],
        "progress": row["progress"],
        "completed": row["completed"],
        "inProgress": row["in_progress"],
        "remaining": row["remaining"],
        "focus": json.loads(row["focus"]),
        "status": row["status"],
        "team": row["team"],
        "startDate": row["start_date"],
        "endDate": row["end_date"],
    }


def _issue_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "priority": row["priority"],
        "assignee": row["assignee"],
        "project": row["project"],
        "projectId": row["project_id"],
        "status": row["status"],
        "cycle": row["cycle"],
        "estimate": row["estimate"],
        "label": row["label"],
        "description": row["description"],
    }


def _with_revision(row: dict[str, Any]) -> dict[str, Any]:
    return {**row["data"], "revision": row["revision"]}


def filter_to_scopes(
    data: dict[str, list[dict[str, Any]]], scope_ids: frozenset[str]
) -> dict[str, list[dict[str, Any]]]:
    """Only the records inside these workspaces; the same rule `load` applies."""
    return _filter_to_scopes(data, scope_ids)


def _filter_to_scopes(
    data: dict[str, list[dict[str, Any]]], scope_ids: frozenset[str]
) -> dict[str, list[dict[str, Any]]]:
    scopes = [scope for scope in data["workspaceScopes"] if scope["id"] in scope_ids]
    project_ids = {project_id for scope in scopes for project_id in scope["allowedProjectIds"]}
    issue_projects = {name for scope in scopes for name in scope["allowedIssueProjects"]}
    return {
        "workspaceScopes": scopes,
        "projects": [project for project in data["projects"] if project["id"] in project_ids],
        "team": [member for member in data["team"] if project_ids.intersection(member["projectIds"])],
        "cycles": [cycle for cycle in data["cycles"] if cycle["projectId"] in project_ids],
        "issues": [
            issue for issue in data["issues"]
            if (issue["projectId"] in project_ids if issue["projectId"] else issue["project"] in issue_projects)
        ],
    }


def _check_references(
    connection, kind: str, record: dict[str, Any], previous_issue: dict[str, Any] | None = None
) -> None:
    """Reject records pointing at projects or people that do not exist or are out of scope.

    Historical assignees may remain on tickets only while both the assignee and
    workspace membership are unchanged. Moving a ticket revalidates ownership.
    """
    project_id = record.get("projectId")
    if kind in ("issue", "cycle") and project_id and not connection.execute(
        "select 1 from demo_projects where id = ?", (project_id,)
    ).fetchone():
        raise InvalidReference(f"Project {project_id} does not exist.")
    if kind != "issue":
        return

    workspace_project_ids = _workspace_project_ids(connection, project_id, record["project"])
    if previous_issue and record["assignee"] == previous_issue["assignee"]:
        previous_workspace_project_ids = _workspace_project_ids(
            connection, previous_issue.get("projectId"), previous_issue["project"]
        )
        if workspace_project_ids == previous_workspace_project_ids:
            return

    assignee = record["assignee"]
    member = connection.execute(
        "select project_ids from demo_team_members where name = ?", (assignee,)
    ).fetchone()
    if member is None:
        raise InvalidReference(f"{assignee} is not a member of this team.")
    if not workspace_project_ids.intersection(json.loads(member["project_ids"])):
        raise InvalidReference(f"{assignee} does not work in this ticket's workspace.")


def _workspace_project_ids(connection, project_id: str | None, issue_project: str) -> set[str]:
    """Project IDs of every workspace containing the issue, matched by ID when it has one."""
    project_ids: set[str] = set()
    for row in connection.execute(
        "select allowed_project_ids, allowed_issue_projects from demo_workspace_scopes"
    ).fetchall():
        scope_project_ids = json.loads(row["allowed_project_ids"])
        contains = (
            project_id in scope_project_ids
            if project_id
            else issue_project in json.loads(row["allowed_issue_projects"])
        )
        if contains:
            project_ids.update(scope_project_ids)
    return project_ids


def _add_project_to_scope(connection, workspace_scope_id: str, project: dict[str, Any]) -> None:
    row = connection.execute(
        "select allowed_project_ids, allowed_issue_projects from demo_workspace_scopes where id = ?",
        (workspace_scope_id,),
    ).fetchone()
    if not row:
        return

    project_ids = json.loads(row["allowed_project_ids"])
    issue_projects = json.loads(row["allowed_issue_projects"])
    if project["id"] not in project_ids:
        project_ids.append(project["id"])
    if project["name"] not in issue_projects:
        issue_projects.append(project["name"])

    connection.execute(
        """
        update demo_workspace_scopes
        set allowed_project_ids = ?, allowed_issue_projects = ?
        where id = ?
        """,
        (json.dumps(project_ids), json.dumps(issue_projects), workspace_scope_id),
    )
