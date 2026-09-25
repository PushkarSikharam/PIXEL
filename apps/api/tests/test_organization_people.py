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


class OrganizationTeamsTest(AddedProductFixture):
    """An administrator makes teams, puts people in them and chooses which team runs a product."""

    def setUp(self):
        super().setUp()
        self.add_product()

    def as_user(self, user_id: str) -> dict:
        return {"Authorization": f"Bearer {create_token(user_id, TENANT)}"}

    def new_team(self, name: str, headers: dict | None = None):
        return self.client.post(f"/api/organizations/{TENANT}/teams", headers=headers or self.headers,
                                json={"name": name})

    def teams(self) -> dict[str, dict]:
        listed = self.client.get(f"/api/organizations/{TENANT}/teams", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        return {team["name"]: team for team in listed.json()["teams"]}

    def records(self, user_id: str) -> int:
        return self.client.get(f"/api/products/{PRODUCT}/records?scope=primary",
                               headers=self.as_user(user_id)).status_code

    def test_a_team_is_made_listed_and_named_only_once(self):
        made = self.new_team("  Mobile   Apps ")
        self.assertEqual(made.status_code, 201, made.text)
        self.assertEqual((made.json()["team_id"], made.json()["name"]), ("mobile-apps", "Mobile Apps"))
        self.assertEqual(self.teams()["Mobile Apps"]["people"], 0)
        self.assertEqual(self.new_team("mobile apps").status_code, 409)
        self.assertEqual(self.new_team("   ").status_code, 422)

    def test_the_team_list_says_who_works_there_and_what_it_runs(self):
        self.client.post(f"/api/organizations/{TENANT}/people", headers=self.headers,
                         json={"email": "a@example.test"})
        home = [team for team in self.teams().values() if PRODUCT in team["products"]]
        self.assertEqual(len(home), 1)
        self.assertGreaterEqual(home[0]["people"], 1)

    def test_somebody_can_be_added_straight_into_a_team(self):
        team = self.new_team("Design").json()["team_id"]
        added = self.client.post(f"/api/organizations/{TENANT}/people", headers=self.headers,
                                 json={"email": "d@example.test", "team_id": team})
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(self.directory.membership(TENANT, added.json()["user_id"]).team_id, team)
        missing = self.client.post(f"/api/organizations/{TENANT}/people", headers=self.headers,
                                   json={"email": "e@example.test", "team_id": "no-such-team"})
        self.assertEqual(missing.status_code, 404)

    def test_moving_a_person_changes_which_products_they_can_open(self):
        added = self.client.post(f"/api/organizations/{TENANT}/people", headers=self.headers,
                                 json={"email": "m@example.test"}).json()
        self.assertEqual(self.records(added["user_id"]), 200)
        design = self.new_team("Design").json()["team_id"]
        moved = self.client.patch(f"/api/organizations/{TENANT}/people/{added['user_id']}",
                                  headers=self.headers, json={"role": "team_member", "team_id": design})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertNotEqual(self.records(added["user_id"]), 200)
        # Moving the product to their team brings it back to them.
        product = self.client.put(f"/api/organizations/{TENANT}/products/{PRODUCT}/team",
                                  headers=self.headers, json={"team_id": design})
        self.assertEqual(product.status_code, 200, product.text)
        self.assertEqual(self.records(added["user_id"]), 200)
        self.assertEqual(self.teams()["Design"]["products"], [PRODUCT])

    def test_making_someone_an_admin_and_back_resets_their_reach(self):
        added = self.client.post(f"/api/organizations/{TENANT}/people", headers=self.headers,
                                 json={"email": "r@example.test"}).json()
        up = self.client.patch(f"/api/organizations/{TENANT}/people/{added['user_id']}",
                               headers=self.headers, json={"role": "org_admin"})
        self.assertEqual(up.status_code, 200, up.text)
        self.assertIsNone(self.directory.membership(TENANT, added["user_id"]).team_id)
        down = self.client.patch(f"/api/organizations/{TENANT}/people/{added['user_id']}",
                                 headers=self.headers, json={"role": "team_member"})
        self.assertEqual(down.status_code, 200, down.text)
        with get_connection() as connection:
            grant = connection.execute("select is_admin from record_grants where user_id = ? and product_id = ?",
                                       (added["user_id"], PRODUCT)).fetchone()
        self.assertEqual(grant["is_admin"], 0)

    def test_only_an_administrator_changes_teams_and_never_their_own_role(self):
        member = self.client.post(f"/api/organizations/{TENANT}/people", headers=self.headers,
                                  json={"email": "n@example.test"}).json()
        theirs = self.as_user(member["user_id"])
        self.assertEqual(self.new_team("Ops", headers=theirs).status_code, 403)
        self.assertEqual(self.client.put(f"/api/organizations/{TENANT}/products/{PRODUCT}/team", headers=theirs,
                                         json={"team_id": "planning-team"}).status_code, 403)
        self_change = self.client.patch(f"/api/organizations/{TENANT}/people/demo-admin",
                                        headers=self.headers, json={"role": "team_member"})
        self.assertEqual(self_change.status_code, 409)
        self.assertEqual(self.client.put(f"/api/organizations/{TENANT}/products/nothing-here/team",
                                         headers=self.headers, json={"team_id": "planning-team"}).status_code, 404)
