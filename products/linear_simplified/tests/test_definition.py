"""The Linear demo is a valid platform-shared Product Definition, run by the seeded demo organization."""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.auth import create_token  # noqa: E402
from app import ops  # noqa: E402
from app.definitions.compatibility import ChangeClass, classify  # noqa: E402
from app.definitions.loader import DEFAULT_SOURCE, DefinitionSource, load_definition  # noqa: E402
from app.definitions.organizations import OrganizationDirectory  # noqa: E402
from app.definitions.registry import DefinitionRegistry  # noqa: E402
from app.definitions.vocabulary import Capability  # noqa: E402
from app.main import app  # noqa: E402
from app.services import env as env_module  # noqa: E402

DEFINITION_ID = "linear_simplified"
# The version the demo seed binds (the product's current version) and the next, unpublished one.
SEED = REPO_ROOT / "products" / DEFINITION_ID / "seed" / "demo_organization.json"
CURRENT = json.loads(SEED.read_text(encoding="utf-8"))["product"]["definition_version"]
NEXT = CURRENT + 1
# The seeded development organization and its product running this definition.
TENANT_ID, PRODUCT_ID = "pixel-dev", "linear-demo"

# Every action the current web app and backend know, and the definition action expressing it.
LEGACY_ACTIONS = {
    "OPEN_DASHBOARD": ("open_dashboard", Capability.NAVIGATE_VIEW),
    "OPEN_ISSUES": ("open_issues", Capability.NAVIGATE_VIEW),
    "OPEN_PROJECTS": ("open_projects", Capability.NAVIGATE_VIEW),
    "OPEN_CYCLES": ("open_cycles", Capability.NAVIGATE_VIEW),
    "OPEN_TEAMS": ("open_teams", Capability.NAVIGATE_VIEW),
    "OPEN_INTEGRATIONS": ("open_integrations", Capability.NAVIGATE_VIEW),
    "OPEN_SYSTEM_ARCHITECTURE": ("open_architecture", Capability.NAVIGATE_VIEW),
    "OPEN_DEMO_ISSUE": ("open_issue", Capability.OPEN_RECORD),
    "CREATE_DEMO_ISSUE": ("create_issue", Capability.CREATE_RECORD),
    "CREATE_DEMO_TEAM_MEMBER": ("create_member", Capability.CREATE_RECORD),
    "UPDATE_DEMO_ISSUE": ("update_issue", Capability.UPDATE_RECORD),
    "FILTER_ISSUES_BY_ASSIGNEE": ("issues_by_assignee", Capability.FILTER_RECORDS),
    "HIGHLIGHT_ASSIGNMENT_CONTROL": ("highlight_assignment", Capability.HIGHLIGHT_CONTROL),
    "HIGHLIGHT_CREATE_TICKET_BUTTON": ("highlight_create_issue", Capability.HIGHLIGHT_CONTROL),
    "HIGHLIGHT_ADD_MEMBER_BUTTON": ("highlight_add_member", Capability.HIGHLIGHT_CONTROL),
    "HIGHLIGHT_CYCLE_PROGRESS": ("highlight_cycle_progress", Capability.HIGHLIGHT_CONTROL),
    "OPEN_GITHUB_SETUP": ("open_github_setup", Capability.HIGHLIGHT_CONTROL),
    "HIGHLIGHT_GITHUB_CARD": ("highlight_github", Capability.HIGHLIGHT_CONTROL),
    "HIGHLIGHT_SLACK_CARD": ("highlight_slack", Capability.HIGHLIGHT_CONTROL),
}


class LinearDefinitionTest(unittest.TestCase):
    VERSION = 1

    def setUp(self):
        self.definition = load_definition(DEFAULT_SOURCE, DEFINITION_ID, self.VERSION).definition

    def test_loads_and_validates(self):
        identity = self.definition.definition
        self.assertEqual((identity.definition_id, identity.ownership), (DEFINITION_ID, "platform_shared"))
        self.assertIsNone(identity.owner_organization)
        self.assertEqual(set(self.definition.entities), {"project", "cycle", "member", "issue"})

    def test_every_current_action_is_expressed(self):
        for legacy, (key, capability) in LEGACY_ACTIONS.items():
            with self.subTest(action=legacy):
                self.assertEqual(self.definition.actions[key].capability, capability)
        self.assertEqual(len(self.definition.actions), len(LEGACY_ACTIONS))

    def test_scope_is_anchored_on_projects_by_reference(self):
        self.assertEqual(self.definition.scope.anchor, "project")
        self.assertEqual(self.definition.scope.paths["issue"], ["project"])
        self.assertEqual(self.definition.scope.paths["member"], ["projects"])

    def test_people_are_references_not_names(self):
        self.assertEqual(self.definition.entities["issue"].fields["assignee"].target, "member")
        self.assertEqual(self.definition.entities["project"].fields["lead"].target, "member")

    def test_no_tenant_business_data_in_the_definition(self):
        text = DEFAULT_SOURCE.definition_path(DEFINITION_ID, self.VERSION).read_text(encoding="utf-8")
        for record_value in ("Maya", "Noah", "Avery", "Iris", "Sam Rivera", "LIN-142", "PRJ-10", "workspace-",
                             TENANT_ID, PRODUCT_ID, "planning-team"):
            with self.subTest(value=record_value):
                self.assertNotIn(record_value, text)


class LinearDefinitionV2Test(LinearDefinitionTest):
    """5a plan, section 7: v2 changes routing only and carries no reply text."""

    VERSION = 2

    def test_v2_is_additive_over_v1(self):
        v1 = load_definition(DEFAULT_SOURCE, DEFINITION_ID, 1).definition
        self.assertEqual(classify(v1, self.definition).change, ChangeClass.ADDITIVE_COMPATIBLE)
        self.assertEqual(self.definition.entities, v1.entities)

    def test_v2_carries_no_reply_text(self):
        self.assertEqual(self.definition.responses, {})

    def test_v2_file_is_byte_stable(self):
        raw = DEFAULT_SOURCE.definition_path(DEFINITION_ID, 2).read_bytes()
        self.assertNotIn(b"\r\n", raw, "definitions are LF on every checkout")

    def test_the_reviewed_routing_changes(self):
        self.assertFalse(self.definition.actions["highlight_assignment"].record)
        self.assertIn("broad_scope", {rule.topic for rule in self.definition.guardrails})
        self.assertNotIn("broad_scope_refused", {rule.response for rule in self.definition.clarifications})
        add_member = [intent.requires for intent in self.definition.intents if intent.action == "highlight_add_member"]
        self.assertEqual(sorted(map(tuple, add_member)), [(), ("unknown_person",), ("unknown_person",)])


class CurrentDefinitionTest(unittest.TestCase):
    """5c plan, section 7.2: the version new sessions pin."""

    def test_a_new_tickets_project_is_asked_for_never_defaulted(self):
        issue = load_definition(DEFAULT_SOURCE, DEFINITION_ID, CURRENT).definition.entities["issue"]
        self.assertIsNone(issue.fields["project"].default)
        self.assertEqual((issue.fields["priority"].default, issue.fields["status"].default), ("Medium", "Todo"))

    def test_every_published_file_is_byte_stable(self):
        for version in range(1, CURRENT + 1):
            with self.subTest(version=version):
                raw = DEFAULT_SOURCE.definition_path(DEFINITION_ID, version).read_bytes()
                self.assertNotIn(b"\r\n", raw, "definitions are LF on every checkout")


class SeededOrganizationFixture(unittest.TestCase):
    """A fresh database with the demo organization seeded, and the live turn endpoint."""

    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env_patch = patch.dict(os.environ, {"LLM_ENABLED": "false", "PIXEL_BLOCK_EXTERNAL_HTTP": "true",
                                            "PIXEL_DEMO_SEEDS": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "linear.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        self.client = TestClient(app, raise_server_exceptions=False)
        self.headers = {"Authorization": f"Bearer {create_token('demo-admin')}"}
        self.registry = DefinitionRegistry()
        self.directory = OrganizationDirectory(self.registry)

    def turn(self, session_id: str, turn_id: int = 1, message: str = "Show me the issues"):
        return self.client.post("/api/turn", headers=self.headers, json={
            "session_id": session_id, "turn_id": turn_id, "product_id": PRODUCT_ID, "message": message,
        }).json()


class LinearDemoOrganizationTest(SeededOrganizationFixture):
    """The seed package creates the demo organization, and its chat turns run on pinned sessions."""

    def test_the_seeded_product_runs_its_current_version(self):
        binding = self.directory.product(TENANT_ID, PRODUCT_ID)
        self.assertEqual(
            (binding.team_id, binding.definition_id, binding.definition_version, binding.state),
            ("planning-team", DEFINITION_ID, CURRENT, "active"),
        )
        # Earlier versions are published first, so each was classified against its predecessor
        # and every earlier version is there to roll back to.
        versions = range(1, CURRENT + 1)
        self.assertEqual([self.registry.get(DEFINITION_ID, version).state for version in versions],
                         ["published"] * CURRENT)
        self.assertEqual(self.directory.membership(TENANT_ID, "demo-admin").role, "org_admin")
        for user_id in ("demo-product-eng", "demo-platform"):
            with self.subTest(user_id=user_id):
                self.assertEqual(self.directory.membership(TENANT_ID, user_id).team_id, "planning-team")

    def test_chat_turns_pin_their_session(self):
        self.assertEqual(self.turn("pinned")["status"], "completed")
        with db.get_connection() as connection:
            row = connection.execute(
                "select tenant_id, team_id, product_id, definition_id, definition_version, definition_checksum "
                "from sessions where id = 'pinned'"
            ).fetchone()
        self.assertEqual(
            (row["tenant_id"], row["team_id"], row["product_id"], row["definition_id"], row["definition_version"]),
            (TENANT_ID, "planning-team", PRODUCT_ID, DEFINITION_ID, CURRENT),
        )
        self.assertEqual(row["definition_checksum"], self.registry.get(DEFINITION_ID, CURRENT).checksum)

    def test_revoking_the_version_ends_live_conversations(self):
        self.assertEqual(self.turn("live")["status"], "completed")
        self.registry.revoke(DEFINITION_ID, CURRENT)
        ended = self.turn("live", turn_id=2)
        self.assertEqual(ended["status"], "denied")
        self.assertIn("definition_revoked", ended["intent_trace"]["reason"])
        self.assertEqual(self.turn("new")["status"], "denied")

    def test_disabling_the_product_stops_its_pixel(self):
        self.assertEqual(self.turn("live")["status"], "completed")
        self.directory.set_product_state(TENANT_ID, PRODUCT_ID, "disabled")
        self.assertIn("product_disabled", self.turn("live", turn_id=2)["intent_trace"]["reason"])


@dataclass(frozen=True)
class _Problem:
    tenant_id: str
    product_id: str
    reason: str


class MoveProductVersionTest(SeededOrganizationFixture):
    """5a plan, section 7: moving the product between versions is explicit and reversible."""

    def move(self, version: int) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = ops.main(["move-product-version", "--tenant", TENANT_ID, "--product", PRODUCT_ID,
                             "--version", str(version)])
        return code, json.loads(output.getvalue())

    def pinned_version(self, session_id: str) -> int:
        with db.get_connection() as connection:
            return connection.execute(
                "select definition_version from sessions where id = ?", (session_id,)
            ).fetchone()["definition_version"]

    def binding_version(self) -> int:
        return self.directory.product(TENANT_ID, PRODUCT_ID).definition_version

    def test_moving_back_and_forth_keeps_open_sessions_on_their_version(self):
        code, result = self.move(1)
        self.assertEqual((code, result["moved"], result["from_version"], result["to_version"]), (0, True, CURRENT, 1))
        self.assertEqual(self.turn("on-v1")["status"], "completed")
        self.assertEqual(self.move(CURRENT)[0], 0)
        self.assertEqual(self.turn("on-current")["status"], "completed")
        # The session opened on v1 keeps its pin and keeps working after the move.
        self.assertEqual(self.turn("on-v1", turn_id=2)["status"], "completed")
        self.assertEqual((self.pinned_version("on-v1"), self.pinned_version("on-current")), (1, CURRENT))
        self.assertEqual(result["definition_checksum"], self.registry.get(DEFINITION_ID, 1).checksum)

    def test_a_version_without_a_file_is_refused_and_nothing_moves(self):
        code, result = self.move(9)
        self.assertEqual((code, result["moved"]), (1, False))
        self.assertEqual(self.binding_version(), CURRENT)
        self.assertIsNone(self.registry.get(DEFINITION_ID, 9))

    def test_a_breaking_version_is_refused_and_nothing_moves(self):
        with tempfile.TemporaryDirectory() as root:
            definitions = Path(root) / DEFINITION_ID / "definition"
            definitions.mkdir(parents=True)
            source_dir = REPO_ROOT / "products" / DEFINITION_ID / "definition"
            for version in range(1, CURRENT + 1):
                name = f"v{version}.yaml"
                (definitions / name).write_bytes((source_dir / name).read_bytes())
            breaking = (source_dir / f"v{CURRENT}.yaml").read_bytes().decode("utf-8")
            breaking = breaking.replace(f"  version: {CURRENT}\n", f"  version: {NEXT}\n", 1)
            breaking = breaking.replace("      estimate: { type: text, max: 30 }\n", "", 1)
            (definitions / f"v{NEXT}.yaml").write_bytes(breaking.encode("utf-8"))
            registry = DefinitionRegistry(DefinitionSource(products_root=Path(root)))
            with patch.object(ops, "OrganizationDirectory", lambda: OrganizationDirectory(registry)):
                code, result = self.move(NEXT)
        self.assertEqual((code, result["moved"]), (1, False))
        self.assertIn("issue.estimate was removed", result["reason"])
        self.assertEqual(self.binding_version(), CURRENT)
        refused = self.registry.get(DEFINITION_ID, NEXT)
        self.assertTrue(refused is None or refused.state != "published")

    def test_failed_readiness_moves_the_binding_back(self):
        problem = _Problem(TENANT_ID, PRODUCT_ID, "definition_checksum_mismatch")
        with patch.object(ops, "session_start_problems", lambda: [problem]):
            code, result = self.move(1)
        self.assertEqual((code, result["moved"], result["to_version"]), (1, False, CURRENT))
        self.assertEqual(self.binding_version(), CURRENT)
        self.assertEqual(self.turn("still-current")["status"], "completed")
        self.assertEqual(self.pinned_version("still-current"), CURRENT)

    def test_an_unknown_product_is_refused(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = ops.main(["move-product-version", "--tenant", TENANT_ID, "--product", "nothing-here",
                             "--version", "2"])
        self.assertEqual((code, json.loads(output.getvalue())["moved"]), (1, False))


if __name__ == "__main__":
    unittest.main()
