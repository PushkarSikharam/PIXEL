"""Moving around the application itself, by asking.

Somebody using Pixel is moving around a place, and asking to be taken somewhere is a request like
any other. These tests hold that to the same rules everything else follows: it is answered by the
engine from a definition, it belongs to one organization, and it never claims to do something the
application cannot.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_cutover import EngineCutoverFixture  # noqa: E402

from app import main  # noqa: E402
from app.auth import create_token  # noqa: E402
from app.definitions.console import configured_console, ensure_console_product  # noqa: E402
from app.definitions.organizations import OrganizationDirectory  # noqa: E402
from app.definitions.registry import DefinitionRegistry  # noqa: E402

TENANT = "pixel-dev"
CONSOLE_DEFINITION = "pixel_console"


class ConsoleAssistantFixture(EngineCutoverFixture):
    def setUp(self):
        super().setUp()
        for patcher in (unittest.mock.patch.dict(os.environ, {
                            "PIXEL_ENGINE_MODE": "definition",
                            "PIXEL_CONSOLE_DEFINITION": CONSOLE_DEFINITION}),
                        unittest.mock.patch.object(main, "_authority", "definition")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.turns: dict[str, int] = {}
        self.directory = OrganizationDirectory(DefinitionRegistry())
        self.console = ensure_console_product(self.directory, TENANT, "planning-team",
                                              ("demo-admin",))

    def ask(self, message: str, session: str = "console") -> dict:
        self.turns[session] = self.turns.get(session, 0) + 1
        response = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={"session_id": session, "turn_id": self.turns[session],
                 "product_id": self.console.product_id, "message": message,
                 "workspace_scope_id": "primary"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def action(body: dict) -> str | None:
        return (body.get("validated_action") or {}).get("type")


class MovingAroundTest(ConsoleAssistantFixture):
    def test_somebody_can_ask_to_be_taken_where_they_want_to_go(self):
        for message, expected in (
            ("take me to my products", "OPEN_PRODUCTS"),
            ("open members", "OPEN_MEMBERS"),
            ("show me the audit log", "OPEN_AUDIT"),
            ("go to settings", "OPEN_SETTINGS"),
            ("take me home", "OPEN_OVERVIEW"),
            ("open deployments", "OPEN_DEPLOY"),
        ):
            with self.subTest(message=message):
                self.assertEqual(self.action(self.ask(message, message[:8])), expected)

    def test_asking_to_leave_where_you_are_takes_you_out(self):
        """Somebody inside a product says "take me out of this" and means it literally."""
        for message in ("take me out of this", "back to products", "leave this product"):
            with self.subTest(message=message):
                self.assertEqual(self.action(self.ask(message, message[:9])), "OPEN_PRODUCTS")

    def test_it_says_what_it_can_do_and_nothing_it_cannot(self):
        offered = self.ask("what can you do")["speech"]
        self.assertIn("Pixel", offered)
        for record_work in ("create", "change", "delete"):
            self.assertNotIn(record_work, offered.lower(),
                             "moving around is all it does; it must not offer record work")

    def test_it_never_answers_about_a_product_it_does_not_hold(self):
        answered = self.ask("how many deals are there")
        self.assertIsNone(self.action(answered))
        self.assertNotIn("deal", answered["speech"].lower())

    def test_pixel_itself_is_not_listed_among_somebody_s_products(self):
        """Opening the application inside the application is not something to offer."""
        listed = self.client.get(f"/api/organizations/{TENANT}/products", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}"}).json()["products"]
        self.assertNotIn(self.console.product_id, [p["product_id"] for p in listed])
        self.assertIn("linear-demo", [p["product_id"] for p in listed])

    def test_it_refuses_destruction_like_everything_else(self):
        self.assertEqual(self.ask("delete everything")["status"], "denied")


class StageNameTest(unittest.TestCase):
    def test_the_response_contract_and_the_engine_agree_on_what_a_fallback_is(self):
        """`app.main` reads the stage without importing the engine, so the two names must match."""
        from app.engine.conversation_engine import TurnStage
        from app.schemas import FALLBACK_STAGE

        self.assertEqual(FALLBACK_STAGE, str(TurnStage.FALLBACK))


class WhoGetsOneTest(ConsoleAssistantFixture):
    def test_a_deployment_that_chose_none_has_none(self):
        with unittest.mock.patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": ""}):
            self.assertIsNone(configured_console())
            self.assertIsNone(ensure_console_product(self.directory, "other-org", "team"))

    def test_giving_it_twice_changes_nothing(self):
        again = ensure_console_product(self.directory, TENANT, "planning-team", ("demo-admin",))
        self.assertEqual(again, self.console)
        bound = self.directory.product(TENANT, self.console.product_id)
        self.assertEqual(bound.definition_id, CONSOLE_DEFINITION)

    def test_it_belongs_to_the_organization_that_was_given_it(self):
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        theirs = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('their-admin', 'somebody-else')}",
        }, json={"session_id": "theirs", "turn_id": 1, "product_id": self.console.product_id,
                 "message": "take me to my products", "workspace_scope_id": "primary"}).json()
        self.assertEqual(theirs["status"], "denied")

    def test_a_version_an_operator_moved_it_to_is_left_alone(self):
        """Re-running setup must never undo a deliberate version change."""
        bound = self.directory.product(TENANT, self.console.product_id)
        ensure_console_product(self.directory, TENANT, "planning-team", ("demo-admin",))
        self.assertEqual(self.directory.product(TENANT, self.console.product_id).definition_version,
                         bound.definition_version)


if __name__ == "__main__":
    unittest.main()
