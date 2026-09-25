"""An administrator adds and removes people, and names their organization."""
from __future__ import annotations

from test_added_product import AddedProductFixture, TENANT, PRODUCT
from app.auth import create_token
from app.db import get_connection


class OrganizationPeopleTest(AddedProductFixture):
    def setUp(self):
        super().setUp()
        self.add_product()

    def as_user(self, user_id: str) -> dict:
        return {"Authorization": f"Bearer {create_token(user_id, TENANT)}"}

    def add(self, email: str, role: str = "team_member", headers: dict | None = None):
        return self.client.post(f"/api/organizations/{TENANT}/people", headers=headers or self.headers,
                                json={"email": email, "role": role})

    def emails(self) -> set[str]:
        listed = self.client.get(f"/api/organizations/{TENANT}/members", headers=self.headers).json()
        return {member["email"] for member in listed["members"] if member["email"]}

    def test_someone_added_is_listed_and_can_work_in_the_products(self):
        added = self.add("  Nora@Example.test ")
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(added.json()["email"], "nora@example.test")
        self.assertIn("nora@example.test", self.emails())
        records = self.client.get(f"/api/products/{PRODUCT}/records?scope=primary",
                                  headers=self.as_user(added.json()["user_id"]))
        self.assertEqual(records.status_code, 200, records.text)

    def test_signing_in_with_that_address_lands_in_this_organization(self):
        added = self.add("kai@example.test").json()
        with get_connection() as connection:
            account = connection.execute(
                "select user_id, tenant_id from email_accounts where email = ?", ("kai@example.test",)
            ).fetchone()
        self.assertEqual((account["user_id"], account["tenant_id"]), (added["user_id"], TENANT))

    def test_an_address_with_an_account_or_no_address_at_all_is_refused(self):
        self.assertEqual(self.add("ivy@example.test").status_code, 201)
        again = self.add("IVY@example.test")
        self.assertEqual(again.status_code, 409)
        self.assertIn("already has a Pixel account", again.json()["detail"])
        self.assertEqual(self.add("not an email").status_code, 422)

    def test_only_an_administrator_of_this_organization_may_add_people(self):
        member = self.add("sam@example.test").json()
        refused = self.add("lee@example.test", headers=self.as_user(member["user_id"]))
        self.assertEqual(refused.status_code, 403)
        elsewhere = self.client.post("/api/organizations/acme/people", headers=self.headers,
                                     json={"email": "x@example.test"})
        self.assertEqual(elsewhere.status_code, 404)

    def test_removing_someone_ends_their_access_at_once(self):
        added = self.add("rem@example.test").json()
        theirs = self.as_user(added["user_id"])
        self.assertEqual(self.client.get("/api/account/session", headers=theirs).status_code, 200)
        removed = self.client.delete(f"/api/organizations/{TENANT}/people/{added['user_id']}",
                                     headers=self.headers)
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertNotIn("rem@example.test", self.emails())
        self.assertNotEqual(self.client.get("/api/account/session", headers=theirs).status_code, 200)

    def test_an_administrator_cannot_remove_themselves(self):
        refused = self.client.delete(f"/api/organizations/{TENANT}/people/demo-admin", headers=self.headers)
        self.assertEqual(refused.status_code, 409)

    def test_a_product_added_later_is_open_to_everyone_already_here(self):
        added = self.add("late@example.test").json()
        with get_connection() as connection:
            connection.execute("delete from record_grants where user_id = ?", (added["user_id"],))
        from app.organization_api import grant_product_to_everyone
        grant_product_to_everyone(TENANT, PRODUCT)
        records = self.client.get(f"/api/products/{PRODUCT}/records?scope=primary",
                                  headers=self.as_user(added["user_id"]))
        self.assertEqual(records.status_code, 200, records.text)

    def test_the_organization_can_be_renamed(self):
        renamed = self.client.patch(f"/api/organizations/{TENANT}", headers=self.headers,
                                    json={"name": "  Northwind   Labs "})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "Northwind Labs")
        self.assertEqual(self.directory.organization(TENANT).name, "Northwind Labs")
        self.assertEqual(self.client.patch(f"/api/organizations/{TENANT}", headers=self.headers,
                                           json={"name": "   "}).status_code, 422)
