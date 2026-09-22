"""Milestone 3.2, Slice 5a: shadow engine tests (plan sections 6.1-6.7).

Engine behaviour runs on the neutral sample-desk definition, so no database or product is
involved. Everything that needs the live engine runs on a temporary database seeded with the demo
organization, never on a developer's local database.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402
from app.auth import create_token, product_record_grant  # noqa: E402
from app.engine.conversation import CapabilityPolicy  # noqa: E402
from app.engine.conversation_engine import ConversationEngine, TurnStage  # noqa: E402
from app.engine.memory import ConversationMemory  # noqa: E402
from app.engine.router import IntentRouter, TurnContext  # noqa: E402
from app.engine.signals import HISTORY_LIMIT, EngineSignal, SignalExtractor, SignalHistory  # noqa: E402
from app.installed_products import package_for  # noqa: E402
from app.schemas import IntentTrace, TurnRequest, TurnResponse  # noqa: E402
from app.services import env as env_module  # noqa: E402
from app.services import shadow_parity  # noqa: E402
from app.services.agent import DemoAgent  # noqa: E402
from app.services.product_data_store import ProductDataStore  # noqa: E402
from app.services.shadow import (  # noqa: E402
    LOCK_STRIPES,
    ShadowController,
    ShadowEntry,
    ShadowKey,
    ShadowMemoryStore,
    ShadowRunner,
)
from app.services.shadow_parity import (  # noqa: E402
    BEHAVIOUR,
    COMPARED,
    GATED,
    LIFECYCLE,
    MATCH,
    MEMORY_RESET,
    NOT_COMPARED,
    OVER_BUDGET,
    TURN,
    ParityCounters,
    ShadowView,
    compare,
)
from engine_fixtures import InMemoryLookup, SampleDesk, load_engine_definition  # noqa: E402
from products.linear_simplified.tests.golden_backend import (  # noqa: E402
    DEFAULT_WORKSPACE,
    HERMETIC_ENV,
    PRINCIPAL,
    PRODUCT_ID,
    load_cases,
)

ALL = CapabilityPolicy(translatable=lambda key: True, permitted=lambda key: True)
# Values that differ between any two runs whatever the shadow does: times and generated IDs.
_VOLATILE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ][\d:.]+.*|[0-9a-f]{32}|[0-9a-f-]{36})$")
# Every table the live turn writes, and those the shadow must never touch.
LIVE_TABLES = (
    "sessions", "messages", "conversation_owners", "action_executions", "provider_attempts",
    "signals", "visitor_context", "demo_issues", "demo_projects", "demo_team_members",
    "demo_cycles", "demo_instances", "demo_instance_records", "demo_instance_receipts",
    "shadow_parity_daily",
)


def sample_engine() -> ConversationEngine:
    lookup = InMemoryLookup(SampleDesk(), frozenset({"ACC-1"}))
    return ConversationEngine(load_engine_definition(), lookup, ALL, definition_version=1)


class Conversation:
    """Sequential engine turns sharing memory and history, as the shadow store keeps them."""

    def __init__(self, engine: ConversationEngine) -> None:
        self.engine = engine
        self.memory = ConversationMemory()
        self.history = SignalHistory()
        self.turn = 0

    def say(self, message: str):
        self.turn += 1
        result = self.engine.turn(message, self.memory, self.history, TurnContext(turn=self.turn))
        self.memory, self.history = result.memory, result.history
        return result


class HermeticDatabase:
    """A temporary, seeded database; nothing here reads a developer's local files."""

    def __init__(self, extra_env: dict[str, str] | None = None) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self._patches = [
            patch.dict(os.environ, {**HERMETIC_ENV, **(extra_env or {})}),
            patch.object(env_module, "_env_files", lambda: ()),
            patch.object(db, "DB_PATH", Path(self._directory.name) / "shadow.sqlite3"),
        ]

    def __enter__(self) -> "HermeticDatabase":
        for item in self._patches:
            item.start()
        db.migrate()
        self.agent = DemoAgent()
        self.runner = ShadowRunner(self.agent.directory, self.agent.sessions.pin_for, package_for)
        self.grant = product_record_grant(PRINCIPAL, PRODUCT_ID)
        return self

    def __exit__(self, *exc) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._directory.cleanup()

    def request(self, session_id: str, turn_id: int, message: str) -> TurnRequest:
        return TurnRequest(session_id=session_id, turn_id=turn_id, product_id=PRODUCT_ID, message=message,
                           current_page="dashboard", workspace_scope_id=DEFAULT_WORKSPACE)

    def turn(self, request: TurnRequest, *, shadow: bool = True):
        visible = ProductDataStore().load(self.grant.visible_scope_ids())
        prepared = self.runner.prepare(PRINCIPAL, self.grant, PRODUCT_ID, visible, request.workspace_scope_id) \
            if shadow else None
        response = self.agent.handle_turn(request, PRINCIPAL, visible)
        result = self.runner.complete(prepared, PRINCIPAL, request, response) if shadow else None
        return response, result


def table_digests() -> dict[str, tuple[int, str]]:
    digests: dict[str, tuple[int, str]] = {}
    with db.get_connection() as connection:
        existing = {row["name"] for row in connection.execute("select name from sqlite_master where type = 'table'")}
        for table in LIVE_TABLES:
            if table not in existing:
                continue
            rows = connection.execute(f"select * from {table}").fetchall()
            normalized = sorted(
                json.dumps([("~" if isinstance(v, str) and _VOLATILE.match(v) else v) for v in tuple(row)],
                           default=str)
                for row in rows
            )
            digests[table] = (len(rows), hashlib.sha256("\n".join(normalized).encode()).hexdigest())
    return digests


class ShadowEngineContractTest(unittest.TestCase):
    """Plan section 6.1: no capability that could call a provider, write or execute."""

    def test_engine_takes_no_connection_ledger_or_transport(self):
        params = set(inspect.signature(ConversationEngine.__init__).parameters) - {"self"}
        self.assertEqual(params, {"definition", "snapshot", "capability_policy", "knowledge",
                                  "definition_version", "translate"})
        forbidden = ("connection", "ledger", "transport", "speech", "model", "http", "client")
        self.assertFalse([name for name in params if any(word in name for word in forbidden)])
        turn_params = set(inspect.signature(ConversationEngine.turn).parameters) - {"self"}
        self.assertEqual(turn_params, {"message", "memory", "history", "context"})

    def test_shadow_modules_reach_no_provider_speech_model_or_execution_code(self):
        code = (
            "import sys\n"
            "import app.services.shadow\n"
            "forbidden = ('http_client', 'speech_service', 'speech_providers', 'agent_reasoner', "
            "'model_turn', 'app.engine.execution', 'app.services.agent')\n"
            "loaded = sorted(m for m in sys.modules if any(f in m for f in forbidden))\n"
            "assert not loaded, loaded\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(API_DIR), str(REPO_ROOT)]),
               "PIXEL_IGNORE_ENV_FILES": "true"}
        result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ShadowInertnessTest(unittest.TestCase):
    """Plan section 6.2: an identical live response and identical tables, shadow on or off."""

    def run_golden(self, shadow: bool) -> tuple[list[dict], dict]:
        responses: list[dict] = []
        with HermeticDatabase({"PIXEL_SHADOW_ENGINE": "on" if shadow else "off"}) as hermetic:
            for case in load_cases():
                ProductDataStore().reset()
                for index, message in enumerate(case["turns"]):
                    request = hermetic.request(f"golden-{case['id']}", index + 1, message)
                    request = request.model_copy(update={"workspace_scope_id": case.get("workspace", DEFAULT_WORKSPACE)})
                    response, result = hermetic.turn(request, shadow=shadow)
                    if shadow:
                        self.assertIn(result.turn_class, {COMPARED, OVER_BUDGET}, (case["id"], index))
                    responses.append(response.model_dump(mode="json"))
            return responses, table_digests()

    def test_live_responses_and_tables_are_identical_with_the_shadow_on(self):
        off_responses, off_tables = self.run_golden(shadow=False)
        on_responses, on_tables = self.run_golden(shadow=True)
        self.assertEqual(json.dumps(off_responses, sort_keys=True), json.dumps(on_responses, sort_keys=True))
        self.assertEqual(off_tables, on_tables)
        # The shadow never reached a provider or an execution key.
        for table in ("provider_attempts", "action_executions"):
            if table in on_tables:
                self.assertEqual(on_tables[table][0], off_tables[table][0], table)

    def test_complete_performs_no_database_writes(self):
        with HermeticDatabase() as hermetic:
            request = hermetic.request("no-write", 1, "Show me the issues")
            visible = ProductDataStore().load(hermetic.grant.visible_scope_ids())
            prepared = hermetic.runner.prepare(PRINCIPAL, hermetic.grant, PRODUCT_ID, visible, DEFAULT_WORKSPACE)
            response = hermetic.agent.handle_turn(request, PRINCIPAL, visible)
            real_connect = sqlite3.connect
            attempts: list[int] = []

            def read_only(*args, **kwargs):
                connection = real_connect(*args, **kwargs)

                def authorizer(action, *_):
                    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                        attempts.append(action)
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(authorizer)
                return connection

            with patch.object(sqlite3, "connect", side_effect=read_only):
                result = hermetic.runner.complete(prepared, PRINCIPAL, request, response)
            self.assertIn(result.turn_class, {COMPARED, OVER_BUDGET})
            self.assertEqual(attempts, [])

    def test_an_exception_in_the_engine_leaves_the_live_turn_alone(self):
        with HermeticDatabase() as hermetic:
            request = hermetic.request("boom", 1, "Show me the issues")
            with patch.object(ConversationEngine, "turn", side_effect=RuntimeError("boom")):
                response, result = hermetic.turn(request)
            self.assertEqual(result.turn_class, "shadow_error")
            self.assertEqual(response.status, "completed")


class ShadowIsolationTest(unittest.TestCase):
    """Plan section 6.3: memory keys, instance resets and restarts."""

    def test_keys_differ_by_instance_generation_and_principal(self):
        base = ShadowKey("t1", "p1", "s1", "u1", "inst-1", 1)
        self.assertNotEqual(base, ShadowKey("t1", "p1", "s1", "u1", "inst-1", 2))
        self.assertNotEqual(base, ShadowKey("t1", "p1", "s1", "u1", "inst-2", 1))
        self.assertNotEqual(base, ShadowKey("t1", "p1", "s1", "u2", "inst-1", 1))
        self.assertNotEqual(base, ShadowKey("t1", "p1", "s1", "u1", None, 0), "member and visitor never share")

    def test_a_restart_resets_a_session_that_started_before_it(self):
        with HermeticDatabase() as hermetic:
            # Turn 1 is compared by the process that was running then.
            _, first = hermetic.turn(hermetic.request("restart", 1, "Show me the issues"))
            self.assertIn(first.turn_class, {COMPARED, OVER_BUDGET})
            # A new process starts later: its shadow never saw turn 1.
            hermetic.runner = ShadowRunner(hermetic.agent.directory, hermetic.agent.sessions.pin_for,
                                           package_for, epoch_started=datetime.now(UTC) + timedelta(seconds=1))
            _, second = hermetic.turn(hermetic.request("restart", 2, "what about Noah"))
            _, third = hermetic.turn(hermetic.request("restart", 3, "Show me the cycles"))
            self.assertEqual((second.turn_class, second.reason), (MEMORY_RESET, "restart"))
            self.assertEqual(third.turn_class, MEMORY_RESET)

    def test_a_session_that_starts_after_the_process_is_not_reset(self):
        with HermeticDatabase() as hermetic:
            hermetic.runner = ShadowRunner(hermetic.agent.directory, hermetic.agent.sessions.pin_for,
                                           package_for, epoch_started=datetime.now(UTC) - timedelta(seconds=1))
            _, first = hermetic.turn(hermetic.request("fresh", 1, "Show me the issues"))
            _, second = hermetic.turn(hermetic.request("fresh", 2, "what about Noah"))
            self.assertIn(first.turn_class, {COMPARED, OVER_BUDGET})
            self.assertIn(second.turn_class, {COMPARED, OVER_BUDGET})

    def test_turns_advance_the_entry_in_order_only(self):
        with HermeticDatabase() as hermetic:
            response, first = hermetic.turn(hermetic.request("order", 1, "Show me the issues"))
            self.assertIn(first.turn_class, {COMPARED, OVER_BUDGET})
            request = hermetic.request("order", 1, "Show me the issues")
            visible = ProductDataStore().load(hermetic.grant.visible_scope_ids())
            prepared = hermetic.runner.prepare(PRINCIPAL, hermetic.grant, PRODUCT_ID, visible, DEFAULT_WORKSPACE)
            replayed = hermetic.runner.complete(prepared, PRINCIPAL, request, response)
            self.assertEqual(replayed.turn_class, NOT_COMPARED, "an older or repeated turn never advances memory")


class ShadowCacheAndPinningTest(unittest.TestCase):
    """Plan section 6.4: a warm cache is gated every time; sessions run their own version."""

    def compare_once(self, hermetic: HermeticDatabase, session: str, turn_id: int, message: str):
        return hermetic.turn(hermetic.request(session, turn_id, message))

    def test_revocation_on_a_warm_cache_is_gated(self):
        with HermeticDatabase() as hermetic:
            _, warm = self.compare_once(hermetic, "gate", 1, "Show me the issues")
            self.assertIn(warm.turn_class, {COMPARED, OVER_BUDGET})
            self.assertEqual(len(hermetic.runner.cache), 1)
            # The seed binds v3, so the warm session is pinned to it.
            hermetic.agent.directory.definitions.revoke("linear_simplified", 3)
            request = hermetic.request("gate", 2, "Show me the cycles")
            visible = ProductDataStore().load(hermetic.grant.visible_scope_ids())
            prepared = hermetic.runner.prepare(PRINCIPAL, hermetic.grant, PRODUCT_ID, visible, DEFAULT_WORKSPACE)
            live = hermetic.agent.handle_turn(request, PRINCIPAL, visible).model_copy(update={"status": "completed"})
            self.assertEqual(hermetic.runner.complete(prepared, PRINCIPAL, request, live).turn_class, GATED)

    def test_disabling_the_product_is_gated(self):
        with HermeticDatabase() as hermetic:
            response, _ = self.compare_once(hermetic, "disabled", 1, "Show me the issues")
            hermetic.agent.directory.set_product_state(PRINCIPAL.tenant_id, PRODUCT_ID, "disabled")
            request = hermetic.request("disabled", 2, "Show me the cycles")
            visible = ProductDataStore().load(hermetic.grant.visible_scope_ids())
            prepared = hermetic.runner.prepare(PRINCIPAL, hermetic.grant, PRODUCT_ID, visible, DEFAULT_WORKSPACE)
            self.assertEqual(hermetic.runner.complete(prepared, PRINCIPAL, request, response).turn_class, GATED)

    def test_a_changed_definition_file_is_gated_before_any_work(self):
        with HermeticDatabase() as hermetic:
            response, _ = self.compare_once(hermetic, "changed", 1, "Show me the issues")
            request = hermetic.request("changed", 2, "Show me the cycles")
            visible = ProductDataStore().load(hermetic.grant.visible_scope_ids())
            prepared = hermetic.runner.prepare(PRINCIPAL, hermetic.grant, PRODUCT_ID, visible, DEFAULT_WORKSPACE)
            with patch("app.definitions.sessions.file_checksum", return_value="0" * 64), \
                    patch.object(ConversationEngine, "turn", side_effect=AssertionError("no work after a gate")):
                result = hermetic.runner.complete(prepared, PRINCIPAL, request, response)
            self.assertEqual(result.turn_class, GATED)

    def test_each_session_runs_on_its_pinned_version(self):
        with HermeticDatabase() as hermetic:
            directory = hermetic.agent.directory
            directory.move_product_version(PRINCIPAL.tenant_id, PRODUCT_ID, 1)
            _, on_v1 = self.compare_once(hermetic, "pinned-v1", 1, "Add a new member Priya Shah")
            directory.move_product_version(PRINCIPAL.tenant_id, PRODUCT_ID, 2)
            _, on_v2 = self.compare_once(hermetic, "pinned-v2", 1, "Add a new member Priya Shah")
            # v2 prefills the named person; v1 had no rule to do so.
            self.assertIsNone(on_v1.engine_turn.validated.action.prefill)
            self.assertEqual(dict(on_v2.engine_turn.validated.action.prefill), {"name": "Priya Shah"})
            self.assertEqual(len(hermetic.runner.cache), 2)


class ShadowEngineBehaviourTest(unittest.TestCase):
    """Plan section 6.5, on the neutral sample-desk definition."""

    def test_the_same_inputs_give_the_same_turn(self):
        engine = sample_engine()
        first = engine.turn("show me the contacts", ConversationMemory(), SignalHistory(), TurnContext(turn=1))
        second = engine.turn("show me the contacts", ConversationMemory(), SignalHistory(), TurnContext(turn=1))
        self.assertEqual(first, second)
        self.assertEqual(first.stage, TurnStage.PROPOSED)

    def test_signals_per_category_and_negatives(self):
        document = load_engine_definition().model_dump()
        document["knowledge_topics"] = {"contacts": ["contact", "contacts"], "mail": ["mail", "inbox"]}
        document["prospect_signals"] = {
            "roles": {"Support lead": ["support lead"]}, "current_tools": {"Helpdesk": ["helpdesk"]},
            "goals": {"Faster replies": ["reply faster"]}, "pain_points": {"Backlog": ["too many tickets"]},
        }
        definition = load_engine_definition(document=document)
        extractor = SignalExtractor(definition)
        normalizer = IntentRouter(definition, InMemoryLookup(SampleDesk(), frozenset({"ACC-1"}))).normalizer
        text = normalizer.normalize("I'm a support lead on Helpdesk, I want to reply faster, too many tickets")
        profile = extractor.profile(text)
        self.assertEqual((profile.role, profile.current_tool, profile.goal, profile.pain_point),
                         ("Support lead", "Helpdesk", "Faster replies", "Backlog"))
        self.assertEqual(extractor.signals(text, feature="contacts", person_name="Ben Okafor"), (
            EngineSignal("feature_interest", "contacts", 0.7),
            EngineSignal("pain_point", "backlog", 0.78),
            EngineSignal("person_interest", "Ben Okafor", 0.84),
        ))
        nothing = extractor.profile(normalizer.normalize("show me the contacts"))
        self.assertEqual((nothing.role, nothing.current_tool, nothing.goal, nothing.pain_point), (None,) * 4)
        # A negated topic is not interest: the corrected request decides.
        self.assertEqual(extractor.feature(normalizer.normalize("not mail, show me the contacts")), "contacts")

    def test_history_accumulates_is_capped_and_is_immutable(self):
        history = SignalHistory()
        for index in range(HISTORY_LIMIT + 10):
            history = history.add([EngineSignal("feature_interest", f"topic-{index}", 0.7)])
        self.assertEqual(len(history.seen), HISTORY_LIMIT)
        self.assertEqual(history.seen[0], ("feature_interest", "topic-10"))
        self.assertEqual(history.latest_value("feature_interest"), f"topic-{HISTORY_LIMIT + 9}")
        with self.assertRaises(Exception):
            history.seen = ()  # type: ignore[misc]

    def test_refusals_and_pending_questions_beat_conversation(self):
        chat = Conversation(sample_engine())
        self.assertEqual(chat.say("delete the contact, what can you do?").stage, TurnStage.REFUSED)
        asked = chat.say("create something new")
        self.assertEqual(asked.stage, TurnStage.CLARIFICATION)
        self.assertEqual(chat.say("What can you do?").stage, TurnStage.CLARIFICATION,
                         "a pending question is answered before any platform conversation")
        self.assertEqual(Conversation(sample_engine()).say("What can you do?").stage, TurnStage.ANSWER)

    def test_yes_would_execute_and_nothing_ever_executes(self):
        self.assertNotIn("executed", {stage.value for stage in TurnStage})
        chat = Conversation(sample_engine())
        chat.say("open Ben's contact")
        asked = chat.say("set the status to closed")
        self.assertEqual(asked.stage, TurnStage.AWAITING_CONFIRMATION)
        confirmed = chat.say("yes")
        self.assertEqual(confirmed.stage, TurnStage.WOULD_EXECUTE)
        self.assertNotIn("Updated", confirmed.reply.speech, "worded as the proposal, never as done")

    def test_a_named_greeting_after_hello(self):
        self.assertEqual(Conversation(sample_engine()).say("Hi, I'm Priya").reply.template_key, "greeting_named")


class ShadowSwitchAndFailureTest(unittest.TestCase):
    """Plan section 6.6: only "on" enters the shadow; stale and cancelled turns are not compared."""

    def test_only_on_enters_the_shadow(self):
        for value in (None, "", "off", "ON!", "true"):
            with self.subTest(value=value):
                env = {} if value is None else {"PIXEL_SHADOW_ENGINE": value}
                with HermeticDatabase(env) as hermetic:
                    if value is None:
                        os.environ.pop("PIXEL_SHADOW_ENGINE", None)
                    client = TestClient(main.app, raise_server_exceptions=True)
                    headers = {"Authorization": f"Bearer {create_token(PRINCIPAL.user_id)}"}
                    with patch("app.main.shadow_controller", side_effect=AssertionError("shadow entered")):
                        response = client.post("/api/turn", headers=headers, json={
                            "session_id": "switch", "turn_id": 1, "product_id": PRODUCT_ID,
                            "message": "Show me the issues", "workspace_scope_id": DEFAULT_WORKSPACE,
                        })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "completed")
                    del hermetic

    def test_on_enters_the_shadow_through_the_endpoint(self):
        with HermeticDatabase({"PIXEL_SHADOW_ENGINE": "on"}) as hermetic:
            controller = ShadowController(hermetic.runner)
            try:
                with patch("app.main.shadow_controller", return_value=controller):
                    client = TestClient(main.app)
                    headers = {"Authorization": f"Bearer {create_token(PRINCIPAL.user_id)}"}
                    response = client.post("/api/turn", headers=headers, json={
                        "session_id": "switch-on", "turn_id": 1, "product_id": PRODUCT_ID,
                        "message": "Show me the issues", "workspace_scope_id": DEFAULT_WORKSPACE,
                    })
                self.assertEqual(response.status_code, 200)
                # The comparison runs on the worker, after the response.
                self.assertTrue(controller.wait_until_idle(10))
                controller.counters.flush()
                counted = {row["class"] for row in shadow_parity.report(1) if row["field"] == TURN}
                self.assertTrue(counted & {COMPARED, OVER_BUDGET}, counted)
            finally:
                controller.close()

    def test_stale_and_cancelled_turns_are_not_compared(self):
        with HermeticDatabase() as hermetic:
            request = hermetic.request("stale", 1, "Show me the issues")
            visible = ProductDataStore().load(hermetic.grant.visible_scope_ids())
            for status in ("stale", "cancelled"):
                with self.subTest(status=status):
                    prepared = hermetic.runner.prepare(PRINCIPAL, hermetic.grant, PRODUCT_ID, visible, DEFAULT_WORKSPACE)
                    live = TurnResponse(session_id="stale", turn_id=1, status=status, speech="x",
                                        proposed_action=None, validated_action=None, intent_trace=IntentTrace())
                    self.assertEqual(hermetic.runner.complete(prepared, PRINCIPAL, request, live).turn_class,
                                     NOT_COMPARED)
            self.assertEqual(len(hermetic.runner.store), 0, "no memory advanced")


def view(action: dict | None, *, mutation: bool = True, open_: bool = True) -> ShadowView:
    return ShadowView(
        status="completed", action=action, action_is_mutation=mutation, lifecycle_open=open_,
        translation_missing=False, speech="Should I update LIN-142?", trace={}, signals=frozenset(),
        confidences={}, sources=(), documents=(), summary={}, declared={},
    )


def live_turn(action: dict | None, status: str = "completed") -> dict:
    return {"session_id": "s1", "turn_id": 1, "status": status, "speech": "Done.",
            "proposed_action": action, "validated_action": action, "intent_trace": {}, "signals": [],
            "retrieved_context": [], "session_summary": {}}


class ShadowRevision2RulesTest(unittest.TestCase):
    """Plan section 6.7: lifecycle equivalence, telemetry, locks and the single process."""

    UPDATE = {"type": "UPDATE_DEMO_ISSUE", "payload": {"issue_id": "LIN-142", "assignee": "Noah Patel"}}

    def test_lifecycle_equivalence(self):
        same = compare(live_turn(self.UPDATE), view(self.UPDATE), session_id="s1", turn_id=1)
        self.assertEqual((same["validated_action"], same["speech"]), (LIFECYCLE, "platform_wording"))
        for changed in ({"issue_id": "LIN-142", "assignee": "Maya Chen"},
                        {"issue_id": "LIN-137", "assignee": "Noah Patel"},
                        {"issue_id": "LIN-142", "assignee": "Noah Patel", "priority": "High"}):
            with self.subTest(payload=changed):
                other = view({"type": "UPDATE_DEMO_ISSUE", "payload": changed})
                classes = compare(live_turn(self.UPDATE), other, session_id="s1", turn_id=1)
                self.assertEqual((classes["validated_action"], classes["speech"]), (BEHAVIOUR, BEHAVIOUR))
        # A navigation that matches is a match, not a lifecycle difference.
        navigation = {"type": "OPEN_ISSUES", "payload": {}}
        self.assertEqual(compare(live_turn(navigation), view(navigation, mutation=False),
                                 session_id="s1", turn_id=1)["validated_action"], MATCH)
        # "Would execute" is credited only against its own turn's live action.
        executed = view(self.UPDATE, open_=False)
        self.assertEqual(compare(live_turn(self.UPDATE), executed, session_id="s1", turn_id=1)["validated_action"],
                         MATCH)
        self.assertEqual(compare(live_turn(None), executed, session_id="s1", turn_id=1)["validated_action"],
                         BEHAVIOUR)

    def test_live_responses_carry_no_executed_state(self):
        """5b adds the execution envelope, null by default; the status never says "executed"."""
        self.assertIsNone(TurnResponse.model_fields["execution"].default)
        statuses = set(TurnResponse.model_fields["status"].annotation.__args__)
        self.assertEqual(statuses, {"completed", "cancelled", "stale", "denied"})

    def test_a_null_execution_envelope_is_a_match(self):
        """Adding the field must not turn every production turn into a behaviour difference."""
        classes = compare({**live_turn(None), "execution": None}, view(None, mutation=False),
                          session_id="s1", turn_id=1)
        self.assertEqual(classes["execution"], MATCH)
        envelope = {"key": "k", "session_id": "s1", "turn_id": 1, "expires_at": "x"}
        live = {**live_turn(self.UPDATE), "execution": envelope}
        self.assertEqual(compare(live, view(self.UPDATE), session_id="s1", turn_id=1)["execution"], LIFECYCLE)
        self.assertEqual(compare(live, view(None, mutation=False), session_id="s1", turn_id=1)["execution"],
                         BEHAVIOUR)

    def test_eviction_leaves_the_lock_pool_unchanged(self):
        store = ShadowMemoryStore(limit=100)
        locks = store.locks
        for index in range(10_000):
            store.put(ShadowKey("t", "p", f"s-{index}", "u", None, 0),
                      ShadowEntry(ConversationMemory(), SignalHistory(), 1, time.monotonic()))
        self.assertEqual(len(store), 100)
        self.assertIs(store.locks, locks)
        self.assertEqual(len(store.locks), LOCK_STRIPES)

    def test_idle_entries_expire(self):
        clock = [0.0]
        store = ShadowMemoryStore(idle_seconds=10, clock=lambda: clock[0])
        key = ShadowKey("t", "p", "s", "u", None, 0)
        store.put(key, ShadowEntry(ConversationMemory(), SignalHistory(), 1, 0.0))
        clock[0] = 11.0
        self.assertIsNone(store.get(key))

    def test_counts_reach_the_table_only_on_flush_and_add_up(self):
        with HermeticDatabase():
            counters = ParityCounters()
            counters.add("t1", "p1", "def1", 1, {TURN: COMPARED, "status": MATCH})
            self.assertTrue(counters.pending())
            self.assertEqual(shadow_parity.report(1), [])
            self.assertEqual(counters.flush(), 2)
            self.assertFalse(counters.pending())
            counters.add("t1", "p1", "def1", 1, {TURN: COMPARED})
            counters.flush()
            rows = {(row["field"], row["class"]): row["count"] for row in shadow_parity.report(1)}
            self.assertEqual(rows, {("turn", COMPARED): 2, ("status", MATCH): 1})

    def test_concurrent_counts_during_flushes_are_never_lost_or_doubled(self):
        with HermeticDatabase():
            counters = ParityCounters()

            def add_many():
                for _ in range(500):
                    counters.add("t1", "p1", "def1", 1, {TURN: COMPARED})

            workers = [threading.Thread(target=add_many) for _ in range(4)]
            for worker in workers:
                worker.start()
            while any(worker.is_alive() for worker in workers):
                counters.flush()
            for worker in workers:
                worker.join()
            counters.flush()
            rows = {(row["field"], row["class"]): row["count"] for row in shadow_parity.report(1)}
            self.assertEqual(rows[("turn", COMPARED)], 2000)

    def test_a_failed_flush_is_logged_and_never_raises(self):
        counters = ParityCounters()
        counters.add("t1", "p1", "def1", 1, {TURN: COMPARED})
        with patch.object(shadow_parity, "get_connection", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("pixel.shadow", level="WARNING") as logs:
            self.assertEqual(counters.flush(), 0)
        self.assertIn("shadow_flush_failed", logs.output[0])
        self.assertFalse(counters.pending(), "failed counts are discarded, not retried on a request")

    def test_flushes_are_scheduled_at_most_once_a_minute(self):
        clock = [0.0]
        counters = ParityCounters(clock=lambda: clock[0])
        tasks: list = []
        counters.add("t1", "p1", "def1", 1, {TURN: COMPARED})
        self.assertTrue(counters.schedule(tasks.append))
        self.assertFalse(counters.schedule(tasks.append), "one flush at a time")
        with patch.object(shadow_parity, "get_connection", side_effect=sqlite3.OperationalError("x")):
            tasks[0]()
        counters.add("t1", "p1", "def1", 1, {TURN: COMPARED})
        clock[0] = 30.0
        self.assertFalse(counters.schedule(tasks.append), "not within a minute of the last")
        clock[0] = 61.0
        self.assertTrue(counters.schedule(tasks.append))

    def test_the_deployment_runs_one_process(self):
        dockerfile = (REPO_ROOT / "Dockerfile.api").read_text(encoding="utf-8")
        self.assertNotIn("--workers", dockerfile)
        self.assertNotRegex(dockerfile, r"gunicorn|WEB_CONCURRENCY")
        railway = json.loads((REPO_ROOT / "railway.json").read_text(encoding="utf-8"))
        replicas = railway.get("deploy", {}).get("numReplicas", 1)
        self.assertEqual(replicas, 1)
        self.assertNotIn("--workers", json.dumps(railway))


if __name__ == "__main__":
    unittest.main()
