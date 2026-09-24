"""Milestone 3.1a: regressions for isolation defects found after the first 3.1 sign-off.

Each class reproduces one reviewed defect exactly, plus the positive cases that must keep working:
- legacy record endpoints crossed organization and product boundaries;
- definition ownership could change through out-of-order or concurrent registration;
- speech ignored the definition lifecycle and invalid sessions, and could spend before refusing.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from app import db, main
from app.auth import AuthUser, create_token, create_visitor_token
from app.definitions.access import authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import RegistryError
from app.definitions.sessions import pin_new_session
from app.services.session_manager import SessionManager
from app.services.speech_providers import SynthesizedSpeech
from app.services.speech_service import SpeechService
from definition_fixtures import DEFINITION_ID, sample_definition
from test_definition_registry import RegistryFixture
from test_usage_ledger import LedgerFixture

DEMO_TENANT, DEMO_PRODUCT, DEMO_DEFINITION = "pixel-dev", "linear-demo", "linear_simplified"


def bearer(user_id: str, tenant_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(user_id, tenant_id)}"}


class ApiFixture(LedgerFixture):
    """Seeded demo organization, a second organization, and an HTTP client."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.directory = OrganizationDirectory()
        self.directory.create_organization("acme", "Acme")
        self.directory.create_team("acme", "acme-team", "Acme Team")


class RecordAccessTest(ApiFixture):
    """Legacy demo records belong to the designated demo product only."""

    def test_reproduction_member_of_another_organization_cannot_read_or_reset(self):
        # The reviewed reproduction: demo-admin joins another organization as an ordinary member.
        self.directory.add_member("acme", "demo-admin", "team_member", "acme-team")
        headers = bearer("demo-admin", "acme")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/demo-data/issues", headers=headers, json=_issue()).status_code, 403)

    def test_demo_organization_members_need_access_to_the_demo_product(self):
        self.directory.create_team(DEMO_TENANT, "other-team", "Other")
        self.directory.add_member(DEMO_TENANT, "stray", "team_member", "other-team")
        headers = bearer("stray")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 403)

    def test_disabled_products_and_teams_close_their_records(self):
        headers = bearer("demo-product-eng")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 200)
        self.directory.set_product_state(DEMO_TENANT, DEMO_PRODUCT, "disabled")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 403)
        self.directory.set_product_state(DEMO_TENANT, DEMO_PRODUCT, "active")
        self.directory.set_team_state(DEMO_TENANT, "planning-team", "disabled")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 403)

    def test_product_access_without_a_record_grant_is_refused(self):
        # An organization admin may use the product, but holds no grant on its records.
        self.directory.add_member(DEMO_TENANT, "olga", "org_admin")
        headers = bearer("olga")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 403)

    def test_reset_requires_the_record_administration_grant(self):
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=bearer("demo-product-eng")).status_code, 403)
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=bearer("demo-admin")).status_code, 200)

    def test_granted_members_keep_their_scoped_view(self):
        engineering = self.client.get("/api/demo-data", headers=bearer("demo-product-eng")).json()
        everything = self.client.get("/api/demo-data", headers=bearer("demo-admin")).json()
        self.assertEqual({scope["id"] for scope in engineering["workspaceScopes"]}, {"workspace-product-eng"})
        self.assertGreater(len(everything["issues"]), len(engineering["issues"]))

    def test_turns_require_a_record_grant_for_the_requested_product(self):
        self.directory.add_member("acme", "demo-product-eng", "team_member", "acme-team")
        self.directory.bind_product("acme", "acme-demo", "acme-team", DEMO_DEFINITION, 1)
        response = self.client.post("/api/turn", headers=bearer("demo-product-eng", "acme"), json={
            "session_id": "acme-1", "turn_id": 1, "product_id": "acme-demo", "message": "Open issues",
        })
        self.assertEqual(response.status_code, 403)
        own = self.client.post("/api/turn", headers=bearer("demo-product-eng", DEMO_TENANT), json={
            "session_id": "own-1", "turn_id": 1, "product_id": DEMO_PRODUCT, "message": "Open issues",
        })
        self.assertEqual(own.json()["status"], "completed")

    def test_records_are_closed_without_a_designated_owner(self):
        with db.get_connection() as connection:
            connection.execute("delete from legacy_record_owner")
        self.assertEqual(self.client.get("/api/demo-data", headers=bearer("demo-admin")).status_code, 403)

    def test_visitors_have_only_their_private_record_instance(self):
        token = create_visitor_token(DEMO_TENANT, DEMO_PRODUCT)[0]
        headers = {"Authorization": f"Bearer {token}"}
        private = self.client.get("/api/demo-data", headers=headers)
        self.assertEqual(private.status_code, 200)
        self.assertTrue(private.json()["issues"])
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 403)


class DefinitionOwnershipInvariantTest(RegistryFixture):
    """Ownership belongs to the definition identity, fixed at first registration."""

    def write_private(self, version: int):
        self.files.write(sample_definition(version=version, owner_organization="acme"), version)

    def test_reproduction_descending_publication_cannot_change_ownership(self):
        self.write_private(2)
        self.registry.register(DEFINITION_ID, 2)
        self.registry.validate(DEFINITION_ID, 2)
        self.registry.publish(DEFINITION_ID, 2)
        self.files.write(sample_definition(version=1), 1)
        with self.assertRaises(RegistryError) as rejected:
            self.registry.register(DEFINITION_ID, 1)
        self.assertIn("ownership", str(rejected.exception))
        self.assertIsNone(self.registry.get(DEFINITION_ID, 1))

    def test_conflicting_drafts_cannot_be_registered(self):
        self.files.write(sample_definition(version=1), 1)
        self.registry.register(DEFINITION_ID, 1)
        self.write_private(2)
        with self.assertRaises(RegistryError):
            self.registry.register(DEFINITION_ID, 2)
        self.assertIsNone(self.registry.get(DEFINITION_ID, 2))

    def test_private_owner_is_part_of_the_identity(self):
        self.write_private(1)
        self.registry.register(DEFINITION_ID, 1)
        other = sample_definition(version=2, owner_organization="beta")
        self.files.write(other, 2)
        with self.assertRaises(RegistryError):
            self.registry.register(DEFINITION_ID, 2)

    def test_concurrent_conflicting_registrations_admit_exactly_one_ownership(self):
        self.files.write(sample_definition(version=1), 1)
        self.write_private(2)
        start = threading.Barrier(2)
        outcomes: dict[int, str] = {}

        def register(version: int):
            start.wait()
            try:
                self.registry.register(DEFINITION_ID, version)
                outcomes[version] = "registered"
            except RegistryError:
                outcomes[version] = "rejected"

        threads = [threading.Thread(target=register, args=(version,)) for version in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes.values()), ["registered", "rejected"])
        registered = [self.registry.get(DEFINITION_ID, version) for version in (1, 2)]
        self.assertEqual(len([version for version in registered if version is not None]), 1)

    def test_publication_rechecks_the_identity(self):
        self.files.write(sample_definition(version=1), 1)
        self.registry.register(DEFINITION_ID, 1)
        self.registry.validate(DEFINITION_ID, 1)
        # A version row that disagrees with the identity (for example, written by an older
        # build) must never be published.
        with db.get_connection() as connection:
            connection.execute(
                "update definition_versions set ownership = 'organization_private', owner_tenant_id = 'acme' "
                "where definition_id = ? and version = 1",
                (DEFINITION_ID,),
            )
        with self.assertRaises(RegistryError):
            self.registry.publish(DEFINITION_ID, 1)
        self.assertEqual(self.registry.get(DEFINITION_ID, 1).state, "validated")

    def test_existing_registrations_receive_an_identity_on_migration(self):
        self.write_private(1)
        self.registry.register(DEFINITION_ID, 1)
        with db.get_connection() as connection:
            connection.execute("delete from definitions")
        db.migrate()
        self.files.write(sample_definition(version=2), 2)
        with self.assertRaises(RegistryError):
            self.registry.register(DEFINITION_ID, 2)

    def test_versions_with_the_same_ownership_still_publish_in_any_order(self):
        for version in (2, 1):
            self.files.write(sample_definition(version=version), version)
            self.registry.register(DEFINITION_ID, version)
            self.registry.validate(DEFINITION_ID, version)
        self.assertEqual(self.registry.publish(DEFINITION_ID, 2).state, "published")
        self.assertEqual(self.registry.publish(DEFINITION_ID, 1).state, "published")


class FakeSpeech:
    name, model = "fake", "fake-model"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(self, text: str) -> SynthesizedSpeech:
        self.calls.append(text)
        return SynthesizedSpeech(b"ID3-fake", "audio/mpeg", "fake-speech")


class SpeechLifecycleTest(ApiFixture):
    """Speech is refused before any reservation or dispatch unless the product and session are valid."""

    def setUp(self):
        super().setUp()
        self.provider = FakeSpeech()
        service = SpeechService(self.ledger, providers=lambda tenant, style: [self.provider])
        patcher = patch.object(main, "speech_service", service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = SessionManager()
        self.registry = self.directory.definitions
        self.admin = AuthUser(kind="member", user_id="demo-admin", tenant_id=DEMO_TENANT, role="org_admin")

    def speak(self, session_id=None, user_id="demo-product-eng", product_id=DEMO_PRODUCT, headers=None):
        body = {"text": "Hello there", "product_id": product_id}
        if session_id:
            body["session_id"] = session_id
        return self.client.post("/api/speech", json=body, headers=headers or bearer(user_id))

    def start_session(self, session_id, user_id="demo-product-eng", product_id=DEMO_PRODUCT, now=None):
        pin = pin_new_session(authorize_product(self.admin, product_id, self.directory), self.registry, now=now)
        self.sessions.ensure_session(session_id, product_id, user_id=user_id, tenant_id=DEMO_TENANT, pin=pin)

    def bound_demo_version(self) -> int:
        product = self.directory.product(DEMO_TENANT, DEMO_PRODUCT)
        self.assertIsNotNone(product, "demo product must be seeded")
        return product.definition_version

    def assert_refused_before_spending(self, response, status: int, reason: str | None = None):
        self.assertEqual(response.status_code, status, response.text)
        if reason:
            self.assertEqual(response.json()["reason"], reason)
        self.assertEqual(self.provider.calls, [], "no provider dispatch")
        self.assertEqual(self.rows(), [], "no budget reservation")

    # --- Reproduction ---

    def test_reproduction_revoked_definition_refuses_sessionless_speech(self):
        self.registry.revoke(DEMO_DEFINITION, self.bound_demo_version())
        self.assert_refused_before_spending(self.speak(), 409, "definition_not_published")

    def test_revoked_definition_ends_pinned_speech(self):
        self.start_session("mine")
        self.registry.revoke(DEMO_DEFINITION, self.bound_demo_version())
        self.assert_refused_before_spending(self.speak("mine"), 409, "definition_revoked")

    # --- Supplied sessions must be valid, never silently ignored ---

    def test_other_users_session_is_rejected(self):
        self.start_session("theirs", user_id="demo-platform")
        self.assert_refused_before_spending(self.speak("theirs"), 404)

    def test_session_of_another_product_is_rejected(self):
        self.directory.create_team(DEMO_TENANT, "other-team", "Other")
        self.directory.bind_product(DEMO_TENANT, "other-desk", "other-team", DEMO_DEFINITION, 1)
        self.start_session("elsewhere", product_id="other-desk")
        self.assert_refused_before_spending(self.speak("elsewhere"), 404)

    def test_unknown_or_unpinned_sessions_are_rejected(self):
        self.assert_refused_before_spending(self.speak("never-started"), 404)
        self.sessions.ensure_session("unpinned", DEMO_PRODUCT, user_id="demo-product-eng", tenant_id=DEMO_TENANT)
        self.assert_refused_before_spending(self.speak("unpinned"), 409, "session_not_pinned")

    def test_expired_sessions_are_rejected(self):
        self.start_session("old", now=datetime.now(UTC) - timedelta(days=2))
        self.assert_refused_before_spending(self.speak("old"), 409, "session_expired")

    def test_disabled_product_refuses_pinned_and_sessionless_speech(self):
        self.start_session("mine")
        self.directory.set_product_state(DEMO_TENANT, DEMO_PRODUCT, "disabled")
        self.assert_refused_before_spending(self.speak("mine"), 404)
        self.assert_refused_before_spending(self.speak(), 404)

    def test_changed_definition_content_refuses_speech(self):
        self.start_session("mine")
        with patch("app.definitions.sessions.file_checksum", lambda *args: "tampered"):
            self.assert_refused_before_spending(self.speak("mine"), 409, "definition_changed")

    # --- Legitimate pinning keeps working ---

    def test_retired_version_still_serves_its_pinned_session(self):
        self.start_session("mine")
        self.registry.retire(DEMO_DEFINITION, self.bound_demo_version())
        self.assert_refused_before_spending(self.speak(), 409, "definition_not_published")
        response = self.speak("mine")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.provider.calls, ["Hello there"])
        self.assertEqual([row["session_id"] for row in self.rows()], ["mine"])

    def test_authorized_speech_is_served_and_attributed(self):
        self.start_session("mine")
        self.assertEqual(self.speak("mine").status_code, 200)
        self.assertEqual(self.speak().status_code, 200)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual([row["session_id"] for row in self.rows()], ["mine", None])
        self.assertEqual({(row["tenant_id"], row["team_id"], row["product_id"]) for row in self.rows()},
                         {(DEMO_TENANT, "planning-team", DEMO_PRODUCT)})

    def test_visitor_speech_is_limited_to_its_own_sessions(self):
        token = create_visitor_token(DEMO_TENANT, DEMO_PRODUCT)[0]
        headers = {"Authorization": f"Bearer {token}"}
        self.start_session("member-session")
        self.assert_refused_before_spending(self.speak("member-session", headers=headers), 404)
        self.assertEqual(self.speak(headers=headers).status_code, 200)


def _issue() -> dict:
    return {
        "title": "Cross-organization write", "priority": "Medium", "assignee": "Maya Chen",
        "project": "Integrations", "projectId": "PRJ-10", "status": "Todo",
    }


if __name__ == "__main__":
    unittest.main()
