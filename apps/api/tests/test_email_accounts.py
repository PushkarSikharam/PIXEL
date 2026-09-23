"""Real sign-in never depends on a publicly selectable synthetic identity."""
import os
import unittest
import yaml
from unittest.mock import Mock, patch
from urllib import error as url_error

from test_engine_cutover import EngineCutoverFixture
from app import account_api, db
from library_fixtures import library_definition
from app.definitions.authoring import store_definition
from app.definitions.loader import DefinitionError


class EmailDeliveryTransportTest(unittest.TestCase):
    def test_resend_host_uses_https_api_not_smtp(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.status = 200
        with patch.object(account_api.url_request, "urlopen", return_value=response) as opened, \
                patch.object(account_api.smtplib, "SMTP_SSL") as smtp:
            account_api.send_code("owner@example.test", "12345678", (
                "secret", "smtp.resend.com", "resend", "re_test_key", "onboarding@resend.dev",
            ))
        smtp.assert_not_called()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer re_test_key", request.headers["Authorization"])

    def test_resend_api_failure_is_treated_as_delivery_failure(self):
        with patch.object(account_api.url_request, "urlopen",
                          side_effect=url_error.URLError("timeout")):
            with self.assertRaises(OSError):
                account_api.send_code("owner@example.test", "12345678", (
                    "secret", "smtp.resend.com", "resend", "re_test_key", "onboarding@resend.dev",
                ))

    def test_non_resend_host_still_uses_smtp(self):
        smtp = Mock()
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=None)
        with patch.object(account_api.smtplib, "SMTP_SSL", return_value=smtp) as smtp_ssl, \
                patch.object(account_api.url_request, "urlopen") as opened:
            account_api.send_code("owner@example.test", "12345678", (
                "secret", "smtp.example.test", "user", "password", "noreply@example.test",
            ))
        opened.assert_not_called()
        smtp_ssl.assert_called_once()
        smtp.login.assert_called_once_with("user", "password")
        smtp.send_message.assert_called_once()


class EmailAccountsTest(EngineCutoverFixture):
    def setUp(self):
        super().setUp()
        enabled = patch.dict(os.environ, {
            "PIXEL_EMAIL_LOGIN_ENABLED": "true", "PIXEL_SELF_SIGNUP_ENABLED": "true",
            "PIXEL_ENGINE_MODE": "definition",
            "PIXEL_AUTH_SECRET": "test-only-stable-secret", "PIXEL_SMTP_HOST": "invalid.example",
            "PIXEL_SMTP_USER": "test", "PIXEL_SMTP_PASSWORD": "test", "PIXEL_EMAIL_FROM": "noreply@example.test",
        })
        enabled.start()
        self.addCleanup(enabled.stop)
        mail = patch.object(account_api, "send_code")
        self.mail = mail.start()
        self.addCleanup(mail.stop)

    def challenge(self, email="owner@example.test"):
        result = self.client.post("/api/account/email-code", json={"email": email})
        self.assertEqual(result.status_code, 200, result.text)
        return {"challenge_id": result.json()["challenge_id"], "code": self.mail.call_args.args[1]}

    def login(self, email="owner@example.test"):
        result = self.client.post("/api/account/verify-code", json=self.challenge(email))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_disabled_signin_does_not_send_mail(self):
        with patch.dict(os.environ, {"PIXEL_EMAIL_LOGIN_ENABLED": "false"}):
            self.assertEqual(self.client.post("/api/account/email-code", json={"email": "a@example.test"}).status_code, 503)
        self.mail.assert_not_called()

    def test_customer_login_requires_definition_authority(self):
        with patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "legacy"}):
            self.assertEqual(self.client.post("/api/account/email-code", json={"email": "a@example.test"}).status_code, 503)
        self.mail.assert_not_called()

    def test_accounts_are_private_and_persist_across_signins(self):
        first = self.login()
        second = self.login("other@example.test")
        again = self.login()
        self.assertNotEqual(first["tenant_id"], second["tenant_id"])
        self.assertEqual(first["tenant_id"], again["tenant_id"])
        headers = {"Authorization": "Bearer " + first["token"]}
        response = self.client.get("/api/account/session", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["teams"][0]["team_id"], "default")
        self.assertEqual(response.json()["role"], "org_admin")
        self.assertEqual(self.client.get(f'/api/organizations/{second["tenant_id"]}/products', headers=headers).status_code, 404)

    def test_code_is_single_use_and_not_stored_in_plaintext(self):
        challenge = self.challenge()
        with db.get_connection() as connection:
            row = connection.execute("select * from email_challenges").fetchone()
            self.assertNotIn(challenge["code"], str(dict(row)))
        self.assertEqual(self.client.post("/api/account/verify-code", json=challenge).status_code, 200)
        self.assertEqual(self.client.post("/api/account/verify-code", json=challenge).status_code, 401)

    def test_five_failed_attempts_survive_requests(self):
        challenge = self.challenge()
        wrong = {**challenge, "code": "00000000" if challenge["code"] != "00000000" else "11111111"}
        for _ in range(5):
            self.assertEqual(self.client.post("/api/account/verify-code", json=wrong).status_code, 401)
        self.assertEqual(self.client.post("/api/account/verify-code", json=challenge).status_code, 401)

    def test_expiry_and_resend_invalidate_codes(self):
        first = self.challenge()
        second = self.challenge()
        self.assertEqual(self.client.post("/api/account/verify-code", json=first).status_code, 401)
        with db.get_connection() as connection:
            connection.execute("update email_challenges set expires_at=0")
        self.assertEqual(self.client.post("/api/account/verify-code", json=second).status_code, 401)

    def test_limit_and_delivery_failure_are_fail_closed(self):
        self.mail.side_effect = OSError("private transport error")
        for _ in range(5):
            result = self.client.post("/api/account/email-code", json={"email": "owner@example.test"})
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private", result.text)
        self.assertEqual(self.client.post("/api/account/email-code", json={"email": "owner@example.test"}).status_code, 429)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from email_challenges where consumed=0").fetchone()[0], 0)

    def test_logout_revokes_only_current_session(self):
        first, second = self.login(), self.login()
        headers = {"Authorization": "Bearer " + first["token"]}
        self.assertEqual(self.client.post("/api/account/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/api/account/session", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/account/session", headers={"Authorization": "Bearer " + second["token"]}).status_code, 200)

    def test_signup_requires_explicit_enable(self):
        with patch.dict(os.environ, {"PIXEL_SELF_SIGNUP_ENABLED": "false"}):
            self.assertEqual(self.client.post("/api/account/verify-code", json=self.challenge()).status_code, 401)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from email_accounts").fetchone()[0], 0)

    def test_verified_owner_can_add_a_private_product_and_answer_from_its_sources(self):
        account = self.login()
        headers = {"Authorization": "Bearer " + account["token"]}
        definition = library_definition()
        definition["definition"].update(ownership="organization_private", owner_organization=account["tenant_id"])
        response = self.client.post(f'/api/organizations/{account["tenant_id"]}/products', headers=headers, json={
            "product_id": "library", "team_id": "default", "definition_id": "sample_library",
            "definition": yaml.safe_dump(definition),
        })
        self.assertEqual(response.status_code, 201, response.text)
        source = self.client.post("/api/products/library/knowledge", headers=headers, json={
            "title": "Loan policy", "text": "A loan lasts fourteen days.", "approved": True,
        })
        self.assertEqual(source.status_code, 201, source.text)
        with patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "definition"}), patch("app.main._authority", "definition"):
            answer = self.client.post("/api/turn", headers=headers, json={
                "session_id": "private-conversation", "turn_id": 1, "product_id": "library",
                "message": "how long does a loan last", "workspace_scope_id": "primary",
            })
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("fourteen days", answer.json()["speech"])
        self.assertIsNone(answer.json()["execution"])

    def test_upload_cannot_publish_shared_or_another_organizations_definition(self):
        account = self.login()
        headers = {"Authorization": "Bearer " + account["token"]}
        for metadata in ({"ownership": "platform_shared"}, {"ownership": "organization_private", "owner_organization": "someone-else"}):
            definition = library_definition()
            definition["definition"].update(metadata)
            response = self.client.post(f'/api/organizations/{account["tenant_id"]}/products', headers=headers, json={
                "product_id": "library", "team_id": "default", "definition_id": "sample_library",
                "definition": yaml.safe_dump(definition),
            })
            self.assertEqual(response.status_code, 403, response.text)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from definition_sources where definition_id='sample_library'").fetchone()[0], 0)

    def test_source_owner_cannot_be_changed_before_registration(self):
        definition = library_definition()
        definition["definition"].update(ownership="organization_private", owner_organization="owner-a")
        store_definition(yaml.safe_dump(definition), definition_id="sample_library", version=1)
        definition["definition"].update(owner_organization="owner-b", version=2)
        with self.assertRaises(DefinitionError):
            store_definition(yaml.safe_dump(definition), definition_id="sample_library", version=2)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from definition_sources where version=2").fetchone()[0], 0)

    def test_installed_definition_namespace_is_reserved(self):
        definition = library_definition()
        definition["definition"].update(definition_id="linear_simplified", version=99)
        with self.assertRaises(DefinitionError):
            store_definition(yaml.safe_dump(definition), definition_id="linear_simplified", version=99)

class SignInLimitsTest(EmailAccountsTest):
    """Abuse limits must not become a way to lock other people out."""

    def request_code(self, email: str):
        return self.client.post("/api/account/email-code", json={"email": email})

    def test_one_address_is_limited_without_limiting_anybody_else(self):
        for _ in range(account_api.ADDRESS_CODES_PER_HOUR):
            self.assertEqual(self.request_code("busy@example.test").status_code, 200)
        exhausted = self.request_code("busy@example.test")
        self.assertEqual(exhausted.status_code, 429, exhausted.text)
        # Everybody else is unaffected, which is the whole point.
        self.assertEqual(self.request_code("somebody@example.test").status_code, 200)

    def test_the_deployment_ceiling_is_far_above_one_persons_share(self):
        """A ceiling one attacker could exhaust would lock every customer out of signing in."""
        self.assertGreaterEqual(account_api.DEPLOYMENT_CODES_PER_HOUR,
                                account_api.ADDRESS_CODES_PER_HOUR * 100)

    def test_a_session_survives_its_organization_being_readable(self):
        session = self.login()
        answered = self.client.get("/api/account/session", headers={
            "Authorization": f"Bearer {session['token']}"})
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertIn("console_product_id", answered.json())

    def test_a_session_does_not_advertise_an_unbound_console_product(self):
        session = self.login()
        with patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": "pixel_console"}):
            answered = self.client.get("/api/account/session", headers={
                "Authorization": f"Bearer {session['token']}"})
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertIsNone(answered.json()["console_product_id"])
