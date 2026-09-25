"""Real sign-in never depends on a publicly selectable synthetic identity."""
import os
import unittest
import yaml
from unittest.mock import Mock, patch
from urllib import error as url_error

from test_engine_cutover import EngineCutoverFixture
from app import account_api, db
from app.auth import CSRF_COOKIE, SESSION_COOKIE, create_token
from library_fixtures import library_definition
from app.definitions.authoring import store_definition
from app.definitions.loader import DefinitionError


class EmailDeliveryTransportTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        external_allowed = patch.dict(os.environ, {"PIXEL_BLOCK_EXTERNAL_HTTP": "false"})
        external_allowed.start()
        self.addCleanup(external_allowed.stop)

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
        self.assertEqual(request.headers["User-agent"], "Pixel/1.0")

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

    def test_the_mail_server_is_reached_the_way_the_library_expects(self):
        """A mock accepts any arguments, so what they mean is checked against the real signature.

        The second positional argument of `SMTP_SSL` is the port, not the timeout. Passing the
        timeout there connects to the wrong port - or fails outright when the port is also given
        by name - and no test that replaces `SMTP_SSL` with a mock can tell, because a mock is
        happy to be called any way at all.
        """
        import inspect
        import smtplib

        smtp = Mock()
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=None)
        with patch.object(account_api.smtplib, "SMTP_SSL", return_value=smtp) as smtp_ssl:
            account_api.send_code("owner@example.test", "12345678", (
                "secret", "smtp.example.test", "user", "password", "noreply@example.test",
            ))
        bound = inspect.signature(smtplib.SMTP_SSL.__init__).bind(
            None, *smtp_ssl.call_args.args, **smtp_ssl.call_args.kwargs).arguments
        self.assertEqual(bound["host"], "smtp.example.test")
        self.assertEqual(bound["port"], 465)
        self.assertEqual(bound["timeout"], account_api.SMTP_TIMEOUT_SECONDS)


class EmailAccountsTest(EngineCutoverFixture):
    def setUp(self):
        super().setUp()
        enabled = patch.dict(os.environ, {
            "PIXEL_EMAIL_LOGIN_ENABLED": "true", "PIXEL_SELF_SIGNUP_ENABLED": "true",
            "PIXEL_ENGINE_MODE": "definition",
            "PIXEL_SECURE_COOKIES": "false",
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

    def bearer(self, session: dict) -> dict[str, str]:
        return {"Authorization": "Bearer " + create_token(session["user_id"], session["tenant_id"])}

    def csrf(self, session: dict) -> dict[str, str]:
        return {"X-Pixel-CSRF": session["csrf_token"]}

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
        headers = self.bearer(first)
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

    def test_a_delivery_failure_says_nothing_and_leaves_no_usable_challenge(self):
        """The transport's own words never reach the caller, and a code that was not delivered
        can never be used. The person's allowance survives it: a mail server we cannot reach is
        our failure, not theirs, and locking them out for it would be the worse outcome. The
        deployment ceiling still counts every attempt, so failures stay bounded overall."""
        self.mail.side_effect = OSError("private transport error")
        for _ in range(6):
            result = self.client.post("/api/account/email-code", json={"email": "owner@example.test"})
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private", result.text)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute(
                "select count(*) from email_challenges where consumed=0").fetchone()[0], 0)
            charged = connection.execute(
                "select attempts from email_login_limits where bucket = ?",
                (account_api.DEPLOYMENT_BUCKET,)).fetchone()
            self.assertGreaterEqual(charged[0], 6, "the deployment ceiling still counts them")
        self.mail.side_effect = None
        self.assertEqual(self.client.post("/api/account/email-code",
                                          json={"email": "owner@example.test"}).status_code, 200)

    def test_logout_revokes_only_current_session(self):
        first, second = self.login(), self.login()
        headers = self.bearer(first)
        second_headers = self.bearer(second)
        self.assertEqual(self.client.post("/api/account/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/api/account/session", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/account/session", headers=second_headers).status_code, 200)

    def test_browser_login_uses_httponly_cookie_and_csrf_not_a_script_token(self):
        session = self.login()
        self.assertNotIn("token", session)
        self.assertRegex(session["csrf_token"], r"^[A-Za-z0-9_-]{30,}$")
        cookie_names = {cookie.name for cookie in self.client.cookies.jar}
        self.assertIn(SESSION_COOKIE, cookie_names)
        self.assertIn(CSRF_COOKIE, cookie_names)
        self.assertEqual(self.client.get("/api/account/session").status_code, 200)
        self.assertEqual(self.client.post("/api/account/logout").status_code, 403)
        self.assertEqual(self.client.post("/api/account/logout", headers=self.csrf(session)).status_code, 204)
        self.assertEqual(self.client.get("/api/account/session").status_code, 401)

    # --- Staying signed in -------------------------------------------------------------------
    #
    # A session lives in the cookie, so every one of these is really the same question asked in
    # the ways a person meets it: a second tab, a reloaded page, a browser opened the next day.
    # They used to differ because the browser decided who was signed in from something one tab
    # happened to hold in memory.

    def test_a_second_tab_is_signed_in_and_can_write(self):
        """A new tab shares the cookie and nothing else. Reading is not enough: it must write."""
        session = self.login()
        # A tab that kept nothing of its own, which is what a newly opened one has.
        self.assertEqual(self.client.get("/api/account/session").status_code, 200)
        written = self.client.post("/api/account/logout", headers=self.csrf(session))
        self.assertEqual(written.status_code, 204, written.text)

    def test_the_token_a_write_needs_is_readable_by_the_page(self):
        """The page is not under /api, so a cookie scoped there would be invisible to it."""
        self.login()
        csrf = next(cookie for cookie in self.client.cookies.jar if cookie.name == CSRF_COOKIE)
        session = next(cookie for cookie in self.client.cookies.jar if cookie.name == SESSION_COOKIE)
        self.assertEqual(csrf.path, "/")
        # And the one that authenticates stays out of the page's reach entirely.
        self.assertEqual(session.path, "/api")

    def test_a_session_outlives_a_day(self):
        """Closing the browser in the evening is not a reason to sign in again."""
        from app.account_api import SESSION_COOKIE_MAX_AGE
        from app.auth import _TOKEN_MAX_AGE_SECONDS

        self.assertGreater(SESSION_COOKIE_MAX_AGE, 86400)
        self.assertGreaterEqual(_TOKEN_MAX_AGE_SECONDS, SESSION_COOKIE_MAX_AGE)

    def test_signing_out_ends_it_everywhere_and_no_tab_can_write_after(self):
        """Every tab shares one cookie, so signing out in one has to end it for all of them.

        The check is the session a tab is actually holding, not a freshly minted one: a new token
        is a different session and revoking one has never touched another.
        """
        session = self.login()
        held = next(cookie.value for cookie in self.client.cookies.jar if cookie.name == SESSION_COOKIE)
        self.assertEqual(self.client.post("/api/account/logout", headers=self.csrf(session)).status_code, 204)
        self.assertEqual(self.client.get("/api/account/session").status_code, 401)
        # The other tab, still sending the cookie it had before anyone signed out.
        self.client.cookies.set(SESSION_COOKIE, held)
        self.client.cookies.set(CSRF_COOKIE, session["csrf_token"])
        stale = self.client.get("/api/account/session")
        self.assertEqual(stale.status_code, 401, stale.text)
        refused = self.client.post("/api/account/logout", headers=self.csrf(session))
        self.assertEqual(refused.status_code, 401, refused.text)

    def test_a_write_without_the_token_is_still_refused(self):
        """What the cookie does not do. The header must still match, or this is not protection."""
        self.login()
        self.assertEqual(self.client.post("/api/account/logout").status_code, 403)
        self.assertEqual(
            self.client.post("/api/account/logout", headers={"X-Pixel-CSRF": "not-the-token"}).status_code,
            403,
        )

    def test_signing_in_brings_the_console_assistant_forward(self):
        """Not only at registration: an account signing in gets the version we ship today."""
        from app.definitions.console import configured_console
        from app.definitions.organizations import OrganizationDirectory

        with patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": "pixel_console"}):
            session = self.login("newcomer@example.test")
            directory = OrganizationDirectory()
            console = configured_console()
            bound = directory.product(session["tenant_id"], console.product_id)
            self.assertIsNotNone(bound, "signing in should give an organization the assistant")
            newest = max(directory.definitions.source.versions(console.definition_id))
            directory.definitions.ensure_published(console.definition_id, 1)
            directory.move_product_version(session["tenant_id"], console.product_id, 1)
            self.login("newcomer@example.test")
            self.assertEqual(
                directory.product(session["tenant_id"], console.product_id).definition_version,
                newest,
            )

    def test_signup_requires_explicit_enable(self):
        """Still off by default, and now it says so instead of blaming the code.

        The refusal was a 401 reading "the code is invalid, expired, or this account has no
        access", which sent somebody whose code was perfectly right to look for a fault in the
        code. It is a 403 saying this Pixel is invite-only. Telling them that discloses nothing:
        reaching this point at all means they read a code we mailed to that address, so they hold
        that inbox, and the answer says nothing about anybody else's.
        """
        with patch.dict(os.environ, {"PIXEL_SELF_SIGNUP_ENABLED": "false"}):
            refused = self.client.post("/api/account/verify-code", json=self.challenge())
            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertIn("invite-only", refused.json()["detail"])
            self.assertNotIn("invalid", refused.json()["detail"])
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from email_accounts").fetchone()[0], 0)

    def test_a_wrong_code_is_still_just_a_wrong_code(self):
        """The other half of that split: nothing about accounts is said to somebody who has not
        proved they hold the address."""
        challenge = self.challenge()
        wrong = self.client.post("/api/account/verify-code",
                                 json={"challenge_id": challenge["challenge_id"], "code": "00000000"})
        self.assertEqual(wrong.status_code, 401, wrong.text)
        self.assertNotIn("invite", wrong.json()["detail"].lower())

    def test_with_signup_open_any_address_gets_its_own_workspace(self):
        """Open sign-up: a stranger's first code creates their organization, and only theirs."""
        mine = self.login("founder@example.test")
        theirs = self.login("someone-else@example.test")
        self.assertNotEqual(mine["tenant_id"], theirs["tenant_id"])
        self.assertNotEqual(mine["user_id"], theirs["user_id"])
        products = self.client.get(f"/api/organizations/{mine['tenant_id']}/products",
                                   headers=self.bearer(theirs))
        self.assertEqual(products.status_code, 404, "one workspace must not see another")

    def test_verified_owner_can_add_a_private_product_and_answer_from_its_sources(self):
        account = self.login()
        headers = self.bearer(account)
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
        headers = self.bearer(account)
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

    def test_a_code_that_never_arrived_is_not_charged_to_the_person(self):
        """A failing mail server must not lock somebody out of their own account."""
        self.mail.side_effect = OSError("smtp is down")
        for _ in range(account_api.ADDRESS_CODES_PER_HOUR + 2):
            self.assertEqual(self.request_code("unlucky@example.test").status_code, 503)
        self.mail.side_effect = None
        self.assertEqual(self.request_code("unlucky@example.test").status_code, 200,
                         "the allowance survives a delivery failure")

    def test_the_deployment_ceiling_is_far_above_one_persons_share(self):
        """A ceiling one attacker could exhaust would lock every customer out of signing in."""
        self.assertGreaterEqual(account_api.DEPLOYMENT_CODES_PER_HOUR,
                                account_api.ADDRESS_CODES_PER_HOUR * 100)

    def test_a_session_survives_its_organization_being_readable(self):
        session = self.login()
        answered = self.client.get("/api/account/session", headers={
            **self.bearer(session)})
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertIn("console_product_id", answered.json())

    def test_a_session_does_not_advertise_an_unbound_console_product(self):
        session = self.login()
        with patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": "pixel_console"}):
            answered = self.client.get("/api/account/session", headers={
                **self.bearer(session)})
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertIsNone(answered.json()["console_product_id"])
