"""Restore the legacy shared member demo between configured member sign-ins.

These records are shared only by explicitly configured organization-member demo logins. The
public web experience uses private visitor instances and never invokes this service.

This is the interim remedy, not isolation: when a visitor signs in to the demo, the shared data
has been changed, and nobody has used the demo for `PIXEL_DEMO_IDLE_RESET_MINUTES`, the seed is
restored before they start. Members who overlap still share one copy; this remains a compatibility
path for the older demo workflow, not a public-demo isolation mechanism.

It restores data, so it is off unless that setting is a positive number, and it is armed only when
the server starts (like the rate limits), so tests that never start the server are unaffected.
State is held in this process: after a restart the data is treated as changed, which at worst
costs one unnecessary restore.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from app.services.env import env_int

logger = logging.getLogger("pixel.demo")


class IdleDemoReset:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._idle_seconds = 0.0
        self._last_activity = clock()
        # Unknown at startup, so assume changed: the file may hold an earlier visitor's edits.
        self._changed = True

    def arm(self) -> None:
        """Read the setting when the server starts. Zero or unset keeps it off."""
        self._idle_seconds = max(env_int("PIXEL_DEMO_IDLE_RESET_MINUTES", 0), 0) * 60.0

    def disarm(self) -> None:
        self._idle_seconds = 0.0

    @property
    def armed(self) -> bool:
        return self._idle_seconds > 0

    def touched(self, *, changed: bool = False) -> None:
        """Someone is using the demo. `changed` when the request may alter shared records."""
        with self._lock:
            self._last_activity = self._clock()
            if changed:
                self._changed = True

    def restored(self) -> None:
        """An operator or administrator restored the seed some other way."""
        with self._lock:
            self._changed = False
            self._last_activity = self._clock()

    def before_sign_in(self, restore: Callable[[], object]) -> bool:
        """Restore the seed if the demo was changed and then left idle. True if it did."""
        if not self.armed:
            return False
        with self._lock:
            now = self._clock()
            idle = now - self._last_activity
            if not self._changed or idle < self._idle_seconds:
                self._last_activity = now
                return False
            restore()
            self._changed = False
            self._last_activity = now
        logger.info(json.dumps({"event": "demo_data_restored", "idle_seconds": round(idle)}))
        return True
