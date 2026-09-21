"""Milestone 3.2, Slice 5b step 1: the shadow scheduler (plan section 10 and the S3 tests).

The controller is driven with a scripted runner, so ordering, parking, admission limits, the
breaker, the watchdog and shutdown are tested as one state machine without a database or engine.
"""
from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import AuthUser  # noqa: E402
from app.schemas import IntentTrace, TurnResponse  # noqa: E402
from app.services import shadow as shadow_module  # noqa: E402
from app.services.shadow import (  # noqa: E402
    LOCK_STRIPES,
    PreparedShadow,
    ShadowController,
    ShadowEntry,
    ShadowKey,
    ShadowMemoryStore,
    ShadowRequest,
    ShadowResult,
)
from app.engine.memory import ConversationMemory  # noqa: E402
from app.engine.signals import SignalHistory  # noqa: E402
from app.services.shadow_parity import (  # noqa: E402
    CIRCUIT_OPEN, COMPARED, NOT_COMPARED, SHADOW_ERROR, SHED, TURN, TURN_REASON, WORKER_UNHEALTHY,
    ParityCounters,
)

PRINCIPAL = AuthUser(kind="member", user_id="u1", tenant_id="t1", role="org_admin")
GRANT = SimpleNamespace(demo_context=None)


class ScriptedRunner:
    """Stands in for `ShadowRunner`: records each comparison and the memory state it saw."""

    def __init__(self, clock=time.monotonic) -> None:
        self.counters = ParityCounters()
        self.store = ShadowMemoryStore()
        self.compared: list[tuple[str, int, str | None]] = []
        self.counted: list[tuple[str, str | None]] = []
        self.delay = 0.0
        self.fail = False
        self.block: threading.Event | None = None
        self.started = threading.Event()
        self.clock_offset = 0.0
        self._lock = threading.Lock()

    def prepare_or_raise(self, principal, grant, product_id, visible_data, scope_id=None):
        return PreparedShadow("d", {}, "scope", None, grant)

    def package_for(self, definition_id):
        return None

    def compare_turn(self, prepared, principal, request, live, key):
        self.started.set()
        if self.block is not None:
            self.block.wait()
        entry = self.store.get(key)
        reset = entry.reset_reason if entry is not None and entry.reset else None
        if self.delay:
            self.clock_offset += self.delay
        with self._lock:
            self.compared.append((key.session_id, request.turn_id, reset))
        if reset is None:
            self.store.put(key, ShadowEntry(ConversationMemory(), SignalHistory(), request.turn_id, time.monotonic()))
        if self.fail:
            return ShadowResult(SHADOW_ERROR, {})
        return ShadowResult(COMPARED, {})

    def count_turn(self, tenant_id, product_id, turn_class, *, reason=None, definition=("-", 0)):
        with self._lock:
            self.counted.append((turn_class, reason))
        return ShadowResult(turn_class, {}, None, 0.0, reason)


def request(session: str, turn: int) -> ShadowRequest:
    return ShadowRequest(session, turn, "p1", "hello", None, "w1")


def live(session: str, turn: int, *, status: str = "completed", entered: bool = True) -> TurnResponse:
    response = TurnResponse(session_id=session, turn_id=turn, status=status, speech="ok",
                            proposed_action=None, validated_action=None, intent_trace=IntentTrace())
    response._engine_entered = entered
    return response


def key(session: str) -> ShadowKey:
    return ShadowKey("t1", "p1", session, "u1", None, 0)


class ControllerFixture(unittest.TestCase):
    def controller(self, runner: ScriptedRunner | None = None, **options) -> ShadowController:
        self.runner = runner or ScriptedRunner()
        clock = options.pop("clock", None) or (lambda: time.monotonic() + self.runner.clock_offset)
        controller = ShadowController(self.runner, clock=clock, **options)
        self.addCleanup(controller.close)
        return controller

    def begin(self, controller, session: str, turn: int):
        return controller.begin(PRINCIPAL, GRANT, request(session, turn), {})

    def finish(self, controller, admitted, session: str, turn: int, **kwargs):
        controller.finish(admitted, request(session, turn), live(session, turn, **kwargs))


class SessionOrderTest(ControllerFixture):
    def test_a_higher_turn_queued_first_waits_for_the_lower_one(self):
        controller = self.controller()
        one, two = self.begin(controller, "s", 1), self.begin(controller, "s", 2)
        self.finish(controller, two, "s", 2)
        time.sleep(0.1)
        self.assertEqual(self.runner.compared, [], "turn 2 is parked while turn 1 is in flight")
        self.finish(controller, one, "s", 1)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(self.runner.compared, [("s", 1, None), ("s", 2, None)])

    def test_a_lower_turn_with_no_context_effect_releases_the_next_at_once(self):
        controller = self.controller()
        one, two = self.begin(controller, "s", 1), self.begin(controller, "s", 2)
        self.finish(controller, two, "s", 2)
        started = time.monotonic()
        self.finish(controller, one, "s", 1, status="stale", entered=False)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertLess(time.monotonic() - started, 1.0, "no gap wait for a turn proven harmless")
        self.assertEqual(self.runner.compared, [("s", 2, None)])
        self.assertIn((NOT_COMPARED, "no_context_effect"), self.runner.counted)

    def test_a_session_whose_first_turn_had_no_effect_continues_normally(self):
        controller = self.controller()
        self.finish(controller, self.begin(controller, "s", 1), "s", 1, status="denied", entered=False)
        self.finish(controller, self.begin(controller, "s", 2), "s", 2)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(self.runner.compared, [("s", 2, None)])

    def test_a_lower_turn_that_entered_the_engine_but_was_discarded_resets_the_session(self):
        controller = self.controller()
        one, two = self.begin(controller, "s", 1), self.begin(controller, "s", 2)
        self.finish(controller, two, "s", 2)
        self.finish(controller, one, "s", 1, status="stale", entered=True)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(self.runner.compared, [("s", 2, "lost")])

    def test_a_gap_that_never_fills_resets_the_session_and_a_late_turn_is_absorbed(self):
        controller = self.controller(gap_seconds=0.2)
        one, two = self.begin(controller, "s", 1), self.begin(controller, "s", 2)
        self.finish(controller, two, "s", 2)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(self.runner.compared, [("s", 2, "gap_lost")])
        # Turn 1 finally arrives: it is compared against the reset session, never rewinding memory.
        self.finish(controller, one, "s", 1)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(self.runner.compared[-1], ("s", 1, "gap_lost"))

    def test_a_parked_session_never_blocks_another_session(self):
        controller = self.controller(gap_seconds=5)
        a1, a2 = self.begin(controller, "a", 1), self.begin(controller, "a", 2)
        self.finish(controller, a2, "a", 2)
        self.finish(controller, self.begin(controller, "b", 1), "b", 1)
        deadline = time.monotonic() + 3
        while ("b", 1, None) not in self.runner.compared and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.runner.compared, [("b", 1, None)], "session b ran while a was parked")
        self.finish(controller, a1, "a", 1)
        self.assertTrue(controller.wait_until_idle(5))

    def test_sessions_are_served_round_robin(self):
        runner = ScriptedRunner()
        runner.block = threading.Event()
        controller = self.controller(runner)
        self.finish(controller, self.begin(controller, "gate", 1), "gate", 1)
        runner.started.wait(2)
        for turn in (1, 2, 3):
            self.finish(controller, self.begin(controller, "a", turn), "a", turn)
        self.finish(controller, self.begin(controller, "b", 1), "b", 1)
        runner.block.set()
        self.assertTrue(controller.wait_until_idle(5))
        order = [session for session, _, _ in runner.compared[1:]]
        self.assertLess(order.index("b"), 3, "session b is not starved behind all of a's turns")


class AdmissionTest(ControllerFixture):
    def test_count_limit_sheds_and_never_leaks_accounting(self):
        runner = ScriptedRunner()
        runner.block = threading.Event()
        controller = self.controller(runner, max_items=2)
        self.finish(controller, self.begin(controller, "x", 1), "x", 1)
        runner.started.wait(2)
        self.finish(controller, self.begin(controller, "y", 1), "y", 1)
        self.finish(controller, self.begin(controller, "z", 1), "z", 1)
        self.finish(controller, self.begin(controller, "w", 1), "w", 1)
        self.assertIn((SHED, "queue_full"), runner.counted)
        runner.block.set()
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(controller.queue_state(), (0, 0))
        self.assertEqual(runner.store.get(key("w")).reset_reason, "queue_full")

    def test_item_and_total_byte_limits_shed_with_their_reason(self):
        controller = self.controller(max_item_bytes=10)
        self.finish(controller, self.begin(controller, "s", 1), "s", 1)
        self.assertIn((SHED, "snapshot_too_large"), self.runner.counted)
        self.assertEqual(controller.queue_state(), (0, 0))
        runner = ScriptedRunner()
        runner.block = threading.Event()
        controller = self.controller(runner, max_bytes=1200)
        self.finish(controller, self.begin(controller, "a", 1), "a", 1)
        runner.started.wait(2)
        self.finish(controller, self.begin(controller, "b", 1), "b", 1)
        self.finish(controller, self.begin(controller, "c", 1), "c", 1)
        self.assertIn((SHED, "queue_full"), runner.counted)
        runner.block.set()
        self.assertTrue(controller.wait_until_idle(5))
        self.assertEqual(controller.queue_state(), (0, 0))

    def test_too_many_turns_in_flight_for_one_session_are_shed(self):
        controller = self.controller(max_in_flight=2)
        tickets = [self.begin(controller, "s", turn) for turn in (1, 2, 3)]
        self.assertIsNone(tickets[2])
        self.assertIn((SHED, "too_many_in_flight"), self.runner.counted)
        self.assertEqual(self.runner.store.get(key("s")).reset_reason, "too_many_in_flight")

    def test_enqueue_never_waits_for_the_worker(self):
        runner = ScriptedRunner()
        runner.block = threading.Event()
        controller = self.controller(runner)
        self.finish(controller, self.begin(controller, "gate", 1), "gate", 1)
        runner.started.wait(2)
        started = time.monotonic()
        for turn in range(1, 30):
            self.finish(controller, self.begin(controller, f"s{turn}", 1), f"s{turn}", 1)
        self.assertLess(time.monotonic() - started, 1.0)
        runner.block.set()
        self.assertTrue(controller.wait_until_idle(10))


class BreakerAndWatchdogTest(ControllerFixture):
    def test_slow_comparisons_open_the_breaker_then_one_trial_closes_it(self):
        runner = ScriptedRunner()
        runner.delay = shadow_module.SLOW_SECONDS + 0.05
        controller = self.controller(runner)
        for turn in range(1, shadow_module.SLOW_LIMIT + 1):
            self.finish(controller, self.begin(controller, f"s{turn}", 1), f"s{turn}", 1)
            self.assertTrue(controller.wait_until_idle(5))
        self.assertIsNone(self.begin(controller, "blocked", 1))
        self.assertIn((CIRCUIT_OPEN, "circuit_open"), runner.counted)
        self.assertEqual(runner.store.get(key("s1")).reset_reason, "circuit_open", "memory is not resumed")
        runner.delay = 0.0
        runner.clock_offset += shadow_module.OPEN_SECONDS + 1
        trial = self.begin(controller, "trial", 1)
        self.assertIsNotNone(trial)
        self.assertIsNone(self.begin(controller, "second", 1), "exactly one trial is admitted")
        self.finish(controller, trial, "trial", 1)
        self.assertTrue(controller.wait_until_idle(5))
        self.assertIsNotNone(self.begin(controller, "after", 1), "a fast trial closes the breaker")

    def test_errors_open_the_breaker(self):
        runner = ScriptedRunner()
        runner.fail = True
        controller = self.controller(runner)
        for turn in range(1, shadow_module.ERROR_LIMIT + 1):
            self.finish(controller, self.begin(controller, f"s{turn}", 1), f"s{turn}", 1)
            self.assertTrue(controller.wait_until_idle(5))
        self.assertIsNone(self.begin(controller, "blocked", 1))

    def test_breaker_state_is_consistent_under_concurrent_requests(self):
        runner = ScriptedRunner()
        runner.delay = shadow_module.SLOW_SECONDS + 0.05
        controller = self.controller(runner)
        for turn in range(1, shadow_module.SLOW_LIMIT + 1):
            self.finish(controller, self.begin(controller, f"s{turn}", 1), f"s{turn}", 1)
            self.assertTrue(controller.wait_until_idle(5))
        runner.delay = 0.0
        runner.clock_offset += shadow_module.OPEN_SECONDS + 1
        admitted: list = []
        barrier = threading.Barrier(16)

        def attempt(index: int) -> None:
            barrier.wait()
            turn = self.begin(controller, f"c{index}", 1)
            if turn is not None:
                admitted.append(turn)

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(admitted), 1, "concurrent requests admit exactly one trial")

    def test_a_hung_comparison_latches_the_shadow_off_without_delaying_anything(self):
        runner = ScriptedRunner()
        runner.block = threading.Event()
        controller = self.controller(runner, hang_seconds=0.3, watchdog_interval=0.05)
        self.finish(controller, self.begin(controller, "hung", 1), "hung", 1)
        runner.started.wait(2)
        self.finish(controller, self.begin(controller, "queued", 1), "queued", 1)
        deadline = time.monotonic() + 3
        while controller.healthy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(controller.healthy)
        self.assertIn((SHED, "worker_unhealthy"), runner.counted, "queued work is drained")
        items, size = controller.queue_state()
        self.assertEqual(items, 1, "only the hung comparison is still charged, exactly once")
        started = time.monotonic()
        self.assertIsNone(self.begin(controller, "later", 1))
        self.assertLess(time.monotonic() - started, 0.1, "the request path never waits")
        self.assertIn((WORKER_UNHEALTHY, "worker_unhealthy"), runner.counted)
        threads_before = threading.active_count()
        started = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - started, 1.5, "shutdown is never blocked by a stuck worker")
        self.assertLessEqual(threading.active_count(), threads_before, "no replacement worker was started")
        runner.block.set()
        deadline = time.monotonic() + 3
        while controller.queue_state() != (0, 0) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(controller.queue_state(), (0, 0), "the charge is released when it returns")

    def test_shutdown_sheds_queued_work(self):
        runner = ScriptedRunner()
        runner.block = threading.Event()
        controller = self.controller(runner)
        self.finish(controller, self.begin(controller, "a", 1), "a", 1)
        runner.started.wait(2)
        self.finish(controller, self.begin(controller, "b", 1), "b", 1)
        controller.close()
        self.assertIn((SHED, "shutdown"), runner.counted)
        self.assertIsNone(self.begin(controller, "c", 1), "no admission after shutdown")
        runner.block.set()


class ResetRulesTest(unittest.TestCase):
    def test_a_new_epoch_resets_every_session(self):
        runner = ScriptedRunner()
        runner.store.put(key("s"), ShadowEntry(ConversationMemory(), SignalHistory(), 3, time.monotonic()))
        runner.epoch_started = None
        controller = ShadowController(runner, start=False)
        from datetime import UTC, datetime
        controller.new_epoch(datetime.now(UTC))
        self.assertEqual(runner.store.get(key("s")).reset_reason, "epoch")

    def test_evicted_and_expired_entries_leave_tombstones(self):
        clock = [0.0]
        store = ShadowMemoryStore(limit=2, idle_seconds=10, clock=lambda: clock[0])
        for session in ("a", "b", "c"):
            store.put(key(session), ShadowEntry(ConversationMemory(), SignalHistory(), 1, clock[0]))
        self.assertTrue(store.tombstoned(key("a")), "evicted")
        clock[0] = 11
        self.assertIsNone(store.get(key("b")))
        self.assertTrue(store.tombstoned(key("b")), "expired")
        self.assertFalse(store.tombstoned(key("never")))
        self.assertEqual(len(store.locks), LOCK_STRIPES)

    def test_tombstones_are_bounded(self):
        store = ShadowMemoryStore(limit=1, tombstones=5)
        for index in range(50):
            store.put(key(f"s{index}"), ShadowEntry(ConversationMemory(), SignalHistory(), 1, time.monotonic()))
        self.assertLessEqual(len(store._tombstones), 5)


if __name__ == "__main__":
    unittest.main()
